"""run_manager.py —— 多 run 真并发托管 + 懒加载元数据（SPEC §2）。

回答「后端怎么托管多个并发 run？」：每个 run 一个独立 ``RunHandle``（bus + tape +
gate_handler 全隔离），``RunManager`` 用 ``asyncio.Semaphore(max_concurrent)`` 真并发
跑（默认 3），超过的 queued。``list_runs`` 只返回**元数据**（不含事件，懒加载红线），
元数据从 ``replay_state(tape)`` 派生（保证与唯一真相源一致）。

设计规则（SPEC §0.1 五条铁律 / §2.3 / §9 决策）：
  - **每个 run 独立 bus + tape + gate_handler**（隔离：多 run 不串事件/gate，§9 决策 5）。
  - **真并发**：``_sem`` 限制同时跑的 run 数，sem 内 ``asyncio`` 自然并发（不是单活跃）。
  - **元数据从 tape 派生**：progress/cost 不另存，从 ``replay_state(handle.tape)`` 算
    （§9 决策 6，保证与真相源一致——断言覆盖）。
  - **懒加载**：``list_runs`` 只 ``RunMeta``，事件走 ``get_run_events`` → ``tape.replay()``
    （§0.1 铁律 2）。
  - **后端无并行内存事件 list**：本模块不维护 ``events: list``（§0.1 铁律 1 / §0.2 反模式①）。
  - **生命周期干净**：run 终态时 ``gate_handler.stop()`` + ``bus.close()``，无 leaked task。

依赖单向：本模块依赖 ``orca.{run, gates, events, schema, compile}``，不被任何模块 import
（SPEC §0.1 铁律 5）。不含编排/gate 决策逻辑——``Orchestrator.run()`` 才是编排，本模块只托管。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict

from orca.compile import ConfigurationError, load_workflow
from orca.iface.cli.config import apply_kb_requirement
from orca.chart._limits import SOCK_PATH_MAX
from orca.chart._paths import chart_sock_path
from orca.events.bus import EventBus
from orca.events.chart_ingestor import chart_ingestor, make_crash_callback
from orca.events.replay import apply_event, replay_state
from orca.events.tape import Tape
from orca.events.tape_reader import (
    count_and_bounds,
    replay as tape_reader_replay,
    since_limited,
    tail_events,
)
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.pending import pending_gates_from_tape
from orca.gates.types import HumanGate
from orca.run.lifecycle import gen_run_id
from orca.run.orchestrator import Orchestrator
from orca.runtime import (
    detect_project_root,
    is_registered_runs_dir,
    list_registered,
    register_project,
)
from orca.schema import Event, RunState, Workflow

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

RunStatus = Literal[
    "queued", "running", "completed", "failed", "cancelled", "live-pending"
]
RunSource = Literal["in-process", "attached"]


# ── RunView 只读协议 + 双实现（SPEC §0 D6 / §2.3）──────────────────────────────
# 单 registry ``_runs: dict[str, RunView]`` 同时容纳 in-process 与 attached。读路径
# （``get_run_events`` / ``get_run_meta`` / WS）经 RunView 统一访问；写路径分支
# （attached 不写 disk，见 ``EventBus.relay`` + ``AttachedTape``）。


class AttachedTape:
    """外部 tape 文件的只读视图（SPEC §0 D2 / §1 铁律 6）。

    暴露与 ``Tape`` 同形的 ``replay(since_seq)`` / ``last_seq()`` / ``close()`` 读 API，
    供 routes/ws_handler 经 ``handle.tape.replay(...)`` 读外部 tape（无需感知 in-process
    vs attached 差异）。**永不写**：``append`` 不可达（attacher 无写权，事件已在宿主进程
    的 tape 持久化，follow task 走 ``EventBus.relay`` 纯转发）。
    """

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id

    def replay(self, since_seq: int = 0) -> Iterator[Event]:
        """read-only 流式（委托 ``tape_reader.replay``，永不写）。"""
        yield from tape_reader_replay(self.path, since_seq=since_seq)

    def last_seq(self) -> int:
        """扫文件取 max seq（无写权，仅只读扫）。"""
        _, _, newest = count_and_bounds(self.path)
        return newest

    def close(self) -> None:
        """no-op（read-only，无写句柄）。"""

    def append(self, _event_data: dict) -> int:  # pragma: no cover - 防御性 dead path
        """永不调用：attached run 的事件落盘在宿主进程；本进程只读。"""
        raise RuntimeError(
            "AttachedTape.append 不可达——attached run 的事件落盘在宿主进程；"
            "follow task 应调 EventBus.relay（仅 fan-out）"
        )


@dataclass
class RunView:
    """run 只读协议（SPEC §0 D6 / §2.3）：单 registry 中 in-process 与 attached 共用基类。

    子类（``InProcessRunHandle`` / ``AttachedRunHandle``）补足各自字段。读路径只依赖
    本基类的 ``run_id`` / ``bus`` / ``tape`` / ``status`` / ``source``。
    """

    run_id: str
    bus: EventBus
    tape: Tape | AttachedTape
    status: RunStatus = "queued"
    source: RunSource = "in-process"
    error: str | None = None
    started_at: float = field(default_factory=time.time)


@dataclass
class InProcessRunHandle(RunView):
    """in-process run 句柄（同 v2 行为，SPEC §0 D6）。orchestrator + gate + chart ingestor
    全在进程内。``source="in-process"``，``writable=True``。"""

    source: RunSource = field(default="in-process", init=False)
    wf: Workflow | None = None
    gate_handler: HumanGateHandler | None = None
    # run task（``_run_with_sem`` 创建）；``wait_done`` await 它。
    _task: asyncio.Task | None = field(default=None, repr=False)
    # gate_handler 是否已 start（收尾时只 stop 已 start 的，幂等）。
    _gate_started: bool = field(default=False, repr=False)
    # phase-13 §3.1：per-run chart ingestor task（script → emit custom(chart) → tape）。
    # ``resume=True`` 重开模式不起（SPEC §3.1 YAGNI）。teardown 时 cancel + unlink socket。
    _chart_ingestor: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class AttachedRunHandle(RunView):
    """attached run 句柄（SPEC §0 D6 / §2.3）：read-only + follow task + 终态/损毁追踪。

    - ``source="attached"``、``tape=AttachedTape(path)``、``tape_path`` 外部 tape 文件。
    - ``follow_task``：asyncio 任务，轮询 mtime/size 增量 → parse → ``bus.relay``。
    - ``terminal``：None=未终态；``True``=收尾正常终态事件；``"corrupted"``=inode/truncate 损毁。
    - **无 ``wf`` / ``gate_handler`` / ``chart_ingestor``**（attached 不跑编排）。
    """

    source: RunSource = field(default="attached", init=False)
    tape_path: Path = field(default=Path())
    follow_task: asyncio.Task | None = field(default=None, repr=False)
    terminal: bool | str | None = False


# 向后兼容别名：v2 代码引用 ``RunHandle``，本 refact 后等价于 in-process 实现。
RunHandle = InProcessRunHandle


@dataclass
class RunMeta:
    """懒加载列表项：**只有元数据，不含事件**（SPEC §2.2 / §0.1 铁律 2）。

    ``progress`` 形如 ``"3/7"``（done/total，从 ``replay_state`` 派生）。
    """

    run_id: str
    workflow_name: str
    status: RunStatus
    progress: str
    cost: float
    elapsed: float
    error: str | None


class RunSummary(BaseModel):
    """跨项目 discovery 返回项（SPEC §13.2 M-5 / §5.2 D5）。

    Pydantic ``extra="forbid"``：schema 白名单 validator（拒 events/其它字段，AC3 反向 fixture）。
    ``response_model_exclude_unset=True`` 让 fastapi 序列化时省略未设字段（如 legacy run 无 project_id）。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_name: str
    project_id: str | None = None
    project_name: str | None = None
    status: RunStatus
    progress: str = "?"
    cost: float = 0.0
    elapsed: float = 0.0
    started_at: float | None = None
    event_count: int = 0
    source: Literal["in-process", "attached", "legacy"] = "in-process"


