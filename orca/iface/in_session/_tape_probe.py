"""_tape_probe.py —— 纯只读 fail-loud 终态扫描 reader（SPEC 2026-08-02-audit-a §4.1）。

回答「stop 在 emit workflow_cancelled 前如何 fail-loud 判 tape 是否已含终态事件？」

**为何不复用 `events/tape_reader.replay`？** 实读 `tape_reader.py:47-68`：其
`json.JSONDecodeError` 路径是 **`return` 停整个迭代**（末尾 partial 与中间残行同处理），
fail-soft：坏 JSON 行下静默停迭代返部分结果 → stop 错走「无终态 → emit」分支往坏 tape
追加 `workflow_cancelled`，违 I1。本 reader 把两种失败模式显式分开（B-2）：

- `json.JSONDecodeError`（中间残行 / 末尾 partial / 任意非合法 JSON）→ **raise
  `TapeParseError`**（fail loud I5；历史 tape 损坏不可恢复，必须让人看见）。
- 行 parse 成 dict 但 pydantic `Event` 校验失败（合法 JSON 不符 schema，schema 演化场景）
  → `logger.warning` + `continue`（**不** raise；schema 演化是可容忍软失败，历史 tape
  不应因 schema 升级而阻塞 stop / bootstrap）。

**重复终态检测两套触发（B-1）**：
- `terminal_count`（不去重，每命中一条终态事件 +1）
- `terminal_types_seen`（去重 set）

扫完后：
- `len(terminal_types_seen) > 1`（不同类型终态）→ raise `TapeContradictionError`。
- `terminal_count >= 2 AND len(terminal_types_seen) == 1`（同类型重复，如 2×
  `workflow_cancelled`）→ warn log `[AUDIT] duplicate-terminal` + 返该类型（让 stop 走
  分支 2 短路 exit 0，不阻塞用户当前操作）。
- 否则返最末命中类型（无终态 → None）。

依赖单向：仅 import `orca.schema.Event`（events→iface 合法方向）+ 标准库。**禁止 import
`orca/iface/web/`**（spec §5.1，依赖铁律）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from orca.schema import Event

logger = logging.getLogger(__name__)


# 三类互斥终态事件（schema/event.py:22-24，replay.py:122-138）。
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({
    "workflow_completed",
    "workflow_failed",
    "workflow_cancelled",
})


class TapeParseError(Exception):
    """tape 行非合法 JSON（含中间残行 / 末尾 partial）—— fail loud I5。

    携带 ``path`` + ``lineno`` 便于 stop / bootstrap 的 stderr / warn log 报位。
    """

    def __init__(self, path: Path, lineno: int):
        self.path = path
        self.lineno = lineno
        super().__init__(
            f"tape {path} 第 {lineno} 行非合法 JSON（含中间残行 / 末尾 partial）"
        )


class TapeContradictionError(Exception):
    """tape 含 ≥2 条**不同类型**终态事件（I1 已破）—— fail loud I5。

    Attributes:
        types: 去重后命中的不同终态类型集合。
        last_seq: 扫描命中的**最末终态事件 seq**（不论类型）；无终态事件时为 0
            （但本 exception 仅在 ≥2 类终态时抛，故实际 > 0）。
    """

    def __init__(self, types: frozenset[str], last_seq: int):
        self.types = types
        self.last_seq = last_seq
        super().__init__(
            f"tape 含 {sorted(types)} 多类终态事件（last_seq={last_seq}）"
        )


def scan_terminal(tape_path: Path) -> str | None:
    """纯只读扫一遍 tape，返**最末终态事件类型**（无终态 → None）。

    失败模式（B-2，显式分级）：
        - 行非合法 JSON → ``raise TapeParseError``。
        - 合法 JSON 但 pydantic 校验失败 → ``logger.warning`` + ``continue``。

    重复终态（B-1，显式分级）：
        - 多类终态 → ``raise TapeContradictionError``。
        - 同类重复 → warn log ``[AUDIT] duplicate-terminal`` + 返该类型（不阻塞）。

    纯只读：不开 ``Tape(resume=True)``（自带截断副作用）、不开 ``EventBus``、不写文件。
    """
    terminal_count = 0
    terminal_types_seen: set[str] = set()
    last_terminal_type: str | None = None
    last_seq = 0

    with open(tape_path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as e:
                # 中间残行 / 末尾 partial / 任意坏 JSON：fail loud，**不**静默 return。
                raise TapeParseError(tape_path, lineno) from e
            # 合法 JSON 但非 dict（如顶层数组 / 标量）→ 当校验失败软跳。
            try:
                event = Event(**obj)
            except Exception as e:  # pydantic 校验失败：schema 演化软失败
                logger.warning(
                    "[AUDIT] tape-probe %s 第 %d 行无法解析为 Event：%s，跳过",
                    tape_path, lineno, e,
                )
                continue
            etype = event.type
            if etype in TERMINAL_EVENT_TYPES:
                terminal_count += 1
                terminal_types_seen.add(etype)
                last_terminal_type = etype
                last_seq = event.seq

    if len(terminal_types_seen) > 1:
        # 多类终态 = 真矛盾（状态字段语义冲突），fail loud。
        raise TapeContradictionError(frozenset(terminal_types_seen), last_seq)
    if terminal_count >= 2 and len(terminal_types_seen) == 1:
        # 同类重复（如 2× workflow_cancelled）= 历史污染但不矛盾，warn 不阻塞。
        logger.warning(
            "[AUDIT] duplicate-terminal types=%s count=%d path=%s",
            sorted(terminal_types_seen), terminal_count, tape_path,
        )
    return last_terminal_type
