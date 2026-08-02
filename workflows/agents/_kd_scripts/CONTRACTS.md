# kd-nas workflow — 接口契约（CONTRACTS，重构版 v4）

> KD-NAS = flatten 任意模型入口成 KD 变体契约 + teacher-gen 纯调参派生 teacher + train-script-gen 生成统一
> 训练脚本 + 确定性蒸馏 sweep（receiver KB 的 model8 `.py` 变体）+ select 脚本化最终选择。
> DAG：`flatten → teacher-gen → train-script-gen → setup → gate → train → select → $end`。
> 改接口 = 改本文件 + 通知依赖方。
> fail loud：脚本遇契约不符输入直接非零退出 + stderr 报因（硬件缺失/探测异常则 fail-soft 退 0，不阻塞 workflow）。

## 0. 目录布局

```
workflows/
  kd-nas.yaml                              # workflow DAG（flatten → teacher-gen → train-script-gen → setup → gate → train → select → $end）
  agents/
    model-flatten/                         # 入口 agent：展平任意模型入口成 KD 变体契约
      agent.md                             # folder-agent 入口（强执行指令 + output schema 前置）
      SKILL.md                             # 6 步工作流（展平 + KNOBS 识别 + 校验迭代）
      scripts/validate_contract.py         # 契约硬校验（fail loud，exit 0=PASS / exit 2=FAIL）
      scripts/measure_latency.py           # 契约默认 cfg latency 测量（自包含；__main__ + flatten agent 复用）
    teacher-gen/                           # 【v4 嵌入】纯调参派生 teacher（深度×3/宽度×2，wrapper 委托 baseline）
      agent.md / SKILL.md / scripts/{validate_teacher,measure_latency}.py
    kd-train-script/                       # 【v4 嵌入】生成统一 train_pipeline.py（teacher+distill 两模式，自包含）
      agent.md / SKILL.md / references/{templates/train_pipeline.py, workflows/, workflow-checklists/}
    kd-setup/agent.md                      # 幂等：teacher 训（train_pipeline --mode teacher）+ teacher_setup + 预检 + GPU 预检
    kd-gate/agent.md                       # 串行 latency gate（一个节点遍历全部变体）
    kd-train/agent.md                      # 有界并发蒸馏池（吃 gate manifest；worker 调 train_pipeline --mode distill）
    kd-select/                             # 【finalize 新增】脚本化最终选择（零 LLM）
      agent.md / scripts/select_and_report.py
    _kd_scripts/
      CONTRACTS.md                         # 本文件
      kd_common.py                         # 共享 helper（sha256/provider_id/read_ledger/is_variant_done/acquire_run_lock/RANK）
      _device.py                           # resolve_device / ort_providers / is_npu_available（cuda:local_rank 支持）
      teacher_model.py                     # 【v4 legacy】teacher（10 层 t1/t2 交替）；active path 改用 teacher-gen 产物，此文件仅 demo/单测消费
      pick_variant.py                      # 确定性变体枚举（_list_variants / _validate_variant / done 谓词）
      tune_latency.py                      # 最小缩量 latency 调参（seed/cache/median+std）
      distill_dispatch.py                  # BLK-17 gate（noop|train）
      gate_all.py                          # 串行 gate 全部变体 → manifest + FAIL_latency 增量落账
      gpu_probe.py                         # GPU 探测 + 并发判定（setup 阶段，fail-soft）
      train_pool.py                        # 有界并发池（吃 manifest；worker 调 train_pipeline.py --mode distill）
      measure_student.py                   # 精度测量（绝对基线；--skip_latency 复用 latency）
      teacher_setup.py                     # teacher_cache.pt + teacher_meta.json（含哈希；latency 可 --teacher_latency_ms 透传）
      setup_helpers.py                     # 【v4 legacy】find-teacher-ckpt / grep-user-train（active path 不再调；train_pipeline 固定 out_ckpt + train-script-gen 接管 loss 适配）
      viz_kd.py                            # sweep 可视化（散点 + 表 + latency bar）
      kd/{losses,wrapper,compose,ema}.py   # KD 库（不变）
      _deprecated/
        train_adapter_template.py          # 【v4 退役】原蒸馏训练脚本，被 train_pipeline.py 取代（保留作历史参考）
knowledge_base/families/receiver/          # model8 变体仓（.py）+ _model8_blocks.py 共享积木
kd-nas-artifacts/                          # 跨 run 稳定 artifact 根（teacher_cache/ledger/ckpts/gate_manifest/...）
```

