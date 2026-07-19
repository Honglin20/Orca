# nas-hp-search —— 轻量 NAS 超参搜索（slim）

> NAS 三件套里的**轻量版**。**回答一个问题：宽度/深度这些超参怎么配，模型又小又准？** 只搜超参、不改 block 组件，脚本化选架构（不调 LLM 评估）。

## 1. 一句话定位

输入一个模型，生成 Elastic 超网 → 训练 → 搜索宽度/深度超参 → 脚本化挑 top-K 架构。slim 5 节点，跳过 LLM 评估，比 `nas-agent-pipeline` 快得多。

## 2. In-session 如何激活

```
用 TARS 搜一下这个模型的宽度深度超参
TARS，跑个轻量 NAS，挑几个又小又准的子网
```

匹配命中的 description 关键词：**「超参搜索 / 宽度 / 深度 / slim / 轻量 NAS」**。

等价手动命令：

```bash
orca nas-hp-search --inputs '{
  "model_path": "demo_target/model.py",
  "project_root": "demo_target/",
  "output_dir": "llm_artifacts/mymodel/"
}'
```

## 3. 输入 / 输出

**输入**（极简 3 个）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` | — | 模型入口（如 model.py） |
| `project_root` | — | 项目根 |
| `output_dir` | `""` | 空→`llm_artifacts/<model_name>/` |

**输出**：top-K 选中架构（`final_report.md`）+ 超网训练曲线图 + 搜索结果图。

## 4. 算法原理

### 什么是 Elastic 超网

传统 NAS：每个候选架构都从头训一遍 → 算力爆炸。**Elastic 超网**（Once-for-All / BigNAS 思路）：**只训一个「超网」**，它内含许多「子网」（不同宽度/深度），子网共享权重——训完一次，采样任意子网直接评估，不用重训。

```
┌─────────────── 一个 Elastic 超网 ───────────────┐
│                                                │
│   width:  0.5 ── 0.75 ── 1.0 ── 1.25           │  ← 弹性宽度
│   depth:  2 层 ── 4 层 ── 6 层 ── 8 层          │  ← 弹性深度
│                                                │
│   训练时：每步随机采一个 (width, depth) 子网     │
│           做前向+反向（in-place 跳层/切片）       │
│   训完：任意子网 = 从超网里「裁」出对应层/通道    │
│                                                │
└────────────────────────────────────────────────┘
        一次训练 → 搜索空间 = 所有子网组合
```

### slim 搜索流程（5 节点）

```
model_optimizer      生成最小 Elastic 超网（只读模型 + Elastic 速查 + 最小模板，
elastic_optimizer    不展平/不读 optimize_rules，上下文从数十文件压到 3 文件）
        ↓
train_script_gen     生成 train_supernet.py（内联 _push_chart：训练 loss/acc live 推图）
        ↓
search_pipeline_gen  生成搜索脚本（采样子网 → 评估 → 排名）
        ↓
runner               真跑训练 + 搜索（search.jsonl 落真记录，output_schema 强制 search_records≥1
                     防假执行）
        ↓
select               脚本化挑 top-K 架构（subprocess + 模板填空 final_report.md，零 LLM）
```

### 与 `nas-agent-pipeline` 的区别

| | nas-hp-search（slim） | nas-agent-pipeline（full） |
|---|---|---|
| 搜索对象 | 宽度/深度**超参** | 超参 **+ block 组件类型** |
| 评估 | 脚本化（零 LLM） | LLM 评估（evaluator 节点） |
| 节点数 | 5 | 7 |
| 速度 | 快 | 慢（LLM 调用多） |

slim 适合「先快速看看超参空间」，full 适合「连 block 类型都要换」。

## 5. 结果示例 + 计划截图

**典型产出**（slim，demo_target）：

```
超网训练 N epoch → 搜索采样 M 个子网 → select 挑 top-3
final_report.md：3 个候选架构（width/depth/params/acc/latency 对比）
```

### 📊 计划截图（放这里）

- **line 图**「超网训练曲线」（内联推）：x=epoch，y=train loss + val acc（双线），live 流。
  > 占位：两条随 epoch 下降/上升的折线。
- **scatter/bar 图**「搜索子网分布」：每个采样子网一个点（x=params 或 latency，y=acc），top-K 高亮。
  > 占位：散点图，点的颜色按是否被 select 选中；右上角小而准的子网被高亮。
- **final_report.md**：top-K 架构的 Markdown 表格（width × depth × params × acc × latency）。
