# Orca Workflows 使用与原理文档

每篇由浅入深：**in-session 如何激活 → 输入输出 → 算法原理 → 结果示例 + 计划截图**。

> 怎么「激活」一个 workflow？本质是**命中 TARS skill + workflow 的 description**——跟主 session 说「用 TARS 做 X」，TARS 用 `orca list` 拿全部 workflow 的 description 做语义匹配。安装与通用用法见 [in-session 使用指南](../in-session-usage.md)。

## NAS（3 个）

| workflow | 文档 | 一句话 |
|---|---|---|
| `nas-hp-search` | [doc](nas-hp-search.md) | 轻量：Elastic 超网只搜宽度/深度超参，脚本化挑 top-K（slim 5 节点） |
| `nas-agent-pipeline` | [doc](nas-agent-pipeline.md) | 完整：端到端 NAS，超参 + block 组件都搜，LLM 评估（7 节点） |
| `agent-struct-exploration` | [doc](agent-struct-exploration.md) | 激进：LLM agent AST 级改结构、实测时延+精度，降时延保精度（不依赖超网） |

## 量化（4 个，全 mxint 基）

| workflow | 文档 | 一句话 | 对比轴 |
|---|---|---|---|
| `quant-sensitivity`（W1） | [doc](quant-sensitivity.md) | 敏感层分析：哪些层怕量化 | 4 种分析方法 |
| `quant-ptq-sweep`（W2） | [doc](quant-ptq-sweep.md) | 训练后量化算法扫描 | SmoothQuant/QuaRot/GPTQ/AutoRound/Q2N(零空间) |
| `quant-bit-curve`（W3） | [doc](quant-bit-curve.md) | 混合精度 Pareto 位宽-精度曲线 | INT8/W4A8/INT4/MX4/MX8 格式 |
| `quant-qat`（W4） | [doc](quant-qat.md) | 量化感知训练 + CAGE 后校正 | rtn / duquantpp 训练态方案 |

### 量化 pipeline 推荐顺序

```
quant-sensitivity（W1，找敏感层）
        ↓ 喂敏感层信息
quant-ptq-sweep（W2，挑 PTQ 算法，固定位宽）
        ↓ 想压更低比特
quant-bit-curve（W3，找位宽-精度 Pareto 前沿）
        ↓ 低比特掉点想拉回
quant-qat（W4，QAT + CAGE 恢复精度）
```

> 截图说明：文档里标 📊 的位置是**计划放截图处**。能画的（流程图/原理示意/概念图）已用 ASCII 内联画出；真实数据图（line/bar/heatmap/scatter）留占位，附「该放什么图」一两句描述——跑一次 workflow 即可由 web 面板（`orca open`）截真实图替换。
