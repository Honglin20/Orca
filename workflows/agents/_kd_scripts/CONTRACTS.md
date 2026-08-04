# kd-nas workflow — 接口契约（CONTRACTS）

> KD-NAS = flatten 任意模型入口成 KD 变体契约 + teacher-gen 纯调参派生 teacher + train-script-gen 生成统一
> 训练脚本 + **串行迭代**蒸馏 sweep（gen-student→distill→decide 循环，每轮单 student）+ finalize 选择。
> **活跃 DAG**（串行）：``flatten → setup → gen_teacher → gen_train_script → train_script_verify →
> train_teacher → (gen_student → distill → decide)* → finalize``。
> 改接口 = 改本文件 + 通知依赖方。
> fail loud：脚本遇契约不符输入直接非零退出 + stderr 报因（硬件缺失/探测异常则 fail-soft 退 0，不阻塞 workflow）。

## 0. 目录布局

```
workflows/
  kd-nas.yaml                              # workflow DAG（活跃串行：flatten→setup→gen_teacher→...→finalize）
  agents/
    model-flatten/                         # 入口 agent：展平任意模型入口成 KD 变体契约
      agent.md / SKILL.md / scripts/{validate_contract,measure_latency}.py
    teacher-gen/                           # 纯调参派生 teacher（深度×3/宽度×2，wrapper 委托 baseline）
      agent.md / SKILL.md / scripts/{validate_teacher,measure_latency}.py
    kd-train-script/                       # 产 4 叶子（user/{loss,data,eval,optim}.py）+ run_config.yaml + run.sh（人类用）
      agent.md / SKILL.md / references/{templates/leaves/*.py.skel, workflows/, workflow-checklists/} / scripts/fidelity_check.py
    kd-setup/agent.md                      # 幂等 setup：teacher 训 + teacher_setup + 预检 + GPU 预检
    gen-student/agent.md                   # 串行：每轮派生 student model.py（首轮固定规则 / 迭代轮 KB+perf）
    distill/agent.md                       # 串行：单 student KD 蒸馏（tune_latency→distill→eval）
    decide/agent.md                        # 串行：champion ratchet + 终止判定 + ledger 落账
    finalize/agent.md                      # 终态：champion eval/ONNX/latency + final_report.md
    _kd_scripts/
      CONTRACTS.md                         # 本文件
      kd_common.py                         # 共享 helper（sha256/provider_id/read_ledger/is_variant_done/acquire_run_lock/RANK/accuracy_direction/is_measured_row）
      _device.py                           # resolve_device / ort_providers / is_npu_available（cuda:local_rank 支持）
      tune_latency.py                      # 最小缩量 latency 调参（seed/cache/median+std）
      gpu_probe.py                         # GPU 探测 + 并发判定（setup 阶段，fail-soft）
      teacher_setup.py                     # teacher_cache.pt + teacher_meta.json
      viz_kd_stage.py                      # 活跃串行每节点 web 推送 sidecar（baseline_seed/teacher/student/distill_table/decide/final；flatten 不推图）
      metrics_tail.py                      # distill loss line（log-tail 推送）
      finalize_kd.py                       # finalize 确定性后端（champion eval/ONNX/latency + final_report.md）
      migrate_flat.py                      # durable artifacts 拍平迁移（旧 artifacts/kd-nas/ → artifacts/；5 步原子 + sentinel 幂等）
      kd/{losses,wrapper,compose,ema}.py   # KD 库（不变）
      kd/{trainer,_leaves,_resume}.py      # 固定训练引擎 + 叶子加载器 + 原子 resume
knowledge_base/families/receiver/          # model8 变体仓（.py）+ _model8_blocks.py 共享积木
<project>/artifacts/                       # ★ 跨 run 稳定 artifact 根（去 kd-nas 层）
```

## 1. 变体 I/O 契约（每个 receiver/*.py 必须暴露）

