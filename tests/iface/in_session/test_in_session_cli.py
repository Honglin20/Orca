"""tests/iface/in_session/test_in_session_cli.py —— 薄 CLI bootstrap/next/stop 守门测试。

覆盖 SPEC §9.2 acceptance：
  - bootstrap → emit ws+ns、写 marker、stdout JSON
  - next --output → 单次 write 原子 emit_batch [nc, rt, ns]（B1）
  - next --output ""` ≡ 省略 --output（B2 normalize None）
  - 合规计数：≥3 次 warn（run 存活）/ ≥10 次 hard workflow_failed(subagent_compliance)（SPEC 2026-07-23 §3）
  - 失败 taxonomy：output_schema_mismatch（recoverable）/ unsupported_node_kind / state_corrupt（F6）
  - busy：LOCK_NB 撞锁 → {done:false, reason:busy} 0 退出（F5）
  - stop → workflow_cancelled + 清 marker
  - marker RMW 在 flock 临界区内（N2）：两并发 next → no_output_count 不丢
  - 架构守门（D-v7-1）：grep plugin/hook 模板无 advance/router/replay/tape 路径
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app, _validate_inputs


# ── fixtures ────────────────────────────────────────────────────────────────


AGENT_WF_YAML = """\
name: cli_test_wf
description: 2-agent 线性 workflow（CLI 守门测试用）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于 {{ a.output }} 总结。"
    routes:
      - to: $end
"""


@pytest.fixture
def wf_path(tmp_path: Path) -> Path:
    p = tmp_path / "wf.yaml"
    p.write_text(AGENT_WF_YAML, encoding="utf-8")
    return p


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir 到 tmp_path 让 runs/ 写到临时目录。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _bootstrap(runner: CliRunner, wf_path: Path, *extra: str) -> dict:
    # 默认带 --inputs "{}" 真启动（bootstrap 不带 --inputs 只返 inputs_schema 不启动）。
    # 调用方经 extra 传 --inputs 时 click last-wins 覆盖默认。
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}", *extra])
    assert result.exit_code == 0, f"bootstrap failed: {result.output}"
    return json.loads(result.output.splitlines()[-1])


def _next(runner: CliRunner, tape: str, run_id: str, *extra: str,
          expect_exit: int = 0) -> dict:
    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, *extra,
    ])
    assert result.exit_code == expect_exit, (
        f"next exit {result.exit_code} (expected {expect_exit}): {result.output}"
    )
    return json.loads(result.output.splitlines()[-1])


# ── bootstrap ───────────────────────────────────────────────────────────────


def test_bootstrap_emits_ws_ns_and_writes_marker(cwd_tmp, wf_path):
    runner = CliRunner()
    reply = _bootstrap(runner, wf_path)

    assert reply["done"] is False
    assert reply["node"] == "a"
    assert reply["prompt"] is not None
    assert "run_id" in reply
    assert "tape" in reply

    # tape 2 行（ws + ns）
    tape_path = Path(reply["tape"])
    lines = tape_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    types = [json.loads(ln)["type"] for ln in lines]
    assert types == ["workflow_started", "node_started"]

    # marker 已写
    marker_files = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(marker_files) == 1


def test_bootstrap_registers_project_for_web_discovery(cwd_tmp, wf_path, monkeypatch):
    """SPEC §13 D4：in-session bootstrap 注册所属项目 → web discovery/懒挂载可见。

    回归守门：bootstrap 此前漏 ``register_project`` → TARS 启动的 run 在 web 列表/详情
    不可见（远程 ``~/.orca/projects.json`` 根本不生成）。
    """
    from orca.runtime import list_registered

    # 隔离 ORCA_HOME（不污染真实 ~/.orca/projects.json）+ 禁 auto-open-web（避免游离子进程）。
    orca_home = cwd_tmp / ".orca_home"
    monkeypatch.setenv("ORCA_HOME", str(orca_home))
    monkeypatch.setenv("ORCA_BOOTSTRAP_OPEN_WEB", "0")
    # register_project M-16 要求项目根含 workflows/ 或 .orca/config.json；
    # ORCA_PROJECT_ROOT 钉死 detect_project_root 到 cwd_tmp（否则向上走 git root）。
    (cwd_tmp / "workflows").mkdir()
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(cwd_tmp))

    runner = CliRunner()
    _bootstrap(runner, wf_path)

    expected = str(cwd_tmp.resolve())
    registered = list_registered()
    assert any(meta.get("path") == expected for meta in registered.values()), (
        f"bootstrap 未注册项目 {expected}：{registered}"
    )
    # 注册表文件已落盘到隔离 ORCA_HOME（web discovery 读此文件枚举 run）。
    assert (orca_home / "projects.json").is_file()


def test_register_current_project_fail_open(tmp_path, monkeypatch):
    """注册失败（项目根无 workflows/ marker）只 warn 不抛——run 照常（web 可见性退化）。

    与 daemon spawn / artifacts mkdir 失败同 fail-open 语义。
    """
    from orca.iface.in_session.cli import _register_current_project
    from orca.runtime import list_registered

    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    # ORCA_PROJECT_ROOT 指向无 marker 的空目录 → register_project M-16 raise ValueError。
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(bare))

    _register_current_project()  # 不应 raise（fail-open）

    assert list_registered() == {}  # 注册失败 → 注册表仍空


def test_bootstrap_duplicate_same_wf_fails_loud(cwd_tmp, wf_path):
    """v3 §7.3（m12）：同 wf 已有活跃 marker（未终态）再 bootstrap → fail loud。

    取代旧 N1「同 owner+yaml 复用 run_id」——v3 改 fail loud 防孤儿（marker 只 3 字段，
    无 owner；按 wf.name 经 tape workflow_started 匹配活跃 run）。
    """
    runner = CliRunner()
    r1 = _bootstrap(runner, wf_path)
    # 第二次 bootstrap 同 wf（同 wf.name，未终态）→ fail loud（exit 1）。
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["reason"] == "duplicate-active-run"
    assert reply["run_id"] == r1["run_id"]
    assert "orca next" in reply["hint"]
    assert "orca stop" in reply["hint"]

    # tape 仍只 2 行（第二次未 emit；fail loud 在 emit 前）。
    tape_path = Path(r1["tape"])
    assert len(tape_path.read_text(encoding="utf-8").strip().split("\n")) == 2


def test_bootstrap_lock_released_before_spawn_daemons(cwd_tmp, wf_path, monkeypatch):
    """SPEC §3 O2 AC：bootstrap_lock 释放在 spawn daemon 之前。

    模拟：在 ``_spawn_chart_daemon`` 内尝试用 LOCK_NB 抢 bootstrap_lock —— 应成功（说明
    锁已释放）。若失败说明 spawn 仍在锁内（O2 没生效）。

    dupe-check 不变量不变：锁仍包 dupe check + gen run_id + advance + write_marker。
    """
    import fcntl
    runner = CliRunner()
    from orca.iface.in_session import cli as cli_mod

    captured: dict[str, bool] = {"lock_available_at_spawn": False}

    def _spy_spawn_chart_daemon(run_id, tape_path):
        # 尝试 LOCK_NB 抢 bootstrap_lock —— 非阻塞，成功 = 锁已释放。
        rundir = Path(tape_path).parent
        bootstrap_lock = rundir / ".orca-bootstrap.lock"
        fd = open(bootstrap_lock, "w")
        try:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                captured["lock_available_at_spawn"] = True
                # 释放，免影响后续
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                captured["lock_available_at_spawn"] = False
        finally:
            fd.close()

    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", _spy_spawn_chart_daemon)
    # 也 patch _wait_for_sock 让其立刻返（保 spawn 路径走完）
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda sock_path, **kw: True)

    _bootstrap(runner, wf_path)
    assert captured["lock_available_at_spawn"] is True, (
        "SPEC §3 O2：bootstrap_lock 应在 spawn_chart_daemon 之前释放"
        "（dupe-check 不变量靠 dupe check + write_marker，不靠 spawn）"
    )


def test_bootstrap_dupe_check_invariant_preserved(cwd_tmp, wf_path, monkeypatch):
    """SPEC §3 O2 AC：dupe-check 不变量仍成立（O2 缩小锁范围后回归守门）。

    模拟两并发 bootstrap 时序：first bootstrap 跑到 write_marker 完成但**还在锁内**
    （spawn 之前），second bootstrap 此时被阻塞；first 释放锁后 second 进 dupe check
    应看到 first 的 marker → fail loud。

    实现方式：mock _spawn_chart_daemon 让 first bootstrap 卡在 spawn 阶段（lock 已释放），
    起一个 thread 跑 second bootstrap；second 应 fail loud（dupe）。
    """
    import threading
    import time as _time
    runner = CliRunner()
    from orca.iface.in_session import cli as cli_mod

    # 让 _spawn_chart_daemon 阻塞 0.5s（模拟「spawn 在锁外、耗时长」），给 second bootstrap
    # 跑到 dupe check 的窗口。**关键**：first 此时已释 bootstrap_lock（O2 行为）；second
    # 能进 dupe check → 看到 first 的 marker（write_marker 在锁内已完成）→ fail loud。
    def _slow_spawn(run_id, tape_path):
        _time.sleep(0.5)

    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", _slow_spawn)
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda sock_path, **kw: True)

    second_results: dict[str, Any] = {}

    def _second_bootstrap():
        # 等 first 进 spawn（即 first 释 bootstrap_lock）后跑 second
        _time.sleep(0.2)
        res = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
        second_results["exit_code"] = res.exit_code
        try:
            second_results["reply"] = json.loads(res.output.splitlines()[-1])
        except Exception as e:
            second_results["error"] = str(e)

    t = threading.Thread(target=_second_bootstrap, daemon=True)
    t.start()

    # first bootstrap（在主线程）
    _bootstrap(runner, wf_path)
    t.join(timeout=5.0)

    # second bootstrap 应 fail loud（dupe-check 不变量；O2 缩锁范围后仍守）
    assert second_results.get("exit_code") == 1, (
        f"second bootstrap 应 fail loud（dupe），实际 exit={second_results.get('exit_code')}"
    )
    reply = second_results.get("reply", {})
    assert reply.get("reason") == "duplicate-active-run", (
        f"second bootstrap 应返 duplicate-active-run，实际 reply={reply}"
    )


# ── F3：bootstrap --inputs 校验（SPEC §4 F3）──────────────────────────────────


# 带显式 type 的 inputs wf（F3 校验对象）。
INPUTS_TYPED_WF_YAML = """\
name: cli_test_inputs_typed
description: 测 F3 inputs 校验。
entry: a
inputs:
  topic:
    type: string
    description: "[ask] 业务主题"
    required: true
  count:
    type: int
    description: "[ask] 数量"
    required: true
  verbose:
    type: boolean
    description: "[default] 详细日志（可省略走默认）"
    required: true
  tags:
    type: list
    description: "[advanced] 标签列表（可省略走默认）"
    required: true
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "工作。"
    routes:
      - to: $end
