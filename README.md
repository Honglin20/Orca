# Orca

vendor-neutral、event-sourced、可视化的 coding-agent 编排控制平面——把 claude / codex / opencode 编进一个 DAG workflow，事件流（tape）是唯一真相源，支持人机决策门（gate）、mid-run 中断纠偏、时间旅行回放、CLI / Web / MCP / in-session 四入口。

> 设计决策见 [docs/TASK.md](docs/TASK.md)；数据/事件契约见 [docs/specs/](docs/specs/)。

---

## 安装

```bash
uv sync                                                       # Python 依赖（Python ≥ 3.10）
```

Web UI 的前端构建产物**已随仓库提交**（`orca/iface/web/static/`），`uv sync` / `pip install -e .` 后直接可用，**无需 npm build**。仅当你要**改前端代码**时才需要 Node：

```bash
cd orca/iface/web/frontend && npm install && npm run build && cd -   # 仅前端开发（改前端后重 build）
```

装好后，把 Orca 的宿主集成（skill + in-session）一次性装到全局：

```bash
orca install                    # 全局装 skill + in-session（Claude Code + opencode 两边）
orca install --target opencode  # 只 opencode
orca install --target claude    # 只 Claude Code
orca install --scope project    # 改装到当前项目（.opencode/ + .claude/），而非全局
```

`pip install orca` 后直接用 `orca ...`（无需 `uv run`）。`orca skill install` 已弃用为 `orca install` 的别名（warn + 委托，向后兼容）。

跑含 `kind: agent` 的 workflow（CLI / Web / MCP 三壳）需要本机有对应后端 CLI 并配好 API。纯 `script` / `set` 的 workflow 零 token、秒级跑完。in-session 壳直接用宿主（opencode / Claude Code）主 session 的 model，不需要单独配后端。

---

## 命令总览

| 命令 | 用途 |
|---|---|
| **跑 / 校验 workflow** | |
| `orca run <yaml> [task] [-i key=value]... [--max-iter N] [--background\|--tui] [--port N] [--stay]` | 跑 workflow（**默认起 Web UI 监控**，浏览器自动开 `/runs/<id>`，跑完自动退；`--tui` 进 Textual TUI；`--background` 后台 headless） |
| `orca validate <yaml>` | 只校验 schema + DAG，不跑 |
| `orca list` | 列可用 workflow（扫 `./workflows` + `~/.orca/workflows`） |
| **后台 run 管理** | |
| `orca ps` | 列全部 background run（dead pid 标 crashed） |
| `orca logs <run_id> [-f] [-n N]` | 查 / tail 后台 run 日志 |
| `orca wait <run_id> [--timeout N]` | 阻塞到终态（exit 0 完成 / 1 失败 / 2 not-found / 3 超时） |
| `orca resume <run_id 或 tape 路径> [--yaml ...]` | 崩溃后续跑：Tape 即 checkpoint，重放到崩溃点继续 |
| **后端二进制配置（`orca executor`，唯一真相源）** | |
| `orca executor show [profile]` | 生效命令 + 每字段来源（env / 项目 / 用户 / default） |
| `orca executor set <profile> [--binary ...] [--flags ...] [--prompt-channel stdin\|argv] [--scope project\|user]` | 三维任组覆盖 |
| `orca executor unset <profile> [binary\|flags\|prompt_channel\|all]` | 恢复 default |
| `orca executor list` | 列可用 profile + 标 * 哪个被 override |
| `orca executor test <profile>` | 真起子进程自检：✓ 端到端 OK / ✗ 给原因 |
| **宿主集成安装** | |
| `orca install [--target claude\|opencode\|all] [--scope user\|project]` | 统一装 skill + in-session，全局默认 |
| `orca skill install [--target ...]` | **已弃用** → `orca install` |
| **in-session shell（`orca in-session ...`）** | |
| `orca in-session start <wf> [--model provider/model]` | **Claude Code 专用**：起一个 run（写 marker + 打印 settings.json hook 片段） |
| `orca in-session status [run_id] [--json]` | 看 run 进度（读 tape replay_state） |
| `orca in-session stop [--owner <sid> \| <run_id>]` | 停 run（清 marker + emit cancelled） |
| `orca in-session doctor` | 钩子诊断（transform 入口 / idle 推进是否真 fire，心跳作证；见下「钩子诊断」） |
| `orca in-session serve` | 无头 CI / 长跑批处理 daemon |
| `orca in-session bootstrap` / `next` | 内部命令（由 plugin / hook 自动驱动，用户不直接敲） |
| **Web / MCP** | |
| `orca serve [--port 7428]` | 启动 Web UI 常驻 server（实时 DAG / 日志 / gate / chart；可 attach 任意 run 的 tape） |
| `orca open <run_id> [--tape <path>] [--port N]` | 用 Web UI 打开一个**已存在**的 run（自动起/复用 serve + attach 它的 tape + 开浏览器）——监控 `--background` / in-session run 用这个 |
| `orca mcp [--with-web]` | 启动 MCP server（stdio JSON-RPC，供 Claude Code / opencode / Cursor 接入） |

