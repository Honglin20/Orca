# 阶段 13 SPEC —— script-side render_chart 接入（env 身份路由 + per-run Unix socket + 大数据防御）

> **状态**：草稿（待监工确认后写实施计划）
> **依据**：[phase-9d-web-gate-chart.md](phase-9d-web-gate-chart.md) §2（图表契约）· [phase-12-cli-tui-redesign.md](phase-12-cli-tui-redesign.md) §2 §3.3（TUI 渲染已就绪）· [phase-3-events.md](phase-3-events.md) §3.3（身份模型：session_id）· [phase-4-exec.md](phase-4-exec.md) §4.3（ClaudeExecutor spawn env overlay）
> **范围**：① script 子进程内可调的 `orca.chart.render_chart` Python API；② per-run Unix socket ingestor（接收 script → emit 事件）；③ spawn 时 env 注入（run_id / node / session_id / socket 路径）；④ 大数据防御（自动降采样 + 硬上限）。
> **不是**：MCP 工具版 render_chart（已废弃，理由见 §0.4）；TUI / Web 渲染改动（已实现，零改动）；schema / tape / EventBus 结构改动（零改动）；chart 历史保留语义（实时替换，§6.2）；非 Python 脚本支持（§10）。
> **commit 规范**：`feat(chart):` 前缀，独立分支 `phase13-render-chart`

---

## 0. 阶段目标 + 铁律

phase 13 回答：**「agent spawn 的 script 子进程里调 render_chart 时，怎么把图绑定到正确的 run？多个 run 并行时怎么不串？chart 数据可能很大，tape 怎么不被撑爆？」**

### 0.1 七条铁律（违反即返工）

1. **chart 是事件不是图片**（沿用 phase-9d §0.1 #4）：script 调 `render_chart()` → emit `custom(kind=chart)` → tape → 三壳各自渲染。零新增真相源。
2. **身份路由 = env 继承，不是参数**：run_id / node / session_id 由 ClaudeExecutor spawn 时注入 env，沿 subprocess 链自然继承到 script；script 的 `render_chart()` **不接收这些参数**，从 env 读。multi-run 并行天然隔离（每条 spawn 链 env 独立）。
3. **per-run Unix socket**：每个 run 一个 `runs/<run_id>.sock`，socket 路径即 run 定位（不需要在 payload 里再带 run_id）。零端口冲突、零跨 run 路由层。
4. **唯一真相源不变**：tape 是真相；socket 是**传输通道**（不是存储），收到即 emit + 丢弃，socket 文件 run 结束删除。三壳只读 tape 渲染。
5. **大数据防御三道关**：① 客户端库自动降采样（`max_points` 默认 2000）；② 硬上限（post-downsample 整条 socket 消息 > 2 MB → fail loud）；③ tape 永不存储超限 payload（防撑爆）。常量同源 `orca/chart/_limits.py::MAX_MESSAGE_BYTES = 2 * 1024 * 1024`。
6. **dedup key = label + title**（沿用 phase-9d §2.7）：同 label+title 后续事件 → 前端替换不堆积（实时更新语义）。
7. **fail loud 优先**：script 不在 Orca 上下文（env 缺）→ raise；socket 连不上 → raise（不静默丢图）；payload 校验失败 → raise（不写半截事件）。

### 0.2 反模式（必须避免）

- ❌ 把 render_chart 实现 为 MCP 工具（claude 直接调）—— agent 不画图，**script 画图**。MCP 版废弃，理由见 §0.4。
- ❌ script 接收 run_id / node 作为函数参数 —— 会触发"agent 把别 run 的 id 传进来"风险；env 继承是单调信息流（Orca → claude → script），无法被 agent 注入。
- ❌ 全局 HTTP endpoint 接收 chart —— multi-run 路由复杂 + 端口冲突 + `orca run <yaml>` 一次性模式无 Web。
- ❌ 在 tape payload 里存 MB 级数据 —— replay 全量扫描，撑爆内存 + 慢。
- ❌ 在 phase-13 实现 workflow 级 chart（node=None）—— env 必有 ORCA_NODE；前端 `__workflow__` 桶属未来扩展点预留，本 SPEC 不产出对应事件。
- ❌ 把 chart 数据存到独立 sidecar 文件 —— 引入第二存储位置，tape 不自包含；YAGNI（§5 三道关足够）。
- ❌ 压缩 chart payload（gzip）—— 破坏 tape JSONL 人读性，replay 工具复杂化。
- ❌ 任何 widget / TUI / Web 改动 —— 渲染侧已就绪，本 SPEC 零前端改动。

### 0.3 与既有契约的关系（零冲突）

| 既有契约 | phase 13 处理 |
|---|---|
| `types.ts` ChartPayload | ✅ 完全复用，零字段改动（label/title/data/...）|
| phase-9d §2 dedup 语义（label+title 替换）| ✅ 沿用 |
| phase-12 §3.3 dispatch 分支（`custom(kind=chart)`）| ✅ 沿用，零改动 |
| phase-12 §2.2 ChartPanel 确定性 fold 投影 | ✅ 沿用 |
| `Event` schema（custom 类型 + data 自由 dict）| ✅ 零改动 |
| Tape / EventBus / Orchestrator | ✅ 零改动 |
| AgentToolsMcpServer | ✅ 零改动（render_chart 不走 MCP）|

