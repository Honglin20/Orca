# In-Session 入口成型与简化 Spec（v5 —— B 路径 + 单一接口 + skill 载体）

> **状态**：Draft v5（2026-07-14），闭环 spec-review v3 step 2-6 + 用户决策。step 1 已实施（commit `d14cde5`），本 spec 剩余 step 2b-6 待实施。
> **v5 相对 v4 变化**：**删 `describe` 命令**（用户决策：冗余）→ `orca list` 直接返 `{name, description, inputs_schema}`（一个命令给 skill 选 wf + 抽 inputs 的全部信息）。命令数回到 **7**。其余同 v4（A2 拆 setup vs gate / A1 MCP migration / B1 禁用 transform / B5 删 orca.ts / step 3-5 拆分）。**A5 修正：nudge 不再 defer merge，改 step 2b 做（idle/Stop hook 提醒，精确判定卡住，不推进）**。
> **排除（推迟）**：决策核心合并 → [`in-session-unified-backend-draft.md`](in-session-unified-backend-draft.md)。

---

## 0. 目标

in-session = 路径 B（主 session 驱动），跨 CC/opencode/NGA/CAC 统一一套 orca 接口；前端只装 skill（删 command）；setup 全删；命令行后端全归 teams（命令名变量化）；旧接口清零。**唯一真相源 + 单一接口 + 架构整洁**。

---

## 1. 执行模型：路径 B（主 session 驱动，已定）

主 session 派子代理 → 自调 `orca next --output` → 读返回 → 再派。Orca CLI 被动 per-call，不靠 hook 推进。主 session 可在调 next 前修正 output（B 天然支持）。

**B 固有代价 + 缓解**：押 LLM（deepseek/CC 已验证够；弱模型用 teams）；静默卡住靠 `status` 显示 `last_next_at`+elapsed（兜底可见）+ **nudge hook（step 2b 做，§4.4）**：idle/Stop 时有活跃 run → 提醒主 session 调 next（精确判定，不推进）。

---

## 2. orca 单一接口（in-session，LLM 唯一可见，7 命令）

> 四前端（CC/opencode/NGA/CAC）**都调这一套**，skill 只声明这一套。

### 2.1 命令族（7 个）

| 命令 | 作用 | 谁调 |
|---|---|---|
| `orca list` | 列 wf，返 `{workflows:[{name, description, inputs_schema}]}`（inputs_schema = wf.inputs 的 name+type+description，skill 选 wf + 抽 inputs 用，**不读 YAML**） | skill/LLM |
| `orca <wf> --inputs '{...}'` | 启动（bootstrap → entry prompt + 驱动协议） | 主 session |
| `orca next --run-id <id> --output '<产出>'` | 推进一步 | 主 session |
| `orca status [--run-id <id>]` | 查状态（无 run_id → 列全部活跃 run；有 → 详情） | 主 session/用户 |
| `orca stop --run-id <id>` | 停 run | 主 session/用户 |
| `orca open [--run-id <id>]` | 打开 web 监控面板 | 用户 |
| `orca doctor` | 自检（skill 落点 + CLI imports；hook 心跳可选） | 用户 |

- `<wf>` 是 hidden `bootstrap` 的 rewrite 语法糖（**单一入口，非两套**）。
- **catalog 扫描**：`./workflows` + `~/.orca/workflows`，**不扫 `examples/`**（用户决策；demo 测试时复制到 `workflows/`）。
- **无 `describe` 命令**（用户决策：冗余）—— inputs_schema 合进 `orca list` 返回值。

### 2.2 保留字黑名单（MS1）

wf name 禁取 `list/next/status/stop/open/doctor` + teams 命令名（`run/serve/ps/...`）→ compile fail loud。

### 2.3 返回契约

| 命令 | 返回 |
|---|---|
| `orca <wf>` | `{run_id, prompt, prompt_file, done:false}` |
| `orca next` | `{done:bool, prompt?, prompt_file?, reason?}` |
| `orca status` | `{runs:[{run_id, node, status, last_next_at, elapsed}]}` 或单 run 详情 |
| `orca list` | `{workflows:[{name, description, inputs_schema:[{name,type,description}]}]}`（**无 has_setup**，B3） |
| `orca stop` | `{run_id, ok, done}` |

