# Orca

vendor-neutral、event-sourced、可视化的 coding-agent 编排控制平面——把 claude / codex / opencode 编进一个 DAG workflow，事件流（tape）是唯一真相源，支持人机决策门（gate）、mid-run 中断纠偏、时间旅行回放、CLI 与 Web 双入口。

> 设计决策见 [docs/TASK.md](docs/TASK.md)；各阶段契约见 [docs/specs/](docs/specs/)；phase 11（CLI feature 补全）见 [docs/releases/2026-07-02-phase11-complete.md](docs/releases/2026-07-02-phase11-complete.md)。

---

## 安装

```bash
uv sync                                                       # Python 依赖（Python ≥ 3.10）
cd orca/iface/web/frontend && npm install && npm run build && cd -   # 仅 Web UI 需要（一次性）
```

跑含 `kind: agent` 的 workflow 需要本机有 `claude` CLI 并配好 API（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`，兼容 ccr 等协议代理）。纯 `script` / `set` 的 workflow 零 token、秒级跑完。

---

## 快速开始

### 1) 零门槛：纯 script workflow（不需要 claude）

```bash
uv run orca run examples/demo_linear.yaml     # a → b → c 线性推进，Textual TUI 实时显示 DAG + 日志
```

### 2) 实测：含 agent 的真实 workflow

`examples/demo_mixed.yaml` 是一个 script → **agent** → set(条件) → script 的完整 DAG，验证 agent 编排 + 条件路由 + 输出接线：

```yaml
entry: prep
nodes:
  - name: prep
    kind: script
    command: "echo data"
    routes: [{to: analyzer}]
  - name: analyzer                       # ← agent 节点：spawn claude -p
    kind: agent
    prompt: "分析 {{ prep.output.stdout }}，回复 OK"
    routes: [{to: judge}]
  - name: judge
    kind: set
    values: {verdict: "pass"}
    routes:
      - {when: "output.verdict == 'pass'", to: reporter}
      - {to: prep}
  - name: reporter
    kind: script
    command: "echo final"
    routes: [{to: $end}]
outputs:
  result: "{{ reporter.output.stdout }}"
```

跑（TTY 环境直接 `orca run`；无 TTY / 想后台跑用 `--background`）：

```bash
uv run orca run examples/demo_mixed.yaml --background
# Started background run: demo_mixed-20260702-075144-848f0c   PID: 43779
uv run orca wait demo_mixed-20260702-075144-848f0c   # 阻塞到完成
uv run orca logs demo_mixed-20260702-075144-848f0c   # 看日志
```

**实测 tape 事件流**（`runs/<run_id>.jsonl`，事件流是唯一真相源）：

```
workflow_started
node_started  kind=script          (prep)
node_completed  stdout="data\n"
route_taken  -> analyzer
node_started  kind=agent           (analyzer ← 真 spawn claude -p)
prompt_rendered                    (prompt 末尾预览，guidance 注入的可观测证据)
agent_thinking / agent_message …   (848 个 token 级流式事件)
node_completed  out="OK.\n\n## `data` 分析\n… loader.py …"
route_taken  -> judge
node_started  kind=set             (judge)
node_completed  {verdict: "pass"}
route_taken  -> reporter           (条件分支命中)
node_started  kind=script          (reporter)
node_completed  stdout="final\n"
route_taken  -> $end
workflow_completed  outputs={"result": "final\n"}
```

agent 真的跑了起来（把 `data` 当真去项目里找了 `tests/e2e_mxint/target_project/data/loader.py` 并产出分析），条件路由 `verdict=='pass'` 命中 reporter，最终输出 `final`。

---

## CLI 参考

### 核心

```bash
uv run orca run    <yaml> [task] [-i key=value]... [--max-iter N] [--background]   # 跑 workflow
uv run orca validate <yaml>     # 只校验 schema + DAG，不跑
uv run orca list                # 列出可用 workflow
```

- 位置参数 `task` 是 `-i task="..."` 的语法糖；`-i key=value` 带类型推断（`true`/`false`/`null`/`[1,2]`/数字/字符串）。
- 退出码：completed→`0` / failed→`1` / 参数或校验错→`2`。
- `orca run` 默认进 Textual TUI（DAG 树 / 日志流 / 答 gate）。无 TTY 自动提示用 `--background`。

### 后端命令配置（`orca executor`）—— 唯一真相源

每个 backend 最终拼出的命令（binary + flags + prompt 投递方式）**完整可见、任意可改**——换平台不用改 profile 源码。`show` 是**唯一真相源**：打印生效 argv + 每字段来源（env / 项目 / 用户 / default）。

```bash
orca executor show [profile]          # 生效命令（唯一真相源）+ 每字段来源标注
orca executor set <profile> \         # 三维任组，写完回打生效命令
    [--binary <path>] [--flags "<s>"] [--prompt-channel <stdin|argv>] [--scope project|user]
