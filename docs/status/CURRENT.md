# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：KD-NAS Trainer 引擎化重构——Phase 2 完成（521e8e3），Phase 3 待开工

**任务**：把 `kd-train-script` 单体 codegen 重构为「固定 `KDTrainer` 引擎 + 4 叶子（loss/data/eval/optim）+ run.sh」；产物拍平到 `artifacts/`（去 kd-nas 层）；agent prompt 去 SPEC 源化；补 resume / 早停 / 循环内 evaluator。

**计划**：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（5 Phase，独立可 commit；v3.2 经 spec-reviewer 四轮闭环）。

**状态**：**Phase 2 完成**（commit `7870b2a`，零回归 + code-reviewer 一轮闭环）。**Phase 3 未开工**。

### Phase 2 已交付（commit 7870b2a，16 文件 +1694/-3420）
- `kd-train-script` codegen 切到产 4 叶子 + run_config.yaml + run.sh（删旧单体模板 692 行）
- 5 调用点（train-teacher train+eval / distill train+eval / finalize eval）切固定引擎入口 + inline flag + `--artifacts_dir`
- distill E4：移除 inline `--kd_config`，每轮 AST 决策 read→patch run_config.yaml 的 kd_config 字段
- distill E13/M1：redirect stdout → `runs/r${ROUND}_student/train.log`，experiment=variant_id
- finalize eval champion 三字段强制 inline（student_model_path / build_cfg / student_ckpt）
- `fidelity_check.py` 重写：`--leaves_dir` + AST 自包含（Q6）+ AST 签名相等（E9）+ kind 方向硬校验（D2）
- `train-script-verify` 重写：4 叶子并行 review + 引擎 smoke + workflow-verifier 子 agent
- CONTRACTS §3.1 flag diff 表（M6）+ 调用点 × 字段 × 数据源矩阵（N1）+ §6 叶子契约节 + E7 修正
- gen_train_script output_schema 切：train_pipeline_path 指固定引擎入口 + ADD leaves_dir/run_config_path/run_sh_path

### Phase 3 待办（产物拍平 + logs 折叠 + 原子迁移，同 commit）
- [ ] `kd-setup`：`kd_artifacts_dir` 去 kd-nas 层；删 `logs/` mkdir；**5 步原子迁移 + 全字段 rewrite + 幂等**（§3.4，Q10/N3）。
- [ ] 调用方 redirect stdout → `runs/<exp>/train.log`（Phase 2 已部分落地：distill + train-teacher 已 redirect；Phase 3 补路径字段调整）
- [ ] `teacher_setup.py` / `finalize_kd.py` / `viz_kd_stage.py`：durable 路径调整（finalize_kd 的 durable `--kd_artifacts_dir` 与 Phase 2 的 `--artifacts_dir` 是不同参数，A4 澄清）
- [ ] `kd-setup` output_schema + `kd-nas.yaml` 路径字段同步

### Phase 4 待办（agent prompt 去 SPEC 化，Q15）
- [ ] grep `workflows/agents/**/*.md` + `_kd_scripts/**/*.md`（含 CONTRACTS.md）扫除来源叙事（D7）—— Phase 2 已对 kd-train-script 直接写任务纯净态；Phase 4 扫剩余（distill / train-teacher / verify / CONTRACTS 等仍有 SPEC §x 引用）
- [ ] distill agent prompt 补「ofd fail → 降级 mse-only 重试一次」提示（M8）

### Phase 5 待办（端到端验证）
- [ ] E2E `examples/kd-nas-demo` 全链路 + resume 多时点 smoke + 早停 patience 触发

**必读**：
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§3.4 拍平+迁移 + §5 Phase 3 checklist + §11 v3.2）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§3.1 flag diff + 调用点矩阵 + §6 叶子契约）
- Phase 1 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase1.md`（引擎设计点）
- Phase 2 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase2.md`（接口切换决策点 + 偏离记录）
- 引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`（Phase 1 不变；Phase 2 零改动）