> **v4 变更**（2026-07-31 嵌入）：teacher-gen + train-script-gen 两 folder-agent 从独立阶段嵌入 workflow
> DAG（新增两节点）；setup teacher 训练改调 ``train_pipeline.py --mode teacher``（固定 ``--out_ckpt``，
> 不再 ``teacher_train_command`` + ``setup_helpers find-teacher-ckpt``）；setup 删 step6 grep-user-train
> （loss/dataloader 适配下沉给 train-script-gen）；teacher_setup latency 从 ``teacher-gen.output`` 透传
> （不再自测）；``train_pool`` worker 改调 ``train_pipeline.py --mode distill``；
> ``train_adapter_template.py`` 退役到 ``_deprecated/``。input ``teacher_train_command`` 改名
> ``user_train_script``（用户原 train.py 路径，给 train-script-gen 读）。

## 1. 变体 I/O 契约（每个 receiver/*.py 必须暴露）

```python
DUMMY_INPUT = {"shape": [1,4,48,64,1], "dtype": "float32"}   # 用户真实 I/O 维度（禁硬编码回退）
BUILD_FN = "build_model"
KNOBS = {                                                      # 可调旋钮；step<0, leverage∈{high,medium,low}
    "num_blocks": {"default":3,"min":1,"step":-1,"leverage":"high"},
    "embed_dim":  {"default":16,"min":8,"step":-4,"leverage":"medium"},
}
def build_model(**cfg) -> nn.Module: ...                       # 零参用 KNOBS.default；cfg 覆盖旋钮
def feature_hook_names(self) -> list[str]: ...                 # 可选，OFD/FitNets 特征对齐
```
- **I/O**：输入 `[B,num_ports,num_subcarriers,num_symbols,1]`，输出同形；内部自理 alpha 归一。
- **文件名 = variant_id**（stem）；`_*.py` 是共享模块（pick_variant glob 排除）。
- **teacher 不在此**：在 `_kd_scripts/teacher_model.py`（10 层 t1/t2 交替）。

## 2. SelectionSpec / gate manifest entry

**SelectionSpec**（pick_variant 内部用，gate_all 复用）：
```json
{"variant_id":"spt_alt","variant_path":"…/spt_alt.py","variant_sha256":"…",
 "build_fn":"build_model","dummy_input":{"shape":[…],"dtype":"float32"},
 "knobs":{"num_blocks":{...},...},"tunable":true}
```

**gate manifest entry**（`gate_all.py` → `<kd_artifacts_dir>gate_manifest.json`，list）：
```json
{"variant_id":"spt_alt","variant_path":"…","variant_sha256":"…",
 "accepted_cfg":{...},"latency_ms_median":7.3,"latency_ms_std":0.1,
 "build_fn":"build_model","dummy_input":{...},"knobs":{...}}
```

## 3. 确定性脚本 CLI（stdout `KEY: value` 供 agent 解析；非零退出 = fail loud）

- **pick_variant.py**（gate_all 内部 + setup 预检用）：`--receiver_dir --ledger --target_latency_ms --latency_provider [--force_rerun] [--out]`
  → `VARIANT_SPEC: <path>` + `VARIANT_ID: <id>` / `ALL_DONE: true` / `NO_VARIANTS`（exit 3）。
- **tune_latency.py**（gate_all 内部用）：`--variant_path --build_fn --dummy_input --knobs --target_latency_ms --latency_provider --artifacts_dir [--max_measurements 40] [--measure_repeats 3] [--device auto] [--seed 0] [--opset 17]`
  → `TUNE_STATUS: ACCEPTED|FAIL_latency` + `ACCEPTED_CFG`/`BEST_EFFORT_CFG` + `LATENCY_MS_MEDIAN` + `LATENCY_MS_STD` + `MEASUREMENTS`。
