# kd-nas-demo —— kd-nas workflow 的真实 E2E 测试靶子

一个**最小可跑**的 kd-nas demo 项目，严格满足 `workflows/kd-nas.yaml`（v4 DAG：
`flatten → teacher_gen → train_script_gen → setup → gate → train → select → $end`）的 inputs 契约。
用**随机数据**+ **tiny 模型**让真实 workflow 在 CPU 上分钟级跑通。

**铁律遵守**：
- **数据可随机，测量必须真实**：训练/评测用随机数据；**latency 用 onnxruntime 实跑取中位数**，
  **精度（NMSE）用真实计算**（`||out-target||² / ||target||²`），无任何硬编码值。
- **小而快**：student 变体默认 `embed_dim=8 / num_blocks=2~3`，teacher 1 epoch，整条 E2E 目标 < 数分钟。

> ⚠️ demo 旨在证明 workflow **真能跑通**（plumbing 正确、契约对齐、setup/gate/train 三节点的确定性
> 后端脚本都能消费 demo 产出），**不追求模型精度**——随机数据下 NMSE 无物理意义（~1.0 = 无信息），
> 故 `accuracy_baseline` 设宽松值（1.5）让多数变体通过精度门。这是**精度阈值的宽松**，**不是测量造假**：
> NMSE / latency 永远是真实算/测出来的。

## 目录结构

```
examples/kd-nas-demo/
├── README.md                                    # 本文件（inputs 全集 + orca run 示例 + 自验）
├── test_smoke.py                                # 契约 smoke（pytest/直跑；变体 I/O 契约 + hook + 前向 shape）
├── baseline_model.py                            # 4 层 baseline（契约文件：时延参考 + gpu_probe representative）
├── train_teacher.py                             # 【v4 退役·历史参考】训 10 层 teacher（teacher_model.py 架构）→ ckpt；不再是 workflow 入口
├── train.py                                     # 用户任务 loss+dataloader；即 inputs.user_train_script（train-script-gen 自包含搬移）
├── test_student.py                              # 读 STUDENT_CKPT env，算 NMSE 打印 STUDENT_ACCURACY 等；kd-train-script 自动发现并移植进 train_pipeline --mode eval（取代旧 test_command input）
├── latency_provider.py                          # measure(onnx,...) onnxruntime 实测；即 latency_provider 值
└── knowledge_base/
    └── families/
        └── receiver/
            ├── _demo_blocks.py                  # 【共享·简化原创】ReceiverShell 基类 + TF/CNN block（非 _model8_blocks 复制）
            ├── _model8_student_blocks.py        # 【共享·真实架构】SignalProcessingTransformer（逐字对齐 teacher_model.py，+norm_type/act_type 开关）
            ├── 00_model8_bn3relu.py             # 【主变体】真实 model8 + BN + 3层 + ReLU（字典序最小，gate 枚举排首位）
            ├── 01_model8_bn3gelu.py             # 变体：真实 model8 + BN + 3层 + GELU
            ├── 02_model8_ln3relu.py             # 变体：真实 model8 + LN + 3层 + ReLU
            ├── 03_model8_bn4relu.py             # 变体：真实 model8 + BN + 4层 + ReLU
            ├── demo_tiny_tf.py                  # 变体：简化 全 t1 transformer（2 层）
            ├── demo_tiny_alt.py                 # 变体：简化 t1/t2 交替 transformer（3 层 / 中等）
            ├── demo_tiny_cnn_pw.py              # 变体：简化 全 CNN pointwise（2 层）
            └── demo_tiny_cnn_dil.py             # 变体：简化 全 CNN dilated（2 层）
```

### 关于 `_demo_blocks.py`（非复制 `_model8_blocks.py`）

真实 KB 变体 `from _model8_blocks import ...` 共享积木；demo 变体照搬这个模式，但共享的是
**demo 专用、原创简化的** `_demo_blocks.py`（`ReceiverShell` 基类 + 去 LayerNorm 的 pointwise
attention/FFN + 简化 CNN block）。结构与 `_model8_blocks.py` 不同，代码原创，目的是更小更快。
I/O 契约与真实 model8 完全一致（`[B,4,48,64,1]` + alpha 功率归一 + 出口 `*alpha`）。
（symlink 共享 `_model8_blocks.py` 因 git `core.symlinks=false` + Windows checkout 风险被排除。）

## 各文件作用

