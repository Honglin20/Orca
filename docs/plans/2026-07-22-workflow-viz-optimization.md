# 8-Workflow 图表可读性优化方案

> 日期：2026-07-22 ｜ 类型：事前计划（SDD）｜ 状态：待执行
> 范围：**只改 workflow 推图脚本（producer 侧），不改前端 widget / TUI 渲染层**。
> 背景：P1（commit a7de596）给 `render_chart` 加了 `x_label/y_label/caption`；P7（66f74ea）修了 struct/kd 图表根因。两轮只覆盖了部分图——本方案把能力**一致地铺到全部 8 workflow 的每一张图**。

---

## 0. 关键结论（先看这 5 条）

1. **渲染能力已完备，缺口 100% 在 producer 侧。** Web 8 个 widget + TUI `chart_canvas.py` 都已支持 `x_label/y_label/caption`（P1 已铺到渲染层）。但 8 workflow 共 ~22 张图里，**只有 7 张带了 label/caption**（struct 全 3 + kd 的 table/2 final bar + qat recovery bar + bit-curve pareto），其余 ~15 张裸奔——轴标签是 schema 字段名（`step_idx`/`metric`/`value`/`generation`），用户看不懂。
2. **P1 铺得不一致**：同一脚本里「主角图」加了 label，配角图漏了。典型：kd 的 table+final bar 有 caption，但同脚本的 candidate_trace + pareto 两张坐标图没有；qat 只有 recovery bar 有方向感知 caption；bit-curve 只有 pareto 有。这是最大的单一问题。
3. **metric 方向（↓better/↑better）是量化图的头号阅读陷阱**：ptq/qat/bit-curve 的 y 轴常是 mse（越低越好），但下行的线「看着像坏消息」。`qat Recovery bar` 已示范正解——方向感知 caption（「mse 口径下负值=改善」）。**这个模式必须复制到所有量化坐标图**。
4. **NAS 的「质量目标取负显示」逻辑对用户完全不可见**（`tail_metrics.py` search 模式把 quality 目标显示成 `-v` 让「全轴越大越好」），但轴上只写原始 obj 名，没有 caption 说明取负，用户看到 `neg_acc` 负值会懵。这是 P0 阅读事故。
5. **NAS 终态帕累托（`push_pareto_final.py`）用 `scatter` 而非 `pareto`**：自算了前沿却只靠颜色区分，**前沿连线（tradeoff 阶梯）丢失**，且 `pareto_direction` 不被消费。应切到 `chart_type=pareto`（或至少连线）。live 版（`tail_metrics`）已经是 pareto，final 反而退化——不一致。

**不在本方案范围**（已评估，建议缓做）：新增 `chart_type=dashboard` 组合图（P2，flat-list 已够用，编排复杂度收益不明确）、heatmap 反向色阶开关（P1，caption 可解）、标题中英统一（P2，title 受 dedup 冻结见下）。

---

## 1. 硬约束（执行前必读）

- **dedup 键 = `label` + `title`**（SPEC §2.7，同键 = 替换/刷新，非追加）。**严禁改 `label` 或 `title` 文案**，否则旧图不消失、新图叠加成重复。本方案所有改动**只新增 `x_label`/`y_label`/`caption` 三个参数**，不动 label/title/x/y/hue/color/value/pareto_*。
- **不改前端**：8 widget + `chart_canvas.py` 已支持三字段（Web 经 `chartTheme.getXAxisLabelProp`/`ChartCaption`；TUI 经 plotext `xlabel/ylabel` + caption 后缀）。本方案不改 `orca/iface/**`。
- **不改 `render_chart` 签名**：三字段 P1 已加，直接用。
- **fail-soft 不变**：推图仍包 try/except 不阻断主循环；caption/label 是锦上添花，加错最坏只是某图少行字，不 raise。
- **caption 简短**：前端 `ChartCaption` 渲染为 `text-[10px]` 小字一行，长文案会被压扁。每条 caption ≤ 80 汉字，只讲「轴单位 + 方向 + 关键语义」。

---

## 2. 当前图表盘点（8 workflow × 每图）

