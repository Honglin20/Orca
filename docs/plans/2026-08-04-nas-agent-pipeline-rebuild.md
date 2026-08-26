# Plan — nas-supernet workflow（v5，spec-review 三轮闭环版）

> 日期：2026-08-04 ｜ 状态：**待 spec-review v5** ｜ SDD
> 源：`D:\Projects\nas-agent`（只读）｜ 新建：`workflows/nas-supernet.yaml`（纯增量）
>
> **v5 变更**（闭环 v4 的 B1/B2/B3）：**B1(blocker)** nas-supernet 全用新 agent 名 `ns_*`（下划线 Jinja 安全），**纯增量——零覆盖零删除**，不碰任何现有 workflow/agent（`nas-hp-search.yaml` 活 workflow 不受影响）；B2 §10 行号引用订正；B3 §9.2 install 挂载点明示。

---

## 1. 背景与根因（fidelity 覆盖已收窄）

旧 `nas-agent-pipeline.yaml` acc 不符预期：跑 nas-agent 重构前过期 skill 快照，缺 fidelity-verifier/porter/memory-verifier + Non-Searchable Logic 处理，5 subagent 一个没迁 → 脚本静默偏离原项目语义 → acc 崩。
**覆盖诚实声明**：迁入 fidelity-verifier 根治 **「ported 训练/eval 逻辑漂移」一类** acc 崩。**不覆盖** NAS 编排正确性（sandwich 采样/subnet 提取/search evaluator 逻辑）——那由 supernet-evaluator + workflow-verifier + 节点 smoke + agent 软判断共同保证。acc 最终**软判断**，fidelity-verifier **必要非充分**。

## 2. 目标与成功标准（I8 可验证）

固化 nas-agent 最新版「3 主 skill + 5 subagent + Pipeline Memory」为 Orca workflow `nas-supernet`，补「auto-run 自愈 + 选架构 + retrain + 可视化」，MNIST headless E2E 验证。
1. `tars validate workflows/nas-supernet.yaml` 通过。
2. **MNIST E2E**：全链 supernet → ckpt → Pareto → 选定架构 → retrain 最终权重 → 图表。
3. 生成节点 `output_schema` 含 `fidelity_passed: bool` / `workflow_verifier_passed: bool`，tape 可查。
4. auto-run 节点：脚本**正确执行**（自愈到 RC=0，绝不带错下传）；收敛/结果软判断。
5. select 确定性可重跑；select 崩 → `terminate_select_failed` fail loud。
6. **时延下降**：选定子网时延 < 全开超网。
7. **图表正常**：帕累托图 + 搜索过程表 + loss 曲线 + test 指标（按项目 metric）+ 张开前后对比表。

## 3. 已对齐决策

| 决策点 | 选择 |
|---|---|
| 范围 | 生成 + auto-run 训练/搜索 + 选架构 + retrain + 可视化 |
| acc 闸门 | 不加硬闸门；软判断+报告 |
| auto-run 出错 | 自愈到正确执行，fail loud，绝不带错下传 |
| 执行环境 | runner 直接执行 launcher（GPU 机 / MNIST 小可 CPU）；detach+轮询 |
| subagent 存储 | `~/.orca/nas-supernet/subagents/`，node 用 `$HOME` read+embed，host 内置通用 `subagent_type` |
| 选架构 | 确定性：`target_latency_ms` 下 max-acc；缺则 knee |
| 时延脚本 | 提供 `latency_script_path` → 包装用户脚本（onnx 单文件禁 .data）；未提供 → nas-agent 内置 PyTorch latency |
| ask-user | 全删；缺关键 → fail loud 或文档化假设 |
| self-heal 约束 | prompt 软约束 + tape 审计字段（`healed_files`/`fidelity_retriggered`），非引擎硬闸门 |
| **现有 workflow/agent** | **纯增量，零覆盖零删除**——nas-supernet 全用新名 `ns_*`，不碰 nas-hp-search/nas-agent-pipeline 等现有（B1） |
| subnet-prune | 不纳入 |
| 旧 workflow | 不动（nas-agent-pipeline.yaml 保留原状；本 plan 不改它——B1 简化） |
| executor | opencode + deepseek（`deepseek/deepseek-v4-flash`） |
| 复用 | SKILL.md → agent.md 最小适配；5 subagent body 逐字 |

