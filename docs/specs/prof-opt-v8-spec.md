# prof-opt v8 SPEC —— 同基前沿探索（永不晋升 + frontier 快照）

> 契约文档，逐字实现。前置草稿：`prof-opt-v8-design-draft.md`（用户 2026-09-10 拍板：
> ①永不晋升 ②被支配变体照旧放行 ③历史 = append-only 机械层 + 派生快照读路径）。
> 引擎（orca/）与 web 前端零引用本次退役概念，改动完全限 `workflows/prof-opt/**` +
> `tests/test_po_*` + 本 docs。

## 0. 语义总纲

- **base 永远是 origin 基线**：`shadow/` 树、`base/model.onnx`、`base/profile`
  从 baseline 冻结后永不再被替换。`base/incumbent.json` 在 v8 工作区**必须不存在**
  （存在 = 旧工作区残留 → fail loud）。
- **准入线恒 = `origin_anchor.baseline_makespan_cycles`**（`latency_improved`
  = 严格低于该线，语义不变）。
- **谱系 = 组合而非树**：`absorbs: [vid...]` 声明设计吸收了前沿哪些 vid 的机制；
  `parent_vid` / `base_at_proposal` 字段退役。
- **避坑 = 机械派生**：`base/frontier.json` 的 `avoid` 段取代 `direction.json`
  / `failed_sigs`（同一信息不再双写）。
- **兼容性 = 锁版本**：`BASELINE.lock` schema version 2 → **3**；旧工作区
  （version 2）在入口 reuse gate 即 exit 3，提示 `fresh_start=true`。

## 1. 新脚本 `frontier_snapshot.py`（_po_scripts/，随 deploy_scripts.sh 部署）

```
用法: frontier_snapshot.py --artifacts <ws>
效果: 原子写 base/frontier.json（tmp + os.replace），stdout 打印同一 JSON；失败 exit 2
```

数据源**仅两处**：`history.jsonl`（history_lib.read_rows）+
`variants/<vid>/train_status.json`。零训练侧改动。

```json
{
  "anchor":    {"baseline_makespan_cycles": int, "target_cycles": int, "accuracy_budget": float},
  "frontier":  [{"vid","change_sig","target_modules","makespan_cycles","final_acc","gap"}],
  "in_flight": [{"vid","stage","epoch","metric","gap","over_budget_streak","risk"}],
  "avoid":     [{"vid","change_sig","outcome"}]
}
```

- **frontier**：候选 = 最新行 `outcome=="success"` 的 vid（数据取该最新行：
  `makespan_cycles` / 终态行 `final_acc` / `gap`；success 本身即预算内，无
  within_budget 字段）。非支配集：A 支配 B ⇔
  `A.makespan<=B.makespan AND A.gap<=B.gap` 且至少一严格。输出按 vid 排序。
  success 行缺整数 makespan 或数值 gap → fail loud。
- **in_flight**：任意版本有 `latency_improved` 行且**任意版本无终态行**的 vid
  （与 gate_decide in_flight 同谓词）。逐 vid 读 `train_status.json`：
  - 文件缺失 → `{"vid", "stage": null, "epoch": null, "metric": null, "gap": null,
    "over_budget_streak": null, "risk": "pending_launch"}`（propose 写行与 probe
    启动之间的合法窗口，**不是错误**）；
  - 存在 → 逐字段透传（`stage/epoch/metric/gap/over_budget_streak`）；
    `risk` 机械分级：`stage in {killed, failed}` → `"terminating"`；
    `over_budget_streak` 整数 >0 → `"at_risk"`；否则 `"on_track"`。
  - 文件存在但 unparseable → **fail loud**（决策输入不允许撕裂）。
- **avoid**：最新行 `outcome in {accuracy_fail, probe_insufficient, latency_fail}`
  的 vid，按 vid 排序。
- **fail loud 清单**：`origin_anchor.json` 缺失/unparseable/缺字段；`history.jsonl`
  unparseable；在飞 vid 的 `train_status.json` unparseable。