| 文件 | workflow 角色 | 作用 |
|---|---|---|
| `baseline_model.py` | `inputs.baseline_model_path` | 4 层 t1 transformer 契约文件。flatten 展平成 KD 契约 + `__main__` 实测 `baseline_latency_ms`（setup 透传作参考线）；gpu_probe 作 representative_variant |
| `train_teacher.py` | （v4 退役·历史参考） | 仓库 `teacher_model.py`（10 层 t1/t2 交替）的独立训练脚本。**不再是 workflow 入口**——v4 起 teacher 训练由 train-script-gen 产出的 `train_pipeline.py --mode teacher` 驱动（setup step5 调） |
| `train.py` | `inputs.user_train_script`（train-script-gen 读） | 暴露 `compute_loss`(MSE) + `build_dataloader`(随机 batch)；train-script-gen 实例化骨架模板并**逐字搬入**其 loss/dataloader/optimizer 进 `train_pipeline.py`（零占位符产物，无 `user_train_import` 运行时注入） |
| `test_student.py` | kd-train-script 自动发现（取代旧 `inputs.test_command`） | 读 `STUDENT_CKPT`/`STUDENT_MODEL_PATH` env，import 变体 + load ckpt，随机数据算真实 NMSE，打印契约 stdout；指标被移植进 train_pipeline `--mode eval` |
| `latency_provider.py` | `inputs.latency_provider`（`<abs>::measure`） | onnxruntime 实跑 ONNX 取中位数时延（ms），签名 `measure(onnx, runs, warmup, device, seed)` |
| `test_smoke.py` | （守护） | 机器化契约 smoke（pytest）：8 变体 + baseline 的 I/O 契约 + hook 数 + 前向 shape |
| `knowledge_base/families/receiver/00-03_model8_*.py` | KB 变体（gate 枚举 + train 蒸馏对象） | **4 个真实 model8 架构 student**（BN/LN × ReLU/GELU × 3/4 层；字典序最小，gate 排首位）。`_model8_student_blocks.py` 共享积木逐字对齐 `teacher_model.py`，仅多 `norm_type`/`act_type` 开关 |
| `knowledge_base/families/receiver/demo_tiny_*.py` | KB 变体（同上） | **4 个简化原创 student**（`_demo_blocks.py`；TF/CNN 混合），让 E2E 在 CPU 分钟级跑通。与 model8 共 8 变体，gate 全枚举 |

## kd-nas inputs 全集

`workflows/kd-nas.yaml` 的 inputs（必填 6 + 可选若干）。**demo 推荐值**：

| input | 值 | 说明 |
|---|---|---|
| `user_train_script` | `<REPO>/examples/kd-nas-demo/train.py` | 用户原 train.py（含 `compute_loss` + `build_dataloader`）；train-script-gen 读它生成 train_pipeline.py（自包含搬 loss/dataloader + 从 test_student.py 发现移植 eval 指标进 `--mode eval`） |
| `latency_provider` | `<REPO>/examples/kd-nas-demo/latency_provider.py::measure` | 绝对路径 `::func`（必填无默认） |
| `baseline_model_path` | `<REPO>/examples/kd-nas-demo/baseline_model.py` | 绝对路径（flatten 展平成 KD 变体契约） |
| `target_latency_ms` | `5.0` | 宽松门：student 变体 CPU 实测 0.2~1.1ms，均通过；baseline 6.6ms（不卡门） |
| `accuracy_baseline` | `1.5` | NMSE 基线（越低越好）。随机数据下变体 NMSE ~1.0~1.2，故 1.5 让多数通过 |
| `accuracy_baseline_kind` | `nmse` | 精度方向（**必填**，越低越好 best=min）。demo 用 NMSE；measure_student / viz_kd / kd-select 三处同源判定 |
| `device` | `cpu` | demo CPU 可跑（GPU 路径后续测试）；device=cpu 时 gpu_probe 自动 fail-soft 到 concurrency=1 |
| `full_epochs` | `1` | 每变体蒸馏 1 epoch（demo 求快） |

> v4 变更（2026-07-31）：input `teacher_train_command` 改名 `user_train_script`（用户原 train.py 路径，
> 给 train-script-gen 读）。teacher 训练改由 train-script-gen 产出的 `train_pipeline.py --mode teacher`
> 驱动（不再原样跑用户命令）；`train_teacher.py` 退役作历史参考（teacher_model.py 架构的独立训练脚本，
> 不再是 workflow 入口）。`seed` / `kd_artifacts_dir` 已下沉到下游 CLI 默认（不再是 workflow input）。
> `accuracy_baseline_kind` 仍是**必填 input**（无默认——防 dB 反向错误，BLK-3/10）。