状态标记：✅=三字段齐全 ｜ ⚠️=部分 ｜ ❌=全缺 ｜ 🏆=金牌样板（供其它图抄）

### 2.1 agent-struct-exploration（`workflows/agents/_struct_scripts/viz_struct.py`，label=`struct-explore`）

| # | title | type | x/y/hue | x_label/y_label/caption | 评判 |
|---|---|---|---|---|---|
| 1 | Champion Trace | line(hue=series) | index/latency_ms | ✅✅✅ 🏆 | 样板。**唯一瑕疵**：caption 写「★=达标」但全图无 ★ 标记（line widget 画不了目标参考线）——「说谎的 caption」，见 §3 P1-1 |
| 2 | Latency-Accuracy Pareto | pareto(hue=status) | latency_ms/accuracy | ✅✅✅ 🏆 | 样板。方向、baseline、target 都在 caption |
| 3 | Candidate Ledger (per change) | table | — | caption ✅ | 列名是 schema 名（latency_ms/accuracy），table 密集可接受 |

**struct = 全仓库金牌基准**，本方案不动它（仅修 ★ 一处文案）。

### 2.2 kd-nas（`workflows/agents/_kd_scripts/viz_kd.py`，label=`kd-nas`）

| # | title | type | x/y/hue | x_label/y_label/caption | 评判 |
|---|---|---|---|---|---|
| 1 | Candidate Trace (proxy_mse, lower is better) | line(hue=series) | round/proxy_mse | ❌❌❌ | **P0**：proxy_mse 是短训代理**非真实精度**，title 提了但坐标图无 caption 复述——用户会把「线下降」当「精度变好」|
| 2 | Latency–Proxy Pareto (both min) | pareto(hue=met_latency) | latency_ms/proxy_mse | ❌❌❌ | **P0**：双 min 方向 + proxy 语义双缺 |
| 3 | Candidate Ledger (proxy_mse = short-train acc proxy) | table | — | caption ✅ 🏆 | caption 把 proxy 语义讲清了——证明同脚本知道该讲，只是没同步到图 1/2 |
| 4 | Final Latency Compare | bar | stage/latency_ms | ✅✅✅ | finalize 阶段 |
| 5 | Final dB Gap Compare | bar | stage/db_gap | ✅✅✅（含 ⚠ teacher_acc 未知变体） | finalize；方向感知 caption 到位 |

**矛盾点**：图 3/4/5 都有 caption，唯独坐标图 1/2 漏——明显是 P1 漏铺。

### 2.3 nas-agent-pipeline + 2.4 nas-hp-search（共享 NAS 脚本）

> 两 workflow 共用 `elastic_optimizer/push_describe.py`、`nas-train-runner/tail_metrics.py`、`nas-select/push_funnel.py`+`push_pareto_final.py`。训练图 C3a/C3b 由 LLM 生成的 `train_supernet.py` 按 checklist 内联推（`supernet-train-script/references/.../01_training.md` 定额 label/title）。

| 脚本 | title | type | x/y/hue | label/caption | 评判 |
|---|---|---|---|---|---|
| push_describe | Baseline → Elastic（per baseline layer） | table | — | ❌ | P1：列名中文已友好，缺 caption 讲 stem/— 语义 |
| tail_metrics (train) | Training Loss | line(hue=phase) | global_step/loss | ❌❌❌ | P1：轴是 schema 名 |
| tail_metrics (train) | Validation Metric | line | global_step/metric | ❌❌❌ | P1：metric 字段名动态（val_acc/acc/metric），轴只写 metric |
| tail_metrics (search) | Search Convergence — {k} | line(hue=stat) | generation/value | ❌❌❌ | **P0**：quality 目标显示 `-v` 的取负逻辑对用户不可见 |
| tail_metrics (search) | Population & Cache per Gen | bar(hue=kind) | generation/count | ❌❌❌ | P2：title 够清 |
| tail_metrics (search) | Pareto Front (live) | pareto | x_obj/y_obj | ❌❌❌ | **P0**：取负 + 双 min/max 方向 + 轴是 obj 名全缺 |
| push_funnel | Selection Funnel | bar | stage/count | ❌❌❌ | P1：漏斗 6 级语义需 caption |
| push_pareto_final | Pareto Front (final) | **scatter**(hue=status) | x_title/y_title | ❌❌❌ | **P0**：应改 `pareto`——scatter 丢前沿连线 + 不消费方向；且无 caption 讲 ↑/↓better |