- **distill_dispatch.py**（gate_all 内部用）：`--tune_status ACCEPTED|FAIL_latency` → `DISTILL_ACTION: noop|train`（BLK-17）。
- **gpu_probe.py**【新，setup step 8】：`--teacher_cache --representative_variant --variants_count [--device auto] [--safety 0.8] [--max_concurrency 8] [--seed 0]`
  → `RESOLVED_DEVICE` + `N_GPUS` + `FREE_VRAM_BYTES` + `PER_VARIANT_VRAM_BYTES` + `CONCURRENCY` + `DEVICE_PLAN`（JSON list）+ `GPU_REPORT`。
  fail-soft：无 CUDA/NPU / 探测异常 → `CONCURRENCY: 1` + `DEVICE_PLAN: [""]` + WARN，exit 0；仅输入契约不符 → exit 2。
- **gate_all.py**【新，gate 节点】：`--receiver_dir --ledger --target_latency_ms --latency_provider --artifacts_dir --kd_scripts_dir --accuracy_baseline --latency_tune_budget [--measure_repeats 3] [--device auto] [--seed 0] [--force_rerun] [--manifest_out <path>]`
  → `ACCEPTED_MANIFEST_PATH` + `N_ACCEPTED` + `N_FAIL_LATENCY` + `ALL_VARIANTS_COUNT` + `ALL_PROCESSED` + `SKIPPED_DONE`。
  串行遍历全部变体（`_list_variants` 固定序）→ 每变体 validate+tune+dispatch → FAIL_latency/FAIL_train 当场增量落账（持 orca.lock）→ ACCEPTED 进 manifest。
- **train_pool.py**【train 节点】：`--manifest --ledger --teacher_cache --kd_scripts_dir --artifacts_dir --per_run_artifacts_dir --project_root --train_pipeline_path --accuracy_baseline [--accuracy_baseline_kind] --latency_provider --target_latency_ms --concurrency --device_plan --per_variant_vram_bytes [--epochs 50] [--seed 0] [--safety 0.8] [--receiver_dir]`
  → `VARIANTS_DONE` + `VARIANTS_TOTAL` + `SWEEP_STATUS: SUCCESS|FAIL` + `FAIL_REASON`。
  Phase B VRAM 再校验（不够降级 WARN / 连 1 都放不下 fail loud 退 2）→ ThreadPoolExecutor round-robin 绑卡 → 每 worker 调 `train_pipeline.py --mode distill` 训练 + `--mode eval` 测精度（取代旧 measure_student --eval_command；eval 逻辑由 train-script-gen 从用户仓 eval 脚本移植、自包含固化进 train_pipeline.py）→ as_completed 增量落账 → 末尾 viz_kd。worker 不再传 `--user_train_import/--user_loss_fn`（train-script-gen 生成 train_pipeline.py 时已自包含搬入用户 loss/dataloader/eval 指标）。
  末态判定 ``classify_final_sweep``：``n_accepted>0`` 但 ledger 0 个 ``SUCCESS``（全 FAIL_accuracy/FAIL_train）→ ``SWEEP_STATUS: FAIL``（Increment E 防假，避免「全 FAIL 但 SUCCESS」）。
- **select_and_report.py**【finalize 新增，select 节点；零 LLM】：`--ledger --kd_artifacts_dir --accuracy_baseline --accuracy_baseline_kind --target_latency_ms [--teacher_latency_ms] [--baseline_latency_ms] [--env_anchor]`
  → `N_SELECTED` + `ALL_VARIANTS_COUNT` + `BEST_VARIANT` + `PARETO_FRONT` + `SELECTION_OK` + `FINAL_REPORT`。
  读 ledger → 按 ``accuracy_baseline_kind`` 显式方向（``kd_common.accuracy_direction`` 单一真相源）挑最优 student（达标项里精度最优，方向：nmse/mse/ber/db=min, snr/acc=max）+ latency-accuracy 非支配帕累托前沿 + 模板填空 ``<kd_artifacts_dir>final_report.md`` + 推 ``chart_type=pareto`` 前沿图（sidecar）。
  **fail loud**：ledger 空/坏 / kind 未知方向 → exit 2 + 报告标注失败（不假装选完）；**无达标 student** → exit 0 + 报告标「无 student 达标」+ ``N_SELECTED: 0`` + ``BEST_VARIANT:`` 空串（**绝不**伪造选出）。