### 0.4 为什么不是 MCP 工具（决策记录）

phase-10 §7 提过 `render_chart` MCP 工具占位，本 SPEC **明确废弃**，理由：

1. **谁画图？**：真实工作流里画图的是 **script**（claude 调 Bash → python train.py → train.py 里 plt 同款调 render_chart），不是 claude 直接调。MCP 工具版本解决的是错误问题。
2. **agent 不应关心 UI**：让 claude 学会"先 spawn 脚本再让脚本调 MCP"是双重复杂；script 内嵌 Python 调用更直接。
3. **身份路由反方向**：MCP 工具版要求 claude 显式传 orca_run_id/orca_node 路由参（agent 可被诱导传错值）；env 继承是**单向信息流**（Orca → 子进程），agent 无法干扰。

phase-10 §7 表格中的 `render_chart` / `ask_user` MCP 工具占位：`ask_user` 已实现（phase-11，agent 主动问是合理 MCP 用例）；`render_chart` 改由本 SPEC 实现，phase-10 文本 stale 不改（D3-b 同 phase-12 处理）。

**优先级说明（不堵死未来路）**：本 SPEC 不否认"agent 直接画图"场景的价值。但**优先级**：script 画图是 95% 真实工作流（训练曲线 / 监控 / 报告），agent 自画图是 5% 长尾。phase-13 聚焦高频场景；agent 自画图未来如需可补 MCP 版（与 ask_user 并存，不冲突）。

---

## 1. 整体架构（3 层 + 身份链）

```
┌─ ClaudeExecutor.exec(node, ctx) ─────────────────────────────────────────┐
│  build_env_overlay() 注入：                                                │
│    ORCA_RUN_ID     = ctx.run_id                                            │
│    ORCA_NODE       = node.name                                             │
│    ORCA_SESSION_ID = session_id   ← ClaudeExecutor 入口生成（与所有 agent_* 事件同源）│
│    ORCA_CHART_SOCK = str(runs_dir / f"{run_id}.sock")                      │
└────────────────┬──────────────────────────────────────────────────────────┘
                 │ subprocess.Popen(env=...)
                 ▼
┌─ claude -p（继承全部 ORCA_* env）──────────────────────────────────────────┐
│  claude 调 Bash 工具：python train_loss_chart.py                           │
└────────────────┬──────────────────────────────────────────────────────────┘
                 │ subprocess 继承 env（Bash 工具默认行为，不清 env）
                 ▼
┌─ train_loss_chart.py ─────────────────────────────────────────────────────┐
│  from orca.chart import render_chart                                       │
│  render_chart(                                                             │
│      chart_type="line", data=rows,                                         │
│      label="training", title="loss",                                       │
│      x="step", y="loss",                                                   │
│      # 注意：不传 run_id / node —— 从 env 读                                │
│  )                                                                         │
│                                                                            │
│  orca.chart.render_chart 内部（§4）：                                       │
│    1. 读 env：run_id / node / session_id / sock_path                       │
│       缺任一 → RuntimeError（fail loud）                                   │
│    2. ChartPayload 校验（§7.2）                                            │
│    3. 自动降采样（若 data 行数 > max_points，§5.1）                         │
│    4. 大小检查（post-downsample 仍 > 1 MB → fail loud，§5.2）               │
│    5. 连 sock_path，发 JSON：{node, session_id, payload}                    │
│    6. 等 ack（含 seq）；socket 不可达 → raise                              │
└────────────────┬──────────────────────────────────────────────────────────┘
                 │ Unix domain socket（per-run，文件系统寻址）
                 ▼
┌─ Orca 进程：RunHandle 启动时 create_task(chart_ingestor) ─────────────────┐
│  chart_ingestor(sock_path, bus, run_id)（§3）：                            │
│    accept connections in loop；每条消息：                                   │
│      await bus.emit("custom",                                              │
│                     {"kind":"chart", "chart":payload},                     │
│                     node=msg["node"], session_id=msg["session_id"])        │
│      → 收到 seq 后回 ack {ok:True, seq:N}                                  │
└────────────────┬──────────────────────────────────────────────────────────┘
                 │ 单一写路径（EventBus → Tape.append）
                 ▼
┌─ Tape (runs/<run_id>.jsonl) ──────────────────────────────────────────────┐
│  {"seq":N,"type":"custom","node":"train","session_id":"<uuid>",            │
│   "data":{"kind":"chart","chart":{...ChartPayload...}}}                    │
└────────────────┬──────────────────────────────────────────────────────────┘
                 │ 单一读路径（_consume_events / WS pump）
                 ▼
       TUI（phase-12 已实现）/ Web（phase-9d 已实现）零改动渲染
```

**核心不变量**：
- 身份维度（run_id / node / session_id）由 Orca 注入，沿 subprocess 链**单调向下**，agent / script 不可能反向伪造或污染其他 run。
- socket 是传输通道，不持久化任何状态；tape 仍是唯一真相源。
- 多 run 并行 = 多条独立 env 继承链 + 多个独立 socket 文件，零交叉。

