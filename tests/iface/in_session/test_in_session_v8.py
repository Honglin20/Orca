"""tests/iface/in_session/test_in_session_v8.py —— v8 增量守门测试。

覆盖 SPEC v8 §2.6 / §2.6.1 / §2.6.2 / §2.7 / §9.2：
  - **Marker regex**（§2.6.1）：run/status/stop/doctor 命中；无 args / 含空格 wf 路径 / 含 `>` 拒绝；
    无 marker 透传。
  - **改写语义**（§2.6.2）：run→.prompt / doctor→.report / status→友好 / stop→ok+run_id；mock
    CLI stdout JSON。
  - **一次性消费**（§2.6.1）：替换文本无 `<!--orca:cmd` 字面。
  - **sessionID argv**：从 info.sessionID 取作 --owner + --session-id argv。
  - **doctor CLI**（§2.7）：3 项 checks、JSON 结构、report 无完整 marker 字面、ok=and(pass)。
  - **架构守门**：plugin 模板无 `@opencode/core/client` / `command.execute.before` /
    `Bun.spawn(` + `stdout:"string"` / `advance_step` / `router.resolve` / `replay_state` /
    `tape.append` / `EventBus` / `Tape(` / `drive_loop` / `advance(`。
  - **start 写入 opencode 模板**：start 命令把 orca.ts + orca.md 写入 .opencode/。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app
from orca.iface.in_session.templates import MARKER_LITERAL, MARKER_REGEX


# ── fixtures ────────────────────────────────────────────────────────────────


PLUGIN_TS = (
    Path(__file__).resolve().parents[3]
    / "orca/iface/in_session/templates/opencode/orca.ts"
)
ORCA_MD = (
    Path(__file__).resolve().parents[3]
    / "orca/iface/in_session/templates/opencode/command/orca.md"
)


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── §2.6.1 Marker regex ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_sub,expected_args",
    [
        ("<!--orca:cmd run wf.yaml-->", "run", "wf.yaml"),
        ("<!--orca:cmd status-->", "status", ""),
        ("<!--orca:cmd stop-->", "stop", ""),
        ("<!--orca:cmd doctor-->", "doctor", ""),
        ("<!--orca:cmd run /abs/path/wf.yaml-->", "run", "/abs/path/wf.yaml"),
        ("<!--orca:cmd   run   wf.yaml  -->", "run", "wf.yaml"),  # 多空格 + 尾空格
        ("<!--orca:cmd run-->", "run", ""),                       # 无 args
    ],
)
def test_marker_regex_matches(text, expected_sub, expected_args):
    """SPEC §2.6.1：regex 行首/行尾锚定 + sub \\w+ + args 非贪婪 [^>]*?。"""
    m = re.match(MARKER_REGEX, text)
    assert m is not None, f"未命中 marker: {text!r}"
    assert m.group(1) == expected_sub
    assert (m.group(2) or "").strip() == expected_args


@pytest.mark.parametrize("text", [
    "not a marker",                                  # 无 marker
    "  <!--orca:cmd run wf.yaml-->",                 # 行首空格（非行首锚定）
    "<!--orca:cmd run wf.yaml--> trailing",          # 行尾非锚定
    "<!--orca:cmd run wf>yaml-->",                   # args 含 >（明令禁止）
    "<!--orca:cmd run wf\nyaml-->",                  # 换行（args 不得跨行）
    "some text <!--orca:cmd run--> more text",       # 非整条文本
    "<!--orca: cmd run-->",                          # orca: 后空格变 orca:cmd 不匹配
])
def test_marker_regex_rejects(text):
    """SPEC §2.6.1：非 marker 文本不命中（行首/行尾锚定 + args 禁 `>` / 禁换行）。"""
    assert re.match(MARKER_REGEX, text) is None, f"误命中：{text!r}"


@pytest.mark.parametrize("text", [
    "<!-- orca:cmd run -->",          # <!-- 后空格容许（regex \s*）
    "<!--orca:cmd   run   wf.yaml-->",  # 多空格分隔
])
def test_marker_regex_tolerates_whitespace(text):
    """SPEC §2.6.1：``<!--`` 后空格与多空格分隔容许（``\\s*`` / ``\\s+``）。"""
    assert re.match(MARKER_REGEX, text) is not None


def test_marker_regex_sub_must_be_word():
    """子命令名 \\w+：含特殊字符不命中。"""
    assert re.match(MARKER_REGEX, "<!--orca:cmd run-x-->") is None   # `-` 非 \w
    assert re.match(MARKER_REGEX, "<!--orca:cmd 123run-->") is not None  # 数字 OK
    assert re.match(MARKER_REGEX, "<!--orca:cmd RUN-->") is not None     # 大写 OK


# ── §2.6.1 一次性消费：替换文本无 marker 字面 ──────────────────────────────


def test_doctor_report_has_no_marker_literal():
    """doctor 输出 .report 不得含 `<!--orca:cmd` 字面（一次性消费保证，§2.6.1）。

    doctor 在跑 = 自证 transform 链路活；下一轮 transform 不应误命中本报告。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert MARKER_LITERAL not in reply["report"]
    # 用反引号描述 marker（SPEC §2.7）—— 至少出现一处 backtick + orca:cmd
    assert "`orca:cmd" in reply["report"]


