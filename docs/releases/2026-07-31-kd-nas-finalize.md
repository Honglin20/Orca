# KD-NAS Finalize（2026-07-31）

> 在嵌入完成（6 节点 DAG，415 passed）基础上补齐「最后一公里」：脚本化最终选择 + 指标方向显式化 +
> 训练监控铁律 + 可视化增强 + 防假 + 修 no-fabrication 误报。逐字实现计划，无伪造兜底路径。

## 做了什么

### 增量 A — kd-select 文件夹 agent（零 LLM 最终选择）
- 新建 `workflows/agents/kd-select/`：`agent.md` + `scripts/select_and_report.py`。
- 脚本读 `ledger.jsonl` → 按 `accuracy_baseline_kind` **显式方向**（`kd_common.accuracy_direction` 单一真相源）挑最优 student（达标项里精度最优）+ 列 latency-accuracy 非支配帕累托前沿 + 模板填空 `final_report.md`（teacher vs students 对比表 + 选择依据 + 前沿列表）+ 推 `chart_type=pareto` 前沿图（sidecar）。
- 嵌入 workflow DAG：`... → train → select → $end`。
- **硬校验 + 防假**：ledger 空/坏 / kind 未知方向 → exit 2 + 报告标注失败；无达标 student → exit 0 + 报告标「无 student 达标」+ `N_SELECTED: 0` + `BEST_VARIANT:` 空串（**绝不**伪造选出）。
- output_schema 强制：`n_selected` / `selection_ok` / `best_variant` / `final_report_path` / `fail_reason`。

### 增量 B — 训练监控铁律（kd-train/agent.md）
- 加「监督铁律」段：`train_pool.py` 同步阻塞（`ThreadPoolExecutor` with 块 join 全 worker）= 结构性 wait，**不许** fire-and-forget；每 worker 的 `train_pipeline.py` 按 epoch 推 loss 实时图（`_make_live_push`）；单 worker 崩不杀整批（落 `FAIL_train`）；`output_schema` 的 `variants_done` 从真 ledger 计数（伪造不出）。

### 增量 C — 可视化增强（viz_kd.py）
- 在原 3 图（sweep scatter / ledger table / latency bar）基础上加 3 图：
  1. **Sweep Progress** —— status 计数（SUCCESS/FAIL_accuracy/FAIL_train/FAIL_latency）+ `n_done/n_total`（新 `--variants_total` arg，train_pool 算后透传）。
  2. **Pareto Front** —— `chart_type=pareto`，latency(min) × accuracy（方向按 kind），前端自绘前沿。
  3. **Accuracy Compare** —— 各变体 accuracy bar + `accuracy_baseline` 参考线，hue=met_accuracy。
- 指标方向在所有图坐标轴体现：`y_label`/`caption` 按 kind 标「越低/越高越好」（`_direction_phrase`）；未知 kind 大声标「方向未知」**不 auto 猜**。

### 增量 D — 指标方向（accuracy_baseline_kind 加回 input + 单一真相源）
- `kd-nas.yaml` 加回 `accuracy_baseline_kind`（[ask] required）：之前瘦身砍掉、下游 auto 检测 → 改回显式 kind 驱动。
- `kd_common.py` 新增 `accuracy_direction(kind)` + `HIGHER_BETTER_KINDS`/`LOWER_BETTER_KINDS`（含 `db`）—— measure_student / viz_kd / select 三处 import 同一函数（DRY，防三处漂移）。
- `measure_student.py` 重构：删本地 `_HIGHER_BETTER`/`_LOWER_BETTER`，改 import；新增 `db`（越低越好）支持。
- `kd-train/agent.md` 透传 `--accuracy_baseline_kind "{{ inputs.accuracy_baseline_kind }}"`。

