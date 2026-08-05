#!/usr/bin/env bash
# Orca CC nudge —— Claude Code Stop + PostToolUse 双事件 hook（v5 §4.4 + SPEC
# posttooluse-rogue-guard.md）。单脚本双事件（DRY），按 stdin 的 hook_event_name 分支：
#
#   - Stop：原 v5 §4.4 行为字节级不变（decision:block + reason，60s 节流）。
#   - PostToolUse：SPEC posttooluse-rogue-guard §7 事后告警（pure hint）：主 session 在
#     活跃 run 期间自己用了「下场干活」类工具（Write/Edit/Bash 跑 train 等）→ stdout 输出
#     additionalContext（**不** emit decision，**不** exit 2）。绝不阻止动作（动作已发生），
#     绝不调 orca next（B 路径铁律不变）。
#
# 主 session 试图结束其 turn 时触发 Stop：若有活跃 Orca run（marker 存在）**且归属当前
# session** → 发 decision:block 注入「请调 orca next 推进」的提醒。**绝不调 orca next**
# （B 路径铁律：主 session 自调 next；hook 自动调 next = 退化 A 路径）。
#
# host-session-binding v2（tape-only，§2.3/§4.4）：
#   - current = ORCA_HOST_SESSION_ID ?? CLAUDE_CODE_SESSION_ID（CC 注入后者，零配置）。
#     PostToolUse env 链未实证（SPEC §10 R5）→ fallback 取 stdin JSON 的 session_id。
#   - glob marker 拿 run_id → 对每个 run 读 tape 首条 workflow_started.data.host_session
#     → 仅收 == current 的（读 tape 派生，marker 不存归属，单一真相源铁律）。
#   - per-session 限流：STATE 按 current 分键（防 A 的 nudge 抑制 B）。
#   - 无 current 但有活跃 marker → stderr warn（区分「手 CLI」与「env 注入 bug」，评审 C10）。
#
# 判定只看 marker 存在（runs/orca-<run_id>.json）——不用 tape 超时（会误报）。marker
# 在终态由 CLI 清掉，故「有 marker」≡「run 还活着」。
#
# 节流：Stop 60s（防 Stop 反复触发刷屏）；PostToolUse guard 30s（SPEC §4.3，与 Stop 60s
# 分键互不影响）。block/guard 后写时间戳，窗口内再次 Stop/PostToolUse 直接放行。
#
# 由 'tars install --target cc' 落到 <cc_root>/hooks/orca-nudge.sh 并在
# .claude/settings.json 的 hooks.Stop + hooks.PostToolUse 声明引用。同目录的
# tool-classification.json 是 §5 工具分类单一真相源（PostToolUse 分支启动时 read）。
#
# 实现：python3（DEFECT-1 修复；orca 本就依赖 python，跨环境可靠——WSL conda orca 等环境
# 不一定有 jq）。**fail loud**：marker 文件不可读 / 非合法 JSON → 写 stderr + exit 2，绝不
# 静默吞错（旧版用 jq 加 2>/dev/null 加 || true 在缺 jq 时静默失败 → nudge 永不触发且无报错，
# 违反 fail-loud；用户看不到任何信号）。节流态文件 / classification 缺失等 hook 本地 best-effort
# 态走 fail-open（与 marker 真相源 fail loud 对称区别）。
#
# 铁律：本脚本全篇**零反引号**——REASON 是双引号 bash 字符串，双引号内反引号 = 命令替换，
# 会误执行 orca next 退化 A 路径。命令名一律纯文本提及（提醒模型去调，非脚本执行）。
set -euo pipefail

# Resolve script dir（tool-classification.json 装在同目录）。BASH_SOURCE 经 bash -c / 直接
# 执行均可用；subprocess 'bash <path>' 形态也可（path 在 BASH_SOURCE[0]）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ORCA_NUDGE_DIR="$SCRIPT_DIR"

