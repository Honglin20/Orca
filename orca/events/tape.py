"""tape.py —— append-only JSONL，编排层唯一真相源。

回答「事件落在哪？」：所有事件只写一处——本文件。无并行内存 list（反 Conductor
``_event_history``），无 sidecar 投影（反 AgentHarness 5 个 sidecar suffix）。

设计规则（SPEC §3.2 / §11 关键决策 3、12）：
  - **seq 写时分配**：``append`` 在一把 ``asyncio.Lock`` 内完成「seq 分配 + write +
    flush」整体，保证 ``seq 序 == 文件行序 == replay 序``（dagu #1835 规则 4 + 6）。
  - **每事件 write + flush**（不 fsync，靠 OS buffer；崩溃最多丢最后一行 —— Conductor
    ``event_log.py:161-174``）。
  - **append-only，永不重写/驱逐**（反模式①）。
  - **resume 先清残行**：``resume=True`` 时以 append 模式重开同 run_id 的 tape；先扫描末尾，
    若最后一行是崩溃截断的不完整 JSON，**截断至最后一个有效行**（绝不接坏行），截断记
    warning（可见，不静默）；``last_seq`` 从有效事件重算，新事件从 ``last_seq+1`` 继续。
    不重建、不 synthesize 事件（反 Conductor ``prepend_workflow_started``）。
  - **``_json_safe``**：bytes/Path/未知类型 → str，保证纯 JSONL（``lossy-but-pure``）。

依赖单向：本模块只依赖 ``orca.schema``（Event 类型），不反向。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from orca.schema import Event

logger = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    """递归把非 JSON 原生类型转成纯 JSON 安全形态（lossy-but-pure，SPEC §2 规则 10）。

    bytes → utf-8 解码（失败回退 repr）；Path → str；其余未知类型 → repr/str。
    dict / list / tuple 递归处理。保证 ``json.dumps`` 不抛 ``TypeError``。
    """
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return repr(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # set / 自定义对象 / 其它 —— lossy 回退（不丢整条事件，只降保真度）
    return repr(obj)


class Tape:
    """append-only JSONL，编排层唯一真相源。永不驱逐，永不重写。

    seq 由 ``append`` 写时分配（调用方不管 seq）；``append`` 在一把 ``asyncio.Lock``
    内完成「seq 分配 + write + flush」整体，保证 seq 序 == 文件行序。
    """

    def __init__(self, path: Path, run_id: str, *, resume: bool = False):
        self.path = Path(path)
        self.run_id = run_id
        # Lock 惰性创建：绑定到首次 append 所在的 event loop（Python 3.12 Lock 绑定 loop，
        # __init__ 时无 running loop 会绑定到已关闭的旧 loop）。Tape 常在 loop 外构造。
        self._lock: asyncio.Lock | None = None
        self._closed = False

        self.path.parent.mkdir(parents=True, exist_ok=True)

        if resume:
            # resume：追加模式重开，先清残行（截断末尾不完整 JSON + warning），再重算 last_seq
            self._last_seq = self._truncate_trailing_partial()
        elif self.path.exists():
            # 文件已存在但未指定 resume：视为续写（兼容同一 Tape 对象复用），从现有内容重算
            self._last_seq = self._scan_last_seq()
        else:
            self._last_seq = 0

        # 追加模式打开文件句柄（不存在则创建）。后续 append 直接 write+flush 到此句柄。
        self._fh = open(self.path, "a", encoding="utf-8")

    # ── 公开 API ──────────────────────────────────────────────────────────────

    async def append(self, event_data: dict) -> int:
        """写一行 JSON + flush。返回分配的 seq（单调递增）。

        seq 由 Tape 分配（写时），调用方不传 seq。``append`` 在一把 ``asyncio.Lock``
        内完成「seq 分配 + 文件 write + flush」整体，保证并发下 seq 序 == 文件行序。

        入参是**不含 seq 的 event 字段 dict**（type/timestamp/node/session_id/data），
        本方法填入 seq 后落盘并返回。落盘前**构造 Event 做一次校验**（fail loud：
        type 不在 EventType / 字段非法时立即在 emit 侧报错，而非延迟到 replay）。
        """
        if self._closed:
            raise RuntimeError("Tape 已 close，不能再 append")

        # 惰性创建 Lock，绑定到当前 running loop（Python 3.12 Lock 绑 loop）。
        if self._lock is None:
            self._lock = asyncio.Lock()

        # 先 _json_safe（bytes/Path/未知 → 纯 JSON 安全）。type 等字段校验在 Lock 内做
        # （构造 Event），保证「坏事件不落盘 + 不分配 seq」（seq 序 == 文件行序不变量）。
        safe_data = _json_safe(event_data.get("data", {}))

        async with self._lock:
            # 校验在分配 seq **之前**：非法 type / extra 字段 → 抛 ValidationError，
            # 此时 _last_seq 未自增（坏事件不留 seq 间隙，SPEC「seq 序 == 文件行序」铁律）。
            # 用占位 seq=0 构造仅校验字段（type/timestamp/node/session_id/data），
            # 真实 seq 校验通过后再分配。
            Event(
                seq=0,
                type=event_data["type"],
                timestamp=event_data.get("timestamp", 0.0),
                node=event_data.get("node"),
                session_id=event_data.get("session_id"),
                data=safe_data,
            )
            self._last_seq += 1
            seq = self._last_seq
            payload = {
                "seq": seq,
                "type": event_data["type"],
                "timestamp": event_data.get("timestamp", 0.0),
                "node": event_data.get("node"),
                "session_id": event_data.get("session_id"),
                "data": safe_data,
            }
            line = json.dumps(payload, ensure_ascii=False)
            self._fh.write(line + "\n")
            self._fh.flush()
            return seq

    def replay(self, since_seq: int = 0) -> Iterator[Event]:
        """从 since_seq 读到底（不含 since_seq 本身）。一行一事件，容忍末尾残行。

        末尾残行（崩溃场景）被跳过不抛（fail-soft 读；残行在 resume 时已被截断）。
        中间行损坏（理论上不应发生）记 warning 并跳过，不阻断后续有效行。

        **调用方须耗尽迭代器**（或用 ``contextlib.aclosing``）：生成器内部持文件句柄，
        提前 break 会延迟到 GC 才关闭句柄。当前调用方（``replay_state`` 等）均完整耗尽。
        """
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    # 末尾残行容忍；中间行（之后还有有效行）属于异常，记 warning 但继续
                    logger.warning(
                        "tape %s 第 %d 行不是合法 JSON，跳过（可能为崩溃残行）",
                        self.path,
                        lineno,
                    )
                    continue
                try:
                    event = Event(**obj)
                except Exception as e:  # pydantic 校验失败（缺字段/类型错）
                    logger.warning(
                        "tape %s 第 %d 行无法解析为 Event：%s，跳过",
                        self.path,
                        lineno,
                        e,
                    )
                    continue
                if event.seq <= since_seq:
                    continue
                yield event

    def last_seq(self) -> int:
        """当前已落盘的最大 seq（下次 append 将分配 last_seq+1）。"""
        return self._last_seq

    def close(self) -> None:
        """关闭文件句柄。close 后 append 抛 RuntimeError（fail loud）。"""
        if not self._closed:
            self._fh.close()
            self._closed = True

    # ── resume / 初始化内部 ───────────────────────────────────────────────────

    def _truncate_trailing_partial(self) -> int:
        """resume 专用：扫描末尾，截断不完整行（截断记 warning），返回有效 last_seq。

        - 文件不存在：返回 0。
        - 最后一行是不完整 JSON（崩溃截断）：截断至最后一个有效行，记 warning。
        - 文件全空或全残：清空，返回 0。

        绝不把新行接到残行后面（否则产生坏行 —— SPEC §3.2 resume 关键约束）。
        """
        if not self.path.exists():
            return 0

        # 读全部字节，逐行判定。保留最后一个有效行末尾偏移，截断其后内容。
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return 0

        lines = text.split("\n")
        # split 后末尾若有空串（文件以 \n 结尾）则去掉；保留有效行结构
        if lines and lines[-1] == "":
            lines.pop()

        # 从末尾向前找最后一个**完整且合法**的行；其后的残行全部截断
        last_valid_idx = -1
        last_seq = 0
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                # 残行：继续向前找
                continue
            try:
                last_seq = int(obj["seq"])
                last_valid_idx = i
            except (KeyError, TypeError, ValueError):
                # 合法 JSON 但不是 Event 形态：视为残行，继续向前
                continue
            break

        if last_valid_idx == len(lines) - 1:
            # 末尾无残行，无需截断
            return last_seq

        # 有残行：截断至 last_valid_idx（含）。无任何有效行则清空。
        if last_valid_idx < 0:
            logger.warning(
                "tape %s resume：未发现任何有效事件行，清空文件重写", self.path
            )
            self.path.write_text("", encoding="utf-8")
            return 0

        truncated_count = len(lines) - 1 - last_valid_idx
        logger.warning(
            "tape %s resume：截断末尾 %d 个不完整行（从 seq %d 之后续写）",
            self.path,
            truncated_count,
            last_seq,
        )
        kept = "\n".join(lines[: last_valid_idx + 1]) + "\n"
        self.path.write_text(kept, encoding="utf-8")
        return last_seq

    def _scan_last_seq(self) -> int:
        """非 resume 但文件已存在：重算 last_seq（不截断，容忍末尾残行）。

        与 resume 不同：此处**不截断**（调用方未要求 crash recovery），但末尾若有
        不完整行会**记 warning**（fail loud，SPEC §6.0 铁律4）—— 提醒调用方：若这是
        崩溃遗留，应使用 ``resume=True`` 截断，否则下次 append 会接到残行后产生坏行。
        """
        if not self.path.exists():
            return 0
        # 检测末尾是否有残行（最后一行非合法 JSON / 非 Event）
        self._warn_if_trailing_partial_non_resume()
        seq = 0
        for event in self.replay():
            seq = max(seq, event.seq)
        return seq

    def _warn_if_trailing_partial_non_resume(self) -> None:
        """非 resume 重开：若末尾存在不完整行，记 warning（不截断，仅提醒）。"""
        if not self.path.exists():
            return
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            return
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return
        last = lines[-1].strip()
        try:
            json.loads(last)
        except json.JSONDecodeError:
            logger.warning(
                "tape %s 非 resume 重开但末尾存在不完整行（可能是崩溃遗留）。"
                "若需 crash recovery 请使用 resume=True；当前不截断，"
                "下次 append 可能产生坏行",
                self.path,
            )
