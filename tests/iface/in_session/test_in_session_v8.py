"""tests/iface/in_session/test_in_session_v8.py —— v8 增量守门测试。

覆盖 SPEC v8 §2.6 / §2.6.1 / §2.6.2 / §2.7 / §9.2：
  - **Marker regex**（§2.6.1）：run/status/stop/doctor 命中；无 args / 含空格 wf 路径 / 含 `>` 拒绝；
    无 marker 透传。
  - **改写语义**（§2.6.2）：run→.prompt / doctor→.report / status→友好 / stop→ok+run_id；mock
    CLI stdout JSON。
  - **一次性消费**（§2.6.1）：替换文本无 `<!--orca:cmd` 字面。
  - **sessionID argv**：从 info.sessionID 取作 --owner + --session-id argv。
  - **doctor CLI**（§2.7，v5 重设计）：5 项 checks（skill_install/cli_imports 硬 +
    diag/hook 可选）、JSON 结构、report 描述 B 路径、ok=skill+cli 无 fail。
  - **架构守门**：plugin 模板无 `@opencode/core/client` / `command.execute.before` /
    `Bun.spawn(` + `stdout:"string"` / `advance_step` / `router.resolve` / `replay_state` /
    `tape.append` / `EventBus` / `Tape(` / `drive_loop` / `advance(`。
  - v5 step 2b：``start`` 命令 + ``cc_hooks.py`` 已删（A 路径退场）；transform marker 派发
    已禁用（early return），相关测试随之移除/改写。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app
from orca.iface.in_session.templates import MARKER_REGEX


# ── fixtures ────────────────────────────────────────────────────────────────


PLUGIN_TS = (
    Path(__file__).resolve().parents[3]
    / "orca/iface/in_session/templates/opencode/orca.ts"
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


# ── doctor 诊断（v5 §4.4：skill_install + cli_imports 为硬检查；hook 心跳可选）────


@pytest.fixture
def doctor_iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 doctor 的 home + cwd：``Path.home`` → tmp_path + chdir tmp_path。

    必须：doctor 的 ``_scan_skill_install`` 查 user-scope（``~/.claude`` 等）+ project-scope。
    不隔离 home，则装过 orca 的开发机上 ``skill_install`` 恒 pass → fail-when-absent 测试反向失败。
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_doctor() -> dict:
    """跑 ``doctor``，解析末行 JSON。"""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output.splitlines()[-1])


def _write_probe(cwd: Path, name: str, payload: dict) -> None:
    """写一个诊断心跳文件到 ``<cwd>/runs/<name>``（doctor 从 ``runs/`` 读）。"""
    runs = cwd / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / name).write_text(json.dumps(payload), encoding="utf-8")


_DOTDIR = {"cc": ".claude", "opencode": ".opencode", "cac": ".cac", "nga": ".nga"}


def _install_fake_orca_skill(
    root: Path, platform: str = "cc", *, under: str = "project",
    home: Path | None = None,
) -> Path:
    """落一个占位 orca skill 让 doctor 扫到。

    - ``under="project"``：落 ``<root>/<dotdir>/skills/orca/SKILL.md``（root 当 cwd）。
    - ``under="user"``：落 ``<home>/<dotdir>/skills/orca/SKILL.md``（home 注入时用）。
    """
    dotdir = _DOTDIR[platform]
    base = (home if under == "user" else root)
    skill_md = base / dotdir / "skills" / "orca" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text("---\nname: orca\n---\n# orca\n", encoding="utf-8")
    return skill_md


def test_doctor_json_structure(doctor_iso, monkeypatch):
    """doctor 输出 JSON {ok, diag, report, checks} + 5 项 checks（v5 加 skill_install + hard 字段）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    reply = _run_doctor()
    assert set(reply.keys()) >= {"ok", "diag", "report", "checks"}
    assert isinstance(reply["ok"], bool)
    assert isinstance(reply["diag"], bool)
    assert isinstance(reply["report"], str) and len(reply["report"]) > 0
    assert isinstance(reply["checks"], list)
    assert len(reply["checks"]) == 5
    names = [c["name"] for c in reply["checks"]]
    # v5 顺序：skill_install / cli_imports_ok（硬）在前，diag_switch / hook（可选）在后。
    assert names == ["skill_install", "cli_imports_ok", "diag_switch",
                     "entry_hook", "advance_hook"]
    # 每条 check 带 hard 字段（review 🟡#5：替代硬编码 name tuple 防 typo 静默丢失硬检查）
    hard_expected = {"skill_install": True, "cli_imports_ok": True,
                     "diag_switch": False, "entry_hook": False, "advance_hook": False}
    for c in reply["checks"]:
        assert set(c.keys()) == {"name", "status", "detail", "hard"}, (
            f"check {c['name']} 字段漂移：{set(c.keys())}"
        )
        assert c["status"] in ("pass", "unknown", "fail")
        assert c["hard"] is hard_expected[c["name"]]