# Read hook stdin payload once（CC 给 Stop/PostToolUse 都 pipe JSON）。TTY 下跳过（手动跑 /
# 测试无 pipe stdin 时不阻塞 read），SPEC §10 R5 fallback 用此 payload 取 session_id。
# /dev/null stdin → [ ! -t 0 ] true（不是 TTY）→ cat 读到空 → 默认 Stop 分支（无字段）。
HOOK_STDIN_JSON=""
if [ ! -t 0 ]; then
    HOOK_STDIN_JSON="$(cat)" || HOOK_STDIN_JSON=""
fi
export ORCA_HOOK_STDIN="$HOOK_STDIN_JSON"

exec python3 - <<'PYEOF'
import glob
import json
import os
import sys
import time
from pathlib import Path

THROTTLE_SEC_NUDGE = 60   # Stop hook（v5 §4.4，不变）
THROTTLE_SEC_GUARD = 30   # PostToolUse guard（SPEC §4.3，与 Stop 分键互不影响）


def _cac_session_id_from_pid() -> str | None:
    """沿 PID 链向上找 CAC 主进程（cmdline 含 codeagentcli），
    从 ~/.cac/sessions/<cac_pid>.json 读 sessionId。
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


def _host_session_from_env() -> str | None:
    """当前宿主 session id（优先级 ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > CAC PID 回溯 > None）。

    与 orca/iface/in_session/cli.py 的 _host_session_from_env 同源（SPEC §4.2 公共 env 契约）。
    CC 给所有 bash 子进程注入 CLAUDE_CODE_SESSION_ID（Stop-hook spike 实证）；PostToolUse
    env 链未实证（SPEC §10 R5）→ caller fallback 取 stdin JSON.session_id。
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
    """读上次 block/guard 的时间戳。文件不存在 / 损坏 / 不可读 → 0（视作可再次触发）。

    throttle 是 hook 本地 best-effort 态（非 orca 真相源），任何读取异常都不该阻断
    主流程——视作「无节流记录，可再次触发」。与 marker 路径的 fail loud 设计对称区别：
    marker 是 orca CLI 经 atomic_write_json 写出的真相源，损坏 = orca 状态已乱，必须报。
    """
    try:
        with open(state, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _scan_my_active_run_ids(current: str, *, strict: bool = True) -> list[str]:
    """扫 runs/orca-*.json 取**归属 current session**的活跃 run_id（SPEC §4.4）。

    marker 文件由 orca CLI 经 sidecar_io.atomic_write_json 写出，合法即合法 JSON。
    marker 只记 run_id（无归属），故读 tape 首行 host_session 派生 + 过滤 == current。
    tape 读失败 → 跳过该 run（不误判；fail-safe）。

    损坏 marker 处理（两路径对称区别，review §四-1）：
    - strict=True（Stop 路径）：marker 真相源损坏 = orca 状态已乱，**fail loud**
      （stderr + exit 2，详见脚本头 DEFECT-1 段）。Stop 是 block 提醒路径，fail loud 符合 v5 §4.4。
    - strict=False（PostToolUse guard 路径）：单个 marker 损坏 → stderr warn + 跳过该 run
      （fail-open）。SPEC §7.3「**绝不 exit 2**」纯提示铁律优先于 marker fail-loud——guard 是
      best-effort 提示路径，不该让半残 marker 把 pure-hint 退化成 exit 2 打扰主 session。
    """
    ids: list[str] = []
    for path in sorted(glob.glob("runs/orca-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            msg = f"orca-nudge: marker {path} 不可读 / 非合法 JSON：{e}\n"
            if strict:
                sys.stderr.write(msg)
                sys.exit(2)
            sys.stderr.write(msg + "（PostToolUse guard 路径：跳过该 run，不 exit 2）\n")
            continue
        rid = data.get("run_id")
        if rid and _host_session_from_tape(str(rid)) == current:
            ids.append(str(rid))
    return ids


# ── PostToolUse 事后告警（SPEC posttooluse-rogue-guard §7）──────────────────────


def _load_classification():
    """读 tool-classification.json（单一真相源，SPEC §5）。

    缺失 / 损坏 / ORCA_NUDGE_DIR 未设 → stderr warn + 写 doctor 心跳 + None（fail-open：
    guard 分支跳过分类，不阻断用户；不影响 Stop 路径——Stop 不需要分类）。文件是 install
    期拷贝的部署产物（非常驻真相源），用户 install 出错时不应让用户 session 卡死，故选
    fail-open + warn；心跳给 doctor 一份可见信号（review 🟢#2）。
    """
    base = os.environ.get("ORCA_NUDGE_DIR")
    if not base:
        _write_heartbeat(
            "runs/.orca-guard-classification-missing.json",
            {"missing_at": int(time.time()), "reason": "no_nudge_dir"},
        )
        return None
    path = Path(base) / "tool-classification.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(
            f"orca-nudge: tool-classification.json 读失败 ({path}): {e}"
            "（PostToolUse guard 降级：不分类 = 不告警）\n"
        )
        _write_heartbeat(
            "runs/.orca-guard-classification-missing.json",
            {"missing_at": int(time.time()), "reason": "read_failed", "error": str(e)},
        )
        return None


def _is_writing_tool(tool_name: str, classification) -> bool:
    """tool_name 在 writing_tools 集 → True（下场干活）。"""
    if not classification:
        return False
    writing = classification.get("writing_tools") or []
    return tool_name in writing


def _is_bash_tool(tool_name: str, classification) -> bool:
    """tool_name 在 bash_tools 集 → True（需看命令）。"""
    if not classification:
        return False
    bash = classification.get("bash_tools") or []
    return tool_name in bash


def _bash_command_is_writing(cmd: str, classification) -> bool:
    """Bash/PowerShell 命令分类（SPEC §5 Bash 分类）。

    1. 复合命令（含任一分隔符）→ True（保守规则，符合 §10 R2）。
    2. 非复合：按 word-boundary 前缀匹配只读白名单 → 命中 False / 未命中 True。
       word-boundary：prefix 后须接 EOL（cmd == prefix）或空白（cmd startswith prefix+" "）；
       禁止 ls 命中 lsof / lsblk（E6 修订）；同时支持多词前缀（git log）。
    3. 空命令 / 异常 → True（保守：bash 工具却无命令 = 异常态，视为下场）。与 hook 整体
       fail-open 不对称——bash 工具调用本就该带命令，空命令更像协议错位/绕过尝试，保守归为
       下场（让守卫告警，模型可忽略）。SPEC §10 R2 fail-open 指分类白名单维护，不覆盖此空命令边界。
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return True
    seps = classification.get("compound_separators") or []
    if any(sep and sep in cmd for sep in seps):
        return True
    cmd_lower = cmd.strip().lower()
    for prefix in classification.get("readonly_bash_prefixes") or []:
        p = prefix.lower()
        if cmd_lower == p or cmd_lower.startswith(p + " "):
            return False
    return True


def _classify(tool_name: str, tool_input, classification) -> bool:
    """SPEC §5 总分类：返 True = 下场干活（告警）；False = 放行。"""
    if not classification:
        return False
    name = (tool_name or "").strip()
    if _is_writing_tool(name, classification):
        return True
    if not _is_bash_tool(name, classification):
        return False  # Read/Glob/Grep/Agent/Task/AskUserQuestion 等 → 放行
    cmd = ""
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command") or tool_input.get("args") or ""
    return _bash_command_is_writing(cmd, classification)


def _write_heartbeat(name: str, payload: dict) -> None:
    """写 hook 本地心跳（best-effort：失败忽略，不阻断主流程）。"""
    try:
        os.makedirs("runs", exist_ok=True)
        with open(name, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
    except OSError:
        pass


def _run_guard(payload: dict) -> int:
    """PostToolUse 事后告警分支（SPEC §7）。返 exit code。"""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # session 解析（SPEC §10 R5 fallback）：env 优先；取不到 → stdin JSON.session_id。
    current = _host_session_from_env()
    if not current:
        sess = payload.get("session_id")
        if isinstance(sess, str) and sess:
            current = sess
    if not current:
        # 两处都取不到 → fail-safe：写心跳 + 放行（不告警，不抛错）。
        _write_heartbeat(
            "runs/.orca-guard-unbound.json",
            {"unbound_at": int(time.time()), "tool": tool_name, "reason": "no_session"},
        )
        return 0

    classification = _load_classification()
    if not _classify(tool_name, tool_input, classification):
        return 0  # 工具不属下场类 → 静默

    # strict=False：marker 损坏时 fail-open（skip + warn），不 exit 2——SPEC §7.3 纯提示铁律。
    ids = _scan_my_active_run_ids(current, strict=False)
    if not ids:
        return 0  # 本 session 无活跃 run（或所有 marker 都坏）→ 静默

    now = int(time.time())
    state = f"runs/.orca-guard-cc-{current}"
    if now - _read_throttle_timestamp(state) < THROTTLE_SEC_GUARD:
        return 0  # 30s 窗内 → 静默

    run_id = ids[0]
    # reason 模板从 classification 单一真相源取（review 🟡#3 DRY），按 {run_id}/{tool} 占位符填充。
    # 模板缺失（classification 加载失败 / 无 guard_reason_template 字段）→ 内联兜底（保持两路径
    # 告警能力，不因模板字段缺失而哑）。{tool} 直接取 tool_name 原值（SPEC §6 注脚 E9）。
    template = (
        classification.get("guard_reason_template")
        if classification else None
    ) or (
        "【Orca 守卫·事后提醒】检测到你在活跃 run（{run_id}）期间自己用了 {tool}。"
        "编排期主 session 不该下场做节点工作——那是子代理的活。建议：改派 Task 子代理完成此步，"
        "或把已有产出作为 --output 调 orca next --run-id {run_id} 推进。"
        "本提醒不阻止（动作已执行）；若这是必要的调试/解锁操作，忽略即可。"
    )
    reason = template.replace("{run_id}", run_id).replace("{tool}", tool_name)

    # 纯提示：additionalContext（不改变控制流）。绝不 decision:block，绝不 exit 2（SPEC §7.3）。
    # print 先于节流时间戳写入（review 🟡#2）：注入失败（print 抛错）不计节流，下个工具调用可重试，
    # 与 orca.ts markNudged 在 promptAsync 成功后调的语义对称（SPEC §8.1 step 5）。
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        }
    }, ensure_ascii=False))

    # 注入成功后才记节流时间戳（raw epoch seconds，与 _read_throttle_timestamp / nudge 路径同款——
    # 保持 cc 家族无 .json 后缀 + raw int 现有约定）。best-effort 写，失败忽略。
    try:
        os.makedirs("runs", exist_ok=True)
        with open(state, "w", encoding="utf-8") as f:
            f.write(str(now))
    except OSError:
        pass
    return 0


def _run_stop() -> int:
    """Stop 分支（v5 §4.4，字节级不变）。"""
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
    if now - _read_throttle_timestamp(state) < THROTTLE_SEC_NUDGE:
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


def main() -> int:
    raw = os.environ.get("ORCA_HOOK_STDIN", "")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"orca-nudge: stdin 非合法 JSON：{e}\n")
        payload = {}
    event = payload.get("hook_event_name", "Stop")
    if event == "PostToolUse":
        return _run_guard(payload)
    return _run_stop()  # Stop / 未声明 event（含空 stdin 默认 Stop）


if __name__ == "__main__":
    sys.exit(main())
PYEOF
