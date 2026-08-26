# 设计草稿：Agent 驱动的结构性探索（降时延、保精度）

> 状态：草稿（2026-07-16），待用户确认后转 workflow YAML。
> 定位：解决传统 NAS 解决不了的问题——**目标无法靠改超参达到**，必须改宏观结构（模型设计）。
> 参考：`references/nas/`（10 篇 LLM-for-NAS 论文 + `methods-comparison.md`）。
> 本草稿是后续 `create-workflow` 出 YAML 的**契约**。

---

## 1. 背景与目标

仓库已有 supernet NAS 流水线（`workflows/nas-agent-pipeline.yaml`），其前提是"最优解在预设搜索空间内"。本 workflow 面向相反场景：**用户手上有一个卡住的模型，靠改超参已无法把时延压到目标 / 精度已到瓶颈，需要在宏观结构层面做突破性探索。**

用 LLM agent + 外挂知识库做开放式（但原语约束下）的结构探索，**以时延下降为第一目标、精度无损/微损为约束**，渐进下探到用户目标。

### 1.1 目标（min latency s.t. accuracy）
- **主目标**：`min latency`，s.t. `accuracy ≥ accuracy_target`（用户定；"无损"=设成 baseline，"微损"= baseline−ε）。
- **渐进下探**：不必一步到 target（baseline 500ms → target 100ms 可一次降一点），每轮 ratchet 降一点。
- **终止**：`champion.latency ≤ target_latency` 且 `champion.accuracy ≥ accuracy_target`，或 `max_rounds` / 预算耗尽。

### 1.2 四条不变量（违反即 fail loud）
1. **时延永远实测，绝不被 LLM 猜**。时延只来自可替换的 cost model 函数；LLM 只提结构、不报时延数。
2. **只改模型结构文件，绝不碰训练函数**。`train_command` 原样 shell 执行。
3. **改动以宏观结构为主，不硬驳回超参**（§9 放宽规则）。
4. **时延门在前、训练在后**。时延没降到 champion 以下 → 直接记失败，不训练。

---

## 2. 系统架构总览

```
                        ┌──────────────── 中央 KB（外挂，按族索引）────────────────┐
                        │  common/  +  families/{transformer,cnn,mamba,...}/       │
                        │  index.json 驱动按族 + 按 agent 任务切片加载（§7）        │
                        └──────────────────────────┬──────────────────────────────┘
                                                   │ (只读切片 / Analyst 追加写)
   ┌───────────────────────────────────────────────┴──────────────────────────────────┐
   │                        一条探索 path = 一个 task subagent                          │
   │                                                                                     │
   │  Hypothesizer(LLM) → Engineer(LLM) → [结构门] → [导出ONNX→cost model] → [时延门]   │
   │                                          │                       │            │     │
   │                                       (tag)                  FAIL(latency)   通过  │
   │                                                                           ↓        │
   │                                          ┌──── GPU 训练池（多卡调度 §8）────┐    │
   │                                          │   train_command 原样执行 → accuracy │   │
   │                                          └─────────────────┬──────────────────┘  │
   │                                          FAIL(accuracy) ←──┴──→ SUCCESS            │
   │                                                             │                       │
   │  Analyst(LLM) ←── 归因 ──────────────────────────────────┘                        │
   │     │ 写回 KB（principles / failures）                                              │
   │  Curator(确定性 reducer): 账本追加 / champion ratchet / exploit-explore 路由       │
   └──────────────────────────────── w─────────────────────────────────────────────────┘
                                  多 path 并行（共享账本 + GPU 池）
```

### 2.1 Agent 角色（Orca task subagent 映射）

| 角色 | LLM? | 职责 | 借鉴 |
|-----|------|------|------|
| **Hypothesizer** | ✅ | 读 champion.model + KB 切片 + 时延缺口 → 提 k 个**宏观结构**假设（附降时延理由 + 新颖性理由） | ASI-ARCH Researcher |
| **Engineer** | ✅ | 假设 → schema/AST 校验过的 `model.py` | NNGPT Coder |
| **Analyst** | ✅ | 归因成败 → 提炼结构原则 → 写回 KB（成功 & 失败都记） | ASI-ARCH Analyst |
| **Evaluator** | ❌ 确定性 | 导出 ONNX、调 cost model、跑 `train_command`、取 accuracy | — |
| **Curator** | ❌ reducer | 账本 append、结构门分类、时延门、champion ratchet、exploit/explore 路由、GPU 调度 | LAPT Principle Adaptation |