## 4. SPIKE 结论（双实证）

SPIKE1：headless `opencode run` 调内置 Task spawn 子 agent；Orca 一等公民读子 session；节点派子 agent 是既定模型。
SPIKE2：`subagent_type` 必填但填 host 内置通用类型 + 指令内嵌 prompt 即可跑特化，无需 host 注册 → subagent 可移植文件。
接线：node body 是 `opencode run` message；subagent 经 read+embed 用 host 通用类型 spawn。

## 5. 架构：2-tier（最小引擎改动）

```
node (opencode run <SKILL body>)
  1. Bash: cat $HOME/.orca/nas-supernet/subagents/<name>.md
  2. Task(subagent_type=<host 内置 general>, prompt=<body> + <任务+inputs> + <上一轮 report?>)
```
**最小引擎改动**：仅扩展 `install_cmds.py` 部署步（`workflows/_nas-supernet_subagents/` → `~/.orca/nas-supernet/subagents/`，§9.2），不动 exec/schema/events/render。不引入新 env（subagent 文件是运行时 data，node Bash `$HOME` 读取，非 compile 期 AgentResolver 解析对象）。host-agnostic：claude/opencode/codex 同一套。

## 6. Workflow DAG

```
ns_expand_supernet (agent, entry)
   │ supported? ──no──► terminate_unsupported (failed)
   ▼
ns_train_script (agent)
   ▼
ns_search_pipeline (agent)         gen search 脚本 + select_architecture.py + AGENTS.md
   ▼
ns_run_train (agent)               self-gate(not viable→skip)；self-heal RC=0；收敛软报告
   ▼
ns_run_search (agent)              self-heal RC=0；Pareto 软报告
   ▼
ns_select (agent, 确定性)           见 §7.2
   │ selected_arch 有 ──no──► terminate_select_failed (failed)
   ▼
ns_retrain (agent)                 gen retrain(scaffold) + fidelity + self-heal 执行 + 最终 acc 软报告
   ▼
ns_visualize (agent)               读 artifacts → 渲染图表（chart 子系统）
   ▼
$end
```
`ns_run_train` 自门控以 **`run_train_supernet.sh` 文件存在性为权威信号**（I10），summary.viable 仅文档。

## 7. Agent 花名册（**全部新名 `ns_*`，零覆盖**）

### 7.1 node agent（`workflows/agents/<name>/agent.md`）

| 节点名 | 源 | 职责 | 内部调 subagent |
|---|---|---|---|
| `ns_expand_supernet` | expand-to-supernet/SKILL.md | 7 步（A/B 删、ask-user 删）；不支持→terminate | supernet-evaluator、workflow-verifier、memory-verifier |
| `ns_train_script` | supernet-train-script/SKILL.md | viability；porter 移植；gen train 脚本；补 summary | porter、fidelity-verifier、workflow-verifier、memory-verifier |
| `ns_search_pipeline` | nas-search-pipeline/SKILL.md | latency_estimator（§10）；search 脚本；+ 生成 select_architecture.py；AGENTS.md；Non-Searchable Logic；NPU foreach=False | workflow-verifier、porter、fidelity-verifier、memory-verifier |
| `ns_run_train` | 新增 | §8 | self-heal；按需 fidelity-verifier |
| `ns_run_search` | 新增 | §8 | 同上 |
| `ns_select` | 新增（确定性） | §7.2 | 无 |
| `ns_retrain` | 新增（消费 AGENTS.md） | §8 | fidelity-verifier |
| `ns_visualize` | 新增 | §16 | 无（chart 子系统） |