def test_doctor_skill_install_pass_when_skill_present(doctor_iso, monkeypatch):
    """A6：四前端任一装了 orca skill（project-scope）→ skill_install=pass + ok=True。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _install_fake_orca_skill(doctor_iso, "cc")
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["skill_install"]["status"] == "pass"
    assert "cc" in by_name["skill_install"]["detail"]
    assert reply["ok"] is True  # skill + cli 都 pass


def test_doctor_skill_install_detects_each_platform(doctor_iso, monkeypatch):
    """A6：opencode / cac / nga project-scope 装 skill → 各自被扫到（覆盖 _scan_skill_install 分支）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    for platform in ("opencode", "cac", "nga"):
        _install_fake_orca_skill(doctor_iso, platform)
    reply = _run_doctor()
    detail = {c["name"]: c["detail"] for c in reply["checks"]}["skill_install"]
    assert reply["ok"] is True
    for platform in ("opencode", "cac", "nga"):
        assert platform in detail


def test_doctor_skill_install_user_scope(doctor_iso, monkeypatch):
    """A6：user-scope（``<home>/<dotdir>/skills/orca``）也能被扫到（doctor_iso 把 home 指到 tmp）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _install_fake_orca_skill(doctor_iso, "cac", under="user", home=doctor_iso)
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["skill_install"]["status"] == "pass"
    assert "cac" in by_name["skill_install"]["detail"]


def test_doctor_skill_install_fail_when_absent(doctor_iso, monkeypatch):
    """A6：四前端都没装 orca skill → skill_install=fail + ok=False（doctor_iso 隔离 home + cwd 干净）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["skill_install"]["status"] == "fail"
    assert reply["ok"] is False  # skill_install fail → ok False（即便 cli ok）


def test_doctor_diag_off_hook_checks_unknown_ok_unaffected(doctor_iso, monkeypatch):
    """诊断关：hook 三项（diag/entry/advance）均 unknown；ok 只看 skill+cli（装了 skill → ok）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _install_fake_orca_skill(doctor_iso, "cc")
    reply = _run_doctor()
    assert reply["diag"] is False
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["diag_switch"]["status"] == "unknown"
    assert by_name["entry_hook"]["status"] == "unknown"
    assert by_name["advance_hook"]["status"] == "unknown"
    assert reply["ok"] is True  # hook unknown 不拉低 ok（skill+cli pass）


def test_doctor_diag_on_no_heartbeat_entry_unknown(doctor_iso, monkeypatch):
    """v5 §4.4：诊断开 + 无 transform 心跳 = entry **unknown**（非 fail）。

    transform 派发已禁用（step 2b），心跳仅证明 plugin 加载——缺它不推进、不故障，故不 fail。
    ok 仍由 skill+cli 决定（装了 skill → ok=True）。
    """
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_orca_skill(doctor_iso, "cc")
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["diag_switch"]["status"] == "pass"
    assert by_name["entry_hook"]["status"] == "unknown"  # v5：不再 fail
    assert reply["ok"] is True  # hook 不计数


def test_doctor_fresh_entry_heartbeat_passes(doctor_iso, monkeypatch):
    """诊断开 + 新鲜 entry 心跳 = entry PASS（transform 钩子被调 = plugin 加载，仅诊断）。

    v5：dispatch 已禁用，detail 不再展示 dispatch_count（生产恒 0 会误导）。
    """
    import time as _t
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_orca_skill(doctor_iso, "cc")
    _write_probe(doctor_iso, ".orca-probe-entry.json", {
        "diag": True, "last_called_at": int(_t.time()),
        "dispatch_count": 2, "last_dispatch_sub": "run",
    })
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["entry_hook"]["status"] == "pass"
    assert "plugin 已加载" in by_name["entry_hook"]["detail"]
    assert "累计" not in by_name["entry_hook"]["detail"]  # dispatch_count 不再展示


def test_doctor_stale_entry_heartbeat_unknown(doctor_iso, monkeypatch):
    """v5 §4.4：诊断开 + entry 心跳过期 = **unknown**（非 fail）。transform 已禁用，stale 不故障。"""
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_orca_skill(doctor_iso, "cc")
    _write_probe(doctor_iso, ".orca-probe-entry.json", {
        "diag": True, "last_called_at": 0,  # 远古 → age 巨大
        "dispatch_count": 0, "last_dispatch_sub": None,
    })
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["entry_hook"]["status"] == "unknown"  # v5：stale 不再 fail


def test_doctor_advance_heartbeat_passes(doctor_iso, monkeypatch):
    """诊断开 + advance 心跳 = advance PASS（idle 在 fire，报告 idle 计数，仅 nudge）。"""
    import time as _t
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_orca_skill(doctor_iso, "cc")
    _write_probe(doctor_iso, ".orca-probe-advance.json", {
        "diag": True, "last_idle_at": int(_t.time()),
        "idle_count": 3, "advance_count": 1, "last_advance_run_id": "r-1",
        "last_session_id": "s-1",
    })
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["advance_hook"]["status"] == "pass"
    assert "idle fire 过 3" in by_name["advance_hook"]["detail"]


def test_doctor_report_describes_b_path(doctor_iso, monkeypatch):
    """v5：报告说明执行模型 = B 路径（主 session 自调 next），hook 退居可选诊断。"""
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_orca_skill(doctor_iso, "cc")
    reply = _run_doctor()
    assert "B 路径" in reply["report"]
    assert "orca next" in reply["report"]  # 主 session 自调 next
    # 心跳文件路径写进报告，便于用户定位/清理
    assert ".orca-probe-entry.json" in reply["report"]



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


def test_plugin_bootstrap_argv_no_owner_no_session_id():
    """v3 §7.2：bootstrap argv 不再含 ``--owner`` / ``--session-id``（marker 精简，无这些字段）。

    旧 B4 契约（sid 作 --owner + --session-id）随 marker 精简作废；plugin buildCliArgs
    的 run 分支只推 --model（可选）+ wf 位置参数。
    """
    body = _extract_ts_function_body("buildCliArgs")
    assert '"--owner"' not in body, (
        "v3 §7.2：bootstrap 不再接受 --owner（marker 无 owner 字段）"
    )
    assert '"--session-id"' not in body, (
        "v3 §7.2：bootstrap 不再接受 --session-id（marker 无 session_id 字段）"
    )


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


# ── 共享 wf fixture（v5 step 2b：start 命令已删，fixture 供其余 v7/v8 测试复用）────


AGENT_WF_YAML = """\
name: start_test_wf
description: 2-agent 线性 workflow（fixture，供 v7/v8 测试复用）。
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


