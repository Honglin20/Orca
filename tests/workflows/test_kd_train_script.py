"""test_kd_train_script.py —— kd-train-script agent 关键不变量测试。

覆盖：
- folder-agent 结构契约（agent.md / SKILL.md / generation workflow / 2 个
  checklist 文件齐全 + 关键不变量文本）
- 参考骨架模板 ``references/templates/train_pipeline.py`` 静态校验
  （py_compile / --help / stable base CLI / 无 distributed+sandwich 残留 /
  用 kd 库 / mode dispatch 顺序）
- 功能 smoke：teacher 模式（**未特化骨架 fail loud** + 特化产物跑真搬入逻辑）
  + distill 模式（端到端：先构造 teacher_cache.pt → distill 跑通）
- 测试侧特化 helper（``_specialize_skeleton`` 程序化填 slot，替代已删的
  ``--user_train_import`` 注入）
- fidelity_check.py（Layer 3 数值级等价性）对 demo fixture PASS
- fail-loud 路径（teacher 缺 --model_path / distill 缺 --student_model_path
  / --teacher_cache / slot 未填 NotImplementedError / 空 dataloader）

不依赖 GPU（全 CPU），不嵌入 workflow yaml。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO / "workflows" / "agents" / "kd-train-script"
SKILL_MD = AGENT_DIR / "SKILL.md"
AGENT_MD = AGENT_DIR / "agent.md"
GEN_WORKFLOW = AGENT_DIR / "references" / "workflows" / "train_pipeline_script_generation.md"
CHECKLIST_TRAINING = (
    AGENT_DIR / "references" / "workflow-checklists" /
    "train_pipeline_script_generation" / "01_training.md"
)
CHECKLIST_CLI = (
    AGENT_DIR / "references" / "workflow-checklists" /
    "train_pipeline_script_generation" / "02_cli.md"
)
TEMPLATE = AGENT_DIR / "references" / "templates" / "train_pipeline.py"
FIDELITY_CHECK = AGENT_DIR / "scripts" / "fidelity_check.py"

# 测试输入（真实存在的契约文件）
KD_SCRIPTS_DIR = REPO / "workflows" / "agents" / "_kd_scripts"
# 旧 teacher_model.py（10 层 t1/t2 交替）已删（2026-08-04 cleanup §3）——
# 活跃 teacher 来自 teacher-gen 产物；此处的 train_pipeline script 测试只需一个
# 「exposes build_model/DUMMY_INPUT/BUILD_FN 的 contract .py」作 --model_path 占位，
# 复用 receiver KB 的 spt_alt.py（同 contract shape [1,4,48,64,1]，KD feature hooks 恒 2）。
USER_TRAIN_PY = REPO / "examples" / "kd-nas-demo" / "train.py"
USER_EVAL_PY = REPO / "examples" / "kd-nas-demo" / "test_student.py"
STUDENT_VARIANT = REPO / "knowledge_base" / "families" / "receiver" / "spt_alt.py"
TEACHER_MODEL = STUDENT_VARIANT  # contract .py 占位（teacher-gen wrapper 同款形状）
# spt_alt.py 依赖同目录 _model8_blocks——直接 importlib 加载前需把 receiver dir 入 sys.path。
# subprocess 调用（train_pipeline.py 等）会自管 sys.path，无需此 setup。
_RECEIVER_DIR = str(STUDENT_VARIANT.parent)
if _RECEIVER_DIR not in sys.path:
    sys.path.insert(0, _RECEIVER_DIR)

# Stable base CLI（generation workflow §1）—— train_pipeline.py 必须全部暴露。
# 4 个 --user_* 覆盖 flag 已删（占位符体系随骨架化移除，无运行时注入）。
STABLE_BASE_CLI = [
    "--mode", "--out_ckpt", "--epochs", "--lr", "--batch_size",
    "--device", "--seed", "--variant_id", "--build_fn", "--build_cfg",
    "--model_path", "--student_model_path", "--teacher_cache", "--kd_config",
    "--student_ckpt", "--accuracy_baseline", "--accuracy_baseline_kind",
    "--project_root", "--env_anchor",
]
REMOVED_USER_FLAGS = [
    "--user_train_import", "--user_loss_fn", "--user_eval_import", "--user_eval_fn",
]
FIXED_SLOTS = [
    "user_compute_loss", "user_build_dataloader", "user_eval_metric",
    "build_user_optimizer", "build_user_scheduler",
]


# ===========================================================================
# 测试侧特化 helper（实例化骨架 + 精确字符串替换填 slot，替代 --user_train_import）
# ===========================================================================
DEMO_LOSS_BODY = '''\
def user_compute_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Ported verbatim from examples/kd-nas-demo/train.py compute_loss (MSE)."""
    return F.mse_loss(s_out, y)
'''


DEMO_LOADER_BODY = '''\
_SHAPE = (1, 4, 48, 64, 1)

class _RandomDataLoader:
    """Re-iterable random (x, y) batch generator — dependency closure ported
    verbatim from examples/kd-nas-demo/train.py."""

    def __init__(self, batch_size: int = 4, n_batches: int = 8, shape: tuple = _SHAPE) -> None:
        self.batch_size = batch_size
        self.n_batches = n_batches
        self.shape = shape

    def __iter__(self):
        inner = tuple(self.shape[1:])
        for _ in range(self.n_batches):
            x = torch.randn(self.batch_size, *inner)
            y = torch.randn(self.batch_size, *inner)
            yield x, y

    def __len__(self) -> int:
        return self.n_batches


def user_build_dataloader(batch_size: int = 4):
    """Ported verbatim from examples/kd-nas-demo/train.py build_dataloader."""
    return _RandomDataLoader(batch_size=batch_size)