> `<REPO>` = Orca 仓库根的绝对路径（例 `/mnt/d/Projects/Orca`）。`latency_provider` /
> `baseline_model_path` 用绝对路径最稳（也可相对 PROJECT_ROOT，但 gate/train 节点 cwd 可能变）。

### 实测参考值（CPU：torch 2.13.0+cpu / onnxruntime 1.27.0）

| 模型 | latency(ms) | 默认 cfg |
|---|---|---|
| baseline_model（4 层 t1） | 6.65 | num_blocks=4, embed_dim=12 |
| teacher（10 层 t1/t2，teacher_setup 实测） | 38.85 | teacher_model 默认 |
| **00_model8_bn3relu**（真实 model8 + BN + 3层 + ReLU） | 1.08 | num_blocks=3, embed_dim=16 |
| **01_model8_bn3gelu**（真实 model8 + BN + 3层 + GELU） | 1.10 | num_blocks=3, embed_dim=16 |
| **02_model8_ln3relu**（真实 model8 + LN + 3层 + ReLU） | 2.95 | num_blocks=3, embed_dim=16 |
| **03_model8_bn4relu**（真实 model8 + BN + 4层 + ReLU） | 1.33 | num_blocks=4, embed_dim=16 |
| demo_tiny_tf（2 层 t1） | 0.52 | num_blocks=2, embed_dim=8 |
| demo_tiny_alt（3 层 t1/t2） | 1.11 | num_blocks=3, embed_dim=8 |
| demo_tiny_cnn_pw（2 层 pointwise） | 0.23 | num_blocks=2, embed_dim=8 |
| demo_tiny_cnn_dil（2 层 dilated） | 0.33 | num_blocks=2, embed_dim=8 |

> model8 四变体 latency 为 gate_all 实测中位数（2026-08-01，CPU，`measure_repeats=2`；embed_dim=16 默认 cfg，
> 均远低于 5.0ms target，**gate 无需缩容直接 ACCEPTED**）。**关键结论**：model8 BN/LN 轻量化路径在 CPU 上
> 时延达标——BN 变体 ~1.1ms、LN 变体 ~3.0ms（LN 慢于 BN，符合预期），证实 model8 是可行的真实 student。
> 真实 NPU/GPU 部署须用用户 `latency_provider` 重测（本表为 ONNXRT-CPU 参考值）。

随机数据 NMSE（untrained / 1-epoch KD）~1.0~1.2（teacher 1-epoch 随机训练 loss ~1.56；KD 1-epoch loss ~1.97）。

## 如何跑（E2E）

### 1. 设 KB 目录（关键）

kd-nas workflow 的 setup/gate 用 `$ORCA_KB_DIR/families/receiver` 枚举变体。**必须**指向 demo KB：

```bash
export ORCA_KB_DIR="$(git rev-parse --show-toplevel)/examples/kd-nas-demo/knowledge_base"
```

### 2. orca run

```bash
REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

orca run workflows/kd-nas.yaml --inputs '{
  "user_train_script": "'"$REPO"'/examples/kd-nas-demo/train.py",
  "latency_provider": "'"$REPO"'/examples/kd-nas-demo/latency_provider.py::measure",
  "baseline_model_path": "'"$REPO"'/examples/kd-nas-demo/baseline_model.py",
  "target_latency_ms": "5.0",
  "accuracy_baseline": "1.5",
  "accuracy_baseline_kind": "nmse",
  "device": "cpu",
  "full_epochs": "1"
}'
```

> input 严格匹配 `workflows/kd-nas.yaml` 的 8 个 input（6 必填 + 2 可选）。**已删字段**（传了会 input
> 校验失败）：`teacher_train_command`（→ `user_train_script`）、`seed`、`kd_artifacts_dir`（后两者下沉为
> 下游 CLI 默认）。`accuracy_baseline_kind` 仍**必填**。

预期：flatten（baseline → KD 契约 + baseline_latency）→ teacher_gen（派生 teacher + teacher_latency）→
train_script_gen（产 `train_pipeline.py`）→ setup（训 teacher + teacher_setup 产 cache + gpu_probe fail-soft）→
gate（串行 **8 变体** tune_latency → manifest）→ train（KD 蒸馏 accepted 变体 → ledger SUCCESS 行）→ select。

> demo **不在本 README 里跑 orca E2E**——那是后续 test-agent 的事。下方「组件自验」已证明
> setup/gate/train 三节点的确定性后端脚本都能消费 demo 产出（契约对齐 + 集成通）。

## 组件自验（本 demo 已全过）

### A. 契约 smoke（机器化，pytest / 直跑）