class RunManager:
    """托管多个并发 run（SPEC §2.1）。

    用法::

        manager = RunManager(max_concurrent=3)
        run_id = await manager.start_run("wf.yaml", {}, None, None)
        metas = manager.list_runs()           # 元数据，无事件
        events = manager.get_run_events(run_id)  # 懒加载全量（tape.replay）
        handle = manager.get_handle(run_id)
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        *,
        runs_dir: Path | str = "runs",
        registry: SessionContextRegistry | None = None,
    ):
        self._max_concurrent = max_concurrent
        # 单 registry（SPEC §1 铁律 5 / §0 D6）：in-process 与 attached 同表。
        self._runs: dict[str, RunView] = {}
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._runs_dir = Path(runs_dir)
        # 共享 registry：claude session_id → (run_id, node)。多 run 的 gate 路由从这里
        # 反查 run_id（routes/gate.py 的多 run 分发依赖它）。
        self._registry = registry or SessionContextRegistry()
        # meta memoize（SPEC §8.4a perf）：key=(path, mtime, size)，val=(count, oldest,
        # newest, overview_data)。hit 时 O(1)；mtime/size 变则失效。客户端高频轮询 /meta
        # 不重算 fold。无上限（typical: 单 digit run 数 × 1 entry = 几条），YAGNI 不加 LRU。
        self._meta_cache: dict[tuple[str, float, int], tuple[int, int, int, dict | None]] = {}
        # SPEC §13.3 P0：持久派生缓存（cache 非 index，可删可重建，不违 R1/§9）。
        # 按runs_dir（每个注册项目）懒加载到内存；miss/hit 写回单条 entry。损坏 → warn + 重建。
        # 结构：``{runs_dir: {"version":1, "entries": {<tape_name>: {mtime,size,count,oldest,newest,overview}}}}``。
        self._persistent_cache_by_runs_dir: dict[Path, dict] = {}
        # SPEC §13.2 M-12：run_id → (project_id, tape_path, project_name) per-process 索引，
        # 每次 ``GET /api/runs?scope=all`` discovery 重建。``resolve_run_path`` miss 时查此索引。
        self._run_path_index: dict[str, tuple[str | None, Path, str | None]] = {}
        # SPEC §13.1 U-3 / §13.2 B-4：WS 控制帧广播回调列表。delete/cancel/attach 时调用，
        # 让所有 WS 通过自身出站 queue 串行化发送 ``run_changed`` 控制帧。
        # 回调签名：``Callable[[str, str], None]`` → (run_id, action)。
        self._run_changed_listeners: list = []

    # ── 公开 API ───────────────────────────────────────────────────────────

    @property
    def registry(self) -> SessionContextRegistry:
        """共享 SessionContextRegistry（routes/gate.py 多 run 分发用）。"""
        return self._registry

    @property
    def runs_dir(self) -> Path:
        """runs 资源根目录（SPEC §0 D10：assets 路由据此解析 run-scoped 资源）。

        只读暴露：routes 层 ``GET /api/runs/<id>/assets/<path>`` 用 ``runs_dir / run_id /
        assets / <rel>`` 解析，受 ``_resolve_asset_path`` 守卫防越界。
        """
        return self._runs_dir

    def resolve_asset_path(self, run_id: str, rel_path: str) -> Path | None:
        """解析 run 私有 asset 路径（SPEC §0 D10）。

        - 未知 run_id → None（routes 层 404）
        - 路径越界（``..`` / 绝对路径 escape）→ None（fail loud 404）
        - 文件不存在 → None
        - 合法且存在 → 返回 resolved absolute path

        单一职责：本方法只做路径解析 + 越界守卫；IO 读字节流由 routes 层 FileResponse 负责。
        """
        if self._runs.get(run_id) is None:
            return None
        assets_root = (self._runs_dir / run_id / "assets").resolve()
        decoded = rel_path.strip()
        if not decoded:
            return None
        # 注意：``.resolve()`` 会跟随 symlink，故先在未 resolve 的路径上 check symlink
        # （否则 ``candidate`` 已是 symlink 目标，``is_symlink()`` 必 False）。
        unresolved = assets_root / decoded
        if unresolved.is_symlink():
            return None
        candidate = unresolved.resolve()
        try:
            candidate.relative_to(assets_root)
        except ValueError:
            return None
        # 二次 check（防御纵深：路径中某段可能是 symlink，unresolved 末端未指向但中间段是）
        # ——此处 ``candidate`` 已 resolve，是真实物理路径；若与 unresolved 不同且非末端 symlink，
        # 上面未 resolve 检查已拦下。再加 ``candidate.is_symlink()`` 兜底中间段 symlink。
        if candidate.is_symlink():
            return None
        if not candidate.is_file():
            return None
        return candidate

    async def start_run(
        self,
        yaml_path: str | Path,
        inputs: dict | None = None,
        task: str | None = None,
        max_iter: int | None = None,
        *,
        resume: bool = False,
        project_path: str | None = None,
    ) -> str:
        """启动一个 run（后台 task，不阻塞）。返回 run_id。

        - 加载 + 校验 workflow（``ConfigurationError`` 透传给调用方 → routes 层 400）。
        - 构造独立 tape + bus + gate_handler + RunHandle。
        - 创建后台 task ``_run_with_sem``（sem 内并发 + 状态机）。
        - phase-13 §3.1：非 resume 模式起 per-run chart ingestor（``runs/<run_id>.sock``）。

        SPEC §13.2 B-1：``project_path`` **keyword-only 可选**。缺省时 manager 内调
        ``detect_project_root()`` + ``register_project()`` 自填。**既有调用零改**（无 project_path
        仍正常工作——cli 启动的 in-process run 走 detect 自填，web ``POST /api/run`` body 必填）。

        Args:
            resume: phase-3 §3.5 resume 模式（重开已存在 tape）。True → **不起 chart ingestor**。
            project_path: 项目根（绝对路径）。缺省 → ``detect_project_root()`` 自填。
        """
        wf = load_workflow(Path(yaml_path))
        # plan sprightly-questing-donut §1.4：requires knowledge_base 时预检 KB（web/MCP 生产路径，
        # 与 orca run / in_session bootstrap 同款；缺 KB → ConfigurationError 由 web 层转 HTTP 错误）。
        apply_kb_requirement(wf)
        run_id = gen_run_id(wf.name)
        # SPEC §13.2 B-1：project_path 缺省 → detect + register。失败（无 project marker 等）
        # 不阻断 run（仍落默认 runs_dir）—— 既有 in-process 路径不强依赖注册（兼容）。
        # 但若显式传入则要求 register 成功（fail loud，让 web POST /api/run 错误能 propagate）。
        resolved_project = self._resolve_project_path_for_run(project_path)
        if resolved_project is not None:
            runs_dir_for_run = resolved_project / "runs"
            runs_dir_for_run.mkdir(parents=True, exist_ok=True)
        else:
            runs_dir_for_run = self._runs_dir
        tape_path = runs_dir_for_run / f"{run_id}.jsonl"
        tape = Tape(tape_path, run_id=run_id, resume=resume)
        bus = EventBus(tape)
        gate_handler = HumanGateHandler(bus)
        handle = InProcessRunHandle(
            run_id=run_id,
            wf=wf,
            bus=bus,
            tape=tape,
            gate_handler=gate_handler,
            status="queued",
        )
        # phase-13 §3.1：起 per-run chart ingestor（resume 模式不起，SPEC §3.1）。
        # sock_path 与 start_run 生命周期一致：teardown 时 cancel + unlink。
        if not resume:
            # phase-13 §7.7（2026-07-08 短路径化）：socket 走 ``<tmp>/orca-<hash>.sock``
            # （``chart_sock_path``），与 runs 目录解耦——runs 可能是深服务器路径致 sun_path
            # 超限。tape/jsonl/prompts 仍在 runs 目录不变。两端（此处 bind + script env）同源。
            sock_path = chart_sock_path(run_id)
            resolved = str(sock_path.resolve())
            if len(resolved) > SOCK_PATH_MAX:
                # 防御性兜底：temp 目录路径正常远短于上限；若 TMPDIR 异常长仍 fail loud，
                # 避免 asyncio.start_unix_server 抛 OSError 触发 crash callback 无限重起。
                raise RuntimeError(
                    f"socket path 过长（{len(resolved)} > {SOCK_PATH_MAX} 字节）："
                    f"{resolved!r}。请改 TMPDIR env 到短路径。"
                )
            handle._chart_ingestor = asyncio.create_task(
                chart_ingestor(sock_path, bus, run_id),
                name=f"orca-chart-ingestor-{run_id}",
            )
            handle._chart_ingestor.add_done_callback(
                make_crash_callback(sock_path, bus, run_id)
            )
        async with self._lock:
            self._runs[run_id] = handle
        handle._task = asyncio.create_task(
            self._run_with_sem(handle, inputs or {}, task, max_iter),
            name=f"orca-web-run-{run_id}",
        )
        return run_id

    # ── X — attach by tape path（SPEC web-attach §2 / §6）───────────────────────

    def resolve_tape_path(self, tape_path: str) -> Path:
        """安全解析 tape 路径（SPEC web-attach §6 三重守卫）。

        守卫顺序：
          1. **lstat 先行**：``raw.is_symlink()`` → 拒（path 中某段是 symlink）
          2. **resolve() + relative_to(runs_dir)**：用 ``relative_to`` 非 ``startswith``
             （防 ``runs_evil`` 前缀碰撞）；命中 ``ORCA_WEB_TAPE_ALLOWLIST`` 等价放行。
          3. **post-resolve re-check**：再 ``resolve()`` / ``is_symlink()`` 确认无逃逸。
          4. **open + fd re-stat 防 TOCTOU**：``os.open(O_RDONLY|O_NOFOLLOW)`` + ``fstat``
             对比 resolved 的 stat，不一致 → 拒（race 中被替换）。

        相对路径相对 CWD（与 ``orca run`` 写 tape 一致）。不符 → ``PermissionError``；
        不存在 → ``FileNotFoundError``。routes 层统一映射为 403 / 404。

        与 ``resolve_asset_path`` 等价强度（同三重守卫，新增 fd re-stat）。
        """
        raw = Path(tape_path)
        # 1. lstat 先行：在 resolve() 之前 check（resolve 会跟随 symlink）。
        if raw.is_symlink():
            raise PermissionError(f"tape_path 含 symlink（拒）：{tape_path}")
        try:
            resolved = raw.resolve()
        except (OSError, RuntimeError) as e:
            # resolve 失败（loop / 权限）→ fail loud
            raise PermissionError(f"tape_path resolve 失败：{tape_path} ({e})") from e

        runs_dir_resolved = self._runs_dir.resolve()
        allowed = False
        # 2a. relative_to(runs_dir)（非 startswith——防 ``runs_evil`` 前缀碰撞）
        try:
            resolved.relative_to(runs_dir_resolved)
            allowed = True
        except ValueError:
            pass
        # 2b. ORCA_WEB_TAPE_ALLOWLIST（os.pathsep 分隔绝对前缀）
        if not allowed:
            allowlist = os.environ.get("ORCA_WEB_TAPE_ALLOWLIST", "")
            for prefix in allowlist.split(os.pathsep):
                prefix = prefix.strip()
                if not prefix:
                    continue
                try:
                    prefix_resolved = Path(prefix).resolve()
                except (OSError, RuntimeError):
                    continue
                try:
                    resolved.relative_to(prefix_resolved)
                    allowed = True
                    break
                except ValueError:
                    continue
        # 2c. 注册表 allowlist（SPEC §13.2 B-3）：tape 在任一注册项目 ``runs/`` 子树下即放行。
        if not allowed and self.is_allowed_tape_path(resolved):
            allowed = True
        if not allowed:
            raise PermissionError(f"tape_path out of bounds: {tape_path}")

        # 3. post-resolve re-check：防 allowlist 内某段是 symlink 指出。
        if raw.is_symlink() or resolved.is_symlink():
            raise PermissionError(
                f"tape_path post-resolve 检出 symlink（拒）：{tape_path}"
            )
        if not resolved.is_file():
            raise FileNotFoundError(f"tape not found: {resolved}")

        # 4. open + fd re-stat 防 TOCTOU：原子 open(O_NOFOLLOW) 后从 fd 取真实 inode，
        #    与 resolved 的 stat 对比；race 中被替换 → 不一致 → 拒。
        try:
            fd = os.open(str(resolved), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            raise PermissionError(
                f"tape_path open(O_NOFOLLOW) 失败（可能 symlink/权限）：{tape_path} ({e})"
            ) from e
        try:
            fd_stat = os.fstat(fd)
        finally:
            os.close(fd)
        try:
            resolved_stat = resolved.stat()
        except OSError as e:
            raise PermissionError(
                f"tape_path post-open stat 失败：{tape_path} ({e})"
            ) from e
        if fd_stat.st_ino != resolved_stat.st_ino or fd_stat.st_dev != resolved_stat.st_dev:
            raise PermissionError(
                f"tape_path TOCTOU 检测：fd inode 与 resolved inode 不一致（{tape_path}）"
            )
        return resolved

    async def attach_run(
        self,
        tape_path: str,
        run_id: str | None = None,
    ) -> str:
        """X — attach a run by tape path（SPEC §2 / §0 D6）。

        read-only + stream-on-demand + tail-follow：
          1. ``resolve_tape_path`` 安全校验（§6）→ 不符 raise ``PermissionError``/``FileNotFoundError``。
          2. **不 bulk replay**：只读探测首行，判 ``workflow_started``；partial / 空 → ``live-pending``。
          3. run_id：入参 > 首行 workflow_started.data.run_id > 文件名 stem。
          4. **run_id 碰撞**（已在 ``_runs``）→ ``ValueError("run_id_collision")``；route 层 409。
          5. **同 tape_path 重复 attach** → 幂等返回既有 handle.run_id（不重起 follow）。
          6. 注册 ``AttachedRunHandle(bus, AttachedTape(path), follow_task)`` → 起 follow task。
          7. follow task：asyncio poll 0.3s mtime/size 增量 → split newline → 每整行 parse →
             ``bus.relay(event)``（fan-out only，M1 single write path）。终态事件 →
             ``terminal=True`` + 停 follow（留 registry）。inode 变化 / size 缩小 →
             ``terminal="corrupted"`` + emit error 事件到 bus。
        """
        resolved = self.resolve_tape_path(tape_path)

        # **initial_offset 必须在 probe/scan 之前捕获**（MAJOR 4 修复）：probe 内部读当时
        # 的 EOF；若 probe 之后 stat，[probe_EOF, stat_EOF] 间外部写者追加的字节会被
        # 静默丢（probe 不见 + follow 从 stat_EOF 起不见）。stat 在前 → follow 从这个早期
        # size 起，能覆盖 probe 期间的写入（重复部分由客户端 seq 去重吸收）。
        try:
            pre_probe_size = resolved.stat().st_size
        except OSError:
            pre_probe_size = 0

        # 单次扫：同时取 first_event + existing_terminal（MAJOR 6 修复，避免两次全扫）。
        first_event, existing_terminal = _probe_head_and_terminal(resolved)

        # SPEC §6.7 / §2.2 step2 / §8 AC9：首行**完整可解析**但非 ``workflow_started``
        # → 立即 403 ``not-orca-tape``（routes 层 PermissionError → 403）。partial / 空
        # 首行（``first_event is None``）走 live-pending → 5s → corrupted 路径，不在此拒。
        if first_event is not None and first_event.type != "workflow_started":
            raise PermissionError(
                f"not-orca-tape: first complete line is '{first_event.type}', "
                f"expected 'workflow_started'"
            )

        if run_id is None:
            if (
                first_event is not None
                and first_event.type == "workflow_started"
            ):
                rid_in_data = first_event.data.get("run_id")
                wf_name = first_event.data.get("workflow_name")
                run_id = (
                    str(rid_in_data)
                    if isinstance(rid_in_data, str) and rid_in_data
                    else (
                        str(wf_name)
                        if isinstance(wf_name, str) and wf_name
                        else resolved.stem
                    )
                )
            else:
                run_id = resolved.stem

        async with self._lock:
            # 幂等检查在锁内（MAJOR 5 修复）：避免并发同 tape_path 双注册。
            for h in self._runs.values():
                if (
                    isinstance(h, AttachedRunHandle)
                    and h.tape_path.resolve() == resolved
                ):
                    return h.run_id
            if run_id in self._runs:
                raise ValueError(f"run_id_collision: {run_id}")
            tape = AttachedTape(resolved, run_id)
            # attached bus 持 read-only AttachedTape；emit 不可达（relay 走 fan-out only）。
            bus = EventBus(tape)
            # 终态 tape → status=completed/failed/cancelled；无需起 follow（无新事件）。
            if existing_terminal is not None:
                status: RunStatus = (
                    "completed"
                    if existing_terminal == "workflow_completed"
                    else "failed"
                    if existing_terminal == "workflow_failed"
                    else "cancelled"
                )
                handle = AttachedRunHandle(
                    run_id=run_id,
                    bus=bus,
                    tape=tape,
                    tape_path=resolved,
                    status=status,
                    terminal=True,
                )
                self._runs[run_id] = handle
                return run_id
            handle = AttachedRunHandle(
                run_id=run_id,
                bus=bus,
                tape=tape,
                tape_path=resolved,
                status="live-pending" if first_event is None else "running",
                terminal=False,
            )
            self._runs[run_id] = handle

        # 起 follow task（轮询外部 tape 增量 → bus.relay）。
        # D3 stream-on-demand：不 bulk replay。first_event 为 None（partial/empty）时
        # 从 offset=0 起（含 partial 行，待 writer 补完后整行 parse）；否则从 pre_probe_size
        # 起（probe 前的 size，覆盖 probe 期间写入，重复由 client seq 去重吸收）。
        if first_event is None:
            initial_offset = 0
        else:
            initial_offset = pre_probe_size
        # probe_validated：probe 已确认首行是 ``workflow_started``（upfront reject 保证了
        # first_event 非 None 时必为 wf-started）。显式传入 follow，避免旧版 ``initial_offset > 0``
        # 推断式 bypass——那条 bypass 在 upfront reject 加入前会误把「complete non-wf-started
        # 首行」标成已验过，从而跳过 5s→not-orca-tape 拒绝路径（AC9 / §6.7）。
        probe_validated = first_event is not None
        handle.follow_task = asyncio.create_task(
            self._follow_tape(handle, resolved, initial_offset, probe_validated),
            name=f"orca-web-attach-follow-{run_id}",
        )
        return run_id

    async def _follow_tape(
        self,
        handle: AttachedRunHandle,
        path: Path,
        initial_offset: int,
        probe_validated: bool,
    ) -> None:
        """follow task：轮询外部 tape mtime/size 增量 → split newline → bus.relay。

        SPEC §2.2 step4-5：
          - poll 0.3s（POSIX，跨平台简单实现；kqueue 是可选优化，YAGNI）。
          - 从上次 offset 起 read 增量；按 ``\\n`` 切分，残留不足一行入 buffer 等下次 poll。
          - 每整行 parse → ``bus.relay``（保留外部 seq）。
          - **inode 变化**（rename/move/rotate）/ **size 缩小**（truncate）→ 停 follow +
            ``terminal="corrupted"`` + emit ``error`` 事件到 bus（让客户端看到错）。
          - 终态事件（workflow_completed/failed/cancelled）→ ``terminal=True`` + 停 follow
            （handle 留 registry 供历史查询）。
        """
        offset = initial_offset
        buffer = ""
        last_size = initial_offset  # D3：初始 size = initial_offset（不视初始内容为 truncate）
        # 显式标志（AC9 / §6.7）：probe 已验过首行 wf-started → follow 信任，不重复校验；
        # 否则 follow 必须等到首个 ``workflow_started`` 才升级 running，5s 仍无 → not-orca-tape。
        # 旧版 ``initial_offset > 0`` 推断式 bypass 已废（upfront reject 后语义等价但意图不显）。
        seen_first_valid = probe_validated
        # MAJOR 3 修复：循环外 open 一次，循环内 seek+read；fd 用 fstat 拿更原子的 inode。
        try:
            f = open(path, "r", encoding="utf-8")
        except FileNotFoundError:
            handle.terminal = "corrupted"
            handle.status = "failed"
            handle.error = "tape file disappeared"
            logger.warning("run %s follow: tape 文件消失 %s", handle.run_id, path)
            await self._emit_attach_error(
                handle, "tape_file_disappeared", f"tape file vanished: {path}"
            )
            return
        try:
            try:
                # 记 fd 的稳定 inode（fd 跟随 open 时的文件实例，即使 path 被 rename 也指向原文件）
                fd_stat = os.fstat(f.fileno())
                fd_inode = fd_stat.st_ino
                fd_dev = fd_stat.st_dev
                # 等首行可解析（live-pending → running）。5s 仍无 → corrupted（routes 层 403）。
                first_line_deadline = time.time() + 5.0
                while True:
                    await asyncio.sleep(0.3)
                    # 用 path.stat() 取「当前 path 指向的」inode（rotation 后会变）；
                    # fd 自身的 inode 永远不变（fd 锚定 open 时的文件实例）。两者对比即可识别 rotate。
                    try:
                        path_st = path.stat()
                    except FileNotFoundError:
                        # path 消失（外部 unlink / 移走）→ corrupted
                        handle.terminal = "corrupted"
                        handle.status = "failed"
                        handle.error = "tape file disappeared"
                        logger.warning(
                            "run %s follow: path 消失 %s", handle.run_id, path
                        )
                        await self._emit_attach_error(
                            handle, "tape_file_disappeared", f"tape path vanished: {path}"
                        )
                        return

                    # inode 变化（path 现指向不同 inode）→ rename/move/rotate
                    if path_st.st_ino != fd_inode or path_st.st_dev != fd_dev:
                        handle.terminal = "corrupted"
                        handle.status = "failed"
                        handle.error = "tape inode changed (rotate/rename)"
                        logger.warning(
                            "run %s follow: inode 变化 %s (path=%s, fd=%s)",
                            handle.run_id,
                            path,
                            path_st.st_ino,
                            fd_inode,
                        )
                        await self._emit_attach_error(
                            handle,
                            "tape_inode_changed",
                            f"tape rotated/moved: {path}",
                        )
                        return

                    # size 缩小 → truncate（fd 当前 size 用 fstat 拿，反映 fd 真实文件状态）
                    try:
                        cur_fd_st = os.fstat(f.fileno())
                    except OSError:
                        cur_fd_st = path_st
                    cur_size = cur_fd_st.st_size
                    if cur_size < last_size:
                        handle.terminal = "corrupted"
                        handle.status = "failed"
                        handle.error = "tape truncated"
                        logger.warning(
                            "run %s follow: size 缩小 %s (%s→%s)",
                            handle.run_id,
                            path,
                            last_size,
                            cur_size,
                        )
                        await self._emit_attach_error(
                            handle,
                            "tape_truncated",
                            f"tape shrank: {path} ({last_size}→{cur_size})",
                        )
                        return

                    last_size = cur_size

                    # 读增量（从 offset 到 EOF）
                    if cur_size == offset:
                        # 无新字节；若仍 live-pending 且超 5s → corrupted（not-orca-tape）
                        if not seen_first_valid and time.time() > first_line_deadline:
                            handle.terminal = "corrupted"
                            handle.status = "failed"
                            handle.error = "not-orca-tape"
                            logger.warning(
                                "run %s follow: 5s 仍无 workflow_started %s",
                                handle.run_id,
                                path,
                            )
                            await self._emit_attach_error(
                                handle,
                                "not_orca_tape",
                                f"5s no workflow_started: {path}",
                            )
                            return
                        continue

                    try:
                        f.seek(offset)
                        chunk = f.read(cur_size - offset)
                    except OSError as e:
                        logger.warning(
                            "run %s follow: 读增量失败 %s: %s",
                            handle.run_id,
                            path,
                            e,
                        )
                        # 读失败不致命（下次 poll 重试）；记 warning。
                        continue

                    offset = cur_size
                    buffer += chunk
                    # 按 newline 切分；末尾不足一行（无 \\n）入 buffer 等下次。
                    lines = buffer.split("\n")
                    buffer = lines.pop()  # 末尾残留

                    for raw in lines:
                        stripped = raw.strip()
                        if not stripped:
                            continue
                        try:
                            obj = json.loads(stripped)
                            event = Event(**obj)
                        except (json.JSONDecodeError, Exception) as e:  # noqa: BLE001
                            # 残行 / 校验失败 → 跳过（不污染 bus）；记 warning。
                            logger.warning(
                                "run %s follow: 行 parse 失败跳过 %s: %s",
                                handle.run_id,
                                stripped[:80],
                                e,
                            )
                            continue

                        if not seen_first_valid:
                            # AC9 / §6.7：首个完整事件必须是 ``workflow_started``。
                            # - 是 wf-started → 升级 running + relay。
                            # - 非 wf-started（partial 首行写完后发现是别的类型）→ 立即
                            #   ``not-orca-tape`` 拒（不 relay、不待 5s 空闲超时——事件一旦
                            #   relay 会污染客户端 fold）。
                            if event.type == "workflow_started":
                                seen_first_valid = True
                                handle.status = "running"
                            else:
                                handle.terminal = "corrupted"
                                handle.status = "failed"
                                handle.error = "not-orca-tape"
                                logger.warning(
                                    "run %s follow: 首个完整事件非 workflow_started "
                                    "(type=%s) → not-orca-tape",
                                    handle.run_id,
                                    event.type,
                                )
                                await self._emit_attach_error(
                                    handle,
                                    "not_orca_tape",
                                    f"first complete event is '{event.type}', "
                                    f"expected 'workflow_started': {path}",
                                )
                                return
                        await handle.bus.relay(event)

                        # 终态事件 → terminal=True + 停 follow（留 registry）
                        if event.type in (
                            "workflow_completed",
                            "workflow_failed",
                            "workflow_cancelled",
                        ):
                            handle.terminal = True
                            handle.status = (
                                "completed"
                                if event.type == "workflow_completed"
                                else "failed"
                                if event.type == "workflow_failed"
                                else "cancelled"
                            )
                            logger.info(
                                "run %s follow: 终态事件 %s，停止 follow",
                                handle.run_id,
                                event.type,
                            )
                            return
            finally:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            # shutdown / detach：clean exit（不标 corrupted）
            raise
        except Exception as e:  # noqa: BLE001 — follow task 任何异常 → corrupted
            handle.terminal = "corrupted"
            handle.status = "failed"
            handle.error = f"follow_task_crashed: {type(e).__name__}: {e}"
            logger.exception("run %s follow task 异常退出", handle.run_id)
            await self._emit_attach_error(
                handle,
                "follow_task_crashed",
                f"follow task crashed: {e}",
            )

    async def _emit_attach_error(
        self, handle: AttachedRunHandle, kind: str, message: str
    ) -> None:
        """attached follow 异常 → emit ``error`` 事件到 bus（让订阅 WS 看到）。

        **fan-out only**（``bus.relay``）——attached run 无写权，本事件不落外部 tape。
        客户端 ``error`` 事件渲染（LogStream 红行 / TopBar 失败指示）。

        **seq**：用单调负数（``-time.monotonic_ns()``）避免客户端 seq 去重吞掉多个 error
        事件（processEvent 按 seq 去重，``seq=0`` 多次会被吞）。
        """
        try:
            error_event = Event(
                seq=-(time.monotonic_ns() & 0x7FFFFFFF) - 1,  # 负 seq：不与真实 seq 冲突
                type="error",
                timestamp=time.time(),
                node=None,
                session_id=None,
                data={"kind": kind, "message": message, "source": "attach_follow"},
            )
            await handle.bus.relay(error_event)
        except Exception:  # noqa: BLE001 — relay 失败不应阻塞 follow 退出
            logger.warning("run %s attach error relay 失败", handle.run_id, exc_info=True)

    # ── X — windowed events + extended meta（SPEC web-attach §3）──────────────

    def get_run_events_window(
        self,
        run_id: str,
        *,
        since: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
    ) -> list[Event]:
        """窗口化读事件（SPEC §3 / M1：pure tape read，**不 emit bus**）。

        - 无参：全量（同 ``get_run_events``）
        - ``since=N``：``seq > N``
        - ``since=N & limit=M``：``[N+1, N+M]``（顺序 = tape 行序 = seq 升序）
        - ``tail=M``：最后 M 条

        **perf（SPEC §8.4b）**：``tail`` 走 ``tape_reader.tail_events`` 反向字节块扫描
        （与 tape 总大小无关，O(last_M_lines_bytes)）；``since+limit`` 走 ``since_limited``
        提前 break（O(limit)，不物化全量）。只有 ``since`` 单参（无 limit）才物化（client
        需要全量）。

        in-process 走 ``Tape.replay``，attached 走 ``tape_reader.replay``。
        **bus.emit / relay 不在此路径**（M1）。
        """
        handle = self._require_handle(run_id)
        # 校验
        if tail is not None and tail < 0:
            raise ValueError(f"tail must be >= 0: {tail}")
        if limit is not None and limit < 0:
            raise ValueError(f"limit must be >= 0: {limit}")

        # ``tail`` 路径优先（与 since 互斥语义：tail 总是返回最后 M 条）
        if tail is not None:
            if tail == 0:
                return []
            # attached / in-process 都有 ``tape.path``（Tape 与 AttachedTape 同形）
            tp = _handle_tape_path(handle)
            if tp is None:
                return list(handle.tape.replay())[-tail:]
            return tail_events(tp, tail)

        since_seq = since if since is not None else 0
        if limit is not None:
            # ``since+limit``：正向扫提前 break（O(limit)）
            tp = _handle_tape_path(handle)
            if tp is None:
                return list(handle.tape.replay(since_seq=since_seq))[:limit]
            return since_limited(tp, since_seq, limit)

        # 无 limit（``since`` 单参 或 全量）
        return list(handle.tape.replay(since_seq=since_seq))

    def get_run_extended_meta(self, run_id: str) -> dict | None:
        """扩展 meta（SPEC web-attach §3）：``{run_id, status, source, event_count,
        byte_size, oldest_seq, newest_seq, writable, huge, overview?}``。

        - ``writable``：in-process=True / attached=False（前端 gate 模态据此禁提交）。
        - ``huge``：``event_count > 50000`` OR ``byte_size > 5MB``（兜底阈值；SPEC §3）。
        - ``overview``：**仅 huge 模式返** —— 服务端 fold 同一 tape 派生 agents/charts/
          cost/run_status（**非第二真相源**——可经 ``load full`` 展开校验，M4）。

        **perf（SPEC §8.4a）**：单次扫文件（``_scan_meta_overview``），同时累计
        ``(count, bounds, topology, charts, state)``——避免 ``_compute_overview`` 三遍
        replay 的 O(3N) 退化。memoize key = ``(path, mtime, size)``，hit 时 O(1)。

        单一真相源仍是 tape；overview 是派生视图，与客户端 fold 同源（前端信任 + 可验）。
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        tape_path = _handle_tape_path(handle)
        if tape_path is None or not tape_path.exists():
            byte_size = 0
            event_count, oldest_seq, newest_seq = 0, 0, 0
            overview_data = None
        else:
            byte_size = tape_path.stat().st_size
            # memoize：mtime+size 不变则复用（client 高频轮询 /meta 不重算）
            mtime = tape_path.stat().st_mtime
            cache_key = (str(tape_path), mtime, byte_size)
            cached = self._meta_cache.get(cache_key)
            if cached is not None:
                event_count, oldest_seq, newest_seq, overview_data = cached
            else:
                event_count, oldest_seq, newest_seq, overview_data = (
                    self._scan_meta_overview_cached(tape_path)
                )
                self._meta_cache[cache_key] = (
                    event_count,
                    oldest_seq,
                    newest_seq,
                    overview_data,
                )
        is_attached = isinstance(handle, AttachedRunHandle)
        huge = event_count > 50_000 or byte_size > 5_000_000
        meta: dict = {
            "run_id": run_id,
            "status": handle.status,
            "source": handle.source,
            "event_count": event_count,
            "byte_size": byte_size,
            "oldest_seq": oldest_seq,
            "newest_seq": newest_seq,
            "writable": not is_attached,
            "huge": huge,
        }
        if huge and overview_data is not None:
            # M4：huge 模式服务端 fold 派生 overview（同一 tape，非第二真相源）。
            meta["overview"] = overview_data["overview"]
        return meta

    def list_runs(self) -> list[RunMeta]:
        """返回所有 run 的元数据（**不含事件**，懒加载红线 SPEC §0.1 铁律 2）。

        元数据从 ``replay_state(handle.tape)`` 派生（progress/cost），保证与唯一真相源
        一致（§9 决策 6）。status 取 ``handle.status``（实时）。
        """
        metas: list[RunMeta] = []
        for handle in self._runs.values():
            metas.append(self._meta_from_handle(handle))
        return metas

    def get_run_events(self, run_id: str) -> list[Event]:
        """懒加载：返回某 run 的全量事件（``tape.replay()``，SPEC §0.1 铁律 1）。

        唯一真相源 = tape；本方法不维护并行内存 list。未知 run_id → KeyError。
        """
        handle = self._require_handle(run_id)
        return list(handle.tape.replay())

    def get_run_state(self, run_id: str) -> RunState:
        """返回某 run 的 RunState 快照（``replay_state(tape)``，SPEC §3.1）。"""
        handle = self._require_handle(run_id)
        return replay_state(handle.tape)

    def get_run_meta(self, run_id: str) -> RunMeta | None:
        """返回单个 run 的 RunMeta（从 tape 派生，懒加载）。

        比 ``list_runs`` 后过滤高效（单 run 算 replay_state，不 replay 全部 run，
        SPEC §3.1 单 run 端点是前端高频轮询路径）。未知 run_id → None。
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return self._meta_from_handle(handle)

    def get_handle(self, run_id: str) -> RunView | None:
        """取 run 的 RunView（WS 订阅 / gate 分发用）。未知返回 None。

        返回 ``RunView`` 基类（in-process 与 attached 共用）。调用方需感知 ``source``
        字段或 ``isinstance`` 判定 attached 形态（attached 无 ``wf`` / ``gate_handler``）。
        """
        return self._runs.get(run_id)

    # ── 程序化客户端查询（MCP / 其它 shell）—— tape-only query path ─────────

    def pending_gates(self, run_id: str) -> list[HumanGate]:
        """返回 run 当前未 resolved 的 gate（SPEC phase-10 §3.3 / §5.1）。

        **tape-only**：派生自 ``pending_gates_from_tape(handle.tape)``，不读
        ``gate_handler._pending`` / ``_gates_meta``（runtime await 状态，重启即丢）。
        重启进程后仍能查（tape 在磁盘），多壳读同一份不漂移。

        未知 run_id → KeyError（fail loud，SPEC §6.0 铁律 4）。
        """
        handle = self._require_handle(run_id)
        return pending_gates_from_tape(handle.tape)

    def run_summary(self, run_id: str) -> dict | None:
        """MCP / 其它程序化客户端友好的 run 摘要（SPEC phase-10 §3.3）。

        返回 dict（不含 ``_hint``——``_hint`` 是 MCP 层加的引导字段，§9.10）::

            {
                "task_id": str,
                "status": "running" | "needs_decision" | "completed" | "failed" | "cancelled",
                "current_node": str | None,
                "progress": "3/7",  # done/total
                "cost": float,
                "elapsed": float,
                "gate": dict | None,  # 仅 needs_decision 时填充
                "output": dict | None,  # 仅 completed 时填充（来自 workflow_completed.data.outputs）
                "error": str | None,  # 仅 failed 时填充
            }

        未知 run_id → None（MCP ``get_task_status`` 据此返回 ``status="unknown"``）。

        全部数据派生自 tape + handle.status（实时），无并行真相源。
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        meta = self._meta_from_handle(handle)
        state = replay_state(handle.tape)
        gates = pending_gates_from_tape(handle.tape)
        status = self._derive_mcp_status(meta.status, gates)
        summary: dict = {
            "task_id": run_id,
            "status": status,
            "current_node": state.current_node,
            "progress": meta.progress,
            "cost": meta.cost,
            "elapsed": meta.elapsed,
            "gate": None,
            "output": None,
            "error": None,
        }
        if status == "needs_decision" and gates:
            summary["gate"] = _gate_to_dict(gates[0])
        elif status == "completed":
            # outputs 来自 workflow_completed 事件 data.outputs（reducer 不进 context）。
            # 扫 tape 找最后一个 workflow_completed（幂等，SPEC §3 单一读路径）。
            summary["output"] = _outputs_from_tape(handle.tape)
        elif status == "failed":
            summary["error"] = meta.error
        return summary

    @staticmethod
    def _derive_mcp_status(
        run_status: RunStatus, pending_gates: list[HumanGate]
    ) -> str:
        """RunStatus + pending_gates → MCP status（SPEC phase-10 §3.3）。

        映射规则：
          - 终态优先（completed / failed / cancelled 直接返回）
          - 非终态且有 pending gate → ``needs_decision``（优先于 running）
          - 其它 → ``running``（含 queued，对 MCP 客户端而言 queued 等价 running）

        ``needs_decision`` 优先于 ``running``：哪怕 status 显示 running，只要 tape 里
        有未 resolved gate，MCP 客户端第一关心的是"该决策了"。
        """
        if run_status == "completed":
            return "completed"
        if run_status == "failed":
            return "failed"
        if run_status == "cancelled":
            return "cancelled"
        if pending_gates:
            return "needs_decision"
        return "running"

    async def cancel_run(self, run_id: str, reason: str | None = None) -> bool:
        """取消 run（SPEC phase-10 §5.3）。

        步骤（顺序重要）：
          1. ``bus.emit("workflow_cancelled", ...)`` 写 tape（**唯一真相**，重启后
             replay 仍见 cancelled，不漂移）—— 必须在 task.cancel 前，避免与 task
             finally 的 teardown（关 bus）竞态。
          2. ``handle.status = "cancelled"``（runtime 状态，list_runs 立刻反映）。
          3. cancel asyncio task（停止编排，触发 task 内 finally 收尾）。
          4. await task 完成（让 _run_with_sem 的 finally 跑完，teardown 幂等）。
          5. teardown gate_handler + bus（幂等，可能已被 task finally 调用）。

        返回值：
          - True：成功 cancel（task 已 cancel + tape 已写 cancelled）
          - False：已终态（completed / failed / cancelled），业务可恢复（§6.1 验收）
          - KeyError：未知 run_id（fail loud，§6.0 铁律 4）
        """
        handle = self._require_handle(run_id)
        if handle.status in ("completed", "failed", "cancelled"):
            return False

        # 1. emit workflow_cancelled 写 tape（唯一真相，SPEC §5.4 决策 9）。
        # 先 emit 后 cancel task：task finally 会 teardown 关 bus，emit 必须在 bus 还
        # 活着时完成，避免 RuntimeError: Tape 已 close。
        try:
            await handle.bus.emit(
                "workflow_cancelled",
                data={"reason": reason or "user_cancelled"},
            )
        except Exception:  # noqa: BLE001 — emit 失败不阻断 cancel（tape 可能已 close）
            logger.warning(
                "run %s emit workflow_cancelled 失败（tape 可能已 close）",
                run_id,
                exc_info=True,
            )

        # 2. runtime status 转 cancelled（list_runs 立刻反映）
        handle.status = "cancelled"

        # 3. cancel asyncio task（若有 in-flight orchestrator.run）
        task = handle._task
        if task is not None and not task.done():
            task.cancel()

        # 4. await task 完成（让 finally 跑完，避免 leaked task）。task 被 cancel 后会
        # 抛 CancelledError——这是预期路径，正常吞。其它异常（编排失败）也吞——cancel
        # 本就是用户主动终止，编排异常已被 status 转 cancelled 覆盖（用户语义优先）。
        if task is not None and not task.done():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # 5. teardown（与正常终态路径一致，幂等——task finally 可能已调过）
        await self._teardown_handle(handle)
        return True

    async def wait_done(self, run_id: str, timeout: float = 30.0) -> None:
        """等某 run 到终态（completed/failed）。测试 + WS 收尾用。

        超时 raise ``asyncio.TimeoutError``（fail loud，不静默 hang）。

        attached run：follow task 是长循环（不自然 done），等终态事件由 follow task 自身
        翻 ``terminal=True``；本方法对 attached 直接返回（不等）——测试侧用 ``sleep`` +
        ``get_run_meta`` 轮询验证。
        """
        handle = self._require_handle(run_id)
        if isinstance(handle, AttachedRunHandle):
            return  # attached：follow task 不自然 done，本方法不适用
        if handle._task is None:
            return
        await asyncio.wait_for(asyncio.shield(handle._task), timeout=timeout)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """收尾：等所有在跑 run 到终态（限时）+ stop 各自 gate_handler。

        ``run_server`` lifespan 退出时调。保证无 leaked task / 未关 tape。

        - in-process：每未 done task ``wait_for(timeout)``（卡在 gate 超时 → cancel），
          之后逐 handle teardown。
        - attached：teardown 内部 cancel follow task（停 tail）。
        """
        # 收 in-process 的 run task（attached 无 ``_task``）
        in_proc_handles = [
            h for h in self._runs.values() if isinstance(h, InProcessRunHandle)
        ]
        pending = [
            h._task for h in in_proc_handles
            if h._task is not None and not h._task.done()
        ]
        for task in pending:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "shutdown: run task %s %ss 未完成（可能卡在 gate），强制 cancel",
                    task.get_name(), timeout,
                )
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — task 自身异常（如编排失败），忽略（已记 error）
                pass
        for handle in list(self._runs.values()):
            await self._teardown_handle(handle)

    # ── 内部 ───────────────────────────────────────────────────────────────

    def _require_handle(self, run_id: str) -> RunView:
        handle = self._runs.get(run_id)
        if handle is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return handle

    def _meta_from_handle(self, handle: RunView) -> RunMeta:
        """从 handle + tape 派生 RunMeta（progress/cost 从 replay_state，§9 决策 6）。

        ``replay_state`` 失败（tape 损坏等罕见）→ progress 退化为 "?/?"，status 仍取
        handle.status（fail loud 记 warning，不崩 list_runs）。

        attached 形态（``wf=None``）：topology 不存在，``total`` 从 tape
        ``workflow_started.data.topology.nodes`` 推导（若有效）或省略（``?``）；不读 ``wf``。
        """
        wf_name_fallback = (
            handle.wf.name
            if (isinstance(handle, InProcessRunHandle) and handle.wf is not None)
            else handle.run_id
        )
        wf_total: int | None
        if isinstance(handle, InProcessRunHandle) and handle.wf is not None:
            wf_total = len(handle.wf.nodes)
        else:
            wf_total = None  # attached: 从 tape topology 推
        try:
            state = replay_state(handle.tape)
            if wf_total is None:
                # attached：topology 从 workflow_started.data.topology.nodes（若有效）
                wf_total = _topology_node_count_from_tape(handle.tape)
            if wf_total is None or wf_total < 0:
                progress = "?"
            else:
                done = sum(1 for s in state.node_status.values() if s == "done")
                progress = f"{done}/{wf_total}"
            cost = _extract_cost(state)
            workflow_name = state.workflow_name or wf_name_fallback
        except Exception:  # noqa: BLE001 — tape 读失败不应崩 list_runs
            logger.warning("run %s replay 失败，元数据退化", handle.run_id, exc_info=True)
            progress = "?" if wf_total is None else f"?/{wf_total}"
            cost = 0.0
            workflow_name = wf_name_fallback
        elapsed = time.time() - handle.started_at
        return RunMeta(
            run_id=handle.run_id,
            workflow_name=workflow_name,
            status=handle.status,
            progress=progress,
            cost=cost,
            elapsed=elapsed,
            error=handle.error,
        )

    # ── Phase C：discovery + 懒挂载 + 删除 + 控制帧广播（SPEC §13） ────────────

    def _resolve_project_path_for_run(
        self, project_path: str | None
    ) -> Path | None:
        """SPEC §13.2 B-1：``project_path`` 缺省 → ``detect_project_root()`` + ``register_project``。

        返回 resolved project root Path，或 None（detect / register 均失败，回退到默认 runs_dir）。
        显式传入但 register 失败（无 marker 等）→ raise（让 web POST /api/run 400 fail loud）。
        """
        if project_path is None:
            try:
                root = detect_project_root()
            except Exception:  # noqa: BLE001 — detect 失败 → 回退
                logger.warning(
                    "start_run: detect_project_root 失败，回退到默认 runs_dir",
                    exc_info=True,
                )
                return None
        else:
            try:
                root = Path(project_path).resolve()
            except (OSError, RuntimeError) as e:
                raise ValueError(f"project_path resolve 失败：{project_path} ({e})") from e
        # register：缺省路径失败不阻断（兼容既有 in-process），显式传入失败则 raise。
        try:
            register_project(root)
        except ValueError:
            if project_path is not None:
                raise
            logger.warning(
                "start_run: register_project(detect) 失败，回退到默认 runs_dir",
                exc_info=True,
            )
            return None
        return root

    def is_allowed_tape_path(self, path: Path | str) -> bool:
        """SPEC §13.2 B-3：tape 是否在某注册项目的 ``runs/`` 子树下。

        ``resolve_tape_path`` 与 ``resolve_run_path`` 的 allowlist 守卫共用本方法。
        """
        return is_registered_runs_dir(path)

    def add_run_changed_listener(self, cb) -> None:
        """注册控制帧广播回调（SPEC §13.1 U-3 / §13.2 B-4）。

        ``cb(run_id: str, action: str) -> None``。WebServer 启动时注册一条，把 run_changed
        事件 enqueue 到每条 WS 的出站 queue（串行化发送，避免 FastAPI 单 WS 并发 send RuntimeError）。
        """
        self._run_changed_listeners.append(cb)

    def remove_run_changed_listener(self, cb) -> None:
        """移除广播回调（WebServer teardown 用，防 leak）。"""
        try:
            self._run_changed_listeners.remove(cb)
        except ValueError:
            pass

    def _broadcast_run_changed(self, run_id: str, action: str) -> None:
        """通知所有 WS：某 run 状态变化（``action="deleted"|"changed"|"attached"``）。"""
        for cb in list(self._run_changed_listeners):
            try:
                cb(run_id, action)
            except Exception:  # noqa: BLE001 — 单 listener 失败不影响其它
                logger.warning(
                    "run_changed listener 异常 (run=%s, action=%s)",
                    run_id, action, exc_info=True,
                )

    def discover_runs(self) -> list[RunSummary]:
        """SPEC §13 §5.2 D5 + M-5 + M-12：跨项目 discovery。

        步骤：
          1. 读注册表 → 拿到所有注册项目根。
          2. 每个存在项目扫 ``runs/*.jsonl``（派生缓存 ``<project>/runs/.orca-meta-cache.json``
             按 mtime/size 校验，P0）→ 抽 RunSummary。
          3. 合并内存 live run（in-process + attached）。
          4. legacy ``~/.orca/runs/*.json`` → source=legacy（project_id=None）。
          5. 重建 ``_run_path_index``（M-12）。

        坏 tape 跳过 + warn（R6 fail loud 但不崩列表）。
        """
        summaries: list[RunSummary] = []
        # 注册表读：失败（corrupt）→ 空 dict 继续（fail loud 由 list_registered 决定，此处不崩）。
        try:
            registered = list_registered()
        except Exception:  # noqa: BLE001 — corrupt 时降级，discovery 不崩
            logger.warning("discover_runs: 注册表读失败，仅返内存 + legacy run", exc_info=True)
            registered = {}

        new_index: dict[str, tuple[str | None, Path, str | None]] = {}
        for pid, meta in registered.items():
            root_str = meta.get("path")
            name = meta.get("name") or "<unnamed>"
            if not isinstance(root_str, str):
                continue
            root = Path(root_str)
            runs_dir = root / "runs"
            if not runs_dir.is_dir():
                continue  # 项目无 runs/ → skip（stale）
            for tape_path in sorted(runs_dir.glob("*.jsonl")):
                try:
                    summary = self._summary_from_tape(
                        tape_path, project_id=pid, project_name=name,
                        source="attached",
                    )
                except Exception:  # noqa: BLE001 — 坏 tape skip+warn
                    logger.warning(
                        "discover_runs: tape 解析失败跳过 %s", tape_path, exc_info=True,
                    )
                    continue
                if summary is None:
                    continue
                # 内存 live run 优先（status 更实时）；attached 同 tape 不重复入列表。
                if summary.run_id in self._runs:
                    continue
                summaries.append(summary)
                new_index[summary.run_id] = (pid, tape_path, name)

        # 内存 live run（in-process + attached 已注册的）。
        for handle in self._runs.values():
            try:
                meta = self._meta_from_handle(handle)
            except Exception:  # noqa: BLE001
                continue
            tp = _handle_tape_path(handle)
            project_id, project_name = self._lookup_project_for_handle(handle, tp)
            summaries.append(
                RunSummary(
                    run_id=handle.run_id,
                    workflow_name=meta.workflow_name,
                    project_id=project_id,
                    project_name=project_name,
                    status=meta.status,
                    progress=meta.progress,
                    cost=meta.cost,
                    elapsed=meta.elapsed,
                    started_at=handle.started_at,
                    event_count=0,
                    source="in-process" if isinstance(handle, InProcessRunHandle) else "attached",
                )
            )
            if tp is not None:
                new_index[handle.run_id] = (project_id, tp, project_name)

        # legacy ~/.orca/runs/*.json（旧 BgRunMeta）
        # 铁律：web 禁 import cli；本处用本地 helper 复刻 ``bg_runner.list_all_meta`` 的
        # 扫描语义（读 ``~/.orca/runs/*.json`` → 简化 meta dict），不引入 cli 依赖。
        try:
            legacy_metas = _list_legacy_metas()
        except Exception:  # noqa: BLE001
            legacy_metas = []
        for lm in legacy_metas:
            tape_path = Path(getattr(lm, "tape_path", "") or "")
            try:
                stat = tape_path.stat()
                _ = stat.st_size
            except OSError:
                # legacy 元数据存但 tape 不在 → 仍展示（前端可看到 stale）
                pass
            summaries.append(
                RunSummary(
                    run_id=lm.run_id,
                    workflow_name=Path(getattr(lm, "yaml_path", "") or "legacy").stem
                    or "legacy",
                    project_id=None,
                    project_name="Legacy",
                    status="cancelled",
                    progress="?",
                    cost=0.0,
                    elapsed=0.0,
                    started_at=getattr(lm, "started_at", None),
                    event_count=0,
                    source="legacy",
                )
            )

        self._run_path_index = new_index
        return summaries

    def _lookup_project_for_handle(
        self, handle: RunView, tape_path: Path | None
    ) -> tuple[str | None, str | None]:
        """从 handle 反查 (project_id, project_name)——优先 _run_path_index，否则注册表扫描。"""
        if tape_path is not None:
            entry = self._run_path_index.get(handle.run_id)
            if entry is not None:
                return entry[0], entry[2]
            # 落地查询：tape 在某注册项目 runs/ 下
            try:
                resolved = tape_path.resolve()
            except (OSError, RuntimeError):
                return None, None
            try:
                registered = list_registered()
            except Exception:  # noqa: BLE001
                return None, None
            for pid, meta in registered.items():
                root_str = meta.get("path")
                if not isinstance(root_str, str):
                    continue
                try:
                    runs_dir = Path(root_str).resolve() / "runs"
                    resolved.relative_to(runs_dir)
                    return pid, meta.get("name")
                except (ValueError, OSError, RuntimeError):
                    continue
        return None, None

    def _scan_meta_overview_cached(self, tape_path: Path) -> tuple[int, int, int, dict | None]:
        """SPEC §8.4a + §13.3 P0：三层派生缓存（in-memory → persistent → 重算）。

        查询顺序：
          1. **in-memory** ``_meta_cache``：key=(path, mtime, size)，hit O(1)。
          2. **persistent** ``<runs_dir>/.orca-meta-cache.json``：跨进程跨重启复用，
             key=tape filename + (mtime, size) 校验；失配重扫 + 原子写回。
          3. **recompute**：``_scan_meta_overview``，结果回填两层缓存。

        持久层语义（P0）：
          - **cache 非 index**：删了能完全重建，不违 R1（tape 唯一真相源）/ §9（无 run DB）。
          - 损坏 JSON → warn + 视为空（不 raise；下次 miss 自然重建）。
          - 写回失败 → warn 不阻断（缓存是 perf 优化，非正确性来源）。
        """
        try:
            stat = tape_path.stat()
        except OSError:
            # 文件不在 / 不可 stat → 返零空（调用方决定 skip）。
            return (0, 0, 0, None)
        mtime = stat.st_mtime
        size = stat.st_size
        cache_key = (str(tape_path), mtime, size)
        # 1. in-memory hit
        cached_mem = self._meta_cache.get(cache_key)
        if cached_mem is not None:
            return cached_mem
        # 2. persistent hit（同 mtime/size）
        cached_disk = self._persistent_cache_lookup(tape_path, mtime, size)
        if cached_disk is not None:
            # 回填 in-memory（下次直接内存命中）
            self._meta_cache[cache_key] = cached_disk
            return cached_disk
        # 3. recompute + 双写
        result = _scan_meta_overview(tape_path)
        self._meta_cache[cache_key] = result
        self._persistent_cache_writeback(tape_path, mtime, size, result)
        return result

    def _persistent_cache_path(self, tape_path: Path) -> Path:
        """持久缓存文件路径：``<runs_dir>/.orca-meta-cache.json``。"""
        return tape_path.parent / ".orca-meta-cache.json"

    def _persistent_cache_lookup(
        self, tape_path: Path, mtime: float, size: int
    ) -> tuple[int, int, int, dict | None] | None:
        """读持久缓存 entry（match mtime+size）。miss / 损坏 / 失配 → None。"""
        runs_dir = tape_path.parent
        data = self._persistent_cache_loaded(runs_dir)
        entry = data.get("entries", {}).get(tape_path.name)
        if not isinstance(entry, dict):
            return None
        if entry.get("mtime") != mtime or entry.get("size") != size:
            return None  # mtime/size 变 → 失配
        try:
            return (
                int(entry.get("count", 0)),
                int(entry.get("oldest", 0)),
                int(entry.get("newest", 0)),
                entry.get("overview"),
            )
        except (TypeError, ValueError):
            return None

    def _persistent_cache_loaded(self, runs_dir: Path) -> dict:
        """加载（懒）runs_dir 对应的持久缓存。损坏 → 空 + warn。"""
        if runs_dir in self._persistent_cache_by_runs_dir:
            return self._persistent_cache_by_runs_dir[runs_dir]
        cache_path = runs_dir / ".orca-meta-cache.json"
        data: dict = {"version": 1, "entries": {}}
        if cache_path.is_file():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    isinstance(raw, dict)
                    and isinstance(raw.get("entries"), dict)
                ):
                    data = raw
                else:
                    logger.warning(
                        "持久 meta cache 结构非法，视为空重建：%s", cache_path
                    )
            except (OSError, json.JSONDecodeError) as e:
                # 损坏 → warn + 空（cache 非 index，可重建，R1/§9）。
                logger.warning(
                    "持久 meta cache 读失败（%s），视为空重建：%s", e, cache_path
                )
                # 删除损坏文件避免每次新进程重复 warn。
                try:
                    cache_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._persistent_cache_by_runs_dir[runs_dir] = data
        return data

    def _persistent_cache_writeback(
        self,
        tape_path: Path,
        mtime: float,
        size: int,
        result: tuple[int, int, int, dict | None],
    ) -> None:
        """单条 entry 原子写回（tmp + os.replace）。失败 → warn 不阻断。"""
        runs_dir = tape_path.parent
        data = self._persistent_cache_loaded(runs_dir)
        entries = data.setdefault("entries", {})
        entries[tape_path.name] = {
            "mtime": mtime,
            "size": size,
            "count": result[0],
            "oldest": result[1],
            "newest": result[2],
            "overview": result[3],
        }
        cache_path = runs_dir / ".orca-meta-cache.json"
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cache_path)
        except OSError as e:
            # 写失败不阻断 scan 结果（cache 是 perf 优化）。
            logger.warning(
                "持久 meta cache 写失败（%s）：%s", e, cache_path
            )

    def _summary_from_tape(
        self,
        tape_path: Path,
        *,
        project_id: str | None,
        project_name: str | None,
        source: str,
    ) -> RunSummary | None:
        """从单 tape 文件派生 RunSummary（discovery 用，部分坏 tape → None + warn）。

        - 坏 tape（read 失败 / 0 有效事件 / 无 workflow_started）→ 返回 None（discovery skip）。
        - 空文件 / 刚创建的 live-pending（count==0）→ 返回 None（discovery 见 live 才有意义，
          纯空 tape 无 discovery 价值）。

        progress 从 ``overview.agents``（node_status 派生）算 done/total；elapsed 从
        workflow_started.timestamp 到终态事件 timestamp（或 now）算（code-reviewer M-3）。
        """
        try:
            count, _, _, overview_data = self._scan_meta_overview_cached(tape_path)
        except Exception:  # noqa: BLE001
            return None
        if count == 0:
            # 0 有效事件（空 tape 或坏 tape）→ skip
            return None
        overview = (overview_data or {}).get("overview") or {}
        # workflow_name 从 overview.run_status 反推不可，扫 tape 取 workflow_started。
        wf_name = _topology_workflow_name_from_tape(tape_path) or tape_path.stem
        # status 映射：overview.run_status 是 workflow 级别字符串
        wf_status = overview.get("run_status") or "pending"
        status: RunStatus
        if wf_status == "completed":
            status = "completed"
        elif wf_status == "failed":
            status = "failed"
        elif wf_status == "cancelled":
            status = "cancelled"
        elif wf_status == "running":
            status = "running"
        else:
            status = "live-pending"
        # progress 从 overview.agents 算（done/total）
        agents = overview.get("agents") or []
        total = len(agents)
        done = sum(1 for a in agents if isinstance(a, dict) and a.get("status") == "done")
        progress = f"{done}/{total}" if total > 0 else "?"
        # started_at + elapsed：扫 tape 取 workflow_started.timestamp + 终态 timestamp
        started_ts, ended_ts = _scan_tape_timebounds(tape_path)
        if started_ts is not None and ended_ts is not None:
            elapsed = max(0.0, ended_ts - started_ts)
        else:
            elapsed = 0.0
        started_at = started_ts
        return RunSummary(
            run_id=tape_path.stem,
            workflow_name=wf_name,
            project_id=project_id,
            project_name=project_name,
            status=status,
            progress=progress,
            cost=float(overview.get("cost_usd") or 0.0),
            elapsed=elapsed,
            started_at=started_at,
            event_count=count,
            source=source,  # type: ignore[arg-type]
        )

    def resolve_run_path(self, run_id: str) -> Path:
        """SPEC §13 §5.3 D7 / AC8：run_id → tape_path（懒挂载路径解析）。

        - 先查 ``_run_path_index``（discovery 期填充）。
        - miss 则扫注册项目 ``runs/<run_id>.jsonl``。
        - 0 命中 → raise ``FileNotFoundError``（routes 层 404）。
        - 多命中 → raise ``RuntimeError`` 列路径（routes 层 500，fail loud R6）。
        """
        entry = self._run_path_index.get(run_id)
        if entry is not None:
            return entry[1]
        # 扫注册项目
        hits: list[Path] = []
        try:
            registered = list_registered()
        except Exception:  # noqa: BLE001
            registered = {}
        for meta in registered.values():
            root_str = meta.get("path")
            if not isinstance(root_str, str):
                continue
            candidate = Path(root_str) / "runs" / f"{run_id}.jsonl"
            if candidate.is_file():
                hits.append(candidate)
        # legacy（``~/.orca/runs/<run_id>.jsonl``）
        legacy_candidate = _legacy_default_tape_path(run_id)
        try:
            if legacy_candidate.is_file():
                hits.append(legacy_candidate)
        except OSError:
            pass
        if len(hits) == 0:
            raise FileNotFoundError(f"run_id not found across projects: {run_id}")
        if len(hits) > 1:
            raise RuntimeError(
                f"run_id 命中多个 tape（数据异常）：{run_id} → {[str(h) for h in hits]}"
            )
        return hits[0]

    async def ensure_attached(self, run_id: str) -> None:
        """SPEC §13 §5.3 D7：幂等懒挂载。已在 ``_runs`` → 直接返；否则 resolve_run_path + attach_run。

        触发面（SPEC §13.2 I-3）：``{/meta,/events,/assets/<path>,WS subscribe}`` 任一遇
        unknown run_id 先调本方法。
        """
        if run_id in self._runs:
            return
        tape_path = self.resolve_run_path(run_id)  # raise FileNotFoundError / RuntimeError
        # attach_run 是幂等的（同 tape_path 重复 attach 返回既有 id）。
        await self.attach_run(str(tape_path), run_id=run_id)
        self._broadcast_run_changed(run_id, "attached")

    async def delete_run(self, run_id: str) -> dict:
        """SPEC §13 §5.7 D10 + §13.1 U-1 + §13.2 B-5：删除 run。

        返回 dict（routes 层据 ``ok`` / ``live`` / ``never_existed`` 映射 HTTP 200/404/409）：
          - in-process non-terminal → ``cancel_run`` + 删盘 → ``{ok, run_id, existed_before:True}``
          - in-process terminal → 删盘 → ``{ok, run_id, existed_before:True}``
          - attached live (他进程) → ``{ok:False, live:True, pid}`` → routes 409
          - attached terminal → 删盘 → ``{ok, run_id, existed_before:True}``
          - 磁盘有但未挂载（dormant）→ ensure_attached 解析路径 + 删盘 → ``{ok, ..., existed_before:True}``
          - 内存+磁盘都无 → ``{ok:False, never_existed:True}`` → routes 404

        Windows file-locked → ``{ok:False, live:True, pid:None, error}` → routes 409。
        越界守卫（同 attach）：路径须在某注册项目 runs/ 下。
        """
        handle = self._runs.get(run_id)
        in_process_live = False
        if handle is not None:
            # in-process live run：先 cancel_run（写 cancelled + 停 task）。
            if isinstance(handle, InProcessRunHandle):
                if handle.status not in ("completed", "failed", "cancelled"):
                    in_process_live = True
                    await self.cancel_run(run_id)
            elif isinstance(handle, AttachedRunHandle):
                # attached live = follow task alive + 非 terminal
                if (
                    not handle.terminal
                    and handle.follow_task is not None
                    and not handle.follow_task.done()
                ):
                    # 他进程 live → 不删（U-1 → 409）
                    return {"ok": False, "live": True, "run_id": run_id, "pid": None}

        # 解析 tape 路径：内存有 → handle.tape.path；无 → resolve_run_path。
        if handle is not None:
            tp = _handle_tape_path(handle)
        else:
            # 先查内存索引：若上次 discovery 见过但磁盘已无（stale 索引）→ never_existed。
            entry = self._run_path_index.get(run_id)
            if entry is not None and not entry[1].is_file():
                self._run_path_index.pop(run_id, None)
                entry = None
            if entry is not None:
                tp = entry[1]
            else:
                try:
                    tp = self.resolve_run_path(run_id)
                except (FileNotFoundError, RuntimeError):
                    return {"ok": False, "never_existed": True, "run_id": run_id}
                if not tp.is_file():
                    # resolve_run_path 扫到的文件已不存在（stale）
                    self._run_path_index.pop(run_id, None)
                    return {"ok": False, "never_existed": True, "run_id": run_id}

        if tp is None:
            return {"ok": False, "never_existed": True, "run_id": run_id}

        # 越界守卫：tape 须在某注册项目 runs/ 下（或默认 runs_dir 下，in-process 兼容）。
        try:
            tp_resolved = tp.resolve()
        except (OSError, RuntimeError) as e:
            return {"ok": False, "live": False, "run_id": run_id, "error": str(e)}
        in_default = self._is_under_default_runs_dir(tp_resolved)
        in_registry = self.is_allowed_tape_path(tp_resolved)
        if not (in_default or in_registry):
            return {
                "ok": False,
                "live": False,
                "run_id": run_id,
                "error": f"tape out of registry allowlist: {tp}",
            }

        # 移除内存 handle（停 follow task）。
        if handle is not None:
            await self._teardown_handle(handle)
            self._runs.pop(run_id, None)

        # 删盘：tape + run 目录（<rid>/ 整目录 + 同名 prompts/assets）。
        deleted = self._delete_run_files(tp_resolved)
        if not deleted:
            return {
                "ok": False,
                "live": True,
                "run_id": run_id,
                "pid": None,
                "error": "file locked or already removed",
            }
        # 清 discovery 索引（防下次 delete_run 把 stale index 当成存在）。
        self._run_path_index.pop(run_id, None)
        self._broadcast_run_changed(run_id, "deleted")
        return {"ok": True, "run_id": run_id, "existed_before": True}

    def _is_under_default_runs_dir(self, resolved: Path) -> bool:
        """路径是否在 ``self._runs_dir`` 子树下（in-process 默认根兼容）。"""
        try:
            runs_root = self._runs_dir.resolve()
            resolved.relative_to(runs_root)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    def _delete_run_files(self, tape_path: Path) -> bool:
        """删 tape + run 目录。Windows file-locked → 返回 False（让 routes 409）。

        显式枚举要删的派生文件（SPEC §13 D10 清单），**禁用通配符**（避免误删用户手放的
        同前缀实验性文件 / `.bak` 等，code-reviewer M-1）。
        """
        # 1. tape 文件（``<rid>.jsonl``）
        try:
            tape_path.unlink(missing_ok=True)
        except OSError as e:
            # Windows file-locked / permission → 409 信号
            logger.warning("delete_run: tape unlink 失败 %s: %s", tape_path, e)
            return False
        # 2. run 目录（``<rid>/`` 整目录，含 prompts/assets/artifacts）
        run_dir = tape_path.with_name(tape_path.stem)
        if run_dir.is_dir():
            try:
                shutil.rmtree(run_dir, ignore_errors=False)
            except OSError as e:
                logger.warning(
                    "delete_run: rmtree 失败 %s: %s（tape 已删，dir 残留）", run_dir, e
                )
                # tape 已删视为成功（dir gc 后续清理）。
        # 3. 同 stem 的显式派生文件（D10）：``<rid>.json``（run 元数据）+ ``orca-<rid>.json``（激活标记）
        # + ``<rid>.web-ready.json`` 等 web 信号 + ``<rid>.log``（legacy log）。
        # 不用通配符——避免误删同前缀的其它文件。
        derived_suffixes = [
            ".json",                # run 元数据
            ".log",                 # legacy daemon log
            ".web-ready.json",      # web-ready 信号文件（orca open）
        ]
        for suffix in derived_suffixes:
            sibling = tape_path.with_name(tape_path.stem + suffix)
            if sibling.is_file():
                try:
                    sibling.unlink(missing_ok=True)
                except OSError:
                    pass
        # ``orca-<rid>.json`` 激活标记
        marker = tape_path.with_name(f"orca-{tape_path.stem}.json")
        if marker.is_file():
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    async def _run_with_sem(
        self,
        handle: RunHandle,
        inputs: dict,
        task: str | None,
        max_iter: int | None,
    ) -> None:
        """sem 内跑 orchestrator（真并发 + max_concurrent 排队）。

        生命周期：acquire sem → start gate_handler → status=running →
        ``Orchestrator.run()`` → 终态（completed/failed）→ teardown。
        """
        async with self._sem:
            handle.status = "running"
            await handle.gate_handler.start()
            handle._gate_started = True
            orch = Orchestrator(
                handle.wf,
                handle.bus,
                inputs,
                task=task,
                max_iter=max_iter,
                run_id=handle.run_id,
            )
            try:
                await orch.run()
                handle.status = "completed"
            except Exception as e:  # noqa: BLE001 — 编排任何异常 → failed（fail loud 记 error）
                handle.status = "failed"
                handle.error = f"{type(e).__name__}: {e}"
                logger.exception("run %s 失败", handle.run_id)
            finally:
                await self._teardown_handle(handle)

    async def _teardown_handle(self, handle: RunView) -> None:
        """清理一个 handle 的资源（幂等）。

        in-process（SPEC §2.3）：cancel chart ingestor + unlink sock + stop gate_handler
        + close bus（同 v2）。

        attached（SPEC §2.3 / §0 D6）：cancel follow task（停止 tail）；**跳过 sock unlink**
        （sock 是 in-process chart ingestor 的，属别进程）；attached 无 gate_handler；
        ``AttachedTape.close`` no-op。bus.close 仍调（幂等；EventBus.close 通知订阅者终态）。
        """
        if isinstance(handle, AttachedRunHandle):
            # attached：cancel follow task 即可（无 sock / gate_handler / chart ingestor）。
            if handle.follow_task is not None and not handle.follow_task.done():
                handle.follow_task.cancel()
                try:
                    await handle.follow_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "run %s follow task 异常退出", handle.run_id, exc_info=True
                    )
            try:
                handle.bus.close()
            except Exception:  # noqa: BLE001
                logger.warning("run %s bus.close 异常", handle.run_id, exc_info=True)
            return

        # in-process 路径（原 v2 行为）
        # phase-13 §3.1：先 cancel chart ingestor（防新事件落已 close 的 tape）。
        if handle._chart_ingestor is not None and not handle._chart_ingestor.done():
            handle._chart_ingestor.cancel()
            try:
                await handle._chart_ingestor
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — ingestor task 异常不应阻塞 teardown
                logger.warning("run %s chart ingestor 异常退出", handle.run_id, exc_info=True)
        # 兜底 unlink socket 文件（crash 重起 task 的 cleanup 不依赖此，但保证 run 结束无残留）。
        # §7.7 短路径化：socket 在 <tmp>/orca-<hash>.sock（chart_sock_path），与 runs 目录解耦。
        sock_path = chart_sock_path(handle.run_id)
        try:
            Path(sock_path).unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001
            logger.warning("run %s sock unlink 失败 %s: %r", handle.run_id, sock_path, e)

        if handle._gate_started:
            try:
                await handle.gate_handler.stop()
            except Exception:  # noqa: BLE001 — teardown 不应崩
                logger.warning("run %s gate_handler.stop 异常", handle.run_id, exc_info=True)
            handle._gate_started = False
        # bus.close 关 tape 句柄（幂等：Tape.close 内部 _closed guard）。
        try:
            handle.bus.close()
        except Exception:  # noqa: BLE001
            logger.warning("run %s bus.close 异常", handle.run_id, exc_info=True)


