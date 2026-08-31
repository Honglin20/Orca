# Prof-Opt v6 设计草稿（SDD）——单变体收敛 + 异步训练流水线 + 流式早停自动评测

> **状态**：草稿 v0（2026-08-31），待用户评审 → 展开为 `prof-opt-v6-spec.md`。
> **前作**：[`prof-opt-v5-design-draft.md`](prof-opt-v5-design-draft.md) / [`prof-opt-v5-spec.md`](prof-opt-v5-spec.md)。
> **范围**：workflows/prof-opt 的机制级重设计——propose 单变体收敛环、probe 资源分配+启动即放行、watchdog 流式早停自动评测、设备后端抽象、前端 top-10+帕累托推送、变体业务逻辑/信息分析（软对齐）。
> **不是**：最终 SPEC。接口签名 / 数据契约 / 验收标准由 `prof-opt-v6-spec.md` 落实。**Web 端展示（分析文档前端视图）另立 spec：`prof-opt-web-view-design-draft.md`，本草案只保留曲线/帕累托推送契约，不展开文档展示细节。**

---

## 0. 用户诉求与已确认决策

### 0.1 动机（v5 后的体验问题）

1. **中间变体过多**：v5 每轮 ≤3 提案 + 小步严格改进（D-V5-8），探索成本高、dashboard 噪音大；用户要求"不要产生太多中间模型"。
2. **两段训练**：k-depth probe（stop-at-k）+ 独立 full-train → winner 被训两遍（[probe_protocol.md](../../workflows/prof-opt/agents/po_probe/references/probe_protocol.md) + [full_train_protocol.md](../../workflows/prof-opt/agents/po_full_train/references/full_train_protocol.md)）。
3. **串行门禁**：probe 同步监督训练，多卡利用率低；精度判定是"k 点快照"，不是全曲线监督。
4. **前端无管控**：实验一多曲线杂乱，需要 top-N + 帕累托视图。
5. **变体缺一致性分析**：基线有 `business_logic.md` / `information_analysis.md`，变体没有对应核验，结构改动可能跑偏业务语义。

### 0.2 决策表（D-V6 系；未列者继承 v5 D 系）

