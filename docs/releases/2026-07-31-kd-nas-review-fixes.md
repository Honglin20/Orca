# KD-NAS Review #6（可视化 conditional-pass）+ #5（思路 pass 建议）修复（2026-07-31）

> 修 review #6（可视化 conditional-pass）4 项必修 + review #5（思路 pass）1 项建议。
> 范围严格限定在列出的 5 项；不动 model-flatten / kd-train-script / teacher-gen / kd-select 的
> SKILL/scripts，不动 `docs/status/CURRENT.md`。逐项 surgical fix，附方向决策说明。

## 做了什么

### 1. latency bar 的 baseline 参考线缺失（review #6-1）
- `train_pool.py`：加 `--baseline_latency_ms`（type=float, default=None）argparse + 条件透传给
  `viz_argv`（None 时不加 flag → viz_kd 按 default=None 跳过 baseline 行，不静默造假）。
- `kd-train/agent.md`：bash 块加 `--baseline_latency_ms "{{ setup.output.baseline_latency_ms }}"`
  + 输入文档行补 `baseline_latency_ms = {{ setup.output.baseline_latency_ms }}`。
- **来源链**：flatten `__main__` 实测 → `flatten.output.baseline_latency_ms` → setup 透传
  → `setup.output.baseline_latency_ms`（yaml required，必填）→ kd-train agent.md → train_pool → viz_kd。
- 现状修前：viz_kd 已支持 `--baseline_latency_ms`（latency bar 已把 baseline 加 data），但 train_pool
  从未透传 → latency bar 永远缺 baseline 那根。

### 2. accuracy_compare 的 baseline 仅 caption 无数据（review #6-2）
- `viz_kd._push_accuracy_compare`：把 baseline 作为一行加入 data
  （`{variant_id:"baseline", accuracy:<disp>, met_accuracy:"ref"}`），对齐同文件 `_push_latency_bar`
  （后者已把 baseline/target 加 data）。前端据 `met_accuracy="ref"` 区分画参考标记 / 普通 bar。
- 修前：caption 承诺「虚线=accuracy_baseline」但 data 无 baseline 行 → 前端无数据画不出。

### 3. 进度图 0 计数过滤与注释不符（review #6-3）
- `viz_kd._push_progress`：去掉 `if counts.get(k,0)>0` 对**固定 order 项**的过滤——固定 status
  （SUCCESS/FAIL_accuracy/FAIL_train/FAIL_latency/FAIL_export）即便计数 0 也保留，实现注释承诺的
  「未见的仍以 0 呈现给 operator 一眼全貌」。仅对 order 之外的杂项 status 过滤 0（杂项不占位）。
- 修前：注释 vs 代码矛盾——`if counts.get(k,0)>0` 把未见固定项也滤掉，operator 看不到全貌。

### 4. min 方向 kind 视觉误导——取负显示（review #6-4，goal 硬要求）
- `viz_kd` 的 accuracy_compare / sweep_scatter / pareto：min 方向 kind（db/nmse/mse/ber）下对 accuracy
  **取负显示**（`acc_disp = -acc`），y_label 改「显示 -原值，越大越好（原指标越低越好）」。
- 新增 helper `_acc_display(acc, kind)`（min→`-acc`，max→原值）+ `_acc_y_label(kind)`。
- 对齐 `nas-train-runner/scripts/tail_metrics.py:158-163,209`（`_classify_obj` + `disp = -v if quality`）。
- max 方向（snr/acc）不变。pareto 的 `pareto_y_direction` 随之恒为 `'max'`（取负后 displayed 数据
  「越大越好」统一；min 取负使 -20<-22 翻转为 20<22，max 前沿与原 raw+min 前沿等价）。

**为什么取负显示（不是只改 y_label 文字）—— Rule 7 决策**：
- goal 是「坐标轴方向不能让人误判」。bar 图 -20dB 的 bar 比 -22dB 高 → 视觉强误导（bar 高度直接
  传达「这个更好」），仅 y_label 标「越低越好」不足以消除 bar 高度的直觉误导（用户瞄一眼仍会判
  -20dB 优于 -22dB）。
- 三档方案：① 仅 y_label 文字（最弱，直觉仍误导）；② 加方向箭头标注（中，依赖前端配合）；
  ③ **取负显示（最强，从数据层消除歧义——displayed 轴上越大越好统一）**。选 ③：与 tail_metrics
  已有口径一致（同一项目内不造两套显示规则），且 operator 一眼可见无需读 label。