### 7.2 ns_select（N1 闭环：agent 节点 + Bash，跨平台）

`ns_search_pipeline` 生成 `$ORCA_ARTIFACTS_DIR/select_architecture.py`（schema-aware：它定义 search 结果 schema + 项目 metric 方向）。`ns_select` 是 folder agent `workflows/agents/ns_select/agent.md`，确定性 Bash 调用：
```bash
python3 "$ORCA_ARTIFACTS_DIR/select_architecture.py" \
  --target-latency-ms "{{ inputs.target_latency_ms }}" \
  --search-results "$ORCA_ARTIFACTS_DIR/search_results.jsonl"
```
agent.md 铁律：「**运行上面命令恰好一次，不许改脚本/不许自己重算，把 stdout JSON 作为唯一输出**」。`$ORCA_ARTIFACTS_DIR` 经 **Git Bash 展开**（`orca/exec/env.py:91` 注入 + `examples/agents/plotter/agent.md:12` 既有 `$ORCA_AGENT_RESOURCES` pattern 证同机制），规避 ScriptNode cmd.exe 不展开 `$VAR` 的 N1 blocker。
**output_schema**（agent 节点直接强制 JSON 契约）：
```json
{"selected_arch": <dict>, "selected_acc": <number>, "selected_latency_ms": <number>,
 "pareto_size": <int>, "select_reason": "max-acc-under-target|pareto-knee"}
```
路由守卫：`when: "ns_select.output.selected_arch is defined"` → ns_retrain；else → `terminate_select_failed`。ns_retrain 引用 `{{ ns_select.output.selected_arch }}`。

### 7.3 subagent（`~/.orca/nas-supernet/subagents/*.md`，body 逐字迁）

`supernet-evaluator`/`workflow-verifier`/`memory-verifier`/`project-porter`/`project-fidelity-verifier`。body 逐字；无 frontmatter、无 host install（I9：源无 tools 字段，read-only/只改目标文件一直靠 prompt，read+embed 零损失）。

## 8. auto-run / retrain 节点设计（N5 诚实声明软约束）

通用执行模型（`ns_run_train`/`ns_run_search`/`ns_retrain`）：
1. **自门控**（仅 train）：`run_train_supernet.sh` 不存在 → `{status: skipped}`。
2. **执行**：`cd $ORCA_ARTIFACTS_DIR` → `nohup bash run_*.sh > run.log 2>&1 &` detach + 轮询（Git Bash win32 经现 `nas-train-runner` 验证可行，沿用——I12）。
3. **self-heal（硬闸门=成功执行）**：执行出错 → 读日志 → **仅按编辑白名单修** → 重跑到 RC=0+预期产物。超 max_retries=3 → fail loud，**绝不带错下传**。
4. **软判断（报告非闸门）**：读收敛曲线/搜索结果，agent 自判写 `assessment`。
5. **output_schema**：`{status: executed|skipped|failed, artifacts:[...], assessment:"...", max_retries_hit: bool, healed_files:[list], fidelity_retriggered: bool}`；`status==failed` → 引擎判失败。`fidelity_retriggered`：触碰白名单内项（训练逻辑）后 agent 主动重触 fidelity-verifier → 自报 true。

**编辑白名单**（**prompt 软约束，非引擎硬闸门**——N5）：仅 `run_*.sh`、import 路径错、明显 typo、`search_config.yaml` 路径/参数对齐。对 `train_supernet.py`/`evaluator.py`/`retrain.py` 训练逻辑（loss/optimizer/sampling/KD/数据管道）改动 = 语义疑点 → **应当**重触 fidelity-verifier。
**防蒙混靠审计非靠引擎**：`healed_files` + `fidelity_retriggered` 写进 output_schema（tape 可查）；用户/下游 review 核对 healed_files 是否触碰禁碰清单。
**禁碰清单**（runner agent.md 铁律段）：`supernet.py`、`project_manifest.md`、`supernet_summary.md`、`<user_project_root>` 下任何文件（只读）。

