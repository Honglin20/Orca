# Prof-Opt v5 设计草稿（SDD）——真机首跑复盘：时延先行顺序门控

> 状态：评审轮 1 补钉已回填（2026-08-27，开放问题 #1/#3 已裁决）；待用户终审 → 展开为 `prof-opt-v5-spec.md`。
> 前作：[`prof-opt-v4-design-draft.md`](prof-opt-v4-design-draft.md) / [`prof-opt-v4-spec.md`](prof-opt-v4-spec.md)。
> 实现排期：**per-wf 目录迁移 commit 之后**（基于 `workflows/prof-opt/` 新路径，2026-08-27 用户拍板）。

---

## 0. 用户诉求与已确认决策（2026-08-27 会话拍板 + 评审轮 1）

### 0.1 复盘背景：真机首跑暴露的三问题

Run `prof-opt-20260826-145057-3c2dd3`（TDD 接收机 / NPU 服务器 in-session，2026-08-26）：

| 变体 | 改动 | 时延 | 粗训 eval | 结果 |
|---|---|---|---|---|
| r1-01 | 全部 Linear→Conv（16 层） | – | – | 结构不匹配跳过 |
| r1-02 | 注意力 Linear→Conv（8 层） | 降 22% | 未过 | 被 eval 拦 |
| r1-03 | 仅 QKV Linear→Conv（4 层） | 降 18% | 未过 | 被 eval 拦 |

1. **结构推进被精度门槛绑死**：双门槛晋升（时延+粗训 eval 双过才推进 base）→ 无晋升 → base 不推进 → 下轮 profiling 仍是旧基线瓶颈，提案必然原地踏步。**这是空转核心原因**——用户要求的「implement → profiling 未达标 → 围绕新 profiling 继续优化」小循环不存在。
2. **循环退出过软**：`stall_rounds=2` / `exhausted` 早退 + `max_rounds` 默认 5，与「没达轮数且没过目标必须继续」的诉求相悖。
3. **gate 读盘 bug**：propose 节点成功后 gate 找不到 `rounds/001/proposals.json` → rc=2 → 兜底 po_report → finish-failed，1 轮即终（本次直接死因；根因待查，见 §7-1）。
4. **inputs 过多**（14 个），启动面重。

### 0.2 决策表（D-V5 系；未列者继承 v4 D 系）