---

## 2. 身份路由：env 注入契约

### 2.1 注入字段

`orca/exec/env.py::build_env_overlay` 在现有签名基础上**扩展 4 个 keyword 参数**（run_id / node / session_id / chart_sock）；ClaudeExecutor / opencode executor / 任何 executor 在 spawn 时显式传入。**缺则不注**（保持与现有调用方的 backward compat，便于逐步迁移）。

| env 变量 | 来源 | 用途 |
|---|---|---|
| `ORCA_RUN_ID` | `ctx.run_id` | script 反查所属 run（debug / 日志）|
| `ORCA_NODE` | `node.name` | chart 事件顶层 `node` 字段 |
| `ORCA_SESSION_ID` | `session_id`（executor 入口生成的 uuid） | chart 事件顶层 `session_id` 字段（与该次 agent spawn 的所有事件同源）|
| `ORCA_CHART_SOCK` | `runs_dir / f"{run_id}.sock"` 绝对路径 | socket 寻址 |

### 2.2 注入点（仅一处）

```python
# orca/exec/env.py::build_env_overlay（既有函数扩展，签名加可选参）
def build_env_overlay(
    prefixes: tuple[str, ...],
    *,
    run_id: str = "",
    node: str = "",
    session_id: str = "",
    chart_sock: str = "",
) -> dict[str, str]:
    """构造 spawn env overlay。prefixes 透传既有；ORCA_* 新增（chart 路由用）。"""
    overlay = { ... 既有 prefix 透传 ... }
    if run_id:    overlay["ORCA_RUN_ID"]     = run_id
    if node:      overlay["ORCA_NODE"]       = node
    if session_id: overlay["ORCA_SESSION_ID"] = session_id
    if chart_sock: overlay["ORCA_CHART_SOCK"] = chart_sock
    return overlay
```

`ClaudeExecutor._build_spawn_config` 把这 4 个值传进 `build_env_overlay`（其余 executor 同步，**executor-agnostic**）。

### 2.3 subprocess 链的继承保证

| 层 | env 继承行为 | 风险 / 兜底 |
|---|---|---|
| Orca → claude -p | `subprocess.Popen(env=overlay)`，overlay 必含 4 个 ORCA_* | spawn 前缺失 → fail loud（编程错误）|
| claude → Bash 工具 | claude Bash 工具 spawn 子进程默认继承父 env | Unix subprocess 默认继承父 env（POSIX 行为）。若某 backend sandbox 清 env（如 bwrap），script 端 §7.1 fail loud 兜底（**设计意图，非缺陷**） |
| Bash → script (`python foo.py`) | Unix 默认行为，子进程继承父 env | 同上 |

**验证手段**：§8.3 e2e 用真 claude / opencode 跑含 render_chart 调用的 script，断言事件落到正确 run 的 tape。

### 2.4 多 run 并行的天然隔离

```
Run A: spawn claude with ORCA_RUN_ID=A, ORCA_CHART_SOCK=.../runA.sock
        └── script 调 render_chart → 连 runA.sock → bus A → tape A

Run B: spawn claude with ORCA_RUN_ID=B, ORCA_CHART_SOCK=.../runB.sock
        └── script 调 render_chart → 连 runB.sock → bus B → tape B
```

两条链 env 完全独立，script 不可能"误连"别 run 的 socket（路径不同）。**这就是 §0.1 铁律 #2 的兑现**。

---

## 3. per-run chart ingestor（task）

### 3.1 启动时机

`RunHandle` 构造时（`orca/iface/web/run_manager.py::start_run`，或一次性模式 `orca.run` 入口）：

```python
# RunHandle.__init__ 或 start_run 内
sock_path = runs_dir / f"{run_id}.sock"
self._chart_ingestor = asyncio.create_task(
    chart_ingestor(sock_path, bus, run_id),
    name=f"orca-chart-ingestor-{run_id}",
)
```

ingestor 在 RunHandle 生命周期内常驻；run 终态时 cancel + 删 socket 文件（`_teardown_handle` 加一行 unlink，幂等）。

**RunHandle dataclass 字段扩展**：`_chart_ingestor: asyncio.Task | None = field(default=None, repr=False)`；`_teardown_handle` 末尾 `if self._chart_ingestor: self._chart_ingestor.cancel()` + `Path(self._runs_dir / f"{run_id}.sock").unlink(missing_ok=True)`。

**resume 边界（不支持）**：phase-3 §3.5 的 `resume=True` 重开 tape 模式下，RunHandle **不构造 chart ingestor**（sock 文件不创建）；script 调 render_chart 会因 socket 不存在 → §7.3 fail loud。这是 YAGNI 决策——resume+chart 真正成为痛点时另开 SPEC。

### 3.2 ingestor 实现（伪代码）