def _extract_cost(state: RunState) -> float:
    """从 RunState.usage 提取 cost（若有）。无 usage → 0.0。"""
    usage = state.usage
    if usage is None:
        return 0.0
    # UsageSummary 形态见 schema/state.py；cost 字段可能不存在（纯 script run 无 token）。
    return float(getattr(usage, "cost", 0.0) or 0.0)


def _gate_to_dict(gate: HumanGate) -> dict:
    """HumanGate → MCP 友好的 dict（run_summary 的 gate 字段）。

    返回字段（SPEC phase-10 §2.2 / §3.3）：gate_id / prompt / options / context /
    source / run_id / node / session_id。客户端据此渲染决策 UI + 调 resolve_gate。
    """
    return {
        "gate_id": gate.id,
        "prompt": gate.prompt,
        "options": gate.options,
        "context": gate.context,
        "source": gate.source,
        "run_id": gate.run_id,
        "node": gate.node,
        "session_id": gate.session_id,
    }


def _outputs_from_tape(tape: Tape) -> dict | None:
    """扫 tape 找最后一个 workflow_completed 事件的 data.outputs（run_summary 用）。

    reducer 不把 outputs 投影进 RunState.context（node_completed 累积的是每 node 的
    output，键是 node 名，非 "outputs"）；workflow 级最终 outputs 在
    ``workflow_completed.data.outputs`` 字段。返回 None 表示无 completed 事件 / 无
    outputs 字段（如纯 script run 也应有 ``{}`` 至少）。
    """
    outputs: dict | None = None
    for event in tape.replay():
        if event.type == "workflow_completed":
            data_outputs = event.data.get("outputs")
            if isinstance(data_outputs, dict):
                outputs = data_outputs
    return outputs