'''


DEMO_EVAL_BODY = '''\
def user_eval_metric(student: torch.nn.Module, device) -> tuple[float, str]:
    """Ported verbatim from examples/kd-nas-demo/test_student.py _compute_nmse."""
    torch.manual_seed(20260725)
    n_samples = 8
    x = torch.randn(n_samples, 4, 48, 64, 1)
    y = torch.randn(n_samples, 4, 48, 64, 1)
    student.eval()
    with torch.no_grad():
        out = student(x)
    target = y.view_as(out)
    num = float(torch.sum((out - target) ** 2).item())
    den = float(torch.sum(target ** 2).item()) + 1e-12
    nmse = num / den
    if not math.isfinite(nmse):
        nmse = 1e9
    return nmse, "nmse"
'''


EMPTY_LOADER_BODY = '''\
def user_build_dataloader(batch_size: int = 4):
    return []  # empty loader — simulates broken/one-shot generator
'''


def _specialize_skeleton(skeleton_path: Path, out_path: Path, *,
                         loss_body: str | None = None,
                         loader_body: str | None = None,
                         eval_body: str | None = None,
                         opt_body: str | None = None,
                         sch_body: str | None = None) -> Path:
    """实例化骨架：把对应 slot 的整个顶层 def 块精确替换为注入代码。

    slot 名 → 正则 ``^def <slot> ...`` 匹配到下一个顶层 ``def `` 前（依赖闭包
    一并注入——loader_body 里含类/常量 + slot def）。返回写盘后的产物路径。
    """
    text = skeleton_path.read_text(encoding="utf-8")
    for name, body in (
        ("user_compute_loss", loss_body),
        ("user_build_dataloader", loader_body),
        ("user_eval_metric", eval_body),
        ("build_user_optimizer", opt_body),
        ("build_user_scheduler", sch_body),
    ):
        if body is None:
            continue
        pattern = re.compile(
            rf"^def {re.escape(name)}\b.*?(?=^\ndef |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        new_text, n = pattern.subn(body, text)
        assert n == 1, f"slot def {name} 未精确命中（n={n}）"
        text = new_text
    out_path.write_text(text, encoding="utf-8")
    return out_path


# ===========================================================================
# helpers
# ===========================================================================
def _run_pipeline(args: list[str], *, env_extra: dict | None = None,
                  expect_success: bool = True,
                  pipeline_path: Path | None = None) -> subprocess.CompletedProcess:
    """跑 train_pipeline.py（默认骨架模板，可传特化产物），注入 ORCA_KD_SCRIPTS_DIR。"""
    env = dict(os.environ)
    env["ORCA_KD_SCRIPTS_DIR"] = str(KD_SCRIPTS_DIR)
    env.pop("ORCA_CHART_SOCK", None)  # 抑制 orca chart 副作用
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        [sys.executable, str(pipeline_path or TEMPLATE), *args],
        capture_output=True, text=True, env=env, timeout=180,
    )
    if expect_success:
        assert r.returncode == 0, (
            f"train_pipeline.py 期望 exit 0 实际 {r.returncode}\n"
            f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
    return r


def _find_forbidden_code_tokens(path: Path, forbidden: set[str]) -> list[str]:
    """AST 扫描：检查禁用标识符是否出现在 **代码级** 使用（import / name /
    attribute access），跳过 docstring / 字符串字面量 / 注释。

    用 AST 而非裸 substring 是为了避免误判模块 docstring 里的说明性
    文本（如 "no DDP / torchrun / sandwich sampling" 出现在 docstring 里
    是合理的，不应触发 forbidden）。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: list[str] = []

    def matches_dotted(dotted: str) -> bool:
        """Check if a dotted path equals or starts with any forbidden token."""
        for fn in forbidden:
            if dotted == fn or dotted.startswith(fn + "."):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if matches_dotted(alias.name):
                    found.append(f"L{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if matches_dotted(mod):
                found.append(f"L{node.lineno}: from {mod} import ...")
            for n in node.names:
                if n.name in forbidden:
                    found.append(f"L{node.lineno}: from {mod} import {n.name}")
        elif isinstance(node, ast.Name):
            if node.id in forbidden:
                found.append(f"L{node.lineno}: name {node.id}")
        elif isinstance(node, ast.Attribute):
            # 构建完整 dotted path（如 nas_agent.train.distillation）
            parts: list[str] = []
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                dotted = ".".join(reversed(parts))
                if matches_dotted(dotted):
                    found.append(f"L{node.lineno}: attribute {dotted}")
    return found


def _build_teacher_cache(cache_path: Path, teacher_model_path: Path,
                         dummy_shape: list[int]) -> None:
    """直接用 kd.wrapper.TeacherCache.build 构造 4-字段 cache blob 落盘。

    镜像 teacher_setup.py 的输出格式（kd/wrapper.py TeacherCache.save 写盘
    的 4 字段：teacher_model_path / state_dict / hook_names / dummy_input_shape）。
    不调 teacher_setup.py 是为了测试自包含（避免 onnxruntime 依赖）。
    """
    sys.path.insert(0, str(KD_SCRIPTS_DIR))
    from kd.wrapper import TeacherCache  # type: ignore  # noqa: E402

    # 构造 fresh teacher（零参默认架构）+ 默认 init（不需要训练就能 cache）
    spec = importlib.util.spec_from_file_location("_cache_teacher", str(teacher_model_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    teacher = mod.build_model()
    hook_names = list(teacher.feature_hook_names())

    cache = TeacherCache.build(
        teacher_model_path=str(teacher_model_path),
        teacher_state_dict=teacher.state_dict(),
        hook_names=hook_names,
        dummy_input_shape=dummy_shape,
    )
    cache.save(str(cache_path))


def _specialized_demo_pipeline(tmp_path: Path, name: str = "tp_spec.py",
                               *, loss: bool = True, loader: bool = True,
                               eval_body: bool = True) -> Path:
    """demo 等价函数体的特化产物（自包含，不 import 用户模块）。"""
    return _specialize_skeleton(
        TEMPLATE,
        tmp_path / name,
        loss_body=DEMO_LOSS_BODY if loss else None,
        loader_body=DEMO_LOADER_BODY if loader else None,
        eval_body=DEMO_EVAL_BODY if eval_body else None,
    )


# ===========================================================================
# folder-agent 结构契约
# ===========================================================================
def test_agent_files_all_present():
    """所有 folder-agent 文件齐全（agent.md / SKILL.md / generation workflow /
    2 checklist / template / fidelity_check.py）。"""
    for p in (AGENT_MD, SKILL_MD, GEN_WORKFLOW, CHECKLIST_TRAINING,
              CHECKLIST_CLI, TEMPLATE, FIDELITY_CHECK):
        assert p.is_file(), f"missing agent file: {p}"


def test_agent_md_has_strong_directive():
    """agent.md 必须开头声明唯一职责 + 红线（❌）+ fail loud。

    抗 spec-审查（deepseek-v4-flash 等 LLM 易把 agent.md 当 spec 评判而不执行）。
    """
    text = AGENT_MD.read_text(encoding="utf-8")
    assert "唯一职责" in text, "agent.md 开头缺「唯一职责」执行指令"
    assert "红线" in text, "agent.md 缺「红线」section"
    assert "❌" in text, "agent.md 缺 ❌ 红线列表"
    # 关键红线（KD-NAS 5 处适配的核心约束）
    assert "DDP" in text
    assert "sandwich" in text
    assert "kd.compose" in text
    assert "nas_agent.train.distillation" in text  # 红线里点名禁用
    assert "自包含" in text or "绝不 import" in text
    # 骨架特化红线（重写新增）
    assert "零占位符残留" in text, "agent.md 缺「零占位符残留」红线"
    assert "user_compute_loss" in text or "5 个固定 slot" in text or "slot" in text
    # 输出 JSON contract（v4 嵌入 workflow：最终消息是 JSON {train_pipeline_path}）
    assert "train_pipeline_path" in text, (
        "agent.md 输出 contract 应声明 train_pipeline_path（v4 workflow 嵌入后的 JSON 终点）"
    )


def test_skill_md_workflow_three_steps():
    """SKILL.md 必须有 Step 1/2/3 三步工作流 + 四层校验 + verifier prompt。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "Step 1: Load Context" in text
    assert "Step 2: Generate" in text
    assert "Step 3: Validate" in text
    # 四层校验（Layer 1 静态+无残留 / Layer 2 smoke / Layer 3 fidelity / Layer 4 verifier）
    assert "Layer 1" in text and "Layer 2" in text and "Layer 3" in text
    assert "Layer 4" in text
    # fidelity_check.py 是 Layer 3（必跑）
    assert "fidelity_check.py" in text
    # verifier prompt 模板（参考 nas-agent-pipeline 的 workflow-verifier 调用）
    assert "workflow-verifier" in text.lower() or "VERDICT:" in text
    # 输出摘要 contract
    assert "OUTPUT_DIR:" in text
    assert "MODES_SUPPORTED:" in text


def test_generation_workflow_doc_complete():
    """generation workflow 文档必须覆盖 9 个核心 sections（CLI / Model / Loss /
    Optimizer / Teacher / Distill / KD / Path / LiveChart）+ Validation + Forbidden。"""
    text = GEN_WORKFLOW.read_text(encoding="utf-8")
    for section in (
        "### 1. CLI And Runtime Args",
        "### 2. Model Construction",
        "### 3. User Task Loss",
        "### 4. Optimizer, Scheduler",
        "### 5. Teacher Mode",
        "### 6. Distill Mode",
        "### 7. KD Loss Composition",
        "### 8. Path Handling",
        "### 9. Live Chart Push",
    ):
        assert section in text, f"generation workflow 缺 section: {section}"
    # KD-NAS 5 处适配（开头明确列出）
    assert "sandwich" in text.lower()
    assert "DDP" in text
    assert "importlib" in text
    assert "kd.compose" in text
    # Validation（四层）+ Forbidden
    assert "## Validation" in text
    assert "## Forbidden" in text
    assert "Layer 1" in text and "Layer 2" in text and "Layer 3" in text and "Layer 4" in text
    assert "fidelity_check.py" in text
    # 已删 flag 点名（逐 flag）
    for flag in REMOVED_USER_FLAGS:
        assert flag in text, f"generation workflow 应点名已删 flag: {flag}"
    # ckpt schemas（teacher / distill）
    assert "TEACHER_CKPT:" in text
    assert "STUDENT_CKPT:" in text
    assert "KD_PROXY_MSE:" in text


def test_checklists_have_critical_items():
    """两个 checklist 文件必须含 [CRITICAL] 项（verifier 必须核查的硬约束）。"""
    t = CHECKLIST_TRAINING.read_text(encoding="utf-8")
    c = CHECKLIST_CLI.read_text(encoding="utf-8")
    assert "[CRITICAL]" in t, "01_training.md 必须有 [CRITICAL] 项"
    assert "[CRITICAL]" in c, "02_cli.md 必须有 [CRITICAL] 项"
    # 关键 critical 项（不可遗漏）
    assert "No Distributed" in t or "No DDP" in t, (
        "01_training.md 缺 [CRITICAL] No Distributed/DDP 项"
    )
    assert "Self-Contained" in t, "01_training.md 缺 [CRITICAL] Self-Contained 项"
    assert "Model Loaded By Path" in t, "01_training.md 缺 [CRITICAL] Model By Path 项"
    assert "KD Library" in t, "01_training.md 缺 [CRITICAL] KD Library 项"
    assert "Stable Base CLI" in c, "02_cli.md 缺 [CRITICAL] Stable Base CLI 项"
    assert "--mode Required" in c, "02_cli.md 缺 [CRITICAL] --mode Required 项"
    # 重写新增项（零占位符语义）
    assert "Zero Placeholder Residue" in t, "01_training.md 缺 [CRITICAL] C21"
    assert "Loss Function Body Ported Verbatim" in t, "01_training.md 缺 [CRITICAL] C22"
    assert "Eval Metric Body Ported Verbatim" in t, "01_training.md 缺 [CRITICAL] C23"
    assert "Fidelity Check PASS Evidence" in t, "01_training.md 缺 [CRITICAL] C24"
    assert "No `--user_*` Override Flags" in c or "user_*" in c, (
        "02_cli.md 缺「无 --user_* 覆盖 flag」[CRITICAL]"
    )
    # 已删占位符语义项（防 verifier 冲突）
    assert "Placeholder Fallback Keeps Script Runnable" not in t, (
        "01_training.md 的 C17 应已删除（占位符语义随骨架化移除）"
    )
    assert "_PlaceholderDataLoader" not in t, (
        "01_training.md 不应再引用 _PlaceholderDataLoader（C20 已改写）"
    )


# ===========================================================================
# 参考模板静态校验
# ===========================================================================
def test_template_py_compile():
    """references/templates/train_pipeline.py 通过 py_compile。"""
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(TEMPLATE)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"py_compile 失败:\n{r.stderr}"


def test_template_help_lists_stable_cli():
    """--help 列出全部 stable base CLI flag（不含已删的 --user_*）。"""
    r = subprocess.run(
        [sys.executable, str(TEMPLATE), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"--help 失败:\n{r.stderr}"
    for flag in STABLE_BASE_CLI:
        assert flag in r.stdout, f"--help 输出缺 flag: {flag}"
    for flag in REMOVED_USER_FLAGS:
        assert flag not in r.stdout, f"--help 输出不应含已删 flag: {flag}"


def test_template_no_forbidden_code_tokens():
    """参考模板的 **代码级**（import / name / attribute）扫描：无 DDP /
    torchrun / DistributedDataParallel / setup_distributed / set_sample_config
    / sandwich / sample_sandwich_arch_configs / is_main_process / get_rank /
    nas_agent.train.distillation / logits_kd_loss / soft_bce_kd_loss /
    cosine_kd_loss。

    用 AST 扫描而非裸 substring：模块 docstring 里会出现 "no DDP / no
    torchrun / no sandwich sampling" 等说明文字，这是合规的（说明意图），
    不应被误判为残留代码。
    """
    forbidden = {
        # distributed / launch
        "torch.distributed",
        "DistributedDataParallel",
        "setup_distributed",
        "is_main_process",
        "get_rank",
        "get_local_rank",
        "save_checkpoint_ddp",
        "set_sample_config_ddp",
        # nas sandwich sampling
        "sandwich",
        "sample_sandwich_arch_configs",
        "set_sample_config",
        # nas distillation library
        "nas_agent.train.distillation",
        "logits_kd_loss",
        "soft_bce_kd_loss",
        "cosine_kd_loss",
    }
    found = _find_forbidden_code_tokens(TEMPLATE, forbidden)
    assert not found, (
        f"train_pipeline.py 代码级含禁用 token（docstring 字面量不算）：\n"
        + "\n".join(found)
    )


def test_template_zero_placeholder_residue():
    """骨架模板零占位符残留：无 {{ 字面量、无 _placeholder_*、无
    USER_TRAIN_MODULE / USER_EVAL_MODULE 常量、无 _load_user_train /
    _load_user_eval（含 docstring——Layer 1 扫描对整文件做）。

    骨架 = 非可运行中间态：5 个 slot 必须 raise NotImplementedError。
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "{{" not in text, "模板不得含 {{ 字面量（含 docstring）"
    assert "_placeholder" not in text, "模板不得含 _placeholder_* 标识符"
    assert "USER_TRAIN_MODULE" not in text and "USER_EVAL_MODULE" not in text
    assert "_load_user_train" not in text and "_load_user_eval" not in text
    for flag in REMOVED_USER_FLAGS:
        assert flag not in text, f"模板不得含已删 flag: {flag}"
    # 5 个固定 slot 都以 NotImplementedError / return None 占位
    for slot in FIXED_SLOTS:
        assert re.search(rf"^def {slot}\s*\(", text, re.MULTILINE), f"缺 slot def {slot}"


def test_template_uses_kd_library():
    """distill mode 用 kd.compose.build_kd_loss / kd.wrapper / kd.ema（lazy import）。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "from kd.compose import build_kd_loss" in text
    assert "from kd.wrapper import KDStudentWrapper" in text
    assert "from kd.wrapper import" in text and "TeacherCache" in text
    assert "from kd.ema import MeanTeacherEMA" in text
    # lazy（在函数体内，不是 top-level）
    distill_start = text.find("def run_distill_mode")
    assert distill_start > 0, "缺 run_distill_mode 函数"
    distill_body = text[distill_start:]
    for line in (
        "from kd.compose import build_kd_loss",
        "from kd.wrapper import KDStudentWrapper, TeacherCache",
        "from kd.ema import MeanTeacherEMA",
    ):
        assert line in distill_body, (
            f"KD import 必须在 run_distill_mode 函数体内（lazy）：{line}"
        )


def test_template_mode_dispatch_in_main():
    """main() 按 args.mode 三分发（eval → run_eval_mode / teacher →
    run_teacher_mode / else → run_distill_mode），且无 _load_user_* 运行时加载
    （用户逻辑在 5 个固定 slot 内，slot 未填 → NotImplementedError fail loud）。

    用 AST 验证而不靠 substring 偏移：避免有人把分支 body 互换（teacher 块调
    run_distill_mode）而 substring 测试仍 pass。
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    tree = ast.parse(text)

    main_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
            break
    assert main_fn is not None, "train_pipeline.py 缺 main() 函数"

    dispatch: dict[str, list[str]] = {}
    for node in ast.walk(main_fn):
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Attribute)
                    and isinstance(test.left.value, ast.Name)
                    and test.left.value.id == "args"
                    and test.left.attr == "mode"):
                for comp in test.comparators:
                    if isinstance(comp, ast.Constant):
                        calls = [
                            c.func.id for c in ast.walk(node)
                            if isinstance(c, ast.Call)
                            and isinstance(c.func, ast.Name)
                            and c.func.id.startswith("run_")
                        ]
                        dispatch[comp.value] = calls

    assert dispatch.get("eval") == ["run_eval_mode"], (
        f"main() eval 分支应调 run_eval_mode，实际：{dispatch.get('eval')}"
    )
    assert dispatch.get("teacher") == ["run_teacher_mode"], (
        f"main() teacher 分支应调 run_teacher_mode，实际：{dispatch.get('teacher')}"
    )
    # else 兜底 → run_distill_mode（main 函数体顶层 return）
    distill_calls = [
        c.func.id for c in ast.walk(main_fn)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "run_distill_mode"
    ]
    assert distill_calls, "main() 缺 run_distill_mode 兜底调用（else 分支）"
    # 已删机制不得复活：无 _load_user_train / _load_user_eval 运行时加载
    assert "_load_user_train" not in text and "_load_user_eval" not in text


# ===========================================================================
# 功能 smoke（teacher 模式）
# ===========================================================================
def test_smoke_teacher_mode_unspecialized_fails(tmp_path):
    """**未特化骨架**跑 teacher 模式 → 非零退出 + stderr 含 NotImplementedError。

    守门 fail-loud：slot 未填 = 不可运行中间态，任何模式直接崩（不是静默
    dummy fallback），且不写 ckpt。
    """
    out_ckpt = tmp_path / "teacher_unspecialized.pth"
    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
    ], expect_success=False)
    assert r.returncode != 0, "未特化骨架应 fail loud（NotImplementedError），不应 return 0"
    assert "NotImplementedError" in (r.stderr + r.stdout), (
        f"错误信息应含 NotImplementedError，实际：\n{r.stderr}\n{r.stdout}"
    )
    assert not out_ckpt.exists(), "未特化骨架不应写 ckpt"


def test_smoke_teacher_mode_real_user_train(tmp_path):
    """teacher 模式 + **特化产物**（demo 等价 loss/loader 逐字搬入，无
    `--user_*` flag）：跑通 + loss 是有限数（非 NaN/Inf）+ state_dict 可 load 回。
    """
    spec = _specialized_demo_pipeline(tmp_path)
    out_ckpt = tmp_path / "teacher_real.pth"
    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "real_user",
    ], pipeline_path=spec)
    assert "TEACHER_CKPT:" in r.stdout
    for line in r.stdout.splitlines():
        if line.startswith("TASK_LOSS_FINAL:"):
            val = float(line.split(":", 1)[1].strip())
            assert val == val and val != float("inf"), f"loss 不是有限数: {val}"
            break
    else:
        pytest.fail("stdout 缺 TASK_LOSS_FINAL 行")

    # state_dict 可 load 回 fresh teacher
    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    spec_mod = importlib.util.spec_from_file_location("_check_teacher", str(TEACHER_MODEL))
    mod = importlib.util.module_from_spec(spec_mod)
    assert spec_mod.loader is not None
    spec_mod.loader.exec_module(mod)
    fresh = mod.build_model()
    fresh.load_state_dict(blob["state_dict"])  # 不抛 = key 完全对齐


# ===========================================================================
# 功能 smoke（distill 模式，端到端）
# ===========================================================================
def test_smoke_distill_mode_end_to_end(tmp_path):
    """distill 模式端到端：

    1. 先用 kd.wrapper.TeacherCache.build 直接构造 teacher_cache.pt（4 字段 blob）
    2. 跑特化产物 train_pipeline.py --mode distill + spt_alt student（无 --user_* flag）
    3. 校验 STUDENT_CKPT / KD_LOSS_FINAL / KD_PROXY_MSE + ckpt schema
    """
    spec = _specialized_demo_pipeline(tmp_path)
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "student.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({"kd_losses": ["mse"], "weights": {"mse": 1.0}}),
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "smoke_student",
    ], pipeline_path=spec)
    assert "STUDENT_CKPT:" in r.stdout
    assert "KD_LOSS_FINAL:" in r.stdout
    assert "KD_PROXY_MSE:" in r.stdout
    assert out_ckpt.is_file()

    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    assert blob["mode"] == "distill"
    assert blob["variant_id"] == "smoke_student"
    assert "student_state_dict" in blob
    assert blob["student_cfg"] == {"num_blocks": 3, "embed_dim": 16}
    assert blob["kd_config"]["kd_losses"] == ["mse"]
    assert isinstance(blob["proxy_mse"], float)

    # student_state_dict 可 load 回 fresh student（按路径 import spt_alt）
    sys.path.insert(0, str(STUDENT_VARIANT.parent))
    try:
        spec_mod = importlib.util.spec_from_file_location(
            "_check_student", str(STUDENT_VARIANT)
        )
        mod = importlib.util.module_from_spec(spec_mod)
        assert spec_mod.loader is not None
        spec_mod.loader.exec_module(mod)
        fresh_student = mod.build_model(num_blocks=3, embed_dim=16)
        fresh_student.load_state_dict(blob["student_state_dict"])
    finally:
        sys.path.remove(str(STUDENT_VARIANT.parent))


def test_smoke_distill_mode_kd_loss_finite(tmp_path):
    """distill 模式 KD loss 是有限数（不要求单调下降——随机数据不求收敛）。

    守门 KD composite 真能 forward + backward + optimizer.step 不崩。
    """
    spec = _specialized_demo_pipeline(tmp_path)
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "student2.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({"kd_losses": ["mse"], "weights": {"mse": 0.5}}),
        "--epochs", "2",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "kd_check",
    ], pipeline_path=spec)
    for line in r.stdout.splitlines():
        if line.startswith("KD_LOSS_FINAL:"):
            val = float(line.split(":", 1)[1].strip())
            assert val == val and val != float("inf"), f"KD loss 非有限: {val}"
            break
    else:
        pytest.fail("stdout 缺 KD_LOSS_FINAL 行")


def test_smoke_distill_mode_fitnets_kd(tmp_path):
    """distill mode + feature-KD (fitnets) 端到端 smoke。

    mse KD 路径不需要 ``kd_loss.prepare()``（无 adapter 参数）；fitnets 需要——
    OFD/FitNets adapter 经 ``prepare()`` 预建 + ``kd_parameters()`` 进 optimizer
    是 distill mode 最复杂的路径，单独守门（reviewer 🟡-2）。

    spt_alt（receiver KB 变体）+ teacher wrapper（teacher-gen 产物；本测用 spt_alt 占位）
    都暴露 ``feature_hook_names()`` 长度 2（KD-NAS 变体契约一致），fitnets 取中间层 hint。
    """
    spec = _specialized_demo_pipeline(tmp_path)
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "student_fitnets.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({
            "kd_losses": ["fitnets"],
            "weights": {"fitnets": 1.0},
        }),
        "--epochs", "1",
        "--batch_size", "2",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
        "--variant_id", "fitnets_test",
    ], pipeline_path=spec)
    assert "STUDENT_CKPT:" in r.stdout
    assert "KD_LOSS_FINAL:" in r.stdout
    assert "KD_PROXY_MSE:" in r.stdout
    assert out_ckpt.is_file()

    # feature-KD 适配器参数应已落到 ckpt 训练（student_state_dict 可 load 回）
    import torch
    blob = torch.load(out_ckpt, map_location="cpu")
    assert blob["mode"] == "distill"
    assert blob["kd_config"]["kd_losses"] == ["fitnets"]
    for line in r.stdout.splitlines():
        if line.startswith("KD_LOSS_FINAL:"):
            val = float(line.split(":", 1)[1].strip())
            assert val == val and val != float("inf"), (
                f"fitnets KD loss 非有限: {val}（adapter forward/backward 应正常）"
            )
            break


# ===========================================================================
# fail-loud 路径
# ===========================================================================
def test_teacher_mode_missing_model_path_fails(tmp_path):
    """teacher 模式缺 --model_path → 非零退出 + stderr/stdout 报因（fail loud）。"""
    r = _run_pipeline([
        "--mode", "teacher",
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    assert "--model_path" in (r.stderr + r.stdout)


def test_distill_mode_missing_student_model_path_fails(tmp_path):
    """distill 模式缺 --student_model_path → 非零退出。"""
    r = _run_pipeline([
        "--mode", "distill",
        "--teacher_cache", str(tmp_path / "nonexistent.pt"),
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    assert "--student_model_path" in (r.stderr + r.stdout)


def test_distill_mode_missing_teacher_cache_fails(tmp_path):
    """distill 模式缺 --teacher_cache → 非零退出。"""
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    assert "--teacher_cache" in (r.stderr + r.stdout)


def test_distill_mode_teacher_cache_not_found(tmp_path):
    """distill 模式传不存在的 teacher_cache 路径 → 非零退出 + FileNotFoundError。

    TeacherCache.load 对缺失文件 raise FileNotFoundError（kd/wrapper.py）。
    """
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(tmp_path / "never_existed.pt"),
        "--out_ckpt", str(tmp_path / "x.pth"),
        "--epochs", "1",
    ], expect_success=False)
    assert r.returncode != 0
    combined = r.stderr + r.stdout
    assert "teacher_cache" in combined or "FileNotFoundError" in combined


# ===========================================================================
# fail-loud: slot 未填 + 空 dataloader（reviewer 🟡#1/#2/#3 修复守门）
# ===========================================================================
def test_missing_loss_slot_fails_loud(tmp_path):
    """**部分特化**（loader/eval 填了、loss slot 未填）→ teacher 模式
    NotImplementedError fail loud。

    覆盖意图：缺失用户逻辑必须 fail loud（原 test_user_train_module_missing_loss_fn
    的骨架版——缺 loss 函数 = slot 未填）。
    """
    spec = _specialize_skeleton(
        TEMPLATE, tmp_path / "tp_no_loss.py",
        loader_body=DEMO_LOADER_BODY, eval_body=DEMO_EVAL_BODY,
    )
    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--device", "cpu",
        "--out_ckpt", str(tmp_path / "x.pth"),
    ], pipeline_path=spec, expect_success=False)
    assert r.returncode != 0, "loss slot 未填应 fail loud（NotImplementedError）"
    assert "NotImplementedError" in (r.stderr + r.stdout), (
        f"错误信息应含 NotImplementedError，实际：\n{r.stderr}\n{r.stdout}"
    )
    assert not (tmp_path / "x.pth").exists(), "slot 未填不应写 ckpt"


def test_teacher_mode_empty_dataloader_fails_loud(tmp_path):
    """teacher 模式 + 空 dataloader（特化 helper 注入空 loader）→ 非零退出 +
    不写 NaN ckpt（CLAUDE.md Rule 12）。

    守门 reviewer 🟡#1：dataloader 空时 last_avg 保持 nan，必须 raise 不静默落盘。
    """
    spec = _specialize_skeleton(
        TEMPLATE, tmp_path / "tp_empty_loader.py",
        loss_body=DEMO_LOSS_BODY, loader_body=EMPTY_LOADER_BODY,
    )
    out_ckpt = tmp_path / "should_not_exist.pth"

    r = _run_pipeline([
        "--mode", "teacher",
        "--model_path", str(TEACHER_MODEL),
        "--build_cfg", "{}",
        "--epochs", "1",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
    ], pipeline_path=spec, expect_success=False)

    assert r.returncode != 0, "空 dataloader 应 fail loud，不应 return 0"
    combined = r.stderr + r.stdout
    # 错误信息含 "no training batches" 或 "NaN"
    assert "no training batches" in combined.lower() or "nan" in combined.lower(), (
        f"错误信息应说明 dataloader 空 / NaN，实际：{combined}"
    )
    # 关键：ckpt 文件不应存在（不应写 NaN ckpt）
    assert not out_ckpt.exists(), (
        "空 dataloader 时不应写 ckpt（防 NaN teacher → NaN student → NaN proxy_mse 链）"
    )


def test_distill_mode_empty_dataloader_fails_loud(tmp_path):
    """distill 模式 + 空 dataloader（特化 helper 注入空 loader）→ 非零退出 +
    不写 NaN ckpt。

    distill mode 先 materialise 一个 batch 做 kd_loss.prepare（用 next(iter(dl))），
    所以空 loader 在 prepare 阶段就会 StopIteration → 未捕获 → 非零退出。
    这是 prepare 阶段的隐式 fail-loud（即使是空的也会响）。
    """
    spec = _specialize_skeleton(
        TEMPLATE, tmp_path / "tp_empty_loader_distill.py",
        loss_body=DEMO_LOSS_BODY, loader_body=EMPTY_LOADER_BODY,
    )
    cache_path = tmp_path / "teacher_cache.pt"
    _build_teacher_cache(cache_path, TEACHER_MODEL, [1, 4, 48, 64, 1])

    out_ckpt = tmp_path / "should_not_exist.pth"
    r = _run_pipeline([
        "--mode", "distill",
        "--student_model_path", str(STUDENT_VARIANT),
        "--teacher_cache", str(cache_path),
        "--build_cfg", json.dumps({"num_blocks": 3, "embed_dim": 16}),
        "--kd_config", json.dumps({"kd_losses": ["mse"], "weights": {"mse": 1.0}}),
        "--epochs", "1",
        "--device", "cpu",
        "--out_ckpt", str(out_ckpt),
    ], pipeline_path=spec, expect_success=False)

    assert r.returncode != 0
    # distill prepare 阶段 next(iter(dl)) 对空 list → StopIteration
    # 或 main loop 跑 0 batch → NaN guard
    assert not out_ckpt.exists(), "空 dataloader 时不应写 ckpt"


# ===========================================================================
# KD loss 强制（distill 必须含非空 kd_losses）
# ===========================================================================
def _import_kd_compose():
    """直接 import _kd_scripts/kd/compose.py（不经 train_pipeline 子进程）。"""
    sys.path.insert(0, str(KD_SCRIPTS_DIR))
    try:
        import importlib
        mod = importlib.import_module("kd.compose")
        return mod
    finally:
        sys.path.pop(0)


def test_compose_rejects_empty_kd_losses_fail_loud():
    """distill 铁律：空 kd_losses ∧ ema off → build_kd_loss/KDComposite 构造即 fail loud。

    意图：student 训练必须加载 KD loss；纯 task loss 不是蒸馏（属 --mode teacher）。
    """
    compose = _import_kd_compose()

    def _task_loss(s_out, y):
        return ((s_out - y) ** 2).mean()

    # 空 kd_losses + ema off → 构造期 ValueError
    with pytest.raises(ValueError, match="kd_losses 为空"):
        compose.build_kd_loss(_task_loss, {"kd_losses": [], "weights": {}})

    # ema 开但 kd_losses 空 → 允许（mean-teacher 一致性也是 KD 信号）
    comp_ema = compose.build_kd_loss(_task_loss, {"kd_losses": [], "ema": True})
    assert comp_ema.use_ema is True

    # 默认 mse config → 构造成功，调用返回 task + mse 项
    import torch
    comp_mse = compose.build_kd_loss(
        _task_loss, {"kd_losses": ["mse"], "weights": {"mse": 1.0}}
    )
    s_out = torch.randn(2, 3)
    t_out = torch.randn(2, 3)
    y = torch.randn(2, 3)
    loss = comp_mse(s_out, y, None, t_out, None, None, epoch=0)
    assert torch.isfinite(loss)
    # KD（mse）从 epoch 0 即贡献：loss > 纯 task loss
    assert loss.item() > _task_loss(s_out, y).item()


def test_compose_feature_term_without_hooks_fails_loud():
    """SPEC §1.3 fail-loud 守卫：kd_losses 含特征项（ofd/fitnets/rkd）但运行时无 feats → raise。

    覆盖 4 分支：
      1. mse+ofd + feats=None → raise ValueError（含"特征项"）；旧逻辑静默 continue 是 bug。
      2. mse-only + feats=None → ok（mse 不依赖 feats）。
      3. kd_losses=[] + ema=True + feats=None → ok（无特征项）。
      4. prepare(sample=[feat,...]) 后 __call__(s_feats=None,...) → 仍 raise
         （守卫看运行时 feats，不看 prepare 历史；锁 intent）。
    """
    import torch
    compose = _import_kd_compose()

    def _task_loss(s_out, y):
        return ((s_out - y) ** 2).mean()

    s_out = torch.randn(2, 3)
    t_out = torch.randn(2, 3)
    y = torch.randn(2, 3)

    # 分支 1：mse+ofd + 无 feats → raise
    comp_ofd = compose.build_kd_loss(
        _task_loss,
        {"kd_losses": ["mse", "ofd"], "weights": {"mse": 1.0, "ofd": 0.3}},
    )
    with pytest.raises(ValueError, match="特征项"):
        comp_ofd(s_out, y, None, t_out, None, None, epoch=0)

    # 分支 2：mse-only + 无 feats → 不 raise
    comp_mse = compose.build_kd_loss(
        _task_loss, {"kd_losses": ["mse"], "weights": {"mse": 1.0}}
    )
    loss = comp_mse(s_out, y, None, t_out, None, None, epoch=0)
    assert torch.isfinite(loss)

    # 分支 3：空 kd_losses + ema + 无 feats → 不 raise（无特征项）
    comp_ema = compose.build_kd_loss(
        _task_loss, {"kd_losses": [], "ema": True, "weights": {"ema": 1.0}}
    )
    loss_ema = comp_ema(s_out, y, None, t_out, None, s_out, epoch=0)
    assert torch.isfinite(loss_ema)

    # 分支 4：prepare(sample=...) 后 __call__(s_feats=None,...) → 仍 raise
    # 守卫看运行时 feats，不看 prepare 历史（intent: prepare 只 build adapter 参数，
    # 不缓解"训练时 forward 没产出 feats"的真实问题）。
    s_feat_sample = [torch.randn(2, 5)]
    t_feat_sample = [torch.randn(2, 5)]
    comp_ofd.prepare(s_feat_sample, t_feat_sample)
    assert comp_ofd.ofd_adapter is not None  # prepare 已 lazy-build adapter
    with pytest.raises(ValueError, match="特征项"):
        comp_ofd(s_out, y, None, t_out, None, None, epoch=0)


def test_distill_gen_student_ast_hook_detection_handles_indented_class_method(tmp_path):
    """SPEC §6.4 / §7 F2 回归守护：distill/gen-student agent.md 的 AST 判定必须能识别
    **缩进的 class method** ``def feature_hook_names(self)``（旧 ``grep '^def'`` 永远漏判 →
    ofd 永远被剥离 → 回归"静默降级"）。

    锁定 agent.md 内嵌的 AST python -c 片段对缩进 method 返回 True、对无 hook 文件返回 False。
    """
    import textwrap
    # 从 distill/agent.md 提取 AST 判定片段（与 gen-student/agent.md step5 同款）。
    ast_snippet = textwrap.dedent('''
        import ast,sys
        t=ast.parse(open(sys.argv[1]).read())
        print(any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="feature_hook_names" for n in ast.walk(t)))
    ''')

    # 含缩进 class method 的 student（demo 真实形态：hook 是 class 成员，不是 module-level def）
    student_with_hook = tmp_path / "with_hook.py"
    student_with_hook.write_text(textwrap.dedent('''
        import torch.nn as nn
        class StudentModel(nn.Module):
            def __init__(self):
                super().__init__()
            def feature_hook_names(self) -> list:
                return ["layer1", "layer2"]
        def build_model(**cfg):
            return StudentModel()
    '''), encoding="utf-8")

    # 无 hook 的 student
    student_no_hook = tmp_path / "no_hook.py"
    student_no_hook.write_text(textwrap.dedent('''
        import torch.nn as nn
        class StudentModel(nn.Module):
            def __init__(self):
                super().__init__()
        def build_model(**cfg):
            return StudentModel()
    '''), encoding="utf-8")

    # 有 hook → True（旧 grep '^def' 会返回 0 → ofd 永远剥离 → F2 bug）
    r1 = subprocess.run(
        [sys.executable, "-c", ast_snippet, str(student_with_hook)],
        capture_output=True, text=True,
    )
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.strip() == "True", f"缩进 class method 应被 AST 识别：{r1.stdout}"

    # 无 hook → False
    r2 = subprocess.run(
        [sys.executable, "-c", ast_snippet, str(student_no_hook)],
        capture_output=True, text=True,
    )
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == "False"


# ===========================================================================
# fidelity_check.py（Layer 3 数值级等价性）
# ===========================================================================
def test_fidelity_check_demo_fixture(tmp_path):
    """fidelity_check.py 对 demo fixture（特化产物 vs 用户原 train.py +
    test_student.py）→ FIDELITY: PASS + FIDELITY_LEVEL: numeric。

    数值级守门：loss（MSE 同输入同种子 allclose）、loader（batch shape +
    re-iterable）、eval（NMSE 同模型实例同种子 allclose + kind 一致）、
    model I/O（teacher forward DUMMY_INPUT shape 同形）。demo train.py 无
    optimizer → OPT_TYPE_OK: skip（非 FAIL）。
    """
    spec = _specialized_demo_pipeline(tmp_path)
    env = dict(os.environ)
    env.pop("ORCA_CHART_SOCK", None)
    # TEACHER_MODEL = spt_alt.py 依赖同目录 _model8_blocks；fidelity_check subprocess 需 receiver dir 入 PYTHONPATH。
    env["PYTHONPATH"] = _RECEIVER_DIR + os.pathsep + env.get("PYTHONPATH", "")

    r = subprocess.run(
        [sys.executable, str(FIDELITY_CHECK),
         "--train_pipeline", str(spec),
         "--user_train", str(USER_TRAIN_PY),
         "--user_eval", str(USER_EVAL_PY),
         "--dummy_input", json.dumps({"shape": [1, 4, 48, 64, 1], "dtype": "float32"}),
         "--model_path", str(TEACHER_MODEL),
         "--build_fn", "build_model", "--build_cfg", "{}",
         "--project_root", str(USER_TRAIN_PY.parent)],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert r.returncode == 0, (
        f"fidelity_check 期望 exit 0 实际 {r.returncode}\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "FIDELITY: PASS" in r.stdout, f"FIDELITY 应为 PASS：\n{r.stdout}\n{r.stderr}"
    assert "FIDELITY_LEVEL: numeric" in r.stdout
    assert "LOSS_ALLCLOSE: true" in r.stdout
    assert "LOADER_SHAPE_OK: true" in r.stdout
    assert "EVAL_ALLCLOSE: true" in r.stdout
    assert "IO_SHAPE_OK: true" in r.stdout
    # demo train.py 无 optimizer → 该项 skip（不算 FAIL）
    assert "OPT_TYPE_OK: skip" in r.stdout


def test_fidelity_check_catches_loss_drift(tmp_path):
    """fidelity_check.py 抓 loss 漂移：特化产物 loss 换成 L1 → LOSS_ALLCLOSE: false
    → exit 2 + FIDELITY: FAIL（守门「train 调一个函数」——静默替换必须被抓）。"""
    drifted_loss = '''\
def user_compute_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """DRIFTED: L1 instead of the user's MSE — must be caught."""
    return F.l1_loss(s_out, y)
'''
    spec = _specialize_skeleton(
        TEMPLATE, tmp_path / "tp_drift.py",
        loss_body=drifted_loss, loader_body=DEMO_LOADER_BODY,
        eval_body=DEMO_EVAL_BODY,
    )
    env = dict(os.environ)
    env.pop("ORCA_CHART_SOCK", None)

    r = subprocess.run(
        [sys.executable, str(FIDELITY_CHECK),
         "--train_pipeline", str(spec),
         "--user_train", str(USER_TRAIN_PY),
         "--dummy_input", json.dumps({"shape": [1, 4, 48, 64, 1], "dtype": "float32"}),
         "--project_root", str(USER_TRAIN_PY.parent)],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert r.returncode != 0, "loss 漂移应 exit 2（fail loud）"
    assert "LOSS_ALLCLOSE: false" in r.stdout, f"LOSS_ALLCLOSE 应为 false：\n{r.stdout}"
    assert "FIDELITY: FAIL" in r.stdout