```python
# orca/events/chart_ingestor.py（新文件）
async def chart_ingestor(sock_path: Path, bus: "EventBus", run_id: str) -> None:
    """per-run Unix socket listener：收 chart 消息 → emit custom(chart) 事件。

    协议（newline-delimited JSON）：
      script → server: {"node": str, "session_id": str, "payload": ChartPayload}
      server → script: {"ok": bool, "seq": int?, "error": str?}

    协议细则：
      (a) 单行 UTF-8 JSON（json.dumps 默认转义 \\n，禁止 raw 换行）；server readline 保证一帧一消息。
      (b) 强制短连接——client 发 1 帧 → 等 ack → close；server 处理完一帧后 writer.close()，
          禁止 keep-alive。

    每条消息独立 accept（短连接，简单 + script 端易实现）。
    """
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():  # stale socket（前次 run crash 残留）
        sock_path.unlink()
    server = await asyncio.start_unix_server(_handle, path=str(sock_path))
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        sock_path.unlink(missing_ok=True)

async def _handle(reader, writer):
    try:
        line = await reader.readline()
        msg = json.loads(line.decode("utf-8"))
        # 校验
        node = msg.get("node"); sid = msg.get("session_id"); payload = msg.get("payload")
        if not isinstance(node, str) or not isinstance(sid, str) or not isinstance(payload, dict):
            await _ack(writer, ok=False, error="malformed message")
            return
        # 大小复核（防 script 端绕过 client lib 直接发巨型 payload）
        size = len(line)
        if size > _MAX_INCOMING_BYTES:  # §5.2，e.g. 2 MB
            await _ack(writer, ok=False, error=f"payload too large: {size} bytes")
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
```

**关键属性**：
- 短连接（每消息一 connect）：script 实现简单（send + recv ack + close）；ingestor 不维护连接状态。
- ingestor 不读 ORCA_* env（它在 Orca 进程内，env 是 script 的）；node/session_id 来自消息体（script 端从 env 读取后塞进消息）。
- 大小复核：客户端 lib 之外的直接 socket 写入也受限（防绕过）。

### 3.3 与 EventBus / Tape 的关系

- ingestor 调 `bus.emit(...)` 走**单一写路径**（与 orchestrator / executor / gate handler 同入口）。
- emit 内部 `tape.append(event)` → 落盘 + flush → 分配 seq。
- seq 回 ack 给 script，script 可日志记录（方便对账）。

### 3.4 错误兜底

| 失败模式 | ingestor 行为 |
|---|---|
| 单条消息解析失败 | 回 ack `{ok:False, error}`，不断 server |
| emit 抛（如 tape 写失败）| 回 ack error；server 继续服下一条 |
| server task 自身崩 | RunHandle 加 `add_done_callback(_on_ingestor_crash)`：log warning + 重起（重起前 `sock_path.unlink(missing_ok=True)` + 重新 `start_unix_server`）。**重起窗口期 in-flight chart 会丢**（符合 §0.1 #4 "收到即 emit + 丢弃"语义，不保证 exactly-once）；script 端在窗口期 connect 会失败 → §7.3 fail loud |
| run 结束 | `_teardown_handle` cancel task + unlink socket |

---

## 4. Python 客户端库 `orca.chart`

### 4.1 API

```python
# orca/chart/__init__.py（新模块）
from orca.chart._render import render_chart  # 公开 API

def render_chart(
    *,
    chart_type: str,            # line|bar|area|scatter|pareto|radar|table（types.ts 7 种）
    data: list[dict],           # 扁平 record array（迁移自 AgentHarness chart.py）
    label: str,                 # 分组键（同 label+title 替换非追加，dedup 维度 1）
    title: str,                 # 图标题（同 label 下唯一，dedup 维度 2）
    x: str = "",
    y: str = "",
    hue: str = "",
    columns: list[str] | None = None,
    pareto_direction: str = "",      # "max"|"min"|""（pareto 特有）
    pareto_x_direction: str = "",
    pareto_y_direction: str = "",
    max_points: int = 2000,          # 自动降采样阈值，§5.1
) -> int:
    """向当前 Orca run 推送一张图。返回分配的 seq。

    必须在 Orca 编排的 script 子进程内调用（env 含 ORCA_*）。直接 python 跑会 raise。

    同 label+title 的后续调用 → 旧图被前端替换（实时更新语义）。
    """
```

### 4.2 内部流程（伪代码）