**ns_retrain 特有**：读 ns_select 选定 arch + AGENTS.md scaffold + summary + manifest → 生成 `retrain.py`/`finetune.py` → fidelity-verifier 复查 → 执行 self-heal → 报告最终 acc。

## 9. 移植策略

### 9.1 SKILL.md → agent.md（8 条替换）
1. `<skill_dir>` → `$ORCA_AGENT_RESOURCES`。
2. `<output_dir>` → `$ORCA_ARTIFACTS_DIR`。
3. `<user_project_root>` → `{{ inputs.user_project_root }}`；`<nas_agent_root>` 探测保留。
4. `Explore` 子 agent → 退化 Read/Grep/Bash 直接探。
5. **A/B consent + ask-user sentinel 全删**；缺关键 → fail loud 或文档化假设。
6. 「present next steps / new session」收尾语 → 删。
7. **subagent read+embed 协议**（节点顶部统一块，正文调用处只写「按协议调 `<name>`，inputs=…」）：
   > `cat $HOME/.orca/nas-supernet/subagents/<name>.md` 取 body → `Task(subagent_type=<host 内置通用>, prompt=<body> + <任务+inputs> [+ <上一轮完整 report>] [+ Fixed:[ids]/Context:[id]])`。
   **verifier resume（I6）**：每轮 fresh Task 须 embed `<body> + <任务+inputs> + <上一轮完整 verifier report> + <Fixed:[ids]/Context:[id]>`。token 语义不变；操作语义变「fresh subagent 凭重 embed 的 report 复核」。大项目多轮 loop 有 token 压力，监控。
8. **todolist 适配（I5）**：opencode 无等价则退化「回复中维护 markdown 编号清单」；verifier checklist 物化改 markdown 内嵌 report。

references/assets 原样迁 `workflows/agents/<name>/{references,assets}/`，保 `workflows/`+`workflow-checklists/` 兄弟。

### 9.2 subagent 部署 + install 拓扑映射（N3+B3 闭环）
- repo 单一源 `workflows/_nas-supernet_subagents/*.md`（5 body 逐字）。
- **install 拓扑映射规则**（通用）：`install_cmds.py` 加 `_install_bundled_subagents`，glob `workflows/_*_subagents/` → 落 `~/.orca/<中间名>/subagents/`（`_nas-supernet_subagents` → `nas-supernet`）。**挂载点**（B3）：在 `run_install` 内、`_install_bundled_workflows` 之后串行调用，与 `_install_bundled_knowledge_base` 同级（`install_cmds.py:529,538`）。
- 运行时 node Bash `$HOME/.orca/nas-supernet/subagents/<name>.md` 读取；repo 是 source of truth，install 单向物化，运行时不回写（I11）。

## 10. 时延脚本规则（N2+B2 修正：默认 PyTorch，非 onnx）

`ns_search_pipeline` 生成 `latency_estimator.py` 时：
- **未提供 `latency_script_path`（默认）**：用 **nas-agent 内置 PyTorch latency**——`measure_module_latency(subnet, dummy_input, ...)`，定义于 `nas-agent/nas_agent/latency/pytorch_latency_utils.py:94`（`@torch.inference_mode()` + `nn.Module`，PyTorch 非 onnx；onnx 是 nas-agent future 规划）。
- **提供 `latency_script_path`**：latency_estimator 包装用户脚本：
  - 候选子网 export 成**单文件 onnx**——保证参数 <2GB（自然不产 `.data`），或 export 后 `onnx.save_model(path, model, save_as_external_data=False)` 显式禁（**`torch.onnx.export` 无 `external_data` 参数，禁 .data 用 onnx 包的 `save_as_external_data=False`**）。
  - **用户脚本契约**（agent.md 明示）：入参 = onnx 文件路径（命令行 arg）；stdout 末行或返回值 = 时延 ms（数字）；退出码 0=成功。dummy_input 构造 = latency_estimator 责任（按 manifest input shape）。
  - latency_estimator 调用户脚本 + 解析时延；IO 张量名/shape/dtype 不匹配由 latency_estimator 适配（不改用户脚本）。