# ── §2.7 doctor CLI 自检 ─────────────────────────────────────────────────────


def test_doctor_json_structure_and_checks():
    """doctor 输出 JSON {ok, report, checks:[{name,pass,detail}]}，3 项 checks。"""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])

    assert set(reply.keys()) >= {"ok", "report", "checks"}
    assert isinstance(reply["ok"], bool)
    assert isinstance(reply["report"], str) and len(reply["report"]) > 0
    assert isinstance(reply["checks"], list)
    assert len(reply["checks"]) == 3

    names = [c["name"] for c in reply["checks"]]
    assert "plugin_load_and_transform_trigger" in names
    assert "marker_dispatch" in names
    assert "cli_imports_ok" in names

    for c in reply["checks"]:
        assert set(c.keys()) == {"name", "pass", "detail"}
        assert isinstance(c["pass"], bool)
        assert isinstance(c["detail"], str) and len(c["detail"]) > 0

    # 默认环境三项全 pass → ok=True
    assert reply["ok"] is True
    assert all(c["pass"] for c in reply["checks"])


def test_doctor_report_annotates_idle_blind_spot():
    """SPEC §2.7：report 标注「session.idle 真触发不在自检范围」。"""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    reply = json.loads(result.output.splitlines()[-1])
    assert "session.idle" in reply["report"]
    assert "/orca run" in reply["report"]


def test_doctor_marker_dispatch_check_uses_canonical_regex():
    """doctor 的 marker_dispatch check 用 ``MARKER_REGEX`` 验证 `doctor` marker 命中。"""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    reply = json.loads(result.output.splitlines()[-1])
    md = next(c for c in reply["checks"] if c["name"] == "marker_dispatch")
    assert md["pass"] is True
    assert "doctor" in md["detail"]


# ── §2.6.2 改写语义（plugin TS 字段提取契约）────────────────────────────────


