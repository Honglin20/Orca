# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：KD-NAS Trainer 引擎化重构——Phase 5 纯净度清扫完成，Phase 5 E2E 待开工

**任务**：把 `kd-train-script` 单体 codegen 重构为「固定 `KDTrainer` 引擎 + 4 叶子（loss/data/eval/optim）+ run.sh」；产物拍平到 `artifacts/`（去 kd-nas 层）；agent prompt 去 SPEC 源化；补 resume / 早停 / 循环内 evaluator。

**计划**：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（5 Phase，独立可 commit；v3.2 经 spec-reviewer 四轮闭环）。

**状态**：**Phase 5 纯净度清扫完成**（commit `<待填>`，零回归 + code-reviewer 一轮闭环：0 must-fix / 1 nice-to-have 已在 release note 显式说明）。

### Phase 5 已交付（任务纯净度清扫 + 守门测试强化）
- agent prompt 决策标签清扫：5 agent.md（kd-setup / kd-train-script / gen-student / train-script-verify / distill）+ eval.py.skel + kd-nas.yaml 4 处注释；description 历史叙事纯化（删「已拆到 / 不再 import / 合并…为一节点」）。
- 引擎 .py 决策标签清扫：migrate_flat.py ~15 处 + trainer.py / _resume.py / kd_reducer.py / finalize_kd.py，删尾部过程 ID（D8/M3/N12/R1...）+ `code-reviewer Rx` 归属，保留设计 why 注释。
- CONTRACTS.md + yaml 迁移叙事清扫（删「旧…现…」对照 + 「随骨架化移除」+ stale `--kd_config recipe 必传` 改为 `read→patch run_config.yaml`）。
- 守门测试 deny-list 分层强化：agent prompt 层加非括号决策标签 + 历史叙事词锁；.py 仅锁括号 + 复合源叙事（D7 边界）；E402 noqa 双重免疫。
- 零逻辑 / 契约 / CLI / 字段改动；468 passed, 3 skipped（零回归）。

### Phase 5 E2E 待办（端到端验证）
- [ ] E2E `examples/kd-nas-demo` 全链路 + resume 多时点 smoke + 早停 patience 触发 + 拍平迁移 smoke（旧 `kd-nas/` 真迁）

**必读**：
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§5 Phase 5 checklist + §11 v3.2）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§3.1 flag diff + 调用点矩阵 + §6 叶子契约 + migrate_flat CLI）
- Phase 1-5 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase{1,2,3,4,5}.md`
- 引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`