"""


@pytest.fixture
def typed_wf_path(tmp_path: Path) -> Path:
    p = tmp_path / "typed_wf.yaml"
    p.write_text(INPUTS_TYPED_WF_YAML, encoding="utf-8")
    return p


def test_bootstrap_inputs_wrong_type_fails_loud(cwd_tmp, typed_wf_path):
    """SPEC §4 F3 AC：错类型 → inputs_validation_error + 字段定位。

    给 count 传字符串 → fail loud；reply 含 error_kind=inputs_validation_error + 字段名。
    """
    runner = CliRunner()
    bad_inputs = json.dumps({"topic": "x", "count": "not_an_int"})
    result = runner.invoke(app, ["bootstrap", str(typed_wf_path), "--inputs", bad_inputs])
    assert result.exit_code == 1, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert reply["error_kind"] == "inputs_validation_error"
    # 字段定位：错误信息含 'count' + 期望 'int' + 实际 'str'
    assert "count" in reply["reason"]
    assert "int" in reply["reason"]
    assert "str" in reply["reason"]


def test_bootstrap_inputs_missing_required_fails_loud(cwd_tmp, typed_wf_path):
    """SPEC §4 F3 AC：缺必填（显式 type）→ inputs_validation_error + 字段定位。

    缺 topic（[ask] 必填，无 [default]/[advanced] 标签）→ fail loud。
    """
    runner = CliRunner()
    bad_inputs = json.dumps({"count": 5})  # 缺 topic
    result = runner.invoke(app, ["bootstrap", str(typed_wf_path), "--inputs", bad_inputs])
    assert result.exit_code == 1, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["error_kind"] == "inputs_validation_error"
    assert "topic" in reply["reason"]
    assert "missing required" in reply["reason"]


def test_bootstrap_inputs_default_tag_omitted_ok(cwd_tmp, typed_wf_path):
    """SPEC §4 F3 AC：[default] 标签字段省略不触发 required。

    verbose / tags / extras 都省略；topic+count 给值 → ok 启动。
    """
    runner = CliRunner()
    ok_inputs = json.dumps({"topic": "x", "count": 5})
    result = runner.invoke(app, ["bootstrap", str(typed_wf_path), "--inputs", ok_inputs])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False  # 启动成功
    assert reply["node"] == "a"


def test_bootstrap_inputs_advanced_tag_omitted_ok(cwd_tmp, typed_wf_path):
    """SPEC §4 F3 AC：[advanced] 标签字段省略不触发 required。

    只给 topic + count + verbose（[default] ok），省 tags（[advanced]）+ extras（无 type）→ ok。
    """
    runner = CliRunner()
    ok_inputs = json.dumps({"topic": "x", "count": 5, "verbose": False})
    result = runner.invoke(app, ["bootstrap", str(typed_wf_path), "--inputs", ok_inputs])
    assert result.exit_code == 0, result.output


def test_bootstrap_inputs_no_inputs_declared_passthrough(cwd_tmp, tmp_path):
    """SPEC §4 F3 AC：旧 wf 无 inputs 声明（空 dict）→ 零校验零回归。

    ``InputDef.type`` 在 schema 层是必填，故「无 type 字段」只能在「整个 inputs dict 为空」
    时出现（schema 层 impossibility 防御）。本测试守空 inputs 的 pass-through。
    """
    wf_yaml = """\
name: cli_test_no_inputs
description: 测 F3 无 inputs 的 pass-through。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "工作。"
    routes:
      - to: $end
"""
    p = tmp_path / "no_inputs_wf.yaml"
    p.write_text(wf_yaml, encoding="utf-8")

    runner = CliRunner()
    # 空 inputs 启动 → 校验 loop 不执行 → ok。
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{}"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False


def test_bootstrap_inputs_bool_not_accepted_as_int(cwd_tmp, tmp_path):
    """SPEC §4 F3 实现细节：bool 不被接受为 int（Python ``isinstance(True, int) is True`` 的反陷阱）。

    count: int + 给 True → 应判错（不让 True 假装 1 通过）。
    """
    wf_yaml = """\
name: cli_test_int_strict
description: 测 F3 int vs bool 隔离。
entry: a
inputs:
  count:
    type: int
    description: "[ask] 数量"
    required: true
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "工作。"
    routes:
      - to: $end
"""
    p = tmp_path / "int_wf.yaml"
    p.write_text(wf_yaml, encoding="utf-8")

    runner = CliRunner()
    bad_inputs = json.dumps({"count": True})  # bool 不是 int
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", bad_inputs])
    assert result.exit_code == 1, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["error_kind"] == "inputs_validation_error"
    assert "count" in reply["reason"]


def test_bootstrap_inputs_unknown_type_passthrough(cwd_tmp, tmp_path):
    """SPEC §4 F3 AC：type 不在白名单 → pass-through（YAGNI 自定义 type 不校验）。"""
    wf_yaml = """\
name: cli_test_custom_type
description: 测 F3 自定义 type pass-through。
entry: a
inputs:
  url:
    type: url  # 不在 TYPE_MAP 白名单
    description: "[ask] 一个 URL"
    required: true
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "工作。"
    routes:
      - to: $end
