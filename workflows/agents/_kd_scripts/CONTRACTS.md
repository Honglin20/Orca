# kd-nas workflow — 接口契约（CONTRACTS，重构版）

> KD-NAS = 确定性蒸馏 sweep（receiver KB 的 model8 `.py` 变体）。改接口 = 改本文件 + 通知依赖方。
> fail loud：脚本遇契约不符输入直接非零退出 + stderr 报因。

## 0. 目录布局

```
workflows/
  kd-nas.yaml                              # workflow DAG（setup → selector → distill → recorder → … → $end）
  agents/
    kd-setup/agent.md                      # 幂等：baseline latency + teacher + train_kd 适配 + 预检
    kd-selector/agent.md                   # pick_variant + tune_latency（gatekeeper）
    kd-distill/agent.md                    # distill_dispatch gate + 完整训练 + measure 精度
    kd-recorder/agent.md                   # 一致性断言 + ledger + viz
    _kd_scripts/
      CONTRACTS.md                         # 本文件
      kd_common.py                         # 共享 helper（sha256/provider_id/read_ledger/is_variant_done/acquire_run_lock/RANK）
      teacher_model.py                     # teacher（10 层 t1/t2 交替，repo 写死）
      pick_variant.py                      # 确定性选变体（glob + done 谓词 + KNOBS 校验）
      tune_latency.py                      # 最小缩量 latency 调参（seed/cache/median+std）
      distill_dispatch.py                  # BLK-17 gate（noop|train）
      measure_student.py                   # 精度测量（绝对基线；--skip_latency 复用 latency）
      teacher_setup.py                     # teacher_cache.pt + teacher_meta.json（含哈希）
      train_adapter_template.py            # 蒸馏训练（路径加载 + 每-epoch render_chart）
      train_variants_parallel.py           # 多变体并行蒸馏（独立工具，复用 distill 逻辑，写共享 ledger）
      viz_kd.py                            # sweep 可视化（散点 + 表 + latency bar）
      kd/{losses,wrapper,compose,ema}.py   # KD 库（不变）
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
def feature_hook_names(self) -> list[str]: ...                 # 可选，OFD/FitNets 特征对齐
```
- **I/O**：输入 `[B,num_ports,num_subcarriers,num_symbols,1]`，输出同形；内部自理 alpha 归一。
- **文件名 = variant_id**（stem）；`_*.py` 是共享模块（pick_variant glob 排除）。
- **teacher 不在此**：在 `_kd_scripts/teacher_model.py`（10 层 t1/t2 交替）。

## 2. SelectionSpec（pick_variant → selector）

```json
{"variant_id":"spt_alt","variant_path":"…/spt_alt.py","variant_sha256":"…",
 "build_fn":"build_model","dummy_input":{"shape":[…],"dtype":"float32"},
 "knobs":{"num_blocks":{...},...},"tunable":true}
```

## 3. 确定性脚本 CLI（stdout `KEY: value` 供 agent 解析；非零退出 = fail loud）

- **pick_variant.py**：`--receiver_dir --ledger --target_latency_ms --latency_provider [--force_rerun] [--out]`
  → `VARIANT_SPEC: <path>` + `VARIANT_ID: <id>` / `ALL_DONE: true` / `NO_VARIANTS`（exit 3）。
- **tune_latency.py**：`--variant_path --build_fn --dummy_input --knobs --target_latency_ms --latency_provider --artifacts_dir [--max_measurements 40] [--measure_repeats 3] [--device auto] [--seed 0] [--opset 17]`
  → `TUNE_STATUS: ACCEPTED|FAIL_latency` + `ACCEPTED_CFG`/`BEST_EFFORT_CFG` + `LATENCY_MS_MEDIAN` + `LATENCY_MS_STD` + `MEASUREMENTS`。
- **distill_dispatch.py**：`--tune_status ACCEPTED|FAIL_latency` → `DISTILL_ACTION: noop|train`（BLK-17）。
- **measure_student.py**：`--student_model_path --build_fn [--build_cfg] [--dummy_input] [--eval_command|--eval_dataset] [--accuracy_baseline] [--accuracy_baseline_kind] [--teacher_meta] [--latency_provider] [--target_latency_ms] --output_dir [--skip_latency] [--device] [--seed]`
  → `STUDENT_LATENCY_MS` + `STUDENT_ACCURACY` + `STUDENT_ACCURACY_KIND` + `MET_ACCURACY` + `MET_LATENCY` + `STUDENT_ONNX` + `ACCURACY_CONFIDENCE`。