> 三个 LLM agent 全无状态、prompt-driven；状态在 tape，由幂等 Curator 维护 → 踩中 Orca 单 tape + 幂等 reducer 底线，符合 `[[deterministic-over-model-mediated]]`。

---

## 3. 主循环（时延中心 · 渐进 ratchet）

```
Setup（一次性）:
  baseline_latency   = cost_model.measure(export_onnx(原始模型))     # 实测，如 500ms
  baseline_accuracy  = 用户给；不给则按 train_command 训一次原始模型
  champion = {latency: baseline_latency, acc: baseline_accuracy, model: 原始, id: "baseline"}
  family = LLM 判断(读 model.py) → 按 index.json 加载该族 + common 的 KB 切片（§7）

循环 until champion.latency ≤ target_latency 且 champion.acc ≥ accuracy_target
            或 max_rounds / 预算耗尽:

  每轮 R（每条 path 各自跑）:
   1. Hypothesizer(LLM): 读 champion + KB 切片 + (target − champion.latency 缺口)
        → 提 k 个结构假设（≥ structural_slot_ratio 比例为宏观结构；§9）
   2. Engineer(LLM): 父 model.py + 假设 → 新 model.py
   3. 结构门(§9): AST diff 出变更摘要 → LLM 终判 tag ∈ {structural, hyperparam, mixed}
        （默认不驳回 hyperparam-only，仅打标）
   4. 导出 ONNX → cost_model.measure()                       ← 实测/可替换
        latency ≥ champion.latency → 记 FAIL(latency)，不训练        ← 廉价并行过滤
        latency <  champion.latency → 进 GPU 训练池（§8）
   5. 训练（train_command 原样）→ accuracy
        记账本（完整 model.py 快照 + latency/acc/status/tag/parent/round/path）
        acc <  accuracy_target → 记 FAIL(accuracy)（时延好但精度丢，负样本）
        acc ≥  accuracy_target → 记 SUCCESS
   6. champion = 全 path 中 min-latency 且 acc 达标的 candidate（ratchet 下探）
   7. Analyst(LLM): 归因 → 原则写回 KB
   8. Curator 路由: champion 改进 → exploit（强化赢的原语族）；否则 → explore（注入 OOD 结构原语）
   9. 刷新可视化
```

> **关于"高于基线则失败"**：实现为 `latency < champion.latency` 门。第 1 轮 champion=baseline，等价于你的 baseline 规则；之后自动 ratchet，避免在已有 300ms 冠军时还训 480ms 候选浪费 GPU。

---

## 4. 评测环 = 时延先行（替代 multi-tier）

> 用户明确不要 multi-tier。**时延门本身就是廉价并行过滤器**，天然起漏斗作用——不需要多层 epoch tier。

```
每轮每 path 生成 k 个候选
   │
   ▼  【零 GPU · 可大规模并行】
   Engineer 生成 model.py → 结构门分类
   → 导出 ONNX → cost_model.measure()            ← 可替换 cost model（快、CPU）
   │
   ├─ latency ≥ champion.latency → 记 FAIL(latency)，不训练
   │
   └─ latency < champion.latency → 进 GPU 训练池
        │
        ▼  【GPU · train_command 原样执行】
        训练 → accuracy
        ├─ acc <  accuracy_target → FAIL(accuracy)
        └─ acc ≥  accuracy_target → SUCCESS（可成新 champion）
```

- ONNX 导出失败（exotic 结构导不出）→ 记 FAIL(export)，不训练，fail loud。
- **贵的资源（GPU）只花在已确认降时延的候选上**——这就是漏斗，用"时延门"实现而非多层 epoch。

---