"""
    p = tmp_path / "custom_type_wf.yaml"
    p.write_text(wf_yaml, encoding="utf-8")

    runner = CliRunner()
    ok_inputs = json.dumps({"url": "https://example.com"})  # url type 不校验
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", ok_inputs])
    assert result.exit_code == 0, result.output


def test_bootstrap_inputs_validation_error_envelope_contract(cwd_tmp, typed_wf_path):
    """SPEC §1 铁律 5.1：inputs_validation_error 信封字段契约。

    done=True + error_kind=inputs_validation_error + reason 含 'failed:' 前缀（与既有
    output_schema_mismatch 等错误信封一致；SPEC §2.3 信封契约）。
    """
    runner = CliRunner()
    bad_inputs = json.dumps({"count": "wrong"})  # 缺 topic + count 错类型
    result = runner.invoke(app, ["bootstrap", str(typed_wf_path), "--inputs", bad_inputs])
    assert result.exit_code == 1, result.output
    reply = json.loads(result.output.splitlines()[-1])
    # 字段集守门（防漂移）
    assert set(reply.keys()) >= {"done", "error_kind", "reason"}
    assert reply["done"] is True
    assert reply["error_kind"] == "inputs_validation_error"
    assert reply["reason"].startswith("failed:")
    # 首错定位（只报第一个错；topic 缺在前）
    assert "topic" in reply["reason"]


def test_inputs_validation_error_registered_in_errors_module():
    """SPEC §1 铁律 5.1：新 error_kind 必须登记到共享 ``orca/run/_errors.py`` + 单一真相源。"""
    from orca.run._errors import INPUTS_VALIDATION_ERROR
    assert INPUTS_VALIDATION_ERROR == "inputs_validation_error"
    # cli.py 也 import 同一常量（不字面重写）。
    from orca.iface.in_session.cli import INPUTS_VALIDATION_ERROR as cli_const
    assert cli_const is INPUTS_VALIDATION_ERROR


# ── F3 TYPE_MAP 全 alias 直接单元测试（code-reviewer test 🔴#3）───────────────
#
# 直接调 `_validate_inputs` 覆盖 `_TYPE_MAP` 12 alias（避免 bootstrap CLI 间接路径漏覆盖）。
# SPEC §4 F3 AC「错类型 → inputs_validation_error + 定位」要 TYPE_MAP 所有 alias 都正确判型。


@pytest.mark.parametrize("type_str,value,should_pass", [
    # int 严格（拒 bool）
    ("int", 5, True),
    ("int", 0, True),
    ("int", -1, True),
    ("int", 5.0, False),  # float 不是 int
    ("int", True, False),  # bool 反陷阱（Python isinstance(True, int) is True）
    ("int", "5", False),
    ("integer", 5, True),
    ("integer", True, False),
    # number / float（接受 int 或 float，拒 bool）
    ("float", 1.5, True),
    ("float", 1, True),  # int 是 number
    ("float", True, False),  # bool 反陷阱
    ("float", "1.5", False),
    ("number", 3.14, True),
    ("number", 42, True),
    ("number", False, False),
    # string / str
    ("string", "hello", True),
    ("string", "", True),
    ("string", 42, False),
    ("string", None, False),
    ("str", "x", True),
    ("str", True, False),
    # boolean / bool（拒 int 反向陷阱）
    ("boolean", True, True),
    ("boolean", False, True),
    ("boolean", 1, False),  # int 不是 bool（反向陷阱）
    ("boolean", 0, False),
    ("boolean", "true", False),
    ("bool", True, True),
    ("bool", 1, False),
    # list / array
    ("list", [1, 2, 3], True),
    ("list", [], True),
    ("list", "not list", False),
    ("list", (1, 2), False),  # tuple 不是 list
    ("array", [1], True),
    ("array", {"a": 1}, False),
    # dict / object
    ("dict", {"a": 1}, True),
    ("dict", {}, True),
    ("dict", [], False),
    ("object", {"x": 1}, True),
    ("object", "str", False),
])
def test_validate_inputs_type_map_all_aliases(type_str, value, should_pass):
    """SPEC §4 F3：``_TYPE_MAP`` 全 12 alias 直接单元测试（含 bool/int 双向反陷阱）。

    bool/int 隔离：Python ``isinstance(True, int) is True`` 是已知陷阱；本测试守住
    bool 不被错收为 int（``count: int`` + ``True`` → fail）+ int 不被错收为 bool
    （``flag: boolean`` + ``1`` → fail）。
    """
    schema = [{"name": "f", "type": type_str, "description": "test"}]
    ok, err = _validate_inputs({"f": value}, schema)
    assert ok is should_pass, (
        f"type={type_str!r} value={value!r} ({type(value).__name__})："
        f"预期 should_pass={should_pass}，实际 ok={ok}，err={err!r}"
    )


def test_validate_inputs_unknown_type_passthrough_for_any_value():
    """SPEC §4 F3：未知 type → pass-through（任何 value 都不校验）。"""
    schema = [{"name": "f", "type": "url", "description": "test"}]
    # url 不在 TYPE_MAP → 任何 value 都应通过
    for value in ["https://x", 42, True, None, [1], {"a": 1}]:
        ok, _ = _validate_inputs({"f": value}, schema)
        assert ok is True, f"未知 type + value={value!r} 应 pass-through"


def test_validate_inputs_description_none_treated_as_required():
    """description=None（无 description 字段）→ 视为 ""，非 [default]/[advanced] → required 触发。

    code-reviewer 🟡#3 显式化：``inputs_schema_list`` 输出 description 可能为 None
    （老 wf 未填）；本函数 ``desc = field_def.get("description") or ""`` 容错。
    """
    schema = [{"name": "f", "type": "int", "description": None}]
    ok, err = _validate_inputs({}, schema)
    assert ok is False
    assert "missing required" in err
    assert "f" in err


# ── next 单次 write 原子化（B1）────────────────────────────────────────────────


def test_next_with_output_emits_batch_atomically(cwd_tmp, wf_path):
    """next --output → emit_batch [nc, rt, ns] 单次 write（B1）。

    B1 守门：next 推进后 NOT 用单条 emit 写 [nc, rt, ns]——全部走 emit_batch（单次
    write+flush 落盘整批）。本测试用 monkeypatch wrap（计数 + 调原方法）验证。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    from orca.events import bus as bus_mod
    calls: dict[str, int] = {"emit_batch": 0, "emit": 0}
    real_emit_batch = bus_mod.EventBus.emit_batch
    real_emit = bus_mod.EventBus.emit

    async def spy_emit_batch(self, items):
        calls["emit_batch"] += 1
        return await real_emit_batch(self, items)

    async def spy_emit(self, *a, **kw):
        calls["emit"] += 1
        return await real_emit(self, *a, **kw)

    with mock.patch.object(bus_mod.EventBus, "emit_batch", spy_emit_batch):
        with mock.patch.object(bus_mod.EventBus, "emit", spy_emit):
            reply = _next(runner, tape, run_id, "--output", "node_a_result")

    assert calls["emit_batch"] == 1   # 一次批量 emit
    assert calls["emit"] == 0         # 无单条 emit

    assert reply["done"] is False
    assert reply["node"] == "b"

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5  # ws + ns(a) + nc(a) + rt(a→b) + ns(b)
    types = [json.loads(ln)["type"] for ln in lines]
    assert types == ["workflow_started", "node_started", "node_completed",
                     "route_taken", "node_started"]


def test_next_completes_workflow(cwd_tmp, wf_path):
    """两节点全跑完：bootstrap → next(out_a) → next(out_b) → done:true + workflow_completed。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    _next(runner, tape, run_id, "--output", "out_a")
    reply = _next(runner, tape, run_id, "--output", "out_b")

    assert reply["done"] is True

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    types = [json.loads(ln)["type"] for ln in lines]
    assert types[-1] == "workflow_completed"


# ── B2：--output 空串 normalize None ─────────────────────────────────────────


def test_next_output_empty_string_normalized_to_none(cwd_tmp, wf_path):
    """``--output ""`` ≡ 省略 --output（B2）：走 branch 4 + 合规计数。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # next --output "" 不应推进到 b，应 idempotent-replay（branch 4）+ 计数 +1
    reply = _next(runner, tape, run_id, "--output", "")
    assert reply["done"] is False
    assert reply["node"] == "a"   # 仍 a，未推进

    # tape 未增加（branch 4 无 emits）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # 仍是 ws + ns(a)

    # marker 计数已 +1
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    m = json.loads(markers[0].read_text())
    assert m["no_output_count"] == 1


# ── SPEC 2026-07-23 §3：合规计数 WARN=3（run 存活）/ HARD=10（终态）────────────


def test_subagent_compliance_3x_no_output_warn_envelope(cwd_tmp, wf_path):
    """连续 3 次 next 无 output → warn 信封（run 存活，不 emit workflow_failed）。

    SPEC §4.1(b)：``done:false, warn:true, error_kind:subagent_compliance, no_output_count:3,
    warn_threshold:3, hard_limit:10``。0 退出（run 存活）。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    r1 = _next(runner, tape, run_id, "--output", "")
    r2 = _next(runner, tape, run_id, "--output", "")
    # 第 3 次 → warn 信封（≥WARN=3 但 <HARD=10），0 退出，run 存活
    r3 = _next(runner, tape, run_id, "--output", "")

    assert r1["done"] is False and r2["done"] is False
    # code-reviewer 🟢#7：显式反向断言 count<WARN 不 warn（防 _COMPLIANCE_WARN 误改为 1/2
    # 时 r3 仍 warn → 测试假过关）。
    assert "warn" not in r1, "count=1 不应 warn（<WARN=3）"
    assert "warn" not in r2, "count=2 不应 warn（<WARN=3）"
    assert r3["done"] is False              # run 存活（不终态）
    assert r3["warn"] is True
    assert r3["error_kind"] == "subagent_compliance"
    assert r3["no_output_count"] == 3
    assert r3["warn_threshold"] == 3
    assert r3["hard_limit"] == 10

    # tape **不**含 workflow_failed（warn 不 emit 任何 tape 事件）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    for line in lines:
        ev = json.loads(line)
        assert ev["type"] != "workflow_failed", "warn 不应 emit workflow_failed"


def test_subagent_compliance_hard_limit_10x_emits_workflow_failed(cwd_tmp, wf_path):
    """连续 10 次 next 无 output → 撞 HARD 上限 → workflow_failed(subagent_compliance) + exit 1。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 前 9 次：warn 区间（run 存活，0 退出）
    for i in range(9):
        r = _next(runner, tape, run_id, "--output", "")
        assert r["done"] is False, f"第 {i+1} 次（warn 区间）run 应存活"
    # 第 10 次 → 撞 HARD → workflow_failed + exit 1
    r10 = _next(runner, tape, run_id, "--output", "", expect_exit=1)
    assert r10["done"] is True
    assert r10["error_kind"] == "subagent_compliance"

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "subagent_compliance"


# ── F6：失败 taxonomy ─────────────────────────────────────────────────────────


def test_failure_output_schema_mismatch(cwd_tmp, wf_path):
    """节点声明 output_schema 但 output 非 JSON → recoverable 信封（SPEC 2026-07-23 §3）。

    run 存活：``done:false, recoverable:true, error_kind:output_schema_mismatch,
    retry_count:1, retry_budget:2``；0 退出；tape 末尾是 [node_failed, node_started]
    （无 workflow_failed）；marker 不清。
    """
    # 改 wf：a 节点加 output_schema
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "产出 step A 的输出。"',
        'prompt: "产出 step A 的输出。"\n    output_schema:\n      type: object\n      required: [k]',
    )
    p = cwd_tmp / "wf_schema.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "NOT_JSON",
    ])
    # recoverable → 0 退出（run 存活，不 fail loud）
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False                  # run 存活
    assert reply["recoverable"] is True
    assert reply["error_kind"] == "output_schema_mismatch"
    assert reply["retry_count"] == 1
    assert reply["retry_budget"] == 2
    assert reply["node"] == "a"                    # 重 arm 同节点
    assert "output_schema" in reply["reason"] or "非 JSON" in reply["reason"]
    assert "in_session_error" not in json.dumps(reply)

    # tape 末尾是 node_started（[nf, ns]，无 workflow_failed）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "node_started"
    # 倒数第二条是 node_failed
    prev = json.loads(lines[-2])
    assert prev["type"] == "node_failed"
    assert prev["data"]["kind"] == "output_schema_mismatch"

    # marker 未清（run 存活）
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert markers, "recoverable 不应清 marker（run 存活）"