- MNIST E2E 不提供 latency_script_path → 走默认 PyTorch latency。

## 11. Pipeline Memory 与状态传递
manifest+summary+全产物落 `$ORCA_ARTIFACTS_DIR`（节点共享）。状态优先走 summary/doc。选定架构经 `ns_select.output.selected_arch` 向 ns_retrain 传递。

## 12. Inputs

| input | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_project_root` | string | 是 | [ask] 原 PyTorch 项目根 |
| `model_path` | string | 是 | [ask] 目标模型入口文件 |
| `target_latency_ms` | number | 是 | [ask] 选架构时延目标；缺则 knee 兜底 |
| `latency_script_path` | string | 否 | [advanced] 用户时延脚本；提供则强制包装用它（§10） |
| `seed` | int | 否 | [default] 0 |

## 13. 设计挑战与解法（I1-I12 + N1-N6 + B1-B3 全闭环）

| # | 闭环方式 |
|---|---|
| I1 | `~/.orca/nas-supernet/subagents/` + `$HOME` read，最小引擎改动（§5/§9.2） |
| I2/I3 | ns_select agent 节点 + output_schema 强制 JSON 契约 + 路由守卫 + terminate_select_failed（§7.2） |
| I4 | fidelity 覆盖收窄（§1） |
| I5 | todolist 退化 markdown（§9.1.8） |
| I6 | read+embed 重 embed 上一轮 report（§9.1.7） |
| I7/N5 | self-heal 白名单=prompt 软约束；审计靠 `healed_files`/`fidelity_retriggered` tape 字段（§8） |
| I8/N4 | output_schema verifier_passed 字段 + select 烟测 fixture（§15.7） |
| I9 | 源无 tools 字段，read+embed 零损失（§7.3） |
| I10 | viability 以文件存在性为权威（§6） |
| I11 | repo 单一源 + install 单向物化（§9.2） |
| I12 | detach 经现 runner 验证（§8.2） |
| N1 | ns_select agent 节点 + Bash + `$ORCA_ARTIFACTS_DIR`（Git Bash 展开，§7.2） |
| N2/B2 | 时延默认 PyTorch `measure_module_latency`（`pytorch_latency_utils.py:94`）；用户脚本 onnx-wrap 用 `onnx.save_model(save_as_external_data=False)`（§10） |
| N3/B3 | 「最小引擎改动」+ install 拓扑映射 + `run_install` 挂载点明示（§5/§9.2） |
| **N6/B1** | **nas-supernet 全用新名 `ns_*`，纯增量零覆盖零删除**，不碰 nas-hp-search 等现有 workflow/agent（§7/§14） |

## 14. 文件布局（B1 闭环：纯增量）

```
workflows/nas-supernet.yaml                            # 新 workflow（唯一改动现有文件=新增此文件）
workflows/agents/
  ns_expand_supernet/{agent.md, references/, assets/}  # 新
  ns_train_script/{agent.md, references/}              # 新（不覆盖 supernet-train-script）
  ns_search_pipeline/{agent.md, references/, assets/}  # 新（不覆盖 nas-search-pipeline）
  ns_run_train/agent.md                                # 新
  ns_run_search/agent.md                               # 新
  ns_select/agent.md                                   # 新
  ns_retrain/agent.md                                  # 新
  ns_visualize/agent.md                                # 新
workflows/_nas-supernet_subagents/                     # 5 subagent body（repo 单一源，新）
  {supernet-evaluator,workflow-verifier,memory-verifier,
   project-porter,project-fidelity-verifier}.md