- in_flight / avoid **不参与**支配计算。

## 2. 逐文件契约改动

### 2.1 `_po_scripts/gate_node.sh`
- 删除 promote 块（promote_incumbent 调用 / `incumbent_promotion.json` /
  `promotion_arg` / emit 的 `incumbent_promotion*` 字段）。
- gate_decide 之前插入：`frontier_snapshot.py --artifacts "$ART" >/dev/null`
  ——非零退出 → finish-failed payload（`reason="frontier snapshot failed"`，
  error 带 stderr）。
- 头注释改为「frontier 快照 + 纯读决策」。

### 2.2 `_po_scripts/gate_decide.py`
- 删 `incumbent_promoted`（参数 / CLI flag / 分支 / 输出字段）。
- 决策序不变：target-success → max_rounds → idle（无重置子句）→ success-vids →
  默认 loop；reason 文案去晋升化（"continue proposing from the frontier"）。

### 2.3 删除 `_po_scripts/promote_incumbent.py`

### 2.4 `_po_scripts/history_lib.py`
- `IMPL_FIELDS = (vid, round, seq, change_sig, probe_epochs, target_modules,
  predicted_delta_cycles, implemented, absorbs)`。
- `append_implemented(..., absorbs: list[str])`——替换 parent_vid/base_at_proposal。
- `expected_base()`：只留 origin 锚分支，恒返回 `(None, baseline_makespan_cycles)`；
  若 `base/incumbent.json` 存在 → raise（"stale v7 incumbent — v8 has no
  promotion, fresh_start required"）。锚缺失/unparseable 照旧 fail loud。
- `PERMANENT_OUTCOMES` 收缩为 `{"unsupported_op"}`（advanced/promoted 读兼容随
  旧工作区一并退役；v8 工作区不可能产生这些行）；模块 docstring 同步 v8 语义
  （accuracy_fail 换路信号 = frontier.json avoid，机械派生）。
- dedup 其余规则不动（joint retry 预算 / probe_insufficient 永久消费）。

### 2.5 `_po_scripts/append_impl_row.py`
- CLI：删 `--parent-vid` / `--base-at-proposal`；增 `--absorbs`（JSON 数组，默认 `[]`）。
- 谱系门：`expected_base()` 照调（锚缺失 fail loud）；校验 absorbs 每个 vid 在
  history 中存在、且不等于本 vid——违规 fail loud（exit 2）。

### 2.6 `_po_scripts/check_propose_emit.py`
- proposal 必填键：`parent_vid`/`base_at_proposal` → `absorbs`；校验 absorbs 为
  vid 字符串数组、每个 vid 存在于 history、不含自身。
- 删 parent/base_at 匹配校验与 `direction.json`/`failed_sigs` 校验块。
- 行-提案一致性：保留 round/change_sig 匹配；**新增 absorbs 等值检查**（对称，
  全 outcome 路径——L0 行经全量快照合并必然携带 impl 行的 absorbs，正常链路恒
  相等；不等 = 谱系谎言，拒）。
- proposal 级 absorbs 校验：vid 字符串数组、每个 vid 存在于 history、不含自身。
- 新增 `architecture_decision.md` 校验：必须含 `## absorbs` 与 `## avoids` 两节；
  两节文本中以 `r\d+-\d+` 提取的 vid 引用必须存在于 history（无引用合法——如
  round 1 或 avoid 为空；节缺失不合法）。

### 2.7 `_po_scripts/check_verdict.py`
- 删 incumbent.json 分支；准入线恒 = `origin_anchor.baseline_makespan_cycles`；
  若 `base/incumbent.json` 存在 → ValueError（同 2.4 文案）。
- 输出键改名：`incumbent_vid`→删、`incumbent_makespan_cycles`→
  `admission_line_makespan_cycles`；`improvement_cycles` = line - variant。
- 错误文案 "not below incumbent" → "not below the frozen origin line"。