### 2.4 可见性守门

`orca --help`/错误/skill 禁出现 teams 命令名；测试断言 `orca --help` 含 7 命令、不含 teams。

---

## 3. teams（命令行 / 后端，命令名变量化）

### 3.1 命令族

`teams run/serve/open/ps/install/validate/mcp/executor/list/logs/wait/resume`（后端 + 运维 + 工具；实施时核实实际数对齐标题）。`install` 单 binary 归 teams；`list` 与 `orca list` 共享 catalog 单一实现。

### 3.2 命令名变量化

env `ORCA_BACKEND_CMD`（默认 `teams`），安装/打包按变量生成命令脚本，改 env 重装即换名。

---

## 4. 前端集成层（skill 唯一，删 command）

### 4.1 删 command，统一 skill

删 `templates/opencode/command/orca/*.md`（4 文件）；新增 `orca` skill（一套 SKILL.md），skill 内联注入主 session。

### 4.2 skill 流程（不读 YAML，三步）

SKILL.md 教主 session：
1. `orca list` → 拿 wf 列表（**含 description + inputs_schema**）→ LLM 据 description 选 wf。
2. 据 inputs_schema 从用户意图抽 inputs。
3. `orca <wf> --inputs '{...}'` → 读驱动协议 → 派 Task 子代理 → 自调 `orca next --run-id --output` 循环到 done。

**skill 绝不读 YAML**（选 wf + 知 inputs 都经 `orca list`，单一接口，一个命令搞定）。

### 4.3 各平台 skill 落点（仅此不同）

| 前端 | 后端 | skill 落点 |
|---|---|---|
| claude code | CAC | `.claude/skills/` |
| opencode | NGA | `.opencode/skills/` |
| CAC | — | `.cac/skills/` |
| NGA | — | 同 opencode |

`teams install --target cc|opencode|cac|nga` 装同一份 SKILL.md 到对应目录。

### 4.4 hook（B 不推进；nudge 提醒 step 2b 做，A5 修正）

- **推进 hook 不用**（主 session 自调 next）。`cc_hooks.py` A 推进（Stop 调 next）退场删。
- **nudge hook（A5，step 2b 做）**——node 完成 → 主 session 空闲 → 有活跃 run → 提醒调 next（**只提醒，不推进**）：
  - **opencode**：`orca.ts` 的 `session.idle` event hook → 检查活跃 marker → `promptAsync` 注入「请调 `orca next --run-id <id>` 推进」（不调 next）。
  - **CC**：settings.json 的 `Stop` hook（`teams install` 生成 nudge 片段）→ 检查活跃 marker → `decision:block` 注入「还有 run `<id>`，请调 `orca next`」（不调 next）。
  - **精确判定**：idle/Stop event 本身区分「子代理在工作」（主 session 在等，**不触发**）vs「卡住」（主 session 空闲没调 next，**触发**）。**不靠 tape 超时**（tape 看不到子代理状态，超时判定会误报）。用宿主事件精确判定。
  - **不推进**：nudge 只提醒，`next` 仍主 session 自调（B 路径不变；hook 自动调 next = 退化 A）。
- **诊断 hook**（doctor 心跳）可选。

### 4.5 skill 守门

SKILL.md **必须含三步指导**（§4.2）+ **禁业务逻辑关键词**（`advance_step/Orchestrator/router.resolve/Tape/replay/compile/load_workflow`）。CI grep 守门。

---

## 5. 输出契约（统一 `--output` + quoting）

主 session 把子代理产出作 `--output` 字符串传 next。**quoting 规约**（M7）：产出含单引号用 `'\''` 转义，drive protocol 教模型 + 单测。**fail loud**：空 output → 合规计数（N=3，有效 output 清零）→ `workflow_failed(subagent_compliance)`；畸形 → `output_schema_mismatch`/`render_error`。

---

## 6. 删 setup phase（全栈）

### 6.1 删除范围（A2：拆 setup vs execute-phase gate）