# ── X — attach helpers（SPEC web-attach §2 / §3）─────────────────────────────


def _probe_first_event(path: Path) -> Event | None:
    """read-only 探测 tape 首个有效事件（不 bulk replay）。

    partial / 空 / 全残行 → None。首行有效 → 返回 Event。
    用于 ``attach_run`` 判 ``workflow_started`` + 推导 run_id。
    """
    try:
        for event in tape_reader_replay(path, since_seq=0):
            return event
    except FileNotFoundError:
        return None
    return None


def _scan_terminal_type(path: Path) -> str | None:
    """read-only 扫 tape 找最末的终态事件类型（D3：不进 bus，仅 status 判定）。

    返回 ``"workflow_completed"`` / ``"workflow_failed"`` / ``"workflow_cancelled"``
    或 None（无终态事件）。用于 ``attach_run`` 跳过终态 tape 的 follow task。
    """
    last_terminal: str | None = None
    try:
        for event in tape_reader_replay(path, since_seq=0):
            if event.type in (
                "workflow_completed",
                "workflow_failed",
                "workflow_cancelled",
            ):
                last_terminal = event.type
    except FileNotFoundError:
        return None
    return last_terminal


def _probe_head_and_terminal(path: Path) -> tuple[Event | None, str | None]:
    """单次扫同时取首个事件 + 最末终态事件类型（MAJOR 6 修复，避免两次全扫）。

    返回 ``(first_event, last_terminal_type)``。partial / 空 → ``(None, None)``。
    一遍扫描完成两个意图：首个有效事件 + 是否已到终态。
    """
    first_event: Event | None = None
    last_terminal: str | None = None
    try:
        for event in tape_reader_replay(path, since_seq=0):
            if first_event is None:
                first_event = event
            if event.type in (
                "workflow_completed",
                "workflow_failed",
                "workflow_cancelled",
            ):
                last_terminal = event.type
    except FileNotFoundError:
        return (None, None)
    return (first_event, last_terminal)