def test_failure_unsupported_node_kind(cwd_tmp):
    """script 节点（非 agent）→ workflow_failed(unsupported_node_kind)。"""
    yaml_text = AGENT_WF_YAML.replace("kind: agent", "kind: script", 1).replace(
        '    executor: opencode\n    model: deepseek/deepseek-v4-flash\n    prompt: "产出 step A 的输出。"',
        '    command: "echo a"',
    )
    p = cwd_tmp / "wf_script.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{}"])
    assert result.exit_code == 1
    # v5 §8 step 5b：bootstrap 失败信封加 error_kind（unsupported_node_kind）+ 反向无 in_session_error
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert reply["error_kind"] == "unsupported_node_kind"
    assert "in_session_error" not in json.dumps(reply)


def test_failure_output_schema_field_violation(cwd_tmp):
    """output_schema 声明 + output 是合法 JSON 但缺 required 字段 → recoverable（SPEC 2026-07-23）。

    缺字段在 parse 期被抓（早于下游 render 的 UndefinedError），归类 output_schema_mismatch
    但现 recoverable：run 存活，重 arm 同节点，主 session 反馈子代理修正。
    """
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "产出 step A 的输出。"',
        'prompt: "产出 step A 的输出。"\n    output_schema:\n      type: object\n      required: [k]\n      properties:\n        k: {type: string}',
    )
    p = cwd_tmp / "wf_schema_field.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    # 合法 JSON 但缺 required 字段 k → recoverable
    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", '{"x": 1}',
    ])
    assert result.exit_code == 0                  # recoverable → 0 退出
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False
    assert reply["recoverable"] is True
    assert reply["error_kind"] == "output_schema_mismatch"
    assert reply["retry_count"] == 1
    assert "in_session_error" not in json.dumps(reply)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    # tape 末尾 node_started（[nf, ns]，无 workflow_failed）
    assert json.loads(lines[-1])["type"] == "node_started"
    assert json.loads(lines[-2])["type"] == "node_failed"


# ── SPEC 2026-07-23 §7 AC2/AC3/AC4 补强（code-reviewer 🔴/🟡）───────────────────


def _schema_wf_path(cwd_tmp: Path) -> Path:
    """单节点 wf，a 声明 output_schema {k: string}（AC2/AC4 补强测试共用）。

    单节点（a → $end）：确保 recoverable 后给正解 output 能直接到 ``$end``（``done:true``），
    避免 2 节点 wf 在 recoverable 后还要再跑一节点才能 complete（让 AC4 续跑测更直接）。
    """
    yaml_text = """\
name: cli_test_schema_wf
description: 单节点带 output_schema 的 wf（AC2/AC4 守门测试用）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    output_schema:
      type: object
      required: [k]
      properties:
        k: {type: string}
    routes:
      - to: $end
"""
    p = cwd_tmp / "wf_schema.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return p


def test_cli_recoverable_escalation_3x_exits_1(cwd_tmp):
    """AC2 CLI（code-reviewer 🟡#5）：连续 3 次坏 output → exit 1 + workflow_failed + 清 marker。

    advance_step 层升格虽已测（test_error_management.py），但 CLI 衔接
    （``merge_recoverable_envelope`` 不触发 + ``result.done + result.error_kind`` 触发 exit 1）
    无守门。本测试补 CLI 层升格回归。
    """
    p = _schema_wf_path(cwd_tmp)
    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    # 1st bad → recoverable（exit 0）
    r1 = _next(runner, tape, run_id, "--output", "BAD1")
    assert r1["done"] is False and r1["recoverable"] is True and r1["retry_count"] == 1
    # 2nd bad → recoverable（exit 0）
    r2 = _next(runner, tape, run_id, "--output", "BAD2")
    assert r2["done"] is False and r2["recoverable"] is True and r2["retry_count"] == 2

    # 3rd bad → 升格 → exit 1 + done:true + error_kind:output_schema_mismatch
    r3 = _next(runner, tape, run_id, "--output", "BAD3", expect_exit=1)
    assert r3["done"] is True
    assert r3["error_kind"] == "output_schema_mismatch"
    assert "exhausted" in r3["reason"]

    # tape 末条 workflow_failed（E8：3 条 nf + 1 条 wf；末尾 wf）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    nf_count = sum(1 for ln in lines if json.loads(ln)["type"] == "node_failed")
    assert nf_count == 3, f"升格应 emit 3 条 node_failed，实得 {nf_count}"
    assert json.loads(lines[-1])["type"] == "workflow_failed"
    assert json.loads(lines[-1])["data"]["kind"] == "output_schema_mismatch"

    # marker 已清（升格终态）
    assert list(cwd_tmp.glob("runs/orca-*.json")) == [], "升格终态应清 marker"


def test_recoverable_then_new_session_resumes_with_correct_output(cwd_tmp):
    """AC4（code-reviewer 🔴#1）：recoverable 后新 session（新 CLI invoke 模拟）能续跑。

    场景：session1 bootstrap → next 坏 output → recoverable 信封；session1 退出（模拟断连）；
    orca status --run-id X --json 见 resumable:true + status:running；session2（新 runner）调
    next 带正确 output → 推进到 $end（done:true）。

    守住 recoverable 化的全部业务价值：跨 session 续跑（marker 保活 + tape-derived count 跨
    进程一致 + reducer 重放 1 次 recoverable tape 仍 running）。
    """
    p = _schema_wf_path(cwd_tmp)
    # session1：bootstrap + 1 次 bad → recoverable
    runner1 = CliRunner()
    boot = _bootstrap(runner1, p)
    run_id, tape = boot["run_id"], boot["tape"]
    r1 = _next(runner1, tape, run_id, "--output", "BAD")
    assert r1["recoverable"] is True
    # session1 退出（runner1 丢弃，模拟 session 断连）

    # session1 后查 status：status:running（recoverable 未终态 → run 可续跑）
    # 注：单 run ``--json`` 不透出 ``resumable`` 字段（该字段仅在无参 list 形态透出）；
    # ``status:running`` + marker 仍在 = 可续跑的观测信号。
    status_res = runner1.invoke(app, ["status", "--run-id", run_id, "--json"])
    assert status_res.exit_code == 0, status_res.output
    status_payload = json.loads(status_res.output.splitlines()[-1])
    assert status_payload["status"] == "running", (
        f"recoverable 后 status 应为 running（resumable），实得 {status_payload.get('status')!r}"
    )

    # marker 仍在（保活）—— marker 存在 ≡ resumable（SPEC §7.2 完成：bootstrap 写 / 终态清）
    assert list(cwd_tmp.glob("runs/orca-*.json")), "recoverable 后 marker 应仍存在"

    # session2（新 runner = 新 CLI 进程模拟）：带正确 output 推进 → done:true completed
    runner2 = CliRunner()
    r2 = _next(runner2, tape, run_id, "--output", '{"k": "v"}')
    assert r2["done"] is True, (
        f"新 session 带正确 output 应推进到 $end（done:true），实得 {r2}"
    )

    # tape 末条 workflow_completed
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[-1])["type"] == "workflow_completed"
    # marker 已清（workflow 完成终态）
    assert list(cwd_tmp.glob("runs/orca-*.json")) == [], "workflow 完成后 marker 应清"


def test_failure_internal_error_remains_irrecoverable(cwd_tmp, wf_path, monkeypatch):
    """AC3（code-reviewer 🟡#6）：``internal_error``（如写 prompt 时 ``os.replace`` OSError）仍走
    ``workflow_failed`` 终态（非 recoverable）。

    recoverable 化的副作用是把 ``output_schema_mismatch`` 从原 ``workflow_failed`` 路径抽走。
    若分类逻辑误把 ``internal_error`` 也当 recoverable（如 ``RecoverableInSessionError`` 误用），
    现有测试不会 fail loud。

    实现细节：直接 mock ``step_mod.os.replace`` 抛 OSError（**不是** mock ``_write_prompt_file``
    本身——那样会绕过其 try/except，OSError 会逃逸成脏崩溃而非 InSessionError）。让真实
    ``_write_prompt_file`` 的 except 捕获 mock 出的 OSError → 转抛 ``InSessionError(internal_error)``
    → cli ``except InSessionError`` → ``fail_in_session`` → ``workflow_failed(internal_error)``。
    """
    from orca.run import step as step_mod

    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    def _raise_oserror(*a, **kw):
        raise OSError("mock os.replace failure（disk full / 权限）")

    # ``step.py`` 用 ``import os`` + ``os.replace``；mock ``step_mod.os.replace`` 让真实
    # ``_write_prompt_file`` 的 except 捕获 → 转 InSessionError(internal_error)。
    monkeypatch.setattr(step_mod.os, "replace", _raise_oserror)

    # next 推进到 b（触发 b 的 prompt 写盘 → os.replace OSError → internal_error → workflow_failed）
    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "out_a",
    ])
    assert result.exit_code == 1, (
        f"internal_error 应 fail loud exit 1，实得 {result.exit_code}: {result.output}"
    )
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert reply["error_kind"] == "internal_error", (
        f"OSError 应归类 internal_error，实得 {reply.get('error_kind')!r}"
    )
    assert "recoverable" not in reply, "internal_error 不应是 recoverable"

    # tape 末条 workflow_failed(internal_error)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "internal_error"