| # | 决策 | 拍板依据 |
|---|---|---|
| D-V5-1 | **inputs 14 → 8**：`[ask]` = project_root / model_path / latency_reduction_min / accuracy_budget；`[advanced]` = seed / max_rounds（默认 **100**）/ fresh_start / full_train_epoch_cap。退役 6 个：npu 三输入（→环境变量+自动探测，§2.3）、write_back（固定 true，新文件名+冲突不覆盖不变）、report_dir（固定 docs/prof-opt）、probe_epochs（纯自动推定） | 用户：「chip 这些都不用保留，用默认的，尽管砍」 |
| D-V5-2 | **时延先行顺序门控**：推进只看时延改进；粗训精度门只在结构 makespan ≤ 目标线后触发；精度不过 → 恢复轮（makespan ≤ 目标线为硬约束修精度）；时延+精度双达标 → full train。**proposer 精度保持责任**：每条提案带定性精度影响预判，优先低精度代价路径——「尽可能保证精度，不为一味降时延牺牲精度」是提案阶段的判断责任（语义约束，非硬 gate） | 用户：「先把结构时延达标，达标后过第一轮粗粒度训练，达到精度阈值，然后再进 full train」+ 评审轮 1：「让 LLM 尽可能保证精度，不要一味降低时延」 |
| D-V5-3 | **gate 退出条件只剩两个**：双达标 → full-train；round ≥ max_rounds → full-train-best-effort（best 存在）/ finish-failed。`stall_rounds` 早退删除；`exhausted` 降级为报告字段（proposer 轮帽前禁声明穷尽）。**无任何平台兜底**：不加 wall-clock 帽、不加平台硬帽——100 轮是唯一硬上限，平台期的答案是换路径探索（D-V5-8）而非停机 | 用户：「默认循环轮数 100……没达到轮数或没通过目标，必须继续优化」+ 评审轮 1：「不要（兜底），继续执行，这个是硬上限」 |
| D-V5-4 | **双锚恒定**：时延锚 = 原始基线 makespan（首跑冻结 `base/origin_anchor.json`，此后只读）；精度锚（曲线第 k 点 / baseline_k_acc / baseline_full_acc）同样首跑冻结。修 v4「晋升不重锚」（gate 现读 `base/bottleneck_report.json`——推进后被 analyze.py 重写为目标线漂移） | 真机复盘中 r1-02 若推进，v4 目标线会跟着收紧 |
| D-V5-5 | **轮路径单一来源**：R 与 `rounds/<RRR>/` 解析收进共享脚本（新增 `round_state.py`），propose Step0 / probe / gate / advance 全部调用；`<RRR>` 零填充目前是 prompt 约定（agent.md 从未定义），读写两端各自实现 = 漂移温床 | §7-1 根因温床 |
| D-V5-6 | **部署件版本戳**：deploy_scripts.sh 落 `scripts/.VERSION`；节点执行前对戳，不符先重部署再跑 + 披露（防旧工作区复用旧脚本——远端疑似成因之一） | v4 scripts 只在 flatten 部署一次，跨 run 复用不校验 |
| D-V5-7 | **精度经验规则沉淀**：追击期**不加**粗训信号（纯顺序）；精度实测点（达标后首粗训 / 恢复轮 / full train）由 analyst 从「改动类型 → 精度差距」提取**结构化规则**（change_pattern / statement / evidence_rounds / confidence），run 内喂 proposer、终态同步 per-wf knowledge_base 跨 run 复用。**规则只从实测提取，绝不预置写死** | 用户评审轮 1：「不用 epoch 粗训……让 LLM 从精度结果中提取规则供下次参考，注意不要写死。例：降层数时延达标但精度达标不了 → 记规则；有些模型不适合线性 attention」 |
| D-V5-8 | **每轮必须有时延改进**：推进判据 = 当轮最优 makespan **严格 < incumbent**；零改进轮 → 下轮 proposer 上下文硬标记「方向失败」，强制换路径/换结构族。逐步逼近是被期望的形态——不要求一步达标，但每轮必须比上次优 | 用户评审轮 1：「时延一定要比上次有优化，这点非常重要……每次可以只下降一点，慢慢尝试，做不同的结构探索，打开上限」 |

### 0.3 v4 被 supersede 的决策清单

- probe 双门槛晋升（eval 阻断推进）→ D-V5-2 顺序门控
- `stall_rounds=2` / `exhausted` 早退 → D-V5-3
- `max_rounds` 默认 5 → 100
- npu 三输入 / write_back / report_dir / probe_epochs 输入形态 → D-V5-1
- gate 相对**当前** base 报告算目标线 → D-V5-4 origin 锚

---

## 1. 循环状态机（核心机制）

### 1.1 两态，盘面机械可推断

`mode = best.makespan ≤ target ? accuracy : latency`（target = origin 锚 × (1−r)，§4）。不引入引擎级状态——一切节点从盘面读。

### 1.2 latency 态轮（时延追击）

```
propose（围绕当前 base 瓶颈 + 精度规则集 + 精度影响预判责任）
→ implement → 时延复测（run_latency_recheck.sh）
→ 机械推进：本轮 makespan 严格 < incumbent 才推进（血缘链叠加）
→ probe 直通（零训练）
→ gate → loop（未达线）| 首次达线 → 下一轮进入 accuracy 态
```

**零改进轮**（本轮无变体严格优于 incumbent）：不推进，但下轮 proposer 上下文硬标记「该方向已失败」，强制换路径/换结构族——平台期的答案是扩大探索，不是停机（D-V5-8）。

### 1.3 accuracy 态轮（首次进入 = 时延达标那轮；其后 = 恢复轮）

**首次进入**：probe 对推进后的 base 粗训（stop-at-k）+ eval vs 基线第 k 锚 → accuracy verdict 落盘：

- 过 → gate → `full-train`
- 不过 → **规则提取**（D-V5-7：analyst 从血缘改动清单 × 精度差距提取规则条目）→ gate → `loop`（恢复轮）