- **viz_kd.py**（train_pool 末尾 sidecar 调用）：`--ledger [--baseline_latency_ms] [--target_latency_ms] [--variants_total] [--accuracy_baseline] [--accuracy_baseline_kind] [--env_anchor]`。
  推 6 图：sweep scatter / ledger table / latency bar / **progress（status 计数 + n_done/n_total）** / **pareto（latency×accuracy，方向按 kind）** / **accuracy_compare（各变体 accuracy + baseline 参考线）**。指标方向经 ``kd_common.accuracy_direction`` 标轴（y_label/caption 注「越低/越高越好」），未知 kind 大声标「方向未知」**不 auto 猜**。
- **train_pipeline.py**【train-script-gen 产物；setup step5 调 --mode teacher + train_pool worker 调 --mode distill/eval】：`--mode teacher|distill|eval --out_ckpt [--model_path（teacher）] [--student_model_path（distill/eval）] [--teacher_cache（distill）] [--student_ckpt（eval）] [--build_fn] [--build_cfg] [--kd_config（distill）] [--user_train_import] [--user_loss_fn] [--user_eval_import（eval）] [--user_eval_fn（eval）] [--accuracy_baseline（eval）] [--accuracy_baseline_kind（eval）] [--epochs] [--lr] [--batch_size] [--device] [--seed] [--variant_id] [--project_root] [--env_anchor]`
  → teacher 模式：`TEACHER_CKPT` + `TASK_LOSS_FINAL`；distill 模式：`STUDENT_CKPT` + `KD_LOSS_FINAL` + `KD_PROXY_MSE`（每-epoch render_chart 推 loss 图）；**eval 模式：`STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `ACCURACY_CONFIDENCE`（只读，不写 ckpt，取代 measure_student 精度路径）**。
  ckpt schema（teacher）：`{state_dict, build_cfg, variant_id, epochs, final_loss, mode}`；（distill）：`{student_state_dict, variant_id, student_cfg, kd_config, epochs, proxy_mse, mode}`；（eval）：**不写 ckpt**（只读评测）。
  runtime 需 `ORCA_KD_SCRIPTS_DIR` env 指向 `_kd_scripts/`（import kd.compose/wrapper/ema）；模板见 `kd-train-script/references/templates/train_pipeline.py`。
- **measure_student.py**【**KD 精度路径已由 train_pipeline.py --mode eval 取代**（train_pool 不再调）；文件保留供测试/struct 复用其纯函数】：`--student_model_path --student_ckpt --build_fn [--build_cfg] [--eval_command|--eval_dataset] [--accuracy_baseline] [--accuracy_baseline_kind] [--latency_provider] [--target_latency_ms] --output_dir [--skip_latency] [--device] [--seed]`
  → `STUDENT_LATENCY_MS` + `STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `MET_LATENCY` + `STUDENT_ONNX` + `ACCURACY_CONFIDENCE`。其 `_parse_accuracy` / `_compute_met_accuracy_absolute` 逻辑是 eval mode stdout 协议 + 方向判定的参照来源。
- **teacher_setup.py**：`--teacher_model_path --teacher_ckpt --build_fn --dummy_input [--eval_command] --output_dir [--latency_provider] [--teacher_latency_ms] [--device] [--seed]`
  → `TEACHER_LATENCY_MS` + `TEACHER_ACCURACY` + `TEACHER_ACCURACY_KNOWN` + `TEACHER_DB_BASELINE` + `TEACHER_ONNX` + `TEACHER_CACHE` + `TEACHER_META`（meta 含 `teacher_model_hash` + `teacher_ckpt_sha256`）。
  latency 来源优先级：`--teacher_latency_ms`（teacher-gen.output 透传，避免重复测量）> `--latency_provider`（自测 ONNX）；两者皆空 → fail loud。
