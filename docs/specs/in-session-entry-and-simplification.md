# In-Session 入口成型与简化 Spec（v3 —— B 路径统一 + 单一接口）

> **状态**：Draft v3（2026-07-14），闭环 spec-review v2 的 4 BLOCKER + 用户决策，待实施。
> **v3 相对 v2 的变化**：① orca 接口**一套统一**（CC/opencode/NGA/CAC 都调这同一套，skill 只声明一套）；② **删 command，统一切 skill**；③ **setup 全删**（含 teams/Orchestrator）；④ **teams 命令名变量化**（env，方便后期切换）；⑤ orca 接口加 `list`/`open`；⑥ 闭环 B1（命令改名同 commit 改 3 处活调用点）/ B3（catalog 归 skill）/ MS1（保留字黑名单）/ MS2（start 删除时序）+ M4（14 命令归宿）/ M7（quoting）/ m11（marker 精简）/ m12（重复 bootstrap fail loud）/ MS6（serve）/ M5（install）。
> **排除（推迟）**：决策核心合并 → [`in-session-unified-backend-draft.md`](in-session-unified-backend-draft.md)。
> **前置**：model-driven 补丁（已合并）+ spike 实证（next CLI 3 节点跑通）+ **CC 路 B spike 通过**（主 session 自调 next 实证，2026-07-14）。

---

## 0. 背景与目标

### 0.1 现状裂缝

- 两套推进机制（opencode B / CC A），A 退化 + 入口笨重 → 统一 B。
- `orca in-session` 子命令层多余 + `orca run` 后端混在 orca → 接口碎片化。
- 前端（CC/opencode/NGA/CAC）集成机制各异，多套集成代码并存（transform/marker/cc_hooks/command）。
- setup phase 过度设计（in-session path 零消费，teams/MCP 范畴，全删）。
- 入口机制四代残留 + 死代码。

### 0.2 目标（一句话）

**in-session = 路径 B（主 session 驱动），跨 CC/opencode/NGA/CAC 统一一套 orca 接口；前端只装 skill（删 command）；setup 全删；命令行后端全归 teams（命令名变量化）；旧接口清零。**

### 0.3 非目标

- 决策核心合并（merge spec，推迟）。
- in-session parallel/foreach（依赖合并）。
- 动态构建 workflow。

---

## 1. 执行模型：路径 B（主 session 驱动）

主 session 是 loop 主导者：派子代理 → **自调 `next`** → 读返回 → 再派。Orca CLI 被动 per-call，**不靠 hook 推进**。区别于 teams（后端 Orchestrator 自动循环、headless）的本质：主 session 在 loop 里、透明、可干预。

### 1.1 工作流（demo: writer→reviewer→summarizer）

```
① orca <wf> --inputs        → emit workflow_started+node_started(writer) → 返 {run_id, prompt, prompt_file}
② 主 session 派 Task → 子代理 Read writer.md → 返产出
③ orca next --run-id --output '<产出>'  → emit nc+rt+ns(reviewer) → 返 {done:false, prompt=reviewer}
④ 循环 ②③ → reviewer → summarizer
⑤ next 返 {done:true} → emit workflow_completed → 主 session 停
```

### 1.2 驱动协议（`_drive_protocol`，附每个 prompt 末尾）

① 派 task 子代理（子代理 Read 节点 .md，主 session 不许自己 Read）② 子代理返回后调 `orca next --run-id <id> --output '<产出>'`（单引号转义见 §5.2）③ 读 JSON：done=true 停，否则 prompt 是下一节点，回①。

### 1.3 关键特征

- 主 session 主导 + loop 透明；**可修正 output**（调 next 前把修正版塞 --output，B 天然支持）。
- Orca CLI 被动 per-call，不自动循环，不靠 hook 推进。
- 跨平台统一（CC/opencode/NGA/CAC 主 session 读同一驱动协议、调同一套 orca 接口）。

### 1.4 B 固有代价 + 缓解

