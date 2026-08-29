# prof-opt 轮末结论闭环 —— 设计记录（已按最小版实现，2026-08-28）

> 用户指令：时延/精度任务跑完必须有分析结论加入 workflow（不是跑完不管）；多轮运行下内容要受管理。
> 初版草稿（lessons.json 双轴 / latency-analyst / top-N 截断）被用户裁定过度设计——**最小版 = 对应 agent 轮末落盘**，本文记录最终实现。初稿的扩张选项留文末备查，未实现。

## 实现（2026-08-28，与 prompt 洁净清理同工作区）

**机制**：`rounds/<NNN>/analysis.md`——每轮一份分析记录，两个写入者、两节、幂等整节重写：

| 环节 | 改动 |
|---|---|
| **落盘·时延节** | `po_propose` 新 Step 6b（Step 6 推进后、emit 前）：`## latency` 节——准入/淘汰归因（逐条）、预测 vs 实测校准（哪个杠杆族高估/低估）、下轮方向；≤~15 行 |
| **落盘·精度节** | `po_probe` Step 3 末（recovery advance 后）：`## accuracy` 节——逐 vid 结论（gap/outcome/curve-vs-eval）、失败时的误差结构诊断、规则更新 id、下轮方向；≤~15 行 |
| **回流** | `po_propose` Step 3 dispatch inputs 增「上轮 analysis.md 全文」；`structure-proposer` Inputs 增 `<prev_analysis>`（排序与方向选择的直接证据；round 1 缺省） |
| **终态汇总** | `po_report` ledger 读取清单增 `rounds/*/analysis.md`；`report_format.md` §5 增 **Round Conclusions** 节（每轮一行蒸馏 + 2-3 行跨轮总结：杠杆族交付/证伪、校准漂移；缺文件跳过不捏造） |
| **产物申报** | propose `generated_artifacts` / probe `artifacts` emit 字段增 `rounds/<RRR>/analysis.md` |
| **注释同步** | workflow.yaml propose/probe 节点 Step 注释同步（Step6b / 轮末分析落盘） |

## 体量管理（多轮应对，天然有界）

1. **盘面优先**：分析 prose 永远住 `analysis.md`，prompt 只进「上一轮这一份」全文（~15 行帽）——不随轮数累积；
2. **幂等**：整节重写（重入安全），他节保护（latency/accuracy 各自只写自己节）；
3. **蒸馏去重**：结构化教训仍走既有 accuracy_rules（change_pattern 键控 + confidence ladder + 终态 merge），analysis.md 是人可读叙事层，两层不混。

## 验证

`tars validate` 0/0 ✓；宽口径（P-ID 豁免）0 finding ✓；静态 0 error ✓。占位符 `R-1` 曾撞 dev-id 宽口径 pattern，改写为 `<previous round>` 后清零。

## 备查（初稿扩张选项，未实现、未拍板）

- latency 结论结构化为规则（lessons.json 双轴 schema）并做 predict_delta 校准参数化；
- 进 proposer 的规则集 top-N 截断（rules_pool 目前无上限，跨 run 池增长需关注）；
- 诊断协议（分 SNR NMSE / 误差相关分解 / 相位-幅度分解）作为 analyst 工具清单入 references。