```python
DUMMY_INPUT = {"shape": [1,4,48,64,1], "dtype": "float32"}   # 用户真实 I/O 维度（禁硬编码回退）
BUILD_FN = "build_model"
KNOBS = {                                                      # 可调旋钮；step<0, leverage∈{high,medium,low}
    "num_blocks": {"default":3,"min":1,"step":-1,"leverage":"high"},
    "embed_dim":  {"default":16,"min":8,"step":-4,"leverage":"medium"},
}
def build_model(**cfg) -> nn.Module: ...                       # 零参用 KNOBS.default；cfg 覆盖旋钮
def feature_hook_names(self) -> list[str]: ...                 # 可选，OFD/FitNets/RKD 特征对齐（缺则 distill 自动 mse-only）
```
- **I/O**：输入 `[B,num_ports,num_subcarriers,num_symbols,1]`，输出同形；内部自理 alpha 归一。
- **文件名 = variant_id**（stem）；`_*.py` 是共享模块（KNOBS 校验由 ``kd_common.validate_variant`` 持）。
- **teacher 不在此**：活跃 teacher 由 `teacher-gen` 节点派生（wrapper .py 委托 baseline），不入 KB。
- **feature_hook_names fail-loud**：distill 用 AST 判定此 fn 是否存在 → 启用/剥离 ofd；
  若 student 缺此 fn 但下游强配 ofd → ``kd/compose.py`` 守卫 fail-loud 抛 ValueError → FAIL_train。

## 2. ledger.jsonl 行 schema（跨 run 真相源，append-only）

```json
{"variant_id":"r1_student","student_path":"…","round":1,"parent":"baseline",
 "accepted_cfg":{...},"cfg_hash":"<sha16>","variant_sha256":"…",
 "status":"SUCCESS","latency_us_median":7.3,"latency_us_std":0.1,
 "accuracy":0.021,"accuracy_kind":"nmse","met_latency":true,"met_accuracy":true,
 "ckpt":"…/r1_student.pt","target_latency_us":8.0,
 "accuracy_baseline":0.02,"latency_provider_id":"path::func|sha16",
 "run_id":"kd-nas-…","fail_reason":"","hypothesis":"…","direction_id":"…"}
```

**字段**：
- `latency_us_median`：哨兵语义按 status 分——
  - **FAIL_latency**：**真测值**（tune_latency 即便 FAIL 也 emit 测得的 latency，只是超 target）。
  - **FAIL_train / measure-fail FAIL_accuracy**：latency 取自 tune（真测值），`accuracy=0` 哨兵。
- `accuracy_kind`：measure rc==0 时非空（真测，即便数值恰为 0.0 或 db-kind -1.0）；measure 失败 / 未进训练池
  时为空串（此时 ``accuracy=0`` 是哨兵）。是区分真测 vs 哨兵的权威字段。
  下游消费者 viz_kd_stage（``_push_pareto_front``）按 ``kd_common.is_measured_row`` 判「是否真测了 accuracy」
  （finalize_kd 用本地 status 字面判定，不 import 此处）
  （status ∈ {SUCCESS, FAIL_accuracy} 且 ``accuracy_kind`` 非空）——哨兵行不计入帕累托前沿。

**status 全部取值**：`SUCCESS` / `FAIL_latency`（tune 不过）/ `FAIL_train`（训练/eval 崩）/
`FAIL_build`（validate_contract 3 strikes）/ `FAIL_accuracy`（训练完但精度未达）/ `FAIL_export`（ONNX 导出失败）。

## 3. 确定性脚本 CLI（stdout `KEY: value` 供 agent 解析；非零退出 = fail loud）

### 3.1 活跃脚本（活跃串行 kd-nas.yaml 直接调用）

- **tune_latency.py**（distill 节点内部）：`--variant_path --build_fn --dummy_input --knobs --target_latency_us --latency_provider --artifacts_dir [--max_measurements 40] [--measure_repeats 3] [--device auto] [--seed 0] [--opset 17]`
  → `TUNE_STATUS: ACCEPTED|FAIL_latency` + `ACCEPTED_CFG`/`BEST_EFFORT_CFG` + `LATENCY_US_MEDIAN` + `LATENCY_US_STD` + `MEASUREMENTS`。
- **gpu_probe.py**【setup step 3】：`[--teacher_cache <.pt>] --representative_variant <.py> --variants_count [--device auto] [--safety 0.8] [--max_concurrency 8] [--seed 0]`（teacher_cache 可选：提供→VRAM 模式测 per-variant 占用算并发；不提供→device-only 模式 concurrency=1，串行 setup teacher 未训时用）
  → `RESOLVED_DEVICE` + `N_GPUS` + `FREE_VRAM_BYTES` + `PER_VARIANT_VRAM_BYTES` + `CONCURRENCY` + `DEVICE_PLAN`（JSON list）+ `GPU_REPORT`。
  fail-soft：无 CUDA/NPU / 探测异常 → `CONCURRENCY: 1` + `DEVICE_PLAN: [""]` + WARN，exit 0；仅输入契约不符 → exit 2。
