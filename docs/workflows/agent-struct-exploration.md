# agent-struct-exploration：LLM 驱动的开放结构搜索

> Orca NAS 三件套中最激进者。前两者（`nas-hp-search`、`nas-agent-pipeline`）预设离散搜索空间、假定最优解落在其中；本 workflow 面向相反场景——目标时延/精度无法靠超参或预设 block 达到，须由 LLM 在**开放的结构空间**上提议宏观结构改写，经 AST 级落码、实测时延与精度，循环逼近约束。不依赖弹性超网。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`agent-struct-exploration` 以 LLM 为结构提议者、确定性脚本为度量与归约者，构成「假设 → 落码 → 门控 → 实测 → 归因 → 归约」的探索循环，在满足精度下界的前提下单调下探时延。其契约见 [`docs/specs/agent-structural-exploration-design-draft.md`](../specs/agent-structural-exploration-design-draft.md)，底层脚本位于 `workflows/agents/_struct_scripts/`。

### 1.2 架构与流程

该 workflow 为 11 节点的循环 DAG：2 个一次性设置节点 → 7 节点循环体（经 `viz_round` 的条件 back-route 回到 `hypothesizer`）→ 2 个一次性收尾节点。

```
 ── 设置（一次性）─────────────────────────────────────────────
 family_detect        浅层 LLM 识别模型族（transformer/cnn/wireless_receiver/hybrid）
                     → 加载族索引的 KB 切片到 kb_cache/ → 建输出目录+账本+worktree 根
        ↓
 baseline_measure     确定性：measure_baseline.py → 导出 baseline ONNX + 实测时延
                     + 取精度（test|given|train）→ 种子 champions.jsonl（baseline=第 0 轮 champion）
 ── 循环体（每轮 = 一条探索 path）───────────────────────────
        ↓  ┌─ hypothesizer     LLM 提宏观结构假设（读 KB：principles + latency_heuristics
        │  │ (struct-hypothesizer)  + {family}.primitives + {family}.latency_moves；从不预测时延数值）
        │  ├─ engineer         LLM 把假设落成 AST 合法/可编译的 model.py
        │  │ (struct-engineer) 写入候选 git worktree + 不可变 snapshots/<id>_model.py（仅改 model.py）
        │  ├─ structure_gate   确定性 ast_diff.py + LLM 标签 ∈ {structural, hyperparam, mixed}
        │  │ (内联)            默认通过（从不拒绝）
        │  ├─ evaluator        确定性漏斗：导出 ONNX → 实测时延 → 时延门（不过则 FAIL_latency 不训练）
        │  │ (struct-evaluator) → 过门才跑 train_command → 精度门（FAIL_accuracy 或 SUCCESS）
        │  ├─ analyst          LLM 归因（成/败的宏观结构原因）→ 写回 KB
        │  │ (struct-analyst)  （families/<family>/failures.md 或 common/principles.md）
        │  └─ curator          确定性 reducer：ledger_reducer.py
        │     (struct-curator) 账本追加 + champion ratchet + explore/exploit 路由 + continue_loop 决策
        ↓
 viz_round            viz_struct.py 推 4 图（champion 追踪/Pareto/探索树/账本表）
 (内联)               ← 当 curator.output.continue_loop=true：back-route 回 hypothesizer
                     ← 否则：前进到 finalize
 ── 收尾（一次性）────────────────────────────────────────────
 finalize             对 champion 快照完整重训 + 导出 ONNX + 实测 + final_report.md
 viz_finalize         重推 4 图 + Baseline vs Champion vs Final 对比柱状图
```

### 1.3 输入 / 输出

**输入**（P9b 按三档原则精简；详 `docs/specs/workflow-input-design-principle.md`）：

| 参数 | Tier | 标签 | 默认 | 说明 |
|---|---|---|---|---|
| `model_path` | A | `[ask]` | — | 模型入口（glob 推断，多候选启动前问用户） |
| `train_command` | A | `[ask]` | — | 训练命令（原样 shell 执行，agent 不改训练逻辑） |
| `test_command` | A | `[ask]` | "" | 评估命令（pre-trained 模式只测不训；空则跑 train_command） |
| `target_latency_ms` | A | `[ask]` | — | 时延目标上界（业务 KPI） |
| `accuracy_target` | A | `[ask]` | "" | 精度下界（空=baseline−0.5%） |
| `max_rounds` | A | `[ask]` | 20 | 最大探索轮数（预算闸门） |
| `device` | C | `[advanced]` | auto | ONNX 导出+latency 测量设备（auto/cuda/npu/cpu） |
| `latency_provider` | C | `[advanced]` | struct 自带 latency_onnxrt.py::measure | `path::func` latency 脚本引用 |
| `seed` | C | `[advanced]` | 0 | 复现种子 |