**NAS = 重灾区**：8 张图 0 张有 label/caption；且含 2 个 P0 阅读事故（取负、scatter 退化）。

### 2.5 quant-ptq-sweep（`run_ptq_sweep.py`，label=`quant/ptq-sweep`）

| mode | title | type | x/y/hue | label/caption | 评判 |
|---|---|---|---|---|---|
| lw | Cumulative PTQ Path Ablation ({metric_kind}) | line(hue=path) | step_idx/metric | ❌❌❌ | **P0**：step_idx 是「累积技术数」（0=baseline,1=+smooth...）非真实步数，用户必误解；metric 方向只在 title 括号里 |
| lw | Final-step Comparison by Path ({metric_kind}) | bar | path/metric | ❌❌❌ | P1：S/Q/A/R 字母无 glossary |
| lw | Lightweight Sweep — All Steps | table | — | ❌ | P2 |
| full | PTQ Recipe × Bitwidth Matrix ({metric_kind}) | heatmap | bitwidth/recipe/value=metric | ❌❌❌ | **P0**：heatmap 色阶方向致命——mse 下 dark=high=坏，但图上无任何方向提示；color legend 只标 min/max 数值 |
| full | PTQ Metric by Bitwidth ({metric_kind}, coral=best) | scatter(color) | bitwidth/metric | ❌❌❌ | P1：coral=best 在 title，方向缺 |
| full | Full Sweep — All Combos | table | — | ❌ | P2 |

### 2.6 quant-sensitivity（`run_sensitivity.py`，label=`quant/sensitivity`）

| title | type | x/y/hue | label/caption | 评判 |
|---|---|---|---|---|
| Layer Sensitivity by model order (coral=selected) | bar(color) | layer/score | ❌❌❌ | P1：「score」是什么算法的敏感度？高=更敏感？selection 阈值（ratio）多少？全在 report.json 里图上看不到 |
| All Layers (selected ranked) | table | — | ❌ | P2 |

### 2.7 quant-qat（`run_qat.py`，label=`quant/qat`）

| title | type | x/y/hue | label/caption | 评判 |
|---|---|---|---|---|
| QAT Convergence ({metric_kind}, per scheme) | line(hue=scheme) | step/metric | x✅y✅ caption❌ | ⚠️：P1 半铺——有轴标无 caption；方向只在 y_label（mse 时写 lower better）|
| QAT Training Loss | line(hue=scheme) | step/loss | ❌❌❌ | P1 |
| QAT Before vs After ({metric_kind}) | bar(hue=phase) | scheme/metric | ❌❌❌ | P1：方向陷阱——mse 下 after<before=改善，但柱图不直观 |
| QAT Recovery (after−before, {metric_kind}) | bar | scheme/recovery | y✅ caption✅ 🏆 | **方向感知 caption 样板**：「mse 口径下负值=改善」——此模式必须复制到全量化图 |
| QAT Scheme Comparison (all schemes) | table | — | ❌ | P2 |

### 2.8 quant-bit-curve（`run_bit_curve.py`，label=`quant/bit-curve`）

| title | type | x/y/hue | label/caption | 评判 |
|---|---|---|---|---|
| Bit-Width vs {metric_kind} Pareto Frontier | pareto | bit/metric | ✅✅✅ 🏆 | 样板：方向动态（`direction: {pareto_y_direction}`）写进 y_label |
| All Evaluated Candidates (coral=frontier) | scatter(color) | bit/metric | ❌❌❌ | P1：与上图同轴，应 caption 说明「这是全候选云 vs 上图前沿」|
| Selected Candidate — Format Mix (mxint base) | bar | format/layers | ❌❌❌ | P1：layers=每格式层数；format 名（INT8/MX4）需 glossary |
| Pareto Frontier Candidates | table | — | ❌ | P2 |

