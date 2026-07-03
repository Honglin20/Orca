# phase-13 实施计划 —— script-side render_chart 接入

> **SPEC**：[`phase-13-render-chart.md`](../specs/phase-13-render-chart.md)（对抗审闭环 v1，16 处修订已并入）
> **目标**：让 claude/opencode spawn 的 script 子进程能调 `orca.chart.render_chart` 推图到 tape，TUI/Web 零改动渲染。
> **分支**：`phase13-render-chart`
> **测试后端**：opencode + deepseek-v4-flash（CLAUDE.md 已记录）

---

## 0. 任务拆解（5 步，每步独立可 review）

| 步 | 主题 | 改动文件 | 验收 |
|---|---|---|---|
| S1 | env 注入扩展 | `orca/exec/env.py` | 单测：注入 4 个 ORCA_* + backward compat |
| S2 | chart ingestor + RunHandle 集成 | `orca/events/chart_ingestor.py`（新）+ `orca/iface/web/run_manager.py` | 单测：emit + ack + teardown + crash 恢复 + sock 路径长度 |
| S3 | Python 客户端库 `orca.chart` | `orca/chart/{__init__, _render, _validate, _downsample, _limits}.py`（新） | 单测：env 缺/socket 不可达/超限/ack timeout/降采样策略 |
| S4 | ClaudeExecutor 接入 env overlay | `orca/exec/claude/executor.py::_build_spawn_config` | 单测：spawn env 含 4 个 ORCA_* |
| S5 | e2e + 压测 + opencode+deepseek 验证 | `tests/e2e_phase13/`（新）+ `tests/e2e_mxint/`（已有，复用）| 5 个 e2e 用例 + TUI snapshot + 压测 |

---

## S1. env 注入扩展（最小改动）

### S1.1 改 `orca/exec/env.py::build_env_overlay`

**当前签名**（既有）：
```python
def build_env_overlay(prefixes: tuple[str, ...]) -> dict[str, str]:
```

**扩展后**：
```python
def build_env_overlay(
    prefixes: tuple[str, ...],
    *,
    run_id: str = "",
    node: str = "",
    session_id: str = "",
    chart_sock: str = "",
) -> dict[str, str]:
    overlay = {...既有 prefix 透传...}
    if run_id:     overlay["ORCA_RUN_ID"]      = run_id
    if node:       overlay["ORCA_NODE"]        = node
    if session_id: overlay["ORCA_SESSION_ID"]  = session_id
    if chart_sock: overlay["ORCA_CHART_SOCK"]  = chart_sock
    return overlay
```

**关键点**：4 个 keyword 默认空串 → 缺省不注 → backward compat（既有调用方 `build_env_overlay(prefixes)` 不破）。

### S1.2 单测

文件：`tests/exec/test_env.py`（已有，扩展）
- `test_build_env_overlay_no_chart_kwargs_no_inject`：旧调用方式不注 ORCA_*
- `test_build_env_overlay_injects_all_four`：4 个 keyword 全传 → 4 个 env 注
- `test_build_env_overlay_partial_kwargs_partial_inject`：仅传 run_id/node → 仅注 2 个

---

## S2. chart ingestor + RunHandle 集成

### S2.1 新文件 `orca/events/chart_ingestor.py`