退出码：`orca run` completed→`0` / failed→`1` / 参数或校验错→`2`。

---

## 跑一个 workflow（CLI）

### 1) 零门槛：纯 script（不需要后端）

```bash
orca run examples/demo_linear.yaml     # a → b → c 线性推进，Textual TUI 实时显示 DAG + 日志
```

### 2) 含 agent 的真实 workflow

```bash
orca run examples/demo_mixed.yaml --background
# Started background run: demo_mixed-20260702-075144-848f0c   PID: 43779
orca wait demo_mixed-20260702-075144-848f0c   # 阻塞到完成
orca logs demo_mixed-20260702-075144-848f0c   # 看日志
```

事件流（tape，`runs/<run_id>.jsonl`）是唯一真相源：`workflow_started` → `node_started`/`node_completed`/`route_taken` … → `workflow_completed`。崩溃后 `orca resume` 从 tape 重放续跑。

### TUI 内交互（`orca run` 时）

| 键 | 动作 |
|---|---|
| `q` | 退出 |
| `g` | 跳到 gate（人机决策门）|
| `Ctrl+G` | **中断 / 纠偏**：弹 InterruptModal，选 CONTINUE（填 guidance 重 spawn）/ SKIP（跳下游 node）/ ABORT |
| `d` | **对话**：node 跑完后多轮追问 agent |

### 后端命令配置（`orca executor`）

每个 backend 最终拼出的命令（binary + flags + prompt 投递）**完整可见、任意可改**——换平台不用改源码。`show` 是唯一真相源：

```bash
orca executor show opencode
orca executor set opencode --binary nga --flags "run --format json"   # 把 opencode 后端换成 nga
orca executor test opencode                                           # 真起子进程自检
```

优先级（per-profile per-field）：`shell env` > 项目 `.orca/config.json` > 用户 `~/.orca/config.json` > profile default。临时覆盖单次：`ORCA_OPENCODE_CLI=nga orca run ...`。详见 [`docs/releases/2026-07-02-executor-config.md`](docs/releases/2026-07-02-executor-config.md)。

---

## in-session shell（在 opencode / Claude Code 主 session 里跑）

前三壳（CLI / Web / MCP）都是 Orca 起子进程跑 workflow。**in-session shell 反过来**：宿主（opencode / Claude Code）的**主 session 用自带 subagent 执行每个节点**，Orca 只独占 tape + 确定性算下一步 + plugin/hook 自动推进。真相源仍是 Orca 单 tape。

**适用**：想让"你正在对话的主 session"亲自跑完整 workflow（保留主 session 上下文、用宿主原生 subagent），而非 fire-and-forget 给 Orca 子进程。

### 安装（一次性，全局）

```bash
orca install --target opencode   # opencode：plugin + /orca 命令 + opencode.json 声明 + skill
orca install --target claude     # Claude Code：skill（in-session hooks 是 per-run，见下）
```

> opencode 靠 `opencode.json` 的 `"plugin"` 声明加载 plugin（**无目录自动发现**，spike 实证 opencode 1.14.22），`orca install` 自动合并该声明。CC 的 in-session Stop/PostToolUse hook 内嵌 run_id/tape（per-run），无法全局装。

### 在 opencode 里用（交互 TUI 或 `opencode serve`）

装好**重启 opencode**，在会话里敲 `/orca`：

| `/orca <sub>` | 用途 |
|---|---|
| `/orca doctor` | **钩子诊断**（transform 入口 / idle 推进是否真 fire）——能回报告 = CLI 可达；用法见下「钩子诊断」 |
| `/orca run <wf.yaml>` | 跑 workflow：注入 entry prompt → 主 session 派 task subagent 逐节点执行 → `session.idle` 自动推进 → 直到 `workflow_completed` |
| `/orca status` | 看 run 进度 |
| `/orca stop` | 停 run（清 marker + emit `workflow_cancelled`） |

流程：敲 `/orca run wf.yaml` → opencode 展开成 marker `<!--orca:cmd run wf.yaml-->` → Orca plugin 的 `experimental.chat.messages.transform` 钩子检测 marker → 调 `orca in-session bootstrap/next` CLI → 把消息改写成节点 entry prompt（**模型只见节点 prompt，不见 marker/原命令**）→ 模型用 task subagent 执行 → 每节点 turn 结束（`session.idle`）plugin 自动提取 subagent 输出 + 推进下一节点。

### 在 Claude Code 里用