| # | 决策 | 拍板依据 |
|---|---|---|
| D-V6-1 | **单变体收敛环**：每轮 1 个 vid。propose 初始设计 → implementer 实现 → mfu-analyzer 实测；未达线 → implementer **读最新 mfu 报告**修同一 vid（修复上限默认 5，见 O-1）→ 复测；达线才放行。**禁止为时延迭代派生多个中间变体** | 用户：「只需要针对一个变体，持续优化，直到达成目标即可……不要产生太多的中间模型」 |
| D-V6-2 | **base 恒为基线模型**：变体不叠加、不晋升；`advance_round.py` / base 晋升 / `.round_advanced` / `round_state mode`（latency\|accuracy 双相态）全部退役。每个变体 = 相对原始模型的一次独立尝试；前序变体的 mfu 报告 + 精度规则 + history 作为知识输入 | 用户：「base 一直都按基线模型即可」 |
| D-V6-3 | **probe 资源分配 + 启动即放行**：probe 读盘验证时延（verdict 单一真相源）→ 设备资源检查（**后端抽象，不绑 npu-smi，兼容 CUDA**）→ 有空卡：占卡 + render 全轮次 + detach + liveness（pid / cmdline / log / **epoch1 指标可解析**）→ emit executed（不等训练）；无卡：等待并阻塞 workflow。**并发 = 空闲卡数，不定死上限** | 用户：「实际机器有多卡……有资源就分配卡，没有就等待，阻塞workflow即可……没必要定死并发上限，按卡的量动态分配」 |
| D-V6-4 | **流式早停自动评测**：watchdog（detached）逐 epoch 提取曲线 → 与基线同深度对比 → **前 10% 不判** → 之后**连续 10 个 epoch 超预算** → 杀训练 + `accuracy_fail`；自然跑完 → 最终 eval → `within_budget` → `success` / `accuracy_fail`。**po_full_train 节点退役**，训练并入 probe + watchdog | 用户：「默认就会走和原来一样的训练轮次，但是会和baseline不断比较，如果持续超过gap，例如连续10个epochs都超过了相对于baseline的指标，那就直接停止……一直在gap内，那就直接走了full train」+「早停按前10%不判即可」 |
| D-V6-5 | **watchdog 前端推送**：live 曲线按 **top-10** 展示（选择策略见 O-2，baseline 恒在）；**全部变体进帕累托图**（x = makespan 或相对降幅，y = 最终指标/gap，状态着色）。盘面保留全量曲线，只收窄推送/显示 | 用户：「watchdog我希望也给前端推送……可以只显示top-10的曲线，然后所有的都进帕累托图显示」 |
| D-V6-6 | **变体业务逻辑/信息分析（软对齐）**：propose 对每个变体（含修复后）产出变体业务逻辑分析与信息成分分析（复用 baseline 同款 analyst 的变体模式）。核验口径与 v5 structure-proposer 既有约束**基本一致**：与基线的 `business_logic.md` / `information_analysis.md` **涵盖主要内容对得上、讲得通即可**——不逐条死扣、不要求信息完全保留（时延降低本就可能牺牲部分信息，属正常）；只有**与基线主要语义冲突或文档自相矛盾**才打回修复。产物：`variants/<vid>/business_logic.md` + `variants/<vid>/information_analysis.md` + 简短核验记录 | 用户：「变体提出propose的过程中，也要对变体有业务逻辑和信息的分析……就是变体要和他们能对的上」+「变体逻辑、信息不必与基线完全一致，能够讲通就行……很可能无法完全保留」+「信息对照逻辑，不要写的太死，要和原来基本一致，涵盖主要内容」 |
| D-V6-7 | **MFU 评测与机器无关**：mfu benchmark 是静态/远程评测，不占本机训练设备；propose 复测**无需**设备资源检查。设备资源只分配给训练 | 用户：「mfu benchmark和机器没关系」 |
| D-V6-8 | **历史/dashboard 自动化**：history + `experiment_ledger.json` 动态更新每个变体的改动摘要 / 最新 epoch / 最新 metric / 与基线 gap；watchdog 每周期原子刷新；作为参考与"天然知道哪个模型最好"的依据 | 用户：「历史记录中把每个变体改了什么简短描述，并动态更新最新的变体的epochs和metrics，以及与基线的gap，这部分也自动化起来，用作参考」 |
| D-V6-9 | **rules 增量刷新**：terminal 结果出现时由 propose 入口增量 dispatch accuracy-analyst 提取/更新规则；不单独设提取节点；规则作提案约束与参考 | 用户：「rules 提取时机这个就是的，这样就不用刻意去提取，然后每次都再刷新一遍即可」 |
| D-V6-10 | **轮帽与收尾**：`max_variants` 默认 100（沿用 max_rounds 语义）；达到后**等在飞训练终态**再 report（不主动杀）；report 收割逻辑从单基线扩展为多训练 | 用户确认默认参数无异议 |

### 0.3 v5 被 supersede 的决策清单

- ≤3 提案/轮 + latency 态"严格更优小步推进"（D-V5-8）→ **D-V6-1** 单变体直达 target
- base 晋升 + 血缘叠加 + `advance_round.py`（D-V5-2/5）→ **D-V6-2** base 固定
- 追击期零训练 / 达标后 stop-at-k 粗训 + 恢复轮（D-V5-2）→ **D-V6-3/4** 全轮次异步训练 + 流式早停
- `po_full_train` 独立节点（v5 全保留）→ **D-V6-4** 并入 watchdog 终局判定
- probe GPU 串行守卫（finalizer.pid 四象限）→ **D-V6-3** 设备分配账本（动态多卡）
- `stop_at_epoch.sh` 固定 stop-epoch k → **D-V6-4** 动态早停条件（超 gap 连续 N）
- 精度规则只在粗训点提取（D-V5-7）→ **D-V6-9** terminal 事件即增量刷新

