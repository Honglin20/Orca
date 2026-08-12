---
name: rx-sweep
description: >-
  RX-SWEEP —— 把一个 OFDM 无线接收机训练工程（train.py 导入 utils/ 训练脚本的结构）
  适配成可批量跑「多方案 × {从头训, 蒸馏}」实验矩阵：rx_models 包提供 7 个方案
  （model8_trf baseline / pure_cnn 纯 CNN / cnn_trf_alt CNN+TRF 交替 / feat_complex
  复数卷积前端 / feat_diff 差分先验 / feat_fft 频域 FFT / feat_adjbeam 邻波束前端），
  8 卡并行跑，检验门把关，结果推图到 web UI，可导昇腾友好 ONNX。
  先 Read reference/contracts.md + experiment-matrix.md + adaptation-guide.md 再动手。
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# RX-SWEEP

<purpose>
你运行在主 session 里。用户的一句话意图（如「把我的接收机多方案全跑一遍」「跑 rx-sweep」）触发你。
你的职责：把用户的训练工程**适配**成可批量实验，**检验门**把关，**8 卡并行**跑完 rx_models 的所有方案
（每个从头训 + 蒸馏两版），收结果**推图**。你不替用户训模型——你驱动固化脚本 + 对用户代码做适配判断。
</purpose>

## 前置：先读契约

动手前 Read 这三份（本 skill 同目录），它们是单一真相源：

- `reference/contracts.md` —— 所有组件接口、GATE 格式、results schema、脚本 CLI。
- `reference/experiment-matrix.md` —— 实验矩阵（variant × {scratch,kd}）。
- `reference/adaptation-guide.md` —— 适配用户 train.py 的具体做法。

接口以 contracts.md 为准；遇冲突以它为裁决。

## 四阶段流程

### 阶段 1：定位工程 + 适配

1. 找用户的训练工程根：扫 `train.py`（入口，导入 `utils/<实际训练脚本>.py`）。问用户给路径，或 glob 找。
2. 把交付模型拷进工程（**整包拷 rx_models + kd_helper**；runner 自举 sys.path 让 `import rx_models` 可解）：
   ```bash
   mkdir -p <工程>/rx_sweep_models
   cp -r "$ORCA_AGENT_RESOURCES/scripts/models/rx_models" <工程>/rx_sweep_models/
   cp    "$ORCA_AGENT_RESOURCES/scripts/models/kd_helper.py" <工程>/rx_sweep_models/
   ```
   拷完结构：`<工程>/rx_sweep_models/rx_models/...` + `<工程>/rx_sweep_models/kd_helper.py`。
3. 适配训练入口接受 `--model <name>` / `--kd` / `--teacher-ckpt <p>`，并在载入模型后、训练前打印一行 `[RX-GATE] ...`（格式见 contracts.md §3）。两种方式：
   - 优先跑固化脚本：`python "$ORCA_AGENT_RESOURCES/scripts/adapt_project.py" --project-root <工程>`（幂等，自动生成 `rx_runner.py` 包装器，用 `rx_models.get_model(name, RxConfig)` 实例化）。
   - 脚本够不到的结构（用户训练脚本特殊）→ 按 `adaptation-guide.md` 手工适配（Edit 用户的 utils/ 脚本）。
4. **维度注入（重要，根治 64/32 漂移）**：runner 默认 `RxConfig(num_symbols=32, ...)`；若用户工程 cfg 有 `beam_num` / `time_wnd_len_pre` / `time_wnd_len_aft`，在 runner 的 `<<<TODO>>> cfg 构造点` 改成 `RxConfig.from_project_cfg(<用户cfg>)`，让维度从用户工程一处流向模型。
5. **KD 前提**：蒸馏实验需 teacher（用户训好的原模型 ckpt）。问用户拿 teacher_ckpt 路径；没有 → KD 实验标 SKIP，仍跑 scratch。

> 用户原训练逻辑（loss/optimizer/scheduler/eval metric/数据管道）是**不可替代权威**，逐字保留，只加 model 选择 + GATE 打印，绝不替换用户的 loss/metric（见 adaptation-guide.md 铁律）。

#### 方案矩阵（rx_models 7 方案）

| name | 类型 | 说明 | 昇腾友好 | ONNX 可导出 |
|---|---|---|---|---|
| `model8_trf` | baseline | 原 attention 主干（SignalTransformerBlock） | 一般（MatMul/Softmax） | 是 |
| `pure_cnn` | 纯 CNN | DualAxisConvBlock（频 + 时间 dilated conv），替 attention | 是（纯 Conv） | 是 |
| `cnn_trf_alt` | CNN+TRF 交替 | 按 `cnn_trf_pattern` 轮换堆叠两种 block | 一般 | 是 |
| `feat_complex` | B1 复数卷积前端 | 复数卷积前端 + CNN 主干 | 是 | 是 |
| `feat_diff` | B2 差分先验前端 | 差分先验前端 + CNN 主干 | 是 | 是 |
| `feat_fft` | B3 频域 FFT 前端 | FFT 前端（Vector 算子，验精度非降时延） | 一般 | **否**（`aten::fft_fft` 不支持） |
| `feat_adjbeam` | B4 邻波束前端 | 邻波束拼接前端 + Conv2d 主干 | 是 | 是 |

