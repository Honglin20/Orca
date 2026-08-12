# rx-sweep 接口契约（组件对齐圣经）

> 所有派发 agent 实现前必读本文件。接口签名以本文件为准；改接口 = 改本文件 + 通知依赖方。
> 本文件是**运行时 operational 规格**（接口/格式/schema），不是设计论证——保持洁净，禁开发期残留。

## 0. skill 布局

```
orca/skills/rx-sweep/
  SKILL.md                          # 运行时指令（主 agent 触发后照此执行）
  reference/
    contracts.md                    # 本文件
    experiment-matrix.md            # 实验矩阵定义（variant × {scratch,kd}）
    adaptation-guide.md             # 适配用户 train.py 的指南
  scripts/
    models/
      pure_cnn_model.py             # 交付物：拷进用户工程
      kd_helper.py                  # 交付物：拷进用户工程
    adapt_project.py                # 适配用户工程（加 --variant/--kd/--teacher-ckpt + GATE 打印）
    gate_check.py                   # 检验门：每 variant 跑 1 步验正确
    build_matrix.py                 # 生成实验矩阵 JSON
    launch_sweep.py                 # 8 卡并行跑实验，收 results.jsonl
    push_results.py                 # 读 results 推图（render_chart，fail-soft 落 JSON）
    ensure_chart_daemon.py          # 起 session chart daemon + 写 orca_env.sh（纯 skill live 推图，需 orca 侧）
  fixture/                          # 假 model8 工程（端到端测试用）
    train.py                        # 入口，导入 utils/train_rx.py
    utils/train_rx.py               # 确定性假训练脚本
    model8_baseline.py              # 假 model8（脱敏结构）
    data/gen.py                     # tiny 合成 OFDM 数据
    README.md
```

`tars install` 自动捡 `orca/skills/*/`（含 SKILL.md 即可），加本目录即装载，**零框架改动**。

## 1. 模型接口（pure_cnn_model.py）

```python
DUMMY_INPUT  = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
OUTPUT_SHAPE = [1, 4, 48, 64, 1]
BUILD_FN     = "build_model"

def build_model(**cfg) -> nn.Module   # 零参用默认；cfg 覆盖
```

**cfg 开关**：

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `variant` | str | `"pure_cnn"` | sugar：`pure_cnn` / `pure_cnn_pilot` / `pure_cnn_lmmse` / `pure_cnn_pilot_lmmse`。设了等价于下面三个开关的组合，显式开关 override |
| `num_blocks` | int | 4 | DualAxisConvBlock 数 |
| `embed_dim` | int | 16 | 通道数，**必须 ÷16**（昇腾 Cube 对齐） |
| `dilations` | tuple | `(1,2,4,8)` | 时间卷积 dilation，跨 block 轮换（block i 用 `dilations[i % len]`） |
| `use_pilot_enrich` | bool | False | pilot 富化 `[Y, Y⊙Xp*, Xp, mask]` |
| `use_lmmse` | bool | False | LMMSE 前置闭式均衡，NN 学残差 |
| `noise_var` | float | 1e-2 | LMMSE 的 σ² |
| `pilot_mask` | Tensor/None | None | `[num_ports, num_subcarriers, num_symbols]` bool，固定导频栅格 |
| `pilot_values` | Tensor/None | None | 同形，固定已知导频值 Xp |

**variant → 开关映射**：

| variant | use_pilot_enrich | use_lmmse |
|---|---|---|
| `pure_cnn` | False | False |
| `pure_cnn_pilot` | True | False |
| `pure_cnn_lmmse` | False | True |
| `pure_cnn_pilot_lmmse` | True | True |

**forward 契约**：`forward(inp: [B,4,48,64,1]) -> [B,4,48,64,1]`，内部 alpha 功率归一（`alpha=sqrt(mean(inp²)·2)`，出口 `*alpha`）——逐位对齐 model8。pilot 富化与 LMMSE **全在 forward 内部**（Y + 固定 pilot 配置即可算，I/O 不变）。

**顺序（重要）**：alpha 归一在 PilotEnrich/LMMSE **之前**——alpha 必须在原始 4 端口输入上算（与 model8/teacher 同功率尺度，KD/FitNets 对齐前提），不能在富化后的多通道张量上算。即 `x_norm = inp/(alpha+1e-6)` → optional PilotEnrich(x_norm) → stem → ... → optional `+LMMSE残差` → `*alpha`。

**结构**（DualAxisConvBlock）：频率分支 `Conv1d-k3 → BN → ReLU → Conv1d-k3`（混 3 邻域子载波，局部先验）+ 时间分支 `Conv1d-k3 dilation=d → BN → ReLU → Conv1d-k3 dilation=d`（混邻近 symbol，替 attention 的全局时间相关）+ 残差。**标准 dense conv，禁 DW**（昇腾 Cube 饿死）。

**KD hook**：`feature_hook_names(self) -> ["main.0", "main.<mid>"]`，恒 2 个（与 teacher 等长，FitNets 要求）。

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

适配后的训练入口启动时打印**一行**（agent / gate_check.py 据此判断）：