- 代价：displayed y 值与原值差一个负号（-20dB 显示为 20）。在 caption + y_label 双重标注
  「显示 -原值」「原指标越低越好」让 operator 可还原原值。可接受（消除误判 >> 多读一行 label）。

**未知 kind 处理 —— Rule 7 决策（扩展口径，已公示）**：
- 任务原文「未知 kind 仍 fail loud 跳过（不 auto 猜）」。pareto 原本就跳过；**本次将 scatter /
  accuracy_compare 也扩展为未知 kind 跳过**（原本它们用「方向未知」label 仍推图）。
- 理由：取负显示需已知方向；未知 kind 不知是否该取负 → 不能安全渲染一个方向不明的 accuracy 轴
  （goal「不能让人误判」+「不 auto 猜」共同要求跳过）。「方向未知」文字标注不足以消除 bar 高度
  误导（同 goal 论证）。
- 若 operator 希望未知 kind 时仍看 latency-accuracy 分布（scatter），可后续把 scatter 单独放宽
  （scatter 是分布图，bar 高度误导较弱）；本次三图同口径一致性优先。

### 5. is_measured_row 直接单测（review #5 建议）
- 新增 7 条 `test_is_measured_row_*`（加到 `test_kd_redesign.py`，紧随 `accuracy_direction` 单测）：
  real SUCCESS / real FAIL_accuracy（accuracy_kind 非空）/ FAIL_latency 哨兵 / FAIL_train 哨兵 /
  measure-fail-FAIL_accuracy 哨兵（accuracy_kind 空）/ SUCCESS 但 accuracy_kind 空（伪造防御）/
  未知 status（FAIL_export 等）。
- 理由：`is_measured_row` 是「真测 vs 哨兵」唯一裁判（决定帕累托前沿画哪些行 + select 选哪些），
  原本仅间接覆盖（经 select._measured_rows 测试）；源头锁契约防演进时漏判。

## 偏离计划
- 无实质偏离。任务列出的 5 项逐字实现；review #6-4 的「未知 kind 跳过」扩展到 scatter/accuracy_compare
  属 Rule 7 决策（已在上方公示理由 + Open Questions 留口）。

## Review-driven 修复（两路 code-reviewer 自检后补）

无 BLOCKER。两路 reviewer 各 1 MAJOR + 若干 MINOR，已逐项处置：

- **MAJOR（测试）— latency_bar baseline 端到端 intent 断裂**：原 `test_train_pool_viz_argv_passes_baseline_latency_ms`
  只验「flag 进 argv」不验「viz_kd 真把 baseline 加 data」。补 `test_latency_bar_baseline_as_data_row`
  + `test_latency_bar_no_baseline_when_none`（值级断言 baseline/target/变体行，与 accuracy_compare
  baseline 行断言对称）——闭合 review #6-1 端到端 intent。
- **MAJOR（文档）— `kd-train/agent.md:69` 预存结构歧义**：`accuracy_baseline_kind 已加回 inputs`
  放在「**已下沉**」标题下，字面归属冲突。**未修**——该行是 finalize 预存文本、非本 session 引入、
  不在列出范围（任务「只改列出的，不扩大范围」）。列 Open Question #4 留待后续 doc 整理。
- **MINOR（文档精确性）— `_acc_display` docstring 易误导**：原文「对齐 tail_metrics.py」未区分
  display 变换 vs kind 检测。tail_metrics 的 `_classify_obj` 实际按符号 auto-guess kind，与 viz_kd
  「显式 kind、未知跳过」**相反**哲学。改 docstring + 模块 docstring 显式说明「display 变换对齐；
  kind 检测相反——tail 按符号 auto-guess，viz 要求显式 kind」。
- **MINOR（鲁棒性）— `status: null` 归一**：原 `str(e.get("status","UNKNOWN")) or "UNKNOWN"` 对
  `status: null` 得字符串 "None"（truthy，不回退）→ progress 图占一个 "None" 类目。改
  `str(e.get("status") or "UNKNOWN")`；补 `test_progress_status_null_normalized_to_unknown` 守门。
- **MINOR（测试）— progress 固定显示序未守**：原 0-保留测试把 data 转 dict 丢顺序。补
  `test_progress_fixed_display_order`（断言 `== ["SUCCESS","FAIL_accuracy","FAIL_train","FAIL_latency","FAIL_export"]`）。