```python
"""per-run Unix socket ingestor：收 chart 消息 → emit custom(chart) 事件。

协议（SPEC §3.2）：
  script → server: 单行 JSON {"node": str, "session_id": str, "payload": ChartPayload}
  server → script: 单行 JSON {"ok": bool, "seq": int?, "error": str?}
强制短连接。
"""
from __future__ import annotations
import asyncio, json, logging
from pathlib import Path
from orca.chart._limits import MAX_MESSAGE_BYTES  # 同源常量

logger = logging.getLogger(__name__)
_MAX_INCOMING_BYTES = MAX_MESSAGE_BYTES  # 防 client 端绕过

async def chart_ingestor(sock_path: Path, bus, run_id: str) -> None:
    """per-run listener。RunHandle 启动时 create_task；终态 cancel。"""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()  # stale socket 残留（前次 crash）
    server = await asyncio.start_unix_server(_make_handler(bus), path=str(sock_path))
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        sock_path.unlink(missing_ok=True)

def _make_handler(bus):
    async def handle(reader, writer):
        try:
            line = await reader.readline()
            if len(line) > _MAX_INCOMING_BYTES:
                await _ack(writer, ok=False, error=f"payload too large: {len(line)} bytes")
                return
            msg = json.loads(line.decode("utf-8"))
            node = msg.get("node"); sid = msg.get("session_id"); payload = msg.get("payload")
            if not (isinstance(node, str) and isinstance(sid, str) and isinstance(payload, dict)):
                await _ack(writer, ok=False, error="malformed message")
                return
            seq = await bus.emit("custom",
                {"kind": "chart", "chart": payload},
                node=node, session_id=sid)
            await _ack(writer, ok=True, seq=seq)
        except Exception as e:
            await _ack(writer, ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
    return handle

async def _ack(writer, *, ok: bool, seq: int | None = None, error: str | None = None) -> None:
    msg = {"ok": ok}
    if seq is not None: msg["seq"] = seq
    if error is not None: msg["error"] = error
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()
```

### S2.2 RunHandle 字段扩展（`orca/iface/web/run_manager.py`）

加字段：
```python
_chart_ingestor: asyncio.Task | None = field(default=None, repr=False)
```

`start_run` 内（或 `_run_with_sem` 起 run 前）：
```python
sock_path = self._runs_dir / f"{run_id}.sock"
if not resume:  # SPEC §3.1：resume 模式不起 ingestor
    handle._chart_ingestor = asyncio.create_task(
        chart_ingestor(sock_path, bus, run_id),
        name=f"orca-chart-ingestor-{run_id}",
    )
    handle._chart_ingestor.add_done_callback(
        lambda t: _on_ingestor_crash(t, sock_path, bus, run_id)
    )
```

`_teardown_handle` 末尾：
```python
if handle._chart_ingestor is not None and not handle._chart_ingestor.done():
    handle._chart_ingestor.cancel()
Path(self._runs_dir / f"{handle.run_id}.sock").unlink(missing_ok=True)
```

### S2.3 crash 恢复 callback

```python
def _on_ingestor_crash(task, sock_path, bus, run_id):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return  # 正常退出（不应发生，serve_forever 永不返回）
    logger.warning("chart_ingestor crash (run=%s): %r", run_id, exc, exc_info=True)
    sock_path.unlink(missing_ok=True)
    new_task = asyncio.create_task(
        chart_ingestor(sock_path, bus, run_id),
        name=f"orca-chart-ingestor-{run_id}-restart",
    )
    new_task.add_done_callback(lambda t: _on_ingestor_crash(t, sock_path, bus, run_id))
    # 注：RunHandle 内的 _chart_ingestor 引用更新需通过 nonlocal / closure
    # 简化：crash 恢复不更新 RunHandle 字段（teardown 时 cancel + unlink 即可，
    # 重起的 task 名字带 -restart，teardown 时按 name 找）
```

**实现备注**：crash 恢复时 RunHandle 字段更新存在 closure 复杂度，简化方案是**不更新字段**（重起的 task 独立运行，teardown 走 sock unlink + name 找 task）。如果测试发现 teardown 漏 cancel，则用 weakref 或全局 task registry 解决。

### S2.4 单测

文件：`tests/events/test_chart_ingestor.py`（新）
- `test_ingestor_emits_chart_event_and_acks_seq`：emit 后 `tape.replay()` 含 1 条 custom(chart) + ack seq == `tape.last_seq()`
- `test_ingestor_malformed_message_acks_error`：非 JSON / 缺字段 → ok=False
- `test_ingestor_oversize_payload_rejected`：> MAX_MESSAGE_BYTES → ok=False
- `test_ingestor_teardown_unlinks_socket`：cancel task 后 socket 文件不存在
- `test_ingestor_crash_triggers_restart`：monkeypatch `bus.emit` 第一次抛 → callback 重起 → 第二次 emit 成功

文件：`tests/iface/web/test_run_manager_chart.py`（新）
- `test_start_run_starts_ingestor`：start_run 后 socket 文件存在
- `test_run_teardown_cancels_ingestor`：run 完成后 socket 文件不存在
- `test_resume_mode_skips_ingestor`：resume=True → socket 不存在