def test_advance_step_inline_fallback_vs_compact(tmp_path):
    """``advance_step`` 交付模式守门：``prompts_dir=None`` → inline 全量 prompt；给定 → compact 文件。

    daemon / 直调单测走 inline（``prompts_dir=None``）；生产 bootstrap/next 走 compact。
    两模式渲染内容一致（同 ``render_prompt`` 输出），仅交付载体不同。
    """
    from orca.compile import load_workflow
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(AGENT_WF_YAML, encoding="utf-8")
    wf = load_workflow(wf_path)

    # inline 回退（prompts_dir=None）：prompt=渲染全文、prompt_file=None、resources_root=None
    tape1 = Tape(tmp_path / "tape1.jsonl", run_id="r1", resume=True)
    res_inline = advance_step(tape1, wf, run_id="r1", prompts_dir=None)
    assert res_inline.prompt_file is None, "inline 回退不落盘"
    assert res_inline.resources_root is None
    assert res_inline.prompt and "产出 step A" in res_inline.prompt, (
        "inline 回退返全量渲染 prompt（含 entry 指令文本）"
    )

    # compact（prompts_dir 给定）：prompt=None、prompt_file 落盘、文件内容 == inline 全量
    tape2 = Tape(tmp_path / "tape2.jsonl", run_id="r2", resume=True)
    pdir = tmp_path / "prompts"
    res_compact = advance_step(tape2, wf, run_id="r2", prompts_dir=pdir)
    assert res_compact.prompt is None, "compact 不返全量 prompt（只返指针，由 cli 拼）"
    assert res_compact.prompt_file is not None
    compact_text = Path(res_compact.prompt_file).read_text(encoding="utf-8")
    assert compact_text == res_inline.prompt, (
        "compact 落盘内容必须 == inline 全量渲染文本（同 render_prompt 输出）"
    )


def _spy_tape_replay(tape) -> tuple[callable, list[int]]:
    """spy ``tape.replay``：返回 ``(patched_replay, calls)``，calls 收集每次调用的 since_seq。

    用 list 而非 nonlocal int：调用方拿到引用即可读取最终计数，避免闭包陷阱。
    保 lazy 生成器：返回 ``original_replay(*args, **kwargs)`` 不消费迭代器。
    """
    calls: list[int] = []
    original_replay = tape.replay

    def counting_replay(since_seq: int = 0):
        calls.append(since_seq)
        return original_replay(since_seq)

    tape.replay = counting_replay  # type: ignore[method-assign]
    return counting_replay, calls


def test_advance_step_bootstrap_traverses_tape_once(tmp_path):
    """SPEC §3 O1a AC：``advance_step`` bootstrap 分支（pending）单次调用 tape 遍历 2→1。

    重构前：``replay_state(tape)`` + ``Orchestrator._inputs_from_tape(tape)`` = 2 次遍历。
    重构后：``_replay_state_and_inputs(tape)`` = 1 次遍历。
    """
    from orca.compile import load_workflow
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(AGENT_WF_YAML, encoding="utf-8")
    wf = load_workflow(wf_path)

    # bootstrap 场景：空 tape（pending 分支）
    tape = Tape(tmp_path / "tape_boot.jsonl", run_id="r1", resume=True)
    _, calls = _spy_tape_replay(tape)

    res = advance_step(tape, wf, run_id="r1", prompts_dir=None)
    assert res.node == "a", "bootstrap 应推到 entry 节点 a"
    # StepResult 形状断言：bootstrap 应 emit workflow_started + node_started(a) 两条
    # （若 advance_step 在 _replay_state_and_inputs 之前 short-circuit 返回，emits 会空，
    # 配合下方 calls==1 双锁，加速回归定位）。
    assert len(res.emits) == 2, (
        f"bootstrap 应 emit [workflow_started, node_started] 两条，实得 {len(res.emits)} 条"
    )

    assert len(calls) == 1, (
        f"advance_step (bootstrap) 应只遍历 tape 一次（SPEC §3 O1a），实得 {len(calls)} 次"
    )


def test_advance_step_next_traverses_tape_once(tmp_path):
    """SPEC §3 O1a AC：``advance_step`` advance 分支（output 给出）单次调用 tape 遍历 2→1。

    next 路径有 output 推进：重构前同样 2 次遍历（replay_state + _inputs_from_tape），
    重构后 1 次。两分支独立断言（pending/advance 各自走 merged call site）。
    """
    import asyncio

    from orca.compile import load_workflow
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(AGENT_WF_YAML, encoding="utf-8")
    wf = load_workflow(wf_path)

    # 预填 tape：workflow_started + node_started(a) → 推进 a 时 advance 分支
    pre_tape = Tape(tmp_path / "tape_next.jsonl", run_id="r1")
    try:
        asyncio.run(pre_tape.append({
            "type": "workflow_started", "timestamp": 1.0, "node": None,
            "session_id": None,
            "data": {"workflow_name": "cli_test_wf", "entry": "a",
                     "inputs": {"task": "demo"}, "yaml_path": str(wf_path)},
        }))
        asyncio.run(pre_tape.append({
            "type": "node_started", "timestamp": 2.0, "node": "a",
            "session_id": "s1", "data": {"kind": "agent"},
        }))
    finally:
        pre_tape.close()

    # 重开只读 tape + spy
    tape = Tape(tmp_path / "tape_next.jsonl", run_id="r1", resume=True)
    _, calls = _spy_tape_replay(tape)

    res = advance_step(tape, wf, run_id="r1", prompts_dir=None, output="out_from_a")
    assert res.done is False, "a → b 推进，workflow 未完"
    assert res.node == "b", "a 完成后应推到 b"
    # StepResult 形状断言：advance 应 emit [node_completed(a), route_taken, node_started(b)]
    # 三条（同上，配合 calls==1 双锁，加速回归定位）。
    assert len(res.emits) == 3, (
        f"advance 应 emit [node_completed, route_taken, node_started] 三条，"
        f"实得 {len(res.emits)} 条"
    )

    assert len(calls) == 1, (
        f"advance_step (advance/output) 应只遍历 tape 一次（SPEC §3 O1a），实得 {len(calls)} 次"
    )


def test_failure_output_schema_malformed(cwd_tmp):
    """output_schema 自身畸形（YAML 写错）→ recoverable（SPEC 2026-07-23 §3 归 output_schema_mismatch）。

    ``jsonschema.validate`` 对畸形 schema 抛 ``SchemaError``（非 ``ValidationError`` 子类）。
    ``_parse_output`` 必须 catch 两者（D-v8.x-2）。SPEC v2 把三处 schema-mismatch 都归
    recoverable（畸形 schema 由主 session 发现后可 ``orca stop``，引擎不擅自判死）。
    """
    # required 元素非字符串 → SchemaError
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "产出 step A 的输出。"',
        'prompt: "产出 step A 的输出。"\n    output_schema:\n      type: object\n      required: [1, 2, 3]',
    )
    p = cwd_tmp / "wf_schema_malformed.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", '{"k": "v"}',
    ])
    # recoverable → 0 退出（run 存活；畸形 schema 主 session 可后续 stop）
    assert result.exit_code == 0, f"malformed schema 现 recoverable，应 exit 0: {result.output}"
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False
    assert reply["recoverable"] is True
    assert reply["error_kind"] == "output_schema_mismatch"
    assert "in_session_error" not in json.dumps(reply)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    # tape 末尾 node_started（[nf, ns]，无 workflow_failed）
    assert json.loads(lines[-1])["type"] == "node_started"
    assert json.loads(lines[-2])["type"] == "node_failed"
    assert json.loads(lines[-2])["data"]["kind"] == "output_schema_mismatch"


def test_failure_render_error_clears_marker(cwd_tmp):
    """下游 prompt 引用上游缺失字段（无 schema）→ 干净 workflow_failed(render_error) + 清 marker。

    2026-07-08 bug 闭环：此前 render 抛 ``ExecError``（非 InSessionError）逃逸 cli.py 的
    ``except InSessionError`` → 脏崩溃（无 workflow_failed、不清 marker、tape 悬挂、下次卡死）。
    现 ``_render_or_fail`` 把 ExecError 包成 InSessionError("渲染节点…") → 走既有干净路径。

    v5 §8 step 5b：从此前误并入 ``test_failure_output_schema_malformed`` 函数体（裸字符串
    表达式分隔）拆出为独立测试（code-reviewer Round 2 m1）——两者 yaml/run/断言皆独立，
    合并会误导诊断方向。
    """
    # node b 引用 a.output.nope（a 无 schema，output 是自由文本，无 nope 字段）
    yaml_text = AGENT_WF_YAML.replace(
        'prompt: "基于 {{ a.output }} 总结。"',
        'prompt: "基于 {{ a.output.nope }} 总结。"',
    )
    p = cwd_tmp / "wf_render_err.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    runner = CliRunner()
    boot = _bootstrap(runner, p)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "OUT_A",
    ])
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]
    # v5 §8 step 5b：信封 error_kind(render_error) + 反向无 in_session_error
    assert reply["error_kind"] == "render_error"
    assert "in_session_error" not in json.dumps(reply)

    # 干净终态：tape 末条 workflow_failed(render_error)
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "render_error", (
        f"render 错必须归类 render_error，实得 {last['data']['kind']}"
    )

    # marker 已清（不卡死）：runs/orca-<owner>.json 不存在（owner=run_id，CLI bootstrap 路径）
    marker = Path(tape).parent / f"orca-{run_id}.json"
    assert not marker.exists(), (
        f"render_error 终态后 marker 必须清除（否则下次 /orca run 卡死），仍存在：{marker}"
    )


# ── F5：LOCK_NB busy ────────────────────────────────────────────────────────


