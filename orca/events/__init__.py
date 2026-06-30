"""orca.events —— 事件层（唯一真相源）。

只回答「事件落在哪、如何分发、如何重建状态」：
  - tape.py   → Tape：append-only JSONL 持久化（唯一真相源）
  - bus.py    → EventBus：持有 Tape + 异步 fan-out
  - replay.py → replay_state / apply_event：从 Tape 重建 RunState（纯 reducer fold）

铁律（SPEC §3）：
  - 唯一真相源：事件只写 Tape 一处，无并行内存 list、无 sidecar。
  - 幂等：reducer 应用同一事件 N 次 = 1 次（streaming text 用 text@seq 不拼接）。
  - 一条读路径：streaming = replay = 同一个 apply_event。
  - session_id 透传到事件顶层（reducer/前端按它分组）。

依赖单向：本层只依赖 ``orca.schema``，不反向（schema 不 import events）。
Event / EventType 从 schema re-export，避免上层多 import。
"""

from orca.events.bus import EventBus, Subscription
from orca.events.replay import apply_event, replay_state
from orca.events.tape import Tape
from orca.schema import Event, EventType

__all__ = [
    "EventBus",
    "Subscription",
    "Tape",
    "replay_state",
    "apply_event",
    "Event",
    "EventType",
]