文件：`tests/chart/test_sock_path_length.py`（新）
- `test_long_sock_path_raises`：mock sock_path 100 字节 → raise RuntimeError（检测点在 ingestor 启动前，§7.7）

---

## S3. Python 客户端库 `orca.chart`

### S3.1 新文件结构

```
orca/chart/
├── __init__.py        # 公开 render_chart
├── _render.py         # render_chart 主逻辑
├── _validate.py       # ChartPayload 校验
├── _downsample.py     # 6 种 chart_type 降采样策略
└── _limits.py         # MAX_MESSAGE_BYTES = 2 * 1024 * 1024
```

### S3.2 `_limits.py`

```python
MAX_MESSAGE_BYTES = 2 * 1024 * 1024  # SPEC §5.2，含 envelope，两端同源
DEFAULT_MAX_POINTS = 2000            # SPEC §5.1 默认
ACK_TIMEOUT_SECONDS = 10.0           # SPEC §7.6
SOCK_PATH_MAX = 90                   # SPEC §7.7（macOS 104 / Linux 108 留余量）
ALLOWED_CHART_TYPES = frozenset({
    "line", "bar", "area", "scatter", "pareto", "radar", "table",
})
```

### S3.3 `_validate.py`

```python
def validate_payload(p: dict) -> None:
    """fail loud：缺字段 / 类型错 / 未知 chart_type → raise ValueError。"""
    if p.get("chart_type") not in ALLOWED_CHART_TYPES:
        raise ValueError(...)
    if not isinstance(p.get("data"), list):
        raise ValueError(...)
    if not p.get("label") or not isinstance(p["label"], str):
        raise ValueError(...)
    if not p.get("title") or not isinstance(p["title"], str):
        raise ValueError(...)
    # 各 chart_type 特有校验按需扩
```

### S3.4 `_downsample.py`

```python
def downsample(chart_type: str, data: list[dict], max_points: int, hue: str = "") -> list[dict]:
    if chart_type in ("line", "area", "scatter"):
        return _by_hue_groups(chart_type, data, max_points, hue)
    if chart_type in ("bar", "pareto"):
        return _aggregate_by_x(data, max_points, hue)
    if chart_type == "table":
        return data[:max_points]
    if chart_type == "radar":
        return data
    return data  # 未知（validate 已抛，此处兜底）

def _by_hue_groups(chart_type, data, max_points, hue):
    if not hue:
        return _resample_one(chart_type, data, max_points)
    groups = _group_by(data, hue)
    per_group = max(1, max_points // max(1, len(groups)))
    out = []
    for _, rows in groups.items():
        out.extend(_resample_one(chart_type, rows, per_group))
    return out

def _resample_one(chart_type, rows, n):
    if len(rows) <= n: return rows
    if chart_type in ("line", "area"):
        return _bucket_average(rows, n)  # 按长度分桶取 x_mean, y_mean
    if chart_type == "scatter":
        return _uniform_sample(rows, n)
    return rows
```

### S3.5 `_render.py`