orca executor unset <profile> [binary|flags|prompt_channel|all] [--scope ...]
orca executor list                    # 列可用 profile + 标 * 哪个被 override
orca executor test <profile>          # 真起子进程自检：✓ 端到端 OK / ✗ 给出原因（未装/协议不兼容/无 key）
```

换平台示例（本仓的 opencode 后端换成 `nga`，且它不带 `--dangerously-skip-permissions`）：

```bash
orca executor set opencode --binary nga --flags "run --format json"   # 默认 --scope project
orca executor show opencode
#   binary   nga              ← 项目  (default: opencode)
#   flags    run --format json  ← 项目  (default: run --format json --dangerously-skip-permissions)
# ▶ 生效命令（唯一真相源）:
#   nga run --format json "<prompt>" [--model <node.model>]
```

- **优先级**（per-profile per-field，多 fallback 生效只一份）：`shell env` > 项目 `.orca/config.json` > 用户 `~/.orca/config.json` > profile default。临时覆盖单次：`ORCA_OPENCODE_CLI=nga orca run ...`（显式 `export` 永远赢 config）。
- **两层 config**：项目级 `.orca/config.json`（跟仓库走，可 check-in 共享）per-field 覆盖用户级 `~/.orca/config.json`（个人默认）。`--scope project|user` 选写哪层（默认 project）。
- **可改范围**：spawn 参数（binary / flags / prompt_channel）。`flags` 规范存 list（`["run","--format","json"]`），`--flags` 输入串自动 shlex 拆 list。**协议参数**（translator / 终态契约 / 事件格式）改了会破坏解析，仍需新增 profile + translator（见 [`docs/releases/2026-07-02-executor-config.md`](docs/releases/2026-07-02-executor-config.md) + [`2026-07-07-executor-cli-extend.md`](docs/releases/2026-07-07-executor-cli-extend.md)）。
- `pip install` 后直接 `orca executor ...` 即可，无需 `uv run`。

### 后台 / 续跑（phase 11）

```bash
uv run orca run examples/long.yaml --background      # fork detached 子进程，立即返回 run_id + pid
uv run orca ps                                       # 列活跃 run（dead pid 标 crashed）
uv run orca logs <run_id> [-f]                        # 查 / tail 日志
uv run orca wait <run_id>                            # 阻塞到终态（exit 0 完成 / 1 失败 / 2 not-found）
uv run orca resume <run_id 或 tape 路径>              # 崩溃后续跑：Tape 即 checkpoint，重放到崩溃点继续
```

`--background` 的 detached 子进程走 headless（无 TUI，直接 `Orchestrator.run`），tape 写到标准 `runs/<run_id>.jsonl`，`resume` 可接。

### 运行中交互（TUI 内）

| 键 | 动作 |
|---|---|
| `q` | 退出 |
| `g` | 跳到 gate（人机决策门）|
| `Ctrl+G` | **中断 / 纠偏**：弹 InterruptModal，可选填 guidance，选 CONTINUE / SKIP / ABORT |
| `d` | **对话**：node 跑完后多轮追问 agent（重 spawn + 拼历史）|

- **Ctrl+G + CONTINUE + guidance**：杀当前 claude 子进程，同一 node 重 spawn，prompt 末尾拼 `[User Guidance]` 段。
- **Ctrl+G + SKIP**：弹 node 选择器，跳到任意下游 node（无兜底 route 时不会 NoRouteMatch 崩溃）。
- Ctrl+G 也会立即打断 `kind: wait` 节点的 sleep（`wait_completed.interrupted=true`）。

---

## phase 11 feature（CLI 补全）

| Feature | 节点 / 用法 | 示例 |
|---|---|---|
| **Retry Policy** | node 下加 `retry:`（max_attempts / backoff / retry_on / jitter），transient claude 失败自动重试 | `examples/with_retry.yaml` |
| **Semantic Validator** | node 下加 `validator:`（criteria + max_retries），LLM 二次校验 output 语义，失败带 issues 反馈重跑 | `examples/with_validator.yaml` |
| **ask_user 工具** | agent prompt 里调 `ask_user(prompt, options)` 问用户，自动经内嵌 MCP server 路由到 CLI AskGate | `examples/with_ask_user.yaml` |
| **Wait Node** | `kind: wait` + `duration`（支持 `"30s"`/`"5m"`/Jinja2），asyncio.sleep，可被 Ctrl+G 打断 | `examples/with_wait.yaml` |
| **Dialog** | node 跑完按 `d` 多轮对话 | `examples/with_dialog.yaml` |
| **Checkpoint Resume** | `orca resume <tape>` 续跑 | — |
| **daemon** | `--background` / `ps` / `logs` / `wait` | — |
| **Skip to Agent** | Ctrl+G → SKIP → node 选择器 | `examples/demo_skip.yaml` |
| **Interrupt + Guidance** | Ctrl+G | `examples/demo_interrupt.yaml` |

> Retry 的 `retry_on` 白名单（`spawn_error`/`timeout`/`api_error`/`http_429`）与 executor 实际产出的 `node_failed.error_type` 对齐；用户 Ctrl+G 触发的中断（`was_interrupted=true`）不重试。Validator 与 Retry 是独立预算（不共享 max_attempts）。详见 SPEC §9.5 / §9.6。

---

## create-workflow skill（让 AI 帮你写 workflow）

手写 Orca YAML 门槛高，Orca 自带一个 `create-workflow` skill，让你用自然语言描述需求、或给它一堆既有 agent md / 别家 workflow，它自动产出可跑的 Orca workflow（YAML + agent md），并强制跑 `orca validate` 自校验（0 error 才算完成）。同时兼容 **Claude Code** 和 **opencode**。

### 安装

```bash
orca skill install                       # 默认装两边：~/.claude/skills/ + ~/.config/opencode/skills/
orca skill install --target claude       # 只装 Claude Code
orca skill install --target opencode     # 只装 opencode
```

幂等（重跑覆盖更新，会先 `⚠` 提示）。opencode 全局目录可用 `OPENCODE_CONFIG_DIR` 覆盖。

### 使用

装好后，在 Claude Code 或 opencode 里直接用自然语言提需求，skill 会按描述自动触发：

- **从零描述**：「我要一个调研 workflow：拆问题→两个 researcher 并行→synthesizer 合并。生成一个 Orca workflow。」
- **转换既有素材**：把别家 workflow 定义 / 散 agent md / CC skill 放进一个文件夹，告诉它「把 `xxx/` 下的东西转成 Orca workflow」。
- **混合**：「用 `researcher.md` 跑调研，再写个 writer 出报告，串成 workflow。」

skill 会：① 归一化成 DAG → ② 写 YAML + 必要 agent md（落 `./workflows/` 或你指定的路径）→ ③ 跑 `orca validate` 自修到 0 error → ④ 画草 DAG 报告路径。生成后用 `orca run <yaml>` 跑。

> skill 内部规则（agent 三态自动选、fan-in 默认 `set`、文件夹 agent 脚本走 `$ORCA_AGENT_RESOURCES`、validator/retry 正交等）见随包 `SKILL.md` + `reference/orca-workflow-contract.md`。

### Benchmark（评测 skill 自身）

`orca/skills/create-workflow/benchmark/` 有 16 个 case（钉死输入 + 预期产物，全过 validate）。`scripts/run_skill_benchmark.py` 是公平 headless harness（opencode 后端真跑 skill、不泄露答案）：

```bash
python scripts/run_skill_benchmark.py                # 跑全部 16 case
python scripts/run_skill_benchmark.py 01 11          # 跑指定 case
```

---

## in-session shell（宿主主 session 执行 workflow）

前三壳（CLI/Web/MCP）都是 Orca 起子进程跑 workflow。**in-session shell 反过来**：宿主（opencode / Claude Code）的**主 session 用自带 subagent 执行每个节点**，Orca 只独占 tape + 确定性算下一步 + plugin/hook 自动推进。体验等价 CCW（在交互界面里逐节点跑），真相源仍是 Orca 单 tape。

**适用**：想让"你正在对话的主 session"亲自跑完整 workflow（保留主 session 上下文、用宿主原生 subagent），而非 fire-and-forget 给 Orca 子进程。

### 安装

in-session shell 随 `orca` 一起装（内置子命令组 `orca in-session ...`）。前置：装好 `orca`（见上方"安装"）+ 配好 opencode（`opencode` 二进制 + provider auth）。

### 使用（opencode，交互 TUI 或 serve）

一条命令把 plugin + slash 命令模板写进当前项目，然后在 opencode 里直接用：

```bash
# 1) 在项目里落 Orca plugin + /orca 命令模板（写到 .opencode/plugins/ + .opencode/command/）
orca in-session start my_workflow.yaml