```python
# orca/chart/_render.py
import json, os, socket
from orca.chart._validate import validate_payload
from orca.chart._downsample import downsample

def render_chart(*, chart_type, data, label, title, **kw) -> int:
    # 1. 读 env（身份路由）
    run_id     = os.environ.get("ORCA_RUN_ID")
    node       = os.environ.get("ORCA_NODE")
    session_id = os.environ.get("ORCA_SESSION_ID")
    sock_path  = os.environ.get("ORCA_CHART_SOCK")
    if not all([run_id, node, session_id, sock_path]):
        raise RuntimeError(
            "render_chart 不在 Orca run 上下文中（缺 ORCA_* env）。"
            "本函数仅可由 Orca 编排的 script 子进程调用。"
        )

    # 2. 降采样（§5.1）
    max_points = kw.pop("max_points", 2000)
    if len(data) > max_points:
        data = downsample(chart_type, data, max_points)
        # 不 raise，仅 stderr warning（透明降采样）

    # 3. 构造 ChartPayload + 校验（§7.2）
    payload = {
        "chart_type": chart_type, "data": data, "label": label, "title": title,
        "x": kw.get("x",""), "y": kw.get("y",""), "hue": kw.get("hue",""),
        "columns": kw.get("columns"),
        **{k: v for k, v in kw.items() if k.startswith("pareto_") and v},
    }
    validate_payload(payload)  # fail loud：缺字段 / 类型错 → raise

    # 4. 大小硬上限（§5.2）
    msg = {"node": node, "session_id": session_id, "payload": payload}
    encoded = (json.dumps(msg) + "\n").encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:  # 2 MB
        raise ValueError(
            f"chart payload too large ({len(encoded)} bytes > {_MAX_PAYLOAD_BYTES}). "
            f"减少 data 行数或显式调低 max_points。"
        )

    # 5. 连 socket + 发 + 等 ack
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.sendall(encoded)
            ack_raw = s.makefile().readline()
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise RuntimeError(
            f"无法连接 Orca chart socket（{sock_path}）。"
            f"Orca 进程可能已退出或 run 已结束。"
        ) from e

    ack = json.loads(ack_raw)
    if not ack.get("ok"):
        raise RuntimeError(f"Orca 拒收 chart：{ack.get('error')}")
    return ack["seq"]
```

### 4.3 不接收 run_id / node 参数（铁律 #2 兑现）

API 上**没有** `run_id` / `node` / `session_id` 参数。理由：
- 若允许 script 显式传 → agent 可诱导 script 传任意值，跨 run 串扰风险。
- env 继承是**单向**的（Orca → script），agent 无法干扰。
- script 作者零认知成本（不用关心 run_id 哪来）。

---

## 5. 大数据防御（三道关）

### 5.1 关一：自动降采样（client lib）

| chart_type | 降采样策略（data 行数 > max_points 时）|
|---|---|
| line / area / scatter | **按 hue 分组各自降采样**：每组 ≤ `max_points // hue_cardinality`（hue 不存在则单组）。line/area 按长度分桶取 (x_mean, y_mean)；scatter 均匀随机抽样。|
| bar / pareto | 按 x 分组聚合（sum/mean），取 top max_points 个 x。hue 存在时按 (x, hue) 聚合。|
| table | **取前 max_points 行**（top-N 语义；不取 head+tail，避免破坏用户排序语义）。原数据保留在 script。|
| radar | 不降采样（数据点本质少）。|

**默认 `max_points=2000`**：覆盖 95% 真实场景（训练曲线、监控、报告）。script 可显式覆盖（`render_chart(..., max_points=10000)`）。

**透明降采样**：不 raise，仅 stderr warning（"chart '<title>' 数据从 N 行降采样到 max_points=M，原数据保留在 script"）。

### 5.2 关二：硬上限 fail loud

post-downsample **整条 socket 消息字节长度**（含 `{node, session_id, payload}` envelope）> **2 MB** → raise `ValueError`。理由：
- 单事件 2 MB 落 tape → replay 全量扫描时单文件可能上 GB（10 个图就 20 MB），可读性 + 性能双崩。
- script 作者必须显式减少 data（降 max_points 或在 script 端预算聚合）。

**ingestor 端复核**（§3.2 `_MAX_INCOMING_BYTES` = 2 MB）：防绕过 client lib 直接写 socket。两端常量同源（`orca/chart/_limits.py::MAX_MESSAGE_BYTES`），envelope 含义一致。

### 5.3 关三：tape 不存超限 payload

§5.2 已保证：超过 2 MB 的 payload **不进 tape**（client 端 raise / ingestor 端 reject）。tape 单文件增长受限于"图表数 × 2 MB"，可控。

### 5.4 为什么不做 sidecar 文件（决策记录）

考虑过 `data > 阈值 → 写 runs/<run_id>.charts/<seq>.json`，tape 仅存引用。**否决**，理由：
1. **tape 不自包含**：replay / inspect / 备份需要两套文件，复杂度上升。
2. **GC 复杂**：被替换的 chart（同 label+title 后续事件）sidecar 文件何时删？删 = 破坏 immutability；不删 = 磁盘泄漏。
3. **YAGNI**：§5.1 + §5.2 已覆盖所有真实场景；2 MB 上限内 inline 是最简方案。
4. **未来真需要时**（如热力图百万像素）：再开独立 SPEC，本 SPEC 不预留钩子。

### 5.5 为什么不做 gzip 压缩（决策记录）

否决，理由：
1. tape JSONL 是人读格式（debug / grep / cat 都靠它），压缩破坏。
2. replay 工具需解压，复杂度扩散。
3. §5.1 降采样已经把数据量从"无界"变"有界"，压缩边际收益低。

---

## 6. custom(chart) 事件契约（零变化）

### 6.1 事件形状（与 phase-9d §2.2 / phase-12 §2.1 完全一致）