```python
def render_chart(*, chart_type, data, label, title, x="", y="", hue="",
                 columns=None, pareto_direction="", pareto_x_direction="",
                 pareto_y_direction="", max_points=DEFAULT_MAX_POINTS) -> int:
    # 1. 读 env（身份路由）
    run_id = os.environ.get("ORCA_RUN_ID")
    node = os.environ.get("ORCA_NODE")
    session_id = os.environ.get("ORCA_SESSION_ID")
    sock_path = os.environ.get("ORCA_CHART_SOCK")
    if not all([run_id, node, session_id, sock_path]):
        raise RuntimeError("render_chart 不在 Orca run 上下文中（缺 ORCA_* env）...")

    # 2. sock path 长度检查（防御）
    if len(sock_path) > SOCK_PATH_MAX:
        raise RuntimeError(f"socket path too long ({len(sock_path)} > {SOCK_PATH_MAX})，"
                          f"改 ORCA_RUNS_DIR 到短路径")

    # 3. 降采样
    if len(data) > max_points:
        sys.stderr.write(f"chart '{title}' 数据从 {len(data)} 降采样到 max_points={max_points}\n")
        data = downsample(chart_type, data, max_points, hue)

    # 4. 构造 payload + 校验
    payload = {"chart_type": chart_type, "data": data, "label": label, "title": title,
               "x": x, "y": y, "hue": hue, "columns": columns}
    for k, v in [("pareto_direction", pareto_direction),
                 ("pareto_x_direction", pareto_x_direction),
                 ("pareto_y_direction", pareto_y_direction)]:
        if v: payload[k] = v
    validate_payload(payload)

    # 5. 大小检查
    msg = {"node": node, "session_id": session_id, "payload": payload}
    encoded = (json.dumps(msg) + "\n").encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError(f"chart payload too large ({len(encoded)} > {MAX_MESSAGE_BYTES})")

    # 6. socket 连接 + 发 + ack
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(ACK_TIMEOUT_SECONDS)
            s.connect(sock_path)
            s.sendall(encoded)
            ack_raw = s.makefile().readline()
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise RuntimeError(f"无法连接 Orca chart socket（{sock_path}）") from e
    except socket.timeout as e:
        raise RuntimeError(f"Orca chart socket ack timeout ({ACK_TIMEOUT_SECONDS}s)") from e

    if not ack_raw:
        raise RuntimeError("Orca chart socket 关闭，未收到 ack")
    ack = json.loads(ack_raw)
    if not ack.get("ok"):
        raise RuntimeError(f"Orca 拒收 chart：{ack.get('error')}")
    return ack["seq"]
```

### S3.6 单测

文件：`tests/chart/test_validate.py` / `test_downsample.py` / `test_render.py`（新）

关键用例：
- `test_validate_*`：合法 / 缺字段 / 类型错 / 未知 chart_type
- `test_downsample_line_with_hue_groups`：3 系列 × 5000 行 → 各组 ≤ 666 行
- `test_downsample_table_takes_first_n`：5000 行 → 前 2000
- `test_downsample_scatter_uniform_sample`：10000 行 → 2000 行（保留分布）
- `test_render_env_missing_raises`：清空 ORCA_* env → raise
- `test_render_socket_missing_raises`：sock 路径不存在 → raise
- `test_render_ack_timeout_raises`：mock socket readline 阻塞 > 10s → raise
- `test_render_oversize_payload_raises`：500k 行 + max_points=200000 → raise ValueError
- `test_render_success_returns_seq`：mock socket + ack → 返回 seq

---

## S4. ClaudeExecutor 接入 env overlay

### S4.1 改 `orca/exec/claude/executor.py::_build_spawn_config`

```python
env_overlay = build_env_overlay(
    profile.env_overlay_prefixes,
    run_id=run_id,
    node=node.name,
    session_id=session_id,
    chart_sock=str(_resolve_chart_sock_path(runs_dir, run_id)),  # 帮助函数
)
```

`_resolve_chart_sock_path` 帮助函数（新）：
- 解析 `runs_dir / f"{run_id}.sock"` 绝对路径
- 长度 > SOCK_PATH_MAX → log warning（不 raise，executor 路径只生成路径，ingestor 启动时才 raise）

### S4.2 同步改 opencode executor（如果存在）

`orca/profiles/builtin/opencode.py` 对应的 executor —— 需检查是否存在；如存在同样接入 4 个 keyword。

### S4.3 单测

文件：`tests/exec/claude/test_executor_env_inject.py`（新）
- `test_spawn_env_has_orca_run_id`：mock subprocess.Popen → 断言 env 含 4 个 ORCA_*
- `test_spawn_env_injects_correct_run_id`：run_id=foo → env["ORCA_RUN_ID"]=="foo"
- `test_long_sock_path_logs_warning`：mock runs_dir 深 → log warning（不 raise）

---

## S5. e2e + 压测 + opencode+deepseek 验证

### S5.1 e2e 测试 fixture

新建目录 `tests/e2e_phase13/`：
```
tests/e2e_phase13/
├── conftest.py                    # opencode+deepseek profile fixture
├── workflows/
│   ├── chart_demo.yaml           # script 节点调 chart_demo.py
│   ├── chart_parallel.yaml       # 3 节点并行各产 chart
│   └── chart_pressure.yaml       # 1 节点产 10 chart
├── scripts/
│   ├── chart_demo.py             # 调 orca.chart.render_chart 推 line chart
│   ├── chart_parallel.py         # 推 bar chart
│   ├── chart_pressure.py         # 循环推 10 chart 不同 label/title
│   └── chart_large.py            # 推 100k 行（验降采样）
└── test_*.py                     # 5 个 e2e 用例
```