统一 I/O `[B, P, F, S, 1]`，`RxConfig.io_shape=[1,P,F,S,1]`。维度 P/F/S 由 `RxConfig` 单一真相源管（默认 P=4 F=48 S=32；`RxConfig.from_project_cfg(用户cfg)` 从用户工程 cfg 推）。所有方案 `forward` 不动训练/数据代码——特征变换在模型内部。

### 阶段 2：检验门（全过才进 sweep）

对矩阵里每个 model 跑 1 步检验：

```bash
python "$ORCA_AGENT_RESOURCES/scripts/gate_check.py" \
  --project-root <工程> --model <name> [--kd --teacher-ckpt <p>] [--gpu <N>]
```

读 stdout 的 `[GATE-RESULT] model=... passed=true|false reason=...`。
**任一 model `passed=false` → 停，把 reason 报给用户**，不进 sweep。常见原因：I/O shape 不对齐、方案名拼错、cfg 维度与用户工程不符、前向/backward 崩。

### 阶段 3：起 daemon + 建矩阵 + 8 卡并行 sweep

定 sweep 工作目录 `$WORK`（results / tb 落此）。先起 live 推图用的 chart daemon（纯 skill 调用没有 workflow run，需自起；默认建到 `~/.orca/runs/`，web 列表能扫到 chart_count）：

```bash
WORK=<工程>/.rx-sweep
python "$ORCA_AGENT_RESOURCES/scripts/ensure_chart_daemon.py"            # 起 session daemon + 写 ~/.orca/runs/orca_env.sh（幂等：活着就不重起）
source ~/.orca/runs/orca_env.sh                                          # 让阶段 4 push_results 的 render_chart 拿到 ORCA_* env
python "$ORCA_AGENT_RESOURCES/scripts/build_matrix.py" --out "$WORK/matrix.json"
python "$ORCA_AGENT_RESOURCES/scripts/launch_sweep.py" \
  --project-root <工程> --matrix "$WORK/matrix.json" --gpus 8 \
  --results "$WORK/results.jsonl" --tb-dir "$WORK/tb" [--teacher-ckpt <p>]
```

- ensure_chart_daemon 在 **orca 侧**（需 orca + pydantic）跑。若当前机器没装 orca（如裸训练服务器），它 fail loud exit 1——**不阻断 sweep**，阶段 4 推图回退 fallback JSON。
- launch_sweep 自动把实验分到 `CUDA_VISIBLE_DEVICES=0..7`，8 个一批并行。**每实验写 TensorBoard 到 `$WORK/tb/<variant>/`**（loss 曲线）。逐实验打印 `[SWEEP]`，末尾 `SWEEP_DONE`。
- `--python` 默认 `sys.executable`（子进程用同一解释器，含 torch/tensorboard；**别用裸 `python`**——WSL 上可能解析到缺包解释器，TB 静默不写）。
- 某实验 FAIL（gate/train/eval）不阻断其它——status 记入 jsonl，继续。

### 阶段 4：可视化 + 汇报

**训练过程（推荐，TensorBoard）**——每实验 loss/epoch 曲线，多实验并排对比：

```bash
tensorboard --logdir "$WORK/tb" --port 6006
```

**结果汇总（orca web，可选）**——最终 accuracy×latency（pareto）+ model 对比（bar）+ 总表：

```bash
python "$ORCA_AGENT_RESOURCES/scripts/push_results.py" --results "$WORK/results.jsonl"
```

- 阶段 3 已 `source orca_env.sh` → push_results 推 3 张图到 web（`orca open` 列表能看到该 run 的 chart_count）。
- ⚠️ **已知限制**：纯 skill 造的 run 在 web **列表能显示 chart 数**，但**点击详情视图**因 orca-web 的 run 解析机制（为真 workflow run 设计）可能打不开（`/events` 404）。**训练过程看 TensorBoard 更可靠**；web 图作结果汇总参考。
- daemon 没起 / orca 未装 → push_results fail-soft 落 `chart.json` + `CHART_FALLBACK_JSON`（不崩）。
- 给用户一句话总结：哪个 model 精度最高、latency 最低、KD 相比 scratch 增益多少。

### 阶段 4b：导出 ONNX（昇腾部署，可选）

挑表现最好的 model 导单文件 ONNX（无 `.data`、static shape、opset=13，昇腾 ATC 友好）：

