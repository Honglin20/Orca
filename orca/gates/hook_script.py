#!/usr/bin/env python3
"""hook_script.py —— PreToolUse hook HTTP 桥（claude spawn 的独立短命进程）。

回答「claude 想调危险工具时，怎么同步阻塞它等 Orca 决策？」：claude 想调工具 → spawn
本脚本（独立短命进程）→ 脚本读 stdin JSON（含 ``session_id, tool_name, tool_input,
tool_use_id``）→ POST ``http://localhost:$ORCA_PORT/gate`` → 阻塞等响应 → 按
``decision`` exit 0（允许）/ 2（拒绝）。

安全语义（SPEC §3.3，**HMIL 底线**，2026-06-30 定稿）：
  - **Orca server 不可达**（连接失败）→ **立即 exit 2**（拒绝，绝不放行）。
  - **超时**（默认 60s，``ORCA_GATE_TIMEOUT`` 可配）→ **exit 2**（拒绝）。
  - **响应非法**（非 JSON / 缺 ``decision``）→ **exit 2**（拒绝）。
  - **响应 decision=="allow"** → exit 0。
  - **响应 decision=="deny"** / 其它 → exit 2。

**权衡**（SPEC §3.3）：可用性 vs 安全。HMIL 本质是「危险操作要人确认」，桥断了放行
= 最坏情况（误删文件不可逆）。workflow 卡住是可接受的（用户能重试），代价：Orca 挂了
workflow 会卡，但这是可接受的安全代价。

依赖约束（SPEC §3.4 关键）：hook 跑在 claude spawn 的子进程里，**可能没有 Orca 的
venv**（claude 用系统 Python spawn hook）。故本脚本**只用 stdlib**（``urllib`` /
``json`` / ``os`` / ``sys``），不 import httpx / fastapi / orca 任何模块。保证它在
任何 Python 3.10+ 环境都能跑。

环境变量：
  - ``ORCA_PORT``：Orca server 端口（默认 7421，与 phase 9 web server 同站）。
  - ``ORCA_GATE_TIMEOUT``：HTTP 超时秒（默认 60；CC 对 hook 无硬超时，但 60s 是合理上限）。
  - ``ORCA_HOST``：Orca server host（默认 ``127.0.0.1``；本地编排，不暴露公网）。

退出码：
  - 0：允许（claude 收到继续）。
  - 2：拒绝 / 任何异常 / 超时（claude 收到拒绝反馈）。

可作为脚本 ``python hook_script.py`` 直接跑，也可被 ``python -m orca.gates.hook_script``
调用（但后者需 Orca venv，不推荐用于真实 hook 场景——真实场景用绝对路径）。
"""

from __future__ import annotations

# 当本文件被 ``python orca/gates/hook_script.py`` 直接运行时，Python 把脚本所在目录
# （``orca/gates/``）加到 ``sys.path[0]``，导致后续 ``import json`` → ``import re``
# → ``from types import GenericAlias`` 时把本目录的 ``types.py`` 误当 stdlib ``types``
# 模块（circular import：``orca/gates/types.py`` 自身又 ``import typing``）。本脚本只用
# stdlib（SPEC §3.4），从不需要 ``orca/gates/`` 在 sys.path——故启动第一动作就是把脚本
# 目录从 sys.path 摘除，恢复 stdlib 优先。必须在所有其他 import 之前执行。
import sys as _sys
_script_dir = _sys.path[0] if _sys.path else ""
if "orca" in _script_dir and "gates" in _script_dir:
    _sys.path.pop(0)

import json
import os
import sys
import urllib.error
import urllib.request

# 默认值（SPEC §3.4；与 phase 9 web server 同站的 7421 是 web UI 端口，gate 端点
# 复用同一 server，故默认端口对齐）。
_DEFAULT_PORT = "7421"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_TIMEOUT = 60.0  # 秒

# 退出码（claude 协议：0=允许，2=拒绝）。
_EXIT_ALLOW = 0
_EXIT_DENY = 2


def _build_request(payload: bytes, host: str, port: str) -> urllib.request.Request:
    """构造 POST /gate 的 Request（``Content-Type: application/json``）。"""
    url = f"http://{host}:{port}/gate"
    return urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def main() -> int:
    """读 stdin → POST /gate → 按响应 decision exit。任何异常 → exit 2（安全优先）。"""
    # 读 stdin（hook 的标准输入：claude 注入的 JSON）。读不到也 exit 2（fail loud）。
    try:
        raw = sys.stdin.read()
        payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    except Exception:
        # 读 stdin 失败 = 协议层异常 → 安全优先 exit 2
        return _EXIT_DENY

    host = os.environ.get("ORCA_HOST", _DEFAULT_HOST)
    port = os.environ.get("ORCA_PORT", _DEFAULT_PORT)
    try:
        timeout = float(os.environ.get("ORCA_GATE_TIMEOUT", _DEFAULT_TIMEOUT))
    except ValueError:
        # 非法 timeout 配置 → 用默认（不阻断，但记 stderr 可见）
        sys.stderr.write(
            f"ORCA_GATE_TIMEOUT 非法，回退默认 {_DEFAULT_TIMEOUT}s\n"
        )
        timeout = _DEFAULT_TIMEOUT

    req = _build_request(payload, host, port)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # 连接失败 / 超时 / HTTP 错误 → 全部 exit 2（安全优先，SPEC §3.3）
        # 超时：URLError 原因是 socket.timeout
        sys.stderr.write(f"hook 桥连接 Orca 失败（exit 2 安全优先）：{e}\n")
        return _EXIT_DENY
    except Exception as e:  # 兜底：任何未预期异常
        sys.stderr.write(f"hook 桥异常（exit 2 安全优先）：{e}\n")
        return _EXIT_DENY

    # 解析响应：必须是 ``{"decision": "allow"|"deny", ...}``。
    try:
        result = json.loads(body)
        decision = result.get("decision")
    except (json.JSONDecodeError, AttributeError):
        # 响应非 JSON / 缺 decision → exit 2（安全优先）
        sys.stderr.write(f"hook 桥响应非法（exit 2）：{body!r}\n")
        return _EXIT_DENY

    # 仅 ``decision == "allow"`` 才放行；其它一律 exit 2。
    if decision == "allow":
        return _EXIT_ALLOW
    return _EXIT_DENY


if __name__ == "__main__":
    # 模块入口 / 脚本入口：执行 main，用其返回值作为 exit code。
    sys.exit(main())
