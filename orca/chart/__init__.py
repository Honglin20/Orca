"""orca.chart —— script 子进程内调用的 chart 推送 API（phase-13 SPEC §4）。

回答「script 子进程里怎么把图绑到正确的 run？」：``render_chart`` 不接收 run_id/node/session_id
参数（铁律 #2：env 继承是单调信息流），从 env 读 ORCA_* 身份变量 → 连 per-run Unix socket
→ ingestor emit custom(chart) → tape（单一写路径）。三壳零改动读 tape 渲染。

**何时调**：仅在 Orca 编排的 script 子进程内（env 含 ORCA_*）。直接 ``python foo.py`` 跑会
fail loud（SPEC §7.1）。

公开 API：``render_chart``（SPEC §4.1）。

依赖单向：本包只依赖 stdlib（json/os/socket/sys）+ 自身子模块（_render/_validate/_downsample/
_limits）。**不依赖** events/exec/schema/run/iface——是 script 子进程内的客户端 lib，
零 Orca runtime 依赖（script 进程不 import orca.events 等），保证 script 端轻量。
"""

from orca.chart._render import render_chart

__all__ = ["render_chart"]
