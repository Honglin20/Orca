---
name: rx-sweep
description: >-
  RX-SWEEP —— 把一个 OFDM 无线接收机训练工程（train.py 导入 utils/ 训练脚本的结构）
  适配成可批量跑「优化点 × {从头训, 蒸馏}」实验矩阵：纯 CNN（attention→时间 dilated conv）、
  pilot 富化、LMMSE 前置等优化点逐一开关，8 卡并行跑，检验门把关，结果推图到 web UI。
  先 Read reference/contracts.md + experiment-matrix.md + adaptation-guide.md 再动手。
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# RX-SWEEP

<purpose>
你运行在主 session 里。用户的一句话意图（如「把我的接收机优化点全跑一遍」「跑 rx-sweep」）触发你。
你的职责：把用户的训练工程**适配**成可批量实验，**检验门**把关，**8 卡并行**跑完所有优化点
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
2. 把交付模型拷进工程：
   ```bash
   cp "$ORCA_AGENT_RESOURCES/scripts/models/pure_cnn_model.py" <工程>/rx_sweep_models/
   cp "$ORCA_AGENT_RESOURCES/scripts/models/kd_helper.py"      <工程>/rx_sweep_models/
   ```
3. 适配训练入口接受 `--variant <v>` / `--kd` / `--teacher-ckpt <p>`，并在载入模型后、训练前打印一行 `[RX-GATE] ...`（格式见 contracts.md §3）。两种方式：
   - 优先跑固化脚本：`python "$ORCA_AGENT_RESOURCES/scripts/adapt_project.py" --project-root <工程>`（幂等，自动生成 `rx_runner.py` 包装器）。
   - 脚本够不到的结构（用户训练脚本特殊）→ 按 `adaptation-guide.md` 手工适配（Edit 用户的 utils/ 脚本）。
4. **KD 前提**：蒸馏实验需 teacher（用户训好的原模型 ckpt）。问用户拿 teacher_ckpt 路径；没有 → KD 实验标 SKIP，仍跑 scratch。

> 用户原训练逻辑（loss/optimizer/scheduler/eval metric/数据管道）是**不可替代权威**，逐字保留，只加 variant 选择 + GATE 打印，绝不替换用户的 loss/metric（见 adaptation-guide.md 铁律）。

### 阶段 2：检验门（全过才进 sweep）

对矩阵里每个 variant 跑 1 步检验：

```bash
python "$ORCA_AGENT_RESOURCES/scripts/gate_check.py" \
  --project-root <工程> --variant <v> [--kd --teacher-ckpt <p>] [--gpu <N>]
```

读 stdout 的 `[GATE-RESULT] variant=... passed=true|false reason=...`。
**任一 variant `passed=false` → 停，把 reason 报给用户**，不进 sweep。常见原因：I/O shape 不对齐、开关没生效、前向/backward 崩。

### 阶段 3：起 daemon + 建矩阵 + 8 卡并行 sweep

定一个 session 工作目录 `$WORK`（results / tape / orca_env.sh 都落此），先起 live 推图用的 chart daemon（纯 skill 调用没有 workflow run，需自起）：

```bash
WORK=<工程>/.rx-sweep
python "$ORCA_AGENT_RESOURCES/scripts/ensure_chart_daemon.py" --work-dir "$WORK"   # 起 session daemon + 写 orca_env.sh（幂等：活着就不重起）
python "$ORCA_AGENT_RESOURCES/scripts/build_matrix.py" --out "$WORK/matrix.json"
python "$ORCA_AGENT_RESOURCES/scripts/launch_sweep.py" \
  --project-root <工程> --matrix "$WORK/matrix.json" --gpus 8 --results "$WORK/results.jsonl" [--teacher-ckpt <p>]
```

