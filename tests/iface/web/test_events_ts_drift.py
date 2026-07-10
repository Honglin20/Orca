"""test_events_ts_drift.py —— D1 codegen drift guard（web-shell-v2 SPEC §0 D1 / §9 铁律 AC）。

断言前端 ``events.ts`` 的 ``EventType`` 联合体成员集合 == Python ``EventType`` Literal
成员集合（fail loud）。SPEC §9 铁律 AC 之一：``events.ts EventType 集合 == event.py``。

非可逆 drift（后端加新 type 但 events.ts 未重生成）会让本测试 fail，CI 拦截——
这正是 21↔39 漂移的根因修复。
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from orca.schema.event import EventType

REPO_ROOT = Path(__file__).resolve().parents[3]
EVENTS_TS = (
    REPO_ROOT
    / "orca"
    / "iface"
    / "web"
    / "frontend"
    / "src"
    / "types"
    / "events.ts"
)


def _parse_ts_event_type(text: str) -> set[str]:
    """从 events.ts 文本反向解析 EventType 成员集合。

    匹配 ``export type EventType = (| "x")+;`` 块，提取所有 ``"name"``。
    """
    block = re.search(
        r"export type EventType =\s*((?:\s*\|\s*\"[a-z_]+\")+\s*);",
        text,
    )
    assert block, "events.ts 中 EventType 联合体未匹配"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def test_events_ts_exists() -> None:
    """events.ts 文件必须存在（codegen 已跑）。"""
    assert EVENTS_TS.exists(), (
        f"缺失 {EVENTS_TS}——请跑 `python scripts/gen_events_ts.py` 生成"
    )


def test_events_ts_event_type_set_matches_python() -> None:
    """events.ts EventType 集合 == Python EventType Literal 集合（drift guard）。"""
    py_set = set(typing.get_args(EventType))
    ts_text = EVENTS_TS.read_text(encoding="utf-8")
    ts_set = _parse_ts_event_type(ts_text)

    missing = py_set - ts_set
    extra = ts_set - py_set
    assert not missing, (
        f"events.ts EventType 缺少（后端已加但 codegen 未跑）：{sorted(missing)}。"
        "请跑 `python scripts/gen_events_ts.py`。"
    )
    assert not extra, (
        f"events.ts EventType 多余（后端已删但 codegen 未跑）：{sorted(extra)}。"
        "请跑 `python scripts/gen_events_ts.py`。"
    )


def test_event_type_count_is_39() -> None:
    """SPEC §3.2：B1 落地后 EventType 全集 = 39（37 + agent_step_started + unknown_event）。

    本断言锁基数：未来后端**有意**扩 type 时需同步更新本期望（DRY：把「下次新增」
    显式化，避免悄悄扩张无人察觉）。
    """
    py_set = set(typing.get_args(EventType))
    assert len(py_set) == 39, (
        f"EventType 基数变化：期望 39（agent_step_started + unknown_event 落地后），"
        f"实际 {len(py_set)}。若是有意扩展，请同步更新本期望。"
    )


@pytest.mark.parametrize(
    "must_have",
    [
        "agent_step_started",  # B1 新增（opencode step_start 心跳）
        "unknown_event",  # B1 新增（translator escape hatch，D8 reducer no-op）
    ],
)
def test_b1_new_types_present(must_have: str) -> None:
    """B1 新增的 2 个 EventType 必须在两侧都在（反 21→31→39 漂移回归）。"""
    assert must_have in typing.get_args(EventType)
    ts_set = _parse_ts_event_type(EVENTS_TS.read_text(encoding="utf-8"))
    assert must_have in ts_set