- **teacher_setup.py**：`--teacher_model_path --teacher_ckpt --build_fn --dummy_input [--eval_command] --output_dir --latency_provider [--device] [--seed]`
  → `TEACHER_LATENCY_MS` + `TEACHER_ACCURACY` + `TEACHER_ACCURACY_KNOWN` + `TEACHER_DB_BASELINE` + `TEACHER_ONNX` + `TEACHER_CACHE` + `TEACHER_META`（meta 含 `teacher_model_hash` + `teacher_ckpt_sha256`）。
- **train_adapter_template.py**（distill 跑）：`--student_cfg --kd_config --teacher_cache --student_model_path --build_fn --variant_id --env_anchor --epochs --out_ckpt [--user_train_import --user_loss_fn --device --seed]`
  → `STUDENT_CKPT` + `KD_LOSS_FINAL` + `KD_PROXY_MSE`（每-epoch render_chart 推 loss 图）。
- **export_onnx.py**（共享）：新增 `--build_cfg`（透传 build_kwargs）+ `build_kwargs: dict|None` 参（向后兼容）。
- **train_variants_parallel.py**（独立工具，非 workflow 节点）：多变体**并行**蒸馏，复用 distill 流水线（tune→dispatch→train_kd→measure），`--concurrency` 并发，结果 append 共享 `ledger.jsonl`（串行 workflow 下次启动把这些变体当 done 跳过）。CLI 见脚本 `--help`；单写者锁 + done 谓词与 workflow 一致。

## 4. 节点 I/O

| 节点 | 关键输出 |
|---|---|
| setup | kd_artifacts_dir / per_run_artifacts_dir / project_root / teacher_model_path / teacher_cache / teacher_meta / teacher_ckpt / ledger_path / ckpts_dir / baseline_latency_ms / kd_scripts_dir / user_train_import / user_loss_fn / variants_count |
| selector | all_done / tune_status(ACCEPTED\|FAIL_latency) / variant_id / variant_path / variant_sha256 / accepted_cfg / latency_ms_median / latency_ms_std / build_fn / dummy_input / knobs / measurements |
| distill | variant_id / status(SUCCESS\|FAIL_latency\|FAIL_accuracy\|FAIL_train\|FAIL_export) / latency_ms_median / latency_ms_std / accuracy / accuracy_kind / met_latency / met_accuracy / ckpt / fail_reason |
| recorder | recorded / variants_done / variants_total / coherence_ok |

**路由**（纯函数 router 求值，无 LLM）：
- selector：`all_done=true` → $end（首条 when）；否则 → distill。
- distill → recorder。
- recorder → selector。
- 循环自终止：所有变体蒸馏完 → selector emit all_done → $end。无 finalize。

## 5. ledger.jsonl 行 schema（跨 run 真相源，append-only）

```json
{"variant_id":"spt_alt","variant_path":"…","variant_sha256":"…","accepted_cfg":{...},
 "status":"SUCCESS","latency_ms_median":7.3,"latency_ms_std":0.1,"accuracy":0.021,"accuracy_kind":"nmse",
 "met_latency":true,"met_accuracy":true,"ckpt":"…/spt_alt.pt","target_latency_ms":8.0,
 "accuracy_baseline":0.02,"latency_provider_id":"path::func|sha16","run_id":"kd-nas-…","fail_reason":""}
```
**done 谓词**（`kd_common.is_variant_done`，跨 run 复用）：见 `kd_common.py` 源码（variant_sha256/latency_provider_id/ckpt/target 校验）。

## 6. 铁律

- **dummy_input 用户指定**：禁硬编码 shape 回退（BLK-4）。
- **latency 必用用户脚本**：`latency_provider` 必填无默认（BLK-3/10，编译期 validator 强制）。
- **确定性路由**：`all_done`/`tune_status` 由确定性脚本算，agent 不自定（LO-5）。
- **跨 run 复用**：稳定 `kd_artifacts_dir` + 哈希校验 + ledger-driven 跳过；单写者（BLK-13 orca.lock）。
