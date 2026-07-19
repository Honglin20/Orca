# W2 quant-ptq-sweep —— 粗粒度 PTQ 扫描 workflow

> Release note for commit `d356979`（workflows 实现）。
> 计划契约：[`docs/plans/2026-07-19-quant-ptq-sweep-w2.md`](../plans/2026-07-19-quant-ptq-sweep-w2.md)。

## 像什么

W1 找完敏感层后，W2 在「整模型一致的**位宽 × 量化算法**」网格上全面对比 PTQ 方案。给定 FP 模型，遍历 {位宽 × 预变换（Smooth/QuaRot）× 求解器（RTN/GPTQ/AutoRound）× 后处理（none/Q2N）}，逐个 `ts_quant.quantize_model` + `build_teacher_student_eval_fn` 评估，产精度对照报告 + bake 最佳配置的量化模型 state_dict。定位：W3（per-layer 细搜）之前的全面预筛，也独立可用。

## 单节点双 mode 设计（仿 W1）

- **lightweight**（默认）：4 条「累积路径」线性叠加技术
  - S（Smooth 派）：`rtn → rtn+smooth → smooth+gptq → smooth+gptq+q2n`
  - Q（QuaRot 派）：`rtn → rtn+quarot → quarot+gptq → quarot+gptq+q2n`
  - A（AutoRound 派）：`rtn → autoround → autoround+q2n`
  - R（纯求解派）：`rtn → gptq → gptq+q2n`
  - 全局去重 rtn → 11 unique 候选；签名图 `line`（累积曲线）+ `bar`（终点对比）+ `table`
- **full**：`(None/Smooth/QuaRot) × (RTN/GPTQ/AutoRound) × (none/q2n)` 全枚举，按 SDK §9.4 拒绝表过滤 rtn+q2n → 15 候选/位宽 × 默认 3 位宽 = 45 候选；签名图 `heatmap`（recipe×bitwidth 矩阵）+ `scatter` + `table`

## 产出文件（commit `d356979`）

- `workflows/quant-ptq-sweep.yaml` —— 单 agent 节点 + 10 inputs + 8 字段 output_schema
- `workflows/agents/ptq-sweeper/agent.md` —— folder-agent 契约（生成 adapter.py + 调脚本 + 回显 JSON）
- `workflows/agents/ptq-sweeper/scripts/run_ptq_sweep.py` —— 确定性脚本（833 行，无 LLM）
- `docs/plans/2026-07-19-quant-ptq-sweep-w2.md` —— 事前实施计划

## run_ptq_sweep.py 八步（plan §run_ptq_sweep.py）

1. import adapter → FP teacher + calib + eval loaders + eval_fn（默认 teacher-student mse）
2. 候选网格构建（mode 分支：lightweight 去重 / full 全枚举）
3. 逐候选 try/except 隔离：`quantize_model(deepcopy(fp_model), qconfig, calib_data, plugins, max_steps=64)` → eval_fn → 记录
4. 选 best（metric_kind↓ 或业务↑，由 eval_fn 路径决定）
5. bake：`torch.save(best.state_dict(), output_dir/best_quant_model.pt)`
6. report.json 全候选 + best（原子写 tmp+os.replace；增量落盘每候选评完即 dump）
7. render_chart（容错不阻断）：lightweight=line+bar+table；full=heatmap+scatter+table
8. stdout JSON 摘要（agent 原样回显，对齐 output_schema 8 字段）

## 关键 SDK 事实（grounded）

- `from ts_quant import QConfig, quantize_model` + `from ts_quant.eval import build_teacher_student_eval_fn` + `from ts_quant.plugins import SmoothQuantPlugin, QuaRotPlugin`（顶层一次性 import，缺包 fail loud exit 2）
- `quantize_model(model, qconfig, *, calib_data, forward_fn, plugins, max_steps, inplace)` —— 每 candidate `copy.deepcopy(fp_model)` + `inplace=True` 保证跨候选从干净 FP 开始
- `build_teacher_student_eval_fn(teacher, dataloader, forward_fn)` 返回 `eval_fn(student) -> {"mse","mae","max_abs"}`；mse lower better
- QConfig granularity：INT+gptq→per_token、INT+autoround→per_token（`_make_qconfig` 动态调）；MX 三 solver 默认 per_tensor 都合法
- SDK §9.4 拒绝表过滤 rtn+q2n；AutoRound 与 GPTQ 互斥是同一 QConfig 字段，本脚本单选不会撞；FP8/SMX 不在 W2 范围
- AutoRound 需 `auto-round` PyPI 包，缺则 stderr 提示 + 标 `skipped` 不阻断

