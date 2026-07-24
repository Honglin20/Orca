# P9a：quant(4)+NAS(2) workflow input 精简 + 切 `$ORCA_ARTIFACTS_DIR`

> Phase 9-a。commit `64c5c11`。SPEC：[`docs/specs/workflow-input-design-principle.md`](../specs/workflow-input-design-principle.md) §5。P8 接口：[`2026-07-22-p8-engine-artifacts-dir-and-gc.md`](2026-07-22-p8-engine-artifacts-dir-and-gc.md)。

## 背景

[Input 三档原则 SPEC](../specs/workflow-input-design-principle.md) 把 workflow inputs 分三档：Tier A（意图/KPI/硬件/种子，[ask] 必填）、Tier B（代码事实，setup 节点 infer-once + output_schema 向后传，缺失走 P4b ask-user 哨兵）、Tier C（工程默认/算法开关，固化不当 input）。P5/P6/P4b 已把 device/seed、loader 哨兵、NAS project_root 下沉等铺好；P8 给了引擎注入的 `$ORCA_ARTIFACTS_DIR` 接口。P9a 负责 quant(4)+NAS(2) 的 input 收敛 + output_dir 切换。

## input 收敛（SPEC §5 目标达成）

| workflow | 收敛前 | 收敛后 | 保留（Tier A） |
|---|---|---|---|
| quant-ptq-sweep | 12 | **3** | model_path / target_hardware / seed |
| quant-sensitivity | 11 | **3** | model_path / target_hardware / seed |
| quant-qat | 15 | **3** | model_path / target_hardware / seed |
| quant-bit-curve | 16 | **6** | model_path / target_hardware / seed / accuracy_tolerance / avg_bit_budget / max_evals |
| nas-agent-pipeline | 6 | **5** | model_path / target_hardware / latency_constraint / max_rounds / seed |
| nas-hp-search | 6 | **5** | 同上 |

## 下沉清单（移出 yaml inputs）

- **Tier C 固化脚本 argparse 默认**：`mode` / `bit_width(s)` / `recipes` / `scheme` / `cage` / `bake` / `method` / `ratio` / `low_bits` / `high_bits` / `candidate_format_space` / `bit_objective` / `granularity`——agent.md bash 不再透传，改脚本默认即改全局。
- **Tier B infer-once + 哨兵**：`project_root`（agent 从 model_path 向上走找 `train.py/pyproject.toml/.git`，对齐 NAS P6 model_optimizer）+ `calib/train/eval loader refs` + `eval_fn_ref`（P4b 哨兵段保留：读代码→找不到返回 `_orca_ask_user_v1` 哨兵→fail loud，绝不造假）。
- **Tier B best-effort + smoke 兜底（显式裁决策，见下「裁决策」）**：qat `lr` / `total_steps`——agent 读用户 train.py/config，找到传真值，找不到传空→脚本 smoke 兜底 + stderr WARN。
- **Tier C 引擎注入**：`output_dir` → `$ORCA_ARTIFACTS_DIR`（P8 接口；env 缺则 fallback 旧 `llm_artifacts/<model>/<wf>/` 保兼容）。

## output_dir 单一真相源

- **quant（4 单 agent workflow）**：agent.md 第 1 步 `echo "$ORCA_ARTIFACTS_DIR"` 取值（run scope 权威产物目录）→ `<output_dir>`（adapter.py 写入处 + `--output_dir` 同源）；脚本仍 `Path(args.output_dir).resolve() + mkdir`。
- **NAS（entry infer-once + propagate）**：entry 节点 `model_optimizer` 从 `$ORCA_ARTIFACTS_DIR` 定 output_dir 写入 output_schema（沿用 P6 既有字段）；下游 `nas-train-runner` / `nas-select` 改读 `{{ model_optimizer.output.output_dir }}`（两 NAS workflow entry 同名 model_optimizer；两 agent 经核查仅用于这 2 workflow，无跨 workflow stale 引用）。

## 清理 P5 遗留 dead required 参数

