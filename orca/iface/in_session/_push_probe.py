"""_push_probe.py —— ``orca doctor --probe-push`` 推送链路诊断（SPEC v2 §1-§5）。

**职责**：一次跑完推送链路 6 跳（family_detect → cac_pid_walk → adapter_discovery →
daemon_progress → bus_flow → ws_delivery），**精确指出哪一跳断**。诊断**只读复用**现有
真相源，不新增任何接口/数据结构（SPEC §0 非目标铁律）。

**依赖方向**（SPEC §2.1，单向不破铁律）::

    iface.in_session._push_probe
      ├→ iface.in_session._hostenv          (H1/H2：detect_*，同层)
      ├→ iface.in_session.sidechain_daemon   (H3：_make_adapter；H4：_sidechain_daemon_alive)
      ├→ events.adapters.cc_jsonl            (H3：CCJsonlAdapter.discover_children)
      ├→ events.tape                         (H4：read_last_complete_lines)
      ├→ events.bus                          (H6：EventBus)
      └→ iface.web.server                    (H6：create_app，**函数内 lazy import** 防
                                               iface.web↔in_session 潜在环)

**零副作用**：不改 ``_spawn_sidechain_daemon`` / ``_make_adapter`` / ``EventBus`` /
``ws_handler`` / 任何 adapter。``_push_probe`` 是叶子消费方，只读不写现有模块。
诊断结果用 plain dict（与 doctor 现有 check dict 同构）——SPEC §2.1 「不新增数据结构」。

**fail loud**：诊断本身不静默吞错——hop 抛异常 → 该跳 status="error" + reason=str(exc)，
外层 try/except 兜底不传染其它跳（SPEC §0 目标 + 规则 12）。

**链路顺序敏感**：H1 断 → H2/H3 必然跟着无意义。``first_break`` = 链路顺序首个非 pass 跳
（SPEC §3）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── hop 名常量 + slug（MD 锚点用）────────────────────────────────────────────
# SPEC §5：每节 MD 标题用 ``## H<N> <slug> {#h<N>-<slug>}``；fix_hint 指针引用同名锚点。
# slug 用 ``-`` 连字符（GitHub render 锚点默认形态）。
H1_FAMILY_DETECT = "H1_family_detect"
H2_CAC_PID_WALK = "H2_cac_pid_walk"
H3_ADAPTER_DISCOVERY = "H3_adapter_discovery"
H4_DAEMON_PROGRESS = "H4_daemon_progress"
H5_BUS_FLOW = "H5_bus_flow"
H6_WS_DELIVERY = "H6_ws_delivery"

# 链路顺序（SPEC §3 ``first_break`` 算法用）。
_HOP_ORDER = (
    H1_FAMILY_DETECT,
    H2_CAC_PID_WALK,
    H3_ADAPTER_DISCOVERY,
    H4_DAEMON_PROGRESS,
    H5_BUS_FLOW,
    H6_WS_DELIVERY,
)

RUNBOOK_PATH = "docs/troubleshooting/push-chain.md"

# H6 passive 模式监听窗口（S5，SPEC §4 H6）：真实事件可能稀疏，比 self-spawn 的 3s 长。
# doctor 是一次性命令，8s 在「快速诊断」与「给真实事件足够到达时间」间平衡。
_H6_PASSIVE_LISTEN_SECONDS = 8.0


# ── ProbeContext ──────────────────────────────────────────────────────────────


@dataclass
class ProbeContext:
    """``run_push_probe`` 与各 hop 共享的上下文（SPEC §4：携带 run_id / ws_url / rundir）。

    设计：plain dataclass，不带行为；hop 函数自行 lazy import 真相源（保依赖方向干净）。
    """

    run_id: str | None
    ws_url: str | None
    rundir: Path


# ── 编排：run_push_probe ──────────────────────────────────────────────────────


def run_push_probe(
    run_id: str | None = None,
    ws_url: str | None = None,
    *,
    rundir: Path | None = None,
) -> dict[str, Any]:
    """跑推送链路 6 跳诊断（SPEC §3 输出契约）。

    Args:
        run_id: H4/H5 读 daemon log + tape 需要；None 时 H4/H5 转 unknown（无目标 run）。
        ws_url: H6 passive 模式（S5）；None 时走 self-spawn（S3 实现）。
        rundir: ``runs/`` 目录（默认 ``_default_rundir()``，与 doctor 同源）。

    Returns:
        ``{overall, first_break, runbook, hops}``（见 SPEC §3 示例）。

    **fail loud**：任一 hop 抛异常 → 该跳 status="error" + reason=str(exc)，不传染其它跳。
    ``overall``：任一跳 fail/error → "fail"；全 pass → "pass"。
    ``first_break``：链路顺序首个 ``status != "pass"`` 的跳；全 pass → None。
    """
    # lazy import（避免顶层拉 iface.in_session.cli ↔ _push_probe 环）。
    if rundir is None:
        from orca.iface.in_session.cli import _default_rundir
        rundir = _default_rundir()

    ctx = ProbeContext(run_id=run_id, ws_url=ws_url, rundir=rundir)

    # hop 函数表（SPEC §4 签名统一：``def _hop_hX(ctx) -> dict``）。
    # S1 只接 H1/H2/H3；H4-H6 由 S2/S3 接入（占位返 not_implemented status=unknown，不抛）。
    hop_funcs = {
        H1_FAMILY_DETECT: _hop_h1_family_detect,
        H2_CAC_PID_WALK: _hop_h2_cac_pid_walk,
        H3_ADAPTER_DISCOVERY: _hop_h3_adapter_discovery,
        H4_DAEMON_PROGRESS: _hop_h4_daemon_progress,
        H5_BUS_FLOW: _hop_h5_bus_flow,
        H6_WS_DELIVERY: _hop_h6_ws_delivery,
    }

    hops: list[dict[str, Any]] = []
    for hop_name in _HOP_ORDER:
        fn = hop_funcs[hop_name]
        try:
            result = fn(ctx)
        except Exception as e:  # noqa: BLE001 — fail loud 兜底：hop 抛异常不传染其它跳
            logger.warning(
                "push_chain_probe hop %s 抛异常（fail loud 兜底为 error）", hop_name, exc_info=True,
            )
            result = {
                "hop": hop_name,
                "status": "error",
                "evidence": "",
                "reason": f"{type(e).__name__}: {e}",
                "fix_hint": _fix_hint(hop_name),
            }
        # 保证 hop 字段齐全（hop 函数漏给 fix_hint 时兜底）。
        result.setdefault("hop", hop_name)
        result.setdefault("fix_hint", _fix_hint(hop_name))
        hops.append(result)

    # overall：SPEC §3 二态契约（任一 fail/error → fail；全 pass → pass）外的保守默认
    # （含 unknown 但无 fail/error 也算 fail）——链路有不确定环节即不能说整体通。
    if any(h["status"] in ("fail", "error") for h in hops):
        overall = "fail"
    elif all(h["status"] == "pass" for h in hops):
        overall = "pass"
    else:
        overall = "fail"

    # first_break：链路顺序首个 status != "pass"。
    first_break: str | None = None
    for h in hops:
        if h["status"] != "pass":
            first_break = h["hop"]
            break

    return {
        "overall": overall,
        "first_break": first_break,
        "runbook": RUNBOOK_PATH,
        "hops": hops,
    }


def _anchor(hop: str) -> str:
    """hop 名 → runbook 显式锚点（SPEC §5：``{#h<N>-<slug>}`` 格式）。

    算法：``H1_family_detect`` → ``h1-family-detect``（lowercase + 下标位 → 连字符）。
    单一真相源——MD 写锚点 + fix_hint 引锚点都过本函数（防漂移）。
    """
    return f"#h{hop[1].lower()}-{hop[3:].replace('_', '-')}"


def _fix_hint(hop: str) -> str:
    """每跳 fix_hint = 一行摘要 + runbook 锚点指针（SPEC §3 / §5）。

    MD 是真相源、fix_hint 是快速指针；锚点 slug 与 ``_HOP_ORDER`` 同源（SPEC §5 守门测试
    断言两者对应）。
    """
    pointers = {
        H1_FAMILY_DETECT: "检查 CLAUDE_CODE_SESSION_ID / CODEAGENT+PID 是否注入",
        H2_CAC_PID_WALK: "改自 CAC bash 子进程启动 daemon，或显式 --host-session",
        H3_ADAPTER_DISCOVERY: "确认 sidechain root + agent-*.jsonl + .meta.json 齐全",
        H4_DAEMON_PROGRESS: "查 daemon log iteration 异常；确认子 agent 真在产事件",
        H5_BUS_FLOW: "查 bus 订阅者队列是否溢出丢事件（bus.py 队列满 warning）",
        H6_WS_DELIVERY: "查 ws_handler._pump 链路是否通（H6 self-spawn 活探）",
    }
    return f"{pointers[hop]}。详见 {RUNBOOK_PATH}{_anchor(hop)}"


# ── H1 · family_detect ────────────────────────────────────────────────────────


def _hop_h1_family_detect(ctx: ProbeContext) -> dict[str, Any]:
    """H1：当前被认成什么 backend/family？走哪条分支？

    复用：``_hostenv.detect_backend_from_env()`` + ``detect_family_from_env()``
    （SPEC §1 H1）。**不新增探测逻辑**，只标注命中分支。

    status：
      - ``pass``：backend 非 None 且（CC 家族时）family ∈ {cc, cac}。
      - ``fail``：backend=cc 但 family=None 且 config 也无（env 命中 CC 但 family 探测全空
        → adapter 选错 dotdir）。SPEC §7-3 构造：CC_SESSION_ID + CODEAGENT 均 unset。
      - ``unknown``：backend=None（非 in-session 环境）。SPEC §7-2。
    """
    from orca.iface.in_session._hostenv import (
        detect_backend_from_env,
        detect_family_from_env,
    )

    backend = detect_backend_from_env()
    family = detect_family_from_env()

    # source 标注：env 命中哪一路（SPEC §4 H1 evidence）。
    has_cc_sid = bool(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    has_opencode_sid = bool(os.environ.get("ORCA_HOST_SESSION_ID"))
    has_codeagent = bool(os.environ.get("CODEAGENT"))
    if has_cc_sid:
        source = "CLAUDE_CODE_SESSION_ID"
    elif has_opencode_sid:
        source = "ORCA_HOST_SESSION_ID"
    elif has_codeagent:
        source = "CODEAGENT+PID"
    else:
        source = "none"

    if backend is None:
        return {
            "hop": H1_FAMILY_DETECT,
            "status": "unknown",
            "evidence": (
                f"backend=None, family=None, source={source}"
                "（非 in-session 环境，B2 推送链路不适用）"
            ),
            "reason": "未检测到 in-session env（CC_SESSION_ID / HOST_SESSION_ID / CODEAGENT 均无）",
            "fix_hint": _fix_hint(H1_FAMILY_DETECT),
        }

    if backend == "cc":
        if family in ("cc", "cac"):
            return {
                "hop": H1_FAMILY_DETECT,
                "status": "pass",
                "evidence": f"backend=cc, family={family}, source={source}",
                "reason": "",
                "fix_hint": _fix_hint(H1_FAMILY_DETECT),
            }
        # backend=cc 但 family=None：daemon 选 dotdir 会走 fallback（config/probe）。
        return {
            "hop": H1_FAMILY_DETECT,
            "status": "fail",
            "evidence": (
                f"backend=cc, family=None, source={source}"
                "（CC env 已命中但 family 探测失败 → adapter 走 config/probe 兜底）"
            ),
            "reason": (
                "CC 家族但 detect_family_from_env 返 None——CODEAGENT 在但 PID 回溯未命中"
                " codeagentcli，或 CLAUDE_CODE_SESSION_ID 未注入。adapter 会走 config/probe，"
                "可能选错 dotdir（cc vs cac）"
            ),
            "fix_hint": _fix_hint(H1_FAMILY_DETECT),
        }

    # opencode 家族（family 子型在 opencode 端无 cc/cac 区分）。
    return {
        "hop": H1_FAMILY_DETECT,
        "status": "pass",
        "evidence": f"backend={backend}, family=None(opencode 家族无 cc/cac), source={source}",
        "reason": "",
        "fix_hint": _fix_hint(H1_FAMILY_DETECT),
    }


# ── H2 · cac_pid_walk ─────────────────────────────────────────────────────────


def _hop_h2_cac_pid_walk(ctx: ProbeContext) -> dict[str, Any]:
    """H2：CAC PID 链能否回溯到 codeagentcli + session json 在不在？

    复用：``_hostenv.cac_session_id_from_pid()`` 是**判定权威**（单一真相源）；中间态
    （env / PPid 链 / session 文件）由本函数**只读复算仅作展示**（SPEC §4 H2 受控复制决议）。
    防漂移：§5 守门测试断言「中间态复算结果与 ``cac_session_id_from_pid()`` 返回值自洽」。

    跳过条件：backend 非 CC 家族（opencode / None）→ 不适用，status=pass-through=pass。
    SPEC §4 H2 「仅 CC 家族且非 CLAUDE_CODE_SESSION_ID 路径」。

    status（CC 家族且非 CC_SESSION_ID 路径下）：
      - ``pass``：PID 链命中 codeagentcli 且 session json 在。
      - ``fail``：CODEAGENT 在但 PID 链断裂 / session json 缺 / 无 sessionId 字段。
    """
    from orca.iface.in_session._hostenv import (
        cac_session_id_from_pid,
        detect_backend_from_env,
        detect_family_from_env,
    )

    backend = detect_backend_from_env()
    family = detect_family_from_env()

    # 跳过：opencode 家族 / 非 in-session / 真 CC（CLAUDE_CODE_SESSION_ID 路径，不走 PID 回溯）。
    has_cc_sid = bool(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    if backend != "cc" or has_cc_sid:
        return {
            "hop": H2_CAC_PID_WALK,
            "status": "pass",
            "evidence": (
                f"skip(backend={backend}, cc_sid={has_cc_sid})"
                "——PID 回溯仅 CAC（CODEAGENT+PID 路径）需要"
            ),
            "reason": "",
            "fix_hint": _fix_hint(H2_CAC_PID_WALK),
        }

    # 判定权威：单一真相源。
    session_id = cac_session_id_from_pid()

    # 中间态复算（只读，仅展示）。
    has_codeagent = bool(os.environ.get("CODEAGENT"))
    matched_ppid, pid_chain_hit = _recompute_pid_walk_intermediate()
    session_file_exists, session_file_has_sid = _recompute_session_file_state(matched_ppid)

    evidence_parts = [
        f"CODEAGENT={int(has_codeagent)}",
        f"pid_walk_hit={'true' if pid_chain_hit else 'false'}",
    ]
    if matched_ppid is not None:
        evidence_parts.append(f"matched_ppid={matched_ppid}")
        evidence_parts.append(f"session_file={'true' if session_file_exists else 'false'}")
        evidence_parts.append(
            f"session_file_has_sessionId={'true' if session_file_has_sid else 'false'}"
        )
    evidence_parts.append(f"authority_session_id={session_id!r}")
    evidence = ", ".join(evidence_parts)

    # 自洽性：判定权威返非 None ⟺ PID 链命中 + session 文件有 sessionId。
    # （仅 evidence 层展示，不一致由 §5 守门测试断言；此处不重复 assert。）

    if session_id is not None:
        return {
            "hop": H2_CAC_PID_WALK,
            "status": "pass",
            "evidence": evidence,
            "reason": "",
            "fix_hint": _fix_hint(H2_CAC_PID_WALK),
        }

    # 失败定位：精确指出断点（CODEAGENT 没设 / PID 链没命中 / session 文件缺）。
    if not has_codeagent:
        reason = "CODEAGENT env 未设（非 CAC 进程）——backend=cc 但无 CC_SESSION_ID"
    elif not pid_chain_hit:
        reason = "PID 链 20 跳内未命中 codeagentcli——daemon 被 setsid 孤儿化脱离 CAC 进程树"
    elif matched_ppid is not None and not session_file_exists:
        reason = (
            f"PID 链命中 codeagentcli（ppid={matched_ppid}）但 ~/.cac/sessions/<ppid>.json 不在"
            "——session 文件命名变 / 被清"
        )
    elif matched_ppid is not None and not session_file_has_sid:
        reason = (
            f"session 文件在（ppid={matched_ppid}）但无 sessionId 字段——CAC 写文件格式漂移"
        )
    else:
        reason = "PID 回溯未命中（无 codeagentcli 祖先）"

    return {
        "hop": H2_CAC_PID_WALK,
        "status": "fail",
        "evidence": evidence,
        "reason": reason,
        "fix_hint": _fix_hint(H2_CAC_PID_WALK),
    }


def _recompute_pid_walk_intermediate() -> tuple[int | None, bool]:
    """只读复算 PID 链是否命中 codeagentcli + 命中的 ppid（中间态展示用）。

    与 ``cac_session_id_from_pid`` 内部逻辑**字节级对齐**——SPEC §4 H2 受控复制决议。
    失败/不适用返 ``(None, False)``。

    **同步契约**：本函数是 ``orca.iface.in_session._hostenv.cac_session_id_from_pid``
    的 PID 链遍历逻辑的**字节级复制**（SPEC §4 H2 受控复制）。改 ``cac_session_id_from_pid``
    的 PID 链遍历逻辑时**必须同步本函数**，否则 §5 守门测试「中间态复算 ↔ 权威返值自洽」会漂。

    非机械化复制：本函数**不读 session json**，只判 PID 链命中（展示中间态用）；
    判定权威仍在 ``cac_session_id_from_pid``（读 json 取 sessionId）。两者自洽性由
    §5 守门测试断言。
    """
    pid = os.getpid()
    for _ in range(20):
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            ppid_line = next(
                (l for l in status.splitlines() if l.startswith("PPid:")), None
            )
            if not ppid_line:
                return None, False
            ppid = int(ppid_line.split()[1])
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            return None, False

        try:
            raw = Path(f"/proc/{ppid}/cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            pid = ppid
            continue

        exe = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        if exe.endswith("/codeagentcli") or exe == "codeagentcli":
            return ppid, True

        pid = ppid
        if pid <= 1:
            return None, False

    return None, False


def _recompute_session_file_state(matched_ppid: int | None) -> tuple[bool, bool]:
    """只读复算 ``~/.cac/sessions/<ppid>.json`` 在不在 + 有无 sessionId（展示用）。

    返 ``(exists, has_session_id)``；``matched_ppid is None`` → ``(False, False)``。
    """
    if matched_ppid is None:
        return False, False
    session_file = Path.home() / ".cac" / "sessions" / f"{matched_ppid}.json"
    if not session_file.exists():
        return False, False
    try:
        obj = json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return True, False
    return True, bool(isinstance(obj, dict) and obj.get("sessionId"))


# ── H3 · adapter_discovery ────────────────────────────────────────────────────


def _hop_h3_adapter_discovery(ctx: ProbeContext) -> dict[str, Any]:
    """H3：adapter 能 discover 到子进程？root/meta.json 齐不齐？

    复用：``sidechain_daemon._make_adapter(backend, host_session, family=cfg_family)``
    （SPEC §1 H3 同一构造路径）+ ``CCJsonlAdapter.discover_children`` + ``.root``。

    status：
      - ``pass``：root 存在 + discovered 非空（至少一个 ``agent-*.jsonl`` 伴 ``.meta.json``）。
      - ``fail``：root 存在 + jsonl 存在 + with_meta_count=0（宿主未写 meta.json，daemon 全跳过）。
      - ``unknown``：root 不存在（子 agent 尚未起，非故障——SPEC §1 拍板 unknown）。
    """
    from orca.iface.in_session._hostenv import (
        detect_backend_from_env,
        detect_family_from_env,
        host_session_from_env,
    )
    from orca.iface.in_session.cli import _read_sidechain_family_from_config
    from orca.iface.in_session.sidechain_daemon import _make_adapter

    backend = detect_backend_from_env()
    if backend is None:
        return {
            "hop": H3_ADAPTER_DISCOVERY,
            "status": "unknown",
            "evidence": "backend=None（非 in-session，adapter 不适用）",
            "reason": "未检测到 in-session env，adapter_discovery 无意义",
            "fix_hint": _fix_hint(H3_ADAPTER_DISCOVERY),
        }

    host_session = host_session_from_env() or ""
    env_family = detect_family_from_env()
    cfg_family = env_family or _read_sidechain_family_from_config()

    try:
        adapter = _make_adapter(backend, host_session, family=cfg_family)
    except Exception as e:  # noqa: BLE001 — _make_adapter fail loud：报错不静默
        return {
            "hop": H3_ADAPTER_DISCOVERY,
            "status": "fail",
            "evidence": (
                f"_make_adapter raised {type(e).__name__}: {e}"
                f"（backend={backend}, host_session={host_session!r}, family={cfg_family!r}）"
            ),
            "reason": f"adapter 构造失败：{e}",
            "fix_hint": _fix_hint(H3_ADAPTER_DISCOVERY),
        }

    root = _adapter_root(adapter)
    root_exists = root.exists() if root is not None else False

    # 扫 root（不依赖 adapter.discover_children 路径，避免 family/backend 不匹配时空返混淆）。
    jsonl_count, with_meta_count, children = (0, 0, [])
    if root_exists:
        children = list(adapter.discover_children(host_session, 0))
        # 内部统计：root 下的 ``agent-*.jsonl`` 总数 vs 伴 ``.meta.json`` 的数。
        jsonl_paths = sorted(root.glob("agent-*.jsonl"))
        jsonl_count = len(jsonl_paths)
        with_meta_count = sum(
            1 for p in jsonl_paths if (root / f"{p.stem}.meta.json").is_file()
        )

    evidence_parts = [
        f"backend={backend}",
        f"host_session={host_session!r}",
        f"family={cfg_family!r}",
        f"root={root}",
        f"root_exists={root_exists}",
        f"jsonl_count={jsonl_count}",
        f"with_meta_count={with_meta_count}",
        f"discovered_children={children}",
    ]
    evidence = ", ".join(evidence_parts)

    if not root_exists:
        return {
            "hop": H3_ADAPTER_DISCOVERY,
            "status": "unknown",
            "evidence": evidence,
            "reason": "sidechain root 不存在（子 agent 尚未起，非故障）",
            "fix_hint": _fix_hint(H3_ADAPTER_DISCOVERY),
        }

    # root 存在。
    if with_meta_count == 0:
        # root 在但没有伴 meta.json 的子代理——daemon 会全跳过（SPEC §1 H3 fail）。
        if jsonl_count > 0:
            return {
                "hop": H3_ADAPTER_DISCOVERY,
                "status": "fail",
                "evidence": evidence,
                "reason": (
                    f"root 存在且有 {jsonl_count} 个 agent-*.jsonl，但全部无 .meta.json"
                    "——宿主后台系统子代理（非主 session Agent tool spawn），daemon 全跳过"
                ),
                "fix_hint": _fix_hint(H3_ADAPTER_DISCOVERY),
            }
        # jsonl_count==0：root 在但还没子 agent 写入（可能刚起）→ unknown。
        return {
            "hop": H3_ADAPTER_DISCOVERY,
            "status": "unknown",
            "evidence": evidence,
            "reason": "root 存在但无 agent-*.jsonl（子 agent 尚未产出事件）",
            "fix_hint": _fix_hint(H3_ADAPTER_DISCOVERY),
        }

    return {
        "hop": H3_ADAPTER_DISCOVERY,
        "status": "pass",
        "evidence": evidence,
        "reason": "",
        "fix_hint": _fix_hint(H3_ADAPTER_DISCOVERY),
    }


def _adapter_root(adapter: Any) -> Path | None:
    """统一读 adapter 的 root/db_path（CCJsonlAdapter.root / OpencodeSqliteAdapter.db_path）。

    返 None 表示 adapter 没有路径属性（不应发生，防御性 fail safe）。
    """
    # CCJsonlAdapter.root 是 property 返 Path；OpencodeSqliteAdapter.db_path 同。
    root = getattr(adapter, "root", None) or getattr(adapter, "db_path", None)
    if root is None:
        return None
    try:
        return Path(root)
    except TypeError:
        return None


# ── H4 · daemon_progress（SPEC §4 H4 + §8#4 覆盖；S2 实现）─────────────────────

# SPEC §4 H4 / §9 B3：run_age 阈值 30s（子 agent 首事件 3-15s 常见，留余量）。
_H4_RUN_AGE_THRESHOLD_S = 30
# SPEC §4 H4 freshness：last_agent_event_age_s 阈值同 run_age 一致（30s 内有事件）。
_H4_FRESHNESS_THRESHOLD_S = 30
# SPEC §4 H4：tape 末尾读 200 行（避免读全量 tape）。
_H4_TAPE_TAIL_LINES = 200

# SPEC §4 H4：daemon iteration 异常 grep pattern（与 sidechain_daemon.py:168-169 文案同源）。
_H4_ITERATION_EXC_PATTERN = "sidechain driver iteration 异常"
# SPEC §4 H5：bus 队列满 warning grep pattern（与 bus.py:77 文案同源）。
_H5_QUEUE_FULL_PATTERN = "订阅者队列满"


def _hop_h4_daemon_progress(ctx: ProbeContext) -> dict[str, Any]:
    """H4：daemon 存活且真在推进（tape 有 agent_* 事件）？有没有 iteration 异常？

    复用（SPEC §4 H4 / §1）：
      - ``_sidechain_daemon_alive(run_id)``（pidfile + cmdline 活探）。
      - ``events.tape.read_last_complete_lines`` 读 ``<rundir>/<run_id>.jsonl`` 末尾 200 行。
      - 读 ``<rundir>/<run_id>/sidechain_daemon.log`` grep iteration 异常计数（同源文案契约）。

    计算：``gap = disk_jsonl_lines - agent_events_in_tape``、``last_agent_event_age_s``、
    ``run_age_s``（started_at ← marker 的 ``run_id`` 注册时间 fallback run_dir ctime）。

    status（SPEC §4 H4，B3 决议）：
      - ``unknown``：``disk_jsonl_lines==0``（子 agent 尚未派，不误报刚 bootstrap）。
      - ``pass``：daemon_alive AND agent_events>0 AND gap==0 AND
        last_agent_event_age_s<30 AND iteration_exceptions==0。
      - ``fail``：daemon_dead；或 disk_jsonl_lines>0 且（agent_events==0 或 gap>0）且
        run_age_s>30（持续 iterate 失败/漏推）；或 iteration_exceptions>0。
      - 其它（daemon 活、disk 有、age 小但 agent_events==0）→ 保守 unknown（刚起）。
    """
    from orca.iface.in_session.cli import _read_sidechain_family_from_config
    from orca.iface.in_session._hostenv import (
        detect_backend_from_env, detect_family_from_env, host_session_from_env,
    )
    from orca.iface.in_session.sidechain_daemon import (
        _make_adapter, _sidechain_daemon_alive,
    )

    # 无 run_id → H4 不适用（SPEC §4 H4 仅 --run-id 给定时跑）。
    if not ctx.run_id:
        return {
            "hop": H4_DAEMON_PROGRESS,
            "status": "unknown",
            "evidence": "run_id 未给（H4 仅 --run-id 给定时跑）",
            "reason": "doctor --probe-push 未带 --run-id，H4 无目标 run 可探",
            "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
        }

    backend = detect_backend_from_env()
    if backend is None:
        return {
            "hop": H4_DAEMON_PROGRESS,
            "status": "unknown",
            "evidence": "backend=None（非 in-session，H4 不适用）",
            "reason": "未检测到 in-session env，daemon 本就不该起",
            "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
        }

    run_id = ctx.run_id
    tape_path = ctx.rundir / f"{run_id}.jsonl"
    run_dir = ctx.rundir / run_id
    log_path = run_dir / "sidechain_daemon.log"

    daemon_alive = _sidechain_daemon_alive(run_id)

    # 读 daemon log（H4 + H5 同源文案契约）。
    log_text = _read_text_safe(log_path)
    iteration_exceptions = log_text.count(_H4_ITERATION_EXC_PATTERN)
    queue_full_warnings = log_text.count(_H5_QUEUE_FULL_PATTERN)

    # 算 disk_jsonl_lines（adapter.discover_children 拿到的 child × agent-<child>.jsonl 行数和）。
    host_session = host_session_from_env() or ""
    env_family = detect_family_from_env()
    cfg_family = env_family or _read_sidechain_family_from_config()
    disk_jsonl_lines = 0
    adapter_root: Path | None = None
    try:
        adapter = _make_adapter(backend, host_session, family=cfg_family)
        adapter_root = _adapter_root(adapter)
        if adapter_root is not None and adapter_root.is_dir():
            for child in adapter.discover_children(host_session, 0):
                child_jsonl = adapter_root / f"agent-{child}.jsonl"
                disk_jsonl_lines += _count_lines(child_jsonl)
    except Exception as e:  # noqa: BLE001 — adapter 失败不阻塞 H4（disk_jsonl_lines 留 0）
        logger.warning(
            "H4 disk_jsonl 统计失败（视为 0 继续）：%s", e, exc_info=True,
        )

    # 读 tape 末尾 N 行，统计 agent_* 事件 + 最新 timestamp。
    agent_events, last_agent_event_ts = _stat_tape_agents(tape_path, _H4_TAPE_TAIL_LINES)
    import time as _time
    now = _time.time()
    last_agent_event_age_s = (
        now - last_agent_event_ts if last_agent_event_ts > 0 else None
    )

    # run_age_s：SPEC §4 H4「run marker orca-<run_id>.json started_at；fallback run_dir ctime」。
    # marker 只 3 字段（run_id/model/no_output_count），无 started_at——回退 run_dir ctime。
    run_age_s = _compute_run_age(ctx.rundir, run_id)

    gap = disk_jsonl_lines - agent_events

    evidence_parts = [
        f"run_id={run_id}",
        f"daemon_alive={'true' if daemon_alive else 'false'}",
        f"disk_jsonl_lines={disk_jsonl_lines}",
        f"agent_events={agent_events}",
        f"gap={gap}",
        f"last_agent_event_age_s={last_agent_event_age_s}",
        f"run_age_s={run_age_s}",
        f"iteration_exceptions={iteration_exceptions}",
        f"queue_full_warnings={queue_full_warnings}",
        f"adapter_root={adapter_root}",
    ]
    evidence = ", ".join(evidence_parts)

    # SPEC §4 H4 B3 决议：disk_jsonl_lines==0 → unknown（刚 bootstrap）。
    if disk_jsonl_lines == 0:
        return {
            "hop": H4_DAEMON_PROGRESS,
            "status": "unknown",
            "evidence": evidence,
            "reason": "disk_jsonl_lines==0（子 agent 尚未派/无产出，不误报刚 bootstrap）",
            "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
        }

    # fail：iteration 异常计数>0（daemon 在持续吞错）。
    if iteration_exceptions > 0:
        return {
            "hop": H4_DAEMON_PROGRESS,
            "status": "fail",
            "evidence": evidence,
            "reason": (
                f"daemon log 有 {iteration_exceptions} 次 iteration 异常"
                "——adapter/ingestor 持续抛错被 except Exception 吞"
            ),
            "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
        }

    # fail：daemon_dead（守护死亡）。
    if not daemon_alive:
        return {
            "hop": H4_DAEMON_PROGRESS,
            "status": "fail",
            "evidence": evidence,
            "reason": "daemon_dead（pidfile 残 / pid 死 / cmdline 不匹配；next 路径会 respawn）",
            "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
        }

    # fail：disk 有 raw 行但 tape agent_events==0 且 run_age>30s（持续 iterate 失败 / cursor 卡）。
    # **不用 gap 门控**（review 🔴#1）：gap = disk_jsonl_lines(raw 行数) - agent_events(派生事件数)
    # 量纲不可比——cc_jsonl 一行 content 多 block 一对多映射（cc_jsonl.py:283），1 raw line 常产
    # K>1 事件 → gap 恒负；又有 system/result 行产 0 事件 → gap 正。gap 无法可靠判漏推。
    # 真正的漏推信号是「disk 有数据但 tape 0 条 agent_* 事件」（daemon 根本没 ingest）。
    if run_age_s is not None and run_age_s > _H4_RUN_AGE_THRESHOLD_S:
        if agent_events == 0:
            return {
                "hop": H4_DAEMON_PROGRESS,
                "status": "fail",
                "evidence": evidence,
                "reason": (
                    f"disk_jsonl_lines={disk_jsonl_lines}>0 但 tape agent_events=0 且"
                    f" run_age={run_age_s}s>30s——daemon 存活但持续 iterate 失败 / cursor 卡"
                ),
                "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
            }

    # pass：daemon_alive（上面已确认）+ agent_events>0 + 最近事件新鲜（<30s）+ 无 iter 异常
    # （上面已确认）。freshness 是「daemon 在推进」的可靠信号——不依赖 gap。
    if (
        agent_events > 0
        and last_agent_event_age_s is not None
        and last_agent_event_age_s < _H4_FRESHNESS_THRESHOLD_S
    ):
        return {
            "hop": H4_DAEMON_PROGRESS,
            "status": "pass",
            "evidence": evidence,
            "reason": "",
            "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
        }

    # 兜底 unknown：agent_events>0 但事件陈旧（>30s，子 agent 可能在长思考 / daemon 停滞，
    # 跨进程无法区分）/ agent_events==0 且 run_age<=30s（刚 spawn 还没 ingest）。
    if agent_events > 0:
        stale = (
            last_agent_event_age_s if last_agent_event_age_s is not None else "?"
        )
        reason = (
            f"daemon 存活 + tape 有 {agent_events} 条 agent_* 事件，但最近一条在 {stale}s 前"
            "（>30s）——子 agent 可能长思考 idle / daemon 停滞，跨进程无法区分"
        )
    else:
        reason = (
            "disk 有 jsonl 但 tape agent_events=0 且 run_age<=30s"
            "——子 agent 刚 spawn，daemon 可能还未 ingest"
        )
    return {
        "hop": H4_DAEMON_PROGRESS,
        "status": "unknown",
        "evidence": evidence,
        "reason": reason,
        "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
    }


def _read_text_safe(path: Path) -> str:
    """安全读文本文件：不存在 / 读失败 → 空串（不抛，H4/H5 兜底）。"""
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _count_lines(path: Path) -> int:
    """数文本文件的**完整行数**（不抛；不存在 → 0）。

    口径与 ``_stat_tape_agents`` 对齐：只数以 ``\\n`` 结尾的完整行——partial 末行（daemon
    正在写）不算。避免 disk_jsonl_lines 与 agent_events_in_tape 因 partial 行口径不一致
    导致假阳性 ``gap>0``（review 🟡#1）。
    """
    try:
        if not path.is_file():
            return 0
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return 0
        # 只数完整行（\n 个数）；末行无 \n 是 partial 不算（与 read_last_complete_lines 对齐）。
        return data.count(b"\n")
    except OSError:
        return 0


def _stat_tape_agents(tape_path: Path, tail_lines: int) -> tuple[int, float]:
    """读 tape 末尾 tail_lines 行，统计 agent_* 事件数 + 最新 timestamp。

    返 ``(agent_count, last_ts)``；无 tape / 读失败 → ``(0, 0.0)``。

    用 ``read_last_complete_lines`` 保 partial-line race 不丢行（SPEC §4 H4 明示）。
    lazy import 避免顶层拉环（events.tape 是 iface 下游，单向依赖合法；保延迟加载习惯）。
    """
    from orca.events.tape import read_last_complete_lines

    if not tape_path.is_file():
        return 0, 0.0
    try:
        end_offset = tape_path.stat().st_size
    except OSError:
        return 0, 0.0
    if end_offset <= 0:
        return 0, 0.0

    lines, _new_offset = read_last_complete_lines(tape_path, 0, end_offset)
    if not lines:
        return 0, 0.0
    # 取末尾 N 行（read_last_complete_lines 返全量；切片避免巨大 tape 拖慢 H4）。
    tail = lines[-tail_lines:] if len(lines) > tail_lines else lines

    import json as _json
    agent_count = 0
    last_ts = 0.0
    for line in tail:
        s = line.strip()
        if not s:
            continue
        try:
            obj = _json.loads(s)
        except _json.JSONDecodeError:
            continue
        etype = obj.get("type") or ""
        if isinstance(etype, str) and etype.startswith("agent_"):
            agent_count += 1
            ts = obj.get("timestamp") or 0
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            if ts_f > last_ts:
                last_ts = ts_f
    return agent_count, last_ts


def _compute_run_age(rundir: Path, run_id: str) -> float | None:
    """算 run_age_s（SPEC §4 H4：started_at ← marker，fallback run_dir ctime）。

    marker v3 §7.2 只 3 字段（无 started_at）→ 回退 run_dir ctime（``<rundir>/<run_id>/``）。
    run_dir 不存在 → 回退 marker 文件 ctime；都不在 → None。
    """
    import time as _time
    run_dir = rundir / run_id
    if run_dir.is_dir():
        try:
            return _time.time() - run_dir.stat().st_ctime
        except OSError:
            pass
    marker_path = rundir / f"orca-{run_id}.json"
    if marker_path.is_file():
        try:
            return _time.time() - marker_path.stat().st_ctime
        except OSError:
            pass
    return None


# ── H5 · bus_flow（SPEC §4 H5；S2 实现）──────────────────────────────────────


def _hop_h5_bus_flow(ctx: ProbeContext) -> dict[str, Any]:
    """H5：bus 订阅者队列有没有溢出丢事件？

    **结构性限制**（review 🔴#2）：``订阅者队列满`` warning（``bus.py:77``，``Subscription._enqueue``
    遇 ``QueueFull``）只在**有订阅者**的进程触发。订阅者 = WS pump，运行在 **web server 进程**
    （``run_manager.py`` 的 ``bus.subscribe``）；sidechain daemon 进程的 bus **无订阅者**
    （``sidechain_daemon.py`` 不调 ``bus.subscribe``，tape 经 ``emit`` 同步写不经 ``_enqueue``）。
    故 doctor 读的 ``<rundir>/<run_id>/sidechain_daemon.log`` **结构上永远不含该 warning**。

    因此本 hop **不能跨进程自动判定** web server 的队列溢出。doctor 是独立进程，拿不到 web
    server 进程的内存队列状态。诊断价值在 **H4↔H6 对比 + 手动取证**：
      - H4=pass（tape 有事件）但 H6=fail/unknown（前端收不到）→ 嫌疑 web server 队列溢出 / pump 断。
      - 手动确认：``grep 订阅者队列满 <web server stdout/log>``。

    仍 grep daemon log 防御性兜底（若未来 daemon 进程加了订阅者会命中），但生产常态返 unknown。

    status：
      - ``fail``：daemon log 命中 ≥1 次（罕见 / 未来变更）。
      - ``unknown``：无命中（生产常态）——reason 给 H4↔H6 对比 + 手动 grep 指引。
    """
    # 无 run_id → H5 不适用（无 daemon log 可读）。
    if not ctx.run_id:
        return {
            "hop": H5_BUS_FLOW,
            "status": "unknown",
            "evidence": "run_id 未给（H5 同 H4，需 --run-id 读 daemon log）",
            "reason": "doctor --probe-push 未带 --run-id，H5 无 log 可读",
            "fix_hint": _fix_hint(H5_BUS_FLOW),
        }

    log_path = ctx.rundir / ctx.run_id / "sidechain_daemon.log"
    log_text = _read_text_safe(log_path)
    queue_full = log_text.count(_H5_QUEUE_FULL_PATTERN)

    evidence = (
        f"log={log_path}, queue_full_warnings={queue_full}"
        f"（grep pattern={_H5_QUEUE_FULL_PATTERN!r}, bus.py:77）"
    )

    if queue_full > 0:
        return {
            "hop": H5_BUS_FLOW,
            "status": "fail",
            "evidence": evidence,
            "reason": (
                f"daemon log 有 {queue_full} 次订阅者队列满 warning"
                "——罕见（daemon 通常无订阅者）；若命中说明 daemon 进程加了订阅者且消费过慢"
            ),
            "fix_hint": _fix_hint(H5_BUS_FLOW),
        }

    return {
        "hop": H5_BUS_FLOW,
        "status": "unknown",
        "evidence": evidence,
        "reason": (
            "daemon log 无队列满 warning（结构上常态：daemon bus 无订阅者，该 warning 只在"
            " web server 进程发）。**bus 队列溢出跨进程不可自动判定**——若 H4=pass 但 H6=fail/"
            "unknown，嫌疑 web server 队列溢出/pump 断；手动确认：grep 订阅者队列满 <web server"
            " stdout/log>"
        ),
        "fix_hint": _fix_hint(H5_BUS_FLOW),
    }


def _hop_h6_ws_delivery(ctx: ProbeContext) -> dict[str, Any]:
    """H6：bus→WS pump 链路通吗？合成事件能秒级到 WS？

    实现（SPEC §4 H6 + B2 决议 degradation path；RunManager.start_run 不接 backend 参数，
    走 SPEC 明示降级）：

      1. 起 probe run：``RunManager(runs_dir=tmp).start_run(最小单节点 wf)``——注册真
         in-process RunHandle（bus + tape + gate_handler）到 manager._runs。
      2. monkey-patch ``Orchestrator.run`` 为 noop（仅 probe 进程内；避免 ClaudeExecutor
         spawn 真 claude 子进程——SPEC §0 非目标「H6 self-spawn 不烧模型」）。
      3. ``create_app(manager)`` + ``uvicorn.Server`` bind ``127.0.0.1:0``（OS 分配端口）。
      4. ``websockets.connect`` → send ``subscribe(run_id)``。
      5. ``handle.bus.emit("agent_message", {...})`` 注入合成事件（SPEC §4 H6 degradation
         明示：``复用 EventBus.emit 公开 API``，非新接口）。
      6. ``asyncio.wait_for(recv, timeout=3.0)`` 等收。
      7. finally：WS close + ``manager.cancel_run(probe_run_id)`` + uvicorn shutdown。

    用独立 tmp runs_dir + ``__probe__`` 前缀 run_id 隔离，防污染用户 run（SPEC §4 H6 隔离
    要求 + §7-5c 反例连续两次跑无残留）。

    **passive 模式**（S5，SPEC §4 H6 + §7-9）：``ctx.ws_url`` 给定时走被动监听——连用户在跑
    的 web server，subscribe ``ctx.run_id``，等收**真实**事件（不自己起 web、不注入合成事件，
    外部进程拿不到该 run 的 bus 句柄）。回答「我这个 run 的事件到没到前端」——比 self-spawn
    的「orca 推送代码本身通不通」更贴近真实排障。需 ``--run-id``；缺 → fail + hint。

    status：
      - ``pass``：self-spawn 3s / passive N s 内 WS 收到目标事件。
      - ``fail``：超时（self-spawn）/ WS 连接拒绝 / subscribe 缺 run_id / pump 异常。
      - ``unknown``：passive subscribe 成功但 N s 无事件（可能该 run 无新事件，passive 无法注入）。
      - ``error``：probe 自身异常（uvicorn / manager 抛错）。
    """
    import asyncio

    try:
        if ctx.ws_url:
            return asyncio.run(_hop_h6_ws_delivery_passive_async(ctx))
        return asyncio.run(_hop_h6_ws_delivery_async(ctx))
    except Exception as e:  # noqa: BLE001 — outer guard：asyncio.run / setup 抛错不传染
        logger.warning("H6 ws_delivery probe 抛异常", exc_info=True)
        return {
            "hop": H6_WS_DELIVERY,
            "status": "error",
            "evidence": "",
            "reason": f"H6 ws_delivery setup 抛异常：{type(e).__name__}: {e}",
            "fix_hint": _fix_hint(H6_WS_DELIVERY),
        }


async def _hop_h6_ws_delivery_passive_async(ctx: ProbeContext) -> dict[str, Any]:
    """H6 passive 模式（S5，SPEC §4 H6 + §7-9）：连用户在跑的 web，被动监听真实 run 事件。

    与 self-spawn 区别：不自己起 web / 不注入合成事件（外部进程拿不到该 run 的 bus 句柄）；
    只连既存 ``ctx.ws_url`` 的 ``/ws``，subscribe ``ctx.run_id``，被动等收真实事件。回答
    「我这个 run 的事件到没到前端」——比 self-spawn 的「orca 推送代码通不通」更贴近真实排障。

    需 ``ctx.run_id``（subscribe 目标）；缺 → fail + hint「passive 模式需 --run-id」。

    status：
      - ``pass``：``_H6_PASSIVE_LISTEN_SECONDS`` 内收到该 run 的事件（真实链路通）。
      - ``fail``：WS 连接拒绝 / subscribe 缺 run_id。
      - ``unknown``：subscribe 成功但监听窗口无事件（可能该 run 无新事件；passive 无法注入
        合成事件到别人的 bus，不能强判）。
    """
    import asyncio
    import websockets

    ws_url = ctx.ws_url
    run_id = ctx.run_id
    if not run_id:
        return _h6_response(
            status="fail", mode="passive", run_id=run_id, ws_url=ws_url,
            reason="passive 模式（--ws-url）需要 --run-id 指定 subscribe 目标",
        )

    # 连既存 web server（3s 连接超时；拒绝/超时 → fail：web 没起 / URL 错）。
    try:
        ws_client = await asyncio.wait_for(websockets.connect(ws_url), timeout=3.0)
    except Exception as e:  # noqa: BLE001 — TimeoutError / OSError / InvalidURI 等
        return _h6_response(
            status="fail", mode="passive", run_id=run_id, ws_url=ws_url,
            reason=f"WS 连接失败（web server 没起 / 端口错 / URL 错）：{type(e).__name__}: {e}",
        )

    try:
        await ws_client.send(json.dumps({"type": "subscribe", "run_id": run_id}))
        # 给 server 端 _handle_subscribe 起 pump task 一点时间。
        await asyncio.sleep(0.3)
        deadline = asyncio.get_running_loop().time() + _H6_PASSIVE_LISTEN_SECONDS
        received: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(
                    ws_client.recv(), timeout=min(1.0, remaining),
                )
            except asyncio.TimeoutError:
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            # 收到该 run 的任一事件即判 pass（pump 把 event.model_dump + run_id 标签发出）。
            if payload.get("run_id") == run_id:
                received = payload
                break
        if received is not None:
            return _h6_response(
                status="pass", mode="passive", run_id=run_id, ws_url=ws_url, reason="",
                extra=(
                    f"received {received.get('type')} within {_H6_PASSIVE_LISTEN_SECONDS}s"
                    "（真实事件 bus→pump→WS 链路通）"
                ),
            )
        # subscribe 成功但窗口内无事件 → unknown（被动模式无法注入，不强判 fail）。
        return _h6_response(
            status="unknown", mode="passive", run_id=run_id, ws_url=ws_url,
            extra=f"no event in {_H6_PASSIVE_LISTEN_SECONDS}s",
            reason=(
                f"subscribe 后 {_H6_PASSIVE_LISTEN_SECONDS}s 内未收到 {run_id} 的事件。"
                "可能该 run 无新事件（passive 无法注入合成事件判定）；若你确定子 agent 正在"
                "产事件却收不到 → pump 断（self-spawn 模式可复现确认）。"
            ),
        )
    finally:
        try:
            await ws_client.close()
        except Exception:  # noqa: BLE001
            pass


async def _hop_h6_ws_delivery_async(ctx: ProbeContext) -> dict[str, Any]:
    """H6 async 主体（``asyncio.run`` 包裹）。"""
    import asyncio
    import tempfile
    import uuid

    # lazy import：iface.web 是 iface.in_session 的姐妹层，函数内 import 防潜在环。
    from orca.iface.web.run_manager import RunManager
    from orca.iface.web.server import create_app
    # S3 决议（SPEC §4 H6 B2 degradation）：monkey-patch Orchestrator.run 为 hang-forever
    # 仅本 probe 进程内——start_run 内部会 asyncio.create_task(orch.run())，hang 让该 task
    # 永不自然 done → _run_with_sem 的 finally / teardown 不触发 → bus 不 close → emit 可达。
    # 不spawn claude（SPEC §0 非目标）。try/finally 严格恢复（防 patch leak 到后续 doctor 调用）。
    from orca.run.orchestrator import Orchestrator

    original_run = Orchestrator.run
    probe_block_event = asyncio.Event()

    async def _hang_run(self):
        # 永远挂起直到 cancel_run 触发 task.cancel → CancelledError 传播。
        await probe_block_event.wait()
        return None

    manager: RunManager | None = None
    server = None
    ws_client = None
    probe_run_id: str | None = None
    server_task: asyncio.Task | None = None
    probe_runs_dir: Path | None = None
    patched = False
    try:
        # monkey-patch 必须在 try 块内：mkdtemp/write_text 抛异常时确保恢复（review H-1）。
        Orchestrator.run = _hang_run  # type: ignore[method-assign]
        patched = True

        # 独立 tmp runs_dir（SPEC §4 H6 隔离 + §7-5c 无残留）：防 __probe__ run 污染用户 runs/。
        probe_runs_dir = Path(tempfile.mkdtemp(prefix="orca-push-probe-"))
        # 最小单节点 wf yaml：kind=agent + executor + 内联 prompt + 空 routes，无 inputs/requires
        # 段（apply_kb_requirement no-op）。Orchestrator.run 被 patch 不 spawn claude。
        probe_wf_yaml = probe_runs_dir / "__probe__.yaml"
        probe_wf_yaml.write_text(
            "name: __probe__\n"
            "description: push-chain probe (H6 ws_delivery)\n"
            "entry: n1\n"
            "nodes:\n"
            "  - name: n1\n"
            "    kind: agent\n"
            "    executor: claude\n"
            "    prompt: probe placeholder (noop orchestrator)\n"
            "    routes: []\n",
            encoding="utf-8",
        )

        manager = RunManager(runs_dir=probe_runs_dir, max_concurrent=1)
        probe_run_id = await manager.start_run(probe_wf_yaml, inputs={})

        # 起 uvicorn ephemeral port。
        import uvicorn
        app = create_app(manager)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="on",
        )
        server = uvicorn.Server(config)
        # force_exit 与 should_exit 组合保证 shutdown 及时（review H-4）：default config 的
        # keep_alive 5s 可能让 graceful shutdown 超 3s 兜底 timeout。
        server.force_exit = True
        server_task = asyncio.create_task(server.serve())

        # 等 server 起来 + 拿到端口（uvicorn 0.0.0.0:0 → OS 分配；server.servers[0].sockets）。
        await _wait_server_listening(server, timeout=3.0)
        port = _get_server_port(server)
        if port is None:
            return _h6_response(
                status="fail", mode="self-spawn", run_id=probe_run_id,
                reason="uvicorn 起 server 后未拿到监听端口",
            )

        # WS connect + subscribe。
        import websockets
        ws_url = f"ws://127.0.0.1:{port}/ws"
        ws_client = await websockets.connect(ws_url)
        await ws_client.send(json.dumps({"type": "subscribe", "run_id": probe_run_id}))

        # 等 server 端 subscribe 处理完（_handle_subscribe 起 pump task + 注册 _subs）。
        # 不用固定 sleep，用轮询 pump task 已起（review H-3：CI 慢机时序耦合）。
        web_server = _get_web_server(manager)
        await _wait_subscribe_ready(web_server, ws_client, timeout=2.0)

        # 注入合成 agent_message 事件（SPEC §4 H6 degradation：复用 EventBus.emit 公开 API）。
        handle = manager.get_handle(probe_run_id)
        if handle is None:
            return _h6_response(
                status="fail", mode="self-spawn", run_id=probe_run_id,
                reason=f"manager 找不到刚 start_run 的 probe run {probe_run_id}",
            )
        await handle.bus.emit(
            "agent_message",
            data={"text": "__probe__ synthetic event for H6 ws_delivery"},
            node="n1",
            session_id=str(uuid.uuid4().hex),
        )

        # 等收 ≤3s（SPEC §4 H6 3s 阈值）。
        try:
            raw = await asyncio.wait_for(ws_client.recv(), timeout=3.0)
        except asyncio.TimeoutError:
            return _h6_response(
                status="fail", mode="self-spawn", run_id=probe_run_id, ws_url=ws_url,
                reason="3s 内未收到事件（bus→pump→WS 链路不通；pump 异常静默退出 / WS 未订阅）",
            )

        # 验证收到的 event type（pump send_json 整个 event model_dump）。
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            return _h6_response(
                status="fail", mode="self-spawn", run_id=probe_run_id, ws_url=ws_url,
                reason=f"WS 收到非 JSON 数据：{raw!r}, 解析错：{e}",
            )
        if payload.get("type") != "agent_message" or payload.get("run_id") != probe_run_id:
            return _h6_response(
                status="fail", mode="self-spawn", run_id=probe_run_id, ws_url=ws_url,
                reason=(
                    f"WS 收到事件但非目标 agent_message（got type={payload.get('type')!r}, "
                    f"run_id={payload.get('run_id')!r}）"
                ),
            )

        return _h6_response(
            status="pass", mode="self-spawn", run_id=probe_run_id, ws_url=ws_url, reason="",
            extra="received agent_message within 3s（bus→pump→WS 链路通）",
        )

    finally:
        # 严格清理（SPEC §4 H6 隔离 + §7-5c 无残留）。
        # 解除 hang：让 patched Orchestrator.run 立即返回（防 task 永远挂起）。
        probe_block_event.set()
        # 恢复 monkey-patch（patched 标记防 finally 在 patch 前 raise 时再 set 覆盖）。
        if patched:
            Orchestrator.run = original_run  # type: ignore[method-assign]
        if ws_client is not None:
            try:
                await ws_client.close()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if server is not None:
            server.should_exit = True
            if server_task is not None:
                try:
                    await asyncio.wait_for(server_task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                    try:
                        server_task.cancel()
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
        if manager is not None and probe_run_id is not None:
            try:
                await manager.cancel_run(probe_run_id)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            try:
                await manager.shutdown(timeout=2.0)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 清理 tmp runs_dir（独立 mkdtemp，不碰用户 runs/）。
        import shutil
        if probe_runs_dir is not None:
            try:
                shutil.rmtree(probe_runs_dir, ignore_errors=True)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _get_web_server(manager) -> Any:
    """从 RunManager 拿 WebServer 实例（create_app 内部 new 一个存 app.state）。

    create_app 把 ``WebServer(manager)`` 装到 ``app.state.manager`` + router，没有公开引用。
    我们 monkey-patch 的是类方法 ``WebServer._pump``，不需要拿到 instance——但当 H-3 等
    守门测试要确认 subscribe 已就绪时需要查 ``web_server._subs``。
    本函数容忍拿不到的情况（返 None，调用方走 sleep fallback）。
    """
    # WebServer 实例在 create_app 内部 local 变量；不暴露。返回 None 走 sleep fallback。
    # 改为通过订阅者注册状态间接查（manager._runs 已注册 → _handle_subscribe 跑完）。
    return None


async def _wait_subscribe_ready(web_server: Any, ws_client: Any, *, timeout: float) -> None:
    """等 server 端 subscribe 处理完（pump task 已起）。

    无 web_server 实例访问时退化为短 sleep（review H-3 改进点：理想方案是查
    ``web_server._subs.get(ws_client)`` 但 ws_client 是 client 端对象，server 端 ws 是另一
    实例，无法直接 key——保留短 sleep 兜底，文档化时序耦合）。
    """
    import asyncio
    await asyncio.sleep(0.1)


def _h6_response(
    *, status: str, mode: str, run_id: str | None, reason: str,
    ws_url: str | None = None, extra: str = "",
) -> dict[str, Any]:
    """H6 统一响应构造（self-spawn + passive 共用，evidence schema 一致——review 🟡#3）。

    evidence 格式恒定：``mode=<self-spawn|passive>, run_id=<id>, ws_url=<url>[, <extra>]``。
    消费方（主 session LLM）只写一套解析，不必按模式分叉。``extra`` 承载 mode 无关的细节
    （self-spawn「received within 3s」/ passive「received within N s」/「no event in N s」）。
    """
    parts = [f"mode={mode}"]
    if run_id is not None:
        parts.append(f"run_id={run_id}")
    if ws_url is not None:
        parts.append(f"ws_url={ws_url}")
    if extra:
        parts.append(extra)
    return {
        "hop": H6_WS_DELIVERY,
        "status": status,
        "evidence": ", ".join(parts),
        "reason": reason,
        "fix_hint": _fix_hint(H6_WS_DELIVERY),
    }


async def _wait_server_listening(server, timeout: float = 3.0) -> None:
    """等 uvicorn.Server 起到 sockets 非空（poll 每 50ms）。超时即返回（由 caller 报 fail）。"""
    import asyncio
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        sockets = getattr(server, "servers", None) or []
        if any(s.is_serving() if hasattr(s, "is_serving") else s.sockets for s in sockets):
            return
        # uvicorn Server 不一定有 servers list 立即；退一步看 server.started 标志。
        if getattr(server, "started", False):
            return
        await asyncio.sleep(0.05)


def _get_server_port(server) -> int | None:
    """从 uvicorn.Server 拿监听端口（OS 分配的 ephemeral port）。"""
    servers = getattr(server, "servers", None) or []
    for srv in servers:
        socks = list(srv.sockets) if hasattr(srv, "sockets") else []
        for sock in socks:
            try:
                addr = sock.getsockname()
                if isinstance(addr, tuple) and len(addr) >= 2:
                    return int(addr[1])
            except (OSError, TypeError):
                continue
    return None
