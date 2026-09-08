# 洁净审查记录 — workflows/prof-opt/subagents/mfu-analyzer.md

- 审查对象：`D:\Projects\Orca\workflows\prof-opt\subagents\mfu-analyzer.md`（**v3**，243 行，frontmatter `version: 3` / `sentinel: MBA7K2`；per-wf 自包含布局）
- 审查性质：v2→v3 全量重写后的洁净复审（2026-09-08，bound 判定矩阵 + MFU 损耗分解 + 硬件参考单文件化；plan：`docs/plans/2026-09-08-mfu-analyzer-v3.md`，SPEC：`docs/specs/prof-opt-v7-spec.md` §3.4 含 v3 修订记录）
- 审查方法：全文通读 + 受众翻转（假设读者 = 只懂 MFU/NPU 业务、不懂 Orca 内部与 workflow 历史的执行子代理）+ 五组禁词 grep + 事实核对（ascend.md / check_baseline_docs.sh / run_baseline_chain.sh / 派发方 agent.md ×2 / 测试锁定点）
- 参照：洁净契约 `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（§0-§9 通读裁决法 + §5 operational 判据 + §9 references 二分）；共享硬件参考 `workflows/prof-opt/subagents/references/ascend.md`（168 行，本次同批新建）
- 审查日期：2026-09-08

## ① 逐段受众翻转结论表

| 段（行号） | 内容 | 受众翻转结论 |
|---|---|---|
| L1-8 | frontmatter 三键 + 首行哨兵回显指令（v3 MBA7K2） | **PASS** — 哨兵串逐字给出，自包含；frontmatter version 与正文哨兵、锁死点（gate/chain/tests）四方一致 |
| L10-25 | purpose（4 步职责 + 「唯一 profiling 方式」宣言） | **PASS** — 纯 WHAT：「时延瓶颈与 MFU 瓶颈是同一诊断的两个面」是执行判定标准（改变 agent 看什么），非设计论证；「没有本地估算、没有环境嗅探、没有降级路径」是 fail-loud 边界，非退役物叙事 |
| L27-37 | Inputs 七参表（新增 `<hardware_ref>`） | **PASS** — 七参与两个派发方实传逐项对应（po_baseline L113 全七参；po_propose L90 补传 hardware_ref）；`<hardware_ref>` 标注「开工前必须 Read」与阶段 0 呼应 |
| L39-50 | 核心脚本用法 | **PASS** — `$ORCA_ARTIFACTS_DIR/scripts/mfu_benchmark.py` 为契约 §5 operational env 串；单次 bash 调用 = 契约 §4 允许类；「完整参数语义以 `--help` 为准，不要凭记忆传」防漂移 |
| L52-68 | 输出产物清单 + 原始产物留置令 | **PASS** — 「latency gate 直接读取原始 schedule_result.json.parallel_cycles，不存在适配器或二次分析器」是下游消费事实（解释 H6 为何不许搬移产物），operational why 非考古 |
| L70-76 | 阶段 0：读硬件参考 | **PASS** — 「其 §2 的 op 分类表是时间轴三分类的依据」为 ascend.md 文件内导航（该文件被明确指示 Read，契约 §5 判据 = operational，非 spec/plan 节号）；「实测与其判定冲突时信实测，在披露段记录反例」与 ascend.md 头部 fail-loud 前提互相印证 |
| L78-86 | 阶段 1：幂等优先 + 失败路径 | **PASS** — 复用判据具体（schedule_result.json 等在场）；非零退出仍进阶段 2（H5）+ 无日志时如实写失败并标注 `评测失败`——fail loud 双分支显式 |
| L88-141 | 阶段 2：读取优先级 + 三窗口 + MFU 损耗分解 + bound 判定矩阵 + 判断语言 | **PASS** — 三窗口分类与 ascend.md §2 op 分类表逐类对应（cube/vector/搬运重排）；bound 五标签（cube 满载/feed-bound/vector-bound/memory-movement-bound/serialization-bound）与报告模板 L178 枚举同集；「MFU 口径硬规则」（vector 用时间占比、搬运重排 MFU≈0/>100% 不作异常信号）与 ascend.md §2 利用率口径列/1.2/1.10 一致；「不存在 MFU<30% 固定阈值」= 相对同类判断语言，无历史包袱措辞。发现 **F1（L137 mfu-cost 裸术语）**、**F2（bound 标签字面微差）**，见 ② |
| L143-195 | 阶段 3：报告模板（六节 + 首行哨兵） | **PASS** — `### 分析源文件` 列 hardware_ref 实读路径（H7 钉死）；`### MFU 损耗分解` 与 check_baseline_docs.sh L97 节清单同步（WSL 实测 exit 0，见 ③）；披露节显式列「MFU>100% 估算偏差/与硬件参考冲突的实测反例」，承接 L76/L137 的「在披露段告警/记录反例」——模板与硬规则零矛盾 |
| L197-214 | 根因类型词汇表（五类） | **PASS** — 五类（DMA 搬运/格式布局转换税/小算子碎片/子图串行化/算力利用率）与 structural-levers.md L12-14 逐字同集；「本报告不开发结构方案、不给配置建议……硬件 reference 的 §3 是事前先验」三刀切职责（诊断/先验/药方），与 ascend.md 双受众头注、structural-levers「Structural priors live HERE and only here」口径闭合 |
| L216-227 | 硬规则 H1/H2/H5/H6/H7 | **PASS** — 五条均可机械遵守；无 H3/H4 引用（编号断档为 v2 重写删除项的稳定编号，SPEC §3.4 保留行引用同组 H1/H2/H5/H6，重编号反而破坏跨文档引用——判定合法，见 ② 非计数 N2） |
| L229-243 | Output（报告落盘 + ≤10 行紧凑摘要）+ Constraints | **PASS** — 哨兵回显、报告路径、只写 report_path 一个文件；与 Constraints「profile_dir 只读」（H6）双重钉死；「报告不开结构药方——那是调用方与 proposer 的职责」收口职责边界 |

