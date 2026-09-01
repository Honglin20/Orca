"""catalog.py —— workflow catalog（纯函数 + 文件系统，SPEC phase-10 §5.6）。

回答「``list_workflows`` / ``describe_workflow`` 数据从哪来？」：扫项目级 + 用户级
``workflows/`` 目录，用 ``compile.load_workflow`` 加载每个 YAML，提取元信息。

设计约束（§5.6 关键约束）：
  - 纯函数 + 文件系统，无 daemon 注册表、无 db。
  - 加载失败（yaml 语法错 / agent 引用缺失）→ log warning + 跳过，不中断列表。
  - ``find_workflow_by_name`` / ``find_workflow_yaml_path`` 给 ``start_workflow`` /
    ``describe_workflow`` 反查用（name → yaml_path）。

进程内缓存（web-perf）：``list_workflows`` / ``find_workflow`` 首扫后缓存扫描结果，
后续调用先比对文件系统指纹（纯 metadata stat，不 parse yaml）——未变则 O(stat)
返回，变了则全量重扫替换缓存。失效语义见缓存段注释（yaml 清单 + load 实际读取
的相邻依赖文件集）。无 TTL 轮询、无后台线程（确定性优先）；对调用方完全透明
（签名与返回值不变，``find_workflow`` 每次返回独立深拷贝，调用方改对象不污染缓存）。

in-session v5 §6.2：setup phase 全栈删除，catalog 不再返 ``has_setup`` / setup 元信息。

依赖单向：本模块依赖 ``orca.compile``（parser / validator / layout）+ ``orca.schema``
（Workflow）。不依赖 run/exec/events。缓存只用标准库（threading / copy），不引入新依赖。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca.compile import ConfigurationError, load_workflow
from orca.compile.layout import resolve_subagents_dir, scan_workflow_yamls
from orca.compile.parser import _iter_agent_nodes
from orca.compile.validator import _AGENT_RESOURCE_SCRIPT_RE
from orca.schema.workflow import Workflow

logger = logging.getLogger(__name__)


# workflow catalog 扫描目录（first-wins：project-local 优先于 user-global）。
def _workflow_dirs() -> list[Path]:
    """catalog 扫描目录列表（project-local + user-global）。

    返回顺序决定 ``find_workflow_by_name`` 的优先级（first-wins）。``~/.orca/workflows``
    在 user home 下，跨 project 共享（与 agent pool 同模式）。
    """
    return [
        Path.cwd() / "workflows",
        Path.home() / ".orca" / "workflows",
    ]


# ── 进程内缓存（web-perf：消除 tars serve 下 web 路由的重复全量扫描）───────────
#
# 失效语义（读码结论 + drvfs 实测驱动）：``load_workflow`` 结果不止取决于 yaml
# 自身——``LocalPoolResolver`` 物化 ``agents/`` 下的 agent md（prompt + meta），
# ``validate_workflow`` 校验 ``subagents/`` md 与 ``$ORCA_AGENT_RESOURCES/scripts``
# 脚本存在性。因此缓存 key = **load 实际读取的依赖文件集** 的 stat 指纹：
#   1. yaml 清单（目录列表 + 每个 yaml 的 (相对路径, mtime_ns, size)）——
#      改写 / 新增 / 删除 yaml 必被感知（start_workflow 消费方契约）；
#   2. 每个**加载成功** workflow 的精确依赖集（从加载产物反推，见
#      ``_entry_watch_paths``）：agent 入口 md 内容、resolver base 目录（agent
#      增删 / 优先级覆盖）、脚本引用父目录（存在性）、subagents 目录 + md；
#   3. 加载**失败**的 yaml 每次校验重试载一次——健康 catalog 零成本，坏 yaml
#      修复即刻被感知（不给 dropped workflow 留盲区）。
# 精确集而非全子树暴力 stat：drvfs 单次 metadata 调用 ~2.5ms，全子树（本 repo
# workflows 树 774 文件）实测 3.6s，比不缓存还慢；精确集 ~180 项 ≈ 0.35s
# （ext4 ~15ms）。

_CACHE_LOCK = threading.Lock()
_CACHE: "_CatalogCache | None" = None


@dataclass(frozen=True)
class _CatalogCache:
    """一次全量扫描的结果 + 触发它的指纹。

    ``entries`` 按扫描序（目录序 + yaml 序）存加载成功的 ``(Workflow, yaml_path)``；
    first-wins / 按 name 查找由消费方按序应用（与无缓存时的目录遍历序一致）。
    ``inventory`` = yaml 清单指纹；``watch_paths`` + ``dep_stamps`` = 依赖观察集
    （路径列表随缓存存——观察集是 entries 的确定函数，增删由目录 stat 戳感知，
    无需每次重新推导）及其 stat 戳（缺失路径记 ``None``，从无到有同样失配）。
    存后不再原地修改（替换式更新）。
    """

    inventory: tuple[Any, ...]
    watch_paths: tuple[str, ...]
    dep_stamps: tuple[tuple[str, tuple[int, int] | None], ...]
    entries: list[tuple[Workflow, str]]


def _stamp(p: Path) -> tuple[int, int] | None:
    """单路径 stat 戳 ``(mtime_ns, size)``；不可达 → ``None``（从无到有可感知）。

    ``NotADirectoryError``（路径中间组件是普通文件，如 ``subagents`` 意外为
    文件）并入缺失语义——该路径不可能成为有效观察对象，视为缺失而非报错；
    其他 OSError（权限等）上抛——由 caller 按既有 fail-soft 语义降级为直扫，
    不静默吞。
    """
    try:
        st = p.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (st.st_mtime_ns, st.st_size)


def _stat_stamps(
    paths: tuple[str, ...],
) -> tuple[tuple[str, tuple[int, int] | None], ...]:
    """观察集逐路径 stat 戳（顺序与 ``paths`` 一致，结果确定）。

    drvfs 单次 stat ~1-3ms，数百项串行要 0.5s+——大集合用**瞬时有界**线程池
    并行（纯 metadata IO，GIL 释放；非后台线程/定时器，不违反无轮询约束）；
    小集合串行省池开销。worker 内异常原样上抛（与串行语义一致）。
    """
    if len(paths) <= 64:
        return tuple((p, _stamp(Path(p))) for p in paths)
    with ThreadPoolExecutor(max_workers=8) as pool:
        stamps = list(pool.map(lambda p: _stamp(Path(p)), paths))
    return tuple(zip(paths, stamps))


def _build_inventory() -> tuple[Any, ...] | None:
    """构建 yaml 清单指纹；OSError → ``None``（无法可靠指纹，本次直扫不缓存）。

    结构：``(目录列表, (目录, yaml 清单 | None), ...)``——每目录一项，目录缺失记
    ``None``（出现 / 消失可感知）。首项目录列表含 cwd 派生路径，cwd 变化自然失配。
    """
    dirs = _workflow_dirs()
    parts: list[Any] = [tuple(str(d) for d in dirs)]
    try:
        for d in dirs:
            if not d.is_dir():
                parts.append((str(d), None))
                continue
            yaml_stats: list[tuple[str, int, int]] = []
            for y in scan_workflow_yamls(d):
                st = y.stat()
                yaml_stats.append(
                    (str(y.relative_to(d)), st.st_mtime_ns, st.st_size)
                )
            parts.append((str(d), tuple(yaml_stats)))
    except OSError as e:
        logger.warning("catalog: 缓存清单指纹构建失败（%s），本次直扫不缓存", e)
        return None
    return tuple(parts)


def _entry_watch_paths(wf: Workflow, yaml_path: str) -> list[Path]:
    """从**加载成功的** Workflow 反推 load 实际读取的依赖路径（观察集）。

    与 ``parser._resolve_agents`` / ``validator`` 的读取面一一对应：
      - resolver base 目录 ``agents/``：agent 新增 / 删除 / 优先级覆盖（目录 mtime，
        新 entry 出现在 base 内即改 base mtime）；
      - agent 入口 md（从物化产物 ``node.resources_root`` 反推）：内容变更；
      - 脚本引用（validator 同款正则）的父目录：``scripts/`` 下文件增删（存在性）；
      - subagents 双形态目录 + 其 ``*.md``：md 增删（目录 mtime）+ 内容变更。
    """
    d = Path(yaml_path).parent
    out: list[Path] = [d / "agents", d / "subagents", d / "subagents" / wf.name]
    for node, _is_body, _parent in _iter_agent_nodes(wf):
        root = getattr(node, "resources_root", None)
        if not root:
            continue  # 内联 prompt 节点（无 agent 引用），load 未读任何 agent 文件
        root = Path(root)
        # 双形态入口**都**进观察集（不靠 is_file 猜形态）：resources_root 两形态
        # 均为入口父目录（agents.py AgentHandle），文件夹形态入口 ``<name>/agent.md``
        # 与单文件形态入口 ``<base>/<name>.md`` 从 root 反推各有唯一候选，但用
        # is_file 探测会被 base 下散置的非法 ``agent.md`` 误判 → 真入口漏观察 →
        # 改写不失效。多观察一个恒缺失（或非法散置）路径的代价是每次多一次
        # stat，正确性优先。
        out.append(root / "agent.md")
        out.append(root / f"{node.agent or node.name}.md")
        for m in _AGENT_RESOURCE_SCRIPT_RE.finditer(node.prompt or ""):
            out.append((root / "scripts" / m.group(1)).parent)
    sub = resolve_subagents_dir(d, wf.name)
    if sub is not None:
        out.append(sub)
        out.extend(sub.glob("*.md"))
    return out


def _watch_set(entries: list[tuple[Workflow, str]]) -> list[Path]:
    """全部 entries 的依赖观察集（去重、路径序稳定）+ resolver 全局 base。"""
    seen: set[Path] = {Path.cwd() / "agents"}  # LocalPoolResolver 第二查找 base
    for wf, yaml_path in entries:
        seen.update(_entry_watch_paths(wf, yaml_path))
    return sorted(seen, key=str)


def _scan_started_before(
    stamps: tuple[tuple[str, tuple[int, int] | None], ...],
    scan_started: int,
) -> bool:
    """所有 stat 戳的写入时刻是否都不晚于扫描起点（mtime_ns ≤ ``scan_started``）。

    拦截「扫描期间（load 之后、依赖 stat 之前）依赖文件被改写」的固化竞态：
    此时 stamp 记新 mtime 而 entries 是旧内容，若照常缓存，下次校验 stat 与
    stamp 一致 → 命中 → 固化过期结果。不缓存即下次重扫，方向保守（最坏多一次
    重扫）。

    时钟前提：本守卫按「文件 mtime 时钟与本进程 wall clock 同源」论证——同源
    时 fs 时间戳粒度粗只可能把旧写记成更晚（误报 → 多重扫），无反向误差。跨
    时钟源写入（Windows 侧编辑器写 /mnt/d、WSL 侧 serve 读）依赖两侧时钟同步：
    偏斜最坏方向 = 守卫绕过（需「冷扫数秒窗口内并发保存 + 秒级偏斜」同时成立，
    极窄）或缓存回退（性能回到优化前，不损正确性之外的保证）。
    """
    return all(
        stamp is None or stamp[0] <= scan_started for _p, stamp in stamps
    )


def _failed_yaml_recovered(
    entries: list[tuple[Workflow, str]], inventory: tuple[Any, ...]
) -> bool:
    """inventory 中存在、但未进 entries 的 yaml（加载失败者）重试加载一次。

    任一恢复可载 → True（须重扫）；仍失败 → False（fail-soft 状态未变）。
    健康 catalog 零成本——闭坏 yaml「修复（不触碰 yaml 本身）却仍被隐藏」的盲区。
    """
    loaded = {yaml_path for _wf, yaml_path in entries}
    for dir_str, yaml_stats in inventory[1:]:
        if yaml_stats is None:
            continue
        for rel, _mtime, _size in yaml_stats:
            yaml_path = str(Path(dir_str) / rel)
            if yaml_path in loaded:
                continue
            try:
                load_workflow(yaml_path)
            except (ConfigurationError, Exception):  # noqa: BLE001
                continue  # 仍失败：fail-soft 保持
            return True  # 恢复可载：重扫
    return False


def _cache_valid(cache: _CatalogCache, inventory: tuple[Any, ...]) -> bool | None:
    """校验缓存是否仍反映当前文件系统；无法判定（OSError）→ ``None``（直扫）。

    yaml 清单未变 + 依赖观察集 stat 戳未变 + 坏 yaml 状态未变 → 有效。清单未变
    保证 cached entries 与当前 yaml 一一对应（同路径同 mtime/size = 同内容 →
    同解析产物）；观察集是 entries 的确定函数（路径列表随缓存存），增删由观察
    集内目录的 stat 戳感知——无需每次重新推导。
    """
    if cache.inventory != inventory:
        return False
    try:
        stamps = _stat_stamps(cache.watch_paths)
    except OSError as e:
        logger.warning("catalog: 缓存依赖校验失败（%s），本次直扫", e)
        return None
    if stamps != cache.dep_stamps:
        return False
    return not _failed_yaml_recovered(cache.entries, inventory)


def _scan_catalog() -> list[tuple[Workflow, str]]:
    """全量扫描 catalog 目录，按扫描序返回加载成功的 ``(Workflow, yaml_path)``。

    fail-soft：加载失败的 yaml log warning + 跳过（不中断整体扫描）。坏 yaml 不进
    entries，修复由清单变化或失败重试载（``_failed_yaml_recovered``）感知——缓存
    不固化坏状态。
    """
    entries: list[tuple[Workflow, str]] = []
    for d in _workflow_dirs():
        if not d.is_dir():
            continue
        try:
            yaml_paths = scan_workflow_yamls(d)
        except OSError:
            continue
        for yaml_path in yaml_paths:
            try:
                wf = load_workflow(yaml_path)
            except (ConfigurationError, Exception) as e:  # noqa: BLE001
                logger.warning(
                    "catalog: 跳过 %s（加载失败：%s）", yaml_path, e
                )
                continue
            entries.append((wf, str(yaml_path)))
    return entries


def _catalog_entries() -> list[tuple[Workflow, str]]:
    """``list_workflows`` / ``find_workflow`` 共用的缓存入口。

    缓存命中 → 直接复用 entries（O(stat)，不 parse yaml）；未命中 / 无法指纹 →
    全量重扫并替换缓存。并发安全：读写各持锁短临界区，重扫在锁外（并发重复扫
    无害——幂等，后写覆盖）。yaml 清单指纹在扫描**前** build（stat ≤ load），
    扫描期间 yaml 变化只会导致下次多一次重扫；依赖观察集 stat 戳在扫描**后**取，
    扫描期间的依赖写入由 ``_scan_started_before`` 校验拦截（否则 stamp 记新值 +
    entries 是旧内容 → 固化过期结果），不缓存即下次重扫，方向保守。
    """
    global _CACHE
    scan_started = time.time_ns()  # wall clock，与文件 mtime 同源
    inventory = _build_inventory()
    if inventory is not None:
        with _CACHE_LOCK:
            cache = _CACHE
        if cache is not None and _cache_valid(cache, inventory):
            return cache.entries
    entries = _scan_catalog()
    if inventory is not None:
        try:
            watch_paths = tuple(str(p) for p in _watch_set(entries))
            stamps = _stat_stamps(watch_paths)
        except OSError as e:
            logger.warning("catalog: 缓存依赖记录失败（%s），本次结果不缓存", e)
            return entries
        if not _scan_started_before(stamps, scan_started):
            logger.warning(
                "catalog: 扫描期间依赖文件被改写（stat 戳晚于扫描起点），"
                "本次结果不缓存，下次调用重扫"
            )
            return entries
        with _CACHE_LOCK:
            _CACHE = _CatalogCache(
                inventory=inventory,
                watch_paths=watch_paths,
                dep_stamps=stamps,
                entries=entries,
            )
    return entries


def _reset_cache() -> None:
    """清空进程内缓存（测试隔离用）。"""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def list_workflows() -> list[dict[str, Any]]:
    """扫描 catalog 目录，返回 workflow 元信息列表（SPEC §5.6 / §2.2）。

    每项字段：``{name, description, entry, inputs_count, inputs_schema}``。
    ``inputs_schema`` = ``[{name, type, description}]``，给消费者按需投影：``tars list`` /
    MCP ``list_workflows`` 经本字段；``orca <wf>``（不带 ``--inputs``）经 ``inputs_schema_list``
    直接调函数（不经本字段）。``orca list`` **不再**消费 inputs_schema（移至 ``orca <wf>``）。

    加载失败的 YAML 跳过（log warning，不中断列表）。经进程内缓存（见模块 docstring），
    元信息 dict 每次调用由缓存 entries 现建（调用方改返回值不污染缓存）。
    """
    seen: dict[str, dict[str, Any]] = {}
    for wf, _yaml_path in _catalog_entries():
        if wf.name in seen:
            continue  # first-wins（project-local 优先）
        seen[wf.name] = {
            "name": wf.name,
            "description": wf.description,
            "entry": wf.entry,
            "inputs_count": len(wf.inputs),
            # inputs_schema 留给消费者按需取（tars list / MCP list_workflows）；
            # orca list 不取此字段（移至 orca <wf>），orca <wf> 经 inputs_schema_list 直调。
            # 每项 {name, type, description}，从 wf.inputs 派生。
            "inputs_schema": inputs_schema_list(wf),
        }
    return list(seen.values())


def describe_workflow(wf: Workflow) -> dict[str, Any]:
    """从已加载的 ``Workflow`` 提取详查字典（SPEC §2.2）。

    返回字段：``{name, description, inputs_schema}``。``inputs_schema`` 是 dict
    ``{key: {type, required, description}}``，给 MCP describe_workflow 工具展示用。
    """
    return {
        "name": wf.name,
        "description": wf.description,
        "inputs_schema": _inputs_to_schema(wf),
    }


def find_workflow(name: str) -> tuple[Workflow, str] | None:
    """按 ``wf.name`` 查找并加载 workflow（SPEC §5.6 / §2.3 start_workflow 依赖）。

    **按 workflow name 字段匹配，不是文件名**：用户可把 ``setup_demo`` workflow
    存在 ``my_setup.yaml`` 里，catalog 按 ``wf.name`` 找到它。

    Returns ``(Workflow, yaml_path)`` 元组（DRY：避免 server 层重复扫 catalog 两次）。
    first-wins：project-local 优先于 user-global。未找到 → None。

    经进程内缓存（见模块 docstring）；命中时返回 Workflow 的**独立深拷贝**——
    调用方（start_workflow / orchestrator / web 路由）拿到等值新对象，改对象不
    污染缓存，语义与无缓存时逐次加载一致。
    """
    for wf, yaml_path in _catalog_entries():
        if wf.name == name:
            return (copy.deepcopy(wf), yaml_path)
    return None


def find_workflow_by_name(name: str) -> Workflow | None:
    """按 ``wf.name`` 查找并加载 workflow（薄 wrapper，SPEC §5.6）。

    仅返 Workflow（不需要 yaml_path 的调用方用）。需要 yaml_path 的场景调
    ``find_workflow`` 取元组（DRY，避免重复扫 catalog）。
    """
    result = find_workflow(name)
    return result[0] if result is not None else None


def find_workflow_yaml_path(name: str) -> str | None:
    """按 ``wf.name`` 反查 yaml_path（薄 wrapper）。"""
    result = find_workflow(name)
    return result[1] if result is not None else None


def _inputs_to_schema(wf: Workflow) -> dict[str, dict[str, Any]]:
    """wf.inputs → JSON-schema 友好的 ``{key: {type, required, description, enum}}`` 字典。"""
    out: dict[str, dict[str, Any]] = {}
    for key, idef in wf.inputs.items():
        out[key] = {
            "type": idef.type,
            "required": idef.required,
            "description": idef.description,
            # SPEC 2026-08-11-inputdef-enum §3.2 D2：始终透出 enum 键（None 透出 None）。
            # 消费者（MCP describe_workflow → claude 选 inputs）``.get("enum")`` 单态，
            # 避免「键在/不在」二态分支；值域提示让 LLM 选 inputs 时约束在合法集。
            "enum": idef.enum,
        }
        if idef.default is not None:
            out[key]["default"] = idef.default
    return out


def inputs_schema_list(wf: Workflow) -> list[dict[str, Any]]:
    """wf.inputs → ``[{name, type, description, enum}, ...]`` 列表。

    给 ``orca <wf>``（不带 ``--inputs``）返回的 ``inputs_schema``：skill/LLM 选定 wf 后据此
    从用户意图抽 inputs（schema 是"启动 wf 时"才需要的信息，故不进 ``orca list``）。与
    ``_inputs_to_schema``（dict 形态，给 MCP describe_workflow 用）并存——两者面向不同消费者、
    形态不同（list 带 name vs dict keyed），非重复逻辑。
    """
    return [
        {
            "name": key,
            "type": idef.type,
            "description": idef.description,
            # SPEC 2026-08-11-inputdef-enum §3.2 D2：始终透出 enum 键（None 透出 None）。
            # 消费者（cli._validate_inputs）统一 ``field_def.get("enum")``，无 enum 时 None。
            "enum": idef.enum,
        }
        for key, idef in wf.inputs.items()
    ]