CC 无 transform plugin，run 由 Stop/PostToolUse **hook** 驱动（每节点一 turn）：

```bash
orca install --target claude        # 装 skill（一次性）
orca in-session start my_wf.yaml    # 每次起 run：写 marker + 打印 settings.json hook 片段
```

把 `start` 打印的片段贴进 `.claude/settings.json`，CC 主 session 派 Task subagent 执行节点，Stop hook 自动 spawn `orca in-session next` 推进。

### 钩子诊断（判定 transform / idle 在你的环境是否生效）

`experimental.chat.messages.transform`（入口）是 opencode **实验性**钩子，部分 fork（如 NGA）未接线会导致 `/orca` 入口瘫。`/orca doctor` 用**心跳作证**诊断两钩子是否真 fire，作为是否保留 transform 的依据。

**诊断开关**：环境变量 `ORCA_DIAGNOSE=1`。开启时 plugin 在两钩子顶部写心跳（`runs/.orca-probe-entry.json` / `runs/.orca-probe-advance.json`）；未设 / `=0` 时**零 I/O**（生产态保持关闭）。

```bash
export ORCA_DIAGNOSE=1        # 在启动 opencode 的 shell 设
# 重启 opencode（plugin 加载时读 env），在 session 里随便聊几句（触发 transform + idle）
/orca doctor                  # transform 活就走它；瘫了就终端跑：orca in-session doctor
```

doctor 报告 4 项（status = pass/unknown/fail）：
- `diag_switch`：诊断开关当前状态。
- `entry_hook`：transform 是否 fire（诊断开 + 无心跳 = **FAIL** → 本环境未接 transform）。
- `advance_hook`：idle 是否 fire（无心跳只算 unknown，idle 是稳定钩子）。
- `cli_imports_ok`：CLI 后端依赖可导入。

**决策矩阵**（据 entry_hook / advance_hook）：
- entry FAIL + advance PASS → fork 砍 transform、留 idle → 删 transform，入口走 prompt-command。
- entry PASS + advance PASS → 两钩子都活 → 保留 transform（确定性拦截）。
- 两 FAIL → fork 砍更多，另议。

定论后 `unset ORCA_DIAGNOSE`（关诊断，零开销），可选 `rm runs/.orca-probe-*.json` 清心跳。

### 约束

- **CLI = 唯一大脑 + 唯一 tape 写者**；plugin / hook = 哑传输（只 spawn CLI + parse JSON + marker 派发，零 Orca 业务逻辑）。
- v1 仅 **agent 节点**（parallel / foreach / gate 会 fail loud 指引走 TUI/Web）。
- opencode 1.14.22 实测可用（入口 `experimental.chat.messages.transform`）。详见 [设计草稿](docs/specs/in-session-shell-design-draft.md)。

---

## workflow 写法

workflow 是 YAML：`entry` + `nodes`（每节点 `kind` + `prompt`/`command` + `routes`）+ `outputs`。节点间用 `{{ <node>.output }}` 接线。

### 节点 kind

| kind | 干什么 | 需后端？ |
|---|---|---|
| `script` | 跑 shell 命令，stdout 存 `output.stdout` | 否 |
| `set` | 设字面量/计算值，常作条件分支 + fan-in | 否 |
| `agent` | spawn 后端 CLI（claude/opencode）跑 prompt，输出即节点输出 | 是（in-session 壳用宿主 session） |
| `parallel` / `foreach` | 并行 / 数组分批（仅 CLI/Web/MCP 壳） | 看 sub-node |
| `wait` | `duration`（`"30s"`/`"5m"`/Jinja2）sleep，可被 Ctrl+G 打断 | 否 |

### 节点级 feature

| feature | 用法 | 示例 |
|---|---|---|
| **Retry** | node 下 `retry:`（max_attempts / backoff / retry_on / jitter），transient 失败自动重试 | `examples/with_retry.yaml` |
| **Validator** | node 下 `validator:`（criteria + max_retries），LLM 二次校验 output 语义，失败带 issues 重跑 | `examples/with_validator.yaml` |
| **ask_user** | agent prompt 里调 `ask_user(prompt, options)`，经内嵌 MCP 路由到 CLI AskGate | `examples/with_ask_user.yaml` |
| **条件路由** | `routes:` 下 `when: "<jinja>"` | `examples/demo_conditional.yaml` |
| **Dialog** | node 跑完 TUI 按 `d` 多轮对话 | `examples/with_dialog.yaml` |

> Retry 的 `retry_on` 白名单与 executor 产出的 `node_failed.error_type` 对齐；用户 Ctrl+G 触发的中断不重试。Validator 与 Retry 是独立预算。

### 用 AI 生成 workflow（create-workflow skill）

手写 YAML 门槛高。`orca install` 已装好 `create-workflow` skill，在 Claude Code / opencode 里直接用自然语言提需求即可：