```jsonc
{
  "seq": 142,
  "type": "custom",
  "timestamp": 1789543200.5,
  "node": "train",                    // ← 来自 env ORCA_NODE
  "session_id": "<uuid>",             // ← 来自 env ORCA_SESSION_ID（与 agent spawn 同源）
  "data": {
    "kind": "chart",
    "chart": {                         // ← ChartPayload（types.ts）
      "chart_type": "line",
      "label": "training",             // dedup 维度 1
      "title": "loss",                 // dedup 维度 2
      "data": [{"step":1,"loss":0.9}, ...],
      "x": "step", "y": "loss"
    }
  }
}
```

### 6.2 dedup 语义（label + title 替换）

- 同 node + 同 label + 同 title 的后续 `custom(chart)` 事件 → 前端替换不堆积（phase-9d §2.7）。
- 不同 label → 多组（同 label 折叠）。
- 不同 title（同 label）→ 多图（同组内）。
- workflow 级（node 为 None）：本 SPEC 不支持（env 必有 ORCA_NODE，§7.1）。如未来需要，另议。

### 6.3 session_id 入库（不保留 iteration 历史）

`session_id` 来自 ClaudeExecutor 入口生成的 uuid（一次 agent spawn 一个），作为事件顶层字段入库（沿用 phase-3 §3.3 身份模型）。

**注意（dedup 语义覆盖 iteration）**：dedup 默认跨 session 替换（§6.4），因此 chart **不保留 iteration 历史**。如需保留：
- script 显式用不同 title（如 `f"loss_v{attempt}"`），让每次迭代产生独立图
- 或未来在 ChartPayload 加 `history: bool` 字段（另开 SPEC，本 SPEC 不预留）

这与 phase-3 §3.3「session_id 入库、attempt/turn 派生」的约定不冲突——流式事件按 session_id 分组保留全部；chart 因 dedup 替换语义只保留同 label+title 最新。

### 6.4 chart 是否替换跨 session 的旧图？

**默认替换**（同 node + label + title，无视 session_id 不同）—— 符合 phase-9d §2.7 实时更新语义。理由：
- 训练曲线场景：retry 后的新曲线应替换旧曲线（用户看最新）。
- dialog 场景：用户问"重画一下" → 新图替换旧图（同 label+title）。
- 极少数要"看演化历史"的场景 → script 显式用不同 title（如 `"loss_v{attempt}"`）。

**前端零改动**：phase-9d `dedupeByLabelTitle` + phase-12 ChartPanel 投影已按此语义实现。

---

## 7. 错误处理与 fail loud

### 7.1 env 缺失（script 不在 Orca 上下文）

```python
raise RuntimeError(
    "render_chart 不在 Orca run 上下文中（缺 ORCA_* env）。"
    "本函数仅可由 Orca 编排的 script 子进程调用。"
)
```

**场景**：用户 `python foo.py` 直接跑含 `render_chart()` 的脚本（开发期常见）。错误信息明确指向原因。

### 7.2 ChartPayload 校验（client lib）

```python
# orca/chart/_validate.py
def validate_payload(p: dict) -> None:
    if p.get("chart_type") not in _ALLOWED_TYPES:
        raise ValueError(f"未知 chart_type: {p.get('chart_type')!r}，允许：{_ALLOWED_TYPES}")
    if not isinstance(p.get("data"), list):
        raise ValueError(f"data 必须为 list，got {type(p.get('data'))!r}")
    if not p.get("label") or not isinstance(p.get("label"), str):
        raise ValueError("label 必须非空 str")
    if not p.get("title") or not isinstance(p.get("title"), str):
        raise ValueError("title 必须非空 str")
    # 各 chart_type 的特有校验（如 pareto 必有 pareto_direction 等）按需扩
```

校验**在 client lib 做**（写 tape 前），ingestor 端只复核大小。理由：错误信息回到 script / agent，可修；落 tape 后再发现已是脏数据。

### 7.3 socket 不可达

```python
raise RuntimeError(
    f"无法连接 Orca chart socket（{sock_path}）。"
    f"Orca 进程可能已退出或 run 已结束。"
)
```

**场景**：script 在 run 结束后才调 render_chart（如 async 长尾任务）。fail loud，让 script 看到 traceback。

### 7.4 ingestor 拒收（ack ok=False）

- payload 校验失败（client 已校，ingestor 复核）
- payload 超限（§5.2）
- emit 抛（tape 写失败等）

client 收到 ok=False → raise RuntimeError(error)。错误信息回 script，可见。

### 7.5 三层重试原则（项目铁律）

socket 写入本身**不重试**（短连接，简单）。失败 = Orca 进程级问题，重试无意义 → fail loud。如未来观测到瞬态失败（如 backlog 满），再加 transport 层重试（**单独 SPEC**，本 SPEC 不预留钩子）。

### 7.6 client ack timeout

client 端 `s.settimeout(10.0)`；ack 读取超时 → raise `RuntimeError("Orca chart socket ack timeout (10s)")`。Orca 正常 emit 是 ms 级，10s 足够覆盖 tape fs 慢 / ingestor 暂时卡住。无 timeout 会让 script（claude 子进程）挂死，连带 claude 超时。

### 7.7 socket 路径长度限制

sock_path 长度 > **90 字节**（macOS `sun_path` 104 / Linux 108，留余量）→ raise `RuntimeError`，错误信息建议用户改 `ORCA_RUNS_DIR` env 到短路径（如 `/tmp/orca-runs/`）。