### 增量 E — 防假（train_pool.py）
- 新增纯函数 `classify_final_sweep(rows, n_accepted, incoming_fail_reason)`：`n_accepted>0` 但 ledger 0 个 `SUCCESS`（全 FAIL_accuracy/FAIL_train）→ `SWEEP_STATUS: FAIL` + 原因（避免「全 FAIL 但 SUCCESS」误导 operator / 下游 select）。
- 同时修正 viz 调用顺序：`variants_total` / `rows` 在 viz 之前算（原本 viz 在前、统计在后，会引用未赋值变量）。

### 增量 F — 修 test_no_fabrication[kd-nas] false positive
- 根因：`check_no_fabrication` 对 `torch.randn` 做上下文感知扫描（合法标记 `smoke/dummy/proxy/materialize/...`），但两处 module-level / 无标记函数被误报。
  - `gpu_probe._probe_per_variant_vram`：docstring 加 dummy/smoke 标记（VRAM 探测造 dummy input batch，合法）。
  - `teacher_model.__main__`：抽成 `_smoke()`（docstring 含 smoke/dummy），`__main__` 调用它。
- **未改 check 逻辑**（保持对真伪造的零容忍），只给合法用途补上下文标记。

## Review-driven 修复（code-reviewer 双轮自检后补）

- **C1（critical，no-fabrication 底线）**：`select_and_report._measured_rows` 原仅过滤 `lat<0 or acc is None`——但 `FAIL_latency` / `FAIL_train` / measure-fail-`FAIL_accuracy` 行的 `accuracy=0` 是**哨兵**（非 None）且 latency 是真测值，会绕过过滤；在 min 方向 kind（nmse/mse/ber/db）下以 `acc=0` 虚假占据帕累托前沿 + 进入 final_report 表格。修法：`kd_common.is_measured_row(row)` 按 `status ∈ {SUCCESS, FAIL_accuracy} ∧ accuracy_kind 非空` 判真测（measure emit `STUDENT_ACCURACY_KIND` 才算真测，真值可能恰为 0.0）；`select._measured_rows` 与 `viz_kd` 所有 accuracy 坐标图（scatter/pareto/accuracy_compare）统一调用。`_best_student` 不受影响（`_qualified_rows` 用 met_accuracy 过滤）。
- **DRY（M2）**：`to_float` 下沉到 `kd_common`，select_and_report / viz_kd 删本地副本，三处同源。
- **H2**：`viz_kd._push_pareto` 未知 kind 时不再用保守 `min` 兜底推图（方向不可靠会误导），改为 stderr WARN + 跳过（与 select fail-loud 同精神）。
- **CONTRACTS.md §5**：修正 latency 哨兵描述（原文称「FAIL_latency 用 -1」与 `gate_all._fail_latency_row` 现实相反——FAIL_latency 用真测 lat；只有 gate-异常 FAIL_train 用 -1）。新增 `accuracy_kind` 字段语义（区分真测 vs 哨兵的权威字段）。
- **测试加固**（+9 条）：C1 回归（FAIL_latency 哨兵不得入 measured/pareto，viz 版同）、空 ledger fail-loud、坏 JSON ledger fail-loud、viz 三新图 + 方向（nmse→min / snr→max）、viz 未知 kind 跳过 pareto、viz 哨兵剔除、measure argv 透传 `--accuracy_baseline_kind`、viz argv 透传 `--variants_total` + `--accuracy_baseline_kind`。

## 偏离计划
- 计划文件 `docs/plans/2026-07-31-kd-nas-finalize.md` 实际不存在（仓库 `docs/plans/` 无此文件）；任务描述本身即是计划，逐字实现。已在本 release note 标注。
- `select` 节点仅在 `gate.n_accepted>0` 路径运行（gate→$end 时 train+select 都跳过）——与 train 同处理（workflow `outputs:` 不模板化 skipped 节点字段）。计划提的「无达标也出报告」在 train 跑过的所有路径成立（select 读 ledger，含 FAIL_accuracy 行）；gate 全 FAIL_latency 直跳 $end 不产 final_report（open question，见下）。