- 押 LLM 自调可靠性（deepseek/CC 已验证够；弱模型 → 用 teams）。
- 静默卡住（LLM 不调 next）：`status` 显示 `last_next_at`+elapsed（卡住可见）+ **nudge hook 强烈推荐默认开**（MS4，只提醒不推进）。

---

## 2. orca 单一接口（in-session，LLM 唯一可见，一套统一）

> **铁律**：CC/opencode/NGA/CAC 四个前端**都调这一套**，skill 只声明这一套。绝不搞多套接口。

### 2.1 命令族（7 个）

| 命令 | 作用 | 谁调 |
|---|---|---|
| `orca list` | 列 workflow（catalog，name+description） | skill/LLM 选 wf |
| `orca <wf> --inputs '{...}'` | 启动（bootstrap → entry prompt + 驱动协议） | 主 session |
| `orca next --run-id <id> --output '<产出>'` | 推进一步 | 主 session 逐步调 |
| `orca status [--run-id <id>]` | 查状态（无 run_id → 列全部活跃 run 摘要；有 → 详情含 last_next_at+elapsed） | 主 session/用户 |
| `orca stop --run-id <id>` | 停 run | 主 session/用户 |
| `orca open [--run-id <id>]` | 打开 web 监控面板（默认当前活跃 run，复用 web attach） | 用户 |
| `orca doctor` | 自检（skill 落点 + CLI imports；hook 可选心跳） | 用户 |

- **删 `orca in-session` 子命令层**：bootstrap→`orca <wf>`、next/status/stop/doctor 上移顶层；新增 `list`/`open`。
- **`start` 删除**（B 路径无 per-run hook）——时序见 §8（spike 通过后才删）。
- catalog 匹配 + inputs 代填**不在 `orca <wf>`**（B3 闭环）——归 skill（§4.2），`orca <wf>` 只接 wf 名 + `--inputs` JSON。

### 2.2 保留字黑名单（MS1 闭环）

`orca <wf>` 用 wf 名作裸顶层子命令，与固定命令冲突。**compile 期校验**：wf name 禁取 `list/next/status/stop/open/doctor/install/run/serve/ps/mcp/executor/validate/<teams 变量名>` → compile fail loud。保 `orca <wf>` 语法糖。

### 2.3 返回契约（M8 闭环）

| 命令 | 返回 JSON |
|---|---|
| `orca <wf>` | `{run_id, prompt, prompt_file, done:false}` |
| `orca next` | `{done:bool, prompt?, prompt_file?, reason?}` |
| `orca status` | `{runs:[{run_id, node, status, last_next_at, elapsed}]}` 或单 run 详情 |
| `orca stop` | `{run_id, ok, done}` |
| `orca list` | `{workflows:[{name, description, has_setup(false 后)}]}` |

### 2.4 可见性守门

CI grep + 测试：`orca --help`/错误/skill 文本禁出现 teams 命令名；测试断言 `orca --help` 输出含 7 命令、不含 teams。

---

## 3. teams（命令行 / 后端，命令名变量化）

### 3.1 命令族（14 命令归宿，M4/MS6 闭环）

所有命令行/headless 后端命令归 teams：

| teams 命令 | 来源（旧 orca） | 说明 |
|---|---|---|
| `teams run <wf>` | orca run | headless 后端执行（默认起 web 监控） |
| `teams serve` | orca serve + in-session serve | web 服务（in-session serve 合并，MS6） |
| `teams open/ps/logs/wait/resume` | 同名 | 运维/监控 |
| `teams install` | orca install | 装 skill 到前端（operator 动作，M5 单 binary） |
| `teams validate/mcp/executor` | 同名 | 开发工具 |
| `teams list` | orca list（命令行侧） | 命令行查 wf（与 orca list 共享 catalog） |

- `install` **单 binary 归 teams**（M5）：operator 动作，`orca`（LLM namespace）不暴露 install。
- `list` catalog 共享：`orca list`（skill 用）+ `teams list`（命令行用）同源 `orca/compile/catalog.py`。

