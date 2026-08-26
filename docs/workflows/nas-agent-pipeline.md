# nas-agent-pipeline：端到端 NAS（超参与组件搜索）

> Orca NAS 三件套的完整版。在 `nas-hp-search` 的超参轴（宽度、深度、核）之上增加 **block 类型轴**，使搜索空间覆盖组件级选择（如 MBConv / TransformerEncoder）。2026-07-22 P6 重设计后对齐 slim 确定性护栏：7→5 节点（删 viz_describe / LLM evaluator / viz_finalize，viz 内联进 setup、选架构脚本化复用 slim `nas-select`），补 KPI inputs，project_root infer-once 下沉。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`nas-agent-pipeline` 读取用户模型，经重量级优化器（展平、应用 `optimize_rules`、架构分类）生成包含 block 组件维度的完整弹性超网，训练后在「超参 × block 类型」的扩展搜索空间上运行 NSGA-II，最后由**脚本化 select 节点**（无 LLM，复用 slim 的 `nas-select`）从 Pareto 前沿选 top-N 架构 + 生成 `final_report.md` + 推 C5/C6 可视化。

### 1.2 架构与流程

该 workflow 为 **5 节点线性链**，与 slim（`nas-hp-search`）拓扑同构、确定性护栏一致（`model_optimizer` output_schema + `train_runner` output_schema `search_records minimum:1`）。与 slim 的唯一差异是节点 1 用重量级优化器（展平 + `optimize_rules` + 架构分类 + 含 block 维度的超网）。

```
 model_optimizer            生成完整 Elastic 超网（重量级，含 block 维度）
 (pytorch-model-optimizer)  展平模型 → <base>_flat.py；应用 optimize_rules；
                            分类 NAS 架构；生成超网；子 agent 迭代修复；
                            output_schema 暴露 project_root + KPI（infer-once）
                            + model_type enum [cnn|...|unsupported]：
                            unsupported → 路由短路 $end（不烧训练/搜索算力）
        ↓
 train_script_gen           生成 train_supernet.py（内联 _push_chart 实时推图）
 (supernet-train-script)
        ↓
 search_pipeline_gen        生成搜索脚本（子网采样含 block_type 维度）
 (nas-search-pipeline)      KPI（latency_constraint / max_rounds）透传 search_config.yaml
        ↓
 train_runner               真实执行训练 + 搜索（output_schema 强制 search_records≥1 防假执行）
 (nas-train-runner)
        ↓
 select                     脚本化挑 top-N 架构（零 LLM，与 slim 复用同 agent）
 (nas-select)               select_and_report.py → nas-select-architecture CLI
                            → final_report.md + 推 C5/C6 图表
```

### 1.3 输入 / 输出

