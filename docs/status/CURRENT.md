# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：KD-NAS Trainer 引擎化重构——Phase 4 完成，Phase 5 待开工

**任务**：把 `kd-train-script` 单体 codegen 重构为「固定 `KDTrainer` 引擎 + 4 叶子（loss/data/eval/optim）+ run.sh」；产物拍平到 `artifacts/`（去 kd-nas 层）；agent prompt 去 SPEC 源化；补 resume / 早停 / 循环内 evaluator。

**计划**：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（5 Phase，独立可 commit；v3.2 经 spec-reviewer 四轮闭环）。

**状态**：**Phase 4 完成**（commit `<TBD>`，零回归 + code-reviewer 两轮闭环：第一轮 10 must-fix + 9 nice-to-have 全修；自加反回归测试 `test_kd_prompt_no_source_narrative.py`）。

### Phase 4 已交付（agent prompt 去 SPEC 源化 + 任务纯净）
- grep 范围：`workflows/agents/{model-flatten,teacher-gen,kd-setup,kd-train-script,train-script-verify,train-teacher,gen-student,distill,decide}/` + `_kd_scripts/**`（含 CONTRACTS.md）+ `workflows/kd-nas.yaml`。
- 基线 128 命中 → 完成后 agent prompt 层 0 命中（引擎设计注释除外；CONTRACTS §N 结构化导航保留）。
- D7 客观边界执行：agent.md 零源叙事硬线；引擎 .py 留设计注释删源叙事（deleted/historical/Phase N/Q2-N21-M8-D2-E6 等决策标签全清）；CONTRACTS.md 保留 §N 章节号删 changelog 叙事。
- distill agent.md 补 M8 ofd 重试提示（trigger + mse-only recipe + 重跑 step 3+4 + 降级失败进 FAIL_train catch）。
- 33 文件改 + 1 新反回归测试 `tests/workflows/test_kd_prompt_no_source_narrative.py`（deny-list grep）。
- 测试：467 passed, 3 skipped（含新加 1 个）；deselect 1 个 pre-existing `nas-supernet.yaml` 解析失败（外部 commit 引入，与 Phase 4 无关）。

### Phase 5 待办（端到端验证）
- [ ] E2E `examples/kd-nas-demo` 全链路 + resume 多时点 smoke + 早停 patience 触发 + 拍平迁移 smoke（旧 `kd-nas/` 真迁）

**必读**：
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§5 Phase 5 checklist + §11 v3.2）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§3.1 flag diff + 调用点矩阵 + §6 叶子契约 + migrate_flat CLI）
- Phase 1-4 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase{1,2,3,4}.md`
- 引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`