def _extract_ts_function_body(name: str) -> str:
    """从 plugin TS 抽某函数体（用 brace counting，避免 reformat 断）。

    NIT-4 闭环：从 ``function name(...) {`` 起按 ``{``/``}`` 平衡扫描到匹配闭 ``}``。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    m = re.search(rf"function {name}\b\s*\(", text)
    assert m, f"未找到函数 {name}"
    # 从匹配处往后找第一个 `{`
    start = text.find("{", m.end())
    assert start >= 0, f"{name} 缺函数体起始 ``{{``"
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    raise AssertionError(f"{name} 函数体未闭合")


def test_rewrite_text_field_extraction_contract():
    """SPEC §2.6.2：plugin ``rewriteText`` 按子命令提取顶层字段（非整 JSON）。

    通过逐子命令断言 plugin 源码包含对应字段名 + 子命令分支，守住提取契约。
    """
    body = _extract_ts_function_body("rewriteText")
    # run → .prompt
    assert "run" in body and "reply.prompt" in body
    # doctor → .report
    assert "doctor" in body and "reply.report" in body
    # status → 友好串（status 字段）
    assert "status" in body and "reply.status" in body
    # stop → ok + run_id
    assert "stop" in body and "reply.ok" in body and "reply.run_id" in body


def test_plugin_spawns_cli_per_subcommand():
    """SPEC §2.6.2：plugin 按 sub 派发到对应 CLI 子命令（buildCliArgs）。"""
    body = _extract_ts_function_body("buildCliArgs")
    assert '"bootstrap"' in body   # run → bootstrap
    assert '"status"' in body
    assert '"stop"' in body
    assert '"doctor"' in body


def test_plugin_passes_session_id_as_owner_and_session_id_argv():
    """SPEC §2.6.2 B4：bootstrap argv 含 --owner <sid> + --session-id <sid>。"""
    body = _extract_ts_function_body("buildCliArgs")
    assert '"--owner"' in body
    assert '"--session-id"' in body


# ── 架构守门（§2.6 / §9.2）──────────────────────────────────────────────────


def _strip_ts_comments(text: str) -> str:
    """去掉 // 行注释与 /* */ 块注释（粗略，仅测试用）。"""
    # 去块注释
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    # 去 // 行注释（不跨行）
    lines = []
    for ln in text.splitlines():
        # 简单切：找行内第一个未在字符串里的 //；测试场景足够用
        idx = ln.find("//")
        if idx >= 0:
            ln = ln[:idx]
        lines.append(ln)
    return "\n".join(lines)


def test_plugin_uses_ctx_client_not_npm_import():
    """v8 spike 实证：``@opencode/core/client`` npm 不存在，client 必须从 ``ctx.client`` 取。

    守门对象是 **import 语句**（不是注释里的提及）：禁 ``from "@opencode/core/client"``
    与 ``require("@opencode/core/client")``。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert 'from "@opencode/core/client"' not in code
    assert "from '@opencode/core/client'" not in code
    assert 'require("@opencode/core/client")' not in code
    assert "require('@opencode/core/client')" not in code
    assert "ctx.client" in code, "plugin 应使用 ctx.client"


def test_plugin_uses_spawn_sync_pipe_not_string():
    """v8 spike 实证：``Bun.spawn({stdout:"string"})`` 非法，必须 ``Bun.spawnSync({stdout:"pipe"})``。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert 'Bun.spawnSync(' in code, "plugin 必须用 Bun.spawnSync"
    assert 'stdout: "pipe"' in code, 'plugin 必须 stdout:"pipe"'
    assert 'stdout: "string"' not in code, (
        'plugin 不得用 stdout:"string"（opencode 内嵌 Bun runtime 非法）'
    )


def test_plugin_exports_flat_hooks_not_nested():
    """SPEC §13 v8：``export const OrcaPlugin = async (ctx) => ({ ...flat hooks })``。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert "export const OrcaPlugin = async" in code, (
        "plugin 必须 `export const OrcaPlugin = async (ctx) => ({...})`（flat hooks）"
    )
    # 不允许 nested hooks: { hooks: { event: ... } }（v7 缺陷）
    assert not re.search(r"\{\s*hooks:\s*\{", code), (
        "plugin 不得 nested hooks（spike 实证 flat hooks 才生效）"
    )


def test_plugin_uses_messages_transform_entry():
    """SPEC §2.6 D-v8-1：入口钩子是 ``experimental.chat.messages.transform``。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert '"experimental.chat.messages.transform"' in code


