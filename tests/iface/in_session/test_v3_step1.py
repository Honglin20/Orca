"""tests/iface/in_session/test_v3_step1.py —— SPEC v3 §8 step 1 验收守门。

覆盖 §11 验收相关项：
  - **保留字黑名单**（§2.2 MS1）：wf 取 ``status``/``next``/``tars`` 等 → compile fail loud。
  - **``orca <wf>`` 语法糖**（§2.1）：未注册的首 token = wf 名 → bootstrap 派发。
  - **``orca --help``**（§2.4）：含 7 命令（list/next/status/stop/open/doctor + <wf> epilog），
    不含 tars 命令名（run/serve/ps/...）。
  - **驱动协议 B1**（§8）：prompt 含 ``orca next``、不含 ``orca in-session``。
  - **marker 3 字段**（§7.2）：grep dataclass 无 tape_path/yaml/session_id/owner。
  - **catalog 单一实现**（§3.1 / coordinator 铁律）：orca list 委托 commands.run_list。
  - **tars 命令名变量化**（§3.2）：ORCA_BACKEND_CMD env 控制 backend_cmd_name 显示。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from orca.compile import ConfigurationError, load_workflow
from orca.iface.cli.skill_cmds import ENTRY_SKILL_NAME
from orca.iface.in_session import marker as marker_mod
from orca.iface.in_session.cli import _drive_protocol, app


# ── §2.2 保留字黑名单（MS1）─────────────────────────────────────────────────


RESERVED_WF_YAML_TMPL = """\
name: {name}
description: wf 取保留名（应被 compile 拒）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "x"
    routes:
      - to: $end
"""


@pytest.mark.parametrize("reserved", [
    "status", "next", "list", "stop", "open", "doctor",  # orca 7 命令
    "run", "serve", "ps", "logs", "wait", "resume",      # tars 后端命令
    "install", "validate", "mcp", "executor",
    "tars",  # ORCA_BACKEND_CMD 默认值
    "bootstrap",
])
def test_reserved_wf_name_rejected_at_compile(tmp_path, reserved):
    """§2.2：wf.name 取保留字 → ConfigurationError（compile fail loud）。"""
    p = tmp_path / "wf.yaml"
    p.write_text(RESERVED_WF_YAML_TMPL.format(name=reserved), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(p)
    assert "保留字" in str(exc.value)
    assert reserved in str(exc.value)


def test_non_reserved_wf_name_accepted(tmp_path):
    """非保留字 wf 名正常通过 compile。"""
    p = tmp_path / "wf.yaml"
    p.write_text(RESERVED_WF_YAML_TMPL.format(name="my_writer_wf"), encoding="utf-8")
    wf = load_workflow(p)
    assert wf.name == "my_writer_wf"


# ── §2.1 ``orca <wf>`` 语法糖 ────────────────────────────────────────────────


SIMPLE_WF_YAML = """\
name: sugar_test_wf
description: 语法糖测试 wf。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "node A"
    routes:
      - to: $end
"""


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_wf_sugar_dispatches_to_bootstrap(cwd_tmp):
    """``orca <wf-path>`` ≡ ``orca bootstrap <wf-path>``（首 token 非注册命令 → 重写）。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()

    # 裸 wf 路径（语法糖）；带 --inputs 才真启动（不带只返 schema）
    sugar = runner.invoke(app, [str(p), "--inputs", "{}"])
    assert sugar.exit_code == 0, sugar.output
    sugar_reply = json.loads(sugar.output.splitlines()[-1])

    # 显式 bootstrap（带 --inputs 真启动 → 撞 sugar 的活跃 run → duplicate fail）
    explicit = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{}"])
    # 第二次 bootstrap 同 wf → duplicate-active-run fail loud（§7.3 m12）
    assert explicit.exit_code == 1
    dup = json.loads(explicit.output.splitlines()[-1])
    assert dup["reason"] == "duplicate-active-run"
    assert dup["run_id"] == sugar_reply["run_id"]