~/.orca/nas-supernet/subagents/                        # install 落点（运行时 read）
# select_architecture.py 由 ns_search_pipeline 生成进 $ORCA_ARTIFACTS_DIR
```
**零覆盖零删除**（B1）：现有 `supernet-train-script`/`nas-search-pipeline`/`nas-select`/`nas-train-runner`/`pytorch-model-optimizer` 及 `nas-hp-search.yaml`/`nas-agent-pipeline.yaml` **全部不动**。nas-supernet 用全新 `ns_*` agent 名，纯 additive。无破坏风险。

## 15. 实施阶段

1. 迁 5 subagent body → `workflows/_nas-supernet_subagents/` + 扩 `install_cmds.py` 加 `_install_bundled_subagents`（§9.2，挂 `run_install` 内 workflows 之后）。
2. 迁 3 生成 node agent（`ns_expand_supernet`/`ns_train_script`/`ns_search_pipeline`，§9.1 八条替换 + read+embed 协议块 + 删 ask-user）+ references/assets。
3. 写 `ns_run_train`/`ns_run_search`/`ns_retrain` node agent（§8）。
4. 写 `ns_select` agent（§7.2）+ `ns_visualize` agent（§16）。
5. 写 `nas-supernet.yaml`（§6 DAG + §12 inputs + output_schema + routes + select 契约）。
6. `tars validate` + **两级 dry-run**：(a) ns_expand_supernet 单节点；(b) **select 契约烟测**——人工写 minimal `select_architecture.py`（10 行：读 jsonl + 按 target_latency 选 max-acc）+ 5 条 fixture search.jsonl，跑 ns_select agent，断言 `output.selected_arch` 非空 + 路由进 ns_retrain（N4，无 GPU 抓 select 类 bug）。
7. 自 review（code-reviewer）。
8. release note + CHANGELOG + CURRENT。

## 16. 可视化阶段（核心逻辑审查通过后）

派 agent 分析可视化项 → 实现 `ns_visualize` 节点（读 `$ORCA_ARTIFACTS_DIR`，走 Orca chart 子系统 `$ORCA_CHART_SOCK`/`render_chart`，`examples/render_chart.yaml` plotter 模式）→ review。**必含**：
- 帕累托前沿解散点图（latency vs metric，标注选定架构）。
- 搜索过程表（所有候选子网：arch 配置 / latency / metric / 是否 Pareto）。
- 超网训练 loss 曲线（按 step/epoch）。
- test 指标（**按用户项目 metric**——MNIST=accuracy；无线=NMSE；不硬编码，由 manifest metric 字段驱动 + 方向 higher/lower-better）。
- 超网张开前后对比表（参数量/FLOPs/latency/metric：全开 vs 选定子网）。
- 其余由分析 agent 补充。

## 17. MNIST fixture（E2E 目标，已建）
`tests/e2e_nas_supernet/fixtures/mnist/{model.py,train.py,test.py,README.md}`（标准 PyTorch MNIST，小 CNN 可 supernet 化，train 最小化 CE loss，test 打印 accuracy）。供 headless E2E（小，CPU/单 GPU 可行）。

## 18. E2E headless 测试
**test-agent headless + tars-skill** 跑 `nas-supernet`（inputs→MNIST fixture + target_latency_ms + seed）。我监控：所有节点 ns_expand→...→ns_retrain→ns_visualize 完成；超网正确训练（loss 降）；搜索跑通；**时延下降**；最终 retrain 出权重；**图表正常**。失败→迭代修复→重跑到全绿。

## 19. 开放项
- A/B consent → optional 优化 input 开关（低优先级 follow-up，当前全删）。
- opencode 1.17.20 resume 实测（verifier loop 已用 read+embed 不依赖 resume）。
- select 无 target_latency 的 knee 算法实现时定。
- 用户时延脚本 onnx-wrap 路径已规格化（§10），MNIST E2E 走默认 PyTorch 不行使；真用户脚本首次用时验证。