def test_plugin_does_not_use_command_execute_before():
    """SPEC §2.6 D-v8-1：``command.execute.before`` 在 opencode 1.14.22 runtime 不触发，禁用。

    守门对象是 **hook 注册键**（不是注释里的提及）：禁 ``"command.execute.before":``
    作为 hook key 注册。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert '"command.execute.before"' not in code, (
        "plugin 不得注册 command.execute.before（opencode 1.14.22 runtime 不触发）"
    )
    assert "'command.execute.before'" not in code


def test_plugin_has_no_orca_business_logic():
    """SPEC §2.6 / §9.2 架构守门：plugin 侧零 Orca 业务逻辑。

    禁词：advance_step / router.resolve / replay_state / tape.append / EventBus /
    drive_loop / advance（带 ``(`` 的调用形式）。
    注：``<task_result>`` 提取 + ``task_id:`` 剥离**允许**（SPEC §2.5 划为宿主侧
    payload 扁平化，非 Orca 业务逻辑）。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    forbidden = [
        "advance_step", "router.resolve", "replay_state", "tape.append",
        "EventBus", "Tape(", "drive_loop", "advance(",
    ]
    for kw in forbidden:
        assert kw not in text, f"plugin 模板含禁词 {kw!r}（违反 D-v7-1）"


def test_plugin_no_chat_message_hook():
    """SPEC §2.6 spike：``chat.message`` 的 ``ignored`` flag 在 1.14.22 不生效，禁用。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    assert '"chat.message"' not in text
    assert "'chat.message'" not in text


def test_plugin_no_count_state_for_doctor():
    """SPEC §2.7 M6：plugin 侧不维护 hook 触发 count++ 状态（与「plugin 零 Orca 状态」守门冲突）。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # 不应有 count[self-type]++ 模式
    assert not re.search(r"count\[\s*[\"']\w+[\"']\s*\]\s*\+\+", text), (
        "plugin 不得维护 hook 触发计数（doctor 不依赖 plugin 自报状态）"
    )


def test_plugin_one_shot_consume_in_rewrite_path():
    """SPEC §2.6.1：plugin rewrite 后若文本意外含 marker 字面，用反引号替换兜底。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    assert "MARKER_LITERAL" in text
    assert '`orca:cmd`' in text or "`orca:cmd`" in text  # split-join 兜底分支


def test_plugin_embeds_canonical_marker_regex():
    """SPEC §2.6.1：plugin TS 必须 embed 与 Python ``MARKER_REGEX`` 同字面 regex。

    单一真相源：改 regex 必须两处同步（本测试守同步契约）。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # TS regex 字面：/^...$/ 形式
    m = re.search(r"const MARKER_REGEX = /(.+?)/", text)
    assert m, "plugin 未定义 MARKER_REGEX 常量"
    ts_regex = m.group(1)
    # Python MARKER_REGEX 去掉前缀 ^ 和后缀 $ 后的中间部分应等同 TS regex
    py_body = MARKER_REGEX
    assert py_body.lstrip("^").rstrip("$") == ts_regex.lstrip("^").rstrip("$"), (
        f"plugin TS regex `{ts_regex}` 与 Python MARKER_REGEX `{py_body}` 不同步"
    )


# ── start 命令 v8：写 opencode 模板文件 ─────────────────────────────────────


AGENT_WF_YAML = """\
name: start_test_wf
description: 2-agent 线性 workflow（start v8 测试）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 A。"
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


def test_start_does_not_write_opencode_templates(cwd_tmp, wf_path):
    """start 收窄为 CC-only run bootstrap：不再写 ``.opencode/`` 模板（落地已移到 ``orca install``）。

    opencode 模板落地（plugin / command / opencode.json 声明）的覆盖在
    ``tests/iface/cli/test_install_cmds.py``。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["start", str(wf_path)])
    assert result.exit_code == 0, result.output
    # .opencode/ 不应被创建（start 只写 CC marker + 打印 settings 片段）
    assert not (cwd_tmp / ".opencode").exists(), (
        "start 不应再写 .opencode/（模板落地已移到 orca install）"
    )


def test_start_command_md_substitutes_to_valid_marker():
    """SPEC §2.6.1：orca.md body 含 ``$ARGUMENTS`` 占位符；用合法 args 替换后应成 valid marker。

    opencode 在用户敲 ``/orca run wf.yaml`` 时把 ``$ARGUMENTS`` 替换为 ``run wf.yaml``。
    orca.md body 本身**不是**合法 marker（含 ``$`` 占位符），但 substitution 后必须合法。
    """
    md = ORCA_MD.read_text(encoding="utf-8")
    body_lines = [ln for ln in md.splitlines() if "orca:cmd" in ln]
    assert body_lines, "orca.md body 无 marker 模板"
    body = body_lines[0].strip()
    assert "$ARGUMENTS" in body, f"orca.md body 不含 $ARGUMENTS 占位符: {body!r}"

    # 模拟 opencode substitution
    substituted = body.replace("$ARGUMENTS", "run wf.yaml")
    m = re.match(MARKER_REGEX, substituted)
    assert m is not None, f"substituted body 非 marker: {substituted!r}"
    assert m.group(1) == "run"
    assert (m.group(2) or "").strip() == "wf.yaml"

    # 反例：args 含 `>` 时 substitution 后应非 marker
    bad = body.replace("$ARGUMENTS", "run wf>yaml")
    assert re.match(MARKER_REGEX, bad) is None


