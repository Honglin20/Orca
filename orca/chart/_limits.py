"""_limits.py —— chart 相关硬限制常量（phase-13 SPEC §5 / §7，两端同源）。

**两端同源**：``orca.chart._render``（script 端 client lib）和 ``orca.events.chart_ingestor``
（Orca 进程端 server）都 import 这里的常量——保证 client 拒收的 size 与 server 拒收的 size
完全一致，防 drift。

依赖单向：本模块零依赖（纯常量），是 orca.chart 包的最底层。ingestor 模块（events/）允许
依赖本模块（events → chart 的常量层不违反单向依赖，因为 chart 是 script-side 客户端 lib
包，其 _limits 子模块是纯常量库）。
"""

from __future__ import annotations

# SPEC §5.2：post-downsample 整条 socket 消息字节硬上限（含 envelope）。
# 两端同源：client lib _render.py 检查 encoded 长度；ingestor server 检查 incoming line 长度。
MAX_MESSAGE_BYTES: int = 2 * 1024 * 1024  # 2 MB

# SPEC §5.1：自动降采样阈值默认值。覆盖 95% 真实场景（训练曲线/监控/报告）。
# script 可显式覆盖（``render_chart(..., max_points=10000)``）。
DEFAULT_MAX_POINTS: int = 2000

# SPEC §7.6：client 等 ack 超时秒数。Orca 正常 emit 是 ms 级，10s 足够覆盖 tape fs 慢 /
# ingestor 暂时卡住。无 timeout 会让 script（claude 子进程）挂死，连带 claude 超时。
ACK_TIMEOUT_SECONDS: float = 10.0

# SPEC §7.7：socket 路径长度上限（macOS ``sun_path`` 104 / Linux 108，留余量取 90）。
# 超过 → ``render_chart`` fail loud，建议用户改 ``ORCA_RUNS_DIR`` 到短路径。
SOCK_PATH_MAX: int = 90

# SPEC §4.1 / types.ts：允许的 chart_type 集合（7 种）。``frozenset`` 防误改。
ALLOWED_CHART_TYPES: frozenset[str] = frozenset({
    "line",
    "bar",
    "area",
    "scatter",
    "pareto",
    "radar",
    "table",
})

# SPEC §7.2 / types.ts：pareto_direction 允许值（仅 ``max`` / ``min`` / 空）。
ALLOWED_PARETO_DIRECTIONS: frozenset[str] = frozenset({"max", "min"})
