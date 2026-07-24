"""contract.py —— 8 workflow 的静态契约校验（纯 Python，不驱动引擎）。

**为什么单独抽模块**：Stage 3 的契约校验（inputs / output_schema 链 / Jinja 残留引用 /
chart 标签 / 造假扫描）是确定性逻辑，应独立于「是否真启动 orca」跑。抽成纯函数后，可在
pytest parametrize 里逐 workflow 断言，也可被 headless harness 复用做 bootstrap 前预检。

**契约规则**（逐条对应任务 §1 结构/契约验证清单）：

1. **inputs 解析 + 节点图合法**：``load_workflow`` 成功（compile validator 已校验 entry/
   routes/name 唯一/死锁等；SPEC §2.4）。
2. **output_schema 链不破**：每个 ``{{ X.output.Y }}`` 引用（yaml ``outputs`` + node
   ``routes.when`` + node prompt/agent.md）的 ``Y`` 必在节点 ``X`` 的 output_schema
   properties 中（或 schema 为 None=自由文本则跳过 Y 校验）。
3. **无 ``{{ inputs.X }}`` 残留引用已删 input**：任何 inputs 引用的 key 必在
   ``wf.inputs`` 声明。
4. **device/target_hardware/seed 在**（quant/nas 系）：对应 input key 存在。
5. **chart 推图调用有标签**：axis-bearing（line/bar/scatter/heatmap/area）必传 x_label+
   y_label+caption；table 必传 caption（无轴）。仅检 active-path 脚本。
6. **无造假兜底**：active-path 脚本 + agent.md 不含 ``torch.randn`` / ``torch.rand`` /
   ``fake_data`` / ``dummy_calib``（spike sentinel.looks_fabricated 同口径）。

**依赖单向**：``orca.compile.parser`` + ``orca.schema.workflow``（纯数据读）+ stdlib
（ast/glob/re）。不 import run/exec/events/iface。

**fail loud**：所有 check 返回 ``list[Finding]``（空=pass），由测试层把 Finding 转成
assertion。check 内部不静默吞错——schema 解析失败直接 raise。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from orca.compile.parser import load_workflow
from orca.schema.workflow import AgentNode, ForeachNode, Workflow

REPO = Path(__file__).resolve().parents[2]
WF_DIR = REPO / "workflows"
AGENTS_DIR = WF_DIR / "agents"


def _rel(path: Path) -> str:
    """路径相对 REPO 的显示串；不在 REPO 下（如测试 tmp_path）退到绝对路径。

    防 ``relative_to`` 在路径不在 REPO 子树时 raise ValueError（planted-fixture 单测会
    把临时 .py 文件放 /tmp 下）。
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)

# 8 workflow → yaml 文件名（orca list 注册名与 yaml stem 一致）。
WORKFLOWS: dict[str, str] = {
    "agent-struct-exploration": "agent-struct-exploration.yaml",
    "kd-nas": "kd-nas.yaml",
    "nas-agent-pipeline": "nas-agent-pipeline.yaml",
    "nas-hp-search": "nas-hp-search.yaml",
    "quant-bit-curve": "quant-bit-curve.yaml",
    "quant-ptq-sweep": "quant-ptq-sweep.yaml",
    "quant-qat": "quant-qat.yaml",
    "quant-sensitivity": "quant-sensitivity.yaml",
}

# 每个 workflow 的 [ask]/必填硬件类 input 期望（任务 §1「device/target_hardware/seed 在」）。
# quant 系用 target_hardware；struct/kd 用 device；nas 用 target_hardware。
# seed 所有 wf 都有。这些是 P5/P6/P9 收敛后的契约（见 docs/specs/workflow-input-design-principle.md）。
HARDWARE_INPUT_EXPECTED: dict[str, set[str]] = {
    "agent-struct-exploration": {"device", "seed"},
    "kd-nas": {"device", "seed"},
    "nas-agent-pipeline": {"target_hardware", "seed"},
    "nas-hp-search": {"target_hardware", "seed"},
    "quant-bit-curve": {"target_hardware", "seed"},
    "quant-ptq-sweep": {"target_hardware", "seed"},
    "quant-qat": {"target_hardware", "seed"},
    "quant-sensitivity": {"target_hardware", "seed"},
}

