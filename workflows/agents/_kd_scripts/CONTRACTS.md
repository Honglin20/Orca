# kd-nas workflow — 接口契约（CONTRACTS，串行 v5）

> KD-NAS = flatten 任意模型入口成 KD 变体契约 + teacher-gen 纯调参派生 teacher + train-script-gen 生成统一
> 训练脚本 + **串行迭代**蒸馏 sweep（gen-student→distill→decide 循环，每轮单 student）+ finalize 选择。
> **活跃 DAG**（串行）：``flatten → setup → gen_teacher → gen_train_script → train_script_verify →
> train_teacher → (gen_student → distill → decide)* → finalize``。
> 改接口 = 改本文件 + 通知依赖方。
> fail loud：脚本遇契约不符输入直接非零退出 + stderr 报因（硬件缺失/探测异常则 fail-soft 退 0，不阻塞 workflow）。

> **v5 变更**（2026-08-03 串行化 + DEPRECATED 标注）：活跃 workflow 改串行迭代（gen_student→distill→decide），
> 旧并行 sweep（gate_all/train_pool/select_and_report/viz_kd）退出活跃 runtime。这些脚本**仍在活跃测试中**
> 被直接 import（test_kd_redesign ~74 测 / test_struct_kd_p7 TestVizKD* ~20 测），物理删除 defer 到独立 followup SPEC
> （先删测试迁移 viz_kd 可复用不变量到 viz_kd_stage 测试，再删脚本）。本文件不再把旧并行脚本描述为活跃路径。

## 0. 目录布局