def _topology_node_count_from_tape(tape: Tape | AttachedTape) -> int | None:
    """扫 tape 找 workflow_started.data.topology.nodes（attached run 用）。

    attached run 无 ``wf`` 对象，但 topology 在 workflow_started 事件里（phase-9c 决策）。
    返回 ``len(nodes)`` 或 None（无 workflow_started / topology 缺失 / shape 异常）。
    """
    for event in tape.replay():
        if event.type != "workflow_started":
            continue
        topo = event.data.get("topology")
        if not isinstance(topo, dict):
            return None
        nodes = topo.get("nodes")
        if not isinstance(nodes, list):
            return None
        return len(nodes)
    return None


def _topology_workflow_name_from_tape(tape_path: Path) -> str | None:
    """扫 tape 取 ``workflow_started.data.workflow_name``（discovery 用，单遍）。

    与 ``_topology_node_count_from_tape`` 类似但输入是 path（discovery 期无 handle）。
    partial / 坏 tape → None（调用方降级到 stem）。
    """
    try:
        for event in tape_reader_replay(tape_path, since_seq=0):
            if event.type == "workflow_started":
                wf_name = event.data.get("workflow_name")
                if isinstance(wf_name, str) and wf_name:
                    return wf_name
                return None
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None
    return None


def _scan_tape_timebounds(tape_path: Path) -> tuple[float | None, float | None]:
    """扫 tape 取 (workflow_started.timestamp, 终态事件 timestamp)（discovery 算 elapsed 用）。

    终态事件 = ``workflow_completed`` / ``workflow_failed`` / ``workflow_cancelled``。
    无终态事件 → ``ended=None``（调用方降级到 0.0 elapsed）。
    partial / 坏 tape → ``(None, None)``。
    """
    started: float | None = None
    ended: float | None = None
    try:
        for event in tape_reader_replay(tape_path, since_seq=0):
            if started is None and event.type == "workflow_started":
                ts = getattr(event, "timestamp", None)
                if isinstance(ts, (int, float)):
                    started = float(ts)
            if event.type in (
                "workflow_completed", "workflow_failed", "workflow_cancelled",
            ):
                ts = getattr(event, "timestamp", None)
                if isinstance(ts, (int, float)):
                    ended = float(ts)
    except FileNotFoundError:
        return (None, None)
    except Exception:  # noqa: BLE001
        return (None, None)
    return (started, ended)


