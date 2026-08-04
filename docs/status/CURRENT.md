# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：KD-NAS Trainer 引擎化重构——Phase 3 完成（b2ce694），Phase 4 待开工

**任务**：把 `kd-train-script` 单体 codegen 重构为「固定 `KDTrainer` 引擎 + 4 叶子（loss/data/eval/optim）+ run.sh」；产物拍平到 `artifacts/`（去 kd-nas 层）；agent prompt 去 SPEC 源化；补 resume / 早停 / 循环内 evaluator。

**计划**：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（5 Phase，独立可 commit；v3.2 经 spec-reviewer 四轮闭环）。

**状态**：**Phase 3 完成**（commit `b2ce694`，零回归 + code-reviewer 一轮闭环 5 must-fix 全修）。**Phase 4 未开工**。

### Phase 3 已交付（commit b2ce694，12 文件 +1043/-27）
- NEW `migrate_flat.py`：5 步原子拍平迁移（copy → rewrite `relative_to` 全字段 → 行数校验 → os.replace → sentinel → rmtree）+ sentinel 幂等（数据安全契约：sentinel 在但 kd_old 内容变 → fail loud 拒绝 rmtree）+ `--dry-run`。全字段 rewrite：`ledger.{ckpt,student_path}` + `champions.{snapshot}` + `teacher_meta.{teacher_onnx,teacher_cache,teacher_ckpt}`；`teacher_model_path` 禁 rewrite（per-run）。零 `kd_common` 依赖。
- `kd-setup`：`kd_artifacts_dir` 去 kd-nas 层 + 删 `logs/` + `onnx/tune` + 集成 migrate_flat 自动迁移。
- `model-flatten` agent.md + SKILL.md：OUTPUT_DIR 同步拍平（R1 闭环：SKILL.md 漏改已补 + 测试扩扫）。
- logs 折叠（M1）：distill 删 `DISTILL_LOG` alias；train-teacher 删 `meta/teacher_train.log` 兼容 cp；metrics_tail 直读 per-run `runs/<exp>/train.log`。
- `teacher_setup.py` 修预存 mkdir subdirs bug（checkpoints/onnx/meta 兜底）。
- CONTRACTS §0 目录树 + §3.1 migrate_flat CLI 全字段清单；yaml setup output_schema + flatten flat_artifacts_dir 描述同步。
- 16 新单测 + 3 预存失败测试同步修（ckpts_dir → checkpoints_dir + flatten 路径字段 + SKILL.md 扩扫）。

### Phase 4 待办（agent prompt 去 SPEC 化，Q15/D7）
- [ ] grep `workflows/agents/**/*.md` + `_kd_scripts/**/*.md`（含 CONTRACTS.md）扫除来源叙事—— Phase 2/3 已对 kd-train-script / model-flatten 直接写任务纯净态；Phase 4 扫剩余（distill / train-teacher / verify / CONTRACTS 等仍有 SPEC §x 引用）
- [ ] distill agent prompt 补「ofd fail → 降级 mse-only 重试一次」提示（M8）

### Phase 5 待办（端到端验证）
- [ ] E2E `examples/kd-nas-demo` 全链路 + resume 多时点 smoke + 早停 patience 触发 + 拍平迁移 smoke（旧 `kd-nas/` 真迁）

**必读**：
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§3.4 拍平+迁移 + §5 Phase 4 checklist + §11 v3.2）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§3.1 flag diff + 调用点矩阵 + §6 叶子契约 + §3.1 migrate_flat CLI）
- Phase 1/2/3 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase{1,2,3}.md`
- 引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`（Phase 1 不变；Phase 2/3 零改动）