**恢复轮**：

```
propose（硬约束 makespan ≤ target；目标 = 修精度；规则集 + 精度缺口注入；
        可提名回退型改动 / KD 型改动）
→ implement → 复测过滤（超线变体机械淘汰）
→ probe 对幸存者逐一粗训 + eval → 预算内最优者推进（accuracy 态判据，§2.1）
→ 规则提取（每轮粗训后增量更新规则集）
→ gate → full-train（有过者）| loop | 轮帽
```

### 1.4 成本对照（100 轮帽的可承受性前提）

v4：每轮每变体 stop-at-k 训练 + eval。v5：**追击期零训练**（只 profile——秒级/分钟级），粗训只发生在达标结构与恢复轮幸存者上。

### 1.5 追击期精度语义（评审轮 1 拍板，原开放问题 #1 关闭）

- **纯顺序**：追击期零粗训（「推进者粗训」折中方案被否决）。
- **精度保持是提案责任**：proposer 每条提案带定性精度影响预判（如「替换 8 层 → 预判精度风险高，改 4 层」），优先低精度代价路径。判断责任在提案阶段——语义约束，不设硬 gate，不回到 v4 死锁。
- **规则沉淀（D-V5-7）**：精度实测点（首粗训 / 恢复轮 / full train）由 accuracy-analyst 子代理提取结构化规则条目，例：「降层数 ≥ 2 → 该项目精度崩（evidence: r3, r5）」「线性 attention 替换不适配该接收机（evidence: r4）」。规则存 `accuracy_rules.json`（工作区，run 内每轮喂 proposer）；po_report 终态同步 per-wf knowledge_base（**失败 run 的教训同样同步**——往往更值钱）。规则只从实测提取，绝不预置写死。

---

## 2. 节点与脚本修改面

### 2.1 DAG 不变形（8 节点、回边 `po_gate --loop--> po_propose` 全不动，只改语义）

| 单元 | v5 增量 |
|---|---|
| `po_propose` | Step5 复测后**新增机械推进**（调 advance_round.py）；exhausted 语义改：轮帽前禁 `true`，穷尽感 → 强制扩大搜索面（更深重写/不同算子族）的提示词指令；**零改进轮 → 下轮上下文硬标记方向失败 + 强制换路**（D-V5-8）；proposer 输入增 `accuracy_rules.json` 规则集 + 精度影响预判责任（D-V5-2/7）；恢复轮上下文注入（精度缺口数值 + makespan ≤ target 硬约束 + 可提名回退型改动） |
| `po_probe` | 语义改为**条件精度门**：latency 态直通 emit（survivors_probed=0，零 GPU）；accuracy 态粗训+eval+落盘 accuracy verdict（首次进入训 base；恢复轮训幸存者）+ **每轮粗训后 dispatch accuracy-analyst 增量提取/更新规则**（D-V5-7）。GPU 串行守卫（finalizer.pid 四象限）保留。output_schema 增 mode / accuracy_verdict 字段 |
| `po_gate` | gate_decide.py 决策序重排（§3）；删 stall 入参；base makespan 改读 origin 锚 |
| `po_baseline` | 早期链首跑写 `base/origin_anchor.json`（write-if-absent，§4） |
| `po_report` | 终态新增：accuracy_rules.json → per-wf knowledge_base 同步（成功失败皆同步 + 披露） |
| `advance_round.py` | 判据**双模化**：latency 态 = makespan 最优且严格 < incumbent；accuracy 态 = makespan ≤ target 约束下 accuracy gap 最小。history outcome 值 `promoted` → `advanced`（v4 双门槛语义退役）。顺手修 `_rank_key` 的 tie-break 方向假设 bug（`-proxy_acc` 硬编码 higher_better） |
| `round_state.py`（新增） | R 推导（`.round_advanced` 联动）+ `<RRR>` 零填充路径解析，单一来源；propose Step0 / gate / advance / probe 全改调 |
| `accuracy_rules` 机制（新增） | 结构化规则文件 + 提取子代理（accuracy-analyst）+ 终态 KB 同步；schema：`{change_pattern, statement, evidence_rounds[], confidence, metric_gap}` |
| `deploy_scripts.sh` | 写 `.VERSION` 戳；提供对戳子命令，节点执行前校验 |