---

## 1. 目标流程（状态机）

### 1.1 总览

```
每一轮 = 一个变体（独立于基线模型，base 永不晋升）

po_propose（含修复内环，快速节点，不占训练设备）
  读基线 mfu 报告 + 前序变体报告 + rules + history
  → 设计 1 个结构（predicted makespan ≤ target_cycles）
  → 一致性核验（business_logic / information_analysis）
  → implementer 实现 → mfu-analyzer 实测
  → 未达线 → 一致性重验 + implementer 读最新报告修同一 vid（≤5 次）
  → 达线 → 落 verdict + 改动摘要 → emit
      ↓
po_probe（阻塞点只在无训练卡时）
  读盘验时延（verdict）→ 设备资源检查（后端抽象）
  → 无空闲卡 → 等待（保持节点，不 emit）
  → 有空卡 → 占卡 → render 全轮次 → detach 训练 + watchdog
  → liveness（pid + cmdline + log + epoch1 指标）→ emit（不等训练）
      ↓
watchdog（detached，每变体一个，接管后续一切）
  逐 epoch：metric_curve extract → 与基线同深度 compare（normalized_loss）
  → 前 10% 不判；连续 10 epoch 超预算 → 杀训练 → accuracy_fail
  → 自然跑完 → 最终 eval → within_budget → success / accuracy_fail
  全程：动态更新 dashboard（epochs / metric / gap / 状态）+ 推送 top-10 & 帕累托
  终态：释放设备锁 + 写 terminal 行 + 标记 rules 待刷新
      ↓
po_gate
  盘面存在 success 变体 → po_report（winner = gap 最优 success）
  变体数 < max_variants → loop（probe 有卡就并行，无卡自然阻塞）
  变体数 ≥ max_variants → 等在飞训练终态 → po_report
      ↓
po_report：终态收割、胜者写回、报告（引用 dashboard 作结论依据）
```

### 1.2 单变体收敛环（po_propose 内环）

1. **提案**：structure-proposer 以基线 mfu 报告为瓶颈证据源，输入含 rules / history / 前序变体 profile 报告 / 上轮 analysis.md 结论；准入 = `predicted makespan ≤ target_cycles`（组合式设计，可跨模块多 op_delta；v5 recovery 组合能力前移到每轮）。
2. **变体业务逻辑/信息分析（软对齐，D-V6-6）**：dispatch business-logic-analyst（变体模式）+ information-analyst（变体模式）产出 `variants/<vid>/business_logic.md` 与 `variants/<vid>/information_analysis.md`——与基线同构、涵盖主要内容，说明改动与基线的关系、被近似/牺牲的信息及其预期精度代价。核验口径 = **与基线主要内容对得上、讲得通**（与 v5 proposer 的业务逻辑约束一致，不逐条一致、不要求信息完全保留）；只有主要语义冲突或文档不自洽 → 打回修复（stamp 键 = vid + change_sig + 修复计数）。
3. **实现 + 实测**：variant-implementer 实现 → mfu-analyzer 实测 → `verdict.json`。
4. **修复内环**：`makespan > target_cycles` → repair_directive = 最新 mfu 报告（实测/预测差、剩余差距、瓶颈根因）→ implementer **修同一 vid** → 一致性重验 → 复测；修复 ≤5 次仍不达线 → 淘汰（`latency_fail` + failed_sigs），下一轮换设计。
5. **达线**：history 记 `latency_pass`（含 makespan）；dashboard 更新改动摘要；emit。

### 1.3 po_probe（资源分配 + 启动即放行）