def test_start_points_to_install_for_opencode(cwd_tmp, wf_path):
    """start 新提示：opencode 用户指向 ``orca install`` + ``/orca run``。

    start 不再自落 opencode 模板 / 不再打 ``$ARGUMENTS`` 限制（那是 install 的事）。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["start", str(wf_path)])
    output = result.output
    assert "orca install" in output
    assert "/orca run" in output


def test_start_preserves_cc_marker_and_settings_fragment(cwd_tmp, wf_path):
    """v7 行为保留：start 仍写 CC marker + 打印 settings.json 片段（CC 路无回归）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["start", str(wf_path)])
    assert result.exit_code == 0
    output = result.output

    # marker 写入
    markers = list(cwd_tmp.glob("runs/orca-*.json"))
    assert len(markers) == 1

    # settings.json 片段
    assert "把以下片段贴进 .claude/settings.json" in output
    assert '"hooks"' in output
    assert "Stop" in output
    assert "PostToolUse" in output


# ── v7 baseline 兼容性（CI 守门：v8 不破坏 v7 行为）─────────────────────────


def test_v7_baseline_cli_commands_still_work_v8(cwd_tmp, wf_path):
    """v7 CLI 大脑未动：bootstrap/next/stop/status/start/serve 全可用。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path)])
    assert boot.exit_code == 0
    reply = json.loads(boot.output.splitlines()[-1])
    assert reply["done"] is False

    tape = reply["tape"]
    run_id = reply["run_id"]
    nxt = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", "out_a"])
    assert nxt.exit_code == 0

    stop = runner.invoke(app, ["stop", run_id])
    assert stop.exit_code == 0


# ── §2.6.2 status JSON 契约（MAJOR-1 闭环）──────────────────────────────────


def test_status_json_flag_outputs_json(cwd_tmp, wf_path):
    """SPEC §2.6.2：``--json`` flag → stdout 是合法 JSON（plugin 改写契约）。

    MAJOR-1 闭环：plugin 期望 status stdout 是 JSON 顶层字段（status/node_status/progress）。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path)])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    result = runner.invoke(app, ["status", run_id, "--json"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])

    assert reply["run_id"] == run_id
    assert reply["status"] == "running"
    assert isinstance(reply["node_status"], dict)
    assert "progress" in reply
    # 顶层字段对齐 plugin rewriteText 提取（status/node_status/progress）