- **从零描述**：「我要一个调研 workflow：拆问题 → 两个 researcher 并行 → synthesizer 合并。」
- **转换既有素材**：「把 `xxx/` 下的 agent md / 别家 workflow 转成 Orca workflow。」

skill 会归一化成 DAG → 写 YAML + agent md → 跑 `orca validate` 自修到 0 error → 报告路径。详见随包 `SKILL.md`。

### 示例（`examples/`）

| 文件 | 演示 | 需后端？ |
|---|---|---|
| `demo_linear.yaml` | 纯线性 a→b→c | 否 |
| `demo_loop.yaml` / `demo_max_iter.yaml` | 回环 / 不终止 → failed | 否 |
| `demo_foreach.yaml` / `demo_parallel.yaml` | 数组分批 / 并行汇聚 | 否 |
| `demo_failure.yaml` | 非零退出被记录 | 否 |
| **`demo_mixed.yaml`** | **综合（script + agent + 条件分支）** | 是 |
| `demo_conditional.yaml` / `demo_task.yaml` | 条件分支 / task 注入 | 是 |
| `demo_interrupt.yaml` / `demo_skip.yaml` | Ctrl+G 中断 / SKIP | 是 |
| `with_retry.yaml` / `with_validator.yaml` / `with_ask_user.yaml` / `with_wait.yaml` / `with_dialog.yaml` | 各 feature | 是 |
| `nas.yaml` / `batch_assess.yaml` / `parallel_research.yaml` / `mxint_analysis.yaml` / `render_chart.yaml` | 真实 workflow | 是 |

---

## Web UI / MCP

```bash
orca serve                # 常驻 Web UI server（阻塞至 Ctrl-C，默认监听 0.0.0.0:7428）
orca mcp                  # stdio JSON-RPC，供 Claude Code / opencode / Cursor 接入
orca mcp --with-web       # 同进程挂 Web UI，stdin EOF 后转 daemon
```

**监听地址 / 端口**（`serve` / `run` / `open` 三命令统一）：默认 `0.0.0.0:7428`（**远程服务器可达**）；本地想限回环用 `--host 127.0.0.1`。env `ORCA_WEB_HOST` / `ORCA_WEB_PORT` 同效。打印的 URL 用本机实际 IP（`ORCA_PUBLIC_HOST` 可显式覆盖，容器/反代场景）。

**阻塞语义**：
- `orca serve` **常驻阻塞**，直到 Ctrl-C（适合服务器长开监控）。
- `orca run`（默认 web）run 跑完 + 无人看（无 WS）N 秒后**自动退**；`--stay` 改为常驻（`ORCA_WEB_AUTOEXIT_SECONDS` 可调窗口）。

Web UI（v2，单 run/页监控面板）：左 Agents rail / 中 `[会话 | 图表]` 页签（markdown + 工具折叠 + thinking + chart）/ 右 LogStream 常驻。tape 唯一真相源，前端纯渲染。**无 run 列表 / +New / Replay**（多 run 管理后置）。MCP 暴露 `start_workflow` / `get_task_status` / `resolve_gate` / `cancel_task` 四件套。

### 运行 + Web 监控（三种）

```bash
# 1) 默认：orca run 直接起 web 监控（run 在 orca run 进程内，浏览器自动开）
orca run examples/demo_mixed.yaml
#   → 浏览器开 /runs/<id>；run 跑完 + 无人看（无 WS）N 秒后自动退。--stay 常驻；--tui 改进 TUI。

# 2) 监控一个 --background run（独立进程，web 经 tape attach 只读 tail-follow）
orca run examples/demo_mixed.yaml --background   # → run_id
orca open <run_id>                                # → 起/复用 serve + attach + 开浏览器（observe-only）

# 3) 监控一个 in-session run（宿主驱动）：在 opencode 里 /orca open <run_id>
```

> **web 能监控任意 run** 的关键：`orca open` / `/orca open` 把 web **attach** 到该 run 的 tape（`runs/<run_id>.jsonl`，read-only，不抢写者 flock）。attached run 是 observe-only（gate 模态禁提交，去该 run 自己的 shell 作答）。大 tape 自动切窗口模式（`/meta` + tail + 上滚懒加载），开得快。
>
> **macOS 注**：`orca run` 默认起的 web，只要浏览器 tab 还开着（WS 活跃）就不会自动退——这是"有人看就不退"的语义；关 tab 后 N 秒退（`ORCA_WEB_AUTOEXIT_SECONDS` 可调）。

---

## 测试

```bash
uv run pytest -q                     # 单元 + script demo（不含真后端 / 浏览器）
uv run pytest -q -m integration      # 真后端 + 浏览器 E2E（慢）
cd orca/iface/web/frontend && npm test   # 前端 vitest
```