## ② Findings

### F1（轻微 · 可读性/可定位）

- **位置**：`workflows/prof-opt/subagents/mfu-analyzer.md:137`
- **问题**：「或 >100%（mfu-cost 对小 shape 的已知估算偏差）」——`mfu-cost` 是评测工具内部的成本模型名，全文无解释，运行时子代理无实底（ascend.md 1.2 陈述同一事实时未点名：「常爆 >100% 估算偏差」）。操作规则本身自洽（「两者都不作为异常信号，只按成本计价」），不构成洁净违规（非开发考古、非引擎路径）。
- **建议修法**：改为「（评测工具的 mfu-cost 估算对小 shape 有已知偏差）」或径直删括注、只留「（定义使然、无信息量）」一层理由。
- **处置**：🟢 非阻塞；待创作方顺手修。

### F2（轻微 · 字面一致性）

- **位置**：`workflows/prof-opt/subagents/mfu-analyzer.md:124`（`cube 满载`，空格形）vs `:178`（`cube满载`，无空格）；`workflows/prof-opt/subagents/references/ascend.md:24`（`vector bound`）、`:46`（`memory-movement bound`，空格形）vs `mfu-analyzer.md:126-128`（`vector-bound`/`memory-movement-bound`，连字形）。
- **问题**：bound 标签是根因段的规范枚举（L178 为下游引用形），同一词汇集三处两种写法。无机械门解析这些串（check_baseline_docs.sh 只查节标题），纯字面锐化。
- **建议修法**：以 L178 模板枚举为规范形，矩阵行（L124-128）与 ascend.md §1 诊断栏统一为连字形。
- **处置**：🟢 非阻塞；待创作方顺手修。

### 非计数观察（不构成 finding，备案）

- **N1（L92）**：`6613_*.csv`/`1951_*.csv` 芯片前缀是 `<chip>` input 枚举（L36）对应的产物命名契约（领域事实），非 §6 测试夹具硬编码。
- **N2（L216-227）**：H 编号断档（无 H3/H4）是 v2 删除项（MFU<30% 阈值/配置类建议）留下的稳定编号；全文零 H3/H4 引用，无 dangling；SPEC §3.4 保留行按同组编号引用——重编号会造成 spec 引用漂移，维持现状正确。
- **N3（L74/L102/L201）**：`§2`/`§3` 均指 ascend.md 文件内节（agent 被明确指示 Read 该文件），契约 §5 判据 = operational；validator 的 spec/plan 节号 pattern（`N.M` 形）不命中，即使命中也属误报。