### 2.2 inputs 契约 v5（8 个；评审轮 1 确认不加第 9 个）

| input | 档 | 默认 | 说明 |
|---|---|---|---|
| project_root | [ask] | – | 用户项目根（绝对路径） |
| model_path | [ask] | – | 模型定义文件 |
| latency_reduction_min | [ask] | – | 相对**原始基线**的最小时延降幅 (0,1) |
| accuracy_budget | [ask] | – | 相对基线指标的最大可接受损失（按方向归一） |
| seed | [default] | 0 | 复现性种子 |
| max_rounds | [advanced] | **100** | 轮数硬帽（**唯一**循环出口之一；无时间帽/平台帽） |
| fresh_start | [advanced] | false | 工作区重置开关 |
| full_train_epoch_cap | [advanced] | ""（不截断） | 完整训练成本阀（真机 300-epoch 级保护的唯一旋钮） |

退役去向：npu 三输入 → §2.3；write_back → 固定 true（`<原名>_prof_optimized.<ext>` 新文件 + 冲突不覆盖不变）；report_dir → 固定 `docs/prof-opt`；probe_epochs → 契约期机械推定 k，不再可覆盖。

### 2.3 npu 模式自动规则（评审轮挑战点）

- `ORCA_PO_NPU_CHIP` 环境变量优先：非空即 mfu 模式，枚举校验（6613/1951）fail loud。
- 否则探测 `npu-smi` 在场 → mfu 模式，芯片型号从其输出解析；解析失败 fail loud（提示用环境变量显式声明）。
- 均无 → placeholder 估算模式；**报告首段显著披露 profiling 模式**（探测不到 NPU 工具是开发机常态，非错误；用户显式要 mfu 而探测失败 → 环境变量声明即 fail loud，不静默回落）。
- `ORCA_PO_NPU_PRECISION` / `ORCA_PO_NPU_CORES`（默认 INT8 / 1）。

---

## 3. gate 决策序（v5，first-match-wins）

```
1. accuracy_pass（盘面最新粗训 verdict 过）        → full-train
2. round ≥ max_rounds（硬帽，永不 loop）           → full-train-best-effort（best 存在）/ finish-failed
3. 其余                                            → loop（无其它出口）
```

读盘：history（每变体末版）/ best.json / `base/origin_anchor.json` / 本轮 accuracy verdict。**stall 计数保留为报告字段**（连续零改进轮数，供人工观察——不参与决策、不触发任何兜底，D-V5-3）。route 语句不变（engine 约束：when 只引节点 output）。

---

## 4. 锚与公平不变量（继承 v4 + 增量）

- `base/origin_anchor.json`：`{baseline_makespan_cycles, frozen_at_round: 0}`，po_baseline 早期链首跑 write-if-absent，此后全链只读——**推进永不重锚**。
- 目标线 = `origin.makespan × (1 − latency_reduction_min)`，整 cycles +1 边界语义继承 v4。
- 粗训锚 = 基线完整训练曲线第 k 点（finalizer 增量维护）/ baseline_k_acc（可寻址时）；full-train 锚 = baseline_full_acc——全部 origin 冻结。
- 变体与基线同模板渲染、同 seed、stop-at-k 同深度对齐——继承 v4 公平不变量。

---

## 5. E2E 验收要点（v5 增量）