# ── legacy ~/.orca/runs/ 兼容（铁律：web 禁 import cli，本处复刻 bg_runner 路径语义）──


def _legacy_runs_root() -> Path:
    """``$ORCA_HOME/runs``（默认 ``~/.orca/runs``）—— 旧 BgRunMeta 落盘根。"""
    env = os.environ.get("ORCA_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".orca"
    return home / "runs"


def _legacy_default_tape_path(run_id: str) -> Path:
    """旧约定：``~/.orca/runs/<run_id>.jsonl``（与 ``bg_runner.default_tape_path`` 同源）。"""
    return _legacy_runs_root() / f"{run_id}.jsonl"


def _list_legacy_metas() -> list:
    """扫 ``~/.orca/runs/*.json``（旧 BgRunMeta）→ 简化对象 list（带 run_id/yaml_path/started_at）。

    复刻 ``bg_runner.list_all_meta`` 的扫描语义但**不**依赖 ``bg_runner.BgRunMeta``（避免
    web → cli 反向依赖）。返回对象用 ``types.SimpleNamespace``，调用方经 ``getattr`` 取字段。
    """
    import types

    root = _legacy_runs_root()
    if not root.is_dir():
        return []
    results: list = []
    for meta_file in sorted(root.glob("*.json")):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        results.append(
            types.SimpleNamespace(
                run_id=str(data.get("run_id") or meta_file.stem),
                yaml_path=str(data.get("yaml_path") or ""),
                started_at=data.get("started_at"),
                tape_path=str(data.get("tape_path") or ""),
            )
        )
    return results


def _handle_tape_path(handle: RunView) -> Path | None:
    """取 handle 对应的 tape 文件路径（meta 的 byte_size / event_count / tail-events 用）。

    统一走 ``handle.tape.path``（Tape 与 AttachedTape 同形），避免 AttachedRunHandle
    ``tape_path`` 字段与 ``tape.path`` 语义重合（两者必须一致，本方法单一来源）。
    """
    if isinstance(handle, (InProcessRunHandle, AttachedRunHandle)):
        tp = getattr(handle.tape, "path", None)
        return Path(tp) if tp is not None else None
    return None


# ── EventType 分类（SPEC §13.4 M-17 / AC14 contract test 守门）─────────────────
#
# ``_scan_meta_overview`` 把每个 EventType 归入两档之一：
#   1. **overview-affecting**：进 full json.loads + fold，影响 ``agents/charts/cost_usd/
#      run_status`` 之一（列入 ``OVERVIEW_AFFECTING_EVENT_TYPES``）。
#   2. **bulk**：只取 seq 计 count/bounds（substring fast-path），不 fold overview
#      （列入 ``BULK_EVENT_TYPES``）。
#
# **契约（AC14）**：``EventType`` 的全集必须被这两档**完全划分**。新增 EventType 必须显式
# 归入其中一档——否则 ``tests/iface/web/test_scan_meta_overview_contract.py`` 失败，
# 守门防漏（reviewer I-9）。
#
# 自动派生关系：``status-affecting subset`` = ``EventType 全集`` − ``BULK_EVENT_TYPES``，
# 必须 == ``OVERVIEW_AFFECTING_EVENT_TYPES``。等价于"白名单（bulk）之外都算 status-affecting"。

OVERVIEW_AFFECTING_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "workflow_started",
        "workflow_completed",
        "workflow_failed",
        "workflow_cancelled",
        "node_started",
        "node_completed",
        "node_failed",
        "node_skipped",
        "agent_usage",
        "custom",
    }
)