```bash
pytest examples/kd-nas-demo/test_smoke.py -v          # 8 变体 + baseline = 9 路径 × 4 断言
# 或：python3 examples/kd-nas-demo/test_smoke.py
```
覆盖：**8 变体（4 model8 + 4 demo_tiny）+ baseline** 的 `DUMMY_INPUT`/`BUILD_FN`/`KNOBS` schema
（step<0, leverage 合法, num_blocks.min≥2）、`build_model()` 默认前向 + 输出同形、
`feature_hook_names` 恒 2 个且 hook 名真实存在。

### B. workflow 集成脚本（三节点的确定性后端，逐个单跑通）

```bash
REPO="$(git rev-parse --show-toplevel)"; cd "$REPO"
mkdir -p /tmp/kd_demo

# B1. setup 前置：训 teacher（5.5s）+ ckpt 能被 teacher_model.build_model() 零 missing/unexpected 加载
python3 examples/kd-nas-demo/train_teacher.py --out /tmp/kd_demo/teacher_ckpt.pt --epochs 1

# B2. setup step5：teacher_setup 消费 teacher ckpt → teacher_cache.pt + teacher_meta + ONNX + latency
python3 workflows/agents/_kd_scripts/teacher_setup.py \
  --teacher_model_path workflows/agents/_kd_scripts/teacher_model.py \
  --teacher_ckpt /tmp/kd_demo/teacher_ckpt.pt --build_fn build_model \
  --dummy_input '{"shape":[1,4,48,64,1],"dtype":"float32"}' \
  --output_dir /tmp/kd_demo/teacher_setup_out --opset 17 \
  --latency_provider "$REPO/examples/kd-nas-demo/latency_provider.py::measure" \
  --device cpu --seed 0
# 期望: TEACHER_LATENCY_MS / TEACHER_CACHE / TEACHER_META 行齐全，exit 0

# B3. gate 核心：tune_latency 对 demo 变体（gate_all 内部调它）
python3 workflows/agents/_kd_scripts/tune_latency.py \
  --variant_path "$REPO/examples/kd-nas-demo/knowledge_base/families/receiver/demo_tiny_tf.py" \
  --build_fn build_model --dummy_input '{"shape":[1,4,48,64,1],"dtype":"float32"}' \
  --knobs '{"num_blocks":{"default":2,"min":2,"step":-1,"leverage":"high"},"embed_dim":{"default":8,"min":4,"step":-2,"leverage":"medium"}}' \
  --target_latency_ms 5.0 \
  --latency_provider "$REPO/examples/kd-nas-demo/latency_provider.py::measure" \
  --artifacts_dir /tmp/kd_demo/tune_test --device cpu --max_measurements 5 --measure_repeats 2
# 期望: TUNE_STATUS: ACCEPTED + ACCEPTED_CFG + LATENCY_MS_MEDIAN

# B4. train 核心：train_adapter KD 蒸馏（train_pool 硬编码 OFD+EMA kd_config；deprecated 模板脚本级自验）
python3 workflows/agents/_kd_scripts/train_adapter_template.py \
  --student_cfg '{"num_blocks":2,"embed_dim":8}' \
  --kd_config '{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}' \
  --teacher_cache /tmp/kd_demo/teacher_setup_out/teacher_cache.pt \
  --student_model_path "$REPO/examples/kd-nas-demo/knowledge_base/families/receiver/demo_tiny_tf.py" \
  --build_fn build_model --variant_id demo_tiny_tf --epochs 1 \
  --out_ckpt /tmp/kd_demo/demo_tiny_tf.pt --device cpu --seed 0
# 期望: STUDENT_CKPT / KD_LOSS_FINAL / KD_PROXY_MSE 三行齐全，exit 0（render_chart WARN 是设计内非阻断）

# B5. setup step8：gpu_probe（device=cpu → fail-soft concurrency=1）
python3 workflows/agents/_kd_scripts/gpu_probe.py \
  --teacher_cache /tmp/kd_demo/teacher_setup_out/teacher_cache.pt \
  --representative_variant "$REPO/examples/kd-nas-demo/baseline_model.py" \
  --variants_count 4 --device cpu
# 期望: CONCURRENCY: 1 + DEVICE_PLAN: [""] + exit 0

# B6. measure_student 端到端（test_command + latency_provider + accuracy_baseline）
python3 workflows/agents/_kd_scripts/measure_student.py \
  --student_model_path "$REPO/examples/kd-nas-demo/knowledge_base/families/receiver/demo_tiny_tf.py" \
  --student_ckpt /tmp/kd_demo/demo_tiny_tf.pt --build_fn build_model \
  --build_cfg '{"num_blocks":2,"embed_dim":8}' \
  --dummy_input '{"shape":[1,4,48,64,1],"dtype":"float32"}' \
  --eval_command "python3 $REPO/examples/kd-nas-demo/test_student.py" \
  --accuracy_baseline 1.5 --accuracy_baseline_kind nmse \
  --latency_provider "$REPO/examples/kd-nas-demo/latency_provider.py::measure" \
  --target_latency_ms 5.0 --output_dir /tmp/kd_demo/measure --device cpu --seed 0 \
  --project_root "$REPO"
# 期望: STUDENT_LATENCY_MS / STUDENT_ACCURACY / MET_ACCURACY: true / ACCURACY_CONFIDENCE: high
```