### S5.2 e2e 用例（对齐 SPEC §8.3 / §8.4）

- `test_e2e_1_basic_chart.py`：合成 workflow → run → replay tape → 断言 custom(chart) 事件，node/session_id 正确
- `test_e2e_2_multi_run_parallel.py`：2 run 并行 → 断言 A tape 只含 A chart，B 同理
- `test_e2e_3_large_data_downsample.py`：100k 行 → 断言 tape 中 data 行数 ≤ 2000 + payload ≤ 2MB
- `test_e2e_4_oversize_rejected.py`：500k 行 + max_points=200000 → raise + tape 无事件
- `test_e2e_5_pressure.py`：3 run × 10 chart → 每个 tape 各 10 chart 事件，无丢失 / 串扰

### S5.3 opencode+deepseek 集成（SPEC §8.4 E2E-6）

`test_e2e_6_opencode_deepseek.py`（`@pytest.mark.integration`）：
- 用 `orca run` 跑 `workflows/chart_demo_opencode.yaml`（agent 节点用 opencode profile + deepseek-v4-flash）
- agent 调 Bash 工具 spawn `python chart_demo.py`
- 断言：
  - tape 含 `custom(chart)` 事件
  - TUI NodeDetail 图表 tab 显示该图（headless snapshot）
  - **每条 agent_message 在 TUI 流式 tab 都可见**（按收到的 N 条事件 → N 行，phase-12 §6.3）
  - 各 panel 渲染合理（拓扑图、NodeDetail、LogStream、ChartBrowser）

**关注点**（用户重点）：
- agent_message 是否每条完成都能在 TUI 看到（验证 opencode stream-json 不丢消息）
- TUI 各块显示是否合理（按 mockup 比对）
- render_chart 是否被正确推送（包括并行多节点场景）
- 图表排布是否合理（同 label 折叠、不同 label 分组）

### S5.4 测试后端配置

`tests/e2e_phase13/conftest.py` 提供：
```python
@pytest.fixture
def opencode_deepseek_profile():
    """opencode profile + deepseek-v4-flash。API key 走 env DEEPSEEK_API_KEY。"""
    ...
```

---

## 1. 时序与依赖

```
S1 (env)        ──┐
                 ├─→ S4 (executor 接入) ──┐
S2 (ingestor)   ──┤                        ├─→ S5 (e2e + 压测 + opencode)
                 │                        │
S3 (client lib) ──┘────────────────────────┘
```

S1 / S2 / S3 可并行（互不依赖）；S4 依赖 S1；S5 依赖 S2 / S3 / S4。

## 2. review checkpoint

每步完成后分发 code-reviewer + 自我 review；S5 完成后分发 test-coverage-e2e 做真跑 + 压测 + opencode+deepseek 验证。

## 3. 完成标准（DoD）

- [ ] S1–S5 全部单测通过
- [ ] phase-12 既有测试套件零回归（1133 passed）
- [ ] phase-9d Web 测试零回归
- [ ] SPEC §8 验收点逐条通过
- [ ] e2e-6 opencode+deepseek 真跑通过，TUI snapshot 留档
- [ ] e2e-5 压测 3 run × 10 chart 无丢失 / 串扰
- [ ] release note + CHANGELOG + CURRENT.md 更新

## 4. 风险与去风险

| 风险 | 去风险 |
|---|---|
| opencode Bash 工具不继承 env | e2e-6 blocker 验证；如失败，调研 opencode sandbox 配置 / 备选方案（stdin 路由参）|
| Unix socket 在 macOS 路径长度 | sock_path_length 单测 + ORCA_RUNS_DIR workaround 文档 |
| ingestor crash 重起竞态 | crash 重起单测；teardown 用 sock unlink + name 找 task 兜底 |
| chart 大数据压力 | e2e-3 / e2e-4 / e2e-5 三档覆盖 |
| TUI 在多 chart 场景渲染乱 | e2e-6 TUI snapshot 比对 mockup |