`orca.run` / `orca serve` 启动时检测 `runs_dir` 解析后路径长度，若加上最长 run_id（约 40 字节）+ `.sock`（5 字节）会超 90 → log warning + 文档化建议。

---

## 8. 验收标准

### 8.1 单元测试

- [ ] `tests/exec/test_env.py`：`build_env_overlay(prefixes, run_id=..., node=..., session_id=..., chart_sock=...)` 注入 4 个 ORCA_*；任一 keyword 缺省 → 对应 env 不注（backward compat）。
- [ ] `tests/chart/test_validate.py`：合法 payload 通过；缺字段 / 类型错 / 未知 chart_type 各 raise；每种 chart_type 的特有校验。
- [ ] `tests/chart/test_downsample.py`：6 种 chart_type 降采样策略正确（line/area/scatter 按 hue 分组、table 取前 N、bar 按 x 聚合等）；hue 存在时分组降采样正确。
- [ ] `tests/chart/test_render.py`：env 全 → mock socket → 验发送消息正确 + ack 返回 seq == mock ingestor 分配的 seq；env 缺 → raise；socket 不存在 → raise；超限 payload → raise；ack timeout（mock readline 阻塞 > 10s）→ raise。
- [ ] `tests/events/test_chart_ingestor.py`：emit 后 `tape.replay()` 含 1 条 custom(chart) 事件 + ack seq == `tape.last_seq()`；malformed → ok=False；超限 → ok=False；run teardown → socket 文件删除；ingestor task crash → done_callback 重起。

### 8.2 集成测试

- [ ] `tests/exec/claude/test_executor_env_inject.py`：ClaudeExecutor spawn 时断言子进程 env 含 4 个 ORCA_*（mock subprocess）；缺 backward-compat 用例（旧调用方不传 4 个 keyword 仍可工作）。
- [ ] `tests/iface/web/test_run_manager_chart.py`：start_run → ingestor 起；script lib（fake）发图 → tape 落 custom(chart)；run 结束 → socket 文件删。
- [ ] `tests/chart/test_sock_path_length.py`：sock_path > 90 字节 → raise RuntimeError；ORCA_RUNS_DIR workaround 验证（短路径 → 不报）。

### 8.3 E2E（CI 可跑，无 API key）

- [ ] **E2E-1**：合成 workflow（`script` 节点调 `python chart_demo.py`，chart_demo.py 内调 `orca.chart.render_chart`）→ run → 完成 → replay tape → 断言含 custom(chart) 事件，node/session_id 正确。
- [ ] **E2E-2 multi-run 并行**：同时 start 2 个 run（A、B），各自的 script 调 render_chart → 断言 A tape 只含 A run_id 的 chart，B 同理；env 不串。
- [ ] **E2E-3 大数据（可客观断言）**：script 传 100k 行 data（fixture row schema `{"x": i, "y": float}` 共 ~25 bytes/row）→ 断言 tape 中 chart 事件 `data.chart.data` 行数 ≤ max_points（默认 2000）+ payload 编码字节 ≤ 2 MB。
- [ ] **E2E-4 超限**：script 传 500k 行 + max_points=200000（fixture 同 E2E-3 row schema → 12.5 MB 必然超 2 MB）→ 断言 client raise ValueError；tape 无对应事件。
- [ ] **E2E-5 压力测试（multi-run × chart 频率）**：3 个 run 并行 × 每个 run 10 张图（不同 label/title 组合），全部用 `orca.chart.render_chart` 推送 → 断言每个 tape 各 10 个 custom(chart) 事件，无丢失 / 错位 / 跨 run 串扰。

### 8.4 E2E（@pytest.mark.integration，**实施 blocker**，必须通过才能 ship）

- [ ] **E2E-6 opencode + deepseek**：agent 节点用 **opencode profile + deepseek-v4-flash 模型**（API 已配置）spawn `python train.py`，train.py 调 render_chart → tape 出图 → TUI / Web 显示。**断言**：(a) tape 含 custom(chart) 事件；(b) TUI NodeDetail 图表 tab 显示该图（headless snapshot）；(c) 每条 agent_message 在 TUI 流式 tab 都可见（按收到的 N 条事件 → N 行）；(d) 各 panel 渲染合理（拓扑图、NodeDetail、LogStream）。
  > **本阶段起测试后端固定使用 opencode + deepseek-v4-flash，不再使用 claude 作为后端测试**（CLAUDE.md 已记录）。

### 8.5 七条铁律（§0.1）

- [ ] **chart 是事件**：grep 断言 ingestor 唯一调用 `bus.emit("custom", ...)`，无第二条 emit 路径。
- [ ] **身份路由 = env**：`grep "ORCA_RUN_ID\|ORCA_NODE\|ORCA_SESSION_ID\|ORCA_CHART_SOCK"` 仅出现在 `exec/env.py` + `chart/_render.py` + 测试。
- [ ] **per-run Unix socket**：`runs/<run_id>.sock` 存在性测试（run 进行中存在，结束删除）。
- [ ] **唯一真相源**：ingestor 不写文件、不存状态；socket 仅传输。
- [ ] **大数据三道关**：单测覆盖降采样 + 硬上限 + ingestor 复核。
- [ ] **dedup key = label+title**：前端既有逻辑（phase-9d / 12）零改动，回归测试通过。
- [ ] **fail loud**：所有失败模式（§7.1–7.4）单测 raise，无静默。

