# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：KD-NAS Trainer 引擎化重构——Phase 1 完成（6929685），Phase 2 待开工

**任务**：把 `kd-train-script` 单体 codegen 重构为「固定 `KDTrainer` 引擎 + 4 叶子（loss/data/eval/optim）+ run.sh」；产物拍平到 `artifacts/`（去 kd-nas 层）；agent prompt 去 SPEC 源化；补 resume / 早停 / 循环内 evaluator。

**计划**：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（5 Phase，独立可 commit；v3.2 经 spec-reviewer 四轮闭环）。

**状态**：**Phase 1 完成**（commit `6929685`，33 单测全绿 + code-reviewer 四轮闭环）。**Phase 2 未开工**。

### Phase 1 已交付（commit 6929685）
- `_kd_scripts/kd/{trainer,_leaves,_resume}.py` NEW（三 mode + Q2 hot-order + 双协议 emit + resume/早停/R1）
- `_kd_scripts/train_pipeline.py` 孤儿入口（仅单测调）
- `kd-train-script/references/templates/leaves/{loss,data,eval,optim}.py.skel`
- `tests/workflows/test_kd_engine_trainer.py`（33 单测）
- 隔离安全：DAG / gen emit / kd-nas.yaml / 旧模板 / kd 库 / CONTRACTS 全未改

### Phase 2 待办（接口原子切换，单 commit）
- [ ] `kd-train-script` SKILL.md / agent.md / workflow doc 重写：产 4 叶子 + run_config.yaml + run.sh + D8 AST 检测。
- [ ] `gen_train_script` output_schema：切 `train_pipeline_path` 指向固定引擎入口 + ADD `leaves_dir`/`run_config_path`/`run_sh_path`（additive）。
- [ ] 5 调用点（train-teacher train+eval / distill train+eval / finalize eval）按 §3.3 矩阵改 inline flag + `--artifacts_dir`；distill read→patch run_config.yaml。
- [ ] `fidelity_check.py`：逐叶子数值等价 + AST 自包含（Q6）+ kind 方向硬校验（D2）。
- [ ] `train-script-verify`：4 叶子并行 review + AST 无残留 + workflow-verifier + kind sanity。
- [ ] grep `references/workflow-checklists/` 更新 `templates/train_pipeline.py` 引用（Q20）。
- [ ] CONTRACTS §3.1 flag diff 表（M6）+ 删 `references/templates/train_pipeline.py`。
- [ ] **Phase 2 开工前先折 v3.2 §11 的 E5-E13/R1/R2 + D-A smoke 对拍**。

### Phase 3-5 见计划 §5。

**必读**：
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§3.3 调用点矩阵 + §5 Phase 2 checklist + §11 v3.2 补充）
- `workflows/agents/_kd_scripts/CONTRACTS.md`
- Phase 1 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase1.md`（引擎设计点 + 偏离记录）
- 新引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`
