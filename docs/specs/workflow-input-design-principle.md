# Workflow Input 设计原则(SPEC)

> 2026-07-21 由「极简派 vs 怀疑派」两 agent 辩论收敛而成。所有 workflow 的 inputs 定义、create-workflow-skill 的生成模板,以此为准。
> 配套:[[agent-ask-user-sentinel]](agent-ask-user-sentinel.md) 提供 Tier B 的「读代码→问用户→fail loud」执行机制。

## 0. 核心立场

**inputs 只放「下游 agent 无法执行 / 会失控」的必须项。** 其余按性质下沉到 Tier B(代码事实,agent 推断)或 Tier C(工程默认,固化)。

判定总纲:**代码里能 grep 出来的是事实(→ Tier B);代码里不存在的是意图(→ Tier A)。会静默产出错误交付物的回退路径必须 fail loud / 问用户(永不 silent default)。**

「向后传」的唯一可靠模式是 **infer-once + propagate**:在某个 setup 节点集中推断一次,写进 `output_schema`,下游用 Jinja 取。**严禁「每个 agent 各自重新自找」同一事实**(违反 DRY、自找不一致时远端崩、破坏复现)。

## 1. 三档分类

### Tier A — `[ask]` 必填 input(意图 / 预算 / KPI / 硬件 / 模型入口 / 业务命令)

agent 无法从代码读到、没有它 workflow 会失控或跑废。**必须**显式 input。

| 子类 | 例子 |
|---|---|
| 模型入口 | `model_path`、`teacher_model_path`(内省起点 + 用户资产锚) |
| 业务命令(原样执行,绝改不得) | `train_command`、`test_command`、`teacher_train_command` |
| 业务 KPI / 终止条件 | `target_latency_ms`、`accuracy_target`、`accuracy_gap_db`、`avg_bit_budget`、`accuracy_tolerance` |
| 预算闸门(被确定性脚本消费,非 LLM 自决) | `max_rounds`、`max_evals` |
| 目标硬件 | `target_hardware`(cuda / npu / cpu) |
| 复现性底座 | `seed`(默认 0,**全部 workflow 必须新增**) |

### Tier B — `[infer]` 代码事实(setup 节点推断一次 + output_schema 向后传;缺失走哨兵)

agent 读用户代码能得到的事实。**不是 workflow input**,是某个 setup 节点的 `output_schema` 字段。

获取顺序固定三步,**任何一档失败才能进下一档,绝不造假**:
1. **读代码**:具体路径提示写进 agent.md(如「在 project_root 下 grep `def load_calib`,dotted-path = `模块:函数`」)。
2. **歧义/找不到 → 哨兵问用户**(见 sentinel SPEC):`{"_orca_ask_user": "...", "options": [...], "context": "..."}`。TARS 恢复同一子 agent 继续。
3. **用户也答不出 → fail loud**:绝不 `torch.randn` / 复用 train 当 eval / 静默默认 0。

| 类别 | 例子 |
|---|---|
| 项目结构 | `project_root`、`build_fn`、`dummy_input`、`model_family` |
| 数据 loader dotted-path | `calib_data_ref`、`train_data_ref`、`eval_data_ref`、`eval_dataset` |
| 评估函数 | `eval_fn_ref`(**注意:这是「意图」边界**——代码里多个 eval 函数时歧义必走哨兵;唯一明确才用;用户无业务 eval_fn 才退 teacher-student mse,且须用户显式同意,**永不静默退回**) |
| 训练超参(可读 train.py/config) | `lr`、`batch_size`、`total_steps`、`epochs`、`short_epochs`、`full_epochs`、`teacher_layers` |

> **lr / epochs 的复现性边界**:默认值是 smoke 不是生产。提供 `smoke` 开关:`smoke=true` 允许默认;`smoke=false` 时这些必须 [ask](用户显式给)。seed 全程固定保复现。

### Tier C — `[default]` 工程默认(固化,不当 input、不强问)

纯 plumbing / 算法开关,99% 用户不该决策。固化在 agent.md 模板 / 脚本默认 / 引擎注入三处之一。