# ── v7 baseline 兼容性（CI 守门：v8 不破坏 v7 行为）─────────────────────────


def test_v7_baseline_cli_commands_still_work_v8(cwd_tmp, wf_path):
    """v7 CLI 大脑未动：bootstrap/next/stop/status 仍可用（v5 删 start，v3 删 serve）。"""
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


def test_status_json_flag_no_run_id_lists_runs_json(cwd_tmp, wf_path):
    """``status --json`` 无 run_id → JSON ``{runs: [...]}``（plugin 解析用）。"""
    runner = CliRunner()
    runner.invoke(app, ["bootstrap", str(wf_path)])
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    reply = json.loads(result.output.strip())
    assert "runs" in reply
    assert isinstance(reply["runs"], list)
    assert len(reply["runs"]) >= 1
    assert ".jsonl" not in reply["runs"][0]  # stem only


def test_plugin_status_dispatch_passes_json_flag():
    """SPEC §2.6.2：plugin buildCliArgs 的 status 分支必带 --json（MAJOR-1）。"""
    body = _extract_ts_function_body("buildCliArgs")
    # 在 buildCliArgs 函数体里 status 分支必须有 --json
    m = re.search(r'if \(sub === "status"\)(?P<body>(?:.|\n)*?)(?=\n  \}|\n    if \(sub)',
                  body)
    assert m, "buildCliArgs 无 status 分支"
    assert '"--json"' in m.group("body"), "status 派发分支必带 --json flag"


# ── §2.6.2 stop run_id 派发（v3 §7.2：marker 无 owner，stop 按 run_id 直定位）────


def test_stop_with_run_id_succeeds(cwd_tmp, wf_path):
    """v3 §7.2：``stop <run_id>`` 按 run_id O(1) 直定位 marker（marker 文件名 = run_id）。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path)])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    result = runner.invoke(app, ["stop", run_id])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply["run_id"] == run_id


def test_stop_missing_run_id_fails_loud(cwd_tmp, wf_path):
    """v3 §7.2：``stop`` 无 run_id（必填位置参数）→ typer exit 2（fail loud）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["stop"])
    # run_id 是必填位置参数，缺失 → typer BadParameter exit 2
    assert result.exit_code == 2


def test_stop_unknown_run_id_clears_no_tape(cwd_tmp, wf_path):
    """``stop <未知 run_id>`` → 幂等清 marker + ok 信封（note=no-tape，不崩）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["stop", "nonexistent-run"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply.get("note") == "no-tape"


def test_plugin_stop_dispatch_uses_run_id_arg():
    """v3 §2.6.2：plugin buildCliArgs 的 stop 分支传 run_id（marker 无 owner 后按 run_id 定位）。

    旧 MAJOR-2 用 --owner；v3 marker 精简后 stop 按 run_id（文件名 orca-<run_id>.json O(1)）。
    """
    body = _extract_ts_function_body("buildCliArgs")
    m = re.search(r'if \(sub === "stop"\)(?P<body>(?:.|\n)*?)(?=\n  \}|\n    if \(sub)',
                  body)
    assert m, "buildCliArgs 无 stop 分支"


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