1. 读 `variants/<VID>/verdict.json`：`makespan ≤ target_cycles` 是放行硬前提（propose 的实测结果是单一真相源，不重测——D-V6-7）。
2. **设备资源检查**（§2）：解析空闲卡集合 = 真实占用（后端 CLI）∪ 锁账本取反；无空闲卡 → 保持节点（status 消息，阻塞 workflow）。
3. 有空卡 → 原子占卡（`devices/<idx>.lock`，含 vid/pid/ts）→ 以 `device=<idx>` 渲染全轮次训练模板（full_train_budget 不变式）→ detach 训练 + watchdog。
4. **liveness 确认（更严一格）**：pid 存活 + /proc cmdline 归属 + train.log 出现 + **metric_curve 能解析出 epoch 1 指标行**（抓 import 卡死 / 数据加载静默失败）。失败 → 重试预算 2 次 → `probe_insufficient`。
5. emit executed（训练与后续判定全部交给 watchdog，本轮即放行）。

### 1.4 watchdog（detached，每变体一个）

生命周期与职责（形态沿用 [run_baseline_chain.sh](../../workflows/prof-opt/agents/po_baseline/scripts/run_baseline_chain.sh) finalizer）：

- **逐 epoch 监督**：每周期 `metric_curve.py extract`（全量重解析）→ `compare --at-epoch <latest>` vs `baseline/baseline_metrics.jsonl`（budget 来自 origin 锚）→ 更新连续超预算计数。
- **warmup**：epoch ≤ `ceil(0.1 × E)`（E = full_train_budget.epochs）不判，不计数。
- **早停**：连续 ≥10 个 epoch `normalized_loss > accuracy_budget` → 杀进程组（复用 stop_at_epoch 的 TERM→grace→KILL + cmdline 归属）→ terminal `accuracy_fail`（记录 stopped_at_epoch）。
- **自然完成**：最终 ckpt eval（复用 baseline finalizer 的 eval 链）→ `verdict_decide final-budget` 判定 → `success` / `accuracy_fail`。
- **训练崩**：无 rc 死亡 → 重派 ≤3（沿用 finalizer 语义）；重试耗尽 → `probe_insufficient`。
- **每周期**：原子更新 `variants/<vid>/train_status.json` + `experiment_ledger.json`（epochs / metric / gap / 状态 / device）+ 推送前端（§4）。
- **终态**：释放设备锁 + 写 history terminal 行 + 标记 rules 待刷新。

### 1.5 po_gate

纯读盘决策（不读节点 output，engine 约束不变）：

```
1. 盘面存在 success 变体（history 任意 vid 有 success 行）  → report（winner = gap 最优 success）
2. 变体数 ≥ max_variants（硬帽，永不 loop）                → report（等在飞终态后）
3. 其余                                                   → loop（无其它出口）
```

`round_state.py` 的 `mode` 命令退役；`current`/`working` 保留（轮 = 变体序号）。

### 1.6 po_report

- 收割在飞：活有界等（沿用 ≤60s）→ 超时按"aborted at terminal"双组 kill 披露，扩展为**多训练收割**。
- 胜者：success 变体中 gap 最优（并列取 makespan 最优）；无 success → no-promotion 披露。
- 写回：新文件名 `<原名>_prof_optimized.<ext>`、冲突不覆盖、复验结构锚——不变。
- 报告引用 dashboard（改动摘要 / 曲线 / 帕累托 / gap 表）+ rules → per-wf KB 同步（成功失败皆同步）——不变。

---

## 2. 设备后端抽象（D-V6-3/7）

### 2.1 训练设备解析（新增 `train_device.json`，flatten 或 probe 首入解析一次）

优先级（first match wins，fail loud）：

1. `ORCA_PO_DEVICE_BACKEND` 环境变量非空 → 显式声明（`npu` | `cuda`），直接采用；
2. `npu-smi` 在场 → `npu`；
3. `nvidia-smi` 在场或 `torch.cuda.is_available()` → `cuda`；
4. 均无 → **fail loud**（可训练设备缺失是硬错误，不静默回落 placeholder）。