**删**（setup 相关）：`schema/workflow.py` Workflow.setup / `compile/validator.py` `_check_setup_phase_constraints`(775+) + `_check_jinja2_refs` setup valid_root(632) / `compile/parser.py`（可选 pre-scan）/ `iface/mcp/server.py` 删 `tool_get_agent_prompt` + `tool_start_workflow` 删 `setup_outputs` / `iface/mcp/setup_phase.py` 整模块删 / `iface/mcp/agent_catalog.py`+`hints.py`(setup)+`catalog.py`(has_setup+setup 段+`_estimate_runtime`) / `iface/web/run_manager.py`(setup_outputs 透传)+`iface/cli/commands.py`(teams run setup 透传) / `exec/context.py` RunContext.setup + `exec/render.py` setup namespace / `run/orchestrator.py` setup_ns 注入(156-170,852-853)。

**保留（与 setup 正交，A2 不能误删）**：`compile/validator.py` `_check_execute_phase_no_gate_tools` + `_INTERRUPT_TOOL_NAMES`(728-769)（execute phase 禁 gate 工具校验，保留）；`exec/mcp_tools/`（grep 0 setup 命中）。

### 6.2 MCP breaking change（A1）

**MCP 工具表 = `iface/mcp/server.py` 注册（代码即契约）**。删 `tool_get_agent_prompt` + 从 `tool_start_workflow` 删 `setup_outputs` 参数。**migration note**：旧客户端不调 `get_agent_prompt`，`start_workflow` 去 `setup_outputs`。**m13**：setup YAML 段靠 pydantic `extra=forbid` fail loud；可选 parser pre-scan friendly error（kind=`unsupported_setup_phase`）。

---

## 7. 旧接口清理（无多套并存，清零）

### 7.1 命令归宿

`orca run→teams run` 等直接断（同 commit 改调用点）。

### 7.2 marker 精简

`ActivationMarker` 只 `{run_id, model, no_output_count}`；文件名 `orca-<run_id>.json`，`next` 用 `marker_path(rundir, run_id)` O(1)。yaml 从 tape 派生（唯一真相源）。`.orca-bootstrap.lock` 残留无害。

### 7.3 重复 bootstrap fail loud

按 **wf.name**（非 yaml realpath；wf.name 经 compile 保唯一）匹配活跃 run → fail loud。

### 7.4 command 模板删除

`templates/opencode/command/orca/{doctor,run,status,stop}.md` 全删。

### 7.5 其他清理

- **删 `orca.ts` 的 transform 入口 + 死代码**（B5/B9），**保留 idle nudge hook**（§4.4，opencode nudge 载体，step 2b 已改提醒模式）；同 commit 删 `_constants.py` Py 侧 `MARKER_REGEX`/`MARKER_LITERAL`（transform 删后无用）+ `test_marker_regex`/`test_plugin_embeds_canonical_marker_regex`（B6）。
- `cc_hooks.py`（A 推进死代码，删；CC nudge 用新 Stop hook 片段，§4.4）。
- `daemon.py` 逐条 emit → batch emit。
- 错误信封×3 → 复用 `InSessionError.error_kind`，归 phase-11。

### 7.6 守门 grep

CI 禁：`orca in-session` / `MARKER_REGEX` / `cc_hooks` 推进 / `extractTaskOutput` / `--output-file` / `describe`（命令）/ SKILL.md 业务逻辑关键词 / `setup`（schema + setup_phase 模块）。

---

## 8. 落地顺序