- **measure_student.py（KD 精度路径）**：KD 精度由 train_pipeline.py --mode eval 承担；
  ``kd_common.parse_accuracy`` / ``kd_common.compute_met_accuracy_absolute`` 是绝对基线对比的不变量入口
  （当前仅 ``test_struct_kd_p7::TestMeasureStudentAbsoluteBaseline`` 调，生产路径 distill/finalize 直接读
  train_pipeline --mode eval 的 MET_ACCURACY——保留作 contract 单点测试入口）。
- **teacher_setup.py**：`--teacher_model_path --teacher_ckpt --build_fn --dummy_input [--eval_command] --output_dir [--latency_provider] [--teacher_latency_us] [--device] [--seed]`
  → `TEACHER_LATENCY_US` + `TEACHER_ACCURACY` + `TEACHER_ACCURACY_KNOWN` + `TEACHER_DB_BASELINE` + `TEACHER_ONNX` + `TEACHER_CACHE` + `TEACHER_META`（meta 含 `teacher_model_hash` + `teacher_ckpt_sha256`）。
- **viz_kd_stage.py**（活跃每节点 web 推送 sidecar）：`--stage <baseline_seed|teacher|student|distill_table|decide|final> [--ledger] [--champions] [--baseline_latency_us] [--baseline_accuracy] [--target_latency_us] [--accuracy_baseline_kind] [--teacher_latency_us] [--champion_latency_us] [--champion_accuracy] [--teacher_meta] [--round_hypothesis] [--env_anchor]`（flatten 不调本脚本——不推图，viz_status 固定 `env_status:skipped`）
  → stdout JSON：`{viz_env_status, charts: {<图名>: {pushed, reason}}}`。
  - `--stage final` 推：``final_compare_bar``（baseline/teacher/champion latency bar）+ ``champion_summary_table``
    + ``all_models_table``（baseline+teacher+students+champions 全模型总表）+ **``pareto_front``**（latency×accuracy
    非支配前沿，port viz_kd 语义；方向门 + display 变换 + sentinel 过滤经 ``kd_common``）+ **``fail_status_bar``**
    （status 计数 bar）。
  - 单图异常不影响其他图（sidecar 不阻断）；env_missing / import_failed 仍 emit 合法 JSON。
- **metrics_tail.py**（distill 节点 loss line sidecar）：`--template --source_log --variant_id --mode [--env_anchor]`。
- **finalize_kd.py**【finalize 确定性后端】：`--ledger --champions --champion_id --terminate_reason --baseline_contract_path --train_pipeline_path --baseline_latency_us --baseline_accuracy --teacher_latency_us --target_latency_us --accuracy_baseline --accuracy_baseline_kind --kd_artifacts_dir --struct_scripts_dir --kd_scripts_dir --device --seed --latency_provider --project_root --per_run_artifacts_dir [--teacher_meta]`
  → `CHAMPION_IS_BASELINE` + `CHAMPION_STUDENT` + `CHAMPION_CKPT` + `FINAL_LATENCY_US` + `FINAL_ACCURACY` + `FINAL_ONNX` + `FINAL_REPORT`。
  champion=baseline 兜底：跳 student eval/ONNX/latency 用 setup 透传值。final_report.md 含
  「All Architectures」总表 + 「Search Outcome」status 计数（文本，图表是唯一真相源）+ 「各轮 student 汇总」。