### 3.2 命令名变量化（用户要求）

`teams` 不硬编码：env `ORCA_BACKEND_CMD`（默认 `teams`）控制命令名，安装时（`teams install`/打包）按该变量生成命令脚本/entry-point。后期想改名（如 `conductor`/`orca-backend`）→ 改 env 重装即可，零代码改动。

### 3.3 setup 清理（teams 也删）

teams/Orchestrator 的 setup 消费一并删（§6 全栈删 setup）。

---

## 4. 前端集成层（skill 唯一，删 command）

### 4.1 删 command，统一 skill（用户决策）

- **删 opencode command 模板**（`command/orca/*.md`）——入口统一切 skill。
- 新增 `orca` skill（一套 SKILL.md），集中调度规则。
- skill 内联注入主 session（不丢上下文、单层子代理）+ 平台无关文本。

### 4.2 catalog/inputs 归 skill，不读 YAML（B3 闭环）

- skill **不直接读 YAML**（不泄漏 catalog 业务逻辑到前端，守 §4.5 铁律）。
- skill 教 LLM：调 `orca list` 拿 wf 列表 → LLM 据 description 判断选哪个 → 从用户意图抽 inputs → 调 `orca <wf> --inputs '{...}'`。
- `catalog.list_workflows()` 留 `orca/compile/catalog.py`（core 共享），CLI 暴露 `orca list`/`teams list`。

### 4.3 各平台 skill 落点（仅此不同）

| 前端 | 后端 | skill 落点 |
|---|---|---|
| claude code | CAC | `.claude/skills/` |
| opencode | NGA | `.opencode/skills/` |
| CAC | — | `.cac/skills/`（同 CC 机制） |
| NGA | — | 同 opencode |

`teams install --target <cc|opencode|cac|nga>` 把同一份 SKILL.md 装到对应目录。**SKILL.md 内容四平台一致**，差异只在落点。

### 4.4 hook 退居诊断/nudge（B 不推进）

- **推进**：不用 hook（主 session 自调 next）。`cc_hooks.py` 的 Stop/PostToolUse 推进（A 路径）退场删除。
- **nudge**（D3 缓解，MS4 提为推荐默认开）：检测主 session 超时没调 next → 提醒调 next。**只提醒，绝不自动推进**。
- **诊断**（可选）：doctor 心跳验集成层生效（m10：无 hook 时验 skill 落点 + CLI imports）。

### 4.5 skill 业务逻辑守门（M9 闭环）

CI grep：SKILL.md 禁 `advance_step/Orchestrator/router.resolve/Tape/replay/catalog.list/compile/load_workflow` 等业务逻辑关键词。skill 只教调 orca 接口，不含 Orca 业务逻辑。

---

## 5. 输出契约（统一 `--output` + quoting）

### 5.1 统一字符串

主 session 把子代理产出**直接作 `--output` 字符串**传 next。不搞文件/hook 导出（BL-1 否决）。

### 5.2 quoting 规约（M7 闭环 —— 不是大产出问题，单个撇号就破）

`--output '<产出>'` 单引号包裹，产出含撇号（`it's`/影评）即破，且 fail loud 覆盖不到（shell 语法错，CLI 收不到）。**drive protocol 加硬规则 + 单测**：产出含单引号时用 `'\''` 转义（`it's` → `it'\''s`）。教模型在调 next 前转义。

### 5.3 fail loud（MS7 写明）

- 空 output（子代理无产出）→ normalize None → 合规计数 `no_output_count` +1；有效 output → 清零。**连续 3 次**（N=3，cli.py:119）→ emit `workflow_failed(subagent_compliance)`。
- output 畸形（schema mismatch/render error）→ 干净 fail loud + 清 marker。

---

## 6. 删 setup phase（全栈，含 teams）

### 6.1 删除范围

