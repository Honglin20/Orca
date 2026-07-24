# kd-nas workflow — 接口契约（CONTRACTS，重构版 v2）

> KD-NAS = 确定性蒸馏 sweep（receiver KB 的 model8 `.py` 变体）。DAG：`setup → gate → train → $end`。
> 改接口 = 改本文件 + 通知依赖方。
> fail loud：脚本遇契约不符输入直接非零退出 + stderr 报因（硬件缺失/探测异常则 fail-soft 退 0，不阻塞 workflow）。

## 0. 目录布局

```
workflows/
  kd-nas.yaml                              # workflow DAG（setup → gate → train → $end）
  agents/
    kd-setup/agent.md                      # 幂等：baseline latency + teacher + train_kd 适配 + 预检 + GPU 预检
    kd-gate/agent.md                       # 串行 latency gate（一个节点遍历全部变体）
    kd-train/agent.md                      # 有界并发蒸馏池（吃 gate manifest）
    _kd_scripts/
      CONTRACTS.md                         # 本文件
      kd_common.py                         # 共享 helper（sha256/provider_id/read_ledger/is_variant_done/acquire_run_lock/RANK）
      _device.py                           # resolve_device / ort_providers / is_npu_available（cuda:local_rank 支持）
      teacher_model.py                     # teacher（10 层 t1/t2 交替，repo 写死）
      pick_variant.py                      # 确定性变体枚举（_list_variants / _validate_variant / done 谓词）
      tune_latency.py                      # 最小缩量 latency 调参（seed/cache/median+std）
      distill_dispatch.py                  # BLK-17 gate（noop|train）
      gate_all.py                          # 【新】串行 gate 全部变体 → manifest + FAIL_latency 增量落账
      gpu_probe.py                         # 【新】GPU 探测 + 并发判定（setup 阶段，fail-soft）
      train_pool.py                        # 【新，前身 train_variants_parallel.py】有界并发池（吃 manifest）
      measure_student.py                   # 精度测量（绝对基线；--skip_latency 复用 latency）
      teacher_setup.py                     # teacher_cache.pt + teacher_meta.json（含哈希）
      train_adapter_template.py            # 蒸馏训练（路径加载 + 每-epoch render_chart）
      viz_kd.py                            # sweep 可视化（散点 + 表 + latency bar）
      kd/{losses,wrapper,compose,ema}.py   # KD 库（不变）
knowledge_base/families/receiver/          # model8 变体仓（.py）+ _model8_blocks.py 共享积木
kd-nas-artifacts/                          # 跨 run 稳定 artifact 根（teacher_cache/ledger/ckpts/gate_manifest/...）
```

> **v2 变更**：删 `kd-selector` / `kd-distill` / `kd-recorder` agent 目录（脚本 tune_latency /
> distill_dispatch / measure_student / train_adapter / pick_variant 全保留，被 gate/train 复用）；
> `train_variants_parallel.py` 重命名 + 重构为 `train_pool.py`（只做训练阶段）。原串行 workflow 循环
> （selector→distill→recorder→…）已被单节点 gate + 单节点 train 取代。

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
- **train_pool.py**【新，train 节点；前身 train_variants_parallel.py】：`--manifest --ledger --teacher_cache --kd_scripts_dir --artifacts_dir --per_run_artifacts_dir --project_root --test_command --accuracy_baseline [--accuracy_baseline_kind] --latency_provider --target_latency_ms --concurrency --device_plan --per_variant_vram_bytes [--epochs 50] [--seed 0] [--safety 0.8] [--user_train_import .. --user_loss_fn ..] [--receiver_dir]`
  → `VARIANTS_DONE` + `VARIANTS_TOTAL` + `SWEEP_STATUS: SUCCESS|FAIL` + `FAIL_REASON`。
  Phase B VRAM 再校验（不够降级 WARN / 连 1 都放不下 fail loud 退 2）→ ThreadPoolExecutor round-robin 绑卡 → 每 worker train_adapter + measure(--skip_latency) → as_completed 增量落账 → 末尾 viz_kd。
- **measure_student.py**（train_pool 内部用）：`--student_model_path --student_ckpt --build_fn [--build_cfg] [--eval_command|--eval_dataset] [--accuracy_baseline] [--accuracy_baseline_kind] [--latency_provider] [--target_latency_ms] --output_dir [--skip_latency] [--device] [--seed]`
  → `STUDENT_LATENCY_MS` + `STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `MET_LATENCY` + `STUDENT_ONNX` + `ACCURACY_CONFIDENCE`。
- **teacher_setup.py**：`--teacher_model_path --teacher_ckpt --build_fn --dummy_input [--eval_command] --output_dir --latency_provider [--device] [--seed]`
  → `TEACHER_LATENCY_MS` + `TEACHER_ACCURACY` + `TEACHER_ACCURACY_KNOWN` + `TEACHER_DB_BASELINE` + `TEACHER_ONNX` + `TEACHER_CACHE` + `TEACHER_META`（meta 含 `teacher_model_hash` + `teacher_ckpt_sha256`）。
- **train_adapter_template.py**（train_pool worker 跑）：`--student_cfg --kd_config --teacher_cache --student_model_path --build_fn --variant_id --env_anchor --epochs --out_ckpt [--user_train_import --user_loss_fn --device --seed]`
  → `STUDENT_CKPT` + `KD_LOSS_FINAL` + `KD_PROXY_MSE`（每-epoch render_chart 推 loss 图）。
- **export_onnx.py**（共享）：新增 `--build_cfg`（透传 build_kwargs）+ `build_kwargs: dict|None` 参（向后兼容）。

## 4. 节点 I/O

| 节点 | 关键输出 |
|---|---|
| setup | kd_artifacts_dir / per_run_artifacts_dir / project_root / teacher_model_path / teacher_cache / teacher_meta / teacher_ckpt / ledger_path / ckpts_dir / baseline_latency_ms / kd_scripts_dir / user_train_import / user_loss_fn / variants_count / **concurrency / device_plan / per_variant_vram_bytes / gpu_report** |
| gate | accepted_manifest_path / n_accepted / n_fail_latency / all_variants_count / all_processed |
| train | variants_done / variants_total / sweep_status / fail_reason |

**路由**（纯函数 router 求值，无 LLM）：
- setup → gate（恒定）。
- gate：`n_accepted | int == 0` → `$end`（首条 when，跳过 train）；否则 → train。
- train → `$end`（恒定）。
- **无 workflow 循环**：gate 在一个节点内串行处理全部变体；train 在一个节点内并发处理全部 ACCEPTED。

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
- `latency_ms_median`：gate FAIL_latency / train FAIL_train 异常路径用 `-1` 哨兵（非 measurement）。
  下游 viz_kd 等消费者按 status 过滤（FAIL_* 行不画入散点）。
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
- **【新】绝不伪造**：latency / accuracy 必须真实测量；无任何 fallback 造假路径（measure 解析不出 → confidence=low + met=false，**不**编造数值）。