## 验证
- WSL `.venv`（Python 3.12.13 + pytest 9.1.1）：`tests/workflows/test_kd_redesign.py` + `test_struct_kd_p7.py` = **126 passed**；`tests/compile` + `tests/schema` + `tests/workflows` + contract 子集 = **715 passed**。
- `test_no_fabrication[kd-nas]` 转绿（baseline 2 findings → 0）。
- select 脚本级 E2E（planted ledger）：NMSE/SNR/db 三方向 + 空 ledger fail-loud + unknown kind fail-loud + 无达标不伪造 + fail-loud 仍写报告 + **C1（FAIL_latency 哨兵 lat=3+acc=0 不得入 pareto 前沿）**，全过。
- 全部 14 条 contract check（kd-nas）0 findings；`kd-select` 进入 active agent 集合。
- 新增/更新测试（共 +18 条新增 + 2 条旧契约更新）：`accuracy_direction` / `db_kind_lower_better` / `classify_final_sweep_anti_fake` / `_best_student` 方向 + 平局 / `_measured/_qualified` 过滤 / `_pareto_front` 方向感知 / select E2E / 无达标不伪造 / unknown kind fail-loud / **空 ledger fail-loud** / **坏 JSON ledger fail-loud** / **C1 sentinel 排除（select 版）** / **viz 三新图 + 方向** / **viz snr 方向翻转** / **viz 未知 kind 跳过 pareto** / **viz 哨兵剔除** / **measure argv 透传** / **viz argv 透传**；DAG 7 节点 + `accuracy_baseline_kind` 加回。
- 唯一失败：`tests/e2e_phase12/test_opencode_e2e.py::test_opencode_drives_tui_end_to_end` —— 环境性（opencode TUI 150s 超时），与 kd-nas 无关，**未触碰**该路径代码。

## 涉及文件
新增：`workflows/agents/kd-select/{agent.md, scripts/select_and_report.py}`。
改动：`workflows/kd-nas.yaml`、`workflows/agents/_kd_scripts/{kd_common,measure_student,viz_kd,train_pool,gpu_probe,teacher_model}.py`、`workflows/agents/_kd_scripts/CONTRACTS.md`、`workflows/agents/kd-train/agent.md`、`tests/workflows/{test_kd_redesign,test_struct_kd_p7}.py`。

## Open Questions
1. **gate 全 FAIL_latency → $end 不产 final_report**：当所有变体在 gate 阶段就被 latency 筛掉（n_accepted=0），train+select 都跳过，无 `final_report.md`。若需要「即使全 FAIL_latency 也出一份报告」，需把 select 改成 gate 之后无条件运行（改 gate 路由）。本次按计划「train → select」实现，未改 gate 路由；如需覆盖，后续单独评估。
2. **agent.md KEY ↔ script stdout KEY 一致性未做静态契约 check**（reviewer 🟡 #4）：kd-train / kd-select 的 agent.md 用 `grep '^KEY:'` 解析脚本 stdout，两端手写易漂移（一端改名另一端不跟 → bash 解析空串 → JSON 字段空）。当前 schema-chain test 校验 output_schema 字段，但 KEY 名集合一致性是软契约，未自动校验。建议后续加一条参数化静态 check（正则扫 agent.md 的 KEY 集合 A + AST 扫脚本 print KEY 集合 B，断言一致）。
3. **`select_and_report.py` 顶层 import kd_common**（reviewer M4）：若 kd_common 不可导入，`__main__` 的 try/except 拿不到（ImportError 在进 `__main__` 块前 raise）→ stdout 全空 → agent.md 填非法 JSON。**保持与所有 `_kd_scripts` 模块一致的约定**（均在顶层 import kd_common），不为单文件特殊处理——`_KD_SCRIPTS` 路径推导简单（`parents[2]/"_kd_scripts"`），命中概率低。若要鲁棒，应 codebase-wide 统一改（超出本次范围）。
4. **未 commit**（任务约束）：故 CHANGELOG 索引不带 commit SHA（占位 `—`），待用户决定合入时补。
