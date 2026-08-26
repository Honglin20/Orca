# rx-sweep 接口契约（组件对齐圣经）

> 所有派发 agent 实现前必读本文件。接口签名以本文件为准；改接口 = 改本文件 + 通知依赖方。
> 本文件是**运行时 operational 规格**（接口/格式/schema），不是设计论证——保持洁净，禁开发期残留。

## 0. skill 布局

```
orca/skills/rx-sweep/
  SKILL.md                          # 运行时指令（主 agent 触发后照此执行）
  reference/
    contracts.md                    # 本文件
    experiment-matrix.md            # 实验矩阵定义（model × {scratch,kd}）
    adaptation-guide.md             # 适配用户 train.py 的指南
  scripts/
    models/
      rx_models/                    # 交付物（多方案包）：整包拷进用户工程 rx_sweep_models/
        __init__.py                 #   registry + get_model + list_models
        config.py                   #   RxConfig（维度单一真相源）
        _base.py                    #   共享积木
        model8_trf.py pure_cnn.py cnn_trf_alt.py
        feat_complex.py feat_diff.py feat_fft.py feat_adjbeam.py
        export_onnx.py              #   单文件 ONNX 导出（昇腾友好）
      kd_helper.py                  # 交付物：拷进用户工程 rx_sweep_models/
      pure_cnn_model.py             # 【deprecated】旧单文件，仅为向后兼容保留
    adapt_project.py                # 适配用户工程（加 --model/--kd/--teacher-ckpt + GATE 打印）
    gate_check.py                   # 检验门：每 model 跑 1 步验正确
    build_matrix.py                 # 生成实验矩阵 JSON
    launch_sweep.py                 # 8 卡并行跑实验，收 results.jsonl
    push_results.py                 # 读 results 推图（render_chart，fail-soft 落 JSON）
    ensure_chart_daemon.py          # 起 session chart daemon + 写 orca_env.sh（纯 skill live 推图，需 orca 侧）
  fixture/                          # 假 model8 工程（端到端测试用，尚未迁移到 rx_models）
    train.py                        # 入口，导入 utils/train_rx.py
    utils/train_rx.py               # 确定性假训练脚本
    model8_baseline.py              # 假 model8（脱敏结构）
    data/gen.py                     # tiny 合成 OFDM 数据
    README.md
```

`tars install` 自动捡 `orca/skills/*/`（含 SKILL.md 即可），加本目录即装载，**零框架改动**。

## 1. 模型接口（rx_models 包）

> **迁移说明**：原 `pure_cnn_model.py` 单文件（`build_model(variant=...)` + 4 variant：`pure_cnn` / `pure_cnn_pilot` / `pure_cnn_lmmse` / `pure_cnn_pilot_lmmse`）**已废弃**（deprecation banner 见该文件顶部）。pilot 富化 / LMMSE 前置两优化点不再以 variant 开关形式存在；新工程一律用 `rx_models` 包的 `get_model(name, RxConfig)`。旧文件仅为已部署用户工程的向后兼容保留。

```python
from rx_models import get_model, list_models, RxConfig

cfg = RxConfig()                            # 默认 num_ports=4 num_subcarriers=48 num_symbols=32
cfg = RxConfig.from_project_cfg(user_cfg)   # 从用户工程 cfg 推（beam_num→num_symbols 等）
model = get_model(name, cfg)                # name ∈ list_models()
# 统一 I/O：
cfg.io_shape       # == [1, num_ports, num_subcarriers, num_symbols, 1]
cfg.dummy_input    # == {"shape": cfg.io_shape, "dtype": "float32"}
```

**已注册方案（7 个，`list_models()` 查）**：

| name | 类型 | 说明 | 昇腾友好 | ONNX 可导出 |
|---|---|---|---|---|
| `model8_trf` | baseline | 原 attention 主干（SignalTransformerBlock） | 一般（MatMul/Softmax） | 是 |
| `pure_cnn` | 纯 CNN | DualAxisConvBlock（频 + 时间 dilated conv），替 attention | 是（纯 Conv） | 是 |
| `cnn_trf_alt` | CNN+TRF 交替 | 按 `cnn_trf_pattern` 轮换堆叠两种 block | 一般 | 是 |
| `feat_complex` | B1 复数卷积前端 | 复数卷积前端 + CNN 主干 | 是 | 是 |
| `feat_diff` | B2 差分先验前端 | 差分先验前端 + CNN 主干 | 是 | 是 |
| `feat_fft` | B3 频域 FFT 前端 | FFT 前端（Vector 算子） | 一般 | **否**（`aten::fft_fft` 不支持） |
| `feat_adjbeam` | B4 邻波束前端 | 邻波束拼接前端 + Conv2d 主干 | 是 | 是 |