### 2.8 `_po_scripts/run_latency_recheck.sh`
- BASE_MS 只读 origin 锚；输出/日志字段与 reason 同步去 incumbent 化
  （`admission_line_makespan_cycles`；"did not improve the frozen origin line"）。
  修复流（repair_trace / 判定逻辑）不动。

### 2.9 `po_flatten`：`write_baseline_lock.py` + `reuse_check.sh`
- 锁 `version: 2 → 3`；mismatch 文案 v7→v8（"the workspace predates the v8
  lock (no promotion exists) — rebuild with fresh_start"）。

### 2.10 `po_propose/agent.md`（重写相关段落）
- Invariants 谱系条：树恒 origin；`absorbs` 为组合谱系；avoid 清单（frontier.json）
  是下轮换路的机械依据；不得把 dead-end 变体当树。
- Step 0：先跑 `frontier_snapshot.py`（新快照落 `base/frontier.json`）。
- Step 1 简报三件套：`base/frontier.json` 全文 + 上轮 `analysis.md` +
  `accuracy_rules` 快照；**移除** raw history 累积、全部前序 variant MFU 报告、
  failed_sigs 手工 union。候选仍读 baseline 三文档 + 当前 shadow + 硬件参考。
- Step 2：proposals.json 字段（删 parent_vid/base_at_proposal，增 absorbs）；
  校验段落同步（absorbs 引用存在、decision 文档两节齐全；引用错误可 selector 修复）。
- Step 3：`append_impl_row.py` 调用形状更新；**exhausted 路径不再写
  direction.json**——只落 latency_fail 终态行（avoid 自动派生）。
- Step 4：generated_artifacts 去 direction.json。

### 2.11 `po_probe/agent.md` + `references/probe_protocol.md`
- "current incumbent" → "frozen origin line"；准入线来源 = origin 锚（注明 v8
  无晋升、`base/incumbent.json` 必须不存在）；`check_verdict.py` 输出键名同步。

### 2.12 `po_report/references/report_format.md` + `workflow.yaml`
- §1 表后注：miss-target success 是 frontier 点非 winner（去 incumbent 词）。
- §2 winner.lineage 语义 = winner 历史行 `absorbs` 清单（空=未吸收）；§3 写回源
  注释（shadow/ 恒 origin 树）；§4 趋势图零测量轮回退 = origin 锚 makespan；
  §5 胜出者节措辞（absorbs 清单 / "no success variant" 行）。
- workflow.yaml：7 节点注释去晋升化；po_report winner.lineage schema 描述改
  absorbs；po_gate 注释 = frontier 快照 + 纯读决策。

### 2.13 subagents 文案（selector / semantic / sota / variant-implementer）
- 一律 "origin baseline（shadow/ 恒定）" 语汇；selector 谱系段改组合谱系 +
  absorbs/avoids 两节义务；variant-implementer declaration.json schema
  （删 parent_vid/base_at_proposal，增 absorbs verbatim）。

## 3. 测试契约

- `tests/test_po_v8_frontier.py`（新）：支配/并列边界、success 提取、
  in_flight 四态（pending_launch / on_track / at_risk / terminating）、
  avoid 三种 outcome、锚缺失/撕裂 fail loud（库层 + CLI exit 2）、锚字段
  严格整数、原子写。
- `test_po_v6.py` / `test_po_v7.py` / `test_po_scripts.py`：删晋升/parent/
  direction 用例；存活用例改新契约（准入线字段名、gate 输出键集、absorbs 行）；
  emit 门组合谱系闸负路径矩阵（缺节 / 捏造引用 / 自引用 / 对称放行 / 行-提案
  不对称）。
- HEAD 既有失败 3 例中，`test_gate_node_sh_parses_after_quote_fix` 因
  gate_node.sh 重写而恢复；`test_baseline_chain_*` ×2 为既有失败（与本次无关，
  不恶化）。

## 4. 非目标

dashboard/web 前端可视化、训练侧（watchdog/finalizer/device_alloc）、rules_pool
机制、`experiment_ledger.py`/`dashboard_snapshot.py`/`push_curves.py`（不读
parent_vid，零改动）。