---

## 3. Readability 缺口清单（按优先级）

### P0 —— 「用户会看错/看不懂」的硬伤（必修）

| ID | 图 | 缺口 | 风险 |
|---|---|---|---|
| P0-1 | kd 图1 Candidate Trace | 无 caption 讲 proxy_mse 是短训代理 | 用户把「proxy 下降」当「真实精度提升」|
| P0-2 | kd 图2 Latency–Proxy Pareto | 无轴标/方向/caption（含 proxy 语义） | 同上 + 双 min 方向不明 |
| P0-3 | NAS tail_metrics search 收敛图 | 取负显示逻辑不可见 | 用户看到负值/反向轴直接误读 |
| P0-4 | NAS tail_metrics Pareto live | 取负 + 方向 + 轴名全缺 | 同上 |
| P0-5 | NAS push_pareto_final | `scatter` 丢前沿连线、不消费方向 | 终态图比 live 还差，自相矛盾 |
| P0-6 | ptq-sweep lw line | step_idx 语义（累积技术数）无说明 | 用户当成训练步数 |
| P0-7 | ptq-sweep full heatmap | 色阶方向无 caption | dark=好还是坏？mse/accuracy 相反 |

### P1 —— 「能看但不顺」（应修）

| ID | 图 | 缺口 |
|---|---|---|
| P1-1 | struct Champion Trace | caption 提「★=达标」但无 ★ 标记 → 删该句或改文案 |
| P1-2 | qat Convergence line | 有轴标无 caption；loss line 全缺 |
| P1-3 | qat Before vs After bar | 方向陷阱无 caption |
| P1-4 | bit-curve scatter / format bar | 无 caption（与主角 pareto 同脚本却漏） |
| P1-5 | ptq-sweep final-step bar / full scatter | path 字母 glossary / 方向缺 |
| P1-6 | sensitivity bar | score 算法/方向/阈值缺 caption |
| P1-7 | NAS push_describe table | 缺 caption 讲 stem/「—」语义 |
| P1-8 | NAS push_funnel bar | 缺 caption 讲 6 级漏斗语义 |
| P1-9 | NAS Training Loss / Validation Metric（checklist 模板） | LLM 生成脚本不传 label/caption → 改 checklist |
| P1-10 | NAS Population & Cache bar | title 够清，仅缺轴标（可选） |

### P2 —— 锦上添花（可缓）

- 各 workflow 的明细 table 加 caption（struct/kd 的 table 已有，其余 table 无 caption 但 table 自解释性强）。
- 标题中英混杂统一（**受 dedup 冻结，不能改 title 文案**——只能在新建 workflow 时统一；现存图不动）。
- heatmap 反向色阶开关（`value_direction` 字段）——caption 已能解，暂不加字段。

---

## 4. 逐图优化方案（具体到字段值）

> 以下 caption 文案均为建议；coder-agent 可微调但必须覆盖「轴单位 + 方向 + 关键语义」三要素。
> 所有改动**只加 `x_label=`/`y_label=`/`caption=` 三个 kw**，不动其它参数。

### 4.1 struct（`viz_struct.py`）—— 仅 1 处文案修

**Champion Trace**（`_push_champion_trace`，~L175 caption）：当前 caption 末句「目标时延 X ms；★=达标。」中 ★ 无对应渲染。改 caption 去掉 ★ 断言：
```python
caption=(
    f"每轮候选的实测时延（灰点）与 champion 轨迹（彩线，running min）。"
    f" 目标时延 {target_latency_ms} ms（champion 轨迹越靠近/低于此线越达标）。"
)
```
（Pareto / table 不动。）

### 4.2 kd（`viz_kd.py`）—— 给图1/图2 补三字段（对齐图3/4/5）