**RxConfig 字段**（维度单一真相源，根治原 64/32 漂移）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `num_ports` | int | 4 | P = polar × {re,im} |
| `num_subcarriers` | int | 48 | F = time_wnd_len_pre + time_wnd_len_aft |
| `num_symbols` | int | 32 | S = beam_num（工程实际值，非 64） |
| `num_blocks` | int | 4 | 主干 block 数 |
| `embed_dim` | int | 16 | 通道数，**必须 ÷16**（昇腾 Cube 对齐），`__post_init__` 校验 |
| `dilations` | tuple | `(1,2,4,8)` | 时间卷积 dilation，跨 block 轮换 |
| `noise_var` | float | 1e-2 | 噪声方差（相关方案读） |
| `adjbeam_k` | int | 3 | B4 邻波束窗口（奇数为佳） |
| `diff_orders` | tuple | `(1,2)` | B2 差分阶数集合 |
| `fft_axis` | str | `"F"` | B3 FFT 轴：`"F"` 子载波 / `"S"` 波束 |
| `cnn_trf_pattern` | tuple | `("cnn","trf")` | 方案2 交替模式（按 num_blocks 循环填充） |

**forward 契约（全方案一致）**：`forward(inp: [B,P,F,S,1]) -> [B,P,F,S,1]`，各方案内部自做 alpha 功率归一 + 特征变换，I/O 不变 → 不动训练/数据代码。`feature_hook_names()`（KD 用）由各方案按自身结构实现。

**导出 ONNX**：`python rx_models/export_onnx.py --model <name> --num-symbols 32 --out <name>.onnx`（static shape / opset=13 / 权重内联 / 单文件无 `.data`，昇腾 ATC 友好）。`feat_fft` 不可导（`aten::fft_fft`）。

## 2. KD 接口（kd_helper.py）

```python
class KDHelper:
    def __init__(self, teacher_build_fn, teacher_ckpt: str|Path,
                 student_hook_names: list[str], device,
                 alpha_out: float = 1.0, beta_feat: float = 0.5): ...
    def __call__(self, student, x, task_loss_fn, y) -> Tensor: ...
```

- `teacher_build_fn()` 返回 model8 实例；载 `teacher_ckpt` 的 `state_dict`；`.eval()` + 冻结参数（`requires_grad_(False)`）；在 teacher 的 `feature_hook_names()` 处注册 forward hook 收 `t_feats`。
- `__call__`：student 前向（hook 收 `s_feats`）→ teacher `no_grad` 前向（收 `t_feats`）→
  `loss = task_loss_fn(s_out, y) + alpha_out·MSE(s_out, t_out) + beta_feat·Σ_i MSE(adapter_i(s_feat_i), t_feat_i)`。
- **adapter 懒建**：首次调用据 `s_feat_i` / `t_feat_i` 形状自动建（同形→identity；通道不同→`nn.Conv1d/Linear` 投影）。adapter 参数计入 student 优化器（kd_parameters）。
- 纯 torch，自包含。
- teacher 前向在 `torch.no_grad()`，仅 student 拿梯度。

## 3. GATE 打印格式（检验门核心）

适配后的训练入口（`rx_runner.py`）启动时打印**一行**（agent / gate_check.py 据此判断）：

```
[RX-GATE] model=pure_cnn P=4 F=48 S=32 blocks=4 embed=16 io_in=[1,4,48,32,1] io_out=[1,4,48,32,1] gate=PASS
```

字段：`model` / `P` / `F` / `S` / `blocks` / `embed` / `io_in` / `io_out` / `gate`(PASS|FAIL)。
维度全从 `RxConfig` 读（M4：单一真相源，GATE 行与 `get_model` 共享一份 cfg，根治 64/32 漂移）。
**第一行就打印**（在载入模型后、训练循环前），`gate=PASS` 仅当 I/O shape == `cfg.io_shape` + 前向 smoke 通过。
`gate_check.py` 从同一行的 `P`/`F`/`S` 推期望 io（不硬编码，纯 stdlib 无需 import rx_models）。

## 4. results.jsonl schema（每实验一行）

```json
{"exp_id":"pure_cnn_scratch","model":"pure_cnn",
 "kd":false,"num_blocks":4,"embed_dim":16,
 "accuracy":0.021,"accuracy_kind":"nmse","latency_ms":3.2,
 "gate_passed":true,"train_loss_final":0.015,"epochs":3,"gpu":0,
 "status":"SUCCESS","fail_reason":""}
```

`status` ∈ {SUCCESS, SKIP, FAIL_gate, FAIL_train, FAIL_eval}。`SKIP` = kd 实验但无 teacher_ckpt（不启）。`accuracy_kind` 由用户工程决定（nmse/ber/db/snr/mse/acc），方向（max/min）也由用户工程给。`train_loss_final` 未抓到时为 `null`（非 0.0，避免误读为真实零值）。