1. **链式推进**：轮 1 降 22%（未达标）→ base 推进 → 轮 2 propose 读到新 base 瓶颈报告（断言报告内容变更 + 血缘 vid 链 + shadow 替换）。
2. **追击期零训练**：latency 态轮无任何 stop-at-k 调用（断言 probe 直通、GPU 零占用记录）。
3. **顺序门控**：首次粗训只出现在达线轮；eval fail → loop 不退出（恢复轮开跑）。
4. **恢复轮可回退**：proposer 可提名回退型 change_sig 并通过机械准入。
5. **轮帽硬出口**：max_rounds=3 fixture 跑满 3 轮 → best-effort / finish-failed；**无任何早退路径**（stall=100 也不停）。
6. **origin 锚恒定**：推进 2 轮后 gate reason 中 target 数值不变。
7. **round_state 单一来源**：现有 gate/advance 用例改造为经 round_state；`<RRR>` prompt 约定清除。
8. **版本戳**：篡改 `scripts/.VERSION` → 下一节点重部署并披露。
9. **规则沉淀**（D-V5-7）：粗训 fail 轮后 `accuracy_rules.json` 出现带 evidence_rounds 的条目，下轮 proposer 输入含规则集；终态后 per-wf KB 有同步文件（失败 run 也同步）。
10. **零改进轮换路**（D-V5-8）：构造连续零改进轮 → 盘面出现方向失败标记，且下轮 proposals 的 change_sig 族不与失败方向重复。

---

## 6. 排期与依赖

- 本草稿 2026-08-27 产出、评审轮 1 补钉同日回填；**实现排 per-wf 目录迁移 commit 之后**（`workflows/prof-opt/` 新路径），避免双 session 文件冲突。
- 实现前置：NPU 服务器上核对 run `…-3c2dd3` 部署 `scripts/` 与仓库 HEAD 差异 + `rounds/001/proposals.json` 实际落点（§7-1 根因确认）。
- `prof-opt-v5-spec.md` 在实现 session 按本草稿展开，走 sdd-loop spec 评审环。
- 衔接点：D-V5-7 规则沉淀的跨 run 载体 = per-wf knowledge_base（**正在由目录迁移任务建立**，天然衔接）。

---

## 7. 开放问题

1. **远端 gate bug 根因**：propose 成功 emit 后 gate 读不到 proposals.json。两个假设——(a) 旧工作区复用了旧版部署脚本（D-V5-6 版本戳防）；(b) 写路径漂移（D-V5-5 单一来源防）。实现前须在服务器实测确认。
2. **恢复轮推进的 makespan 恶化容忍**：恢复轮幸存者 makespan 在 target 内但劣于 incumbent——草稿立场：只要求 ≤ target（accuracy 优先），评审时可收紧为「不劣于 incumbent」。
3. ~~追击期零精度信号风险~~（已裁决 → D-V5-7 规则沉淀 + D-V5-2 提案精度责任，评审轮 1）。
4. ~~平台期烧轮兜底~~（已裁决 → 不加任何兜底，100 轮硬上限 + 零改进轮强制换路，评审轮 1）。

---

## 附 A：审查记录

- **评审轮 1（2026-08-27，用户裁决）**：开放问题 #1 → 纯顺序 + proposer 精度责任 + 规则沉淀（不粗训、规则从实测提取不写死）→ 新增 D-V5-7、D-V5-2 增补；开放问题 #3 → 无兜底、100 轮硬上限、每轮必须严格改进 + 零改进轮强制换路 → 新增 D-V5-8、D-V5-3 增补；inputs 维持 8 个（不加 max_hours）。
- **SPEC 评审轮 1 → 轮 2 前置裁决（2026-08-27，用户，修订 spec-reviewer 9 阻塞项时拍板）**：U1 恢复轮**底座固定 + 组合式提案**（不推进 fail 变体、非棘轮；仅 accuracy_pass 推进即终局）——D-V5-8 链式推进仅指时延追击期；U2 规则**双层池**（项目镜像 docs/prof-opt + 全局池 $ORCA_HOME；model_hash 同配方键控跨文件夹继承、generality 打标跨模型迁移、confirm/refute 机械计数升/隔离、full-train 后补提取）；U3 版本戳 = flatten 入口幂等重部署（含 REUSE 分支）+ 节点对戳 fail loud。
- **SPEC errata 回填（2026-08-27，评审三轮 PASS 后）**：§2.3 比对集收窄为测量配置四字段（排除 resolved_by 溯源字段，防同硬件来源翻转伪漂移强制 wipe）；§6.1 崩溃撕裂恢复读法（marker 缺该轮该模记录 + best.round==current + 无本轮 advanced 行 → 按盘面补齐共同动作，满足 §6.2 重放收敛；良性残余子窗口披露）。均不改用户裁决，出处 planner/plan-adversary 三轮攻击验证。