## 5. 时延 cost model —— 可替换插件接口

契约钉死为**一个函数**，workflow 永远通过它取时延，绝不内联时延逻辑：

```python
# plugins/latency_onnxrt.py —— 默认实现（onnxruntime 实跑取中位数），可整体替换
def measure(onnx_path: str, runs: int = 20, warmup: int = 5) -> float:
    """接受 ONNX 路径，返回时延(ms)。用户后续把自己的 cost model 填这里。"""
    import onnxruntime as ort, numpy as np, time, statistics
    sess = ort.InferenceSession(onnx_path, providers=["CUDAExecutionProvider","CPUExecutionProvider"])
    inp = {i.name: np.random.randn(*[d if isinstance(d,int) else 1 for d in i.shape]).astype(np.float32)
           for i in sess.get_inputs()}
    for _ in range(warmup): sess.run(None, inp)
    ts=[]
    for _ in range(runs):
        t=time.perf_counter(); sess.run(None, inp); ts.append(time.perf_counter()-t)
    return statistics.median(ts)*1000
```

- 替换：config `latency_provider: "/abs/path/my_cost_model.py::measure"`，workflow 动态加载。
- 不是 callable / 报错 → **fail loud**，整轮停。
- **时延一定是 ground-truth 实测，LLM 永不预测时延。**

---

## 6. 文件 / 历史管理：append-only 账本 + 每路线一 git worktree

**选择**（三选里最鲁棒、唯一干净支持多路线并行）：append-only 账本 + 每条 path 一个 git worktree。

```
Orca 输出目录（账本，跨 path 共享，append-only，永不删改）:
  llm_artifacts/<model>/runs/<run_id>/
    ledger.jsonl                 # 每行一个 candidate 记录
    snapshots/<id>_model.py      # 每个 candidate 完整 model.py 快照（不可变 = 历史真相）
    champions.jsonl              # 冠军轨迹（驱动可视化）
    kb_cache/                    # 本 run 的 KB 切片缓存 + Analyst 写回
    viz/*.html

用户项目（git 仓库）:
  model.py      ← 原始基线，永不破坏性改（基线快照已入账本 + git 跟踪）
  train.py      ← 永不碰
  .worktrees/
    path-1/     ← git worktree，路线1 独立 checkout
       model.py ← workflow 写入当前 candidate（train.py 照常 import）
       train.py ← 原样
    path-2/     ← 路线2 …（并行）
```

**为什么不是另外两个**：
- ❌ 直接改原 model 文件：丢基线、不能并行、无历史。
- ❌ 新文件 + 软链接：并行抢同一 import 路径会撞；训练函数要 import 变路径或靠 symlink，脆弱。
- ✅ worktree + 账本：path 独立 checkout → model.py 不互撞；train.py 原样 `import model`（在各自 worktree 跑）→ **训练函数零改动**；原仓库永不破坏；账本全快照 → 可回溯/diff/复现任意 candidate。

**工程细节**：
- 训练输出按 worktree 隔离（`cwd=worktree` 或 per-path output env），避免共享固定路径撞。
- worktree 只 checkout tracked 文件；大数据若 gitignored 不受影响。**非 git 仓库** → 自动 fallback 为 per-path 目录拷贝。

---

## 7. 外挂知识库：按模型族分库 + 索引（避免冗余读取）

> 用户要求：transformer / cnn 各有不同 KB（仅为示例，可扩展任意族）；做好索引，避免冗余读取。

### 7.1 目录结构
```
knowledge_base/
  index.json                       # 总索引：族指纹 + 文件清单 + agent 任务→切片映射
  common/
    primitives.md                  # 通用结构原语（残差、bottleneck、归一化位置、skip…）
    principles.md                  # 通用结构-性能原则
    latency_heuristics.md          # 通用降时延手法（算子融合、结构化剪枝即结构、量化友好结构）
  families/
    transformer/
      meta.json                    # 指纹 keywords（触发本族加载）
      primitives.md                # MHA/linear-attn/GQA/MoE/RoPE/FFN 分解…
      patterns.md                  # 已知高效变体结构前提（FlashAttn 前提、sliding-window…）
      latency_moves.md             # 本族降时延 move（GQA、FFN 融合、KV-cache 结构、序列并行…）
      failures.md                  # 本族已知失败结构（Analyst 持续追加）
    cnn/
      meta.json
      primitives.md                # depthwise-sep / ghost / bottleneck / group conv…
      latency_moves.md             # DW-sep、channel shuffle、BN-ReLU 融合、early-down…
      failures.md
    mamba/  rnn/  hybrid/  …       # 示例：任意可扩展
```