```
1. ✅ DONE（step 1, d14cde5）：orca 接口打包 + teams 变量化 + marker 精简 + dupe-check + 保留字 + B1 命令改名 + inputs 噪声

2b. 入口切 skill（下一个实施）：
   (1) 建 orca skill（一套 SKILL.md，三步指导）+ teams install --target cc/opencode/cac/nga 四平台落点代码
   (2) orca list 返 inputs_schema（name+type+description）+ 删 has_setup（B3）【无 describe 命令】
   (3) doctor 加 skill_install 检查（A6）+ hook 心跳改 optional
   (4) 【B1】禁用 orca.ts transform marker dispatch（early return + 注释指 step 4 整删）
   (5) 删 templates/opencode/command/orca/*.md（4 文件）
   (6) 删 start 命令 + cc_hooks.py（A 推进死代码）
   (7) 【nudge A5，§4.4】opencode idle hook（orca.ts）改「提醒」模式 + CC 新 Stop hook（teams install 生成 nudge 片段）：有活跃 run → 提醒调 next（promptAsync/decision:block），**不自动推进**

3a. inputs 代填 skill 完善（list 已返 inputs_schema，skill 三步定型）
3b. catalog 物理迁 orca/compile/catalog.py —— 延后到 step 5 setup 删之后（B7）

4. opencode 收尾：删 orca.ts 的 transform 入口 + 死代码（**保留 idle nudge hook**，§4.4）+ _constants.py（MARKER_REGEX）+ 相关测试（B5/B6/B9）

5a. 删 setup 全栈（§6.1 清单）+ MCP migration note（§6.2）
5b. daemon batch emit + 错误信封统一（独立 commit，C3）

6. teams install --target nga/cac 代码实施（B10）+ 真机验证留用户侧
```

**Plan B**：CC 路 B spike 已过（2026-07-14）；回归失败 → 暂停 step 2b 的 start/cc_hooks 删。

---

## 9. 待定 / 风险

| # | 项 |
|---|---|
| 1 | NGA/CAC skill 加载真机（留用户侧） |
| 2 | ~~nudge hook~~ 已改 **step 2b 做**（§4.4，A5 修正——不再 defer merge） |
| 3 | 命令分家后两套决策核心共存（根治 merge spec） |
| 4 | 主 session 全链路 E2E 部署（orca 装 WSL / opencode Windows；需 orca 装 Windows 或 opencode 装 WSL，非代码） |

---

## 10. 决策清单（v5 冻结）

1. 执行模型 = B（主 session 驱动）；A/hook 推进退场。四前端统一 B。
2. orca 单一接口 **7 命令**（`list/<wf>/next/status/stop/open/doctor`）；删 `in-session` 子命令层 + `start` + **无 describe**。
3. 命令迁移直接断（同 commit 改调用点）。
4. 删 command，统一 skill（SKILL.md 三步：list→抽 inputs→<wf>，不读 YAML）。
5. `orca list` 返 `{name, description, inputs_schema}`（合并 describe，单一命令）。
6. setup 全删（§6.1，execute-phase gate 校验保留 A2）+ MCP migration。
7. teams 命令名变量化（env ORCA_BACKEND_CMD）。
8. catalog 只扫 workflows/（不扫 examples/，demo 测试复制 workflows/）。
9. 前端集成 = skill（一套 SKILL.md，四平台落点）；hook = nudge 提醒（idle/Stop，不推进，§4.4，step 2b 做）+ 诊断可选。
10. 输出统一 `--output` 字符串 + quoting 转义。
11. marker 只 `{run_id, model, no_output_count}`；重复 bootstrap fail loud。
12. 删整个 orca.ts plugin + _constants.py + 相关测试。
13. in-session parallel / 动态构建本期不做。

---

## 11. 验收标准

- `orca --help` 含 7 命令、不含 teams 命令名、**不含 describe**。
- `orca list` 返 `{workflows:[{name, description, inputs_schema}]}`（**无 has_setup**）。
- execute phase agent 配 ask_user → compile 仍 fail loud（A2 gate 校验保留）。
- `orca doctor` 报 `skill_install=pass`（A6）。
- `orca.ts` **transform 入口 + 死代码删除**（保留 idle nudge hook）+ grep `MARKER_REGEX` 全仓 = 0（B5/B6，step 4 后）。
- nudge：opencode idle / CC Stop 有活跃 run 时提醒调 next（不推进，§4.4，step 2b 后）。
- `teams install --target nga/cac` 生成对应目录 SKILL.md（B10）。
- opencode 主 session **调 skill** 完成 3 节点 wf（demo 复制 workflows/）→ workflow_completed。
- CC 主 session 调 skill 完成 wf（复现）。
- 重复 `orca <wf>` 同 wf → fail loud。
- marker 只 `{run_id, model, no_output_count}`。
- grep `describe`（命令）= 0；grep `setup`（schema + setup_phase）= 0（step 5 后）。