def test_wf_sugar_preserves_inputs_option(cwd_tmp):
    """``orca <wf> --inputs '{...}'`` 把 --inputs 透传到 bootstrap。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, [str(p), "--inputs", '{"x":1}'])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False
    assert reply["node"] == "a"


def test_reserved_command_name_not_treated_as_wf(cwd_tmp):
    """``orca status`` 走 status 命令（注册命令优先），不当 wf 名 bootstrap。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    runner.invoke(app, [str(p), "--inputs", "{}"])  # bootstrap 一个 run（带 --inputs 真启动）
    # status 是注册命令 → 走 status（列 run），不重写为 bootstrap
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # status 列表输出含 tape stem（run_id）
    assert ".jsonl" not in result.output


# ── §2.4 ``orca --help`` 含 7 命令、不含 tars ─────────────────────────────


def test_orca_help_lists_seven_commands_no_tars():
    """§2.4：--help 含 list/next/status/stop/open/doctor + <wf> epilog；不含 tars 命令名。"""
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    help_text = result.output

    # 7 命令（注册的 6 + <wf> epilog 提及）
    for cmd in ("list", "next", "status", "stop", "open", "doctor"):
        assert cmd in help_text, f"--help 缺命令 {cmd!r}"
    # <wf> 语法糖在 epilog 提及
    assert "wf" in help_text.lower()

    # 不含 tars 后端命令名（§2.4 守门）
    for tars_cmd in ("run", "serve", "ps", "logs", "wait", "resume",
                      "install", "validate", "mcp", "executor"):
        # 命令名作独立 token 出现（避免 substring 误判，如 "run" in "runtimes"）
        # typer help 列命令名缩进，用 "  run" / "\nrun" 作粗略独立行匹配
        assert f"\n{tars_cmd}" not in help_text and f"  {tars_cmd} " not in help_text, (
            f"--help 不应含 tars 命令 {tars_cmd!r}（§2.4 守门）"
        )

    # v5 §8 step 2b：start 已删（不再出现作命令）；describe 命令从未存在（也守门）
    for gone in ("start", "describe"):
        assert f"\n{gone}" not in help_text and f"  {gone} " not in help_text, (
            f"--help 不应含 {gone!r}（start 已删 / describe 不存在）"
        )


# ── §8 B1：驱动协议 + marker 3 字段 + catalog 单一 ────────────────────────────


def test_drive_protocol_uses_top_level_next_command():
    """B1：驱动协议含 ``orca next``，不含旧 ``orca in-session next`` namespace。"""
    text = _drive_protocol("r-test-123")
    assert "orca next" in text
    assert "orca in-session" not in text
    assert "--run-id r-test-123" in text  # run_id 注入


def test_drive_protocol_documents_single_quote_escaping():
    """§5.2（M7）：驱动协议教模型 ``'\''`` 转义撇号（单个撇号就破 quoting）。"""
    text = _drive_protocol("r-x")
    assert "\\'\\''" in text or "it's" in text  # 转义示例存在


def test_activation_marker_exactly_three_fields():
    """§7.2：ActivationMarker dataclass 字段集 = {run_id, model, no_output_count}。

    coordinator 铁律：marker 必须只 3 字段（无 tape_path/yaml/session_id/owner）。
    """
    fields = set(marker_mod.ActivationMarker.__dataclass_fields__.keys())
    assert fields == {"run_id", "model", "no_output_count"}, (
        f"marker 字段漂移：实得 {fields}"
    )


def test_marker_module_has_no_find_scan():
    """§7.2：删 ``find_marker_by_run_id`` 扫描（改 ``marker_path`` O(1) 直定位）。"""
    assert not hasattr(marker_mod, "find_marker_by_run_id"), (
        "marker 模块不应再有 find_marker_by_run_id（v3 §7.2 删扫描）"
    )


