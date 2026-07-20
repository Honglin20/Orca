#!/usr/bin/env bash
# Orca CC nudge —— Claude Code Stop hook（v5 §4.4 / step 2b(7) + host-session-binding v2）。
#
# 主 session 试图结束其 turn 时触发：若有活跃 Orca run（marker 存在）**且归属当前 session**
# → 发 decision:block 注入「请调 orca next 推进」的提醒。**绝不调 orca next**（B 路径铁律：
# 主 session 自调 next；hook 自动调 next = 退化 A 路径）。
#
# host-session-binding v2（tape-only，§2.3/§4.4）：
#   - current = ORCA_HOST_SESSION_ID ?? CLAUDE_CODE_SESSION_ID（CC 注入后者，零配置）。
#   - glob marker 拿 run_id → 对每个 run 读 tape 首条 workflow_started.data.host_session
#     → 仅收 == current 的（读 tape 派生，marker 不存归属，单一真相源铁律）。
#   - per-session 限流：STATE 按 current 分键（防 A 的 nudge 抑制 B）。
#   - 无 current 但有活跃 marker → stderr warn（区分「手 CLI」与「env 注入 bug」，评审 C10）。
#
# 判定只看 marker 存在（runs/orca-<run_id>.json）——不用 tape 超时（会误报）。marker
# 在终态由 CLI 清掉，故「有 marker」≡「run 还活着」。
#
# 节流：60s 内不重复 block（防 Stop 反复触发刷屏）。block 后写时间戳，窗口内再次 Stop 直
# 接放行（让模型能停下来等用户/子代理）；窗口外再 block。
#
# 由 'tars install --target cc' 落到 <cc_root>/hooks/orca-nudge.sh 并在
# .claude/settings.json 的 hooks.Stop 声明引用。
#
# 实现：python3（DEFECT-1 修复；orca 本就依赖 python，跨环境可靠——WSL conda orca 等环境
# 不一定有 jq）。**fail loud**：marker 文件不可读 / 非合法 JSON → 写 stderr + exit 2，绝不
# 静默吞错（旧版用 jq 加 2>/dev/null 加 || true 在缺 jq 时静默失败 → nudge 永不触发且无报错，
# 违反 fail-loud；用户看不到任何信号）。
#
# 铁律：本脚本全篇**零反引号**——REASON 是双引号 bash 字符串，双引号内反引号 = 命令替换，
# 会误执行 orca next 退化 A 路径。命令名一律纯文本提及（提醒模型去调，非脚本执行）。
set -euo pipefail

# CC Stop hook 的 cwd = 用户项目根；python heredoc 在该 cwd 下跑，相对路径 runs/... 即项目
# runs/ 目录（marker 落点 = runs/orca-<run_id>.json）。
exec python3 - <<'PYEOF'
import glob
import json
import os
import sys
import time
from pathlib import Path

THROTTLE_SEC = 60