- **train_pipeline.py**【固定引擎入口；train_teacher 调 --mode teacher + distill 调 --mode distill/eval + finalize 调 --mode eval】：`--mode teacher|distill|eval --artifacts_dir <per-run> [--config run_config.yaml] [--experiment <variant_id>] [--resume <latest.pt>] [--early_stop_patience N] [--out_ckpt（teacher/distill）] [--model_path（teacher）] [--student_model_path（distill/eval）] [--teacher_cache（distill）] [--student_ckpt（eval）] [--build_fn] [--build_cfg] [--kd_config（distill；distill 走 yaml，不传 inline）] [--accuracy_baseline（eval）] [--accuracy_baseline_kind（eval）] [--epochs] [--lr] [--batch_size] [--eval_every] [--device] [--seed] [--variant_id] [--project_root] [--env_anchor]`
  → teacher：`TEACHER_CKPT` + `TASK_LOSS_FINAL`；distill：`STUDENT_CKPT` + `KD_LOSS_FINAL` + `KD_PROXY_MSE`；
  eval：`STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `ACCURACY_CONFIDENCE`。
  runtime 需 `ORCA_KD_SCRIPTS_DIR` env 指向 `_kd_scripts/`。
  叶子位于 ``<artifacts_dir>/user/{loss,data,eval,optim}.py``（kd-train-script codegen 产）。

  **★ flag diff 表（相对单体 train_pipeline.py）**：

  | flag | 状态 | 说明 |
  |---|---|---|
  | `--artifacts_dir` | **新增 required** | per-run 叶子目录（leaves 在 `<dir>/user/`；引擎注入恒存在） |
  | `--config` | **新增 optional** | `run_config.yaml` 路径；CLI flag > yaml > 引擎默认 |
  | `--experiment` | **新增 optional** | experiment id（= variant_id）；驱动 `runs/<exp>/` 子目录 |
  | `--resume` | **新增 optional** | 从 `latest.pt` 续训（原子 tmp+replace 写） |
  | `--early_stop_patience` | **新增 optional** | patience 轮无改进 break（0=禁用） |
  | `--eval_every` | 保留 | mid-train eval cadence（默认 1） |
  | `--mode` / `--out_ckpt` / `--build_fn` / `--build_cfg` / `--kd_config` / `--model_path` / `--student_model_path` / `--teacher_cache` / `--student_ckpt` / `--accuracy_baseline` / `--accuracy_baseline_kind` / `--epochs` / `--lr` / `--batch_size` / `--device` / `--seed` / `--variant_id` / `--project_root` / `--env_anchor` | 保留 | 语义不变（CLI > yaml > 默认） |
  | 单体 inline ``user_*`` slot / `--user_*` flag | **已移除** | 用户逻辑经 kd-train-script codegen 产 4 叶子承载 |

  **★ 调用点 × 字段 × 数据源矩阵**（5 调用点）：

  | 调用点 | mode | student_model_path | build_cfg | kd_config | epochs/lr | ckpt | accuracy_baseline |
  |---|---|---|---|---|---|---|---|
  | train-teacher train | teacher | n/a（用 `--model_path`=teacher wrapper） | `{}` inline | n/a | inline（gen 提取的 teacher_default_lr/epochs） | inline `--out_ckpt` | n/a |
  | train-teacher eval（teacher_setup --eval_command shell 字符串嵌套） | eval | `--student_model_path`=teacher wrapper inline | `{}` inline | n/a | n/a | `--student_ckpt`=teacher_ckpt inline | inline |
  | distill train | distill | inline（每轮 student 不同） | inline（=accepted_cfg） | **yaml**（AST 决策；禁用 inline `--kd_config`） | inline（`--epochs`=inputs.full_epochs；run_config.yaml 的 epochs 由 gen_train_script 写入但 inline 优先） | inline `--out_ckpt` | n/a |
  | distill eval | eval | inline | inline（=accepted_cfg） | n/a | n/a | inline `--student_ckpt`=本轮 ckpt | inline |
  | finalize eval（champion） | eval | **inline**（champion 真相源） | **inline** | n/a | n/a | **inline** `--student_ckpt`=champion ckpt | inline |

  所有调用点额外加 `--artifacts_dir {{ setup.output.per_run_artifacts_dir }}`（叶子定位 = workflow-run-scope 共享）。
  **distill redirect 片段**：`mkdir -p "$PER_RUN/runs/$EXP" && python3 ... > "$PER_RUN/runs/$EXP/train.log" 2>&1`；experiment=variant_id；`metrics_tail --source_log` 指此。
- **export_onnx.py**（共享）：`--model_path --build_fn --dummy_input --opset --out --device --seed [--build_cfg]`。
- **migrate_flat.py**【durable artifacts 拍平迁移，kd-setup step1 检测旧 kd-nas/ 存在时调】：
  `--kd_old <abs artifacts/kd-nas> --flat_new <abs artifacts/> [--dry-run]`
  → `ACTION: migrated|sentinel_present_rmtree_old|dry_run` + 行数对账（`LEDGER_COUNTS` /
  `CHAMPIONS_COUNTS` / `TEACHER_META_MIGRATED`）+ `MIGRATION_DONE: 1`（或 `DRY_RUN: 1`）。
  5 步原子：copy checkpoints/meta/models/onnx/reports + 根 jsonl（``dirs_exist_ok=True``
  覆盖语义）→ rewrite 路径字段（``Path.relative_to(kd_old) → flat_new / rel``，禁裸 string replace）→
  行数校验 → ``os.replace`` 逐文件 → sentinel ``.migration_done``（manifest 含 sha256）→ rmtree 旧 kd-nas/。
  全字段 rewrite 清单：``ledger.{ckpt,student_path}`` + ``champions.{snapshot}`` + ``teacher_meta.{teacher_onnx,teacher_cache,teacher_ckpt}``；
  禁 rewrite ``teacher_meta.teacher_model_path``（per-run scope，不在 kd-nas 子树）。
  幂等：sentinel 缺 → 从 copy 重跑；sentinel 在 → 校验 flat 文件存在 → 直接 rmtree 旧。
  ``tune_cache.json`` 不迁移（latency 缓存路径键失效，删旧重建）。

## 4. 节点 I/O（活跃串行 DAG）

| 节点 | 关键输出 |
|---|---|
| flatten | baseline_contract_path / project_root / model_name / flat_artifacts_dir / baseline_latency_us / viz_status（展平任意模型入口成 KD 变体契约 .py；`__main__` 跑「正确性 + latency」统一契约 → baseline_latency_us 由 inputs.latency_provider 实测） |
| setup | kd_artifacts_dir / per_run_artifacts_dir / project_root / kd_scripts_dir / struct_scripts_dir / ledger_path / champions_path / checkpoints_dir / student_models_dir / scripts_dir / onnx_dir / meta_dir / reports_dir / worktree_root / device / concurrency / baseline_latency_us / baseline_accuracy / viz_status |
| gen_teacher | teacher_model_path / teacher_latency_us / project_root / depth_axis / width_axis / viz_status |
| gen_train_script | train_pipeline_path（固定引擎入口）/ leaves_dir / run_config_path / run_sh_path / teacher_default_lr / teacher_default_epochs |
| train_script_verify | verified / issues |
| train_teacher | teacher_cache / teacher_meta / teacher_ckpt / teacher_latency_us / teacher_accuracy / teacher_accuracy_known / viz_status |
| gen_student | student_model_path / round / hypothesis / direction_id / knobs / status（OK|FAIL_build） / viz_status |
| distill | round / student_model_path / accepted_cfg / cfg_hash / latency_us / latency_us_std / accuracy / met_latency / met_accuracy / ckpt / tune_status / status（SUCCESS|FAIL_latency|FAIL_train|FAIL_build） / viz_status |
| decide | round / continue_loop / champion_id / champion_latency_us / champion_accuracy / viz_status（+ terminate_reason） |
| finalize | final_model / final_onnx / final_latency_us / final_accuracy / final_report / viz_status |

**路由**（纯函数 router 求值，无 LLM）：
- flatten → setup（恒定）。
- setup → gen_teacher（恒定）。
- gen_teacher → gen_train_script（恒定）。
- gen_train_script → train_script_verify（恒定）。
- train_script_verify → train_teacher（恒定）。
- train_teacher → gen_student（恒定，进入循环）。
- gen_student → distill（恒定）。
- distill → decide（恒定）。
- decide：``terminate == true`` → finalize；否则 → gen_student（下一轮）。
- finalize → ``$end``（恒定）。
- **串行迭代**：每轮单 student（gen_student→distill→decide），decide 据进度决定继续/终止；无并发池。

## 5. 铁律

- **dummy_input 用户指定**：禁硬编码 shape 回退（BLK-4）。
- **latency 必用用户脚本**：`latency_provider` 必填无默认（BLK-3/10，编译期 validator 强制）。
- **确定性路由**：`terminate` / `tune_status` 由确定性脚本算，agent 不自定（LO-5）。
- **跨 run 复用**：稳定 `kd_artifacts_dir` + 哈希校验 + ledger-driven 跳过；单写者（BLK-13 orca.lock）。
- **时延测量必串行**：latency 对 contention 敏感（并发测→读数失真→false FAIL_latency）。
- **绝不伪造**：latency / accuracy 必须真实测量；无任何 fallback 造假路径。finalize 无达标 → 报告标「无 student 达标」（champion 维持 baseline），**不**假装选出。
- **指标方向显式 + 单一真相源**：``accuracy_baseline_kind`` 是必填 [ask] input。方向判定统一走 ``kd_common.accuracy_direction``
  （viz_kd_stage._push_pareto_front + kd_common.compute_met_accuracy_absolute 经此函数判方向；finalize_kd 用本地 kind 字面，不 import 此处），**禁**符号 auto 猜（防「-20dB 误判优于 -22dB」反向错误）。
  未知 kind → fail loud / 低置信 + met=false，绝不静默 pass。
- **特征蒸馏 fail-loud**：``kd/compose.py`` 守卫——kd_losses 含 ofd/fitnets/rkd 且运行时 feats 空 → raise ValueError → FAIL_train。
  distill agent 默认 KD_CONFIG 已 AST 条件化（按 student.feature_hook_names 存在决定启 ofd 还是 mse-only），无 hook 时自动剥离特征项不崩。

## 6. 叶子契约（kd-train-script codegen 产物）

> 叶子 = LLM 唯一产物（~30 行/个），落 per-run ``$ORCA_ARTIFACTS_DIR/user/``。
> 引擎入口 ``_kd_scripts/train_pipeline.py`` 经 ``kd/_leaves.load(<artifacts_dir>/user)`` 加载（不注入 sys.path）。

**4 个叶子 + 必填 callable 签名**：

| 叶子 | callable | 签名 | 返回 |
|---|---|---|---|
| ``loss.py`` | ``compute_loss`` | ``(s_out, y)`` | ``Tensor``（标量） |
| ``data.py`` | ``build_dataloader`` | ``(batch_size)`` | re-iterable 对象，每 ``iter()`` yield ``(x, y)`` |
| ``eval.py`` | ``eval_metric`` | ``(student, device)`` | ``(value, kind)``，kind ∈ {nmse, mse, ber, db, snr, acc} |
| ``optim.py`` | ``build_optimizer`` | ``(params, lr)`` | ``Optimizer | None``（None → 引擎 fallback Adam） |
| ``optim.py`` | ``build_scheduler`` | ``(optimizer, epochs)`` | ``LRScheduler | None`` |

**自包含校验（引擎 loader + fidelity_check 双重执行）**：

- 禁 sibling / 相对 import（``ImportFrom.level > 0`` → FAIL）。
- 顶层的 ``import`` / ``from import`` 仅允许白名单 {``torch``, ``math``, ``numpy``,
  ``typing``, ``itertools``, ``functools``, ``collections``, ``dataclasses``, ``random``}。
- 常量 / helper 必须内联同文件（不允许跨 ``user/*.py`` 文件互相 import）。

**AST 签名相等**：函数名相等 + 必填位置参数集相等（``compute_loss`` 必须有 ``s_out, y``；
``build_optimizer`` 必须有 ``params, lr``；等）。默认参数 additive（可加新 optional kwargs，不可删 / 改名 required）。

**kind 方向硬校验**：leaf kind 方向组（``{snr, acc}``=max / ``{mse, nmse, ber, db}``=min）
必须与 ``inputs.accuracy_baseline_kind`` 方向组一致，否则 fail loud（不静默 WARN）。

**run_config.yaml**（gen_train_script 产 teacher 模板；distill 每轮 read→patch ``kd_config``）：

```yaml
epochs: <user_default>
lr: <user_default>
batch_size: 4
eval_every: 1
early_stop_patience: 0
accuracy_baseline: <from inputs>
accuracy_baseline_kind: <from inputs>
build_cfg: {}    # teacher default；distill 走 inline flag，yaml 此字段对 distill 无效
# 注意：mode 不写 yaml（mode 由 --mode flag 唯一决定）
```

**优先级**：``CLI --flag`` > ``run_config.yaml`` > 引擎默认。**distill 走 yaml 唯一 kd_config 真相源**（移除 inline ``--kd_config``）。