## 5. 脚本 CLI

| 脚本 | CLI | stdout 关键行 |
|---|---|---|
| `adapt_project.py` | `--project-root <dir> [--train-py <path>] [--dry-run]` | `ADAPTED: <runner_path>` / `DRYRUN`（生成接受 `--model/--kd/--teacher-ckpt` 的 `rx_runner.py`） |
| `gate_check.py` | `--project-root <dir> --model <name> [--kd] [--teacher-ckpt <p>] [--gpu N]` | `[GATE-RESULT] model=... passed=true\|false reason=...`，exit 0/1 |
| `build_matrix.py` | `--out matrix.json [--variants ...] [--modes scratch,kd]` | `MATRIX: <path>` 含 N 实验 |
| `launch_sweep.py` | `--project-root <dir> --matrix matrix.json --gpus 8 --results results.jsonl [--runner <path>]` | 每实验 `[SWEEP] exp_id=... gpu=.. status=...`，末尾 `SWEEP_DONE: <path>` |
| `push_results.py` | `--results results.jsonl [--out chart.json]` | `CHART_PUSHED` 或 `CHART_FALLBACK_JSON: <path>` |
| `ensure_chart_daemon.py` | `--work-dir <dir> [--run-id <id>] [--ttl 86400]` | `DAEMON_READY run_id=.. sock=.. work_dir=.. env_file=.. results_path=<work-dir>/results.jsonl spawned=true\|false(已活)`（或 `DAEMON_FAILED`，exit 1） |

> ⚠️ **`build_matrix.py` / `launch_sweep.py` 仍用旧 `--variants` / `variant` API**（尚未迁移到 `--model`）。当前 `rx_runner.py`（新）只接受 `--model`，故 `launch_sweep` 调 runner 时会传一个 runner 不认的 `--variant`——这是已知缺口，迁移这两个脚本时同步改 `exp_id`/`variant` → `model`，并让它们调用 `get_model`。迁移前以 `adapt_project` + `gate_check` 手工驱动单方案为可靠路径。

## 6. 实验矩阵

models（rx_models 7 方案）× {scratch, kd}：

| exp_id | model | kd |
|---|---|---|
| model8_trf_scratch（/_kd） | model8_trf | F (/T) |
| pure_cnn_scratch / pure_cnn_kd | pure_cnn | F / T |
| cnn_trf_alt_scratch / _kd | cnn_trf_alt | F / T |
| feat_complex_scratch / _kd | feat_complex | F / T |
| feat_diff_scratch / _kd | feat_diff | F / T |
| feat_fft_scratch / _kd | feat_fft | F / T |
| feat_adjbeam_scratch / _kd | feat_adjbeam | F / T |

共 14 实验（baseline 亦参与 KD，因 model8_trf 可作 teacher 自比）。8 卡并行：两批。KD 实验需 teacher_ckpt（= 训好的 model8_trf 或用户原 model8）。`feat_fft` 的 KD 与训练照常，仅 ONNX 导出不可（见 §1）。

## 7. 推图契约（push_results.py）

- 读 results.jsonl → `render_chart(chart_type="pareto", x="latency_ms", y="accuracy", pareto_x_direction="min", pareto_y_direction=<依 accuracy_kind>)` + 一张 `bar`（model × accuracy，hue=kd）+ 一张 `table`（总表）。
- label `rx-sweep/results`，title 各异。
- **render_chart 仅在 Orca 编排子进程可用**（需 `ORCA_*` env）。缺 env → fail-soft：写 `chart.json`（含 chart payload）+ 打印 `CHART_FALLBACK_JSON`，**不崩**。外层 try/except 包 render_chart，失败只 stderr。
- accuracy 方向（max/min）由 `accuracy_kind` 推：`{snr,acc}`=max / `{mse,nmse,ber,db}`=min。
- **live 推图前提（纯 skill）**：chart daemon 是 per-orca-run 的（由 `orca <wf>` bootstrap 起），纯 skill 调用没有 run → 需先跑 `ensure_chart_daemon.py --work-dir <dir>` 自起一个 session daemon + 写 `orca_env.sh`（4 身份键）。**`results.jsonl` 必须落该 work-dir 内**——push_results 的 `load_run_env_from_artifacts` 从 results 向上找 `orca_env.sh` 补 env。ensure_chart_daemon 在 orca 侧跑（需 orca + pydantic）；幂等（同 work-dir 派生同 run_id → 同 socket，probe 命中不重起）。已实测：render_chart → socket → daemon → tape 3 条 chart 事件端到端通。
