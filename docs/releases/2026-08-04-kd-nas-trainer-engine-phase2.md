# Release: KD-NAS Trainer 引擎化 Phase 2（接口原子切换——叶子化 codegen + 5 调用点）

**Date**: 2026-08-04
**Commit**: `521e8e3`（commit message 受并行 session 干扰成 "nas-supernet MNIST fixture"；实际包含本 Phase 2 全部改动 + 2 个无关的 nas-supernet 改动 `bfe857f`/`2d9b5ff` 的 docs。`git show 521e8e3 --stat` 可见 18 个文件、-3246/+1700 行属于本 phase）
**Plan**: [`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md) §5 Phase 2

## What was done

把 `kd-train-script` codegen 从产单体 `train_pipeline.py`（5 inline `user_*` slot）切到产 **4 叶子**
（`user/{loss,data,eval,optim}.py`）+ `run_config.yaml` + `run.sh`（人类用）。下游 5 调用点
（train-teacher train+eval / distill train+eval / finalize eval）按 §3.3 矩阵原子切到固定引擎入口
（`_kd_scripts/train_pipeline.py`）+ inline flag + `--artifacts_dir` per-run。

**Phase 2 严格单 commit 原子**（Q1/N4 铁律）：A-F 同 commit，中间态不开。

### 6 项产出

1. **`workflows/kd-nas.yaml` gen_train_script output_schema**：`train_pipeline_path` 切指固定引擎入口
   （`_kd_scripts/train_pipeline.py`）+ ADD additive 字段 `leaves_dir` / `run_config_path` / `run_sh_path`
   + 保留 `teacher_default_lr` / `teacher_default_epochs`。下游所有 Jinja 引用
   （`{{ gen_train_script.output.train_pipeline_path }}`）零改动。

2. **5 调用点切到 inline flag + `--artifacts_dir`**（按 §3.3 矩阵原子同改）：
   - **train-teacher train**：`--mode teacher --artifacts_dir $PER_RUN --experiment teacher --model_path …
     --out_ckpt …` + redirect stdout → `runs/teacher/train.log`（软拷贝到旧 `meta/teacher_train.log` 兼容 metrics_tail）。
   - **train-teacher eval**（经 teacher_setup `--eval_command` shell 字符串嵌套，E6）：`--mode eval
     --student_model_path … --student_ckpt … --artifacts_dir … --experiment …`（eval read-only，**不传 `--out_ckpt`**）。
   - **distill train**：AST 决策 kd_config → **read→patch `run_config.yaml` 的 `kd_config` 字段**（E4：移除
     inline `--kd_config`；唯一真相源 = yaml）+ inline flag + redirect stdout → `runs/r${ROUND}_student/train.log`
     （E13/M1）+ experiment = variant_id。
   - **distill eval**：inline flag（student_model_path / build_cfg / student_ckpt 全 inline）。
   - **finalize eval（champion）**：`finalize_kd._run_eval` champion 三字段强制 inline（student_model_path /
     build_cfg / student_ckpt）+ `ORCA_KD_SCRIPTS_DIR` env 注入；**矩阵第 5 行硬约束**（yaml 可能被末轮 distill 覆盖）。

3. **`kd-train-script` codegen 重写**（产叶子，不再产单体）：
   - `SKILL.md` / `agent.md` / `references/workflows/train_pipeline_script_generation.md` 全部重写为叶子化契约。
   - 加 **D8 AST 检测**（gen_train_script 读用户 `train.py` 时扫 GAN/RL/DDP token，命中 fail loud +
     `--force-template` override）。
   - 任务纯净形态：prompt 只描述「要做什么」（端口契约 / 输入输出 / fail 条件），零 SPEC 来源叙事。
   - 4 个叶子骨架（`references/templates/leaves/*.py.skel`，Phase 1 已建）作实例化起点。
   - 删 `references/templates/train_pipeline.py`（旧单体，692 行）。
   - 2 个 checklist 重写为叶子契约（AST 自包含 + AST 签名 + kind 方向硬校验）。

4. **`fidelity_check.py` 重写**（叶子模式）：
   - `--leaves_dir` 模式（保留旧 `--train_pipeline` 作被忽略的 back-compat 形参）。
   - **AST 自包含**（Q6：白名单 {torch,math,numpy,typing,itertools,functools,collections,dataclasses,random}；
     禁 sibling / 相对 import）—— 与 `kd/_leaves.py::_check_self_contained` 镜像。
   - **AST 签名相等**（E9：函数名 + 必填位置参数集；默认参数 additive）—— 与 `_leaves._check_signature` 镜像。
   - **kind 方向硬校验**（D2：leaf kind 方向组 {snr,acc}=max / {mse,nmse,ber,db}=min 必须与
     `--accuracy_baseline_kind` 方向组一致，否则 fail loud）。
   - 数值等价（loss / loader / eval / optimizer / model I/O）保留。

5. **`train-script-verify` 重写**：4 叶子并行 review sub-agent + fidelity_check + 引擎 smoke
   （合成 model+ckpt 跑 `--mode teacher` 1 epoch + `--mode eval`）+ workflow-verifier 子 agent。
   verified=false → fail loud 阻塞（不进 train_teacher）。

6. **`CONTRACTS.md` 更新**：
   - §0 目录加 `kd/{trainer,_leaves,_resume}.py` + kd-train-script 改为产叶子描述。
   - §3.1 line 121 「`train_teacher 调 --mode teacher + distill 调 --mode distill/eval + finalize 调 --mode eval`」（E7：
     原为「`setup 调`」错位）。
   - §3.1 flag diff 表（M6：保留 / 新增 `--config` / `--artifacts_dir` / `--experiment` / `--resume` /
     `--early_stop_patience` / 删除单体 inline slot + `--user_*` flag）。
   - §3.1 调用点 × 字段 × 数据源矩阵（N1：5 行，含 epochs/lr 列澄清）+ distill redirect 片段（E13/M1）。
   - §6 新章——叶子契约（4 叶子 + 必填 callable 签名 + 自包含校验 + AST 签名相等 + kind 方向硬校验 +
     run_config.yaml schema）。

## Verification

- **零回归**：`test_kd_engine_trainer.py`（33 单测，Phase 1 引擎）+ `test_kd_train_script.py`（19 单测，
  重写为叶子契约）+ `test_finalize_kd.py`（13 单测，含 Phase 2 eval 契约 + 修 2 个预存 mkdir bug +
  修 sys.path 污染）+ `test_kd_redesign.py` / `test_kd_reducer.py` / `test_viz_kd_stage_metrics_tail.py`
  —— 共 **164 passed, 3 pre-existing failures**（`test_struct_kd_p7` 中 ckpts_dir 字段名 +
  latency_source，经 `git stash` 验证与 Phase 1 同源预存）。
- **code-reviewer 一轮闭环**：1 must-fix（train-teacher `--out_ckpt` 泄漏到 eval_command）+ 1 决策
  （distill `--epochs` inline vs yaml 矩阵澄清 → 更新矩阵）+ 2 optional cleanup（其中一个采纳另一个保留）
  全修；eval read-only 契约在 finalize_kd / train-teacher / distill / matrix / test 五处字面一致。
- **Q2 §9.2b smoke**：minimal leaves + 引擎 `--mode teacher` 1 epoch + `--mode eval` 端到端跑通
  （合成 teacher model + ckpt + 4 minimal leaves）；D-A 拓扑对拍（gen_train_script leaves_dir =
  `$ORCA_ARTIFACTS_DIR/user/` == distill 读的 per_run_artifacts_dir 字面相等）。
- **Phase 1 引擎零改动**：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py` 未改
  （`git diff 6929685..521e8e3 -- workflows/agents/_kd_scripts/kd/` 空）。

## Deviations from plan

- **distill `--epochs` inline**：矩阵原文写「epochs/lr = yaml」，实现走 inline `--epochs {{ inputs.full_epochs }}`
  （CLI > yaml 优先级仍成立；full_epochs 是 workflow-scope input 而非 per-variant）。决策：保留 inline
  （每轮 full_epochs 是 workflow input，写 yaml 多此一举），同步更新 CONTRACTS 矩阵第 3 行 epochs/lr
  列为「inline」消除文档/实现不一致（surface conflict + pick one + explain why，Rule 7）。
- **train-teacher eval `--out_ckpt` 移除**：code-reviewer 发现 train-teacher eval_command 仍带
  `--out_ckpt '$TEACHER_CKPT'`（旧单体时代 eval 也写 ckpt 的残留）。Phase 2 eval 是 read-only（矩阵第 2
  行 + finalize_kd + test 三处一致），移除此 flag 消除契约不一致。
- **commit message 错位**：`521e8e3` 的 message 受并行 session 干扰成「nas-supernet MNIST fixture」，
  实际包含本 Phase 2 全部 18 个文件改动 + 2 个无关 docs（`bfe857f` / `2d9b5ff` 的索引）。`git show --stat`
  可鉴别；后续 Phase 3+ 不受影响（树状态正确）。
- **预存 test setup bug 顺手修**：`test_finalize_kd.py` 两个预存失败（缺 `kd/reports/` + `kd/onnx/` mkdir）
  和一个 sys.path 污染（synthetic `train_pipeline.py` 在 tmp_path 被 `from train_pipeline import` 误抓）
  —— 顺手修了（rename `fake_pipeline.py`），把 suite 拉到全绿（除预存 test_struct_kd_p7）。

## Next (Phase 3)

产物拍平 + logs 折叠 + 原子迁移（同 commit）：`kd-setup` 去 kd-nas 层 + 删 `logs/` mkdir + 5 步原子迁移
+ 全字段 rewrite + sentinel 幂等（§3.4）；调用方 redirect stdout → `runs/<exp>/train.log`（已部分落地，
Phase 3 补 kd-setup 路径调整 + teacher_setup / finalize_kd / viz_kd_stage durable 路径字段）；
`teacher_setup.py` / `finalize_kd.py` / `viz_kd_stage.py` durable 路径调整。