setup（`workflow.setup`）全栈删：schema `Workflow.setup` + compile validator「execute phase 不配 ask_user/gate」+ MCP `start_workflow(setup_required)`/`get_agent_prompt`/`setup_outputs` 注入 + RunContext `setup_outputs` + catalog `has_setup` + **Orchestrator setup 消费（teams）** + 三重杠杆防跳过。in-session path（advance_step）零 setup 引用（code 核实），teams/MCP 一并清。

### 6.2 MCP breaking change（MS3 闭环）

setup 是 phase-10 已发布 MCP 契约。**标 breaking change**：MCP 客户端迁移说明（`start_workflow` 去掉 `setup_required`/`setup_outputs`、删 `get_agent_prompt` 工具）+ cross-ref phase-10 SPEC。旧 wf 含 `setup:` 段 → compile fail loud（error kind + message，m13）。

---

## 7. 旧接口清理（无多套并存，清零）

### 7.1 命令归宿（14 命令，M4 闭环）

见 §3.1（teams）+ §2.1（orca）。`orca run`→`teams run` 等**直接断**（不加兼容期，同 commit 改调用点，B1/D3）。

### 7.2 marker 精简（m11 闭环 —— 删 desync 向量）

- 删 `session_id` + `owner`（B 下 owner=run_id 恒定，冗余）+ `tape_path` + `yaml`（run_id 可派生，留着是 desync 向量——tape 被移→marker 悬空）。
- **只留 `{run_id, model, no_output_count}`**。tape_path/yaml 运行时从 run_id + config 派生。
- 文件名固定 `orca-<run_id>.json`，`next` 改 `marker_path(rundir, run_id)` O(1) 定位（删 `find_marker_by_run_id` 扫描）。
- **并发安全**：run_id 唯一 → 每 run 独立 marker+tape，多 session / 一 session 多 wf 天然隔离。

### 7.3 重复 bootstrap fail loud（m12 闭环）

同 wf（yaml realpath）已有活跃 marker（未终态）时再次 `orca <wf>` → **fail loud**：「已有 run `<id>`，用 `orca next --run-id <id>` 续跑，或 `orca stop --run-id <id>` 后再建」。不静默新建孤儿 run。复用 N1 幂等键（key=yaml realpath）。

### 7.4 command 模板删除

`templates/opencode/command/orca/*.md` 全删（统一切 skill，§4.1）。时序：skill 建好 + 切换后删（§8）。

### 7.5 其他清理

- `cc_hooks.py` A 推进逻辑（Stop/PostToolUse 调 next）退场。
- `orca.ts` 死代码（extractTaskOutput/serverBaseUrl/injecting/promptAsync）+ transform/marker/buildCliArgs（skill 切换后删，BL-2 时序）。
- `_constants.MARKER_REGEX`（Py+TS 双写）随 marker 弱化删。
- `daemon.py` 逐条 emit → batch emit（B-8）。
- 错误信封×3 → 复用 `InSessionError.error_kind`，统一归 phase-11。
- `in-session serve` → 合并 `teams serve`（MS6）。
- `_inputs_from_tape` 首调噪声修复（tape 无 workflow_started 时静默，不 WARNING）。

### 7.6 守门 grep

CI 禁旧符号：`orca in-session` / `MARKER_REGEX` / `cc_hooks` 推进 / `extractTaskOutput` / `--output-file` / SKILL.md 业务逻辑关键词 / `setup`（schema 字段）。

---

## 8. 落地顺序（B1/MS2 时序闭环）

```
1. orca 接口打包 + 14 命令归宿 + teams 变量化
   - orca 7 命令定型；删 in-session 子命令层
   - 【B1】同 commit 改 3 处活调用点：cli.py:127 驱动协议 / orca.ts:122 spawn / (run.md 此时随 command 一起处理)
   - teams 命令名变量化（env ORCA_BACKEND_CMD）
   - marker 精简（§7.2）+ 重复 bootstrap fail loud（§7.3）
   - start 标 deprecated（warn），不删（MS2）

2a. CC 路 B spike 验证（gate，已通过 2026-07-14）→ 若回归失败见 Plan B

2b. spike 通过 → 删 cc_hooks A + 删 start + 建 orca skill 入口 + 删 command 模板

3. skill 完善（catalog via orca list + inputs 代填）+ catalog 下沉 orca/compile/

4. opencode 收尾：删 orca.ts 死代码 + transform + MARKER_REGEX（skill 切换后）

5. 删 setup（全栈 §6）+ daemon batch emit + 错误信封 + _inputs_from_tape 噪声修复

6. NGA/CAC 适配（skill 落点真机验证，留用户侧）
```

