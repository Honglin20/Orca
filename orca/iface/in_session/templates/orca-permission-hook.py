#!/usr/bin/env python3
"""orca-permission-hook.py —— CC PermissionRequest hook HTTP 桥（stdlib-only）。

回答「in-session workflow 期间 CC 想调危险工具时，怎么把它推到 web 让用户审批？」：
CC 触发 PermissionRequest → spawn 本脚本（独立短命进程）→ 读 stdin JSON（容错多候选
字段名）→ POST ``http://$ORCA_HOST:$ORCA_PORT/approval`` → 阻塞等响应 → 按
``decision.behavior`` emit JSON 决策。

**安全语义**（SPEC in-session-permission-hook.md §3.1 / §7，与 hook_script.py **有意反转**）：
  - **broker 不可达**（URLError/连接失败）→ ``ask``（fail-open to CC native prompt）。
    理由：broker 不在线 = web 没了，无 web 可审批；退原生 prompt 让用户在 CC 里答。
  - **HTTP 4xx/5xx**（broker 活着但出错）→ ``deny`` + stderr warn（fail loud）。
  - **响应非 JSON / 缺 behavior** → ``deny`` + stderr warn（fail loud）。
  - **stdlib timeout** → 按 ``ORCA_APPROVAL_TIMEOUT_POLICY``（默认 ``allow``，用户明示
    notify-proceed 语义；可配 ``ask``/``deny``）。

**与 hook_script.py（gate 桥）区别（SPEC §3.1 末段）**：
  - 协议：emit ``decision.behavior`` JSON（非 exit code）。
  - 事件：PermissionRequest（非 PreToolUse）。
  - 连接失败语义：``ask``（gate 桥是 ``deny``）——in-session run 已是 AttachedRunHandle
    只读 follow，broker 不可达时退原生 prompt 优于卡死。
  - 无活跃 run 判定：本 hook 不做（broker 侧 ``resolve_session_context`` 兜底 → ``ask``）。

依赖约束（SPEC §3.1 铁律）：hook 跑在 CC spawn 的子进程，**可能没有 Orca venv**。故本
脚本**只用 stdlib**（``urllib``/``json``/``os``/``sys``/``uuid``/``time``），不 import
httpx/fastapi/orca 任何模块。

环境变量：
  - ``ORCA_HOST``：broker host（默认 ``127.0.0.1``；跨 WSL/Windows 可改，N9）。
  - ``ORCA_PORT``：broker 端口（默认 7428）。
  - ``ORCA_APPROVAL_TIMEOUT``：HTTP 超时秒（默认 600；语义级「人未在合理时间响应」，
    与 CC hook timeout=86400 留巨大余量，SPEC §3.4）。
  - ``ORCA_APPROVAL_TIMEOUT_POLICY``：``allow``|``ask``|``deny``（默认 ``allow``）。
  - ``ORCA_HOST_SESSION_ID`` / ``CLAUDE_CODE_SESSION_ID``：host session 标识（CC 注入后者）。
"""

from __future__ import annotations

# 同 hook_script.py：被 ``python <abs>/orca-permission-hook.py`` 直接跑时，脚本所在目录
# 会被加到 sys.path[0]；本脚本只用 stdlib，从不需要该目录在 sys.path——启动第一动作就是
# 摘除，恢复 stdlib 优先。开发期跑（仓库内 templates 目录）会触发；install 后跑（hooks/ 目录）
# 一般不含 "templates" / "orca"，启发式不触发但无害。
import sys as _sys
_script_dir = _sys.path[0] if _sys.path else ""
if "templates" in _script_dir or "orca" in _script_dir:
    _sys.path.pop(0)

import json
import os
import sys
import time
import urllib.error
import urllib.request


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = "7428"
_DEFAULT_TIMEOUT = 600.0
_DEFAULT_POLICY = "allow"  # SPEC §3.5：用户明示 notify-proceed
_VALID_POLICIES = ("allow", "ask", "deny")

# stdin 多候选字段名（SPEC §2 N6 spike 冻结前置；fallback 容错读取）。
_TOOL_NAME_KEYS = ("tool_name", "toolName")
_TOOL_INPUT_KEYS = ("tool_input", "toolUseInput", "input")
_SESSION_ID_KEYS = ("session_id", "sessionId")


def _read_stdin_json() -> dict:
    """读 stdin → parse JSON。

    - 读失败 / 空 stdin → 返空 dict（最坏 broker ask 兜底）。
    - JSON 解析失败（``json.JSONDecodeError``）→ **上抛**，由 main 兜底 deny+warn（fail loud，
      SPEC §7「marker 损坏 / 非 JSON」分支）。
    - 非 object JSON（如裸数字 / 数组）→ 返空 dict。
    """
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 — stdin 异常（closed pipe 等）→ 空 dict
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    data = json.loads(raw)  # JSONDecodeError 由 main 兜底（fail loud deny）。
    if not isinstance(data, dict):
        return {}
    return data


def _pick(payload: dict, candidates: tuple[str, ...], default=None):
    """容错取多候选字段名中首个命中（SPEC §2 N6）。"""
    for key in candidates:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


