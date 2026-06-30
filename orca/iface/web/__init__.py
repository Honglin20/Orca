"""orca.iface.web —— Web 壳后端（FastAPI + WS + RunManager，SPEC phase 9a）。

回答「用户在浏览器怎么跑/看/回放 workflow？」的后端部分：单进程同引擎 uvicorn 托管多 run
真并发，懒加载 REST（``/api/runs`` 只元数据，事件走 ``/api/runs/<id>/events``）+ 单通道
WS（按需订阅某 run 的事件）+ 多 run gate 分发。

核心铁律（SPEC §0.1）：
  - **Tape 唯一真相源**：后端无并行内存事件 list，全量事件 ``tape.replay()``。
  - **懒加载**：``/api/runs`` 不返回事件（红线）。
  - **WS 单通道 + 按需订阅**：subscribe(run_id) 后只推该 run。
  - **真并发**：RunManager 用 Semaphore(max_concurrent) 真并发跑 N run。
  - **依赖单向**：iface/web → run+gates+events+schema+compile，不被任何模块 import。

依赖单向：本包是渲染/转发壳，不含编排/gate 决策逻辑（那是 run/gates 职责）。
"""

from orca.iface.web.run_manager import RunHandle, RunManager, RunMeta
from orca.iface.web.server import create_app, run_server

__all__ = ["create_app", "run_server", "RunManager", "RunHandle", "RunMeta"]