**图1 `_push_candidate_trace`**（~L216）：
```python
x_label="搜索轮次（round）",
y_label="proxy_mse（短训代理，越低越好）",
caption=(
    "proxy_mse = student vs teacher 的 soft-MSE，是短训精度代理，"
    "非真实精度（真实 dB gap 推迟 finalize）。champion 轨迹=每轮 ratchet 最优。"
),
```
**图2 `_push_pareto`**（~L253）：
```python
x_label="时延 ms（越低越好）",
y_label="proxy_mse（短训代理，越低越好）",
caption=(
    "双 min 帕累托：左下=又快又贴近 teacher。hue=met_latency 标时延达标与否。"
    "proxy_mse 非真实精度，仅短训排序用。"
),
```

### 4.3 NAS（`tail_metrics.py` / `push_funnel.py` / `push_pareto_final.py` / `push_describe.py`）

**tail_metrics train 模式**（`_mode_train`）：
- Training Loss（~L104）加：`x_label="全局训练步（global_step）"`, `y_label="loss（越低越好）"`, `caption="每 log_interval 步采样的训练 loss；hue=phase 区分 train/val。"`
- Validation Metric（~L129）加：`x_label="全局训练步（global_step）"`, `y_label=f"{metric_key}"`（动态字段名比死写 metric 强，但若前端回退也行；建议显式）, `caption=f"验证集指标（字段={metric_key}）；每 eval 周期一个点。"`

**tail_metrics search 模式**（`_mode_search`）—— 最关键，要让取负逻辑可见：
- Search Convergence（~L209）加：
  ```python
  x_label="进化代数（generation）",
  y_label=f"{k}（{'显示 -原值，越大越好' if kind=='quality' else '越小越好'}）",
  caption=(
      f"每代 best/mean。{'质量目标已取负显示（-v），故全轴越大越好；' if kind=='quality' else ''}"
      "best=该代最优，mean=该代均值。"
  ),
  ```
- Population & Cache（~L230）加：`x_label="代数"`, `y_label="个体数"`, `caption="每代 evaluated（实算）/cached（命中缓存免算）/pareto（入当前前沿）三者计数。"`
- Pareto live（~L257）加：
  ```python
  x_label=f"{x_obj}（越小越好{'+，已取负显示' if obj_kind[x_obj]=='quality' else ''}）",
  y_label=f"{y_obj}（越大越好{'+，已取负显示' if obj_kind[y_obj]=='quality' else ''}）",
  caption="当前 per-generation pareto 子集；finalize 全局前沿见 push_pareto_final。坐标值按 cost/quality 符号归一为「全轴越大越好」。",
  ```

**push_funnel.py**（~L51）：
```python
x_label="筛选阶段",
y_label="架构数（对数级递减）",
caption="百万评估 → 最终 N 架构的收敛证据：input→pareto(非支配)→unique(去重)→feasible(满足约束)→feasible_pareto→selected(入选再训)。",
```

**push_describe.py**（~L248）加 caption：
```python
caption="每个 baseline 结构层对应的 elastic 替换。stem=固定不可变；depth∈{...}=深度候选；「—」=非常量无法静态推断（不编造）。",
```

**push_pareto_final.py**（~L148）—— **P0-5：scatter → pareto**（同时加 label/caption）：
> 切 `chart_type="pareto"` 后前端自动画前沿连线 + 消费 `pareto_x_direction/pareto_y_direction`。当前 scatter 的 `hue=status`（dominated/front/selected）语义要保留——**pareto widget 不吃 hue 分组**（它自己分 dominated/front）。折中：主图切 pareto（拿前沿线），selected 高亮改用前端不可得的遗憾接受；**或**保留 scatter 但加 caption 说「前沿=珊瑚色点的包络」。建议：
- **方案 A（推荐）**：切 `chart_type="pareto"`，`pareto_x_direction="min"`, `pareto_y_direction="max"`，data 只留 `{x: xv, y: yv}` 两列（去掉 status/hue），靠前端算前沿。selected 高亮牺牲（终态图里 selected 已在 Selection Funnel 体现）。
- 方案 B（保守）：保留 scatter，仅补 caption。
- 无论 A/B 都加：
```python
x_label=f"{x_obj}（↓better{'+，已取负' if obj_kind[x_obj]=='quality' else ''}）",
y_label=f"{y_obj}（↑better{'+，已取负' if obj_kind[y_obj]=='quality' else ''}）",
caption="全局非支配前沿（sidecar 自算，非 per-gen 标志）。坐标按 cost/quality 归一为「全轴越大越好」。",
```
⚠️ 选方案 A 时 `data` shape 变（去 status），但 `label+title` 不变 → dedup 正常替换旧 scatter 图。需同步删 `hue="status"`。