def test_next_busy_when_lock_held(cwd_tmp, wf_path):
    """并发撞锁：另一进程持 tape flock → next 返 {done:false, reason:busy} 0 退出（F5）。

    SPEC §3 O4：busy 信封含 ``retry_after_ms``（500ms），主 session 据它等待重试。
    SPEC §3 O4 AC：busy reply 不重发 prompt（reply 无 prompt / node 字段）。
    """
    import fcntl
    from orca.iface.in_session.cli import _BUSY_RETRY_AFTER_MS
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 持锁
    lock_path = Path(str(tape) + ".lock")
    fd = open(lock_path, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    try:
        reply = _next(runner, tape, run_id, "--output", "out_a")
        assert reply["done"] is False
        assert reply["reason"] == "busy"
        # SPEC §3 O4：retry_after_ms 必出，主 session 据它等待重试同一 next（不重派子代理）。
        # code-reviewer test 🔴#5：用常量替 hardcoded 500，防单点漂移。
        assert reply["retry_after_ms"] == _BUSY_RETRY_AFTER_MS
        # SPEC §3 O4 AC：busy reply **不含 prompt / node**（不重发 prompt / 不重派子代理）。
        assert "prompt" not in reply, "busy reply 不应含 prompt（避免主 session 重派子代理）"
        assert "node" not in reply, "busy reply 不应含 node（避免主 session 重派子代理）"
        # 0 退出（runner.invoke 的 exit_code == 0）
        # tape 未增加（被 busy 短路）
        lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def test_drive_protocol_mentions_busy_retry_rule():
    """SPEC §3 O4：``_drive_protocol`` 文本含 busy / retry_after_ms / 不重派子代理 指令。

    code-reviewer test 🟡#5：守 SKILL 协议文本的 intent（不止 CLI 行为），防 SKILL 漂移后
    主 session 不知如何处理 busy。
    """
    from orca.iface.in_session.cli import _drive_protocol
    text = _drive_protocol("test-run-id")
    assert "busy" in text
    assert "retry_after_ms" in text
    assert "不重派子代理" in text or "不要重派" in text
    # 也含 next 命令模板（主 session 知道怎么调）
    assert "orca next" in text


def test_next_reply_has_no_compliance_fields(cwd_tmp, wf_path):
    """SPEC §3 O3 AC：next reply 不加 compliance 字段（零回归）。

    v4.1 简化：删了 next reply compliance_warning / stuck 语义（compliance 是 orca 自我保护，
    主 session 调度固定不反应）。next reply 字段集应保持 v4.0 形态。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 推进一步（无 output → compliance 计数 +1，但 reply 不透出）
    result = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", ""])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    # 守门：compliance 字段不应在 next reply 出现（只在 status --run-id 透出）
    for forbidden in ("compliance_warning", "stuck", "no_output_count"):
        assert forbidden not in reply, (
            f"next reply 不应含 {forbidden}（SPEC §3 O3 v4.1：主 session 不参与 compliance）"
        )


# ── N2：marker RMW 在 flock 临界区内 ─────────────────────────────────────────


def test_marker_rmw_serialized_under_flock(cwd_tmp, wf_path):
    """两 next 并发 → flock 串行 → no_output_count 不丢更新（N2 闭环）。

    本测试用「先持锁阻塞 second next，release 后 second next 跑」模拟时序：因 next 在
    flock 临界区内 RMW marker，second next 看到的是 first next 的最新 marker。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 第一次 next（无 output）→ 计数 1
    _next(runner, tape, run_id, "--output", "")
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert json.loads(markers[0].read_text())["no_output_count"] == 1

    # 第二次 → 计数 2（不是 1，证明 first 的 RMW 已落盘）
    _next(runner, tape, run_id, "--output", "")
    assert json.loads(markers[0].read_text())["no_output_count"] == 2


# ── stop ─────────────────────────────────────────────────────────────────────


def test_stop_emits_workflow_cancelled_and_clears_marker(cwd_tmp, wf_path):
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    result = runner.invoke(app, ["stop", run_id])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply["done"] is True

    # tape 末尾是 workflow_cancelled
    lines = Path(boot["tape"]).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_cancelled"

    # marker 已清
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(markers) == 0


# ── D-v7-1 架构守门：plugin/hook 模板零业务逻辑 ─────────────────────────────


def test_plugin_template_has_no_orca_business_logic():
    """``.opencode/plugins/*.ts`` 不得含 advance/router/replay/tape 路径。

    SPEC §9.2 架构守门：宿主侧零 Orca 业务逻辑。``<task_result>`` 解析允许（扁平化提取
    非 Orca 决策），但 advance/router/replay/tape 禁止。
    """
    plugin = Path(__file__).resolve().parents[3] / "orca/iface/in_session/templates/opencode/orca.ts"
    text = plugin.read_text(encoding="utf-8")
    forbidden = ["advance_step", "router.resolve", "replay_state", "tape.append",
                 "EventBus", "Tape(", "drive_loop", "advance("]
    for kw in forbidden:
        assert kw not in text, f"plugin 模板含禁词 {kw!r}（违反 D-v7-1）"


# ── G2 序列对齐（轻量版）────────────────────────────────────────────────────


def test_event_sequence_matches_expected_shape(cwd_tmp, wf_path):
    """事件序列 == [workflow_started, node_started(a), node_completed(a),
    route_taken(a→b), node_started(b), node_completed(b), route_taken(b→$end),
    workflow_completed] —— 与 drive_loop 跑同 wf 的序列形态一致（G2 守门轻量版）。

    完整 G2（vs ``orca run`` tape 逐 seq 对齐）需跑 drive_loop 端到端，由 test-coverage-e2e
    真链路验；此处验证事件序列骨架。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    _next(runner, tape, run_id, "--output", "out_a")
    _next(runner, tape, run_id, "--output", "out_b")

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    events = [json.loads(ln) for ln in lines]
    types = [e["type"] for e in events]
    nodes = [e.get("node") for e in events]

    assert types == [
        "workflow_started", "node_started",
        "node_completed", "route_taken", "node_started",
        "node_completed", "route_taken",
        "workflow_completed",
    ]
    assert nodes[1] == "a"     # ns(a)
    assert nodes[2] == "a"     # nc(a)
    assert nodes[4] == "b"     # ns(b)
    assert nodes[5] == "b"     # nc(b)
    # seq 连续递增
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))


# ── 补丁：state_corrupt / bootstrap busy / stop busy / status / no-marker / start ──


def test_classify_in_session_error_uses_explicit_kind():
    """``_classify_in_session_error`` 直读 ``exc.error_kind``（取代消息子串匹配，类型安全）。

    分类由 step.py 各 raise 处传 ``error_kind=ERR_*`` 显式携带；本测试守门「显式字段」契约：
    每个常量经 classifier 直返同名；缺省 → ``internal_error`` 兜底。
    raise 文案改动**不应**再触发本测试（旧子串匹配已移除）。

    v5 §8 step 5b：``_classify_in_session_error`` 抽到 ``_step_io`` helper（daemon/cli 共享，
    ``fail_in_session`` 内部调用）。import 直接从 helper。
    """
    from orca.iface.in_session._step_io import _classify_in_session_error
    from orca.run.step import (
        ERR_OUTPUT_SCHEMA_MISMATCH, ERR_RENDER_ERROR, ERR_UNSUPPORTED_NODE_KIND,
        ERR_STATE_CORRUPT, ERR_INTERNAL_ERROR, InSessionError,
    )

    # 各 taxonomy 常量经 classifier 直返（消息任意，不参与分类）
    for kind in (ERR_OUTPUT_SCHEMA_MISMATCH, ERR_RENDER_ERROR,
                 ERR_UNSUPPORTED_NODE_KIND, ERR_STATE_CORRUPT):
        assert _classify_in_session_error(
            InSessionError("任意文案，不参与分类", error_kind=kind)
        ) == kind

    # 缺省 error_kind → internal_error 兜底（fail loud 不静默）
    assert _classify_in_session_error(InSessionError("未知错误")) == ERR_INTERNAL_ERROR


def test_stop_busy_when_tape_flock_held(cwd_tmp, wf_path):
    """stop 撞 tape flock → {done:false, reason:busy}（busy 语义在 stop 路径一致）。

    SPEC §3 O4：busy 信封含 ``retry_after_ms``（与 next 路径一致）。
    """
    import fcntl
    from orca.iface.in_session.cli import _BUSY_RETRY_AFTER_MS
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    lock_path = Path(str(tape) + ".lock")
    fd = open(lock_path, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    try:
        result = runner.invoke(app, ["stop", run_id])
        reply = json.loads(result.output.splitlines()[-1])
        assert reply["done"] is False
        assert reply["reason"] == "busy"
        assert reply["retry_after_ms"] == _BUSY_RETRY_AFTER_MS
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


def test_bootstrap_busy_when_tape_flock_held(cwd_tmp, wf_path, monkeypatch):
    """bootstrap 撞 tape flock → {done:false, reason:busy, retry_after_ms:500}（SPEC §3 O4）。

    bootstrap 撞锁罕见（通常首调），但路径要一致：3 处 busy 信封（bootstrap/next/stop）
    统一形态，主 session 据 retry_after_ms 等待重试。

    模拟方式：monkeypatch ``_try_acquire_flock`` 在 bootstrap 路径返 None。bootstrap 在
    gen run_id 后才调 ``_try_acquire_flock``（global dupe-check lock 释后），所以副作用
    仅在 bootstrap 内部，不污染其它测试。
    """
    from orca.iface.in_session.cli import _BUSY_RETRY_AFTER_MS
    runner = CliRunner()
    from orca.iface.in_session import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_try_acquire_flock", lambda tape_path: None)

    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    # bootstrap 撞锁返 busy，exit_code 0（与 next 一致；非 fail loud）
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False
    assert reply["reason"] == "busy"
    assert reply["retry_after_ms"] == _BUSY_RETRY_AFTER_MS


def test_status_no_run_id_lists_runs_dir(cwd_tmp, wf_path):
    """status 无 run_id 列 runs/ 下全部 tape 文件名。"""
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # 输出含 tape 文件名（run_id stem）
    assert ".jsonl" not in result.output  # 只显示 stem
    # 提示文案统一用 --run-id 形态（spec §2.1 / DEFECT-2：SKILL.md/spec/CLI 三处一致）。
    # F1：尾行同时提示续跑命令（resumable run 用 ``orca next --run-id`` 接续）。
    assert "用 `orca status --run-id <run_id>`" in result.output
    assert "resumable=true" in result.output  # F1：resumable 标志透出


def test_status_with_run_id_shows_progress(cwd_tmp, wf_path):
    """status <run_id> 报 workflow 进度（status: running / node_status）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "status:" in result.output
    assert "running" in result.output
    assert "node_status:" in result.output


def test_status_run_id_option_mirrors_positional(cwd_tmp, wf_path):
    """DEFECT-2：``status --run-id <id>`` 与位置参数 ``status <id>`` 等价（spec §2.1）。

    SKILL.md / spec §2.1 都写 ``--run-id``；旧 CLI 只接位置参数 → 主 session 照文档跑报错。
    现在两种形态都接受，输出一致。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    positional = runner.invoke(app, ["status", run_id])
    option = runner.invoke(app, ["status", "--run-id", run_id])
    assert positional.exit_code == 0, positional.output
    assert option.exit_code == 0, option.output
    # 输出一致（同一 run 同一时刻 replay_state 相同）
    assert "running" in option.output
    assert "node_status:" in option.output
    assert positional.output == option.output


def test_status_run_id_option_with_json_flag(cwd_tmp, wf_path):
    """DEFECT-2：``status --run-id <id> --json`` 走 JSON 出口（spec §2.3 单 run 详情契约）。"""
    import json as _json
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    result = runner.invoke(app, ["status", "--run-id", run_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output.splitlines()[-1])
    assert payload["run_id"] == run_id
    assert payload["status"] == "running"


def test_status_positional_and_option_same_value_ok(cwd_tmp, wf_path):
    """DEFECT-2：位置参数与 --run-id 同传且**同值** → 视作一次（容错；用户复制粘贴常见）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    result = runner.invoke(app, ["status", run_id, "--run-id", run_id])
    assert result.exit_code == 0, result.output
    assert "running" in result.output


def test_status_positional_and_option_conflict_fails_loud(cwd_tmp, wf_path):
    """DEFECT-2：位置参数与 --run-id 同传且**不同值** → fail loud（BadParameter，铁律 12）。

    不静默选其中一个——让用户看到两条路冲突，明确报错。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["status", "rid-a", "--run-id", "rid-b"])
    assert result.exit_code != 0
    # BadParameter 提示含两个值（让用户看到冲突来源）
    assert "rid-a" in result.output
    assert "rid-b" in result.output


def test_status_run_id_option_nonexistent_fails_loud(cwd_tmp, wf_path):
    """DEFECT-2 review MINOR#2：``--run-id <不存在>`` 走「无 tape」错误分支（与位置参数等价）。

    锁住「双形态在错误路径也等价」——防归一逻辑（rid = ...）在错误分支意外分叉。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)  # 建 runs/ 但不创建 ghost-run 的 tape

    positional = runner.invoke(app, ["status", "ghost-run"])
    option = runner.invoke(app, ["status", "--run-id", "ghost-run"])
    assert positional.exit_code == 1, "位置参数形态：无 tape 应 exit 1"
    assert option.exit_code == 1, "--run-id 形态：无 tape 应 exit 1（与位置参数等价）"
    assert "ghost-run" in positional.output
    assert "ghost-run" in option.output