```bash
cd <工程>
python rx_sweep_models/rx_models/export_onnx.py \
  --model <name> --num-symbols 32 --out <name>.onnx
# 或完整 RxConfig JSON 覆盖单项维度
python rx_sweep_models/rx_models/export_onnx.py \
  --model feat_complex --cfg-json '{"num_symbols":32,"num_blocks":4}' --out feat_complex.onnx
```

- 单文件、权重内联（`save_as_external_data=False`）、`model.eval()`、`do_constant_folding=True`。
- 算子清单会打印到 stdout：一眼看到 `model8_trf` 的 MatMul/Softmax、`feat_fft` 的 DFT（Vector 算子）、`pure_cnn` 的纯 Conv（目标形态）。
- ⚠️ **`feat_fft` 不可导 ONNX**：`torch.onnx` 不支持 `aten::fft_fft`。需 FFT 方案上昇腾时，改用 `pure_cnn` / `feat_complex` / `feat_diff` / `feat_adjbeam` 等可导方案。

## 硬规则

- **H1 检验门是硬门**：阶段 2 任一 model 不过 → 不进 sweep，不静默继续。
- **H2 用户逻辑即权威**：不替换用户的 loss/optimizer/scheduler/eval metric/数据管道（adaptation-guide.md 铁律）。
- **H3 fail loud**：脚本遇契约不符输入非零退出 + stderr 报因；你不悄悄重跑。
- **H4 推图是 sidecar**：ensure_chart_daemon / render_chart 失败只 stderr + 回退 fallback JSON，绝不阻断 sweep。live 推图需 orca 侧（ensure_chart_daemon + push_results 都在装了 orca 的机器跑）。
- **H5 测试先**：拿不准适配是否对时，先在 `fixture/`（本 skill 自带假 model8 工程）跑一遍 adapt→gate→sweep→push 验证脚本链路，再上用户工程。

## 用 fixture 自检（工程测试）

> ⚠️ **冲突说明（rule 7：surface, don't average）**：`fixture/` 仍是**旧式 pure_cnn_model 单文件工程**（自带 `pure_cnn_model.py` + `--variant` 入口），尚未迁移到 `rx_models` 包——这是已知缺口，待 fixture 单独迁移。下方命令因此仍用 `--variant`/`pure_cnn_model` 旧契约（对当前 fixture 真实可用）；**生产链路（阶段 1–4）一律用 `--model` + `rx_models`**，别被这里的旧命令误导。迁移 fixture 时本段同步改成 `--model`。

本 skill 带 `fixture/`（假 model8 + 假 train.py + 合成数据）。验证脚本链路时：

```bash
cd "$ORCA_AGENT_RESOURCES/fixture"
python train.py --variant pure_cnn --epochs 1   # 旧式 fixture：应打印 [RX-GATE] ... gate=PASS
python "$ORCA_AGENT_RESOURCES/scripts/gate_check.py" --project-root "$ORCA_AGENT_RESOURCES/fixture" --model pure_cnn   # 注意：当前 fixture runner 仍要 --variant，此命令会报 model mismatch —— 迁移 fixture 后才通
python "$ORCA_AGENT_RESOURCES/scripts/build_matrix.py" --out /tmp/matrix.json
python "$ORCA_AGENT_RESOURCES/scripts/launch_sweep.py" --project-root "$ORCA_AGENT_RESOURCES/fixture" --matrix /tmp/matrix.json --gpus 1 --results /tmp/results.jsonl --tb-dir /tmp/tb
python "$ORCA_AGENT_RESOURCES/scripts/push_results.py" --results /tmp/results.jsonl
```

全跑通 = 脚本链路 OK，可上用户工程。（fixture 未迁移前，从第 2 步起可能因 `--variant`/`--model` 不一致而失败——以阶段 1–4 生产链路为准。）

<success_criteria>
- [ ] Read 了 contracts.md / experiment-matrix.md / adaptation-guide.md
- [ ] 拷 rx_models 包 + kd_helper.py 进用户工程 rx_sweep_models/
- [ ] 适配训练入口接受 --model/--kd/--teacher-ckpt + 打印 [RX-GATE] 行（用 rx_models.get_model）
- [ ] 维度注入：runner 用 RxConfig / RxConfig.from_project_cfg（根治 64/32 漂移）
- [ ] 阶段 2 每个 model gate_check 全 passed=true 才进 sweep（任一失败即停报用户）
- [ ] build_matrix + launch_sweep 8 卡并行跑完，results.jsonl 收齐
- [ ] push_results 推图（或 fallback JSON），给用户精度/latency/KD 增益总结
- [ ] 需部署时用 export_onnx 导昇腾友好 ONNX（feat_fft 例外）
- [ ] 未替换用户 loss/metric/数据管道
- [ ] 拿不准时先用 fixture 自检脚本链路
</success_criteria>