- **MINOR（测试）— 杂项 status 计数>0 正路径未测**：补 `test_progress_extra_status_with_count_retained`
 （造 status="TIMEOUT" 行断言 count=1 保留 + 排在 order 之后）。
- **MINOR（测试）— `variants_total=None` 回退分支未测**：补 `test_progress_variants_total_unknown_when_none`
  （断言 caption 含「variants_total=未知」）。
- **MINOR（测试）— agent.md `--baseline_latency_ms` 存在性未守门**：补
  `test_kd_train_agent_md_passes_baseline_latency_ms`（与 `..._passes_receiver_dir` 对称，防整段 flag 被删）。

**延后**（reviewer 标「可放下一轮」）：MINOR 6 三新图的「数据不足 WARN 跳过」分支（<2 点 / <1 行 / 空 ledger）
未加 smoke 测试；MINOR 8 FAIL_accuracy 真测行入图正路径（predicate 已单元覆盖，viz 层仅间接）。

## 验证
- WSL `.venv`（Python 3.12.13 + pytest 9.1.1）：
  - `tests/workflows/test_struct_kd_p7.py` + `tests/workflows/test_kd_redesign.py` = **149 passed**（含 review 修复新增 7 条）。
  - `tests/workflows` + `tests/compile` + `tests/schema` = **634 passed**（无回归；修复前基线）。
  - kd-nas contract + no-fabrication 子集 = **79 passed**。
- 新增/更新测试：viz 取负显示（scatter+compare）+ snr 不取负 + baseline-as-data-row（含 min 取负）+
  baseline=None 不加行 + latency_bar baseline 端到端（值级）+ latency_bar None 反向 + 进度图固定项 0 保留 +
  进度图固定显示序 + 杂项 status 计数>0 保留 + 杂项 status 0 过滤 + variants_total=未知 + status:null 归一 +
  未知 kind 三图齐跳 + train_pool baseline 透传（给 / 不给两路）+ agent.md baseline flag 守门 +
  is_measured_row 7 边界；更新 `test_new_charts_pushed_and_direction_aware`（pareto_y_direction min→max + 取负数据守门）。

## 涉及文件
- 改动：`workflows/agents/_kd_scripts/{viz_kd,train_pool}.py`、`workflows/agents/kd-train/agent.md`、
  `tests/workflows/{test_struct_kd_p7,test_kd_redesign}.py`。
- 未动（范围外）：model-flatten / kd-train-script / teacher-gen / kd-select 的 SKILL.md/scripts、
  `docs/status/CURRENT.md`、三件套脚本。

## Open Questions
1. **select 推图与 viz_kd 数据表示不同**（读 select_and_report.py 后确认）：kd-select 的
   `select_and_report._push_pareto_chart` 仍用 **raw accuracy + `pareto_y_direction={min|max}`**
   （方向元数据法），而 viz_kd 改为 **取负显示 + `pareto_y_direction='max'`**（取负法）。两者
   **语义等价**（帕累托前沿非支配点集相同），但 y 轴数值表示不同（viz_kd 显示正数 20/22，
   select 显示原值 -20/-22）。任务范围限定「不改 kd-select scripts」，故未同步；若希望两图视觉
   一致（都取负显示），后续可单独评估改 select（注意 select 的 `_best_student` / `_pareto_front`
   内部用 raw+direction，改显示需隔离到推图层）。
2. **未知 kind 是否该让 scatter 放宽**（见上方决策）：scatter 是 latency-accuracy 分布图，bar 高度
   误导弱于 accuracy_compare。本次三图同口径跳过；若 operator 反馈未知 kind 时仍想看分布，
   可把 scatter 单独放宽（保留「方向未知」label），accuracy_compare/pareto 仍跳过。
3. **未 commit**（任务约束）：CHANGELOG 索引不带 commit SHA（占位），待用户决定合入时补。
4. **`kd-train/agent.md:69` 预存文档结构歧义**（code-reviewer MAJOR，未修）：`accuracy_baseline_kind
   已加回 inputs` 放在「**已下沉**」标题下，字面归属冲突（读者可能误以为它也已下沉）。该行是
   finalize 预存文本、非本 session 引入、不在本次列出范围 → 未改（守「只改列出的」纪律）。
   建议后续 doc 整理时拆为两条独立列表项（已下沉：seed / 已加回 inputs：accuracy_baseline_kind）。

## Commit
未 commit（任务约束「不 commit / 不 push」）。CHANGELOG 索引留 SHA 占位。