### 8.6 三壳零改动回归

- [ ] phase-12 TUI 测试套件全过（1133 passed 不回归）。
- [ ] phase-9d Web 测试套件全过。
- [ ] `orca serve` 起后注入 chart 事件 → TUI + Web 渲染（既有的）。

---

## 9. 给后续阶段的契约

| 后续 | phase 13 提供 |
|---|---|
| `orca serve` / `orca mcp --with-web` | chart ingestor 自动起（同 RunHandle 生命周期），Web 前端零改动 |
| `orca run <yaml>` 一次性 | 同上（RunHandle 在 `orca.run` 入口也构造 ingestor）|
| 非 Python script（Node/Shell/R） | ingestor 协议是 newline-delimited JSON over Unix socket，任何语言可实现客户端；本 SPEC 不提供 |
| chart 历史保留（不替换）语义 | 未来需要时：扩 ChartPayload 加 `history: bool`，前端 dedup 逻辑改 if 分支；本 SPEC 不预留 |
| workflow 级 chart（node=None）| 本 SPEC 不支持（env 必有 ORCA_NODE）；未来需要 → ingestor 收空 node 也接受，TUI 已有 `__workflow__` 桶 |
| 压缩 / sidecar 大文件优化 | §5.4 / §5.5 否决；真需要时另开 SPEC |

---

## 10. 不做的事（边界）

- ❌ **MCP 工具版 render_chart**（§0.4 决策记录：废弃，方向错误）。
- ❌ **接收 run_id / node 作为 API 参数**（铁律 #2，env 继承是单调信息流）。
- ❌ **chart 数据 sidecar 文件**（§5.4，YAGNI）。
- ❌ **gzip 压缩 chart payload**（§5.5，破坏 JSONL 人读性）。
- ❌ **TUI / Web / types.ts 任何改动**（渲染已就绪）。
- ❌ **schema / tape / EventBus 结构改动**（铁律 #1，chart 是事件）。
- ❌ **跨语言客户端**（Python only，§10 边界；协议开放，第三方可实现）。
- ❌ **chart 历史保留语义**（实时替换是默认，§6.4；其他场景 script 用不同 title）。
- ❌ **chart ingestor 跨 run 共享 / 跨进程**（per-run，单进程）。
- ❌ **socket 写入重试 / 退避**（§7.5，Orca 进程级失败重试无意义）。

---

## 11. 关键决策备忘（防 drift）

1. **render_chart 不是 MCP 工具**（§0.4）—— script 内 Python 调用，env 继承路由。MCP 版废弃。
2. **身份路由 = env 继承**（§0.1 #2 / §2）—— 单向信息流，agent 无法干扰。multi-run 并行天然隔离。
3. **per-run Unix socket**（§0.1 #3 / §3）—— socket 路径即 run 定位，零端口冲突、零跨 run 路由层。
4. **chart 是事件**（§0.1 #1 / §6）—— 沿用 phase-9d / phase-12 契约，零 schema 改动。
5. **dedup key = label + title**（§0.1 #6 / §6.2）—— 沿用 phase-9d §2.7，**跨 session_id 也替换**（默认实时更新）；**chart 不保留 iteration 历史**（与 §6.3 一致）。
6. **大数据三道关**（§5）：自动降采样（max_points=2000）+ 硬上限（**2 MB fail loud，含 envelope**）+ tape 拒收超限。两端常量同源 `orca/chart/_limits.py::MAX_MESSAGE_BYTES`。
7. **不做 sidecar / gzip**（§5.4 / §5.5）—— YAGNI；inline + 降采样 + 上限已足够鲁棒。
8. **session_id 不保留 iteration 历史**（§6.3）—— dedup 跨 session 替换；如需保留 history 用不同 title 或未来扩 `history: bool`。
9. **executor-agnostic**（§2.1）—— env 注入对 claude / opencode / 任何 executor 同样工作。
10. **fail loud 9 处**（§7）—— env 缺、payload 校验失败、socket 不可达、ingestor 拒收、大小超限、ack timeout（§7.6）、socket 路径过长（§7.7），全部 raise，无静默。
11. **三壳零改动**（§8.6）—— phase-13 是生产者侧接入，TUI/Web 渲染侧不动，1133 passed 不回归。
12. **client lib 不接收身份参数**（§4.3）—— 杜绝 agent 诱导 script 传错 run_id 的攻击面。
13. **不支持 resume+chart 共存**（§3.1）—— YAGNI；resume 模式不起 ingestor，script 调 render_chart fail loud。真痛点时另开 SPEC。
14. **socket 路径长度限制**（§7.7）—— > 90 字节 fail loud；`ORCA_RUNS_DIR` workaround。
15. **测试后端固定 opencode + deepseek-v4-flash**（§8.4）—— 不再用 claude 作为后端测试（CLAUDE.md 已记录）。