- **train_adapter_template.py**【v4 退役，移到 `_deprecated/`】：原 train_pool worker 调的蒸馏训练脚本，被 `train_pipeline.py`（teacher+distill 两模式）取代。保留作历史参考，**active path 不再引用**。
- **export_onnx.py**（共享）：新增 `--build_cfg`（透传 build_kwargs）+ `build_kwargs: dict|None` 参（向后兼容）。

## 4. 节点 I/O

| 节点 | 关键输出 |
|---|---|
| flatten | baseline_contract_path / project_root / model_name / flat_artifacts_dir / **baseline_latency_ms**（展平任意模型入口成 KD 变体契约 .py；`__main__` 跑「正确性 + latency」统一契约 → baseline_latency_ms 由 inputs.latency_provider 实测） |
| teacher-gen | **teacher_model_path** / **teacher_latency_ms** / project_root / depth_axis / width_axis（纯调参派生 teacher wrapper：深度×3/宽度×2，委托 baseline.build_model；`__main__` 测 latency；双重硬校验 PASS） |
| train-script-gen | **train_pipeline_path**（生成统一 train_pipeline.py：teacher+distill+**eval** 三模式，自包含搬用户 loss/dataloader/optimizer + **从用户仓自动发现并移植 eval 指标**，按路径 import 模型；读 flatten.output + teacher-gen.output + inputs.user_train_script） |
| setup | kd_artifacts_dir / per_run_artifacts_dir / project_root / teacher_model_path（透传 teacher-gen.output）/ teacher_cache / teacher_meta / teacher_ckpt / ledger_path / ckpts_dir / baseline_latency_ms（透传 flatten.output）/ kd_scripts_dir / receiver_dir / variants_count / **concurrency / device_plan / per_variant_vram_bytes / gpu_report** |
| gate | accepted_manifest_path / n_accepted / n_fail_latency / all_variants_count / all_processed |
| train | variants_done / variants_total / sweep_status / fail_reason |
| select | n_selected / all_variants_count / best_variant / final_report_path / pareto_front_size / selection_ok / fail_reason |

**路由**（纯函数 router 求值，无 LLM）：
- flatten → teacher-gen（恒定）。
- teacher-gen → train-script-gen（恒定）。
- train-script-gen → setup（恒定）。
- setup → gate（恒定）。
- gate：`n_accepted | int == 0` → `$end`（首条 when，跳过 train+select）；否则 → train。
- train → select（恒定）。
- select → `$end`（恒定）。
- **无 workflow 循环**：gate 在一个节点内串行处理全部变体；train 在一个节点内并发处理全部 ACCEPTED；select 读 ledger 出最终报告。
- **skipped node 约束**：gate→$end 时 train+select 都被跳过（output=None），故 workflow ``outputs:`` 不模板化 train/select 的字段（仅 setup/gate 恒跑）；train 与 select 的产出经 agent stdout + ledger.jsonl / final_report.md 真相源暴露。

## 5. ledger.jsonl 行 schema（跨 run 真相源，append-only）

```json
{"variant_id":"spt_alt","variant_path":"…","variant_sha256":"…","accepted_cfg":{...},"cfg_hash":"<sha16>",
 "status":"SUCCESS","latency_ms_median":7.3,"latency_ms_std":0.1,"accuracy":0.021,"accuracy_kind":"nmse",
 "met_latency":true,"met_accuracy":true,"ckpt":"…/spt_alt.pt","target_latency_ms":8.0,
 "accuracy_baseline":0.02,"latency_provider_id":"path::func|sha16","run_id":"kd-nas-…","fail_reason":""}
```
**字段**：
- `cfg_hash`：`accepted_cfg` 的 sha16（`hashlib.sha256(json.dumps(cfg, sort_keys=True)).hexdigest()[:16]`）；
  gate_all 与 train_pool 都写。done 谓词不读它（身份用 `variant_sha256`+`latency_provider_id`），但
  force_rerun upsert（旧 recorder 路径）按 `(variant_id, cfg_hash, run_id)` 去重。