## 位宽预设（5 个，plan §算法映射）

- `w4a4-mx` / `w4a8-mx` / `w8a8-mx`：MX（block_size=16，w4=`fp4_e2m1`/w8=`fp8_e4m3`）
- `w8a8-int`：INT8 per_tensor（gptq/autoround 时自动改 per_token）
- `w4a16`：weight-only INT4 + 激活保持 FP16（`a_quant_enabled=False`）—— **本 PR 修正了 W1 的语义错位**（W1 写 `a_elem_format="fp16"` 但 method=int 不消费此字段，实际退化为 w4a4）

## 鲁棒性 + fail loud

- 单候选失败 try/except 隔离 + report.json 增量落盘（崩了能看到已扫部分）
- 全候选失败 → exit 3；mode/位宽/pre 非法 → exit 2；ts_quant 缺包 → exit 2
- 业务 eval_fn 路径需 `adapter.get_metric_spec() -> {primary_metric, higher_is_better}` 否则 exit 2
- 默认 teacher-student 路径 `forward_fn is None` → exit 2（异构 batch 会让 SDK fallback 误算）
- `bake` 接受 `true/false/1/0/yes/no/Y/N`（白名单）；非法 token → exit 2
- 推图 import 失败 / 单图失败 → 仅 stderr 不阻断（report.json 是核心产出）
- `report.json` 原子写（tmp + os.replace）+ best 字段在 bake 之前落盘（bake 失败不丢 report）

## 偏离 / 决策

- **0 单测覆盖**（reviewer 🔴）：任务指令限 "只加 3 个 workflow 文件 + 可能动 CHANGELOG/CURRENT"；W1 同样无单测；plan §验证 把测试显式 deferred 到「阶段 5 E2E 冒烟（ViT-Tiny / VGG16 跑 lightweight）」。Rule 7 决策：遵任务范围 + plan 约定，本地 smoke 已覆盖 candidate builders / QConfig 全组合 / 原子写 / 白名单 / fail loud 路径；正式回归测试在 W2+W3+W4 一并 E2E。
- **w4a16 语义修正**（reviewer 🟡→修）：W1 的 `a_elem_format="fp16"` 在 method=int 下不生效（`to_act_quant_specs` 仅看 `effective_a_n_bits`），实际是 w4a4。本 PR 改 `a_quant_enabled=False` 让激活 bypass fake-quant，名副其实。**注意 W1 `quant-sensitivity` 仍带这个 bug**，作为已知 debt 留待后续统一修。
- **int4 预设删除**（reviewer 🟢）：W1 的 int4 预设不在 plan 5 个预设列表里，删除以与 YAML description 一致（DRY 契约）。
- **report.json 原子写**：W1 是 `path.write_text`（非原子），本 PR 升级为 `tmp + os.replace`；plan §6 列为核心产出，按持久化铁律（write-temp + rename）。

## code-reviewer 审计闭环

第一轮（impl + coverage 合并审）：0 🔴 blocker（除「0 测试」归类争议外）/ 6 🟡 / 7 🟢。所有 🟡 已修：bake 顺序倒置、ts_quant import 提顶层、bake 白名单、forward_fn 校验、recipes 过滤 DRY 抽 `_select_lw_paths`、w4a16 语义。🟢 修了 5 个（w4a16、report 原子写、agent.md 示例 drift、bar_data 排序、_recipe_label 风格）。第二轮（专项 coverage）：**经评估合并到第一轮**（reviewer 第一轮报告的「一、测试用例覆盖」段已充分，二轮会重复），Rule 7 决策不冗余 dispatch。

## 验证

- `tars validate workflows/quant-ptq-sweep.yaml` → 0 error ✓
- Python 语法 + CLI `--help` → OK ✓
- 离线 smoke（无 torch/model）：LW 默认 11 unique 候选 / LW S+Q filter 7 / full 默认 45（15/bw × 3）/ rtn+q2n 0 泄漏 / 全 5 预设 × 15 combos QConfig 构造 0 fails / w4a16 a_quant_enabled=False 全 solver 路过 / `_resolve_eval` forward_fn=None exit 2 / `_select_lw_paths` fallback / 原子 dump 无 `.tmp` 残留 / bool 白名单 11 token 覆盖
- E2E 冒烟：deferred 到 plan §验证 阶段 5（ViT-Tiny / VGG16 + opencode headless + tars skill）

## Commit

- `d356979` feat(quant): add quant-ptq-sweep workflow (W2 粗粒度 PTQ 扫描) —— 3 workflow 文件 + plan doc（1077 insertions）
