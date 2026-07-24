"""_hostenv.py —— 宿主 session / backend / family 的 env+进程身份探测（单一来源）。

**回答的问题**：sidechain family（cc 读 ``~/.claude`` / cac 读 ``~/.cac``）该由什么决定？
答案：**当前 session 的 env/进程身份**，不是 dotdir 存在性。``orca install`` 会主动建
``~/.cac`` / ``~/.nga``（装 hook+skill 给换皮前端），故 dotdir 存在只代表"装过该前端"，
不代表"当前 session 是它"。用 dotdir 存在性判 family 会误判：真 CC（``CLAUDE_CODE_SESSION_ID``
在、数据在 ``~/.claude``）+ ``~/.cac`` 存在（install 副作用）→ 误读空的 ``~/.cac``，daemon
ingest 0 条 → 子 agent 消息进不了 web。

**family 决策优先级（单一真相源）**：env 身份 > config 显式（``sidechain.family``）>
dotdir 探测（仅兜底）。caller 用 ``detect_family_from_env() or <config>``，None 时回退不清空。

**为什么独立模块**：env/进程探测（读 ``/proc``、env var）是独立于 config I/O（``config.py``）
的职责域。``cli.py`` 与 ``sidechain_cmds.py`` 都需要，而 ``sidechain_cmds.py`` 严禁 import
``cli``（cli 模块级 ``add_typer`` 挂载它，反 import 成环）；放 in_session **同层**两方共享，
无环。同时消除既有 ``cac_session_id_from_pid`` / ``host_session_from_env`` 在 cli.py 与
sidechain_cmds.py 的**字节级副本**（DRY）。

**四个函数**（backend 与 family 同源，两轴正交）：
  - ``detect_backend_from_env``：CC 家族（cc）vs opencode 家族——选 adapter（CCJsonlAdapter /
    OpencodeSqliteAdapter）。
  - ``detect_family_from_env``：CC 家族的子型 cc vs cac（读 ``.claude`` vs ``.cac``）。
    ``CLAUDE_CODE_SESSION_ID`` 在 → cc（真 Claude Code）；``CODEAGENT`` + PID 回溯命中
    codeagentcli → cac（CAC 换皮，不注入 ``CLAUDE_CODE_SESSION_ID``）。仅在 backend=="cc"
    时有意义；其余返 None（caller 回退 config/probe）。
  - ``host_session_from_env``：宿主 session id（env > cac PID 回溯；tape host_session 真相源）。
  - ``cac_session_id_from_pid``：PID 链回溯 codeagentcli，读 ``~/.cac/sessions/<pid>.json``
    （CAC 不把 session id 写 ``process.env`` 的兜底）。

**依赖单向**：stdlib only（``json`` / ``os`` / ``pathlib``），不 import 任一 orca 子包——保本模块
可被 cli.py / sidechain_cmds.py / 测试零代价共享（无环、无跨层副作用、import 时不读 ``/proc``，
不拖慢任何 import 它的模块）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def cac_session_id_from_pid() -> str | None:
    """沿 PID 链向上找 CAC 主进程（cmdline 含 ``codeagentcli``），
    从 ``~/.cac/sessions/<cac_pid>.json`` 读 ``sessionId``。

    解决 CAC 未将 session id 写入 ``process.env`` 的问题——CAC 把 sessionId 存在内存变量
    ``eZ.sessionId``，``subprocessEnv()`` 传的是 ``process.env``，故 bash 子进程继承不到。
    本函数经 ``/proc`` 回溯找到 CAC 父进程 PID，再从 sessions 目录查 sessionId。

    并行安全：每个 bash 进程沿自己的 PID 链回溯，只指向自己的 CAC 父进程，不会错混到
    同一项目下其他并行 session。
    """
    sessions_dir = Path.home() / ".cac" / "sessions"
    if not sessions_dir.is_dir():
        return None

    pid = os.getpid()
    for _ in range(20):
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            ppid_line = next(
                (l for l in status.splitlines() if l.startswith("PPid:")), None
            )
            if not ppid_line:
                break
            ppid = int(ppid_line.split()[1])
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            break

        try:
            raw = Path(f"/proc/{ppid}/cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            pid = ppid
            continue

        # 第一个 \0 前是 exe 路径；精确匹配可执行文件名，避免 bash snapshot 等子进程的
        # cmdline 参数含 "codeagentcli" 字样而误匹配。
        exe = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        if exe.endswith("/codeagentcli") or exe == "codeagentcli":
            session_file = sessions_dir / f"{ppid}.json"
            if session_file.exists():
                try:
                    return json.loads(session_file.read_text()).get("sessionId")
                except (json.JSONDecodeError, KeyError):
                    pass
            break

        pid = ppid
        if pid <= 1:
            break

    return None


def host_session_from_env() -> str | None:
    """宿主 session id：优先级 ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > CAC PID 回溯 > None。

    - **CC**：零配置（CC 给所有 bash 子进程注入 ``CLAUDE_CODE_SESSION_ID``）。
    - **opencode**：需 plugin ``shell.env`` 钩子注入 ``ORCA_HOST_SESSION_ID``；未注入 → None
      （fail-safe：该 run 的 host_session 落 tape 为 null，nudge 跳过）。
    - **CAC**：未注入 env → PID 链回溯 ``codeagentcli`` 父进程，读 ``~/.cac/sessions/<pid>.json``
      （并行安全：每个 bash 的 PID 链只指向自己的 CAC 父进程）。

    单一真相源铁律：host_session 单路采集（env → bootstrap → tape），marker 不复存。
    """
    sid = os.environ.get("ORCA_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sid
    return cac_session_id_from_pid()


def detect_backend_from_env() -> str | None:
    """从 env 推断宿主 backend（``cc`` / ``opencode``），spawn sidechain 守护选 adapter 用。

    推断规则（SPEC-B v4 §0：backend 选择属 daemon 启动参数）：
      - ``CLAUDE_CODE_SESSION_ID`` 存在 → ``"cc"``（CC 自动注入所有 bash 子进程）。
      - 否则 ``ORCA_HOST_SESSION_ID`` 存在 → ``"opencode"``（opencode plugin 显式注入）。
      - 否则 ``CODEAGENT=1`` + ``host_session_from_env()`` 可用（PID 回溯命中） → ``"cc"``
        （CAC 是 CC 换皮，但不注入 ``CLAUDE_CODE_SESSION_ID``；PID 回溯反查 session id）。
      - 都无 → ``None``（非 in-session 起的 run，B2 守护无法启动）。

    返 ``None`` 时调用方应 skip spawn + warn（fail-open：run 仍可推进，只是子 agent 过程
    不进 web）。
    """
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "cc"
    if os.environ.get("ORCA_HOST_SESSION_ID"):
        return "opencode"
    if os.environ.get("CODEAGENT") and host_session_from_env():
        return "cc"
    return None


def detect_family_from_env() -> str | None:
    """从 env/进程身份推断 CC 家族子型（``cc`` / ``cac``），与 ``detect_backend_from_env`` 同源。

    **回答**：当前 CC 后端是"真 Claude Code"还是"CAC 换皮"？前者读 ``~/.claude``，后者读
    ``~/.cac``。判据是 env/进程身份，**不是 dotdir 存在性**——``orca install`` 会主动建
    ``~/.cac``（装 hook/skill 给 cac 前端），dotdir 存在与"当前 session 是 cac"无关
    （详见模块 docstring）。用 dotdir 存在性判 family 会误判真 CC 走 cac。

    推断规则：
      - ``CLAUDE_CODE_SESSION_ID`` 存在 → ``"cc"``（真 Claude Code 自动注入）。
      - 否则 ``CODEAGENT=1`` + ``cac_session_id_from_pid()`` 命中 → ``"cac"``
        （CAC 是 CC 换皮，不注入 ``CLAUDE_CODE_SESSION_ID``；PID 回溯反查 session id）。
      - 其它 → ``None``（opencode 家族 / 非 in-session；family 由 caller 走 config/probe）。

    与 ``detect_backend_from_env`` 的关系：backend 区分 CC vs opencode 家族（选 adapter）；
    本函数区分 CC 家族的 cc vs cac 子型（选 dotdir），仅在 backend=="cc" 时有意义。

    单一真相源：family 决策此处为权威 env 源，优先于 config（``sidechain.family``）与 dotdir
    探测；caller 用 ``detect_family_from_env() or <config>`` 保证 None 时回退（不清空）。
    """
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "cc"
    if os.environ.get("CODEAGENT") and cac_session_id_from_pid():
        return "cac"
    return None