def _cac_session_id_from_pid() -> str | None:
    """沿 PID 链向上找 CAC 主进程（cmdline 含 ``codeagentcli``），
    从 ``~/.cac/sessions/<cac_pid>.json`` 读 ``sessionId``。
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
            cmdline = Path(f"/proc/{ppid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError):
            pid = ppid
            continue

        if "codeagentcli" in cmdline:
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


def _host_session_from_env() -> str | None:
    """当前宿主 session id（优先级 ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > CAC PID 回溯 > None）。

    与 orca/iface/in_session/cli.py 的 _host_session_from_env 同源（SPEC §4.2 公共 env 契约）。
    CC 给所有 bash 子进程注入 CLAUDE_CODE_SESSION_ID（spike 实测），Stop-hook 同 env 链。
    """
    sid = os.environ.get("ORCA_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sid
    return _cac_session_id_from_pid()


def _host_session_from_tape(run_id: str) -> str | None:
    """读 runs/<run_id>.jsonl 首条 workflow_started.data.host_session（同 yaml_path 派生模式）。

    tape-only 真相源：marker 不存归属，nudge 需要时读 tape 首行派生（§2.3）。
    首行非 workflow_started / 读失败 / 缺 host_session → None（fail-safe，§2.5）。
    """
    try:
        with open(f"runs/{run_id}.jsonl", encoding="utf-8") as f:
            for line in f:                       # 首条即 workflow_started
                s = line.strip()
                if not s:
                    continue
                o = json.loads(s)
                if o.get("type") == "workflow_started":
                    return o.get("data", {}).get("host_session")
                break                            # 只看首条有效行
    except (OSError, json.JSONDecodeError):
        return None                              # fail-safe
    return None


def _read_throttle_timestamp(state: str) -> int:
    """读上次 block 的时间戳。文件不存在 / 损坏 / 不可读 → 0（视作可再次 block）。

    throttle 是 hook 本地 best-effort 态（非 orca 真相源），任何读取异常都不该阻断
    主流程——视作「无节流记录，可再次 block」。与 marker 路径的 fail loud 设计对称区别：
    marker 是 orca CLI 经 atomic_write_json 写出的真相源，损坏 = orca 状态已乱，必须报。
    """
    try:
        with open(state, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _scan_my_active_run_ids(current: str) -> list[str]:
    """扫 runs/orca-*.json 取**归属 current session**的活跃 run_id（SPEC §4.4）。

    marker 文件由 orca CLI 经 sidecar_io.atomic_write_json 写出，合法即合法 JSON。
    读失败 / JSON 非法 → **fail loud**（stderr + exit 2，详见脚本头注释 DEFECT-1 段）。
    marker 只记 run_id（无归属），故读 tape 首行 host_session 派生 + 过滤 == current。
    tape 读失败 → 跳过该 run（不误判；fail-safe）。
    """
    ids: list[str] = []
    for path in sorted(glob.glob("runs/orca-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(
                f"orca-nudge: marker {path} 不可读 / 非合法 JSON：{e}\n"
            )
            sys.exit(2)
        rid = data.get("run_id")
        if rid and _host_session_from_tape(str(rid)) == current:
            ids.append(str(rid))
    return ids


def main() -> int:
    now = int(time.time())
    current = _host_session_from_env()

    # 无 host session env：放行（不 block），但若有活跃 marker → warn（评审 C10）。
    # 区分「手 CLI 起 run（预期，无 env）」与「Stop-hook env 注入坏（bug，应有 CLAUDE_CODE_SESSION_ID）」。
    # warn 走 stderr（不污染 stdout 的 decision JSON）；不 fail（手 CLI 是合法用法）。
    if not current:
        if glob.glob("runs/orca-*.json"):
            sys.stderr.write(
                "orca-nudge: 无 host session env 但有活跃 marker"
                "（手动 CLI 起 run 或 env 注入异常）\n"
            )
        return 0

    # per-session 限流（§2.4）：STATE 按 session 分键，防 A 的 nudge 抑制 B。
    state = f"runs/.orca-nudge-cc-{current}"
    if now - _read_throttle_timestamp(state) < THROTTLE_SEC:
        return 0

    ids = _scan_my_active_run_ids(current)
    # 无归属本 session 的活跃 run → 放行（不 block）。
    if not ids:
        return 0

    # 记节流时间戳 + 发 block 提醒。
    os.makedirs("runs", exist_ok=True)
    with open(state, "w", encoding="utf-8") as f:
        f.write(str(now))

    reason = (
        f"你还有活跃的 Orca run：{', '.join(ids)}。"
        "若上一个节点的子代理已完成，请把它的产出作为 --output 调 "
        "orca next --run-id <run_id> --output '<产出>' 推进；"
        "若 workflow 已结束或要中止，先 orca stop <run_id>。"
        "（Orca nudge：提醒，Orca 不会自动推进。）"
    )
    # 输出 decision:block JSON（CC Stop hook 协议：block = force continuation）。
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
PYEOF