**Tier B 下沉**（setup 节点 output_schema 字段，缺失走 ask-user 哨兵）：`project_root` / `build_fn` / `dummy_input` / `struct_scripts_dir`。
**Tier C 固化**：`iterations`（移除，引擎兜底 100，长 run 用 `--max-iter` CLI 覆盖）、`output_dir`（走引擎注入 `$ORCA_ARTIFACTS_DIR`）。

**输出**：`champion`（满足目标的最佳模型快照）+ `champions.jsonl`（冠军轨迹）+ `ledger.jsonl`（全候选账本）+ `final_report.md` + 可视化（时延-精度下探曲线、轮次账本表）。workflow 产出含 `result`、`champion`、`final_latency_ms`、`final_accuracy`。

### 1.4 如何激活

```
用 TARS 把这个模型的时延压到 100ms，精度别掉太多
TARS，优化模型结构，降时延保精度
```

匹配命中的关键词：**结构搜索 / 降时延 / 保精度 / 改结构 / AST**。等价手动命令：

```bash
orca agent-struct-exploration --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "train_command": "python demo_target/vit_tiny_cifar100/train.py",
  "test_command": "python demo_target/vit_tiny_cifar100/train.py --eval",
  "target_latency_ms": "100", "accuracy_target": "0.70", "max_rounds": "5"
}'
```

---

## 2. 定义

本 workflow 求解一个**约束优化问题**：

$$m^{\*} \;=\; \arg\min_{m\in\mathcal{M}}\; L(m) \qquad \text{s.t.}\quad A(m)\ge A_{\text{target}}$$

其中 $L(m)$ 为模型 $m$ 的实测 ONNX 推理时延，$A(m)$ 为实测训练后精度，$A_{\text{target}}$ 为精度下界。**关键区别**在于结构空间 $\mathcal{M}$：与超网 NAS 的预设离散空间不同，$\mathcal{M}$ 是 LLM 提议的 AST 结构改写的闭包，是开放且不可枚举的。优化仅受「原语约束」（改写须保持模型可导出、可前向）与四条不变量（§4.3）约束。

---

## 3. 背景

### 3.1 预设搜索空间的局限

超网 NAS 的前提是最优解落在预设的宽度/深度/block 组合空间内。当目标时延远低于该空间内任何子网所能达到，或精度瓶颈源于宏观结构设计（而非超参）时，预设空间失效，须突破到结构改写层面。

### 3.2 LLM 驱动的 NAS

近期工作以 LLM 作为结构提议者：LLMatic（Nasr et al., 2024）以 Researcher/Analyst 双角色协作搜索，ASI-ARCH 借鉴该角色分工，NNGPT 以 Coder 角色生成结构，LAPT 以原理自适应归约。本 workflow 将 LLM 提议与**检索增强的领域知识库（KB）**结合：LLM 并非凭空生成，而是基于按模型族索引的 KB 切片提出假设，并将成败经验回写 KB，形成越探索越精炼的闭环。

### 3.3 确定性与 LLM 的职责切分

本 workflow 的核心设计是**将度量与归约交由确定性脚本、将结构创造交由 LLM**。时延与精度永远由脚本实测，LLM 仅负责提议结构与归因；循环控制（champion 更新、路由、终止）由确定性 reducer 决定。该切分使结果可审计、度量不可被 LLM 臆造。

---

## 4. 方法

### 4.1 探索循环

每轮探索为一条「假设 → 落码 → 门控 → 实测 → 归因 → 归约」的 path（见 §1.2 架构图）。LLM 节点（hypothesizer/engineer/analyst）负责结构创造与归因，确定性节点（structure_gate/evaluator/curator）负责度量与控制。循环经 `viz_round` 的条件 back-route 驱动：当 `curator.output.continue_loop=true` 时回到 `hypothesizer`，否则进入收尾。

### 4.2 时延测量协议

时延由可插拔的 cost model 实测，定义为本 workflow 的**唯一时延真值来源**：