def _emit(behavior: str, reason: str = "") -> None:
    """emit ``{decision:{behavior, reason}}`` JSON 到 stdout（CC PermissionRequest 输出契约）。"""
    out = {"decision": {"behavior": behavior}}
    if reason:
        out["decision"]["reason"] = reason
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _resolve_session_id(payload: dict) -> str | None:
    """session_id = ORCA_HOST_SESSION_ID | CLAUDE_CODE_SESSION_ID | stdin（SPEC §3.1 step 2）。"""
    for env_key in ("ORCA_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        val = os.environ.get(env_key)
        if val:
            return val
    sid = _pick(payload, _SESSION_ID_KEYS)
    return sid if isinstance(sid, str) and sid else None


def _build_request(payload: bytes, host: str, port: str) -> urllib.request.Request:
    """构造 POST /approval 的 Request（``Content-Type: application/json``）。"""
    url = f"http://{host}:{port}/approval"
    return urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def main() -> int:
    """读 stdin → POST /approval → emit decision。任何未预期异常 → ask（fail-open to native）。"""
    # stdin 读失败（非 JSON 损坏）→ deny + warn（fail loud，SPEC §7 marker 损坏行）。
    try:
        payload_in = _read_stdin_json()
    except json.JSONDecodeError as e:
        sys.stderr.write(f"orca-permission-hook: stdin 非 JSON（fail loud deny）：{e}\n")
        _emit("deny", "hook stdin non-JSON")
        return 0

    tool = _pick(payload_in, _TOOL_NAME_KEYS, default="<unknown>")
    tool_input = _pick(payload_in, _TOOL_INPUT_KEYS, default={})
    session_id = _resolve_session_id(payload_in)
    hook_event = payload_in.get("hook_event_name") or "PermissionRequest"

    out_body = {
        "session_id": session_id,
        "tool": tool,
        "tool_input": tool_input if isinstance(tool_input, (dict, list)) else {},
        "hook_event": hook_event,
    }

    host = os.environ.get("ORCA_HOST", _DEFAULT_HOST)
    port = os.environ.get("ORCA_PORT", _DEFAULT_PORT)
    try:
        timeout = float(os.environ.get("ORCA_APPROVAL_TIMEOUT", _DEFAULT_TIMEOUT))
    except ValueError:
        sys.stderr.write(
            f"ORCA_APPROVAL_TIMEOUT 非法，回退默认 {_DEFAULT_TIMEOUT}s\n"
        )
        timeout = _DEFAULT_TIMEOUT

    policy = os.environ.get("ORCA_APPROVAL_TIMEOUT_POLICY", _DEFAULT_POLICY).strip().lower()
    if policy not in _VALID_POLICIES:
        sys.stderr.write(
            f"ORCA_APPROVAL_TIMEOUT_POLICY={policy!r} 非法（允许 {list(_VALID_POLICIES)}），"
            f"回退默认 {_DEFAULT_POLICY}\n"
        )
        policy = _DEFAULT_POLICY

    req = _build_request(
        json.dumps(out_body, ensure_ascii=False).encode("utf-8"), host, port,
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # broker 活着但 4xx/5xx → deny + warn（fail loud N4）。
        sys.stderr.write(
            f"orca-permission-hook: broker HTTP {e.code}（fail loud deny）：{e.reason}\n"
        )
        _emit("deny", f"broker HTTP {e.code}")
        return 0
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # 连接失败 / 解析期超时。Python 3.10+ ``urlopen`` 超时可能抛 builtin TimeoutError
        # 或 URLError(reason=socket.timeout)；统一在此分支判 timeout vs 不可达。
        # 3.10+ ``socket.timeout`` 已统一为 builtin ``TimeoutError`` 别名，故 ``isinstance`` 兜底。
        elapsed = time.monotonic() - t0
        reason = getattr(e, "reason", e)
        is_timeout = isinstance(e, TimeoutError) or isinstance(reason, TimeoutError)
        # elapsed 兜底（接近 timeout 视作超时，防 reason 形态差异）。
        if is_timeout or elapsed >= timeout * 0.9:
            sys.stderr.write(
                f"orca-permission-hook: broker 超时（policy={policy}）：{reason}\n"
            )
            _emit(policy, "approval timeout")
            return 0
        # 真网络失败（broker 不可达）→ ask（fail-open to native，SPEC §7）。
        sys.stderr.write(
            f"orca-permission-hook: broker 不可达（fail-open ask）：{reason}\n"
        )
        _emit("ask", "broker unreachable")
        return 0
    except Exception as e:  # noqa: BLE001 — 未预期异常 → ask（保守降级到原生）
        sys.stderr.write(
            f"orca-permission-hook: 未预期异常（fail-open ask）：{e}\n"
        )
        _emit("ask", "hook unexpected error")
        return 0

    # 检查 HTTP 状态（urllib 不把 4xx/5xx 当异常——上面 HTTPError 其实会 catch；双保险）。
    if status >= 400:
        sys.stderr.write(
            f"orca-permission-hook: broker HTTP {status}（fail loud deny）：{body[:200]}\n"
        )
        _emit("deny", f"broker HTTP {status}")
        return 0

    # 解析响应：``{behavior: "allow"|"deny"|"ask", approval_id?, resolved_by?}``。
    try:
        result = json.loads(body)
        behavior = result.get("behavior")
    except (json.JSONDecodeError, AttributeError):
        sys.stderr.write(
            f"orca-permission-hook: 响应非 JSON（fail loud deny）：{body!r}\n"
        )
        _emit("deny", "broker response non-JSON")
        return 0

    if behavior not in ("allow", "deny", "ask"):
        sys.stderr.write(
            f"orca-permission-hook: 响应 behavior 非法（fail loud deny）：{behavior!r}\n"
        )
        _emit("deny", "broker behavior invalid")
        return 0

    _emit(behavior)
    return 0


if __name__ == "__main__":
    sys.exit(main())