- `latency_ms_median`：哨兵语义按 status 分——
  - **gate FAIL_latency**：**真测值**（tune_latency 即便 FAIL 也 emit 测得的 latency，只是超 target）。
  - **gate-异常 FAIL_train**（tune_latency rc!=0）：`-1` 哨兵（未测到）。
  - **train FAIL_train / measure-fail FAIL_accuracy**：latency 取自 gate manifest（真测值），`accuracy=0` 哨兵。
  下游消费者（viz_kd / select_and_report）按 ``kd_common.is_measured_row`` 判「是否真测了 accuracy」
  （status ∈ {SUCCESS, FAIL_accuracy} 且 ``accuracy_kind`` 非空）——哨兵行不画入 accuracy 坐标图 / 帕累托前沿。
- `accuracy_kind`：measure rc==0 时非空（真测，即便数值恰为 0.0）；measure 失败 / 未进训练池时为空串
  （此时 ``accuracy=0`` 是哨兵，**非**真测）。是区分真测 vs 哨兵的权威字段。
- `run_id`：`$ORCA_RUN_ID`（in-session run 标识，跨 run 复用不依赖它）。

**status 全部取值**：`SUCCESS` / `FAIL_latency`（gate 落）/ `FAIL_accuracy`（train 落）/ `FAIL_train`（gate 或 train 落）。
**写时机**（v2 新）：
- gate_all：FAIL_latency / gate-异常 FAIL_train 行**当场增量 append**（主线程持 orca.lock，逐行 write+flush）。
- train_pool：SUCCESS / FAIL_accuracy / FAIL_train 行 `as_completed` 主线程**逐行增量 append**（同锁）。
- **不再有「整批跑完才一次性 append」**（kill 不丢已完成行）。

**done 谓词**（`kd_common.is_variant_done`，跨 run 复用）：见 `kd_common.py` 源码（variant_sha256/latency_provider_id/ckpt/target 校验）。gate_all 与 train_pool 写的行身份字段一致 → 下次 run gate 把这些变体当 done 跳过。

## 6. 铁律

- **dummy_input 用户指定**：禁硬编码 shape 回退（BLK-4）。
- **latency 必用用户脚本**：`latency_provider` 必填无默认（BLK-3/10，编译期 validator 强制）。
- **确定性路由**：`n_accepted` / `tune_status` 由确定性脚本算，agent 不自定（LO-5）。
- **跨 run 复用**：稳定 `kd_artifacts_dir` + 哈希校验 + ledger-driven 跳过；单写者（BLK-13 orca.lock）。
- **【新】时延测量必串行**：latency 对 contention 敏感（并发测→读数失真→false FAIL_latency）。gate_all 串行测；train_pool 用 `--skip_latency` 复用 gate 的干净 latency（HI-1）。
- **【新】并发数唯一权威 = setup.gpu_probe**：gate/train 信任 setup 的 concurrency；train_pool 仅做 Phase B 启动前 VRAM 再校验防护（setup→train 之间显存可能被抢），不重算并发。
- **【新】绝不伪造**：latency / accuracy 必须真实测量；无任何 fallback 造假路径（measure 解析不出 → confidence=low + met=false，**不**编造数值）。select 无达标 → 报告标「无 student 达标」（``N_SELECTED: 0``），**不**假装选出；train 0 SUCCESS → ``SWEEP_STATUS: FAIL``。
- **【finalize】指标方向显式 + 单一真相源**：``accuracy_baseline_kind`` 是必填 [ask] input（用户一开始声明：nmse/mse/ber/db 越低越好 | snr/acc 越高越好）。方向判定统一走 ``kd_common.accuracy_direction``（measure_student / viz_kd / select 三处 import 同一函数），**禁**符号 auto 猜（防「-20dB 误判优于 -22dB」反向错误）。未知 kind → fail loud（select exit 2）/ 低置信 + met=false（measure），绝不静默 pass。