### 7.2 index.json（索引 · 避免冗余读取的核心）
```json
{
  "families": {
    "transformer": {
      "fingerprints": ["MultiheadAttention","Attention","Transformer","qkv","scaled_dot_product","Encoder"],
      "files": {"primitives":"families/transformer/primitives.md",
                "latency_moves":"families/transformer/latency_moves.md",
                "patterns":"families/transformer/patterns.md",
                "failures":"families/transformer/failures.md"}
    },
    "cnn": {
      "fingerprints": ["Conv2d","Conv1d","BatchNorm","MaxPool","ResNet"],
      "files": { ... }
    }
  },
  "common": {"files": {"principles":"common/principles.md",
                       "latency_heuristics":"common/latency_heuristics.md",
                       "primitives":"common/primitives.md"}},
  "agent_slices": {
    "hypothesizer": ["common.principles","common.latency_heuristics","{family}.primitives","{family}.latency_moves"],
    "engineer":     ["{family}.patterns","common.primitives"],
    "analyst_read": ["common.principles","{family}.failures"]
  }
}
```

### 7.3 加载策略（避免冗余读取的三重保证）
1. **族级过滤**：Setup 时由 **LLM 读 model.py 判断族**（输出族名，可多族取并集；`meta.json` 的 fingerprints 仅作可选提示，不参与判定）→ 只加载命中族 + common。**未命中族永不读。**
2. **任务级切片**：每个 agent 只注入它在 `agent_slices` 里声明的文件（Hypothesizer 不读 failures.md；Engineer 不读 latency_moves.md）。**不相关文件不进 prompt。**
3. **run 级缓存**：本 run 需要的每个文件**只读一次**进 `kb_cache/`，之后 agent 引用路径由编排器注入切片。**无文件被重复读。**
- Analyst 写回：追加到对应族文件（failures.md / principles.md），缓存同步失效重载该单文件。

> family 检测可被 `model_family` config 显式覆盖（如 hybrid / 自定义族）。

---

## 8. 多 GPU + 多 agent 并行调度

> 用户：有多个 GPU，要多 agent 并行。

### 8.1 GPU 按需分配 + agent 启动训练
- 训练由 **agent 启动**（复用 nas-train-runner 模式）：训练 agent 启动时 **按需探测空闲卡**（`nvidia-smi` 查显存/利用率）→ claim 一张 → 设 `CUDA_VISIBLE_DEVICES=<id>` → 原样跑 `train_command` → 结束释放。
- 无静态卡池；`gpus` config 可选（默认 auto = 用所有探测到的卡）。多 agent 并发各自按需抢卡，抢不到则排队等下一张空闲。
- 时延门（CPU/廉价）不受 GPU 限制，可大规模并行筛。

### 8.2 多 path 并行
- N 条 path = N 个 task subagent 并发，各自一条假设血脉（parent 树）、各自 champion。
- 共享**中央账本**（append 安全）+ **共享 KB**（只读切片 + Analyst 仲裁写）+ **共享 GPU 池**。
- 每条 path 可注入不同**策略种子**（path-1=深度可分离方向、path-2=注意力分解方向、path-3=结构化剪枝即结构重写…）。
- 全局 champion = 跨所有 path 的 min-latency 达标点。

### 8.3 调度时序
```
path-1 ┐
path-2 ├─ 廉价并行: 提假设→落码→导ONNX→测时延   （CPU，无 GPU 争用）
path-3 ┘            │
                    ├─ FAIL(latency) → 丢弃
                    └─ 通过 → 训练 agent 按需抢空闲卡 → train → 释放
```