4 quant 脚本删 `--project_root` / `--calib_data_ref` / `--eval_data_ref`（qat 另删 `--train_data_ref`）/ `--eval_fn_ref`——这些是 P5 时 `required=True` 但脚本 body 从不消费的 dead 参数（loader/eval_fn 逻辑全在 adapter 模块）。P5 code-reviewer 当时登记「留给 P9 input slim 同期清理」，本任务收口。

## 裁决策（Rule 7 surface，选一条说明 why）

1. **qat lr/total_steps 走 smoke 兜底，不走哨兵**。SPEC §5 写「lr/total_steps→[infer]」，SPEC §1 Tier-B 协议是「读代码→哨兵→fail loud」。这里选**读代码→（找不到）smoke 兜底 + WARN**，跳过哨兵层。Why：QAT 的 `total_steps` 是「fake-quant 后短训恢复」步数（诊断步），与用户 train.py 里的**全量训练 epochs 是两回事**；用户的 lr/epochs 并非 QAT 恢复超参的合适来源，强制哨兵问「QAT 恢复 lr=?」会 over-ask（用户多半答不出 QAT 专用值）。故降级 smoke 兜底——但**绝不静默**（SPEC §0 + Rule 12）：脚本兜底时 stderr 打 WARN「smoke 不是生产精度」（`run_qat.py` 兑现），用户可见、可覆盖。
2. **SPEC §5 表 nas 目标 4→5**。原表写 4 是算术笔误（描述「补 4 项 + 下沉 2 项」= 5，非 4）。已订正 SPEC 表 + nas 两行「现」列回填 P6 后的 6。

## 验证

- `tars validate` 6 yaml → 0 error。
- Jinja2 StrictUndefined 渲染全 16 agent 节点（mock context：精确新 input keys + 宽容上游 output mock）→ 0 failures。harness：临时 `/tmp/verify_render.py`（负测试确认能抓 `{{ inputs.removed }}`）。
- `python -m py_compile` 4 脚本 → OK。
- `python -m pytest tests/workflows/ tests/compile/ tests/schema/test_workflow.py` → **250 passed**（含新增 `tests/workflows/test_p9a_input_contract.py` 12 契约用例：6 workflow 各 2 条——input 集合逐字等于 SPEC §5 + 已下沉 Tier B/C 项不回潮）。
- 既有测试无回归。

## code-reviewer 闭环

一轮 review（impl + 验证充分性并轨）：**0 🔴** + **2 🟡** + **4 🟢**，全处理：
- 🟡#1 qat agent.md 承诺 smoke WARN 但脚本静默兜底 → `run_qat.py` 补 stderr WARN（lr/total_steps 兜底可见）。
- 🟡#2 qat lr/total_steps 跳过哨兵 → 上文裁决策 #1 留痕（agent.md「已下沉」段显式标注「走 smoke 兜底不走哨兵 + why」）。
- 🟢#1 SPEC §5 nas 目标 4→5 笔误 → 已订正。
- 🟢#2 缺 input 契约回归测试 → 新增 `test_p9a_input_contract.py`。
- 🟢#3 output_dir env 解析在 agent prompt（非脚本，Rule 5）→ 保留现状（可辩护：脚本 env-agnostic + 与 NAS P6 一致 + fallback 需模型名推断 + `--output_dir required=True` 兜底）。
- 🟢#4 工作树卫生（index.html / nas-agent / 非 P9a）→ 显式 `git add` 20 文件规避；预存 `orca/skills/tars/SKILL.md` 含禁词「compile」（`test_entry_skill_md_has_no_business_logic_keywords` 失败）与本任务无关（该文件 `git diff HEAD` 为空，禁词在 committed HEAD），登记后续小修。

## 范围外（不动）

- struct/kd 文件（P9b）、create-workflow-skill（P9c）、`orca/` 引擎代码。
- agent-struct-exploration / kd-nas 两 workflow 的 input 精简（struct/kd 系，P9b）。
- `tars/SKILL.md` 预存禁词 bug（另开小修）。