`train_device.json`：`{"backend": "npu"|"cuda", "device_count": N, "resolved_by": ...}`。

`profile_mode.json`（profiling 模式 + chip）保留原语义：mfu 评测与机器无关（远程/静态，D-V6-7），chip 型号仍由现有 [resolve_profile_mode.sh](../../workflows/prof-opt/agents/_po_scripts/resolve_profile_mode.sh) 解析；两套解析互不耦合。

### 2.2 设备分配账本

- **占卡**：`devices/<idx>.lock`，`O_CREAT|O_EXCL` 原子创建，内容 `{vid, pid, acquired_at, backend}`；同一 idx 已存在 → 换下一空闲卡。
- **空闲集合**：probe 每次 = 后端真实状态（npu-smi / nvidia-smi 当前存活进程）∪ 锁账本取反；锁文件残留（pid 死亡）→ 归属校验失败即回收。
- **释放**：watchdog 终态删除锁；report 收割兜底清理。
- **绑卡**：训练模板新增 `device` token（渲染为 CUDA_VISIBLE_DEVICES / NPU 设备 index），baseline 与所有变体共用；baseline 启动时经同一 allocator 占卡（默认首空闲卡），finalizer 终态释放。
- 不跨 run 抢卡：只做 run 内账本 + 真实占用双重确认（跨 run 冲突由锁冲突 fail loud 披露）。

---

## 3. 数据契约（新增 / 变更）

### 3.1 新增文件

| 文件 | 内容 | 消费者 |
|---|---|---|
| `train_device.json` | 训练设备后端 + 卡数 + 解析来源 | probe / render / watchdog |
| `devices/<idx>.lock` | 设备分配账本（vid/pid/ts） | probe / watchdog / report |
| `variants/<vid>/watchdog.pid` + `watchdog.log` | watchdog 生命周期（同 finalizer 模式） | probe 重入 / report 收割 |
| `variants/<vid>/train_status.json` | watchdog 状态：stage / epoch / metric / gap / 连续超预算计数 / stopped_at_epoch / device / ts（原子替换） | dashboard / gate / 前端 |
| `variants/<vid>/business_logic.md` | 变体业务逻辑分析（与基线同构、涵盖主要内容；改动说明） | propose 核验 / dashboard / web（独立 spec） |
| `variants/<vid>/information_analysis.md` | 变体信息成分分析（信息核心 / 近似与牺牲项 / 预期精度代价） | propose 核验 / dashboard / web（独立 spec） |
| `variants/<vid>/conformance.md` | 简短核验记录（与基线主要内容对齐结论 + 差异披露，**非逐条一致清单**） | propose 校验 |
| `variants/<vid>/eval/final_acc.json` | 最终 eval 判定输入（vid / final_acc / baseline_full_acc / within_budget 初值 null） | verdict_decide final-budget |

### 3.2 变更文件

| 文件 | 变更 |
|---|---|
| `round_state.py` | `mode` 命令退役；`current`/`working` = 变体轮次 |
| `gate_decide.py` | 决策序改 success / cap / loop；删 accuracy_pass invariant |
| `advance_round.py` | **删除**（base 固定，无晋升） |
| `stop_at_epoch.sh` | 泛化为动态条件早停（或新增 `watch_variant.sh` 内置）：停止条件 = 连续超 gap N，warmup 前不判 |
| `run_latency_recheck.sh` | 判定统一为 `makespan ≤ target_cycles`（双模退役） |
| `push_curves.py` | 扩展：top-10 曲线（选择策略 O-2）+ 全量帕累托（`chart_type="pareto"`/`scatter`）双图推送；盘面全量曲线保留 |
| `dashboard_snapshot.py` / `experiment_ledger.json` | 增字段：change_summary / latest_epoch / latest_metric / gap / status / device |
| `run_full_finetune.template.sh` | 新增 `device` token（render 契约级变更） |
| `check_probe_emit.py` / `check_propose_emit.py` | 对齐新产物（conformance / train_status / verdict 语义） |