$$L(m) \;=\; \mathrm{median}_{t\in\mathcal{T}}\!\bigl[\,\mathrm{perf\_counter}(\mathrm{ort.run}(m_{\mathrm{ONNX}}))\,\bigr]\;\times\;10^{3}\;\;(\text{ms})$$

其中 $\mathcal{T}$ 为 20 次推理（前 5 次预热丢弃），以 ONNX Runtime 执行。cost model 经 `importlib.util.spec_from_file_location` 动态加载、失败 fail loud，保证可替换性。

### 4.3 四条不变量

以下四条不变量由脚本强制，违反即 fail loud，是本 workflow 可信度的基石。

**(I1) 时延永远实测，绝不被 LLM 预测**。$L(m)$ 仅来自 §4.2 的实测；hypothesizer 被约束从不输出时延数值，仅提结构意图。此不变量防止 LLM 臆造「改完应该会快」。

**(I2) 仅结构编辑**。engineer 仅修改 `model.py` 的结构（AST 算子层面）；`train_command` 作为 shell 原样执行，训练逻辑是用户领地、agent 不越界。

**(I3) 时延门先于训练**。evaluator 形成确定性漏斗：导出 ONNX → 实测时延 → 若 $L(m)\ge L(m^{\*}_{\text{cur}})$（当前 champion 时延）则记 `FAIL_latency` 并**跳过训练**；仅当时延过关才执行 `train_command` 并测精度。此机制避免「时延未改善却白训一轮」的算力浪费。

**(I4) champion ratchet（单调下探）**。全局 champion 由确定性规则更新：

$$m^{\*} \;=\; \arg\min\bigl\{\,L(m)\;:\;\mathrm{status}(m)=\texttt{SUCCESS}\;\wedge\;A(m)\ge A_{\text{target}}\,\bigr\}$$

平局按 FIFO 取先达者。冠军时延单调不增——只接受时延更低且精度达标者为新 champion，永不回退。

### 4.4 AST 差异分类

`structure_gate` 经 `ast_diff.py` 对父子模型做纯 AST 差异，将改动分为三类。判定规则：

- **算子变化（structural）**：任何匹配算子前缀（`nn.`、`torch.nn.`、`F.`、`torch.nn.functional.`、`torch.`）的调用发生增删或类型改变；
- **数值变化（hyperparam）**：匹配数值关键字（`hidden`/`dim`/`depth`/`layers`/`dropout`/`channels`/`heads`/`num_*` 等）的关键字参数改变，且函数签名未变；
- **混合（mixed）**：二者兼有。

为区分结构与数值，`_decl_signature` 遍历 AST 将每个 `Constant.value` 屏蔽为占位符后再比较 `ast.dump`，从而滤除纯字面量改动。门控默认通过（不拒绝候选），但其标签喂入 explore/exploit 路由的结构配额（`structural_slot_ratio=0.5`），保证探索不退化为纯超参调优。

### 4.5 路由与终止

**explore/exploit 路由**为二元决策：

$$\mathrm{route} \;=\; \begin{cases} \text{exploit} & \text{本轮产生了新 champion} \\ \text{explore} & \text{否则} \end{cases}$$

exploit 模式强化当前致胜的原语族，explore 模式注入分布外（OOD）结构原语。hypothesizer 消费该路由调整提议倾向。

**终止条件**由确定性 reducer 决定：

$$\mathrm{continue\_loop} \;=\; \neg\Bigl[\bigl(L(m^{\*})\le L_{\text{target}}\;\wedge\;A(m^{\*})\ge A_{\text{target}}\bigr)\;\vee\;\bigl(\mathrm{round}\ge \mathrm{max\_rounds}\bigr)\Bigr]$$

即 champion 达成双目标、或达最大轮数时终止。

### 4.6 知识库机制

外部 KB（`knowledge_base/`，按族索引）经 `index.json` 组织。各 LLM 节点按需加载切片：hypothesizer 读 `common.principles` + `common.latency_heuristics` + `<family>.primitives` + `<family>.latency_moves`；engineer 读 `<family>.patterns` + `primitives`；analyst 读 `<family>.failures`。其中 analyst 是唯一写回 KB 的节点，将成败归因追加至 `failures.md`/`principles.md`，使后续轮次的提议基于累积经验——构成检索增强生成的闭环。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 目标 | $\min$ 时延，s.t. 精度 $\ge A_{\text{target}}$ |
| 时延测量 | ONNX Runtime，20 runs（5 预热），取中位数 |
| 循环 | 最多 `max_rounds` 轮，champion ratchet 单调下探 |