# render_chart 调用名（viz_struct/viz_kd 用别名 _orca_render_chart）。
_RENDER_CHART_NAMES = {"render_chart", "_orca_render_chart"}
# axis-bearing chart type：必传 x_label+y_label+caption（有轴）。
_AXIS_BEARING_TYPES = {"line", "bar", "scatter", "heatmap", "area", "histogram", "box", "violin"}
_LABEL_KEYS = {"x_label", "y_label", "caption"}

# 造假扫描禁词（spike sentinel.looks_fabricated 同口径 + agent.md 严禁造假段落）。
# 分两类：
#   - 无歧义造假标记（任何出现都 = 造假兜底）：fake_data / dummy_calib
#   - 有合法用途的词（需上下文判定）：torch.randn / torch.rand（ONNX dummy input /
#     KD proxy dataset / smoke generator / docstring 都合法）
_UNAMBIGUOUS_FABRICATION = (
    re.compile(r"\bfake_data\b"),
    re.compile(r"\bdummy_calib\b"),
)
# torch.randn/torch.rand 的「合法用途」上下文标记（函数名/docstring 含以下词 → 视为合法）：
# smoke generator / KD proxy dataset / ONNX dummy input / 显式 _dummy / docstring 示例。
_LEGIT_RAND_CONTEXT = (
    "smoke", "dummy", "proxy", "materialize", "rand_input", "fake_input",
    "fallback_shape", "_dummy", "dummy_input",
)
# agent.md「严禁造假」正向存在标记（P5/P6 prompt-layer guard 契约）。
_PROHIBITION_MARKERS = ("绝不", "严禁", "不要造假", "禁止造假")

