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


# ── H4-H6 placeholder（S2/S3 接入；S1 占位返 not_implemented status=unknown）────


def _hop_h4_daemon_progress(ctx: ProbeContext) -> dict[str, Any]:
    """H4：daemon 存活且真在推进（tape 有 agent_* 事件）？（SPEC §4 H4；S2 实现）"""
    return {
        "hop": H4_DAEMON_PROGRESS,
        "status": "unknown",
        "evidence": "not_implemented(placeholder for S2)",
        "reason": "H4 daemon_progress 由 S2 接入（SPEC §8 落地拆分）",
        "fix_hint": _fix_hint(H4_DAEMON_PROGRESS),
    }


def _hop_h5_bus_flow(ctx: ProbeContext) -> dict[str, Any]:
    """H5：bus 订阅者队列有没有溢出丢事件？（SPEC §4 H5；S2 实现）"""
    return {
        "hop": H5_BUS_FLOW,
        "status": "unknown",
        "evidence": "not_implemented(placeholder for S2)",
        "reason": "H5 bus_flow 由 S2 接入（SPEC §8 落地拆分）",
        "fix_hint": _fix_hint(H5_BUS_FLOW),
    }


def _hop_h6_ws_delivery(ctx: ProbeContext) -> dict[str, Any]:
    """H6：bus→WS pump 链路通吗？合成事件能秒级到 WS？（SPEC §4 H6；S3 实现）"""
    return {
        "hop": H6_WS_DELIVERY,
        "status": "unknown",
        "evidence": "not_implemented(placeholder for S3)",
        "reason": "H6 ws_delivery 由 S3 接入（SPEC §8 落地拆分）",
        "fix_hint": _fix_hint(H6_WS_DELIVERY),
    }