## ③ 哨兵一致性 + 门实测（本次复审机械证据）

- 全仓 v2 哨兵扫描：唯一命中 `docs/plans/2026-09-08-mfu-analyzer-v3.md:25`（D3 修订记录，开发文档，允许）；`docs/releases/2026-08-26-prof-opt-v4-refactor.md:36` 的 v1 提及为历史 release 记录，允许。运行时锁定点全部 v3：本文 L7/L146/L149/L233、`run_baseline_chain.sh:354`、`check_baseline_docs.sh:26`、`tests/test_po_scripts.py:756/1437/1456`（+ :1646 stale-replace 改 v3→v1）、`tests/test_po_v6.py:62`。
- WSL 实测 `check_baseline_docs.sh` 三用例（合成报告，ORCA_ARTIFACTS_DIR 绝对路径）：
  1. v3 哨兵 + 六节全 → **exit 0 PASS**；
  2. stale v2 哨兵 → **exit 1**，报文同时给出期望（v3）与实得（v2）——旧工作区遗留报告被正确拒绝；
  3. v3 哨兵缺 `### MFU 损耗分解` → **exit 1** 并点名该节——节清单同步生效。
- `bash -n`：`run_baseline_chain.sh` / `check_baseline_docs.sh` 语法均通过。

## ④ 契约一致性核对（SPEC §3.4 v3 修订行 + 派发方 + ascend.md）

| 核对项 | 基准 | 结论 |
|---|---|---|
| frontmatter `version: 3` + 哨兵 `[subagent:mfu-analyzer v3 MBA7K2]`（哨兵码不变） | SPEC §3.4 L100-101 | **一致** |
| Inputs 增 `<hardware_ref>`（必读），两个派发方同步传入 | SPEC §3.4 v3 修订；po_baseline L113 / po_propose L90 | **一致**（两处路径写法逐字同为 `{{ subagents_root }}/references/ascend.md`） |
| 三窗口分类依据 = ascend.md §2 op 分类表 | ascend.md L120-131（cube/vector/搬运重排 + 利用率口径列） | **一致**（三类逐项对应；「不在表内按语义归入最近类别并说明理由」的兜底条款在 ascend.md 侧） |
| bound 判定五标签 | SPEC §3.4 v3 修订 + 报告模板 L178 | **一致**（矩阵 L122-128 与模板枚举同集；字面微差见 F2） |
| 报告节增「MFU 损耗分解」；check_baseline_docs.sh 节清单增同名节 | SPEC §3.4 v3 修订末条；check_baseline_docs.sh L94-97 | **一致**（门五节 = 模板六节中的五节必需，`### 分析源文件` 不入门属门侧从简，非矛盾；实测见 ③） |
| MFU 口径硬规则（vector 占比 / 搬运重排不作异常信号） | SPEC §3.4 v3 修订；ascend.md §2 口径列 | **一致** |
| 根因词汇表五类 | structural-levers.md L12-14 | **一致**（逐字同集，4→5 同步完成） |
| 失败也分析（H5）+ 无日志时报告标注 `评测失败` | SPEC §3.4 保留行；po_baseline L237（gate 对失败报告仍验哨兵+节） | **一致**（`评测失败` 标注写进「模型概况」段 → 门节非空判定仍可过） |
| 不开结构药方、不给配置建议 | SPEC §3.4 删除行；structural-levers 单一来源声明 | **一致**（L201/L243 两处收口） |
| 硬件参考单文件、无第二副本 | `subagents/references/ascend.md` 唯一活跃副本；`agents/po_propose/references/hardware/ascend.md` 已删（find 证实目录仅剩 structural-levers.md） | **一致** |

## 结论

v3 全量重写后受众翻转通读通过：诊断框架（三窗口 + MFU 损耗分解 + bound 矩阵）自洽可执行、报告模板与硬规则/校验门/硬件参考三方对齐、哨兵与测试锁定点全量同步、五组禁词零命中。两条轻微 finding（F1 mfu-cost 裸术语、F2 bound 标签字面微差）均为非阻塞润色，待创作方顺手修；无洁净契约违规。

VERDICT: CLEAN（附 2 条 🟢 非阻塞润色项，2026-09-08 v3 复审）