### 4.4 ptq-sweep（`run_ptq_sweep.py`）

- lw line（~L572）：
  ```python
  x_label="累积技术数（0=baseline RTN，每+1=叠加一项技术）",
  y_label=f"{metric_kind}（{'越低越好' if not higher_is_better else '越高越好'}）",
  caption=f"4 条 ablation 路径(S=Smooth/Q=QuaRot/A=AutoRound/R=纯求解)的累积收益。step_idx 非训练步，是叠加技术计数。",
  ```
  > 注意：`_push_lw_charts` 当前签名没收 `higher_is_better`；需从 `_push_charts` caller 透传（report 里有 `higher_is_better` 字段，main 已算）。**这是本方案唯一需要改函数签名处**，见 §6 checklist batch D。
- lw final bar（~L607）：`x_label="路径(S/Q/A/R)", y_label=f"{metric_kind}", caption="每路径终点（叠加全部技术后）的 metric 横向对比。"`
- full heatmap（~L649）：
  ```python
  x_label="位宽预设",
  y_label="recipe(预处理+求解器+后处理)",
  caption=f"cell 色 = {metric_kind}（{'深色=高=差' if not higher_is_better else '深色=高=好'}）。coral 高亮见 scatter。",
  ```
  > heatmap widget 把 `x_label`/`y_label` 渲染成角标小字（非轴标题），caption 是主说明位。
- full scatter（~L669）：`x_label="位宽预设", y_label=f"{metric_kind}", caption=f"每候选一个点；珊瑚=best（{best_label}）。"`

### 4.5 sensitivity（`run_sensitivity.py`）

- bar（~L150）：
  ```python
  x_label="模型层（原始程序顺序）",
  y_label="敏感度 score（越高=越不能低位宽）",
  caption=f"珊瑚=入选敏感层（保持高精度）。按 ratio={ratio} 选 top-{len(auto_sensitive)} 层留高精度，其余可低位宽。",
  ```
  > 当前 `_push_charts(auto_sensitive, ranked, module_order)` 没收 ratio；ratio 在 main 里。需透传 ratio（签名小改，见 §6 batch D）或从 report 反推。简单做法：caption 不写具体 ratio 数值，改写「按设定 ratio 选出的敏感层（珊瑚）留高精度，其余低位宽」。
- table（~L175）：caption="全部候选层；selected=true 的进入高精度白名单，rank=白名单内序号（越前=越敏感）。"

### 4.6 qat（`run_qat.py`）

- Convergence line（~L330）：已有 x_label/y_label，**补 caption**：
  ```python
  caption=f"每 scheme 的 eval metric 收敛（每 ~{total_steps//16} 步采样）。mse 口径下下行=改善。",
  ```
  > `total_steps` 在 `_push_charts` 签名外；caption 可去 total_steps 依赖，写「每约 16 等分步采样」。
- Training Loss（~L356）：`x_label="QAT 训练步", y_label="teacher-student MSE loss（越低越好）", caption="label-free 蒸馏 loss（student 拟合 teacher 输出），与 eval metric 同向但不等价。"`
- Before vs After bar（~L376）：`x_label="QAT scheme", y_label=f"{metric_kind}", caption=f"每 scheme 训练前后 metric 对比。mse 口径下 after<before=QAT 有效（见 Recovery 图的方向感知版本）。"`
- Recovery bar：不动（已是样板）。
- table：caption="全集含失败 scheme；recovery=after−before（mse 下负=改善）。"

### 4.7 bit-curve（`run_bit_curve.py`）