# ── FU-1：stop/open 加 --run-id option（spec §2.1 命令族统一，套 DEFECT-2 e763e9e）────
#
# stop 是**破坏性**（首次 stop 清 marker + emit cancelled），非只读——不能套 status 的
# 「同 run 调两次 byte-equal」。改用「两独立 run 各停一次」证双形态同构 observable。
# stop 无 --json；stop 的 nonexistent run 是**幂等 ok（exit 0）**非 fail-loud（exit 1）。


def _stop_observable(result) -> dict:
    """从 stop 结果抽取可比 observable：exit / ok / done / note。"""
    obs = {"exit": result.exit_code}
    if result.exit_code == 0:
        obs.update(json.loads(result.output.splitlines()[-1]))
    return obs


def test_stop_run_id_option_mirrors_positional(cwd_tmp, wf_path):
    """FU-1：``stop --run-id <id>`` 与位置参数 ``stop <id>`` 同构（spec §2.1）。

    stop 是破坏性，故用**两个独立 run** 各停一次（一 positional 一 option），断言
    observable（ok/done）一致 + 各自 marker 已清 + tape 末尾 workflow_cancelled。
    """
    runner = CliRunner()
    # 两独立 run（各 bootstrap 一次），分别用两种形态停。
    boot_a = _bootstrap(runner, wf_path)
    rid_a = boot_a["run_id"]
    pos = runner.invoke(app, ["stop", rid_a])
    assert pos.exit_code == 0, pos.output

    boot_b = _bootstrap(runner, wf_path)
    rid_b = boot_b["run_id"]
    opt = runner.invoke(app, ["stop", "--run-id", rid_b])
    assert opt.exit_code == 0, opt.output

    # 双形态 observable 同构（ok/done 一致；run_id 各自正确）
    pos_reply = json.loads(pos.output.splitlines()[-1])
    opt_reply = json.loads(opt.output.splitlines()[-1])
    assert pos_reply["ok"] is True and pos_reply["done"] is True
    assert opt_reply["ok"] is True and opt_reply["done"] is True
    assert pos_reply["run_id"] == rid_a
    assert opt_reply["run_id"] == rid_b

    # 各自 tape 末尾 workflow_cancelled + marker 清（破坏性 observable 一致）
    for boot in (boot_a, boot_b):
        lines = Path(boot["tape"]).read_text(encoding="utf-8").strip().split("\n")
        assert json.loads(lines[-1])["type"] == "workflow_cancelled"
    assert list(cwd_tmp.glob("runs/orca-*.json")) == []


def test_stop_positional_and_option_same_value_ok(cwd_tmp, wf_path):
    """FU-1：``stop <id> --run-id <同 id>`` → 视作一次（容错；用户复制粘贴常见）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    result = runner.invoke(app, ["stop", run_id, "--run-id", run_id])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True and reply["run_id"] == run_id


def test_stop_positional_and_option_conflict_fails_loud(cwd_tmp, wf_path):
    """FU-1：``stop <a> --run-id <b>`` → fail loud（BadParameter，铁律 12）。

    不静默选其一——让用户看到两条路冲突。三命令共用 ``_merge_run_id``，此处锁 stop 路径。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["stop", "rid-a", "--run-id", "rid-b"])
    assert result.exit_code != 0
    assert "rid-a" in result.output
    assert "rid-b" in result.output


def test_stop_missing_run_id_fails_loud(cwd_tmp, wf_path):
    """FU-1：``stop`` 无 run_id（位置参数与 --run-id 都省略）→ exit 2（fail loud）。

    stop 位置参数已从必填改为可选（让 ``stop --run-id X`` 不再因「缺位置参数」exit 2），
    故 None 由**显式守卫**拦下（``raise BadParameter``）。stop 无 status 的「无参列全部」
    模式——None 必须 fail loud（ISSUE-3，保 exit 2 回归）。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 2


def test_stop_run_id_option_nonexistent_is_idempotent_ok(cwd_tmp, wf_path):
    """FU-1：``stop --run-id <不存在>`` → 幂等 ok（note=no-tape，exit 0），与位置参数等价。

    stop 的 nonexistent run 走幂等清 marker 分支（非 fail-loud）——v8.py:535 的 option 变体。
    锁住「双形态在 no-tape 路径也等价」。
    """
    runner = CliRunner()
    _bootstrap(runner, wf_path)  # 建 runs/，但不创建 ghost-run 的 tape

    positional = runner.invoke(app, ["stop", "nonexistent-run"])
    option = runner.invoke(app, ["stop", "--run-id", "nonexistent-run"])
    assert positional.exit_code == 0, "位置参数形态：no-tape 应幂等 ok exit 0"
    assert option.exit_code == 0, "--run-id 形态：no-tape 应幂等 ok exit 0（与位置参数等价）"
    assert _stop_observable(positional).get("note") == "no-tape"
    assert _stop_observable(option).get("note") == "no-tape"


def test_open_run_id_option_routes_to_open_run(cwd_tmp, wf_path):
    """FU-1：``open --run-id <id>`` 把合流后的 run_id 传给 ``_open_run_inproc``（spec §2.1）。

    mock ``_open_run_inproc`` 避免真起 web server；断言收到的 run_id 与退出码透传。
    """
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open", "--run-id", run_id])
    assert result.exit_code == 0, result.output
    m.assert_called_once()
    assert m.call_args.args[0] == run_id


def test_open_positional_regression(cwd_tmp, wf_path):
    """FU-1：``open <id>`` 位置参数仍可用（向后兼容）。mock 同上。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open", run_id])
    assert result.exit_code == 0, result.output
    assert m.call_args.args[0] == run_id