# 2) 打开 opencode（交互 TUI 或 opencode serve），在会话里敲：
#    /orca doctor        ← 一键自检入口链路（plugin 加载 / marker 派发 / CLI 可达，能回报告即通）
#    /orca run           ← 跑起 workflow：注入 entry prompt → 主 session 派 task subagent 逐节点执行
#                           → session.idle 自动推进 → 直到 workflow_completed
#    /orca status        ← 看 run 进度
#    /orca stop          ← 停 run（清 marker + emit workflow_cancelled）
```

`/orca run` 流程：用户敲命令 → opencode 展开成 marker `<!--orca:cmd run ...-->` → Orca plugin 的 `experimental.chat.messages.transform` 钩子检测 marker → 调 `orca in-session bootstrap/next` CLI → 把用户消息改写成节点 entry prompt（**模型只见节点 prompt，不见 marker/原命令**）→ 模型用 task subagent 执行 → 每节点 turn 结束（`session.idle`）plugin 自动提取 subagent 输出 + 推进下一节点。

### 命令面

| `/orca <sub>` | CLI 子命令 | 用途 |
|---|---|---|
| `/orca run <wf>` | `bootstrap` + idle 驱动 `next` | 跑一个 workflow（marker → transform → bootstrap → entry prompt → idle 多节点驱动） |
| `/orca doctor` | `doctor` | **入口链路自检**（plugin 加载 / marker 派发 / CLI 可达）——能回报告即 `/orca` 入口活着 |
| `/orca status` | `status` | 看 run 进度（读 tape replay_state） |
| `/orca stop` | `stop` | 停 run（清 marker + emit cancelled） |

CC（Claude Code）路径：`orca in-session start` 生成 `.claude/settings.json` 的 Stop/PostToolUse hook 脚本（output cache 跨 hook 传递），走同一套 CLI 子命令。

### 设计与约束

- **CLI = 唯一大脑 + 唯一 tape 写者**；plugin / hook = 哑传输（只 spawn CLI + parse JSON + marker 派发，零 Orca 业务逻辑，CI grep 守门 + 签名契约测试锁死）。
- **单一接口**：所有 `/orca <sub>` 经同一 marker 派发；加 command = CLI 子命令 + plugin 派发分支两处。
- **per-call flock + `Tape.append_batch` 单次 write 原子化**；崩溃 `bootstrap`/`next` 以 `Tape(resume=True)` 半写恢复续跑。
- **hook 驱动**：模型不调任何 Orca 工具；opencode `session.idle`（含子 session 过滤）/ CC `Stop` hook 自动推进。
- v1 仅 agent 节点（parallel/foreach/gate fail loud 指引走 TUI/Web）；opencode 1.14.22 实测可用（入口 `experimental.chat.messages.transform`，`command.execute.before` 在该版本 runtime 不触发，故不用）。

详见 [设计草稿 v8](docs/specs/in-session-shell-design-draft.md) + [铁律 1 扩展 ADR v3](docs/specs/2026-07-07-in-session-iron-law-1-adr.md) + [v8.1 bugfix release](docs/releases/2026-07-08-in-session-shell-v8.1-bugfixes.md)。

---

## Web UI

```bash
uv run orca serve                # → http://127.0.0.1:7428
uv run orca serve --port 8000    # 自定义端口
```

左侧 run 列表 → 点 **+New** 填 yaml 路径启动 → 实时 DAG / 日志；gate 弹窗富交互作答；run 完成后点 **⏮ Replay** 时间旅行回放。多 run 真并发，事件按需懒加载。首次用需先构建前端（见安装）；hook 桥（claude 工具权限拦截）复用 serve 端口。

---

## Demo workflows（`examples/`）

| 文件 | 演示 | 节点 | 需 claude？ |
|---|---|---|---|
| `demo_linear.yaml` | 纯线性 a→b→c | script ×3 | 否（零 token）|
| `demo_loop.yaml` | 回环循环 + max_iter 终止 | set + script | 否 |
| `demo_foreach.yaml` | 数组分批并行 | set + foreach | 否 |
| `demo_parallel.yaml` | parallel 组并行汇聚 | script ×3 | 否 |
| `demo_failure.yaml` | 非零退出被记录（不 fail loud）| script | 否 |
| `demo_max_iter.yaml` | 循环不终止 → workflow_failed | set | 否 |
| **`demo_mixed.yaml`** | **综合（script + agent + set 条件分支）** | 混合 | **是（实测通过）** |
| `demo_conditional.yaml` | 条件分支 | set + agent ×2 | 是 |
| `demo_task.yaml` | task 位置参数注入 | agent | 是 |
| `demo_interrupt.yaml` / `demo_skip.yaml` | Ctrl+G 中断 / SKIP 跳转 | agent | 是 |
| `with_retry.yaml` / `with_validator.yaml` | Retry / Validator | agent | 是 |
| `with_ask_user.yaml` / `with_wait.yaml` / `with_dialog.yaml` | ask_user / wait / dialog | agent | 是 |
| `nas.yaml` / `batch_assess.yaml` / `parallel_research.yaml` / `mxint_analysis.yaml` | 真实 workflow | 混合 | 是 |

script / set 驱动的 demo 不需要 claude 或 API key，秒级跑完，适合先体验编排。

---

## 测试

```bash
uv run pytest -q                              # 单元 + script demo（不含真 claude / 浏览器）
uv run pytest -q -m integration               # 真 claude + 浏览器 E2E（慢，需 claude CLI）
cd orca/iface/web/frontend && npm test        # 前端 vitest
```

CI（`.github/workflows/test.yml`）每次 push / PR 自动跑 `pytest -m "not integration"`（matrix Python 3.10/3.11/3.12）；真 claude E2E 走 `integration.yml`，在 PR 评论 `/integration` 触发。