- pareto：不动（样板）。
- scatter（~L347）：`x_label="avg bit-width（越低越好）", y_label=f"{metric_kind}（方向 {pareto_y_direction}）", caption="全部 evaluated 候选云；珊瑚=前沿/选中（与上图 Pareto Frontier 同前沿，此图加噪声点背景）。"`
- format bar（~L377）：`x_label="量化格式", y_label="该格式的层数", caption="选中候选的混合精度构成（mxint 基）：INT8/MX8=高精度档，INT4/MX4=低位宽档。"`
- table：caption="前沿候选明细；accuracy_loss=相对 FP baseline 的损失。"

---

## 5. 前端 / TUI 适配清单（本方案不改，仅标注）

经核查，**前端 8 widget + TUI `chart_canvas.py` 对 `x_label/y_label/caption` 的渲染均已完备**（P1 已铺到渲染层）。本方案无需前端改动。仅记录两处已知渲染特性供 coder-agent 知情：

1. **RadarChartWidget 不渲染轴标签**（polar 坐标无 XAxis/YAxis label 位）。当前 8 workflow 无雷达图，不受影响。未来若加雷达图，标签需进 caption。
2. **HeatmapChartWidget 把 `x_label/y_label` 渲染为矩阵角标小字**（非轴标题），真正的方向/单位说明应放 `caption`。ptq-sweep heatmap 改动按此约定。
3. **TUI plotext 渲染**：`x_label/y_label` → `plt.xlabel/ylabel`；`caption` → 图下缩进后缀；空数据分支也保留 caption（防静默丢）。无需适配。

**建议（P2，不在本批）**：若后续要加 heatmap 反向色阶（lower-better 时 dark=好），需在 `ChartPayload` 加 `value_direction` 字段 + widget 反转色阶 + `_validate` 放行。当前用 caption 解决，不动契约。

---

## 6. 给 coder-agent 的执行 checklist（分 5 批）

> 每批独立可 commit，建议每批一个 commit（「commit immediately on change」规则）。
> 通用自检：每批改完跑 `python -c "import ast; ast.parse(open('<script>').read())"` 语法检查；有现成 spike/test 的脚本跑一遍。
> **每批必须自我 review**：确认只加了 `x_label/y_label/caption` kw（+ §6-D 的签名透传），没动 `label/title/x/y/hue/color/value/pareto_*`。

### Batch A — kd 两图补齐（P0-1, P0-2）—— 对齐同脚本已有 caption 风格
- [ ] `workflows/agents/_kd_scripts/viz_kd.py` `_push_candidate_trace`：加 x_label/y_label/caption（见 §4.2）
- [ ] 同文件 `_push_pareto`：加 x_label/y_label/caption（见 §4.2）
- [ ] 自检：label=`kd-nas`、title 不变；语法 OK
- [ ] commit：`fix(viz): kd candidate_trace/pareto 补 axis label + caption（proxy_mse 语义对齐 table）`

### Batch B — NAS search 取负逻辑可见化（P0-3, P0-4, P1-8, P1-10）—— 最高优先
- [ ] `workflows/agents/nas-train-runner/scripts/tail_metrics.py` `_mode_search`：3 张图（convergence/population/pareto-live）加 x_label/y_label/caption，caption 显式说明取负（见 §4.3）
- [ ] 同文件 `_mode_train`：Training Loss + Validation Metric 加三字段（见 §4.3）
- [ ] `workflows/agents/nas-select/scripts/push_funnel.py`：加三字段（见 §4.3）
- [ ] **同步 `workflows/agents/nas-viz/scripts/` 下的同名副本**（push_funnel.py 等）——核查是否仍被引用；若 nas-viz 是死代码则跳过并在 commit 说明（见 §7 待办）
- [ ] commit：`fix(viz): NAS search/train 图补 label+caption，取负显示逻辑显式化`

### Batch C — NAS final 帕累托 scatter→pareto（P0-5）+ describe caption（P1-7）
- [ ] `workflows/agents/nas-select/scripts/push_pareto_final.py`：`chart_type` scatter→pareto，删 `hue="status"`，data 改两列 `{x,y}`，加 pareto_x/y_direction + x_label/y_label/caption（见 §4.3 方案 A）
- [ ] `workflows/agents/elastic_optimizer/scripts/push_describe.py`：table 加 caption（见 §4.3）
- [ ] 同步 nas-viz/ 副本（同 Batch B 处理）
- [ ] 自检：label=`nas/search`、title=`Pareto Front (final)` 不变 → dedup 替换旧 scatter
- [ ] commit：`fix(viz): NAS final 帕累托 scatter→pareto（恢复前沿连线）+ describe caption`