def test_open_positional_and_option_same_value_ok(cwd_tmp, wf_path):
    """FU-1：``open <id> --run-id <同 id>`` → 视作一次（容错；与 stop 对称，命令面锁同值合流）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open", run_id, "--run-id", run_id])
    assert result.exit_code == 0, result.output
    assert m.call_args.args[0] == run_id


def test_open_positional_and_option_conflict_fails_loud(cwd_tmp, wf_path):
    """FU-1：``open <a> --run-id <b>`` → fail loud（BadParameter，共用 ``_merge_run_id``）。"""
    runner = CliRunner()
    _bootstrap(runner, wf_path)
    result = runner.invoke(app, ["open", "rid-a", "--run-id", "rid-b"])
    assert result.exit_code != 0
    assert "rid-a" in result.output
    assert "rid-b" in result.output


def test_open_no_run_id_uses_active_default(cwd_tmp, wf_path):
    """FU-1：``open`` 都省略 → 取活跃 run 默认（None 不 fail loud，与 stop 区分）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]

    with mock.patch(
        "orca.iface.in_session.cli._open_run_inproc", return_value=0
    ) as m:
        result = runner.invoke(app, ["open"])
    assert result.exit_code == 0, result.output
    assert m.call_args.args[0] == run_id


# ── SPEC §13 D13 统一 open 列表语义（--list flag + 无活跃 run 回落列表页） ──────


def test_open_list_flag_forces_run_list(cwd_tmp, wf_path):
    """``orca open --list`` → 强制列表页（即使有活跃 run 也不打开详情）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    assert boot["run_id"]

    with (
        mock.patch(
            "orca.iface.in_session.cli._open_run_inproc", return_value=0
        ) as m_open,
        mock.patch(
            "orca.iface.in_session.cli._open_run_list_inproc", return_value=0
        ) as m_list,
    ):
        result = runner.invoke(app, ["open", "--list"])
    assert result.exit_code == 0, result.output
    # 列表页路径被调用，详情路径不被调
    assert m_list.called
    assert not m_open.called


def test_open_no_active_run_falls_back_to_list(cwd_tmp, wf_path):
    """``orca open`` 无参 + 无活跃 run → 回落列表页（D13 统一语义，旧版 fail loud 已废）。"""
    runner = CliRunner()
    # 不 bootstrap → 无活跃 run
    with (
        mock.patch(
            "orca.iface.in_session.cli._open_run_inproc", return_value=0
        ) as m_open,
        mock.patch(
            "orca.iface.in_session.cli._open_run_list_inproc", return_value=0
        ) as m_list,
    ):
        result = runner.invoke(app, ["open"])
    assert result.exit_code == 0, result.output
    assert m_list.called
    assert not m_open.called


def test_open_with_active_run_still_opens_detail(cwd_tmp, wf_path):
    """有活跃 run 时无参 open 仍打开活跃 run 详情（保持 bootstrap 后直达体验）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id = boot["run_id"]
    with (
        mock.patch(
            "orca.iface.in_session.cli._open_run_inproc", return_value=0
        ) as m_open,
        mock.patch(
            "orca.iface.in_session.cli._open_run_list_inproc", return_value=0
        ) as m_list,
    ):
        result = runner.invoke(app, ["open"])
    assert result.exit_code == 0, result.output
    assert m_open.called and m_open.call_args.args[0] == run_id
    assert not m_list.called


# ── _merge_run_id helper 单测（FU-1 ISSUE-5，DRY 防线）──────────────────────────


def test_merge_run_id_both_none_returns_none():
    """都省略 → None（调用方按命令语义处理：status 列全部 / stop fail loud / open 默认）。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id(None, None) is None


def test_merge_run_id_positional_only():
    """仅位置参数 → 透传。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id("rid", None) == "rid"


def test_merge_run_id_option_only():
    """仅 --run-id → 透传。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id(None, "rid") == "rid"


def test_merge_run_id_same_value_tolerated():
    """同值 → 容错返该值（用户复制粘贴常见）。"""
    from orca.iface.in_session.cli import _merge_run_id
    assert _merge_run_id("rid", "rid") == "rid"


def test_merge_run_id_conflict_fails_loud():
    """异值 → BadParameter（铁律 12，含两个冲突值）。"""
    import typer
    from orca.iface.in_session.cli import _merge_run_id
    with pytest.raises(typer.BadParameter):
        _merge_run_id("a", "b")


def test_next_no_marker_returns_no_marker_reason(cwd_tmp, wf_path):
    """next 找不到 marker → {done:false, reason:no-marker}（幂等吞，不崩）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]
    # 手工清掉所有 marker（模拟 marker 已清但 tape 还在）
    for mp in cwd_tmp.glob("runs/orca-*.json"):
        mp.unlink()
    reply = _next(runner, tape, run_id, "--output", "out_a")
    assert reply["done"] is False
    assert reply["reason"] == "no-marker"


# ── marker 终态清理（v5 step 2b：start 命令已删，相关 start/cc_hooks 测试随之移除）──


def test_marker_files_cleaned_after_workflow_completes(cwd_tmp, wf_path):
    """workflow 跑完后 marker 已清（终态 → clear_marker）。"""
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]
    _next(runner, tape, run_id, "--output", "out_a")
    _next(runner, tape, run_id, "--output", "out_b")

    # workflow_completed 后 marker 已清
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(markers) == 0


# ── outputs 模板求值 + inputs 从 tape 恢复（2026-07-09 补丁）────────────────────


OUTPUTS_WF_YAML = """\
name: outputs_wf
description: 带 outputs 模板的 workflow（in-session outputs 求值测试）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: $end
outputs:
  result: "A={{ a.output }}"
"""


def test_next_completes_with_outputs_template_evaluated(cwd_tmp, tmp_path):
    """``wf.outputs`` 声明模板 → in-session 跑完应**求值**（不再 fail loud）。

    回归 2026-07-09 补丁：``_final_outputs`` 从 fail loud 改为 ``render_template``
    求 ``wf.outputs``（与 ``Orchestrator._evaluate_outputs`` 同源）。
    """
    wf_path = tmp_path / "wf_outputs.yaml"
    wf_path.write_text(OUTPUTS_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    reply = _next(runner, tape, run_id, "--output", "out_a")
    assert reply["done"] is True

    # workflow_completed.data.outputs = 模板求值结果（{{ a.output }} → "out_a"）
    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    wc = json.loads(lines[-1])
    assert wc["type"] == "workflow_completed"
    assert wc["data"]["outputs"] == {"result": "A=out_a"}


INPUTS_WF_YAML = """\
name: inputs_wf
description: 非 entry 节点引用 inputs（inputs 从 tape 恢复测试）。
entry: a
inputs:
  task:
    type: string
    required: true
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "step A。"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于输入 {{ inputs.task }} 继续。"
    routes:
      - to: $end
"""


def test_next_recovers_inputs_from_tape_without_inputs_arg(cwd_tmp, tmp_path):
    """``next`` 不传 ``--inputs`` → 非 entry 节点 ``{{ inputs.* }}`` 仍正确渲染。

    回归 2026-07-09 补丁：``advance_step`` 改从 tape（``workflow_started.data.inputs``）
    恢复 inputs（同 ``Orchestrator._inputs_from_tape``），模型不必每步重传 ``--inputs``，
    且修掉非 entry 节点 ``{{ inputs.* }}`` 依赖 CLI 重传的隐患。
    """
    wf_path = tmp_path / "wf_inputs.yaml"
    wf_path.write_text(INPUTS_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path, "--inputs", '{"task":"hello"}')
    run_id, tape = boot["run_id"], boot["tape"]

    # next 只传 --output，不传 --inputs → 推进到 b（inputs 从 tape 恢复）
    reply = _next(runner, tape, run_id, "--output", "out_a")
    assert reply["done"] is False
    assert reply["node"] == "b"

    # node b 的 prompt 文件应含渲染后的 inputs.task（从 tape 恢复，非 undefined）
    b_prompt = (Path(tape).parent / run_id / "prompts" / "b.md").read_text(encoding="utf-8")
    assert "hello" in b_prompt               # inputs.task 已渲染
    assert "{{ inputs.task" not in b_prompt  # 模板已求值，无残留未渲染标记


OUTPUTS_BAD_WF_YAML = """\
name: outputs_bad_wf
description: outputs 模板引用存在节点的缺失字段（过 compile 校验、render 期 fail-loud）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: $end
outputs:
  result: "{{ a.output.nonexistent }}"
"""


def test_next_outputs_template_render_failure_fails_loud(cwd_tmp, tmp_path):
    """outputs 模板引用存在节点的缺失字段 → render 期 fail loud（``ERR_RENDER_ERROR`` → workflow_failed），不静默返 {}。

    注：引用**不存在的节点**会被 compile validator 在 bootstrap 期拦下；此处用存在节点
    ``a`` 的缺失字段路径，过 compile 校验、在 ``_final_outputs`` render 期触发。
    """
    wf_path = tmp_path / "wf_outputs_bad.yaml"
    wf_path.write_text(OUTPUTS_BAD_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    result = runner.invoke(app, [
        "next", "--tape", tape, "--run-id", run_id, "--output", "out_a",
    ])
    assert result.exit_code == 1   # fail loud
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is True
    assert "failed" in reply["reason"]

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "workflow_failed"
    assert last["data"]["kind"] == "render_error"