**Plan B（B2 闭环）**：CC 路 B spike 若回归失败 → 暂停 step 2b 的 start/cc_hooks 删除 → CC 临时保留 A 或 doc-only，直至 spike 复通过。

---

## 9. 待定 / 风险

| # | 项 | 说明 |
|---|---|---|
| 1 | CC 路 B spike | 已通过（2026-07-14）。回归失败有 Plan B（§8）。|
| 2 | crash 孤儿 marker（MS5） | bootstrap 写 marker 后 crash → 孤儿。重复 bootstrap 已 fail loud（§7.3）提示已有 run。doctor 可加孤儿检测。|
| 3 | NGA/CAC skill 加载真机 | 留用户侧验证（待验证决策，非冻结）。|
| 4 | nudge hook 默认开（MS4） | B 押 LLM，nudge 提为推荐默认开（只提醒不推进）。|
| 5 | 命令分家后两套决策核心共存 | node 级 tape 一致、agent_tool_call 层不一致。根治见 merge spec。|
| 6 | teams 命令名变量化实现 | env ORCA_BACKEND_CMD + 安装生成命令脚本，实现细节留 step 1。|

---

## 10. 决策清单（v3 冻结，勿重新讨论）

1. **执行模型 = B**（主 session 驱动，自调 next）；A/hook 推进退场。CC/opencode/NGA/CAC 统一 B。
2. **orca 单一接口**：`list/<wf>/next/status/stop/open/doctor` 7 命令；删 `in-session` 子命令层 + `start`。**四前端都调这一套**。
3. **命令迁移直接断**（不加别名，同 commit 改调用点，B1/D3）。
4. **删 command，统一 skill**（入口只有 skill）。
5. **setup 全删**（含 teams/Orchestrator + MCP breaking change）。
6. **teams 命令名变量化**（env ORCA_BACKEND_CMD，默认 teams）。
7. **catalog/inputs 归 skill**（via `orca list` + LLM 判断，skill 不读 YAML）；`orca <wf>` 只接 `--inputs`。
8. **前端集成 = skill**（一套 SKILL.md，四平台只落点不同）；hook 退居 nudge/诊断（不推进，nudge 推荐默认开）。
9. **输出统一 `--output` 字符串** + quoting 转义规约（`'\''`）。
10. **marker 只留 `{run_id, model, no_output_count}`**；重复 bootstrap fail loud。
11. **保留字黑名单**（wf name 禁取固定命令名，compile fail loud）。
12. in-session parallel / 动态构建本期不做。

---

## 11. 验收标准（MS8 闭环）

- `orca --help` 含 7 命令（list/wf 语法糖/next/status/stop/open/doctor）、**不含 teams 命令名**。
- opencode 3 节点 wf 跑通到 `workflow_completed`（主 session 自调 next）。
- CC 3 节点 wf 跑通（spike-gated，已通过）。
- `grep -r "orca in-session"` = 0 hits（清理后）。
- `grep` SKILL.md 业务逻辑关键词（advance_step/Orchestrator/router/Tape/compile）= 0 hits。
- setup YAML → compile fail loud（保留字/setup 段）。
- wf 取保留名（status/next/...）→ compile fail loud。
- `teams` 命令名可经 env 切换（改 ORCA_BACKEND_CMD 重装生效）。
- 重复 bootstrap 同 wf → fail loud 提示已有 run。
- marker 只含 `{run_id, model, no_output_count}`（grep 确认无 tape_path/yaml/session_id/owner）。