def test_orca_list_returns_name_and_description_only(cwd_tmp):
    """``orca list`` 只返 ``{workflows:[{name, description}]}``（选 wf 用）。

    - inputs_schema **不在此**（移至 ``orca <wf>`` 不带 --inputs 的返回，抽 inputs 用）——选 wf
      阶段不需要全量 schema，塞进 list 是噪音。
    - **无 has_setup / entry / inputs_count**（B3）：只暴露选 wf 所需的 2 字段。
    - 单一 catalog 真相源：与 ``tars list`` 调同一个 ``catalog.list_workflows()``，渲染层不同。
    """
    wf_with_inputs = """\
name: inputs_demo_wf
description: 带 inputs 的 demo wf。
entry: a
inputs:
  topic:
    type: string
    description: 要写的主题
  count:
    type: int
    description: 产出条数
    required: false
    default: 3
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "x"
    routes:
      - to: $end
"""
    p = cwd_tmp / "workflows" / "w.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(wf_with_inputs, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[-1])

    # 顶层契约
    assert "workflows" in payload
    # 不要求恰 1 个（~/.orca/workflows 的全局 wf 也会被 catalog 扫到，见 CURRENT.md 隔离缺陷）；
    # 按名定位本测试建的 wf，校验其字段。
    wfs = {w["name"]: w for w in payload["workflows"]}
    assert "inputs_demo_wf" in wfs, f"未在 list 输出找到 inputs_demo_wf：{list(wfs)}"
    wf = wfs["inputs_demo_wf"]

    # 恰 2 字段（name/description），无 inputs_schema / has_setup / entry / inputs_count
    assert set(wf.keys()) == {"name", "description"}, (
        f"orca list 项字段漂移：实得 {set(wf.keys())}（应恰 name/description，"
        f"inputs_schema 已移至 orca <wf>）"
    )
    assert "inputs_schema" not in wf
    assert "has_setup" not in wf
    assert wf["description"] == "带 inputs 的 demo wf。"