**输入**（6 个；与 slim 同输入契约。project_root 已下沉给 `model_optimizer` 节点从 model_path 推断，output_dir 暂留 [default]，Phase 4-A 将 sink 到 `$ORCA_ARTIFACTS_DIR`）：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_path` | string | — | [ask] 模型入口 |
| `output_dir` | path | 推断 | [default] 留空→`llm_artifacts/<model_name>/` |
| `target_hardware` | string | — | [ask] cuda\|npu\|cpu，目标硬件（无目标=瞎搜） |
| `latency_constraint` | string | `""` | [ask] 目标时延(ms)，透传 search_config.yaml 剪枝过时延候选；空=无硬约束 |
| `max_rounds` | string | `20` | [ask] NSGA-II 代数预算闸门（透传 num_generations） |
| `seed` | string | `0` | [ask] 复现性种子，全 workflow 必填 |

**输出**：`final_report.md`（最优架构报告，含 block 组成/参数量/延迟/精度对比）+ `selection_summary.json` + 可视化（C1/C2 由 setup 末尾内联推送，C5/C6 由 select 末尾内联推送）。workflow 产出为 `select` 节点的 `result`。

### 1.4 如何激活

```
用 TARS 做一轮完整的 NAS，连 block 类型也搜
TARS，端到端搜个最优架构，组件和超参都搜
```

匹配命中的关键词：**端到端 NAS / 超网 / block 组件 / Elastic / 架构搜索**。等价手动命令：

```bash
orca nas-agent-pipeline --inputs '{
  "model_path": "demo_target/model.py",
  "output_dir": "llm_artifacts/mymodel/",
  "target_hardware": "cuda",
  "latency_constraint": "10.0",
  "max_rounds": "20",
  "seed": "0"
}'
```
（`project_root` 不是 input——agent 从 `model_path` 向上走自动推断。）

---

## 2. 定义

本 workflow 的搜索空间为 `nas-hp-search` 超参空间与 **block 类型轴**的直积。形式化地，设轻量版空间为 $\mathcal{S}_{\text{hp}}$，本 workflow 在每个活动位置 $p$ 引入候选 block 集合 $\mathcal{B}_{p}$，扩展为

$$\mathcal{S}_{\text{full}} \;=\; \mathcal{S}_{\text{hp}} \;\times\; \prod_{s,p}\mathcal{B}_{s,p}$$

**ChoiceLayer** 是承载 block 类型轴的原语：每个位置持有一个分支字典 $\{b\to\text{module}_b\}$（≥1 候选 block），搜索时由 `set_sample_config(choice_name=b)` 选定其一执行前向。子网采样在 `nas-hp-search` 的均匀采样基础上，对每个位置额外均匀抽取一个 block 类型：

$$\alpha \;=\; \Bigl(\,\cdots,\;\;b_{s,p}\sim\mathrm{Uniform}(\mathcal{B}_{s,p}),\;\;c_{s,p,k}\sim\mathrm{Uniform}(\mathcal{C}_{s,b,k})\,\Bigr)$$

由此搜索空间基数相对轻量版放大 $\prod_{s,p}|\mathcal{B}_{s,p}|$ 倍。

---

## 3. 背景

### 3.1 组件级搜索的动机

当模型精度/延迟瓶颈不在超参配置，而在 block 组件类型本身（如需将 CNN block 替换为 attention block）时，仅调宽深无效，必须将 block 类型纳入搜索。该混合 block 搜索遵循 ProxylessNAS（Cai et al., 2019）与 BigNAS（Yu et al., 2020）的混合架构搜索范式。

### 3.2 脚本化 select 的定位

不同 block 类型的架构在参数量、延迟、精度上的差异非线性。NSGA-II 产出的 Pareto 前沿可能含多个在纯指标上接近、但结构迥异的候选。本 workflow 在节点 5 复用 slim 的 `nas-select` agent（**零 LLM**）：`select_and_report.py` subprocess 调 `nas-select-architecture` CLI 取 top-N → 读 `selection_summary.json` + `search.jsonl` 模板填空生成 `final_report.md` → 推 C5 终态 Pareto + C6 漏斗。该步骤不参与搜索算法本身，仅作用于搜索结果的后处理选择，且**完全确定性**（P6 前为内联 LLM evaluator，已删）。

---

## 4. 方法

### 4.1 重量级超网构建

与轻量版仅套用最小模板不同，节点 1 的 `pytorch-model-optimizer` 执行完整的 7 步流水线：(i) 展平用户模型为 `<base>_flat.py`；(ii) 按 `optimize_rules` 推荐并应用结构优化规则；(iii) 将模型分类为 CNN / 分层 Transformer / 各向同性 Transformer 之一；(iv) 据分类生成含 block 组件维度的完整超网；(v) 经子 agent 迭代修复超网评估问题。该流程的上下文较重，但能识别并暴露 block 级可搜维度。

### 4.2 训练与搜索

超网训练与 NSGA-II 搜索的机制与 `nas-hp-search` §4.2–4.3 一致（均匀采样子网交替训练、整数基因编码、均匀交叉、随机重置变异、非支配排序环境选择、目标 $\min(-\mathrm{acc},\mathrm{latency})$），区别仅在于基因维度增加了对应每个位置 block 选择的分量，编解码由生成的 `arch_codec.py` 处理 `ChoiceLayer` 的分支映射。

### 4.3 脚本化 select 节点（节点 5）

节点 5 复用 slim 的 `nas-select` agent（folder-agent），主脚本是 `select_and_report.py`：subprocess 调 `nas-select-architecture` 选 top-N → 读 `selection_summary.json` + `search.jsonl` → **模板填空** 生成 `final_report.md`（不调 LLM）→ 推 C5 终态 Pareto + C6 漏斗（sidecar `|| true`）。`output_schema` 不强制该节点（终态节点，stdout 原样作为 `outputs.result`），但脚本对 `nas-select-architecture` 失败 fail loud（非零退出 + 原因写 final_report.md）。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 搜索空间 | 超参轴（宽/深/核）× block 类型轴 |
| 搜索算法 | NSGA-II + LLM 后处理选择 |
| 子网评估 | 继承超网权重，免重训 |

### 5.2 结果

典型产出为：超网训练 → 搜索 `(width, depth, block_type)` 组合 → 脚本化 select（零 LLM）→ 最优架构。`final_report.md` 含参数量/延迟/精度/block 组成对比 + 选择依据（模板填空，可审计）。

### 5.3 计划截图

- **C2 图**「超网搜索空间」：宽度 × 深度 × block 类型的网格示意。
- **line 图**「超网训练曲线」：loss + acc 实时流。
- **C5 图**「最终 Pareto 前沿」：搜索候选散点 + 选中架构高亮。
- **final_report.md**：top 候选对比表（含 block_type 列与 LLM 评语）。

> 标注「📊 计划截图」处为预设图位。原理示意以 ASCII 内联；真实数据图待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

block 类型轴的引入使搜索空间显著扩张，对超网训练时长与 NSGA-II 评估预算要求更高；block 类型间的权重共享亦较同质 block 更难均衡。脚本化 select 完全确定性，可由 `selection_summary.json` 与 `search.jsonl` 审计。当预设的 block 候选集仍无法触及目标时，应转用 `agent-struct-exploration`，由 LLM 在开放结构空间上提议任意改写。

---

## 附录 A：nas_agent 库接口手册（增量）

超网原语、`SearchSpace`/`ArchConfig`、`DiscreteNSGA2` 及 CLI 的通用接口见 [`nas-hp-search.md` 附录 A](nas-hp-search.md)。本 workflow 额外使用以下接口。

### A.1 block 类型轴：`ChoiceLayer`

```python
from nas_agent.blocks.choice_layer import ChoiceLayer