---

## 9. 结构变更门（放宽版 · 不硬驳回超参）

> 用户：不必硬驳回超参-only；有些结构再优化超参就能达标。

### 9.1 分类（AST 出事实摘要 → LLM 终判；不驳回，只打标）
**确定性事实 + 语义判断**两步合一：
1. **AST diff 出"变更摘要"**（Curator 跑 module-level AST diff vs 父 model.py）：客观列出"算子类型/拓扑是否变 / 哪些是纯数值改"，**作为参考输入**喂 LLM——让 LLM 在事实面前无法把自己的超参微调硬标成 structural。
2. **LLM 终判 tag** ∈ {structural, hyperparam, mixed}：在 AST 摘要 grounding 下做语义判定（能处理 AST 判不出的边界，如 group conv groups 1→8 是数值改但语义上算结构性）。

AST **不是唯一判据、也不直接驳回**；它是防 LLM 自凑配额的**事实参考**。配额（§9.2）按 LLM 终判的 tag 计数确定性执行。默认全部放行，tag 写入账本（驱动可视化与策略）。

### 9.2 防退化成纯超参搜索（软配额，非硬驳回）
- config `structural_slot_ratio: 0.5`（默认）：每轮 k 个假设中，**至少一半**被 Hypothesizer 指示为宏观结构方向。
- config `reject_hyperparam_only: false`（默认）：hyperparam-only 候选**允许**。
- 仅当用户显式 `reject_hyperparam_only: true`（严格模式）时，hyperparam-only 才被 reject。

> 兼顾两点：保留"结构 + 超参调优可达标"的合法路径（不误杀），又防止搜索退化为纯超参调优（保持 workflow 的结构性定位）。

---

## 10. 输入契约 / 配置

```yaml
inputs:
  model_path: examples/ViT/model.py              # 模型定义文件（只改它）
  project_root: examples/ViT/
  build_fn: build_model                           # 实例化入口（导出 ONNX 用）
  dummy_input: {shape: [1,3,224,224], dtype: float32}
  train_command: "python train.py --epochs 30 --config base"   # 原样执行，绝不改
  baseline_accuracy: 0.812                        # 可选；不给则先训一次
  target_latency_ms: 100
  accuracy_target: 0.807                          # = baseline−0.5%（"微损"）；无损则设 baseline
  latency_provider: "plugins/latency_onnxrt.py::measure"       # 可替换 cost model
  gpus: auto                                      # 或 [0,1,2,3]
  model_family: auto                              # 或显式 transformer/cnn/hybrid/...
  knowledge_base: "knowledge_base/"              # 外挂 KB 根
  paths: 3                                        # 并行路线数
  max_rounds: 20
  structural_slot_ratio: 0.5                      # 每轮结构假设最低占比
  reject_hyperparam_only: false                   # 严格模式才 true
  onnx_export: {opset: 17}
```

---

## 11. 数据 schema

### 11.1 ledger.jsonl（每行一个 candidate）
```json
{"id":"r2_p1_c3","parent":"r1_p1_c1","path":"p1","round":2,
 "status":"SUCCESS",                       // SUCCESS | FAIL_latency | FAIL_accuracy | FAIL_export | REJECT_struct
 "tag":"structural",                        // structural | hyperparam | mixed
 "latency_ms":412.3,"accuracy":0.809,
 "delta_latency_ms":-37.7,"met_accuracy":true,
 "snapshot":"snapshots/r2_p1_c3_model.py","onnx":"snapshots/r2_p1_c3.onnx",
 "diff_summary":"将第3-5层 MHA 换成 GQA(q_group=4)","hypothesis":"...",
 "timestamp":null}                          // 由调度器写入（脚本内禁用 Date.now）
```

### 11.2 champions.jsonl
```json
{"round":2,"id":"r2_p1_c3","latency_ms":412.3,"accuracy":0.809,
 "delta_vs_baseline_ms":-87.7,"snapshot":"..."}
```

