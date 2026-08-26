# Release: KD-NAS Trainer 引擎化 Phase 3（产物拍平 + logs 折叠 + 原子迁移）

**Date**: 2026-08-04
**Commit**: `b2ce694`（12 文件，+1043/-27）
**Plan**: [`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md) §3.4 + §5 Phase 3 checklist + §11 v3.2

## What was done

把 durable artifacts 从 `${PROJECT_ROOT}/artifacts/kd-nas/` 拍平到 `${PROJECT_ROOT}/artifacts/`
（去 kd-nas 层），删除 `logs/` 顶层目录（M1：日志走 per-run `runs/<exp>/train.log`），
新增 **5 步原子迁移脚本**（migrate_flat.py）+ 全字段 rewrite + sentinel 幂等。

**Phase 3 严格单 commit 原子**（A-E 同 commit）：拍平根 + 删 logs + 迁移脚本 + 下游路径同步
+ CONTRACTS/yaml 同步。

### 5 项产出

1. **NEW `workflows/agents/_kd_scripts/migrate_flat.py`** —— 5 步原子拍平迁移（plan §3.4）：
   - **copy**（`dirs_exist_ok=True` 覆盖语义）旧 `checkpoints/` / `meta/` / `models/` / `onnx/` /
     `reports/` + 根 jsonl → flat_new；`meta/tune_cache.json` 不迁移（R2）。
   - **rewrite 路径字段** → `.new` 文件，`Path.relative_to(kd_old) → flat_new / rel`（禁裸
     string replace，E3）：`ledger.{ckpt, student_path}` + `champions.{snapshot}` +
     `teacher_meta.{teacher_onnx, teacher_cache, teacher_ckpt}`；**禁 rewrite**
     `teacher_meta.teacher_model_path`（per-run scope，不在 kd-nas 子树，A4/E2）。
   - **行数校验**：新 ledger / champions 行数 == 旧。
   - **os.replace** 原子替换（逐文件 `.new` → 正名）。
   - **sentinel `.migration_done`**（manifest 含文件清单 + 行数 + sha256）作最后一步原子 touch。
   - **rmtree** 旧 `kd-nas/` 子树（sentinel 成功后）。
   - **幂等（E1）**：sentinel 缺 → 从 copy 重跑（覆盖语义读未动的 kd_old 原始）；
     sentinel 在 → 校验 flat 文件存在 + **数据安全契约**（kd_old 内容行数与 manifest 一致 →
     rmtree；不一致 → fail loud 拒绝，防「用户后续写入的新数据被静默 rmtree」）。
   - **`--dry-run`**：只报告（copy 哪些 + 行数 + rewrite 路径数），不动文件系统。
   - 零 `kd_common` 依赖（独立模块）。

2. **`kd-setup/agent.md`**：`kd_artifacts_dir` 改 `${PROJECT_ROOT}/artifacts/`（去 kd-nas 层）；
   删 `logs/` mkdir + `onnx/tune`（仅留浅 `onnx/`）；检测旧 `artifacts/kd-nas/` 存在时调
   migrate_flat.py。

3. **`model-flatten/agent.md` + `SKILL.md`**：`OUTPUT_DIR` 同步改 `${PROJECT_ROOT}/artifacts/
   models/baseline/`（与 setup 拍平后根同根合流）。**code-reviewer R1 闭环**：SKILL.md 漏改
   已同步修（agent.md 改了 SKILL.md 漏改是跨文件 drift 典型漏网——LLM 实际跑 SKILL.md 工作流，
   若残留旧 kd-nas/ 路径会与 setup 已拍平的 flat 路径不同根）。

4. **logs 折叠（M1）**：
   - `distill/agent.md`：删 `DISTILL_LOG` alias 变量；metrics_tail 直读 `$PER_RUN/runs/$EXP/
     train.log`。
   - `train-teacher/agent.md`：删 `meta/teacher_train.log` 兼容 cp；metrics_tail 直读
     `setup.output.per_run_artifacts_dir/runs/teacher/train.log`。

5. **同步更新**：
   - `teacher_setup.py`：修预存 mkdir subdirs bug（checkpoints/onnx/meta 在 production 由
     kd-setup mkdir，但测试 / 直调场景无 mkdir → fail；与 Phase 2 finalize_kd 同款 fix）。
   - `CONTRACTS.md` §0 目录树（去 kd-nas 层）+ §3.1 加 migrate_flat.py CLI 全字段清单。
   - `kd-nas.yaml`：setup output_schema 路径字段描述 + flatten flat_artifacts_dir 描述。

### 测试

- **NEW `tests/workflows/test_migrate_flat.py`（16 单测）**：happy path / 全字段 rewrite /
  行数保持 / sentinel 两分支（sentinel 在 + sentinel 缺）/ dry-run / 坏 JSON fail loud /
  teacher_meta 缺失分支 / **sentinel 数据安全契约**（kd_old 内容变 → 拒绝 rmtree）/
  relative_to 算法守护（防 `kd-nas-artifacts` 同前缀误伤）/ CLI 子进程 smoke。
- **同步修 3 个预存失败测试**（`test_struct_kd_p7.py::test_kd_setup_node_exposes_path_fields`
  `ckpts_dir` → `checkpoints_dir` + `test_model_flatten.py::test_flatten_agent_md_output_dir_co_rooted_with_setup`
  路径字段同步 + 扩扫 SKILL.md）。

## Verification

- **零回归**：`test_kd_engine_trainer.py`（33）+ `test_kd_train_script.py`（19）+ `test_finalize_kd.py`
  （13）+ `test_kd_redesign.py` + `test_kd_reducer.py` + `test_viz_kd_stage_metrics_tail.py` +
  `test_struct_kd_p7.py` + `test_model_flatten.py` + `test_migrate_flat.py`（16）+ `test_teacher_gen.py`
  —— 共 **281 passed, 3 skipped, 0 failed**。
- **`tars validate workflows/kd-nas.yaml` PASS**（schema 不变，只描述/路径值更新）。
- **code-reviewer 一轮闭环**：5 项 must-fix 全修——
  1. 🔴 `model-flatten/SKILL.md:33,35` 残留旧 kd-nas 路径（跨文件 drift）→ 修 + 测试扩扫 SKILL.md；
  2. 🟡 sentinel_present 分支无条件 rmtree 有数据丢失风险 → 加行数校验 + fail loud 拒绝；
  3. 🟡 silent skip 必需子目录/jsonl 违反 fail loud → 加 stderr WARN（checkpoints/meta/models +
     ledger/champions 缺失可观测）；
  4. 🟡 测试缺坏 JSON fail loud 用例 → 加 `test_migrate_rejects_corrupt_ledger_jsonl`；
  5. 🟡 测试缺 teacher_meta 缺失分支 → 加 `test_migrate_without_teacher_meta`。
- **§9.2c 迁移 smoke**：dry-run 报差异 + 真跑成功 + 多时点幂等（sentinel 两分支覆盖）。
- **Phase 1 引擎零改动**：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py` 未改。
- **Phase 2 codegen/调用点不动**：5 调用点逻辑零改动（仅路径根字段值更新，经 Jinja 透传）。

## Deviations from plan

- **sentinel 数据安全契约加严**：plan §3.4 prose 写「sentinel 在 → 校验 flat 文件存在 → 直接
  rmtree 旧」，但 code-reviewer R3 指出此 prose 有 hole（kd_old 复活含新数据 → 静默 rmtree 丢数据）。
  实现加行数校验：sentinel 在 + kd_old 内容行数与 manifest 一致 → rmtree；不一致 → fail loud 拒绝。
  这是「实现照 spec 走但 spec 有 hole」→ 实现补强 + spec 默认采纳更严契约（surface conflict +
  pick safer + explain why，Rule 7）。
- **silent skip 必需文件加 WARN**：plan §3.4 未明说必需文件缺失的处理；code-reviewer R3 指出
  silent skip 违反 fail loud（is_variant_done 突然返 False 无信号）。实现加 stderr WARN，不 raise
  （旧实例真空非契约违反，但须可观测）。
- **teacher_setup.py mkdir subdirs**：Phase 3 范围外（独立预存 bug），但 3 个预存失败测试中 2 个
  根因在此。code-reviewer 同款 Phase 2 precedent（finalize_kd 加 kd/reports + kd/onnx mkdir 修预存
  bug），此处一并 surgical fix（fail loud 不靠运气）。

## Next (Phase 4)

agent prompt 去 SPEC 化（Q15/D7）：grep `workflows/agents/**/*.md` + `_kd_scripts/**/*.md`（含
CONTRACTS.md）扫除来源叙事；distill agent 补「ofd fail → 降级 mse-only 重试一次」提示（M8）。
Phase 2 已对 kd-train-script 直接写任务纯净态；Phase 4 扫剩余（distill / train-teacher / verify /
CONTRACTS 等仍有 SPEC §x 引用）。