```
workflows/
  kd-nas.yaml                              # workflow DAG（活跃串行：flatten→setup→gen_teacher→...→finalize）
  agents/
    model-flatten/                         # 入口 agent：展平任意模型入口成 KD 变体契约
      agent.md / SKILL.md / scripts/{validate_contract,measure_latency}.py
    teacher-gen/                           # 纯调参派生 teacher（深度×3/宽度×2，wrapper 委托 baseline）
      agent.md / SKILL.md / scripts/{validate_teacher,measure_latency}.py
    kd-train-script/                       # 生成统一 train_pipeline.py（teacher+distill+eval 三模式，自包含）
      agent.md / SKILL.md / references/{templates/train_pipeline.py, workflows/, workflow-checklists/}
    kd-setup/agent.md                      # 幂等 setup：teacher 训 + teacher_setup + 预检 + GPU 预检
    gen-student/agent.md                   # 串行：每轮派生 student model.py（首轮固定规则 / 迭代轮 KB+perf）
    distill/agent.md                       # 串行：单 student KD 蒸馏（tune_latency→distill→eval）
    decide/agent.md                        # 串行：champion ratchet + 终止判定 + ledger 落账
    finalize/agent.md                      # 终态：champion eval/ONNX/latency + final_report.md
    kd-gate/agent.md                       # 【DEPRECATED】旧并行 gate 节点（活跃路径不调用，见 §3）
    kd-train/agent.md                      # 【DEPRECATED】旧并行 train 节点
    kd-select/                             # 【DEPRECATED】旧并行 select 节点
      agent.md / scripts/select_and_report.py
    _kd_scripts/
      CONTRACTS.md                         # 本文件
      kd_common.py                         # 共享 helper（sha256/provider_id/read_ledger/is_variant_done/acquire_run_lock/RANK/accuracy_direction/is_measured_row）
      _device.py                           # resolve_device / ort_providers / is_npu_available（cuda:local_rank 支持）
      teacher_model.py                     # 【legacy】teacher（10 层 t1/t2 交替）；active path 改用 teacher-gen 产物，此文件仅 demo/单测消费
      pick_variant.py                      # 确定性变体枚举（_list_variants / _validate_variant / done 谓词）
      tune_latency.py                      # 最小缩量 latency 调参（seed/cache/median+std）
      distill_dispatch.py                  # BLK-17 gate（noop|train）
      gate_all.py                          # 【DEPRECATED】旧并行串行 gate（活跃串行 kd-nas.yaml 不调用）
      gpu_probe.py                         # GPU 探测 + 并发判定（setup 阶段，fail-soft）
      train_pool.py                        # 【DEPRECATED】旧并行有界并发池（活跃串行 kd-nas.yaml 不调用）
      measure_student.py                   # 精度测量（纯函数供测试/struct 复用；KD 精度路径已由 train_pipeline --mode eval 取代）
      teacher_setup.py                     # teacher_cache.pt + teacher_meta.json
      setup_helpers.py                     # 【legacy】find-teacher-ckpt / grep-user-train（active path 不再调）
      viz_kd.py                            # 【DEPRECATED】旧 sweep 可视化（活跃串行路径用 viz_kd_stage；pareto 语义已 port 到 viz_kd_stage）
      viz_kd_stage.py                      # 活跃串行每节点 web 推送 sidecar（baseline/teacher/student/distill_table/decide/final）
      metrics_tail.py                      # distill loss line（log-tail 推送）
      finalize_kd.py                       # finalize 确定性后端（champion eval/ONNX/latency + final_report.md）
      kd/{losses,wrapper,compose,ema}.py   # KD 库（不变）
      _deprecated/
        train_adapter_template.py          # 【v4 退役】原蒸馏训练脚本，被 train_pipeline.py 取代
knowledge_base/families/receiver/          # model8 变体仓（.py）+ _model8_blocks.py 共享积木
kd-nas-artifacts/                          # 跨 run 稳定 artifact 根（teacher_cache/ledger/ckpts/...）
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
- **文件名 = variant_id**（stem）；`_*.py` 是共享模块（pick_variant glob 排除）。
- **teacher 不在此**：在 `_kd_scripts/teacher_model.py`（10 层 t1/t2 交替）。
- **feature_hook_names fail-loud**（SPEC §1）：distill 用 AST 判定此 fn 是否存在 → 启用/剥离 ofd；
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
  下游消费者（viz_kd_stage / finalize）按 ``kd_common.is_measured_row`` 判「是否真测了 accuracy」
  （status ∈ {SUCCESS, FAIL_accuracy} 且 ``accuracy_kind`` 非空）——哨兵行不计入帕累托前沿。

**status 全部取值**：`SUCCESS` / `FAIL_latency`（tune 不过）/ `FAIL_train`（训练/eval 崩）/
`FAIL_build`（validate_contract 3 strikes）/ `FAIL_accuracy`（训练完但精度未达）/ `FAIL_export`（ONNX 导出失败）。

## 3. 确定性脚本 CLI（stdout `KEY: value` 供 agent 解析；非零退出 = fail loud）

### 3.1 活跃脚本（活跃串行 kd-nas.yaml 直接调用）

- **tune_latency.py**（distill 节点内部）：`--variant_path --build_fn --dummy_input --knobs --target_latency_us --latency_provider --artifacts_dir [--max_measurements 40] [--measure_repeats 3] [--device auto] [--seed 0] [--opset 17]`
  → `TUNE_STATUS: ACCEPTED|FAIL_latency` + `ACCEPTED_CFG`/`BEST_EFFORT_CFG` + `LATENCY_US_MEDIAN` + `LATENCY_US_STD` + `MEASUREMENTS`。
- **gpu_probe.py**【setup step 8】：`--teacher_cache --representative_variant --variants_count [--device auto] [--safety 0.8] [--max_concurrency 8] [--seed 0]`
  → `RESOLVED_DEVICE` + `N_GPUS` + `FREE_VRAM_BYTES` + `PER_VARIANT_VRAM_BYTES` + `CONCURRENCY` + `DEVICE_PLAN`（JSON list）+ `GPU_REPORT`。
  fail-soft：无 CUDA/NPU / 探测异常 → `CONCURRENCY: 1` + `DEVICE_PLAN: [""]` + WARN，exit 0；仅输入契约不符 → exit 2。
- **measure_student.py**【**KD 精度路径已由 train_pipeline.py --mode eval 取代**（活跃路径不再调）；文件保留供测试/struct 复用其纯函数】：`--student_model_path --student_ckpt --build_fn [--build_cfg] [--eval_command|--eval_dataset] [--accuracy_baseline] [--accuracy_baseline_kind] [--latency_provider] [--target_latency_us] --output_dir [--skip_latency] [--device] [--seed]`
  → `STUDENT_LATENCY_US` + `STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `MET_LATENCY` + `STUDENT_ONNX` + `ACCURACY_CONFIDENCE`。
  其 `_parse_accuracy` / `_compute_met_accuracy_absolute` 逻辑是 eval mode stdout 协议 + 方向判定的参照来源。
- **teacher_setup.py**：`--teacher_model_path --teacher_ckpt --build_fn --dummy_input [--eval_command] --output_dir [--latency_provider] [--teacher_latency_us] [--device] [--seed]`
  → `TEACHER_LATENCY_US` + `TEACHER_ACCURACY` + `TEACHER_ACCURACY_KNOWN` + `TEACHER_DB_BASELINE` + `TEACHER_ONNX` + `TEACHER_CACHE` + `TEACHER_META`（meta 含 `teacher_model_hash` + `teacher_ckpt_sha256`）。
