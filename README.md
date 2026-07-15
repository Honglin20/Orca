# TARS / Orca

vendor-neutral、event-sourced 的 coding-agent 编排控制平面——把 claude / codex / opencode 等编进一个 DAG workflow，事件流（tape）是唯一真相源，支持人机决策门（gate）、mid-run 中断、回放、多入口。

**三套命名**（in-session v5 收口）：
- **TARS** —— 用户面前的 **skill**。主 session 里说「用 TARS 做 X」，它自动找匹配的 workflow 并驱动完成。
- **`tars`** —— 后端/运维 CLI（`install` / `run` / `serve` / `validate` / `list` / `mcp` …），operator 装/跑/管用。
- **`orca`** —— in-session 驱动 CLI（`list` / `next` / `status` / `stop` / `open` / `doctor`）。TARS skill 在后台调它推进 workflow。

> 设计决策见 [docs/TASK.md](docs/TASK.md)；数据/事件契约见 [docs/specs/](docs/specs/)；in-session 契约见 [docs/specs/in-session-entry-and-simplification.md](docs/specs/in-session-entry-and-simplification.md)。

---

## 安装

```bash
uv sync                                    # Python ≥ 3.10（或 pip install -e .）
# 仅改前端时才需 Node：cd orca/iface/web/frontend && npm install && npm run build && cd -
```

Web UI 前端产物已随仓库提交（`orca/iface/web/static/`），装后直接可用，无需 npm build。

把宿主集成（TARS skill + nudge/plugin）一次性装到全局——**四前端 cc / opencode / cac / nga**：

```bash
tars install                     # 全局装全套（cc + opencode + cac + nga）
tars install --target cc         # 只 Claude Code
tars install --target cac        # 只 CAC（≡ Claude Code，.claude→.cac）
tars install --target opencode   # 只 opencode
tars install --target nga        # 只 NGA（≡ opencode，.opencode→.nga）
tars install --scope project     # 装到当前项目（.<dotdir>/）而非全局
```

- **cc 家族**（`cc` / `cac`）：skill + nudge Stop-hook（`hooks/orca-nudge.sh` + `settings.json` 声明，提醒主 session 调 `orca next`，不自动推进）。
- **opencode 家族**（`opencode` / `nga`）：skill + plugin `orca.ts`（idle nudge）+ `opencode.json` plugin 声明。
- install **同时把 `workflows/*.yaml` 部署到 `~/.orca/workflows/`**（全局内置，任何目录的 `orca list` 都能扫到）。

`pip install orca` 后 `tars` 与 `orca` 直接可用（无需 `uv run`）。跑含 `kind: agent` 的 workflow 需本机有后端 CLI + API；纯 `script` / `set` 零 token 秒级跑完；in-session 壳直接用宿主主 session 的 model。

### 注册自己的 workflow（让 TARS 找得到）

`orca list` 扫两个目录：`./workflows/`（项目级）+ `~/.orca/workflows/`（全局，install 部署的内置）。注册 = 把 workflow YAML 放进其一：

- **全局内置**（随包发）：放仓库 `workflows/`（已版本化；`tars install` 自动部署到 `~/.orca/workflows/`）。当前内置：`nas-agent-pipeline`（端到端 NAS 流水线）。
- **项目级**：放该项目的 `./workflows/`。

TARS skill 据 workflow 的 **`description`** 语义匹配用户意图（说「用 TARS 优化模型结构」→ 匹中 NAS workflow；多个候选则简短问选哪个）。description 写清楚 = 自动匹中。用 `create-workflow` skill（`tars install` 已装）自然语言生成 YAML（一句话需求 → 合规 DAG + agent md + 自校验）。

---

## 用法：在主 session 里（TARS）

装好**重启宿主**（CC / opencode / cac / nga），在主 session 里直接说：

> 「用 TARS 帮我优化模型结构」「用 TARS 跑 X workflow」「TARS，调研一下 Y」

TARS skill 自动驱动：`orca list` 选 wf（据 description）→ 据 `inputs_schema` 抽 inputs（缺的才问）→ `orca <wf> --inputs` 启动 → 派 Task 子代理逐节点执行 → `orca next --run-id --output` 循环到 `done:true`。卡住时 nudge hook 提醒主 session 调 next（**不自动推进**，B 路径铁律）。