### 3.3 history 行语义（新）

- `impl`（proposal 实现，含 change_sig / predicted_delta / 改动摘要）——不变
- `latency_pass`（时延达线，进入训练队列；含 measured makespan）——保留但语义收窄
- `success`（完整训练 + 最终 eval within_budget）——**新**
- `accuracy_fail`（早停或完整训练超预算；含 gap / stopped_at_epoch）——**新语义**（替代 promote_gate=fail）
- `probe_insufficient`（liveness / 训练失败重试耗尽）——保留
- `latency_fail`（时延修复耗尽淘汰）——保留
- 退役：`advanced` / `promote_gate` / `proxy_acc`（k 深度指标）

---

## 4. 前端推送（D-V6-5）

> 注：分析文档（business_logic / information_analysis / mfu 瓶颈报告等）的 web 展示见独立 spec `prof-opt-web-view-design-draft.md`；本节只管曲线与帕累托的推送契约。

- **top-10 曲线**：line 图，baseline 恒在 + 至多 9 个变体曲线；选择策略见 O-2（默认：在飞训练优先 → 近终态/达线者 → 其余按 gap 排序）。
- **帕累托图**：`chart_type="pareto"`（前端原生支持），x = makespan（或相对基线降幅 %），y = 最终指标 / gap；全量变体一个点，状态着色（success 实心 / in-flight 半透明 / fail / eliminated 灰）；达线未训变体用 y=null 占位披露。
- **推送节奏**：watchdog 每周期（复用现有 chart socket + 幂等替换语义），report 终稿推送 `(final)` 后缀不变。
- **管控原则**：盘面保留全部曲线文件（不删数据），只收窄推送与显示；`dashboard.json` 快照同步扩展。

---

## 5. 判定与公平不变量

- **公平不变量保留**：baseline 与所有变体同模板、同 `full_train_budget`（epochs/seed/data 指纹）、同 LR 调度视野；变体只差结构 + 可能被 watchdog 外部早停。
- **早停只影响停止时机**：不改变渲染预算；早停曲线是完整预算渲染下的前缀（fairness 判定语义与 v5 stop-at-k 一致，只是深度动态）。
- **对比深度**：逐 epoch 同深度 vs `baseline/baseline_metrics.jsonl`（baseline finalizer 增量生产，probe 前已终态）；`normalized_loss` 方向归一沿用 [metric_curve.py](../../workflows/prof-opt/agents/_po_scripts/metric_curve.py) / [verdict_decide.py](../../workflows/prof-opt/agents/_po_scripts/verdict_decide.py)。
- **最终判定**：完整跑完 → 最终 ckpt eval vs `baseline_full_acc` → `within_budget`；预算 = origin 锚 `accuracy_budget`（冻结，不重锚）。
- **warmup**：前 `ceil(0.1 × E)` 个 epoch 不判（E 为生效轮数）。

---

## 6. 与现状差异总表

| 单元 | v5 | v6 |
|---|---|---|
| 提案 | ≤3/轮、小步严格改进 | 1/轮、组合式直击 target、同 vid 修复内环（≤5） |
| 一致性 | proposer 约束（prompt 级） | 变体业务逻辑/信息分析三件套，软对齐（主要内容对得上、讲得通；主要冲突才打回） |
| base | 晋升 + 血缘叠加 | 恒为基线，无晋升 |
| probe | 追击期直通 / 达标后 stop-at-k 同步训练 | 每轮验证 + 占卡 + 启动即放行（liveness 含 epoch1） |
| 训练 | 同步监督（单 GPU 串行） | 每变体 watchdog 异步（多卡动态并发） |
| 判定 | k 点快照 + full-train 两段 | 全曲线流式早停 + 终局 eval（一段） |
| po_full_train | 独立节点 | 退役（并入 watchdog 终局） |
| 设备 | npu 探测仅用于 profiling | 训练设备后端抽象（npu/cuda）+ 分配账本 |
| 前端 | 全量曲线推送 | top-10 曲线 + 全量帕累托 |
| dashboard | 静态快照 | watchdog 实时更新（epochs/metric/gap/状态/摘要） |
| 终态 | best + accuracy_pass → full-train | success 变体 → report；轮帽等在飞 |

