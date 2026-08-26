# Release: push_describe baseline→elastic 对比表富化（2026-07-31）

## 背景

`nas-agent-pipeline`（`model_optimizer` 节点）/ `nas-hp-search`（`elastic_optimizer` 节点）末尾推的
baseline→elastic 结构对比表被用户反馈「毫无信息量」——典型行如 `fc2 Linear(?->?) ElasticLinear`：

1. **层名无意义**：写死 `conv{idx}` / `fc{idx}`，丢失源码里的真实属性名。
2. **替换前维度是 `?`**：`in_channels` / `num_classes` 等构造参是变量名，AST `literal_eval` 直接返回 None。
3. **替换后超网维度看不到**：conv 的「替换后」只拼 depth+block 串、丢 `stage_widths`；Linear 只写 `ElasticLinear` 无维。
4. **组件替换被埋**：`stage_layer_configs` 的 block 选择（tiny_conv / res_conv / dw_conv）挤在一个长串里、head 完全没有。

## 改动

`workflows/agents/{pytorch-model-optimizer,elastic_optimizer}/scripts/push_describe.py`（两份同步），
表头从 3 列 `[name, 替换前, 替换后]` 重构为 5 列 `[层名, 替换前, 替换后, 超网维度(后), 组件/深度/核候选]`：

| 列 | 来源 | 解决 |
|---|---|---|
| 层名 | AST 赋值目标真名（`self.head`→`head`；`self.features = nn.Sequential(...)` 内→`features[0]/[3]/...`，下标对所有 positional 计位）；匹配不到才 fallback `conv{idx}/fc{idx}` | ① |
| 替换前 | baseline 类型 + 维度。符号表消解变量名，优先级 `__main__` 实例化 kwargs > `__init__` 形参默认 > 模块级常量；仍非常量才显 `?`（不编造） | ② |
| 替换后 | elastic 类型：`stem（固定）` / `ElasticConv2d` / `ElasticLinear` | — |
| 超网维度(后) | conv→匹配 stage 的 `stage_widths[i]`（`super_out_ch` / `super_emb_dim`）；head→`super_in`(末级宽度)→`super_out`(num_classes)；stem/非常量→`—` | ③ |
| 组件/深度/核候选 | `stage_depth_candidates[i]` + `stage_layer_configs[i]` 的 block 选择与参数候选 | ④ |

确定性逻辑（rule 5）：全程 AST 静态解析 + `SearchSpace` dataclass `asdict`，不实例化模型、不靠 LLM 判断。
fail-soft 不变：import / 字段缺 / AST 失败仍推 ERROR 兜底表（F1），`?`/`—` 仅在确实无法静态推断时出现。

## 实测（demo_run / tiny_cnn）

| 层名 | 替换前 | 替换后 | 超网维度(后) | 组件/深度/核候选 |
|---|---|---|---|---|
| features[0] | Conv2d(3→16, k=3) | stem（固定） | — | — |
| features[3] | Conv2d(16→32, k=3) | ElasticConv2d | super_out_ch=32 | depth∈{1,2}; 组件: tiny_conv(k∈{3,5}) \| res_conv(...) \| dw_conv(...) |
| features[6] | Conv2d(32→64, k=3) | ElasticConv2d | super_out_ch=64 | depth∈{1,2}; 组件: ...（hidden/expand∈{64,128}） |
| head | Linear(64→10) | ElasticLinear | super_in=64→super_out=10 | — |

## 测试

新增 `tests/workflows/test_push_describe.py`（10 测试，纯函数级——`_build_symbols`/`_collect_baseline` 走 AST、
`_build_rows` 喂手搓 SearchSpace dict，不依赖 nas_agent / orca.chart 运行时）：
层名真值、维度变量消解、5 列 + 超网维度 + 组件候选、表头契约、非 head Linear 不臆造、非常量 out_ch fail-loud、
`__main__` kwargs 优先级 override、transformer `stage_emb_dims` 标签 + head 冲突暴露、两副本 `filecmp` 同步性、
无 `*_flat.py` 的 `_err` ERROR 兜底表。全绿。

## 实测 + review 后修正（2026-07-31）

两轮收口（实测 + code-reviewer 审查 4d55c2b）：

1. **非 head Linear 不臆造**（实测发现）：MLP 的 fc1 / transformer 的 embed 原用 baseline 维度冒充「超网维度(后)」，
   但 `SearchSpace` 只标准化 head 的超网维度。修正：非 head Linear 显 `—`；head 维度不变。
2. **`_build_rows` 进 try**（review 发现）：原在 `main()` 的 try 之外，SearchSpace 字段类型异常会绕过 `_err` ERROR 兜底表、
   只在 stderr 留 traceback（与「绝不静默空图」相抵触）。修正：并入 try/except → `_err("组装对比表失败: ...")`。
3. **head `super_in` 暴露矛盾**（review 发现）：原静默取末级 stage 宽度，掩盖 baseline `in_feat` 与之不一致
   （transformer / 带 pooling 的非直连 head）。修正：`in_feat` 已解析且 ≠ 末级宽度时显 `?(baseline=X,last_stage=Y)`，
   不静默选一个；demo_run（in=64==末级 64）行为不变。

Commit: `4d55c2b`（主体）+ `22e0762`（实测：非 head 不臆造）+ `54cf33a`（review：try 闭环 + 矛盾暴露）。