def test_status_default_human_readable_unchanged(cwd_tmp, wf_path):
    """v7 行为：``status <run_id>`` 默认人类可读多行（无 --json 不变）。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path)])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "status:" in result.output
    assert "running" in result.output
    assert "node_status:" in result.output


def test_plugin_status_dispatch_passes_json_flag():
    """SPEC §2.6.2：plugin buildCliArgs 的 status 分支必带 --json（MAJOR-1）。"""
    body = _extract_ts_function_body("buildCliArgs")
    # 在 buildCliArgs 函数体里 status 分支必须有 --json
    m = re.search(r'if \(sub === "status"\)(?P<body>(?:.|\n)*?)(?=\n  \}|\n    if \(sub)',
                  body)
    assert m, "buildCliArgs 无 status 分支"
    assert '"--json"' in m.group("body"), "status 派发分支必带 --json flag"


# ── §2.6.2 stop --owner 派发（MAJOR-2 闭环）─────────────────────────────────


def test_stop_with_owner_lookup_resolves_run_id(cwd_tmp, wf_path):
    """SPEC §2.6.2：``stop --owner <sid>`` → 按 marker 查 run_id → stop（MAJOR-2 闭环）。

    plugin transform 入口（``/orca stop`` 无 args）→ plugin 用 sid 查 marker → CLI
    ``stop --owner <sid>`` → 本测试验此路径可停 run。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--owner", "sess-xyz",
                                "--session-id", "sess-xyz"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    # 用 --owner 而非 run_id 调 stop
    result = runner.invoke(app, ["stop", "--owner", "sess-xyz"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply["run_id"] == run_id


def test_stop_no_args_no_owner_returns_error_envelope(cwd_tmp, wf_path):
    """无 run_id + 无 --owner → JSON 错误信封 + 非 0 退出（fail loud，不静默崩）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is False
    assert "reason" in reply


def test_stop_with_unknown_owner_returns_no_active_marker_envelope(cwd_tmp, wf_path):
    """``--owner`` 找不到 marker → JSON ok=false 信封（不破坏 busy state，fail loud 透传）。"""
    runner = CliRunner()
    runner.invoke(app, ["bootstrap", str(wf_path), "--owner", "real-owner"])
    result = runner.invoke(app, ["stop", "--owner", "nonexistent-sid"])
    assert result.exit_code == 0   # 不 raise，返 ok=false 信封
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is False
    assert reply["reason"] == "no-active-marker-for-owner"


def test_plugin_stop_dispatch_uses_owner_flag():
    """SPEC §2.6.2：plugin buildCliArgs 的 stop 分支必带 --owner（MAJOR-2）。"""
    body = _extract_ts_function_body("buildCliArgs")
    m = re.search(r'if \(sub === "stop"\)(?P<body>(?:.|\n)*?)(?=\n  \}|\n    if \(sub)',
                  body)
    assert m, "buildCliArgs 无 stop 分支"
    assert '"--owner"' in m.group("body"), "stop 派发分支必带 --owner flag"


# ── §2.6 plugin spawnCli fail loud（MAJOR-3 闭环）───────────────────────────


def test_plugin_spawncli_checks_exit_code_and_surfaces_stderr():
    """SPEC 鲁棒性底线：spawnCli 检查 exitCode，非 0 时把 stderr 回显（MAJOR-3）。

    守门对象是 spawnCli 函数体：必须含 ``exitCode`` 检查 + ``__orca_error`` 信封。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    # 抽 spawnCli 函数体（粗略）
    m = re.search(r"function spawnCli\([^)]*\)[^{]*\{(?P<body>[\s\S]+?)\n\}", code)
    assert m, "未找到 spawnCli 函数"
    body = m.group("body")
    assert "exitCode" in body, "spawnCli 必须检查 exitCode"
    assert "__orca_error" in body, "spawnCli 失败时返 __orca_error 信封"
    assert "stderr" in body, "spawnCli 必须读 stderr 并回显"


def test_plugin_rewritetext_handles_error_envelope():
    """SPEC §2.6.2：rewriteText 见 __orca_error 信封时返失败回显（fail loud）。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    m = re.search(r"function rewriteText\([^)]*\)[^{]*\{(?P<body>[\s\S]+?)\n\}", code)
    assert m, "未找到 rewriteText 函数"
    body = m.group("body")
    assert "__orca_error" in body, "rewriteText 必须处理 __orca_error 信封"


# ── §2.6.1 一次性消费兜底（plugin side）─────────────────────────────────────


def test_plugin_unknown_subcommand_replaces_text_safely():
    """SPEC §2.6.1：未知子命令 → 安全回显（替换文本无 marker 字面）。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    # transform hook 内 unknown 分支
    assert "cannot dispatch" in code or "unknown subcommand" in code


# ── §2.6.1 MARKER_LITERAL 同步契约（NIT-2）──────────────────────────────────


def test_plugin_embeds_canonical_marker_literal():
    """SPEC §2.6.1：plugin TS embed 与 Python ``MARKER_LITERAL`` 同字面。

    单一真相源：改 literal 必须两处同步（regex literal 也要守同步）。
    """
    from orca.iface.in_session.templates import MARKER_LITERAL

    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    m = re.search(r'const MARKER_LITERAL = "([^"]+)"', code)
    assert m, "plugin 未定义 MARKER_LITERAL 常量"
    assert m.group(1) == MARKER_LITERAL, (
        f"plugin MARKER_LITERAL `{m.group(1)}` 与 Python `{MARKER_LITERAL}` 不同步"
    )


# ── v8.1 签名契约测试（防 builder 回退，e2e /tmp/orca-e2e-v8/ 实证形态）─────────
#
# 教训（task 根因）：plugin TS 纯单测（marker regex / 字段提取）验不出运行时签名 bug
# —— 52 单测全过却 shipped inert。hook 的调用签名（参数个数、payload 包装）只能由
# 真 opencode runtime 决定，spike 已实证的形态是唯一真相源。以下 4 测试断言 shipped
# 模板里 transform/event/message-fetch 三处的代码形态 == spike 实证形态 + bootstrap
# 返的 prompt prepend Task-tool 指令。任何回退 → 测试红。


def test_transform_hook_signature_is_two_param_async_input_out():
    """Bug A 签名契约：transform hook 必须是 ``async (input, out)`` 两参形态。

    spike ``/tmp/orca-xform/.opencode/plugins/xform.ts`` 实证：opencode 1.14.22
    runtime 实调 ``(input, out)`` 两参，``input`` 空 ``{}``、messages 在 ``out`` 上。
    shipped v8 曾回退为单参 ``async (input) => { const out = input.out ?? input }``
    —— runtime 下 input 为空对象、input.out 永远 undefined → messages 永远 [] →
    transform 静默 passthrough → 整个入口链路死。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # hook 注册行：必须形如 `"experimental.chat.messages.transform": async (input: any, out: any) =>`
    m = re.search(
        r'"experimental\.chat\.messages\.transform":\s*async\s*\(input:\s*any,\s*out:\s*any\)\s*=>',
        text,
    )
    assert m, (
        "transform hook 签名必须严格 `async (input: any, out: any) =>`（e2e /tmp/orca-xform "
        f"实证两参形态）。实际：{text.split(chr(10))[next(i for i,l in enumerate(text.split(chr(10))) if 'messages.transform' in l)][:120]}"
    )


def test_event_hook_payload_unwrap_input_event_fallback_input():
    """Bug B 签名契约：event hook 必须兼容 ``input.event`` 包装形态。

    spike ``/tmp/orca-f4/.opencode/plugin/orca.ts:7`` 实证：opencode 1.14.22 runtime
    包一层 ``{event}``，即 ``input.event.type``。shipped v8 曾回退为裸 ``event: ...``
    直访形态 → runtime 下 ``event.type`` 永远 undefined → idle 永远 early-return。
    守门形态：``const event = input?.event ?? input``（兼容解构包装 + 直传两种）。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # hook 注册：event 必须接 input 单参，不能直接解构 event
    assert re.search(r'event:\s*async\s*\(input:\s*any\)\s*=>', text), (
        "event hook 必须 `async (input: any) =>` 单参（包装在 input.event 上）"
    )
    # 必须有 unwrap + 兜底直传
    assert re.search(r'const\s+event:\s*any\s*=\s*input\?\.\s*event\s*\?\?\s*input', text), (
        "event hook 必须含严格形态 `const event: any = input?.event ?? input`（兼容包装+直传）"
    )


def test_message_fetch_uses_rest_fetch_not_sdk_client_session_message():
    """Bug F 签名契约：拉消息必须用 REST ``fetch(`/session/<sid>/message`)``。

    e2e ``/tmp/orca-e2e-v8/idle-debug.log`` + ``client-debug.log`` 实证：SDK
    ``client.session.message({id})`` 是 get-one-message-by-id（要 messageID），
    把 sessionID 当字面占位符返 ``invalid_format prefix:"ses"``。**不是** list-messages。
    spike patch `/tmp/orca-e2e-v8/orca.ts.patched-with-fixes` 实证可用形态 = REST。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    # 必须用 fetch( 拉消息
    assert "await fetch(" in code, (
        "拉 session message 必须用 REST `await fetch(`${base}/session/<sid>/message`)`"
    )
    # 守门：不得在代码（非注释）里调 client.session.message( 作 list 用途
    assert "client.session.message(" not in code, (
        "禁用 SDK client.session.message({id})（runtime 实证：那是 get-one-by-id，"
        "把 sessionID 当字面占位符返 invalid_format 错）—— 用 REST fetch 替代"
    )


def test_bootstrap_and_next_return_pointer_and_write_prompt_file(cwd_tmp, wf_path):
    """compact 交付契约（2026-07-08，替代 Bug G 的 prepend 形态）。

    bootstrap + next 不再把整段渲染后 prompt 经 .prompt 注入主 session；改为：
      1. ``.prompt`` = host-facing **指针**（"用 task 工具派子代理"+"完整指令已写入 <path>"）。
      2. ``.prompt_file`` = 渲染后 prompt 落盘路径，文件含渲染全文（含上游 output 插值）。
    两种 agent 形态渲染无差别（compile 已扁平化）；plugin 仍读 .prompt（指针文本）。
    """
    runner = CliRunner()

    # bootstrap：entry 节点 → 指针 + 文件（含 entry prompt 全文）
    boot = runner.invoke(app, ["bootstrap", str(wf_path)])
    assert boot.exit_code == 0
    boot_reply = json.loads(boot.output.splitlines()[-1])
    assert boot_reply.get("prompt_file"), "compact：bootstrap 必须返 .prompt_file"
    pointer = boot_reply["prompt"]
    assert "task 工具" in pointer and "完整节点指令已写入" in pointer, (
        "bootstrap .prompt 必须是 host-facing 指针（含 task 工具 + 文件路径提示）"
    )
    entry_file = Path(boot_reply["prompt_file"])
    assert entry_file.is_file(), f"compact prompt 文件未落盘：{entry_file}"
    assert "产出 A。" in entry_file.read_text(encoding="utf-8"), (
        "compact prompt 文件必须含 entry 节点渲染全文"
    )

    # next：下一节点 → 指针 + 文件（含上游 output 插值后的渲染全文）
    tape = boot_reply["tape"]
    run_id = boot_reply["run_id"]
    nxt = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", "OUT_A"])
    assert nxt.exit_code == 0
    nxt_reply = json.loads(nxt.output.splitlines()[-1])
    assert nxt_reply.get("prompt_file"), "compact：next 必须返 .prompt_file"
    assert "task 工具" in nxt_reply["prompt"], "next .prompt 必须是指针"
    b_file = Path(nxt_reply["prompt_file"])
    assert b_file.is_file() and b_file.name == "b.md", (
        f"next compact prompt 文件按节点名命名：<prompts_dir>/b.md，实得 {b_file}"
    )
    # node b prompt = "基于 {{ a.output }} 总结。" → 渲染后上游 output 已代入
    assert "基于 OUT_A 总结。" in b_file.read_text(encoding="utf-8"), (
        "compact prompt 文件必须含 Jinja 渲染后的完整文本（上游 output 已插值），而非 {{ }} 占位符"
    )


def test_build_pointer_is_single_source():
    """DRY 守门：指针文本单一定义（``_build_pointer``），防 bootstrap/next 各处拼字面漂移。"""
    from orca.iface.in_session.cli import _build_pointer
    from orca.run.step import StepResult

    # 有 resources_root：指针含 task 工具 + 子代理 + 文件路径 + 资源目录
    r = StepResult(node="x", prompt_file="/abs/path/x.md", resources_root="/agents/x")
    p = _build_pointer(r)
    assert "task 工具" in p and "子代理" in p
    assert "/abs/path/x.md" in p
    assert "/agents/x" in p

    # 无 resources_root：不附资源目录行
    r2 = StepResult(node="y", prompt_file="/p/y.md", resources_root=None)
    p2 = _build_pointer(r2)
    assert "/p/y.md" in p2 and "资源目录" not in p2


def test_build_cli_args_run_branch_passes_user_model():
    """Bug E 签名契约：buildCliArgs 的 run 分支必须把用户 model 作 --model argv。

    shipped v8 曾不透传 → marker.model 永远 CLI 默认，idle 注入 promptAsync 调该
    provider 失败（环境没配 deepseek 时尤甚）。
    """
    body = _extract_ts_function_body("buildCliArgs")
    m = re.search(r'if \(sub === "run"\)(?P<body>(?:.|\n)*?)(?=\n  \}|\n    if \(sub)',
                  body)
    assert m, "buildCliArgs 无 run 分支"
    run_body = m.group("body")
    assert '"--model"' in run_body, (
        "buildCliArgs run 分支必须含 --model 透传（Bug E：用户当前 model → marker.model）"
    )