- **viz_kd_stage.py**（活跃每节点 web 推送 sidecar）：`--stage <baseline|baseline_seed|teacher|student|distill_table|decide|final> [--ledger] [--champions] [--baseline_latency_us] [--baseline_accuracy] [--target_latency_us] [--accuracy_baseline_kind] [--teacher_latency_us] [--champion_latency_us] [--champion_accuracy] [--teacher_meta] [--round_hypothesis] [--env_anchor]`
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
- **train_pipeline.py**【train-script-gen 产物；setup 调 --mode teacher + distill 调 --mode distill/eval】：`--mode teacher|distill|eval --out_ckpt [--model_path（teacher）] [--student_model_path（distill/eval）] [--teacher_cache（distill）] [--student_ckpt（eval）] [--build_fn] [--build_cfg] [--kd_config（distill）] [--accuracy_baseline（eval）] [--accuracy_baseline_kind（eval）] [--epochs] [--lr] [--batch_size] [--device] [--seed] [--variant_id] [--project_root] [--env_anchor]`
  → teacher：`TEACHER_CKPT` + `TASK_LOSS_FINAL`；distill：`STUDENT_CKPT` + `KD_LOSS_FINAL` + `KD_PROXY_MSE`；
  eval：`STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `ACCURACY_CONFIDENCE`。
  runtime 需 `ORCA_KD_SCRIPTS_DIR` env 指向 `_kd_scripts/`。
- **export_onnx.py**（共享）：`--model_path --build_fn --dummy_input --opset --out --device --seed [--build_cfg]`。

### 3.2 DEPRECATED 脚本（旧并行 sweep 残留，活跃串行 workflow 不调用，保留供历史测试；删除见 followup SPEC）

> 这些脚本被 ``test_kd_redesign.py``（~74 测）/``test_struct_kd_p7.py::TestVizKD*``（~20 测）等直接 import；
> 物理删除需先迁/删测试，单独 SPEC 处理（不在本 CONTRACTS 范围）。**新增功能不应再调用它们**。

- **gate_all.py**【DEPRECATED，旧 gate 节点】：原并行 sweep 的串行 latency gate（一个节点遍历全部变体）。
- **train_pool.py**【DEPRECATED，旧 train 节点】：原并行 sweep 的有界并发训练池。
- **select_and_report.py**【DEPRECATED，旧 select 节点】：原并行 sweep 的脚本化最终选择 + 报告。
- **viz_kd.py**【DEPRECATED，旧 sweep 可视化】：原并行 sweep 的 6 图推送（scatter / table / latency bar / progress / pareto / accuracy_compare）。
  **pareto + 方向门 + sentinel 过滤的可复用不变量已 port 到活跃的 ``viz_kd_stage --stage final``**（SPEC §3），为后续物理删除铺路。
- **pick_variant.py / distill_dispatch.py**：旧并行 sweep 的内部 helper（gate_all 用）。tune_latency 的 done 谓词逻辑
  仍由 ``kd_common.is_variant_done`` 复用（活跃路径不直接调 pick_variant）。

## 4. 节点 I/O（活跃串行 DAG）

| 节点 | 关键输出 |
|---|---|
| flatten | baseline_contract_path / project_root / model_name / flat_artifacts_dir / **baseline_latency_us**（展平任意模型入口成 KD 变体契约 .py；`__main__` 跑「正确性 + latency」统一契约 → baseline_latency_us 由 inputs.latency_provider 实测） |
| setup | kd_artifacts_dir / per_run_artifacts_dir / project_root / teacher_model_path / teacher_cache / teacher_meta / teacher_ckpt / ledger_path / champions_path / ckpts_dir / student_models_dir / baseline_latency_us / baseline_accuracy / kd_scripts_dir / receiver_dir / **concurrency / device_plan / per_variant_vram_bytes / gpu_report** |
| gen_teacher | teacher_model_path / teacher_latency_us / depth_axis / width_axis |
| gen_train_script | train_pipeline_path |
| train_script_verify | verify_status / fidelity_report |
| train_teacher | teacher_cache（透传 setup） |
| gen_student | student_model_path / round / hypothesis / direction_id / knobs / status（OK|FAIL_build） |
| distill | round / latency_us / accuracy / accuracy_kind / met_latency / met_accuracy / ckpt / status（SUCCESS|FAIL_latency|FAIL_train|FAIL_build） |
| decide | terminate / terminate_reason / champion_id / ledger append |
| finalize | champion_is_baseline / champion_student / final_latency_us / final_accuracy / final_onnx / final_report / viz_status |

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
- **绝不伪造**：latency / accuracy 必须真实测量；无任何 fallback 造假路径。select/finalize 无达标 → 报告标「无 student 达标」（champion 维持 baseline），**不**假装选出。
- **指标方向显式 + 单一真相源**：``accuracy_baseline_kind`` 是必填 [ask] input。方向判定统一走 ``kd_common.accuracy_direction``
  （measure_student / viz_kd_stage / select 三处 import 同一函数），**禁**符号 auto 猜（防「-20dB 误判优于 -22dB」反向错误）。
  未知 kind → fail loud / 低置信 + met=false，绝不静默 pass。
- **特征蒸馏 fail-loud**（SPEC §1）：``kd/compose.py`` 守卫——kd_losses 含 ofd/fitnets/rkd 且运行时 feats 空 → raise ValueError → FAIL_train。
  distill agent 默认 KD_CONFIG 已 AST 条件化（按 student.feature_hook_names 存在决定启 ofd 还是 mse-only），无 hook 时自动剥离特征项不崩。