| 类别 | 例子 |
|---|---|
| 工程路径 | `output_dir`(→ 引擎注入 `$ORCA_ARTIFACTS_DIR`)、`struct_scripts_dir`、`kd_scripts_dir`、`kb_cache_dir` |
| 循环推导 | `iterations`(由 `max_rounds × 每轮节点数` 自动算) |
| 算法开关 / 预设 | `mode`、`recipes`、`scheme`、`bit_width(s)`、`candidate_format_space`、`bit_objective`、`granularity`、`method`、`ratio`、`low_bits`、`high_bits`、`bake`、`cage`(=auto)、`proxy_dataset_spec` |

## 2. 反向判据(否决 KEEP)

一个 input 满足任一条 → 强制下沉:
- 能在 `model.py`/`train.py`/`config.yaml` grep 到 → Tier B
- 改它需要懂 workflow 内部 → Tier C
- 留空有合理默认且非业务 KPI → Tier C
- 与代码事实会漂移(用户改代码忘改 input)→ **必须** Tier B

## 3. 标签约定(给 TARS in-session 编排器的语义合约)

每个 input 描述以标签起头:
- `[ask]` Tier A:TARS bootstrap 时集中问用户(若用户意图里没给)。
- `[infer]` Tier B:不是 input,是 setup 节点 output 字段;agent 运行时推断,缺失走哨兵。
- `[default]` Tier C:固化,不问。
- `[advanced]` Tier C 子集:罕见 override,固化默认,文档可见但不暴露为主输入。

## 4. 黄金模板

`workflows/agent-struct-exploration.yaml` 的 `family_detect` 节点(L72-116)已实现 Tier B 的 infer-once + propagate:`project_root/build_fn/dummy_input` 下沉为节点 output,下游 `{{ family_detect.output.project_root }}` 取。**所有 workflow 对齐此范式。**

## 5. 各 workflow 目标 input 数(收敛后)

| workflow | 现 | 目标 | 说明 |
|---|---|---|---|
| agent-struct-exploration | 8 | 6 | 已近模板;补 seed;下沉 struct_scripts_dir/iterations |
| kd-nas | 14 | 6 | 下沉 9 项(eval_dataset→infer、teacher_layers/short/full_epochs→default/infer、scripts_dir/iterations) |
| nas-agent-pipeline | 3 | 4 | 补 target_hardware/latency_constraint/max_rounds/seed;下沉 project_root/output_dir |
| nas-hp-search | 3 | 4 | 同上 |
| quant-ptq-sweep | 10 | 3 | calib/eval 走 [infer];mode/bit_widths/recipes/bake→default;补 target_hardware/seed |
| quant-sensitivity | 9 | 3 | 同上 |
| quant-qat | 13 | 3 | lr/total_steps→[infer];cage/scheme/bit_width/bake→default;补 target_hardware/seed |
| quant-bit-curve | 14 | 6 | calib/eval→[infer];mode/format_space/granularity/bake→default;保留 accuracy_tolerance/avg_bit_budget/max_evals(KPI/预算);补 target_hardware/seed |

## 6. create-workflow-skill 自校验 checklist

生成 / 改 workflow 后必跑:
- [ ] 每个 input 归类到 Tier A 四子类之一,否则下沉
- [ ] Tier B 项有 setup 节点 output_schema 字段承接(infer-once + propagate,链不破)
- [ ] Tier B 项在 agent.md 有「读代码→哨兵→fail loud」契约段
- [ ] `output_dir` 不作 input(走 `$ORCA_ARTIFACTS_DIR`)
- [ ] `iterations` 不作 input(自动算)
- [ ] 算法开关 / 预设都不作 input(固化)
- [ ] 业务 KPI 不缺(latency / accuracy / max_rounds / target_hardware 至少齐其相关项)
- [ ] **全部 workflow 有 `seed`(默认 0)**
- [ ] 移除任何 input 时,同步更新所有引用 `{{ inputs.X }}` 的 agent.md Jinja(避免 StrictUndefined 崩)