手动驱动（不等 skill）：

```bash
orca list                                    # 列 workflow（name + description + inputs_schema）
orca nas-agent-pipeline --inputs '{...}'     # 启动 → run_id + 首节点 prompt + 驱动协议
orca next --run-id <id> --output '<产出>'     # 推进一步（循环到 done）
orca status [--run-id <id>]                   # 看进度（无参=活跃 run 列表）
orca stop --run-id <id>                       # 停 run（清 marker + emit cancelled）
```

失败 fail loud：产出畸形 → `orca next` 返 `{done:true, error_kind:"output_schema_mismatch", reason:"failed: ..."}` exit≠0。事件流（tape，`runs/<run_id>.jsonl`）是唯一真相源。

---

## 命令总览

### `orca` —— in-session 驱动（TARS skill 调它，LLM 唯一可见）

| 命令 | 用途 |
|---|---|
| `orca list` | 列 workflow（扫 `./workflows` + `~/.orca/workflows`），返 `{name, description, inputs_schema}` |
| `orca <wf> --inputs '{...}'` | 启动（bootstrap → entry prompt + 驱动协议） |
| `orca next --run-id <id> --output '<产出>'` | 推进一步（主 session 把子代理产出回传） |
| `orca status [--run-id <id>]` | 查状态（无参→活跃 run 列表 `{runs:[{run_id,node,status,last_next_at,elapsed}]}`；有→详情） |
| `orca stop --run-id <id>` | 停 run |
| `orca open [--run-id <id>]` | 打开 Web 监控面板 |
| `orca doctor` | 自检（skill 落点 + CLI imports） |

### `tars` —— 后端 / 运维（operator）

| 命令 | 用途 |
|---|---|
| `tars install [--target cc\|opencode\|cac\|nga\|all] [--scope user\|project]` | 装 skill + nudge/plugin + 部署内置 workflow（全局默认） |
| `tars run <yaml> [task] [-i key=value]... [--max-iter N] [--background] [--port N] [--stay]` | headless 跑 workflow（默认起 Web 监控；`--background` 后台） |
| `tars validate <yaml>` | 只校验 schema + DAG，不跑 |
| `tars list` | 列 workflow（与 `orca list` 共享 catalog 单一实现） |
| `tars serve [--port 7428] [--host ...]` | 常驻 Web UI server |
| `tars open <run_id> [--tape <path>] [--port N]` | Web 打开已存在 run（attach tape，observe-only） |
| `tars mcp [--with-web]` | MCP server（stdio JSON-RPC，供 CC/opencode/Cursor 接入） |
| `tars ps` / `tars logs <id> [-f]` / `tars wait <id>` / `tars resume <id>` | 后台 run 管理（ps 列 / logs tail / wait 阻塞 / resume 崩溃续跑） |
| `tars executor show\|set\|unset\|list\|test` | 后端 CLI 配置（binary + flags + prompt 投递，唯一真相源） |

退出码：`tars run` completed→0 / failed→1 / 参数校验错→2。

---

## 跑一个 workflow（headless CLI）

### 纯 script（零后端、零 token）

```bash
tars run examples/demo_linear.yaml      # a→b→c 线性，默认起 Web 监控
```

### 含 agent 的真实 workflow

```bash
tars run examples/demo_mixed.yaml --background
# Started background run: demo_mixed-...  PID: ...
tars wait demo_mixed-...                # 阻塞到完成
tars logs demo_mixed-...                # 看日志
```

崩溃后 `tars resume <id>`：Tape 即 checkpoint，重放到崩溃点续跑。

### 后端命令配置（`tars executor`）

每个 backend 最终拼出的命令（binary + flags + prompt 投递）完整可见、任意可改——换平台不改源码：

```bash
tars executor show opencode                                        # 生效命令 + 每字段来源
tars executor set opencode --binary nga --flags "run --format json"  # 换后端
tars executor test opencode                                         # 真起子进程自检
```

优先级（per-profile per-field）：shell env > 项目 `.orca/config.json` > 用户 `~/.orca/config.json` > default。详见 [`docs/releases/2026-07-02-executor-config.md`](docs/releases/2026-07-02-executor-config.md)。

---

## workflow 写法