### 11.3 KB 写回（Analyst）
- `families/<family>/failures.md`：append "结构指纹 → 失败原因（时延没降 / 精度掉 / 导不出）"。
- `common/principles.md`：append 跨族通用原则（如"在浅层做下采样比深层省时延且精度损失小"）。

---

## 12. 可视化（复用并扩展 nas-viz agent，静态 HTML）

1. **Latency Champion Trace（主图）**：x=candidate 序号，y=latency(ms)；灰点=全部候选，高亮线=连接 running-min-latency（达标者）的 champion 轨迹；水平虚线 baseline/target；达 target 处标 ★。
2. **Latency vs Accuracy Pareto**：候选按 status 着色，Pareto 前沿高亮，标 baseline/target。
3. **Exploration Tree（多路线图）**：节点=candidate，边=parent，按 path 着色、status 标记（形似 git log graph）。
4. **Round Ledger 表 + 降幅瀑布**：每轮提案/过门/训练/达标数 + champion latency + Δbaseline；waterfall baseline→每冠军步 shaved ms。

---

## 13. 借鉴来源（本草稿"活下来"的）

| 设计 | 来自 | 在本设计干嘛 |
|---|---|---|
| 多 agent 分工 | ASI-ARCH | 提结构/落码/归因/确定性门与路由 |
| Cognition KB（原语+原则+台账+失败） | ASI-ARCH + LAPT + LLM-NAS | grounding + 跨 run 复利 |
| exploit/explore 二值路由 + LLM 看结构不看数值 | LAPT | 无 RL 的探索/利用切换 |
| code 生成 + AST/schema 校验 | NNGPT | 结构门、可编译性校验 |
| 多样性/新颖性度量 | LLMatic | 防 Hypothesizer 反复提同款 |
| 按族外挂 KB + 索引切片 | （新增，用户要求） | 避免冗余读取、跨族复用 |
| ~~multi-tier 漏斗~~ | ~~LLM-NAS/AgentNAS~~ | 被"时延门 + 单次训练"取代 |
| ~~training-free proxy 选架构~~ | ~~LLM-NAS~~ | 被"时延实测 + 真训练验精度"取代 |

---

## 14. 已确认决策（转 YAML 前已定）

1. ✅ **族检测**：LLM 判断（读 model.py，可多族取并集），**不用 AST 脚本**。
2. ✅ **accuracy_target 默认** = `baseline − 0.5%`（微损），用户可覆盖。
3. ✅ **结构分类**：**AST diff 出事实摘要 → LLM 终判**（AST 作参考、非唯一判据、不直接驳回），既防 LLM 自凑配额又能处理语义边界；配额 `structural_slot_ratio=0.5`、`reject_hyperparam_only=false` 由 Curator 按 LLM 终判 tag 计数确定性执行。
4. ✅ **GPU**：按需探测空闲卡 + agent 启动训练，无静态卡池。
5. ✅ **收尾**：终止后自动 champion 从头重训 + 导出 ONNX + final_report。
6. ✅ **KB 预置**：transformer / cnn 两族预填真实原语 + SOTA 降时延 move，参考 ASI-ARCH（线性注意力族发现）/ EvoPrompting / LLMatic / NNGPT / LLM-NAS 等论文的实际结构手法。

> 原则总结：**语义判断交 LLM（族检测、结构分类终判、假设、归因），确定性的事交脚本/代码（时延实测、训练、AST 事实摘要、配额计数、champion ratchet、exploit/explore 路由）。**

---

## 15. 下一步

§14 已确认，进入实现：
1. **预置 KB**（先做，YAML 依赖它）：落盘 `knowledge_base/`，transformer / cnn 两族预填真实原语 + SOTA 降时延 move（参考 `references/nas/` 各论文的实际结构手法），common + index.json + 可扩展族模板齐备。
2. **落 workflow**：`create-workflow` 出 `workflows/agent-struct-exploration.yaml` + 5 个 agent md（Hypothesizer / Engineer / Analyst / Curator / Evaluator）。
3. **可视化**：`nas-viz` agent 扩展四张图（champion trace / Pareto / exploration tree / ledger+waterfall）。
4. 本草稿转"已确认"。