- **`results.jsonl` 必须落 `$WORK` 内**——ensure_chart_daemon 的 `orca_env.sh` 在此 → 阶段 4 push_results 的 env 自加载接得上 → live 推图通。
- ensure_chart_daemon 在 **orca 侧**（需 orca + pydantic）跑。若当前机器没装 orca（如裸训练服务器），它 fail loud exit 1——**不阻断 sweep**，阶段 4 推图自动回退 fallback JSON。
- launch_sweep 自动把实验分到 `CUDA_VISIBLE_DEVICES=0..7`，8 个一批并行。逐实验打印 `[SWEEP] exp_id=... gpu=.. status=...`，末尾 `SWEEP_DONE`。
- 某实验 FAIL（gate/train/eval）不阻断其它——status 记入 jsonl，继续。

### 阶段 4：推图 + 汇报

```bash
python "$ORCA_AGENT_RESOURCES/scripts/push_results.py" --results "$WORK/results.jsonl"
```

- 阶段 3 已 ensure_chart_daemon → push_results 的 `load_run_env_from_artifacts` 从 `$WORK/orca_env.sh` 补 4 个 `ORCA_*` env → render_chart 连 daemon → 推 pareto（latency×accuracy）+ bar（variant×accuracy，hue=kd）+ 总表 到 web UI（`orca open` 看）。
- daemon 没起 / 或ca 未装 → push_results fail-soft 落 `chart.json` + 打印 `CHART_FALLBACK_JSON`（不崩）。
- 给用户一句话总结：哪个 variant 精度最高、latency 最低、KD 相比 scratch 增益多少。

## 硬规则

- **H1 检验门是硬门**：阶段 2 任一 variant 不过 → 不进 sweep，不静默继续。
- **H2 用户逻辑即权威**：不替换用户的 loss/optimizer/scheduler/eval metric/数据管道（adaptation-guide.md 铁律）。
- **H3 fail loud**：脚本遇契约不符输入非零退出 + stderr 报因；你不悄悄重跑。
- **H4 推图是 sidecar**：ensure_chart_daemon / render_chart 失败只 stderr + 回退 fallback JSON，绝不阻断 sweep。live 推图需 orca 侧（ensure_chart_daemon + push_results 都在装了 orca 的机器跑）。
- **H5 测试先**：拿不准适配是否对时，先在 `fixture/`（本 skill 自带假 model8 工程）跑一遍 adapt→gate→sweep→push 验证脚本链路，再上用户工程。

## 用 fixture 自检（工程测试）

本 skill 带 `fixture/`（假 model8 + 假 train.py + 合成数据）。验证脚本链路时：

```bash
cd "$ORCA_AGENT_RESOURCES/fixture"
python train.py --variant pure_cnn --epochs 1   # 应打印 [RX-GATE] ... gate=PASS
python "$ORCA_AGENT_RESOURCES/scripts/gate_check.py" --project-root "$ORCA_AGENT_RESOURCES/fixture" --variant pure_cnn_pilot
python "$ORCA_AGENT_RESOURCES/scripts/build_matrix.py" --out /tmp/matrix.json
python "$ORCA_AGENT_RESOURCES/scripts/launch_sweep.py" --project-root "$ORCA_AGENT_RESOURCES/fixture" --matrix /tmp/matrix.json --gpus 1 --results /tmp/results.jsonl
python "$ORCA_AGENT_RESOURCES/scripts/push_results.py" --results /tmp/results.jsonl
```

全跑通 = 脚本链路 OK，可上用户工程。

<success_criteria>
- [ ] Read 了 contracts.md / experiment-matrix.md / adaptation-guide.md
- [ ] 拷 pure_cnn_model.py + kd_helper.py 进用户工程
- [ ] 适配训练入口接受 --variant/--kd/--teacher-ckpt + 打印 [RX-GATE] 行
- [ ] 阶段 2 每个 variant gate_check 全 passed=true 才进 sweep（任一失败即停报用户）
- [ ] build_matrix + launch_sweep 8 卡并行跑完，results.jsonl 收齐
- [ ] push_results 推图（或 fallback JSON），给用户精度/latency/KD 增益总结
- [ ] 未替换用户 loss/metric/数据管道
- [ ] 拿不准时先用 fixture 自检脚本链路
</success_criteria>
