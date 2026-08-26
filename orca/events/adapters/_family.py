"""_family.py —— sidechain root / DB path resolver（SPEC §P4，events 层，**零 iface import**）。

**回答的问题**：B2 adapter（cc_jsonl / opencode_sqlite）的路径解析原本硬编 ``~/.claude`` /
``~/.local/share/opencode``，无法适配 cac（cc 换皮）与 nga（opencode 换皮）。本模块给 adapter
与 doctor 共用的统一 resolver，按家族映射 + 4 级优先级解析路径。

**家族映射**（与 ``orca.iface.cli.skill_cmds.HOST_DOTDIR`` 同源）：

    CC_FAMILY_DOTDIR        = {"cc": ".claude",   "cac": ".cac"}
    OPENCODE_FAMILY_DOTDIR  = {"opencode": ".opencode", "nga": ".nga"}

**同源约束（不跨层 import）**：events 层不得 import iface（``rg 'from orca.iface' orca/events/``
零 hit 守门）。两处 dict 各自维护，加新前端时同步（reviewer 应 grep 双向校验）。理由：iface 层
cli.py 与 events 层 adapters 是单向依赖（iface → events），反向 import 会引入循环 + 把 iface
的 typer / 配置 I/O 副作用拉进 events。

**resolver 4 级优先级（SPEC §P4 接口契约）**：

  1. env 整体覆盖（``ORCA_CC_SIDECHAIN_ROOT`` / ``ORCA_OPENCODE_DB``）→ source="env"
  2. ``family`` 参数显式（caller 从 config 读传入，resolver 自己不读 config）→ source="config"
  3. 探测两个 dotdir 哪个路径存在：
     - 单一存在 → source="probe"
     - 两存歧义：CC 家族 **cac 优先**（.cac 存在即选 cac）；opencode 家族默认 opencode。
       source="probe"（caller 用 ``detect_cc_existing_roots`` / ``detect_opencode_existing_dbs``
       报告两存）
  4. 默认 cc/opencode → source="default"

**caller 责任**：
  - **adapter**（cc_jsonl / opencode_sqlite）：调 resolver，``family=None``（adapter 不读 config）。
    daemon 启动时从 argv ``--family`` 把 config 值透传到 adapter ctor → resolver。
  - **doctor**（``cli.py``）：iface 层读 ``sidechain.family`` config，调 resolver 拿 resolved 路径
    + source + 存在性 + hint（SPEC §P4 验收「doctor 输出 resolved 路径」）。

**fail loud**：
  - ``family`` 非法（不在 ``*_FAMILY_DOTDIR`` keys）→ raise ``ValueError``（adapter 在 ctor 里
    包装成自家 AdapterError；doctor 直接报 fail）。
  - CC ``host_session`` 空（非 env 路径下）→ raise ``ValueError``（同上）。

**依赖单向**：本模块只依赖 stdlib（``os`` / ``pathlib``），不 import 任一 orca 子包——保 events 层
依赖图干净（无环、无跨层），也使 resolver 可被 doctor / daemon / adapter 三方零代价共享（DRY）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 家族 → dotdir 映射。
#
# **同源声明**：与 ``orca.iface.cli.skill_cmds.HOST_DOTDIR`` 字段同源——加新前端（如再加 ``caco``）
# 时**两处同步更新**（reviewer grep 校验：``rg '\.cac|\.nga|\.claude|\.opencode' orca/events/adapters/_family.py orca/iface/cli/skill_cmds.py``）。
# 为何不跨层 import：events 层依赖铁律（见模块 docstring）。
CC_FAMILY_DOTDIR: dict[str, str] = {"cc": ".claude", "cac": ".cac"}
OPENCODE_FAMILY_DOTDIR: dict[str, str] = {"opencode": ".opencode", "nga": ".nga"}

# opencode 家族 DB 主名（v1.18+ 实证写 opencode.db）；session.db 作 fallback（B2-VRFY Bug #1）。
_OPENCODE_DB_PRIMARY = "opencode.db"
_OPENCODE_DB_FALLBACK = "session.db"


def _encode_cwd(cwd: str) -> str:
    """``<encoded-cwd>`` = cwd 中**非字母数字**字符逐个 → ``-``（CC projects 目录约定）。

    CC 逐字符归一：凡 ``[^a-zA-Z0-9]`` —— ``/`` ``.`` ``_`` 空格 ``\\`` …… —— 一律变 ``-``，
    大小写与数字保留。e.g. ``/mnt/d/Projects/Orca`` → ``-mnt-d-Projects-Orca``；
    ``/home/u/my_app.v2`` → ``-home-u-my-app-v2``。

    **实证来源**：在含 ``_`` / ``.`` / 空格 的目录跑 headless CC（``claude -p``）观察其生成的
    ``~/.claude/projects/`` 目录名——CC 把这三种字符都编码成 ``-``。旧实现仅换 ``/``，在含特殊
    字符的 cwd 下算出的 sidechain root 与 CC 实际写入目录不符，daemon discover 不到子 agent
    jsonl（子 agent 消息进不了 web；doctor H3 误报 root 不存在）。

    **实证范围**：仅 Linux/macOS（POSIX 路径）。Windows 盘符（``C:``）与 Unicode（CJK/emoji）
    cwd 的 CC 实际编码**未实测**——若遇此场景，先 headless ``claude -p`` 实测确认再依赖。

    **fail loud**：空 cwd raise（CC projects 目录需要非空 cwd；与 ``host_session`` 空校验对称）。

    从 ``cc_jsonl.py`` 移入并复用（DRY：CC sidechain root 派生 + doctor 探测目录都需此）。
    ``cc_jsonl`` 通过 ``from orca.events.adapters._family import _encode_cwd`` 再导出，保既有
    import（``from cc_jsonl import _encode_cwd``）零回归。
    """
    if not cwd:
        raise ValueError("cwd 为空，无法派生 <encoded-cwd>（CC projects 目录需要非空 cwd）")
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


# ── CC sidechain root ─────────────────────────────────────────────────────────


def _cc_sidechain_path(
    family: str, host_session: str, *, cwd: str | None = None,
) -> Path:
    """构造 ``~/.<dotdir[family]>/projects/<enc>/<host_session>/subagents``。

    ``family`` 必须 ∈ ``CC_FAMILY_DOTDIR``（caller 责任，本函数不校验）。
    纯构造，不做存在性探测。
    """
    dotdir = CC_FAMILY_DOTDIR[family]
    actual_cwd = cwd or os.getcwd()
    encoded = _encode_cwd(actual_cwd)
    return Path.home() / dotdir / "projects" / encoded / host_session / "subagents"


def detect_cc_existing_roots() -> set[str]:
    """探测 ``~/.cac`` 与 ``~/.claude`` 哪个 dotdir 存在（机器级：装了哪个 CC 换皮），返家族集合。

    机器级决策（这台机用 cac 还是 cc），**不依赖 host_session / per-session subagents 目录**——
    family 不该看某 session 派没派过子 agent（避免 daemon 启动早于首次派子 agent、目录未建时的
    时序坑）。doctor 用本函数报告两存（``len() >= 2``；CC 家族 cac 优先消解，两存不再视作歧义）。
    """
    return {
        fam for fam in CC_FAMILY_DOTDIR
        if (Path.home() / CC_FAMILY_DOTDIR[fam]).is_dir()
    }


def resolve_cc_sidechain_root(
    host_session: str, *, cwd: str | None = None, family: str | None = None,
) -> tuple[Path, str]:
    """解析 CC sidechain root 路径 + 来源标记（SPEC §P4 接口契约）。

    解析顺序（family 决策全在本函数内，不依赖 daemon ``--backend`` 参数）：

      1. ``ORCA_CC_SIDECHAIN_ROOT`` env（整体覆盖，**不依赖 host_session**）→ source="env"
      2. ``family`` 参数显式（"cc" 或 "cac"）→ ``~/.<dotdir[family]>/projects/...`` → source="config"
      3. 探测 ``~/.cac`` / ``~/.claude`` 哪个 dotdir 存在（机器级，**cac 优先**）：
         - ``~/.cac`` 存在（无论 .claude）→ ``~/.cac/...`` → source="probe"
         - 仅 ``~/.claude`` 存在 → ``~/.claude/...`` → source="probe"
         （caller/doctor 可调 ``detect_cc_existing_roots`` 报告两存；如需 .claude 显式设 family=cc）
      4. 默认 ``cc`` / ``.claude``（两路径都不存在）→ source="default"

    Args:
        host_session: 宿主 CC session id（env 路径下可空；其它路径下空 → raise）。
        cwd: 当前工作目录（默认 ``os.getcwd()``），派生 ``<encoded-cwd>``。
        family: 显式家族 ``"cc"`` / ``"cac"``；None → 走探测。caller 从 config ``sidechain.family``
            读入传入（resolver 不读 config）。

    Returns:
        ``(root, source)``；root ``Path`` **不保证存在**（caller ``is_dir`` / ``exists`` 自判）；
        source ∈ ``{"env", "config", "probe", "default"}``。

    Raises:
        ValueError: ``family`` 非 ``"cc"``/``"cac"``；非 env 路径下 ``host_session`` 为空。
    """
    env_root = os.environ.get("ORCA_CC_SIDECHAIN_ROOT")
    if env_root:
        return Path(env_root), "env"

    if family is not None and family not in CC_FAMILY_DOTDIR:
        raise ValueError(
            f"unknown CC family {family!r}（预期 'cc' 或 'cac'）"
        )

    if family is not None:
        if not host_session:
            raise ValueError(
                "CC sidechain root 解析失败：host_session 为空"
                "（需要 CLAUDE_CODE_SESSION_ID 或显式 --host-session）"
            )
        return _cc_sidechain_path(family, host_session, cwd=cwd), "config"

    if not host_session:
        raise ValueError(
            "CC sidechain root 解析失败：host_session 为空"
            "（需要 CLAUDE_CODE_SESSION_ID 或显式 --host-session）"
        )

    existing = detect_cc_existing_roots()
    # cac 优先：.cac 存在即走 cac（无论 .claude 是否存在）；仅 .claude 存在才走 cc。
    # doctor 用 detect_cc_existing_roots 报告两存（提示如需 .claude 显式设 family=cc）。
    if "cac" in existing:
        return _cc_sidechain_path("cac", host_session, cwd=cwd), "probe"
    if "cc" in existing:
        return _cc_sidechain_path("cc", host_session, cwd=cwd), "probe"

    return _cc_sidechain_path("cc", host_session, cwd=cwd), "default"


# ── opencode DB path ──────────────────────────────────────────────────────────


def _opencode_db_path(family: str, *, name: str = _OPENCODE_DB_PRIMARY) -> Path:
    """构造 ``~/.local/share/<family>/<name>``（默认 opencode.db）。

    ``family`` 必须 ∈ ``OPENCODE_FAMILY_DOTDIR``（caller 责任）。

    **注意**：opencode data 目录用 **bare 家族名**（``opencode`` / ``nga``，无前导点），
    **不**用 dotdir（``.opencode`` / ``.nga``）。dotdir 是 skill install / config 路径用的
    （见 ``skill_cmds.HOST_DOTDIR``）；data 目录与之不同（opencode v1.18+ 实证 DB 在
    ``~/.local/share/opencode/opencode.db``，无点）。nga 沿用同结构（用户只换 dotdir 与 data
    subdir 名）。
    """
    return Path.home() / ".local" / "share" / family / name


def detect_opencode_existing_dbs() -> set[str]:
    """探测 ``.opencode`` 与 ``.nga`` 哪个 dotdir 有主 DB（opencode.db），返存在的家族集合。

    doctor 用本函数报告 nga 歧义（``len() >= 2``）。
    """
    return {
        fam for fam in OPENCODE_FAMILY_DOTDIR
        if _opencode_db_path(fam).is_file()
    }


def resolve_opencode_db(*, family: str | None = None) -> tuple[Path, str]:
    """解析 opencode sqlite DB 路径 + 来源标记。

    解析顺序：

      1. ``ORCA_OPENCODE_DB`` env → source="env"
      2. ``family`` 参数显式（"opencode" 或 "nga"）→ ``~/.local/share/<dotdir>/opencode.db``
         → source="config"
      3. 探测 ``.opencode`` / ``.nga`` 哪个 dotdir 的 ``opencode.db`` 存在：
         - 单一存在 → source="probe"
         - 两存歧义 → 默认 ``opencode``（保守）→ source="probe"
      4. 默认：保留既有 ``opencode.db`` > ``session.db`` 回退语义（B2-VRFY Bug #1）→ source="default"

    Args:
        family: 显式家族 ``"opencode"`` / ``"nga"``；None → 走探测。

    Returns:
        ``(db_path, source)``；source ∈ ``{"env", "config", "probe", "default"}``。

    Raises:
        ValueError: ``family`` 非 ``"opencode"``/``"nga"``。
    """
    env_db = os.environ.get("ORCA_OPENCODE_DB")
    if env_db:
        return Path(env_db), "env"

    if family is not None and family not in OPENCODE_FAMILY_DOTDIR:
        raise ValueError(
            f"unknown opencode family {family!r}（预期 'opencode' 或 'nga'）"
        )

    if family is not None:
        # config 显式：返 family 主 DB（opencode.db）。session.db fallback 仅 default 路径用。
        return _opencode_db_path(family), "config"

    existing = detect_opencode_existing_dbs()
    if len(existing) == 1:
        return _opencode_db_path(next(iter(existing))), "probe"
    if len(existing) == 2:
        return _opencode_db_path("opencode"), "probe"  # 歧义默认 opencode

    # default：detect_opencode_existing_dbs 用 ``_opencode_db_path(fam).is_file()`` 判存——
    # 既然 detect 返空集，``~/.local/share/opencode/opencode.db`` 必不存在，故直接走 session.db
    # fallback（B2-VRFY Bug #1 回退语义：opencode.db > session.db；probe 阶段已耗尽 opencode.db
    # 命中可能，default 兜底 session.db）。不重复 ``primary.is_file()`` 检查（曾是死代码）。
    return _opencode_db_path("opencode", name=_OPENCODE_DB_FALLBACK), "default"