> B4 手跑的是已退役的 `train_adapter_template.py`（脚本级自验参考，走其自身历史 placeholder
> 回退 MSE + 随机 batch）。**真实用户 loss 搬入由 `train_pipeline.py` 生成流程承担**：kd-train-script
> 实例化骨架模板 + 逐字搬入 `inputs.user_train_script` 的 `compute_loss` / `build_dataloader`
> （零占位符产物，无 `--user_train_import` 注入——该 flag 已随占位符体系删除）。

## 设计决策

1. **`_demo_blocks.py` 而非复制 `_model8_blocks.py`**：CLAUDE 指示「不要复制 `_model8_blocks.py`」。
   采用 demo 本地原创简化积木（`ReceiverShell` 基类 + 简化 block），既满足 DRY（4 变体共享）又
   非复制（结构/代码独立）。symlink 因 `core.symlinks=false` + Windows checkout 风险被排除。
2. **student 变体 `from _demo_blocks import ...`**：与真实 KB 变体 `from _model8_blocks import ...` 同款模式（一致）。
3. **`test_student.py` 末行打印 `{"nmse": X}` JSON**：measure_student 的 `_parse_accuracy` 反向扫
   stdout 优先命中 JSON 行 → 稳定检 kind=nmse（配合 `accuracy_baseline_kind=nmse` 锁方向，无 WARN）。
   `STUDENT_ACCURACY: X` 行仅人类可读（measure_student 不直接解析，因 JSON 行优先）。
4. **`accuracy_baseline=1.5`（宽松）**：随机数据 NMSE 无物理意义，宽松阈值让 workflow 跑出 SUCCESS。
   这是**阈值宽松**，非测量造假——NMSE 永远真实计算。真实数据集场景应换成业务 KPI（如 0.02）。
5. **demo `train.py`（消除 `user_train_script` 非确定性）**：train-script-gen 读 `inputs.user_train_script`
   实例化骨架模板并逐字搬移 loss/dataloader/optimizer（5 个固定 `user_*` slot）。demo 显式提供
   `train.py`（`compute_loss` + `build_dataloader`），让 train-script-gen 确定性地搬出真实 loss。
   **train_pipeline 是骨架特化产物**：slot 未搬入即 NotImplementedError fail loud（无 placeholder
   回退）；B4 路径（`train_adapter_template.py` 的历史 placeholder 回退）仅作脚本级自验参考，
   非 train_pipeline 行为。
6. **KNOBS `num_blocks.min=2`**：保 `feature_hook_names` 两 hook 落在 distinct block（main.0/main.1），
   避免 num_blocks=1 时 hook 重复致 KD OFD 特征对齐退化。tune_latency 不缩到 1 块。
7. **teacher 训练不求收敛**：随机数据 + 1 epoch，teacher 只作 KD 软标签源（精度基线用户另给）。
   ckpt 真实前向+反传产出，能被 `teacher_model.build_model()` 零 missing/unexpected 加载。

## 已知观察（非 demo 缺陷）

- 仓库 `workflows/agents/_struct_scripts/latency_onnxrt.py` 的 `rng.standard_normal(*shape)` 在
  numpy 2.x 对多维 shape 会报 `TypeError`（5 positional args）。demo 的 `latency_provider.py` 用
  `size=shape` 规避（已验通）。仓库原版 bug 超出本任务范围，未顺手改（非 surgical 修复）。
- ONNX 导出有 `TracerWarning`（`if inp.dim()==5 and inp.shape[-1]==1` data-dependent 分支），与真实
  `_model8_blocks.py` / `teacher_model.py` **同款**——`DUMMY_INPUT.shape[-1]==1` 恒真，分支单边走，
  tracer 警告不影响推理正确性。对 demo 可接受。