# 每个位置持 ≥1 候选 block，搜索时选其一
layer = ChoiceLayer(branches={
    "mbconv": MBConv(...),
    "attn":   TransformerEncoder(...),
})
layer.set_sample_config(choice_name="attn", **choice_kwargs)  # 选定分支
out = layer(x)
subnet = layer.get_active_subnet()
```

### A.2 重量级优化器流水线

节点 1 的 `pytorch-model-optimizer` 执行展平 + `optimize_rules` + 架构分类 + 完整超网生成；其内部经子 agent 迭代修复超网评估问题。相关模板与规则位于 `workflows/agents/pytorch-model-optimizer/`。

### A.3 脚本化 select 契约（节点 5）

节点 5 复用 slim `nas-select` agent（folder-agent），核心脚本 `workflows/agents/nas-select/scripts/select_and_report.py`：subprocess 调 `nas-select-architecture --config <output_dir>/search_config.yaml --input <output_dir>/runs/search/search.jsonl --arch_output_dir <output_dir>/runs/retrain/selected -n 3` → 读 `selection_summary.json` + `search.jsonl` 模板填空 `<output_dir>/final_report.md`（不调 LLM）→ sidecar 推 C5 终态 Pareto + C6 漏斗。CLI 非零退出 → 脚本非零退出 + 原因写 final_report.md（fail loud，不假装完成）；推图失败 `|| true` 不阻断。`outputs.result` 取脚本 stdout 原样（含 `OUTPUT_DIR / SELECTED / SELECTION_SUMMARY / FINAL_REPORT`）。
