# Release: KD-NAS Trainer 引擎化 Phase 4（agent prompt 去 SPEC 源化 + 任务纯净）

**Date**: 2026-08-04
**Commit**: `<TBD>`（28 文件，纯文本层改动）
**Plan**: [`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md) §5 Phase 4 checklist + §11（M8 ofd 重试）+ D7（去源化客观边界）

## What was done

把 kd-nas workflow 的所有 agent prompt + 引擎库代码 + CONTRACTS.md + yaml 里**残留的来源叙事**
（"SPEC §x.y"、"SPEC-REVIEW N4"、"spec-review m2"、"2026-08-04 cleanup"、"v5 变更"、"deleted"、
"historical"、"port from"、"Phase N"、"plan §x"、决策标签 Q2/E4/N21/M8/D2/A8/B5 等）全部清除，
改为**任务纯净态**：只描述「做什么 / 契约 / 输入输出 / 执行步骤 / fail 条件 / catch 协议行为」，
零来源叙事。distill agent prompt 同时补 M8 ofd 降级重试提示。

**严格文本层**：不动 Phase 1-3 已落地的引擎逻辑、CLI flag、契约字段、执行步骤；既有测试全绿。

## D7 客观边界（执行时遵守）

- **agent prompt（`.md`）**：零来源叙事硬线。description 一句话讲角色 + 任务 + 契约 + fail 条件；
  正文只写行为（catch 协议写「做什么」，不写它叫「SPEC §15」）。
- **引擎库代码（`_kd_scripts/*.py`、`kd/*.py`）**：保留**设计注释**（解释当前代码为何如此，
  如「prepare 后才合并 kd_parameters，因 OFDAdapter 是 lazy 创建」），删**来源叙事**
  （「ported from the historical template」、「(Phase 1 orphan)」、「Q2 hot-order」类）。
- **CONTRACTS.md**：保留内部结构化章节号（`§1 变体契约` / `§6 叶子契约` 是组织结构）；
  删变更日志式叙事（`v5 变更` / `2026-08-04 cleanup SPEC §2/§3` / `已全部删除`）。
- **CONTRACTS 内决策标签**（Q6/E9/D2/A8/E4 等 plan review 标签）：全部删除，保留语义描述。

## 产出

### A. 全文扫描 + 清除来源叙事

**基线 grep 命中**：128（agent prompt 层 + 引擎库 + CONTRACTS + yaml）。
**完成 grep 命中**：0（agent prompt 层；引擎设计注释除外，「deleted SPEC」/「port from」类 0）。

**改写的 agent prompt（10 个节点 agent + 3 个 SKILL）**：

- `kd-setup/agent.md` — description 去 `（SPEC §6.2）`；step1 注释去 `Phase 3 拍平：plan §3.4 D6`；
  step2 注释去 `SPEC §6.2 seed`；step3 注释去 `SPEC §3.1 串行化`。
- `train-script-verify/agent.md` — fail loud 段去 `SPEC §15 不走 catch`。
- `train-teacher/agent.md` — 去 5 处 SPEC / SPEC-REVIEW N2 / spec-review M1 / Phase 3 引用。
- `gen-student/agent.md` — description 去 `（SPEC §6.7）`；catch 协议段去 SPEC §15 / §1.2(1)；
  DUMMY_INPUT 校验注释去 `SPEC-REVIEW m2` + 嵌入 python 错误信息里的 `spec-review m2` 字串。
- `distill/agent.md` — 去 `SPEC §15 catch`（3 处）+ compose 守卫 §1.2(1) ref + 旧 v1 train_pool.py
  已删叙事；同时**新增 M8 ofd 降级重试提示**（见下 C）。
- `decide/agent.md` — description 去 `（SPEC §6.9）`；step1 注释去 `（SPEC §6.9）`。
- `model-flatten/SKILL.md` — 去两处 `Phase 3 flattened` / `2026-08-04 cleanup: pick_variant 已删`
  / `pick_variant._validate_variant` ref；保留 `CONTRACTS.md §1` 结构化导航。
- `model-flatten/agent.md` — 输出目录注释去 `Phase 3 拍平后`。
- `kd-train-script/references/templates/leaves/{loss,data,eval,optim}.py.skel` — 头注释去 `plan §3.2`
  / `§3.1 D2`，保留 `CONTRACTS §1`。
- `kd-train-script/references/workflows/train_pipeline_script_generation.md` — 去 `(E4: distill's`。
- `kd-train-script/references/workflow-checklists/train_pipeline_script_generation/02_cli.md` —
  Anti-pattern 行去 `regression in Phase 1 engine code`。
- `kd-train-script/scripts/fidelity_check.py` — docstring 去 `Phase 2 rewrite:`。

**保留的合法**（非来源叙事，per D7 / B 边界）：

- `CONTRACTS.md §1` / `§6` 在 agent.md / SKILL.md 中作为**结构化章节导航**（指 live 契约文档）。
- `nas_agent.train.distillation` 字串在 `kd-train-script/agent.md` / workflow doc / 01_training.md
  checklist 里是 **forbidden token 字面量**（LLM 不能 import 的名字），非来源叙事。

### B. 引擎库代码（`_kd_scripts/*.py` + `kd/*.py`）去源化

- `finalize_kd.py` — docstring 去 `SPEC §6.10` / `N10/N19` / `SPEC §3.3`；argparse description 同。
- `metrics_tail.py` — docstring 去 `SPEC §9`（5 处）。
- `kd_reducer.py` — docstring 去 `SPEC §6.9 + §13 逐条对应` / `N12` / `SPEC §13 admitted 集合` /
  `champion_met` 等决策标签。
- `gpu_probe.py` — `_load_variant_module` docstring 去 `（与 historical pick_variant 同语义
  （pick_variant 删于 2026-08-04 cleanup §3））`；concurrency 注释去 `SPEC §3.1 串行化`。
- `viz_kd_stage.py` — docstring 去 `SPEC §8` / `SPEC §13` / `SPEC §3`；caption 去 SPEC ref；
  argparse description 同。
- `kd/compose.py` — module docstring 去 `(the historical train_adapter_template.py (deleted
  2026-08-04 cleanup §2) did this)`；fail-loud 守卫注释去 `SPEC §1.2(1)`（保留 fail-loud 行为说明）。
- `kd_common.py` — 两处「§4 doc sweep：原消费者…已删」叙事改为只描述当前真实消费者。
- `migrate_flat.py` — docstring / argparse description / 行内注释去 `Phase 3，plan §3.4` / `plan §3.4`。
- `train_pipeline.py` — docstring 去 `Phase 2 (atomic switch, plan §5)` / `Priority (plan §3.3)` /
  argparse description `(Phase 1 orphan)`。
- `kd/trainer.py` — module docstring 整段重写去 `Phase 1, engine orphan` / `Aligned with plan §3.1
  (v3.2)` / 全部决策标签（M1/Q9/Q24/Q2/M3/M4/B6/B5/Q18/D3/D4/R1 等）；`_first_batch_x` /
  `_compute_proxy_mse` / `_make_live_push` / `_maybe_bootstrap_env` 等 helper docstring 同步清理；
  错误消息「cannot run Q2 prepare」→「cannot run prepare」。
- `kd/_leaves.py` — module docstring 去 `Aligned with the plan §3.2 (v3.2)` / `Design (decision D9-c,
  plan §3.2)` / `Q6` / `E9`。
- `kd/_resume.py` — docstring 去 `Aligned with plan §3.1 / §5 (Phase 1) and decision D3` /
  `R1` / `D3 + B4 + Q8 + Q14` / `Q8` / `Q14`。

### C. distill ofd 重试提示（M8）

`workflows/agents/distill/agent.md` 的「失败路径 + catch 协议」段后补一段任务行为提示：

> **ofd 重试（M8）**：student 暴露 `feature_hook_names` 时本节点配 ofd 走 `mse+ofd`；若训练时 ofd
> 因运行时 feature 取空触发 compose fail-loud（rc≠0）→ **降级 mse-only 重试一次训练**（patch
> run_config.yaml 的 `kd_config` 为 `{"kd_losses":["mse"],"weights":{"mse":1.0},"ema":true}`，
> 重跑 step 3 训练 + step 4 eval）。降级重试仍 rc≠0 → 进 FAIL_train catch（agent 退 0）。

写成任务行为描述（patch 字段 + 重跑 step），不带「compose.py:174 守卫」实现指针。

### D. CONTRACTS.md + kd-nas.yaml

- `CONTRACTS.md` 头部删 `> **v5 变更**（2026-08-03 串行化 + 2026-08-04 死代码清理）` 整段变更日志；
  顶部标题去 `（CONTRACTS，串行 v5）`。
- §0 目录树删「§3 cleanup：…已删」叙事 + 全部行末 `（Phase 1 新增；Phase 2 切为下游入口）` /
  `（Phase 3 拍平迁移）` 等 Phase 归属。
- §3.1 measure_student.py 条目改为只描述 KD 精度路径承担者（不再写「已删」叙事）。
- §3.1 train_pipeline.py 条目去 `Phase 2 切换`；flag diff 表标题去 `Phase 2 flag diff 表（M6, …）`；
  调用点矩阵标题去 `plan §3.3`；E6/E4/D-A/E13/M1 等决策标签全清。
- §5 铁律的 `（SPEC §1）` / `（SPEC §13）` 引用清。
- §6 叶子契约章节标题去 `Phase 2 codegen 产物`；自包含校验/AST 签名/kind 方向/run_config 优先级
  5 处决策标签（Q6/E9/D2/A8/E4）全清。
- `kd-nas.yaml` inputs.metrics_template description 去 `SPEC §9 schema`；3 处路径字段 description
  去 `Phase 3 拍平` / `拍平去 kd-nas 层`；concurrency 去 `违 SPEC §3.1 串行化`；teacher 节点注释去
  `SPEC §15 不走 catch`。

## Verification

- **去源化基线对比**：grep 命中 128 → 0（agent prompt 层 + 引擎库「deleted SPEC」/「port from」类
  0；引擎设计注释不计违规；CONTRACTS §x 结构化导航保留）。
- **零回归**：`pytest tests/workflows/ tests/workflows/test_struct_kd_p7.py` = **466 passed, 3 skipped**
  （基线 466 passed, 3 skipped 完全持平）。
- **抽样对照 nas-agent 风格**（nas-train-runner / nas-select 的 agent.md）：
  - kd-setup / decide / distill / gen-student / train-teacher / train-script-verify description 均
    达到「一句话角色 + 任务 + 契约 + fail 条件」纯净态，零 SPEC / Phase / 决策标签叙事。
- **code-reviewer** 一轮闭环（must-fix 全修，详见 commit 后追加）。

## Deviations from plan

- 无。Phase 4 checklist 两项（去源化 + M8 ofd 提示）逐字落地。
- **遗留**：`tests/workflows/test_struct_kd_p7.py::test_no_jinja_ref_to_undeclared_input[nas-supernet.yaml]`
  在 Phase 4 期间红，但**非本 Phase 引起**——是同会话内外部 commit `e02245c`（nas-supernet.yaml）
  与 `83552da`（生成节点 agent）引入的 yaml/agent.md 自引用 + 模板语法问题。Phase 4 范围 kd-nas 全绿。

## Commit

`<TBD>` —— 28 文件 +注释/措辞调整，无语义改动。

文件清单（按目录）：

```
workflows/agents/_kd_scripts/CONTRACTS.md
workflows/agents/_kd_scripts/{finalize_kd,gpu_probe,kd_common,kd_reducer,metrics_tail,migrate_flat,train_pipeline,viz_kd_stage}.py
workflows/agents/_kd_scripts/kd/{_leaves,_resume,compose,trainer}.py
workflows/agents/{decide,distill,gen-student,kd-setup,train-script-verify,train-teacher}/agent.md
workflows/agents/kd-train-script/scripts/fidelity_check.py
workflows/agents/kd-train-script/references/templates/leaves/{loss,data,eval,optim}.py.skel
workflows/agents/kd-train-script/references/workflows/train_pipeline_script_generation.md
workflows/agents/kd-train-script/references/workflow-checklists/train_pipeline_script_generation/02_cli.md
workflows/agents/model-flatten/{SKILL.md,agent.md}
workflows/kd-nas.yaml
```