def test_wf_without_inputs_flag_returns_schema_no_run(cwd_tmp):
    """``orca <wf>`` 不带 ``--inputs`` → 只返 ``{name, description, inputs_schema}``，不启动。

    schema 是「启动 wf 时」才需要的信息（抽 inputs 用），故不进 ``orca list``，改由启动命令
    按需带出。本测试验证：不带 --inputs 返 schema 且**不产生 run/tape/marker**（纯只读）。
    """
    wf_yaml = """\
name: schema_demo_wf
description: 测 schema 返回。
entry: a
inputs:
  topic:
    type: string
    description: 要写的主题
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "x"
    routes:
      - to: $end
"""
    p = cwd_tmp / "workflows" / "w.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(wf_yaml, encoding="utf-8")
    runner = CliRunner()

    # 不带 --inputs → 返 schema
    result = runner.invoke(app, ["schema_demo_wf"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert set(reply.keys()) == {"name", "description", "inputs_schema"}
    assert reply["name"] == "schema_demo_wf"
    schema = reply["inputs_schema"]
    assert isinstance(schema, list) and len(schema) == 1
    assert set(schema[0].keys()) == {"name", "type", "description"}
    assert schema[0] == {"name": "topic", "type": "string", "description": "要写的主题"}

    # 不启动：cwd 下根本不应有 runs/（纯只读查询，无副作用）
    runs_dir = cwd_tmp / "runs"
    assert not runs_dir.exists(), "不带 --inputs 不应在 cwd 产生 runs/（纯只读，应无副作用）"


# ── §3.2 tars 命令名变量化（env ORCA_BACKEND_CMD）──────────────────────────


def test_backend_cmd_name_reads_env():
    """§3.2：``ORCA_BACKEND_CMD`` env 控制 backend 命令名（默认 tars）。"""
    from orca.iface.cli.commands import DEFAULT_BACKEND_CMD, backend_cmd_name

    assert DEFAULT_BACKEND_CMD == "tars"
    # 默认（env 未设）
    monkeypatch_env = {"ORCA_BACKEND_CMD": "conductor"}
    with mock.patch.dict("os.environ", monkeypatch_env, clear=False):
        assert backend_cmd_name() == "conductor"
    # env 未设 → 默认 tars
    with mock.patch.dict("os.environ", {}, clear=True):
        assert backend_cmd_name() == "tars"


# ── review 补缺：并发 bootstrap / yaml_path 恢复 / open 默认 / state_corrupt ─────


def test_bootstrap_uses_well_known_serialize_lock(cwd_tmp):
    """review B1：bootstrap 锁用 well-known 路径（``.orca-bootstrap.lock``），NOT per-run_id。

    per-run_id 锁无法 serialize 同 wf 并发（两进程各 gen 不同 run_id → 各锁不同文件 →
    都过 dupe check → 孤儿）。本测试验证 well-known 锁文件落盘（独立于 run_id），保
    TOCTOU 闭环的契约：锁资源选对。
    """
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{}"])
    assert result.exit_code == 0, result.output

    # well-known bootstrap serialize lock 必须落盘（rundir = cwd/runs/）
    lock_file = cwd_tmp / "runs" / ".orca-bootstrap.lock"
    assert lock_file.is_file(), (
        "bootstrap 必须用 well-known `.orca-bootstrap.lock` serialize（review B1），"
        "per-run_id 锁无法防同 wf 并发 TOCTOU"
    )
    # 不应残留 per-run_id 的 marker-level .flock（旧错误设计）
    bad_locks = list((cwd_tmp / "runs").glob("orca-*.json.flock"))
    assert not bad_locks, (
        f"不应有 per-run_id `.json.flock`（TOCTOU 漏洞设计）：{bad_locks}"
    )


def test_next_recovers_wf_from_tape_yaml_path(cwd_tmp, tmp_path):
    """review §7.2：``next`` 从 tape.workflow_started.data.yaml_path 恢复 wf（marker 不存 yaml）。

    bootstrap 把 yaml_path 记入 tape；next 读 tape 反查 → load_workflow。验证非 catalog
    路径（tmp_path 下的 yaml）也能 next 推进。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{}"])
    assert boot.exit_code == 0
    reply = json.loads(boot.output.splitlines()[-1])
    run_id, tape = reply["run_id"], reply["tape"]

    # tape workflow_started.data.yaml_path 应记入（canonical realpath）
    import json as _json
    ws = _json.loads(Path(tape).read_text(encoding="utf-8").splitlines()[0])
    assert ws["type"] == "workflow_started"
    assert ws["data"]["yaml_path"]

    # next 推进（wf 从 tape yaml_path 恢复，非 catalog）
    nxt = runner.invoke(app, ["next", "--run-id", run_id, "--output", "out_a"])
    assert nxt.exit_code == 0
    nxt_reply = _json.loads(nxt.output.splitlines()[-1])
    assert nxt_reply["done"] is True  # 单节点 wf，output 后即 completed


def test_default_active_run_id_no_active_fails_loud(cwd_tmp):
    """review：``orca open`` 无活跃 run → fail loud（exit 1）。cwd_tmp 确保 runs/ 为空。"""
    import typer
    from orca.iface.in_session.cli import _default_active_run_id

    with pytest.raises(typer.Exit) as exc:
        _default_active_run_id()
    assert exc.value.exit_code == 1


def test_default_active_run_id_multiple_active_fails_loud(cwd_tmp):
    """review：多个活跃 run → fail loud（提示指定 run_id）。"""
    import typer
    from orca.iface.in_session.cli import _default_active_run_id, _default_rundir
    from orca.iface.in_session.marker import marker_path, write_marker, ActivationMarker

    rundir = _default_rundir()
    rundir.mkdir(parents=True, exist_ok=True)
    for rid in ("r-a", "r-b"):
        write_marker(marker_path(rundir, rid), ActivationMarker(run_id=rid))
    try:
        with pytest.raises(typer.Exit) as exc:
            _default_active_run_id()
        assert exc.value.exit_code == 1
    finally:
        # 清理：删测试 marker，不污染其他测试。
        for rid in ("r-a", "r-b"):
            (rundir / f"orca-{rid}.json").unlink(missing_ok=True)


def test_default_active_run_id_single_returns_it(cwd_tmp):
    """review：恰好一个活跃 run → 返它（``orca open`` 默认目标）。"""
    from orca.iface.in_session.cli import _default_active_run_id, _default_rundir
    from orca.iface.in_session.marker import marker_path, write_marker, ActivationMarker

    rundir = _default_rundir()
    rundir.mkdir(parents=True, exist_ok=True)
    write_marker(marker_path(rundir, "r-only"), ActivationMarker(run_id="r-only"))
    try:
        assert _default_active_run_id() == "r-only"
    finally:
        (rundir / "orca-r-only.json").unlink(missing_ok=True)


def test_load_wf_for_run_state_corrupt_on_missing_workflow_started(cwd_tmp, tmp_path):
    """review：tape 无 workflow_started → _load_wf_for_run raise state_corrupt。"""
    from orca.iface.in_session.cli import _load_wf_for_run
    from orca.run.step import InSessionError, ERR_STATE_CORRUPT
    from orca.events.tape import Tape

    # 空 tape（无 workflow_started）
    empty_tape = tmp_path / "empty.jsonl"
    empty_tape.write_text("", encoding="utf-8")
    tape = Tape(empty_tape, run_id="r-x", resume=True)
    with pytest.raises(InSessionError) as exc:
        _load_wf_for_run("r-x", tape)
    assert exc.value.error_kind == ERR_STATE_CORRUPT


def test_start_command_removed(cwd_tmp):
    """v5 §8 step 2b(6)：``start`` 命令已删（CC 路 A 退场）。``orca start`` 不再是注册命令。

    裸 token ``start`` 经 ``_OrcaTopLevelGroup.resolve_command`` 当 wf 名重写 → bootstrap
    → ``_resolve_wf_path`` 找不到名/路径 → BadParameter exit 2（fail loud）。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["start", "some_wf"])
    assert result.exit_code == 2  # start 不是命令，重写为 bootstrap → 解析失败


def test_bootstrap_bad_inputs_fails_loud(cwd_tmp):
    """review：``--inputs`` 非 JSON → BadParameter exit 2（fail loud）。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{not json"])
    assert result.exit_code == 2


def test_bootstrap_unresolvable_wf_name_fails_loud(cwd_tmp):
    """review：wf 名既非路径也不在 catalog → BadParameter exit 2。"""
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", "no_such_wf_anywhere_xyz"])
    assert result.exit_code == 2


# ── §4.5 SKILL.md 守门（三步指导 + 禁业务逻辑关键词 + 禁 tars 命令）──────────────
#
# 入口 skill = TARS（用户面）；目录 ``orca/skills/<ENTRY_SKILL_NAME>/``。skill body 仍调 ``orca``
# CLI 命令（引擎不改），故守门断言里的 ``orca list`` / ``orca next`` 字面不变——只目录名换了。

ENTRY_SKILL_MD = (
    Path(__file__).resolve().parents[3]
    / "orca" / "skills" / ENTRY_SKILL_NAME / "SKILL.md"
)


def test_entry_skill_md_frontmatter_name_matches_dir():
    """§4.5 契约锁：frontmatter ``name`` == 目录名 == ``ENTRY_SKILL_NAME`` 三者一致。

    frontmatter ``name`` 是 slash 命令（``/tars``）的触发源；目录名是 install 落地点 +
    doctor 扫描点。三者必须一致——任一漂移（如 builder 改 frontmatter name 却没改目录）
    会导致 slash 触发名与落地目录静默分叉，doctor / install 测试都抓不住（它们只看目录）。
    """
    text = ENTRY_SKILL_MD.read_text(encoding="utf-8")
    # frontmatter name 字段 == 常量值（slash 触发名 == 单一真相源）
    assert re.search(rf"^name:\s*{re.escape(ENTRY_SKILL_NAME)}\s*$", text, re.MULTILINE), (
        f"SKILL.md frontmatter name 应为 {ENTRY_SKILL_NAME!r}（与目录名 / ENTRY_SKILL_NAME 一致）"
    )


def test_entry_skill_md_contains_three_step_guide():
    """§4.5：SKILL.md 必须含三步指导（list → 据 inputs_schema 抽 inputs → <wf> + next 循环）。"""
    text = ENTRY_SKILL_MD.read_text(encoding="utf-8")
    # 第 1 步：orca list 选 wf（CLI 引擎名不变）
    assert "orca list" in text
    assert "inputs_schema" in text
    # 第 2 步：抽 inputs
    assert "--inputs" in text
    # 第 3 步：启动 + 驱动循环（派子代理 + next --output）
    assert "orca next" in text
    assert "--run-id" in text
    assert "--output" in text
    # skill 绝不读 YAML（§4.2 铁律）
    assert "不读" in text or "绝不" in text


def test_entry_skill_md_has_no_business_logic_keywords():
    """§4.5 / §7.6：SKILL.md 禁业务逻辑关键词（CI grep 守门）。

    skill 是主 session 的入口指导，不是 Orca 内部——不得泄露 advance/router/tape/replay/
    compile/load_workflow 等内部路径（否则 LLM 可能误调内部 API 而非走 7 命令接口）。
    """
    text = ENTRY_SKILL_MD.read_text(encoding="utf-8")
    forbidden = [
        "advance_step", "Orchestrator", "router.resolve",
        "Tape", "replay", "compile", "load_workflow",
    ]
    for kw in forbidden:
        assert kw not in text, f"SKILL.md 含禁业务逻辑关键词 {kw!r}（§4.5 守门）"


def test_entry_skill_md_has_no_tars_backend_commands():
    """§2.4：skill 只教 orca 7 命令，禁出现 tars 后端命令名（防第二入口泄漏）。

    注：``list`` 是 orca 与 tars 共享命令（``orca list`` 合法），不在禁词内。
    ``tars`` 同时是入口 skill 的 slash 名（``/tars``），故禁词用 ``tars <子命令>`` 整串
    而非裸 ``tars``——避免误伤 skill 自我引用。
    """
    text = ENTRY_SKILL_MD.read_text(encoding="utf-8")
    # tars-unique 后端命令（run/serve/ps/install/validate/mcp/executor/logs/wait/resume）
    tars_only = ["serve", "validate", "executor", "resume",
                 "tars run", "tars serve", "tars install", "orca install",
                 "tars validate", "tars mcp", "tars executor"]
    for kw in tars_only:
        assert kw not in text, f"SKILL.md 不应含 tars 后端命令 {kw!r}（§2.4 单一接口守门）"


# ── §3.1 单一 catalog 真相源契约（orca list + tars list 共享 catalog）──────────


def test_orca_list_and_tars_list_share_single_catalog(cwd_tmp, monkeypatch):
    """§3.1 / coordinator 铁律：``orca list`` 与 ``tars list`` 都调 ``catalog.list_workflows()``。

    渲染层可不同（orca list 给 skill 出 JSON / tars list 给运营出文本），但**数据源唯一**——
    非两套 list 实现。mock catalog 返 canned 数据并计数，验证双方调用点。
    """
    runner = CliRunner()
    call_count = {"n": 0}

    def _fake_list():
        call_count["n"] += 1
        # canned item（in-session v5 §6.2：catalog 不再返 has_setup；保留 inputs_schema）
        return [{
            "name": "w", "description": "d", "entry": "a",
            "inputs_count": 0, "inputs_schema": [],
        }]

    with mock.patch("orca.compile.catalog.list_workflows", side_effect=_fake_list):
        # orca list（出 JSON）
        r1 = runner.invoke(app, ["list"])
        assert r1.exit_code == 0, r1.output
        # tars list（commands.app 的 list 命令委托 run_list → catalog，出文本）
        from orca.iface.cli.commands import app as tars_app
        r2 = runner.invoke(tars_app, ["list"])
        assert r2.exit_code == 0, r2.output

    assert call_count["n"] == 2, (
        "orca list 与 tars list 应各调 catalog.list_workflows() 一次（单一真相源）"
    )