# Bulk event types that don't affect meta/overview (skip in fast-path).
# Pre-compiled markers for substring check (cheaper than full json.loads).
_META_BULK_MARKERS = (
    '"agent_message"',
    '"agent_thinking"',
    '"agent_tool_call"',
    '"agent_tool_result"',
    '"agent_step_started"',
    '"unknown_event"',
    '"prompt_rendered"',
    '"route_taken"',
    '"retry_started"',
    '"retry_succeeded"',
    '"retry_exhausted"',
    '"validator_started"',
    '"validator_passed"',
    '"validator_failed"',
    '"wait_started"',
    '"wait_completed"',
    '"dialog_started"',
    '"dialog_message"',
    '"dialog_ended"',
    '"foreach_started"',
    '"foreach_item_started"',
    '"foreach_item_completed"',
    '"foreach_completed"',
    '"interrupt_requested"',
    '"interrupt_resolved"',
    '"human_decision_requested"',
    '"human_decision_resolved"',
    '"workflow_resumed"',
    # ``error`` 事件不影响 overview 派生（agents/charts/cost/run_status），只计 count/seq，
    # 显式归入 bulk 档（AC14 完备性：必须归入一档，避免契约 test 失败）。
    '"error"',
)
# 等价 set（contract test 用）：所有 bulk type 字面值。
BULK_EVENT_TYPES: frozenset[str] = frozenset(
    m.strip('"') for m in _META_BULK_MARKERS
)
_META_SEQ_RE = __import__("re").compile(r'"seq":\s*(\d+)')