### Batch D — ptq-sweep + sensitivity（P0-6, P0-7, P1-5, P1-6）—— 含签名透传
- [ ] `workflows/agents/ptq-sweeper/scripts/run_ptq_sweep.py`：`_push_charts`/`_push_lw_charts`/`_push_full_charts` 透传 `higher_is_better`（report 已有）；4 张图（lw line/bar, full heatmap/scatter）加三字段（见 §4.4）
- [ ] `workflows/agents/sensitivity-analyzer/scripts/run_sensitivity.py`：bar + table 加 caption（见 §4.5，caption 不依赖 ratio 数值）
- [ ] 自检：签名改动向后兼容（新参数默认值不破现有调用）
- [ ] commit：`fix(viz): ptq-sweep/sensitivity 补 label+caption，heatmap 色阶方向 + step_idx 语义显式`

### Batch E — qat + bit-curve 补齐 + struct 文案（P1-1..P1-4）
- [ ] `workflows/agents/qat-trainer/scripts/run_qat.py`：Convergence 补 caption、Training Loss + Before/After + table 加三字段（见 §4.6）
- [ ] `workflows/agents/bit-curve-searcher/scripts/run_bit_curve.py`：scatter + format bar + table 加三字段（见 §4.7）
- [ ] `workflows/agents/_struct_scripts/viz_struct.py` Champion Trace：caption 去 ★ 断言（见 §4.1）
- [ ] commit：`fix(viz): qat/bit-curve 配角图补 label+caption；struct champion_trace 去 ★ 假断言`

### Batch F（可选，P1-9）—— NAS 训练图 checklist 模板
- [ ] `workflows/agents/supernet-train-script/references/workflow-checklists/train_supernet_script_generation/01_training.md`：在 C3a/C3b 的 `render_chart(...)` 契约里补 `x_label`/`y_label`/`caption` kw 示例（见 §4.3 train 模式文案），让 LLM 生成的 train_supernet.py 自带 label。
- [ ] commit：`fix(viz): NAS 训练图 checklist 补 axis label/caption 契约`

---

## 7. 遗留 / 待办（非本方案）

1. **nas-viz/ vs nas-select+elastic_optimizer 重复脚本**：`workflows/agents/nas-viz/scripts/{push_describe,push_funnel,push_pareto_final}.py` 与 nas-select/elastic_optimizer 同名。需单独排查 nas-viz 是否死代码（谁引用）；若是，应删以消歧（DRY）。本方案 Batch B/C 假设 nas-select/elastic_optimizer 为正源——若 nas-viz 仍被某 workflow 引用，改动需同步两边。
2. **标题中英混杂**：受 dedup 冻结不能原地改；仅未来新 workflow 统一。
3. **heatmap 反向色阶**：caption 已解 P0-7，但若用户反馈仍误读，再考虑加 `value_direction` 契约字段（前端+TUI+_validate 三处改）。
4. **dashboard 组合图**：评估为 P2 缓做。若用户反馈「图太多看不到决策故事」，再设计 `chart_type=dashboard`（label 键下多 sub-chart 编排）。

---

## 8. 验收标准

- 8 workflow 每张**坐标图**（line/bar/area/scatter/pareto/heatmap）都有 `x_label` + `y_label` + `caption`（table 至少 caption）。
- 所有量化坐标图的 caption/y_label 显式标注 metric 方向（↓better/↑better），mse 类必须写「负值/下行=改善」。
- NAS search 所有图的 caption 显式说明「质量目标取负显示」。
- `push_pareto_final` 切 pareto 后前端能看到前沿连线。
- `label`/`title` 全仓库无一处被改（dedup 不破）。
- 每批改完语法 + 自检通过即 commit。