```
[RX-GATE] variant=pure_cnn pilot=on lmmse=off kd=off num_blocks=4 embed_dim=16 dilations=(1,2,4,8) noise_var=0.01 io_in=[1,4,48,64,1] io_out=[1,4,48,64,1] gate=PASS
```

字段：`variant` / `pilot`(on|off) / `lmmse`(on|off) / `kd`(on|off) / `num_blocks` / `embed_dim` / `dilations` / `noise_var` / `io_in` / `io_out` / `gate`(PASS|FAIL)。
**第一行就打印**（在载入模型后、训练循环前），`gate=PASS` 仅当 I/O shape 对齐 + 前向 smoke 通过。

## 4. results.jsonl schema（每实验一行）

```json
{"exp_id":"pure_cnn_scratch","variant":"pure_cnn","pilot":false,"lmmse":false,
 "kd":false,"num_blocks":4,"embed_dim":16,
 "accuracy":0.021,"accuracy_kind":"nmse","latency_ms":3.2,
 "gate_passed":true,"train_loss_final":0.015,"epochs":3,"gpu":0,
 "status":"SUCCESS","fail_reason":""}
```

`status` ∈ {SUCCESS, SKIP, FAIL_gate, FAIL_train, FAIL_eval}。`SKIP` = kd 实验但无 teacher_ckpt（不启）。`accuracy_kind` 由用户工程决定（nmse/ber/db/snr/mse/acc），方向（max/min）也由用户工程给。`train_loss_final` 未抓到时为 `null`（非 0.0，避免误读为真实零值）。

## 5. 脚本 CLI

| 脚本 | CLI | stdout 关键行 |
|---|---|---|
| `adapt_project.py` | `--project-root <dir> [--train-py <path>] [--dry-run]` | `ADAPTED: <runner_path>` / `DRYRUN` |
| `gate_check.py` | `--project-root <dir> --variant <v> [--kd] [--teacher-ckpt <p>] [--gpu N]` | `[GATE-RESULT] variant=... passed=true\|false reason=...`，exit 0/1 |
| `build_matrix.py` | `--out matrix.json [--variants ...] [--modes scratch,kd]` | `MATRIX: <path>` 含 N 实验 |
| `launch_sweep.py` | `--project-root <dir> --matrix matrix.json --gpus 8 --results results.jsonl [--runner <path>]` | 每实验 `[SWEEP] exp_id=... gpu=.. status=...`，末尾 `SWEEP_DONE: <path>` |
| `push_results.py` | `--results results.jsonl [--out chart.json]` | `CHART_PUSHED` 或 `CHART_FALLBACK_JSON: <path>` |
| `ensure_chart_daemon.py` | `--work-dir <dir> [--run-id <id>] [--ttl 86400]` | `DAEMON_READY run_id=.. sock=.. work_dir=.. env_file=.. results_path=<work-dir>/results.jsonl spawned=true\|false(已活)`（或 `DAEMON_FAILED`，exit 1） |

## 6. 实验矩阵

variants（纯 CNN 族 4 个 + model8 baseline 作参考）× {scratch, kd}：

| exp_id | variant | kd |
|---|---|---|
| model8_baseline | model8（仅 fixture 参考） | False |
| pure_cnn_scratch / pure_cnn_kd | pure_cnn | F / T |
| pure_cnn_pilot_scratch / _kd | pure_cnn_pilot | F / T |
| pure_cnn_lmmse_scratch / _kd | pure_cnn_lmmse | F / T |
| pure_cnn_pilot_lmmse_scratch / _kd | pure_cnn_pilot_lmmse | F / T |

共 9 实验（model8 不蒸馏）。8 卡并行：一批 8 个 + 余 1 个。KD 实验需 teacher_ckpt（= 训好的 model8）。

## 7. 推图契约（push_results.py）

- 读 results.jsonl → `render_chart(chart_type="pareto", x="latency_ms", y="accuracy", pareto_x_direction="min", pareto_y_direction=<依 accuracy_kind>)` + 一张 `bar`（variant × accuracy，hue=kd）+ 一张 `table`（总表）。
- label `rx-sweep/results`，title 各异。
- **render_chart 仅在 Orca 编排子进程可用**（需 `ORCA_*` env）。缺 env → fail-soft：写 `chart.json`（含 chart payload）+ 打印 `CHART_FALLBACK_JSON`，**不崩**。外层 try/except 包 render_chart，失败只 stderr。
- accuracy 方向（max/min）由 `accuracy_kind` 推：`{snr,acc}`=max / `{mse,nmse,ber,db}`=min。
- **live 推图前提（纯 skill）**：chart daemon 是 per-orca-run 的（由 `orca <wf>` bootstrap 起），纯 skill 调用没有 run → 需先跑 `ensure_chart_daemon.py --work-dir <dir>` 自起一个 session daemon + 写 `orca_env.sh`（4 身份键）。**`results.jsonl` 必须落该 work-dir 内**——push_results 的 `load_run_env_from_artifacts` 从 results 向上找 `orca_env.sh` 补 env。ensure_chart_daemon 在 orca 侧跑（需 orca + pydantic）；幂等（同 work-dir 派生同 run_id → 同 socket，probe 命中不重起）。已实测：render_chart → socket → daemon → tape 3 条 chart 事件端到端通。