def _scan_meta_overview(path: Path) -> tuple[int, int, int, dict | None]:
    """单遍扫 tape 同时累计 ``(event_count, oldest_seq, newest_seq, overview_data)``。

    SPEC §8.4a perf 修复（BLOCKER 2）：原 ``_compute_overview`` 调 ``replay_state`` +
    2 次全扫 = 3 遍 O(N)；现合并为 1 遍，且只算 huge 模式需要的 overview 数据。

    **fast-path**（perf 关键）：``agent_message``/``agent_thinking``/``agent_tool_*``
    占 huge tape 99% 行数但不影响 overview —— substring check 后用 regex 提取 seq
    跳过 json.loads（避免 Python stdlib json 的 ~500ms/60k 行开销）。只有 ``workflow_*`` /
    ``node_*`` / ``agent_usage`` / ``custom`` 进 full parse + fold。

    overview_data 形如 ``{"overview": {agents, charts, cost_usd, run_status}}``。
    """
    count = 0
    oldest = 0
    newest = 0
    node_status: dict[str, str] = {}
    wf_status = "pending"
    topo_nodes: list[str] = []
    charts: list[dict] = []
    saw_topology = False
    cost = 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    continue

                # Fast-path: bulk event types → substring check + regex seq（no json.loads）
                is_bulk = False
                for marker in _META_BULK_MARKERS:
                    if marker in stripped:
                        is_bulk = True
                        break
                if is_bulk:
                    m = _META_SEQ_RE.search(stripped)
                    if m:
                        try:
                            seq = int(m.group(1))
                        except ValueError:
                            continue
                        count += 1
                        if oldest == 0 or seq < oldest:
                            oldest = seq
                        if seq > newest:
                            newest = seq
                    continue

                # Full parse for state-changing types
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    break  # partial 末行
                seq = obj.get("seq")
                if not isinstance(seq, int):
                    continue
                count += 1
                if oldest == 0 or seq < oldest:
                    oldest = seq
                if seq > newest:
                    newest = seq

                t = obj.get("type")
                node = obj.get("node")
                data = obj.get("data") or {}

                if t == "workflow_started":
                    wf_status = "running"
                    if not saw_topology:
                        saw_topology = True
                        topo = data.get("topology")
                        if isinstance(topo, dict):
                            nodes = topo.get("nodes")
                            if isinstance(nodes, list):
                                for n in nodes:
                                    if isinstance(n, dict):
                                        name = n.get("name")
                                        if isinstance(name, str):
                                            topo_nodes.append(name)
                elif t == "workflow_completed":
                    wf_status = "completed"
                elif t == "workflow_failed":
                    wf_status = "failed"
                elif t == "workflow_cancelled":
                    wf_status = "cancelled"
                elif t == "node_started" and isinstance(node, str):
                    node_status[node] = "running"
                elif t == "node_completed" and isinstance(node, str):
                    node_status[node] = "done"
                elif t == "node_failed" and isinstance(node, str):
                    node_status[node] = "failed"
                elif t == "node_skipped" and isinstance(node, str):
                    node_status[node] = "skipped"
                elif t == "agent_usage":
                    c = data.get("cost_usd")
                    if isinstance(c, (int, float)):
                        cost += float(c)
                elif t == "custom" and data.get("kind") == "chart":
                    chart = data.get("chart")
                    if isinstance(chart, dict):
                        charts.append(
                            {
                                "label": str(chart.get("label") or "misc"),
                                "title": str(chart.get("title") or ""),
                                "chart_type": str(chart.get("chart_type") or "chart"),
                            }
                        )
    except Exception:  # noqa: BLE001 — 读失败不应崩 /meta
        return (count, oldest, newest, None)

    # agents：topology + node_status 合并（同前端 selectAgents；保序：topo 优先 + 后来 node 补）
    # **注**：此派生与前端 selectAgents 同源同逻辑——若改一处需同步另一处（SPEC U1：服务端
    # fold = 客户端 fold，M4 server-asserted）。
    for name in node_status:
        if name not in topo_nodes:
            topo_nodes.append(name)
    agents = [
        {"name": name, "status": node_status.get(name) or "pending"}
        for name in topo_nodes
    ]
    overview = {
        "agents": agents,
        "charts": charts,
        "cost_usd": cost,
        "run_status": wf_status,
    }
    return (count, oldest, newest, {"overview": overview})