---

## 7. 风险与开放问题

| # | 问题 | 默认（待裁决） |
|---|---|---|
| O-1 | 单变体修复上限 | **5 次**；耗尽仍未达线 → 淘汰 + failed_sigs，下一轮换设计 |
| O-2 | top-10 曲线选择策略 | 在飞优先 → 近终态/达线 → 按 gap 排序；baseline 恒在 |
| O-3 | 帕累托 x 轴量纲 | makespan cycles 或相对基线降幅 %；建议两者（报告双轴描述），图取降幅 % |
| O-4 | 多 run 并发设备冲突 | 锁冲突 fail loud 披露，不做跨 run 抢占 |
| O-5 | 变体分析/核验频率与口径 | 每次修复后重验；口径 = 与基线主要内容对得上、讲得通（不逐条死扣、不要求信息完全保留）；产物原子写 + 哨兵校验 |
| O-6 | `max_variants` | **100**（沿用） |
| O-7 | 变体淘汰后下一轮起点 | 从基线重新设计（不继承未达线结构）；前序报告/rules 作知识 |
| O-8 | success 出现即终局 or 等所有在飞 | 默认 **第一个 success 即触发 report**（目标已达成）；到帽收尾时取 gap 最优 |

---

## 8. 验收标准（草案，SPEC 展开）

1. **单变体收敛环**：构造距 target 有差距的 mfu 报告 → 同一 vid 经 implementer 读最新报告迭代修复达线（≤5 次），全程不派生新 vid；修复中途软对齐核验拦截一次与基线主要语义冲突（或文档不自洽）的改动，同时放行一次"与基线不同但讲得通"的改动。
2. **异步多卡**：2 张卡 + 2 个变体先后进 probe → 并行训练互不干扰；卡满时 probe 阻塞（状态消息，不 emit）；watchdog 终态释放锁后新变体进。
3. **流式早停**：构造曲线 → 前 10% 不判；连续 10 epoch 超预算被杀、曲线/终态落盘、dashboard 更新；自然跑完 → final-budget 判定 success/fail 正确。
4. **前端**：mock chart socket 验证 top-10 曲线 + 全量帕累托推送（label/title 幂等替换；帕累托状态着色）。
5. **公平不变量**：实跑轮数 == 渲染值（成功路径）；早停变体渲染预算不变式由对称终检/曲线长度断言。
6. **回归**：`tars validate` 0/0；prof-opt 相关单测全绿；E2E（真机清单归用户）。

---

## 9. 实施阶段（草案，SPEC 展开时细化）

- **P0 数据契约与共享脚本**：train_device.json / device lock / round_state 改 / gate_decide 改 / history 新行语义 / ledger 扩展
- **P1 propose 单变体收敛环 + 变体分析（软对齐）**：proposer/implementer/mfu 修复内环 + 变体 business_logic/information_analysis/conformance（analyst 变体模式 + 机械校验）
- **P2 probe 资源分配 + liveness**：设备后端抽象 + 占卡 + device token + epoch1 检查
- **P3 watchdog + 流式早停**：动态早停脚本 + 终局 eval + train_status + 锁释放 + rules 增量刷新
- **P4 前端推送**：push_curves top-10 + 帕累托 + dashboard 快照扩展
- **P5 收尾与回归**：gate/report 多训练收割 + 文档 + 全量测试