workflow 是 YAML：`entry` + `inputs` + `nodes`（每节点 `kind` + `prompt`/`command` + `routes`）+ `outputs`。节点间用 `{{ <node>.output }}` 接线。

### 节点 kind

| kind | 干什么 | 需后端？ |
|---|---|---|
| `script` | 跑 shell 命令，stdout 存 `output.stdout` | 否 |
| `set` | 设字面量/计算值，常作条件分支 + fan-in | 否 |
| `agent` | spawn 后端 CLI 跑 prompt，输出即节点输出 | 是（in-session 壳用宿主主 session） |
| `parallel` / `foreach` | 并行 / 数组分批（headless CLI/Web/MCP 壳） | 看 sub-node |
| `wait` | `duration`（`"30s"`/Jinja2）sleep，可被中断 | 否 |

### 节点级 feature

| feature | 用法 | 示例 |
|---|---|---|
| **Retry** | node 下 `retry:`（max_attempts / backoff / retry_on / jitter），transient 失败自动重试 | `examples/with_retry.yaml` |
| **Validator** | node 下 `validator:`（criteria + max_retries），LLM 二次校验 output 语义 | `examples/with_validator.yaml` |
| **ask_user** | agent 调 `ask_user(prompt, options)`，经内嵌 MCP 路由到 gate | `examples/with_ask_user.yaml` |
| **条件路由** | `routes:` 下 `when: "<jinja>"` | `examples/demo_conditional.yaml` |
| **工具白名单** | agent 节点 `tools:`（bash/read/write/edit/glob/grep/task/…） | `workflows/nas-agent-pipeline.yaml` |

### 用 AI 生成 workflow（create-workflow skill）

手写 YAML 门槛高。`tars install` 已装 `create-workflow` skill，在宿主里直接用自然语言提需求：

- **从零描述**：「我要一个调研 workflow：拆问题 → 两个 researcher 并行 → synthesizer 合并。」
- **转换既有素材**：「把 `xxx/` 下的 agent md / 别家 workflow 转成 Orca workflow。」

skill 归一化成 DAG → 写 YAML + agent md → 跑 `tars validate` 自修到 0 error → 报告路径。

### 示例

- `examples/`：`demo_linear` / `demo_foreach` / `demo_parallel` / `demo_conditional` / `demo_mixed` / `with_retry` / `with_validator` / `with_ask_user` / `nas.yaml` / `parallel_research` / `render_chart` …（参考/测试用，不自动装）。
- `workflows/`：**内置 workflow 源**（随包发，`tars install` 部署全局）——当前 `nas-agent-pipeline.yaml`。

---

## Web UI / MCP

```bash
tars serve                # 常驻 Web UI server（默认 0.0.0.0:7428，远程可达；本地限回环 --host 127.0.0.1）
tars open <run_id>        # Web 打开已存在 run（attach tape，observe-only）
tars mcp                  # stdio JSON-RPC，供 CC/opencode/Cursor 接入
tars mcp --with-web       # 同进程挂 Web UI，stdin EOF 后转 daemon
```

env `ORCA_WEB_HOST` / `ORCA_WEB_PORT` 同效；`ORCA_PUBLIC_HOST` 覆盖打印的 URL（容器/反代）。`tars run`（默认 web）跑完 + 无人看 N 秒后自动退（`--stay` 常驻，`ORCA_WEB_AUTOEXIT_SECONDS` 可调）。

Web UI（v2，单 run/页）：左 Agents rail / 中 `[会话 | 图表]` 页签（markdown + 工具折叠 + thinking + chart）/ 右 LogStream。tape 唯一真相源，前端纯渲染。`tars open` 把 web attach 到任意 run 的 tape（read-only，不抢写者 flock）。

> **监控 in-session run**（TARS/orca 驱动）：`orca open --run-id <id>`（或 `tars open <id>`）attach 该 run 的 tape。

---

## 测试

```bash
uv run pytest -q                     # 单元 + script demo（不含真后端 / 浏览器）
uv run pytest -q -m integration      # 真后端 + 浏览器 E2E（慢）
cd orca/iface/web/frontend && npm test   # 前端 vitest
```

in-session E2E 约定：opencode + deepseek-v4-flash（API 已配）；`orca` 装于 WSL conda，opencode 跨平台真机加载（cac/nga）留用户侧验证。
