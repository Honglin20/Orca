# prof-opt v8 —— 同基前沿探索（永不晋升 + frontier 快照）设计草稿

> 状态：**待用户确认**（2026-09-10 会话）。确认后据本稿撰写 v8 SPEC。
> 前序：v7（`prof-opt-v7-spec.md`）；本稿为跨节点架构议题的前置草稿。

## 1. 背景与动机

v7 实跑暴露两个缺口 + 一个结构性不匹配：

1. **在飞失明**：变体训练未达终态时，history.jsonl 只有 `latency_improved` 过程行
   （dedup 明文"永不阻塞"）。后续轮 propose 看到的全是正面时延证据——精度信号
   **不存在于任何盘面位置**。而数据其实已在盘上：watchdog 每 epoch 原子重写
   `variants/<vid>/train_status.json`（`{stage, epoch, metric, gap,
   over_budget_streak}`，watch_variant.py write_status）——只差读端。
2. **精度失败无机械换路**：`direction.json` 的 `failed_sigs` 只在时延 5 轮修复
   耗尽路径写入并校验（check_propose_emit 仅 latency_fail 路径）；`accuracy_fail`
   从不进 failed_sigs，dedup 有意放行（组合恢复是新 sig）。"避坑"完全靠 LLM 读
   history 行的自觉。
3. **异步时代 vs 链式血缘的结构性不匹配**：训练已流水线化（多轮在飞），链式晋升
   却假设串行换代。各代换树使跨变体比较打折；同基并行探索让所有变体**同树实现、
   同锚实测**，帕累托前沿才是干净可比的。

## 2. 已拍板（用户 2026-09-10）

| 决策 | 结论 |
|---|---|
| base 晋升 | **永不晋升**——树冻结在 origin，退役树换机制 |
| 被支配变体 | **照旧放行训练**（advisory，不设机械拦截门） |
| 历史管理形态 | append-only 机械层（history.jsonl 不动）+ **派生快照**读路径；不做"总结文本"（避免第二真相源） |

## 3. 设计

### 3.1 base 永久冻结在 origin

- po_gate 不再调用 promote_incumbent → `base/incumbent.json` 永不产生。
- **关键既有事实**：`history_lib.expected_base()` 与 `check_verdict.py` 在
  incumbent.json 缺席时已天然回退 origin 锚——永不晋升的核心改动就是 gate 停调
  promote，其余全部是清理。
- recheck 比较线恒 = origin 锚 `baseline_makespan_cycles`（`latency_improved`
  语义不变：严格低于该线）。
- gate 的 `--incumbent-promoted` / `incumbent_promoted` idle 重置逻辑、
  `incumbent_promotion.json` 一并退役。

### 3.2 新确定性脚本 `frontier_snapshot.py` → `base/frontier.json`

数据源仅两处（零训练侧改动）：history.jsonl 终态行 + `variants/<vid>/train_status.json`。

```json
{
  "anchor":   {"baseline_makespan_cycles": N, "target_cycles": N, "accuracy_budget": F},
  "frontier": [{"vid","change_sig","target_modules","makespan_cycles",
                "final_acc","gap","within_budget"}],
  "in_flight":[{"vid","stage","epoch","metric","gap","over_budget_streak","risk"}],
  "avoid":    [{"vid","change_sig","outcome"}]
}
```

- **frontier**：success 终态行内的非支配集（makespan 升、gap 降；并列按 vid）。
- **in_flight**：最新行 `latency_improved` 且无终态的 vid，逐字段读
  train_status.json；`risk` 纯机械分级（streak≥1 → at_risk；watchdog 已进早停
  路径 → stopping；否则 on_track）——精确阈值 SPEC 定。
- **avoid**：`accuracy_fail / probe_insufficient / latency_fail` 终态行全集。
  取代 failed_sigs（机械派生 vs agent 手写，同一信息不再双写）。
- in_flight / avoid 不参与支配计算（数据不完整）。
- 快照是决策输入：锚缺失、行撕裂、状态文件 unparseable 一律 **fail loud**，
  不静默降级出残缺前沿。
- 刷新时机：po_propose Step 0（propose 前现算）+ po_gate（决策前现算）。
  幂等纯读派生，随时可再生。

### 3.3 propose 上下文换血（三件套，有界）

候选 proposer / selector 的简报从「raw history 累积 + 全部前序 variant MFU 报告
+ failed_sigs 手工 union」（随轮数无界）改为：

1. `base/frontier.json` 全文（数字层，O(变体数)）；
2. 上轮 `analysis.md`（既有 ~15 行帽，叙事层）；
3. `accuracy_rules` 快照（既有教训层，机制不动）。

上下文随轮数**有界**。原始 history.jsonl 仍是判 winner / 审计基座，agent 不再直读。

### 3.4 selector 显式声明 absorbs / avoids

`architecture_decision.md` 必须含两节：

- **absorbs**：吸收了前沿哪些 vid 的什么机制、如何融合进新设计（组合谱系代替树谱系）；
- **avoids**：避开了 avoid 清单的哪些 vid、为何不再同路。

check_propose_emit 校验两节引用的 vid 均存在于盘面（机械）；引用不存在 → emit 门拦截。
proposals.json 增可选 `absorbs: [vid...]` 披露字段。

### 3.5 退役清单

| 退役项 | 说明 |
|---|---|
| `promote_incumbent.py` | 连同 gate_node.sh 的调用 |
| `--incumbent-promoted` + idle 重置 | gate_decide 参数与分支 |
| `direction.json` / `failed_sigs` | propose 写端、check_propose_emit 校验端、Step 1 union——被 `avoid` 派生取代 |
| proposals/history 行 `parent_vid`、`base_at_proposal` | v8 起恒 null/锚，字段删除；IMPL_FIELDS 同步收缩 |
| `expected_base()` incumbent 分支 | 只留 origin 锚分支（含 fail loud） |
| report「incumbent 进展披露」 | 改 frontier 进展披露；winner 判定不变（本来就读 history） |

## 4. 代价与观察点（诚实记录）

- **组合深度**：树不换 = 每轮设计要在 origin 树上一步融合全部已验证机制才能逼近
  大目标。selector 本就做融合，差距可接受，但机制越叠越多，单轮实现风险与精度
  交互验证成本上升。**退路（不设计、仅备查）**：若多轮观察融合设计顶不住组合
  深度，重新引入"纯 ratchet 晋升"——只推进时延比较线，永不换树。
- **被支配变体仍烧卡**：已拍板接受；proposer 看到 frontier 后支配性重复自然减少。

## 5. 兼容性

- v8 入口检测旧工作区特征（`base/incumbent.json` 存在 ∨ history 行含
  `parent_vid` 字段）→ **fail loud**，提示 `fresh_start=true` 重建（复用版本
  不匹配的既有语义轨道）。
- rules_pool / 流式早停 / probe 占卡 / report 收割 / 写回全链不动。

## 6. 非目标

- web 前沿可视化（dashboard 已有曲线/pareto；v8 只落 JSON，前端另立项）。
- 训练侧（watchdog / finalizer / 占卡）任何改动。
- 精度规则池（rules_pool）机制改动。