### 5.2 结果（典型轨迹）

```
基线: latency 500ms, acc 0.72  →  目标: ≤100ms, acc≥0.70
轮1: 换 attention 变体   → 320ms / 0.71  (champion)
轮2: 减层数              → 180ms / 0.70  (champion)
轮3: 改 patch 化         →  95ms / 0.70  ✓ 达标 champion
最终: latency 95ms（降 81%），精度仅掉 2pp
```

### 5.3 计划截图

- **scatter/line 图**「时延-精度下探曲线」（主图）：每轮一个点（横轴=latency，纵轴=acc），champion 路径连线，目标区高亮。
- **bar 图**「每轮时延降幅」：横轴=轮次，纵轴=latency，展示 ratchet 单调下降。
- **table**「探索账本」：轮次 | 改动描述 | latency | acc | 成/败 | analyst 归因（写回 KB）。

> 标注「📊 计划截图」处为预设图位。原理示意以 ASCII 内联；真实数据图待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

开放结构搜索的候选质量受 KB 覆盖度与 LLM 提议能力制约；结构改写可能引入难复现的训练方差。champion ratchet 的单调性保证时延不回退，但可能在精度边界附近过早收敛于局部最优，必要时可放宽 `accuracy_target` 或增大 `max_rounds`。本 workflow 不训练超网、每个候选独立训练评估，单轮代价高于超网 NAS，适用于结构瓶颈明确、超网搜索已穷尽的场景。

---

## 附录 A：结构与度量脚本接口手册

本 workflow 的确定性度量与归约由 `workflows/agents/_struct_scripts/` 下的脚本提供；延迟测量复用 `nas_agent` 库。下列接口供用户独立调用。

### A.1 时延测量：`latency_onnxrt.py`

```python
# 经 importlib 动态加载为 cost_model
measure(onnx_path, runs=20, warmup=5) -> float   # 返回中位数延迟（ms）
```

底层 `nas_agent.latency.latency_ort.measure_ort_latency` 提供更丰富的 `LatencyStats`（支持 GPU IOBinding）。

### A.2 ONNX 导出：`export_onnx.py`

```python
export_onnx(model_path, build_fn, dummy_input, opset=17, out=None, device="cpu") -> str
# 动态加载 model.py，取 build_fn，factory() 后 torch.onnx.export
```

### A.3 AST 差异：`ast_diff.py`

```python
diff(parent_path, child_path) -> DiffResult
# 返回 {topology_changed, operator_changes, numeric_changes, added, removed, summary}
# 算子前缀：nn./torch.nn./F./torch.nn.functional./torch.
# 数值关键字：hidden/dim/depth/layers/dropout/channels/heads/num_*/...；签名屏蔽 Constant.value
```

### A.4 基线测量：`measure_baseline.py`

```python
measure_baseline(args) -> dict
# 编排：导出 ONNX + 加载 cost_model + 解析精度（正则/JSON 行）+ 种子 champions.jsonl
# accuracy_mode 优先级：test_command > baseline_accuracy > train_command
# accuracy_target 默认 = acc − 0.005
```

### A.5 账本归约：`ledger_reducer.py`

```python
reduce_ledger(
    *, ledger_path, champions_path, candidate,
    target_latency_ms, accuracy_target, max_rounds,
    baseline_latency_ms, baseline_accuracy,
    structural_slot_ratio=0.5,        # 结构改写软配额
) -> dict
# 输出：账本追加 + 全局 champion（FIFO 平局）+ route_mode(exploit/exploit) + continue_loop + terminate_reason
```

`_global_best_champion` 取 `status==SUCCESS ∧ met_accuracy` 中时延最小者；`route_mode` 据本轮是否产新 champion 决定；`continue_loop` 由 §4.5 终止条件决定。

### A.6 nas_agent 训练辅助

`nas_agent.train`（`nas_agent/train/__init__.py`）导出分布式训练辅助（`isolate_device`/`resolve_device`/DDP 工具）、知识蒸馏损失（`logits_kd_loss`/`cosine_kd_loss`/`mse_kd_loss`/`KDWeightScheduler`）、`AverageMeter` 与检查点工具，供候选训练与最终重训复用。