# ``{{ inputs.X }}`` / ``{{ inputs['X'] }}`` 引用提取（只抓 inputs 命名空间）。
_INPUT_REF_RE = re.compile(r"{{\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)")
# ``{{ <node>.output.<field> }}`` 引用提取（跨节点 output_schema 链）。
_NODE_OUTPUT_REF_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\.output\.([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Finding:
    """单条契约违例。测试层把同一 workflow 的 Finding 列表转成 assertion。"""

    check: str        # 哪条 check（"input_refs" / "schema_chain" / "chart_labels" / ...）
    location: str     # 文件:行 或 node 名
    detail: str       # 人话描述


def load_parsed(wf_name: str) -> Workflow:
    """加载并 compile-validate workflow（compile validator 内嵌于 load_workflow）。"""
    return load_workflow(WF_DIR / WORKFLOWS[wf_name])


def active_agent_names(wf_name: str) -> set[str]:
    """该 workflow 实际引用的 agent 名（active path；folder-agent = ``agents/<name>/``）。"""
    wf = load_parsed(wf_name)
    names: set[str] = set()
    for node in wf.nodes:
        if isinstance(node, AgentNode) and node.agent:
            names.add(node.agent)
        elif isinstance(node, ForeachNode) and isinstance(node.body, AgentNode) and node.body.agent:
            names.add(node.body.agent)
    return names


def active_script_files(wf_name: str) -> list[Path]:
    """该 workflow active-path 的所有 ``.py`` 脚本（folder-agent scripts/ + 共享 _xxx_scripts/）。

    folder-agent（如 ``ptq-sweeper/``）自带 ``scripts/``；共享脚本目录（``_kd_scripts/``
    / ``_struct_scripts/``）在 agent.md 里被引用——这里把 agents/<active-agent>/scripts/
    与（若 agent 是 struct/kd 系）对应共享目录一并返回。
    """
    agents = active_agent_names(wf_name)
    files: list[Path] = []
    seen: set[Path] = set()
    for name in sorted(agents):
        scripts_dir = AGENTS_DIR / name / "scripts"
        if scripts_dir.is_dir():
            for py in sorted(scripts_dir.rglob("*.py")):
                if py not in seen:
                    seen.add(py)
                    files.append(py)
    # struct/kd 系共享脚本目录（agent-struct-exploration / kd-nas 专用）。
    if wf_name == "agent-struct-exploration":
        shared = AGENTS_DIR / "_struct_scripts"
        for py in sorted(shared.rglob("*.py")):
            if py not in seen:
                seen.add(py)
                files.append(py)
    elif wf_name == "kd-nas":
        shared = AGENTS_DIR / "_kd_scripts"
        for py in sorted(shared.rglob("*.py")):
            if py not in seen:
                seen.add(py)
                files.append(py)
    return files


def active_agent_md_files(wf_name: str) -> list[Path]:
    """该 workflow active-path 的所有 agent.md（folder-agent 入口）。"""
    agents = active_agent_names(wf_name)
    files: list[Path] = []
    for name in sorted(agents):
        md = AGENTS_DIR / name / "agent.md"
        if md.is_file():
            files.append(md)
    return files


# ── check 1+3: inputs / 残留引用 ────────────────────────────────────────────────


def check_no_undeclared_input_refs(wf_name: str) -> list[Finding]:
    """``{{ inputs.X }}`` 引用的 X 必在 ``wf.inputs`` 声明（防已删 input 残留引用）。"""
    wf = load_parsed(wf_name)
    declared = set((wf.inputs or {}).keys())
    findings: list[Finding] = []
    # yaml 文件全文 + active agent.md 全文都扫
    yaml_text = (WF_DIR / WORKFLOWS[wf_name]).read_text(encoding="utf-8")
    for src, label in [(yaml_text, f"{WORKFLOWS[wf_name]}")]:
        for m in _INPUT_REF_RE.finditer(src):
            key = m.group(1)
            if key not in declared:
                findings.append(Finding(
                    check="input_refs", location=label,
                    detail=f"引用未声明的 inputs.{key}（已删 input 残留引用）",
                ))
    for md in active_agent_md_files(wf_name):
        text = md.read_text(encoding="utf-8")
        for m in _INPUT_REF_RE.finditer(text):
            key = m.group(1)
            if key not in declared:
                rel = _rel(md)
                findings.append(Finding(
                    check="input_refs", location=str(rel),
                    detail=f"引用未声明的 inputs.{key}（已删 input 残留引用）",
                ))
    return _dedupe(findings)


def check_hardware_inputs_present(wf_name: str) -> list[Finding]:
    """quant/nas/struct-kd 系的 device/target_hardware/seed input 必须存在。"""
    wf = load_parsed(wf_name)
    actual = set((wf.inputs or {}).keys())
    expected = HARDWARE_INPUT_EXPECTED.get(wf_name, set())
    missing = expected - actual
    return [
        Finding(check="hardware_inputs", location=WORKFLOWS[wf_name],
                detail=f"缺 input {sorted(missing)}（P5/P6/P9 设备/种子契约）")
        for _ in [0] if missing
    ]


# ── check 2: output_schema 链不破 ───────────────────────────────────────────────


def _node_schema_properties(wf: Workflow) -> dict[str, set[str] | None]:
    """node 名 → output_schema properties 键集合（None=自由文本节点，不校验 Y）。"""
    result: dict[str, set[str] | None] = {}
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            schema = node.output_schema
            if schema is None:
                result[node.name] = None
            else:
                props = (schema.get("properties") or {}).keys()
                result[node.name] = set(props)
    return result


def check_output_schema_chain(wf_name: str) -> list[Finding]:
    """``{{ X.output.Y }}`` 引用的 Y 必在节点 X 的 output_schema properties 中。

    扫 yaml 全文（含 outputs / routes.when / node 内联 prompt）。X 不在节点表 / X.schema=None
    （自由文本，Y 不可校验）→ 跳过；Y 不在 X 的 properties → finding。
    """
    wf = load_parsed(wf_name)
    schema_props = _node_schema_properties(wf)
    node_names = set(schema_props.keys())
    yaml_text = (WF_DIR / WORKFLOWS[wf_name]).read_text(encoding="utf-8")
    findings: list[Finding] = []
    for m in _NODE_OUTPUT_REF_RE.finditer(yaml_text):
        node_x, field_y = m.group(1), m.group(2)
        if node_x == "inputs":  # 防御性：inputs 不带 .output. 命名空间，当前正则永不命中
            continue
        if node_x not in node_names:
            # 引用了未知节点——compile validator 一般已拦，但 routes/foreach 可能漏
            findings.append(Finding(
                check="schema_chain", location=WORKFLOWS[wf_name],
                detail=f"引用未知节点 {node_x}.output.{field_y}",
            ))
            continue
        props = schema_props[node_x]
        if props is None:
            continue  # 自由文本节点，Y 不可校验
        if field_y not in props:
            findings.append(Finding(
                check="schema_chain", location=WORKFLOWS[wf_name],
                detail=f"引用 {node_x}.output.{field_y} 不在其 output_schema properties "
                       f"({sorted(props)})",
            ))
    return _dedupe(findings)


# ── check 5: chart 标签 ─────────────────────────────────────────────────────────


def _chart_type_of(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "chart_type" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def check_chart_labels(wf_name: str) -> list[Finding]:
    """active-path 脚本的 render_chart 调用标签契约。

    - axis-bearing（line/bar/scatter/heatmap/...）：x_label+y_label+caption 必显式传。
    - table：caption 必传（无轴）。
    """
    findings: list[Finding] = []
    for py in active_script_files(wf_name):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as e:
            findings.append(Finding(
                check="chart_labels", location=_rel(py),
                detail=f"脚本语法错无法 AST 解析: {e}",
            ))
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in _RENDER_CHART_NAMES):
                continue
            kws = {kw.arg for kw in node.keywords if kw.arg}
            missing = _LABEL_KEYS - kws
            if not missing:
                continue
            ct = _chart_type_of(node)
            if ct == "table":
                # table 只要求 caption（无轴）
                if "caption" in missing:
                    findings.append(Finding(
                        check="chart_labels",
                        location=f"{_rel(py)}:{node.lineno}",
                        detail=f"table render_chart 缺 caption（missing={sorted(missing)}）",
                    ))
                continue
            if ct in _AXIS_BEARING_TYPES:
                findings.append(Finding(
                    check="chart_labels",
                    location=f"{_rel(py)}:{node.lineno}",
                    detail=f"{ct} render_chart 缺标签（missing={sorted(missing)}；"
                           f"axis-bearing 必传 x_label+y_label+caption）",
                ))
                continue
            # 未知 chart_type（含 None）→ 要求 caption 至少在（保守）
            if "caption" in missing:
                findings.append(Finding(
                    check="chart_labels",
                    location=f"{_rel(py)}:{node.lineno}",
                    detail=f"render_chart (type={ct!r}) 缺 caption（missing={sorted(missing)}）",
                ))
    return findings


# ── check 6: 造假扫描（上下文感知） ─────────────────────────────────────────────


def check_no_fabrication(wf_name: str) -> list[Finding]:
    """active-path 脚本 + agent.md 无造假兜底（上下文感知，零误报）。

    **分层**（避免 spike 的 ``looks_fabricated``（针对 agent output）直接套源码产生的误报）：
    1. ``fake_data`` / ``dummy_calib``：无歧义造假标记，任何出现都 = finding。
    2. ``torch.randn`` / ``torch.rand``：仅当**不在合法上下文**时才 finding。合法上下文：
       - .py：``torch.randn`` 出现在函数名或 docstring 含 ``smoke``/``dummy``/``proxy``/
         ``materialize``/``dummy_input`` 等标记的函数内（smoke generator / KD proxy
         dataset / ONNX dummy input），或在注释/docstring 行。
       - .md：torch.randn 几乎只在「严禁造假」prohibition 段落或 ``` 围栏里出现——
         本 check 不扫 .md 的 torch.randn（prohibition 段落由正向存在 check 覆盖）。
    3. 正向存在 check（``check_fabrication_prohibition_present``）：quant/nas 系 agent.md
       必须含「严禁造假」prohibition 段落（P5/P6 prompt-layer guard 契约）。
    """
    findings: list[Finding] = []
    # (a) 无歧义标记：所有 .py + .md
    for path in active_script_files(wf_name) + active_agent_md_files(wf_name):
        text = path.read_text(encoding="utf-8")
        rel = _rel(path)
        for pat in _UNAMBIGUOUS_FABRICATION:
            for m in pat.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append(Finding(
                    check="fabrication", location=f"{rel}:{line_no}",
                    detail=f"命中无歧义造假标记 {pat.pattern!r}",
                ))
    # (b) torch.randn 仅在 .py 检查、且上下文感知
    for path in active_script_files(wf_name):
        findings.extend(_check_rand_in_py(path))
    return _dedupe(findings)


def _check_rand_in_py(path: Path) -> list[Finding]:
    """``.py`` 里 ``torch.randn``/``torch.rand`` 的上下文感知检查。

    用 AST 找每个 ``Call(torch.randn/torch.rand)`` 的 enclosing 函数：函数名 + docstring
    含合法上下文标记 → 跳过；否则 finding。注释/独立 docstring 行的 randn 不构成 Call，
    AST 自然不命中（故 docstring 里 ``torch.randn`` 字面量不会误报）。
    """
    findings: list[Finding] = []
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except SyntaxError:
        return findings
    rel = _rel(path)
    # 建 lineno → enclosing 函数（名 + docstring）映射。O(N²)（每个 FunctionDef 各 ast.walk
    # 一遍），workflow 脚本 <500 行无感；若未来脚本显著增大，改单次 walk + 函数栈维护。
    func_context: dict[int, tuple[str, str]] = {}  # call_lineno → (func_name, docstring)
    for func in _walk_functions(tree):
        fname = func.name
        doc = ast.get_docstring(func) or ""
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                func_context[node.lineno] = (fname, doc)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func
        # 匹配 torch.randn / torch.rand（attr.value 不深究——名约定）
        if not (isinstance(attr.value, ast.Name) and attr.value.id in ("torch",)
                and attr.attr in ("randn", "rand")):
            continue
        ctx = func_context.get(node.lineno)
        if ctx and _is_legit_rand_context(ctx[0], ctx[1]):
            continue
        findings.append(Finding(
            check="fabrication", location=f"{rel}:{node.lineno}",
            detail=f"torch.{attr.attr} 在非合法上下文（非 smoke/dummy/proxy/materialize）——"
                   f"疑似 production-path 造假兜底",
        ))
    return findings


def _walk_functions(tree: ast.AST):
    """遍历所有 FunctionDef / AsyncFunctionDef（含嵌套）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _is_legit_rand_context(func_name: str, docstring: str) -> bool:
    """函数名或 docstring 含合法上下文标记 → 视为 smoke/proxy/dummy 用途（非造假）。"""
    haystack = (func_name + " " + docstring).lower()
    return any(marker in haystack for marker in _LEGIT_RAND_CONTEXT)


# ── check 7: 严禁造假 prohibition 正向存在（prompt-layer guard 契约） ─────────


# 需含 prohibition 段落的 workflow（quant/nas/struct-kd 系——P5/P6/P7 prompt-layer guard）。
_PROHIBITION_REQUIRED_WF = {
    "quant-ptq-sweep", "quant-sensitivity", "quant-qat", "quant-bit-curve",
    "nas-agent-pipeline", "nas-hp-search", "kd-nas", "agent-struct-exploration",
}


def check_fabrication_prohibition_present(wf_name: str) -> list[Finding]:
    """quant/nas/struct-kd 系 **至少一个** active agent.md 含「严禁造假」prohibition。

    这是 P5/P6/P7 落地的 prompt-layer guard 契约。语义：workflow 级至少一个 agent（通常是
    读数据 loader / 测精度的那个）声明「绝不 torch.randn 造假 / 缺数据走哨兵」即可，不要求
    每个 agent 都有（如 nas-select 只从 search records 选架构、supernet-train-script 生成
    脚本模板——它们不直接 touch calib/eval loader，无造假风险，不强求）。
    """
    if wf_name not in _PROHIBITION_REQUIRED_WF:
        return []
    mds = active_agent_md_files(wf_name)
    if not mds:
        return []  # 无 agent.md（如纯 inline prompt workflow）——不强制
    has_any = any(
        any(marker in md.read_text(encoding="utf-8") for marker in _PROHIBITION_MARKERS)
        for md in mds
    )
    if not has_any:
        return [Finding(
            check="prohibition_present",
            location=WORKFLOWS[wf_name],
            detail=f"workflow {wf_name} 无任何 active agent.md 含「严禁造假」prohibition"
                   f"（P5/P6/P7 prompt-layer guard 契约）",
        )]
    return []


# ── aggregate ──────────────────────────────────────────────────────────────────


def all_checks(wf_name: str) -> list[Finding]:
    """跑该 workflow 的全部静态契约 check，返回聚合 finding 列表（空=全 pass）。"""
    findings: list[Finding] = []
    for check_fn in (
        check_no_undeclared_input_refs,
        check_hardware_inputs_present,
        check_output_schema_chain,
        check_chart_labels,
        check_no_fabrication,
        check_fabrication_prohibition_present,
    ):
        findings.extend(check_fn(wf_name))
    return findings


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """同 (check, location, detail) 去重（agent.md 可能被多 node 引用）。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.check, f.location, f.detail)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
