# 洁净审查记录 — po_report/references/report_format.md

- **审查对象**：`D:\Projects\Orca\workflows\agents\po_report\references\report_format.md`（261 行，全文通读）
- **审查方法**：受众翻转通读（审查者视角 = po_report 节点 agent，先读 `po_report/agent.md` 再按其 Resource Anchors 指引读本格式文档并逐段执行）+ 词表 grep + spec §4 po_report 行逐条核对
- **判据**：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（agent.md L23-25 指示 Step 1 必读并 "follow it exactly" → prompt-adjacent prose，契约适用）+ `docs/specs/prof-opt-v4-spec.md` §4 L103 / §2 L84 / §1 L45
- **交叉核对源**：`workflows/agents/po_report/agent.md`、`workflows/agents/po_probe/references/probe_protocol.md`（variant pid 路径）、`workflows/agents/po_propose/scripts/run_latency_recheck.sh`（verdicts.jsonl / base_makespan_cycles）、`workflows/agents/po_flatten/agent.md`（shadow_synthesized）、`verify/cleanliness/po_report__agent.md`
- **日期**：2026-08-25；只读审查，问题只记录不修复

## ① 逐段结论表（受众翻转）

| 行号 | 段落 | 结论 | 说明 |
|---|---|---|---|
| L1-17 | 标题 + builder 契约 | 通过（1 LOW，F3） | 单一 `report_builder.py`、幂等（same-content 规则）、stdout 单行 JSON = 最终回复——全部可机械执行。占位符声明（L7）中 `<accuracy-budget>` 全文无消费点 → F3 |
| L19-31 | §0 终态收割 | 通过 | finalizer.pid 死直过（终态 = train_final.json 所言）/ 活等 ≤60s / 双组 kill（`baseline/train.pid` + finalizer 组）+ `variants/*/train/train.pid` 扫杀（glob 与 probe_protocol.md L105/L116 的真实落盘路径一致）/ 每 kill 先 /proc cmdline 归属校验（`train.rendered.sh` / `--finalizer`），误用 pid 跳过并披露 / `"aborted at terminal"` 只进 reason、不改 status/stage——与 SPEC §4 逐点一致 |
| L33-57 | §1 终态表 + 行注 | 通过 | row1-9 首匹配语义、每行条件键于盘面文件，可机械执行；row6b / row8 tiebreaker / row9 行注均为产品说明书式（对用户报告的披露措辞指引），非设计论证 |
| L59-80 | row7 内归因 | 通过（1 LOW，F2） | DONE marker / verdict.json / history 终态三态判据（L64-67）✓；全部 proposal-loop 死亡归 stage `propose`、幸存者缺终态精度 → `probe` ✓。L60-61 尾句版本考古 → F2 |
| L82-133 | §2 字段装配 | 通过 | 14 个 schema 字段（agent.md L137-141 / yaml required）逐个有装配规则；`baseline.proxy_acc` = 全量曲线@k（k = `proxy_budget.epochs`，读 `baseline/baseline_metrics.jsonl`；不足/缺文件/缺 epoch-k 行 → null + reason 披露）✓；`ref_acc` 三态直读 `baseline_full_acc.json`（train_final 缺失 → null+披露含 aborted-at-terminal / failed → 引其 stage 归因 / done → 读盘 "never from anywhere else"）✓；makespan 三级优先源中 (b) `verdicts.jsonl` 的 `base_makespan_cycles` 经 run_latency_recheck.sh 证实为真实产物；no-promotion 零写回措辞（L123-127）✓ |
| L135-181 | §3 写回 | 通过 | v3.5 全套继承且自包含：lock 复验（结构锚 vs 逐文件两级）→ final shadow diff → `shadow_synthesized` 跳过（字段经 po_flatten/agent.md L227/L356 证实真实，非 best-effort 编造）→ deletions 冲突 → 写后字节校验 → `done` 语义（空 diff 亦完成、锚败即 false）。用户原件永不改写、目标异内容不覆盖 |
| L183-223 | §4 charts | 通过（1 中，F1） | 两确定性共享脚本（experiment_ledger / dashboard_snapshot）失败非 best-effort = builder 失败；两图钉死（无 rounds → 双跳 + 固定串 `none (no rounds recorded)`；≥1 round → 恒双图）；live push 全 keyword-only + try/except + 仅动 `charts_summary`。`push_curves.py --title "(final)"` 终稿推送缺位于本文件 → F1 |
| L225-252 | §5 prof_opt_report.md | 通过 | Fairness Note 轮数 = `full_train_budget.epochs` 读自 `contracts.json`（生效值、非 raw argparse count；stale anchor 双值披露）✓；Stop-Status Disclosure（killed/natural_done 计数 + `monitor_failed: true` 计数）= v4 stop-at-k 语义；no-promotion 显式行 "no promoted variant — nothing to write back" ✓；Enablement Note 产品语气 |
| L254-261 | §6 Emission | 通过 | 单行 JSON 全字段校验 + fix-loop ≤3 + minimal-valid-JSON fail loud（`stage=report`，在 8 值 enum 内）；"never an unparseable reply" |

## 残留 grep（命中 = 0）

- **任务词表 21 词**（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / `D:\Projects` / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft，`-i`）：**0 命中**。
- **追加口径**（implement / verify / epoch-only / 懒 / 补训 / pretrained_ref 出现处逐一人工核）：`pretrained_ref_acc` + `baseline/pretrained_ref.json` 为合法输出字段（yaml/agent.md 14 字段清单在内），非 `baseline_ref` 残留；implement/verify 仅现于 L60-61 考古句（→ F2）与 "latency recheck" 当前机制描述，无 stage 值残留。**v3.5 已删机制（ref-input / auto-trained / 懒补训 / epoch-only proxy）零残留**。

## ③ 契约一致性核对（spec §4 L103 po_report 行）

| spec 条目 | 文件实现 | 一致 |
|---|---|---|
| 终态收割三分支（死直过 / 活 bounded 等 ≤60s / 到点无终态） | L21-29 | ✓ |
| 双组 kill（训练组读 baseline pid 文件 + finalizer 组）+ variants 扫杀 | L23-25（`baseline/train.pid` + finalizer + `variants/*/train/train.pid`） | ✓ |
| 披露 `"aborted at terminal"` | L29（只进 reason，不改 status/stage） | ✓ |
| row3 读 baseline_full_acc 三态判定 | L39（failed → 归因其 stage / missing → 非失败、披露于 ref_acc）+ L95-100（done → 读盘） | ✓ |
| ref_acc 删 baseline_ref.json 直读 baseline_full_acc | L95-100（唯一来源 `baseline_full_acc.json`，"never from anywhere else"；全文无 baseline_ref.json / 优先级链） | ✓ |
| Fairness Note 轮数读 full_train_budget.epochs | L236-238（生效值；stale anchor 双值披露） | ✓ |
| 内归因 implement/verify 并入 propose（DONE/verdict/history 三态） | L59-74（三态判据字面落地；loop 死亡全归 propose） | ✓ |
| baseline.proxy_acc = 曲线@k，不足 → null+披露 | L91-94 | ✓ |
| finalize chart：push_curves.py `(final)` 终稿兜底 | **本文件缺**（仅 agent.md Step 3 L115-121 有）→ **F1**；每轮 makespan 趋势 ✓（L193-196 静态图 + L204-220 live push） | ✗→F1 |
| 写回 ≥1 promoted 全套 / no-promotion 零写回+披露 | §3 全套；L123-127 + L247-249 显式行 | ✓ |
| stage enum 8 值 | 全文出现 flatten/contract/baseline/propose/probe/gate/full-train/report 恰 8 值，无 implement/verify 等额外值 | ✓ |

## ② Findings

- **F1（中，SPEC §4 finalize-chart 落位缺口 / 跨文件机械规格不完整）** `report_format.md:183-188`（§4 前言）— SPEC §4 po_report 行钉 "finalize chart：`push_curves.py` title `(final)`（终稿推送兜底）+ 每轮 makespan 趋势"。makespan 趋势本文件已落（L193-196 + live push），但 `push_curves.py --title "(final)"` 终稿推送只存在于 `po_report/agent.md` Step 3（L115-121，标注 "inside the builder"），本文件 §4 前言只枚举两个确定性共享脚本。本文件 L11-17 自称 builder "implements this document **mechanically**"、agent.md 又要求 Step 1 写 builder 时 "follow it exactly"——纯按本文档机械实现的 builder 会漏掉终稿曲线推送，而 E2E §7 ⑨ 的 "report finalize 终稿推送兜底 + 幂等重入 finalize 复验" 依赖它。节点级 SPEC 由 agent.md 满足（po_report__agent.md 记录 §四 已判 ✓），故非节点违约，是本文件机械规格与 agent.md Step 3 的不对齐。**建议**：§4 前言补一行（"invoke `push_curves.py --artifacts <workspace> --title "(final)"`（best-effort，`|| true`；成功追加 `.chart_push.log` 审计行）"），与 agent.md Step 3 对齐。
- **F2（轻，版本考古）** `report_format.md:60-61` — "(the proposal loop — propose, implement, latency recheck — closed inside ONE node, **so the old implement/verify split no longer exists**)"：后半句是 v3.5 旧结构的版本考古（契约 §4 禁「前身/前作/the old X no longer exists」类迁移措辞），运行时 agent 只需当前归因规则，"旧分裂已不存在" 对其零可执行价值。**建议**：删 "so the old implement/verify split no longer exists"（保留 "closed inside ONE node" 即可，或整句删去——归因 bullets 自足）。
- **F3（轻，死占位符）** `report_format.md:7` — 占位符声明含 `<accuracy-budget>`，但全文无消费点：§2 `final.gap` 的锚 = `final/final_acc.json` 内记录的 `baseline_full_acc`、`within_budget` 亦读该文件（L114-118），§3/§5 均不使用该输入。属 v3.5（report 自算 budget 判定）遗留的输入锚声明，v4 判定已下沉 po_full_train 后过期。**建议**：从 L7 占位符清单删除；若产品上确需在 markdown 报告披露用户预算输入，则在 §5 Baseline vs Final 明写其消费点。
- 其余检查显式**零 finding**：词表 0 命中；v3.5 已删机制（ref-input / auto-trained / 懒补训 / epoch-only proxy / baseline_ref 优先级链 / run_verify / playbook / mfu / perturb_ckpt）零残留；SPEC §4 行 11 项核对除 F1 外全 ✓；无 plan/issue/SPEC/ADR 编号、无迁移出处词、无 Orca 引擎源码路径（`orca.chart` import 属契约 §5 operational 白名单）、无测试项目名/fixture 数值硬编码、无事故复盘叙事。

**零严重度观察（备案不计 finding）**：a) train_final=done 而 `baseline_full_acc.json` 缺失的边缘 L99-100 未显式分支 → builder 崩溃 → §6 fail-loud minimal JSON 兜底，符合 fail loud 原则，可接受；b) render_chart 示例实参（L210-215）未对 `orca/chart` API 逐参核验（不在本审查范围；best-effort try/except 包裹，失败仅 stderr + `charts_summary` 后缀）；c) §2 `pretrained_ref_acc`（读 `baseline/pretrained_ref.json`）与 §5 pretrained reference 行（键于 `readiness.json` 记录）为值/路径互补对，不矛盾；d) 示例数据 `{"round": 1, "makespan": 15288}` 为无项目身份的裸示例数，非 fixture 硬编码。

---

VERDICT: ISSUES (3)（词表残留 0、v3.5 机制删净、SPEC §4 行除 finalize-chart 落位外全一致；F1 中——push_curves `(final)` 需镜像进 §4 机械规格，F2/F3 轻考古/死占位符，修后复审）

## ④ 复验（fix commit `24eb711`，2026-08-26）

**方法**：`git diff 2de195e..24eb711 -- workflows/agents/po_report/references/report_format.md` 逐 hunk 核对（4 hunks，全部对应 findings + 交叉备案项）；`git log 24eb711..HEAD -- <file>` 为空 + `git diff 24eb711 -- <file>` 为空 → 工作树即 24eb711 版本，无后续漂移；修复处 grep/交叉源复核。

| 项 | 修复 hunk | 复验结论 |
|---|---|---|
| F1（中，push_curves `(final)` 缺位于 §4） | §4 前言新增（现 L188-191）："Also finalize the live training-curve chart (best-effort, never blocks the report): `push_curves.py --artifacts <workspace> --title "(final)"` — the terminal push ... a successful push appends the `.chart_push.log` audit line" | **已修** ✓ — 与 agent.md Step 3（L115-121）语义逐点对齐（best-effort / `(final)` title / `.chart_push.log` 审计行），SPEC §4 "finalize chart：push_curves.py title `(final)`（终稿推送兜底）" 本文件侧落地；措辞洁净无考古 |
| F2（轻，版本考古尾句） | L60-61 删 "so the old implement/verify split no longer exists"，保留 "(...closed inside ONE node):" | **已修** ✓ — 考古措辞清零，保留部分为当前机制 operational 描述；内归因三态 bullets 未动 |
| F3（轻，死占位符 `<accuracy-budget>`） | L7 占位符清单收为 `(`<project-root>`, `<write-back>`)` | **已修** ✓ — 两个保留占位符均有消费点（§3 标题门 `<write-back>`、L165 落位 `<project-root>`）；无新悬空引用 |
| 交叉备案 L230（Stop-Status 路径缺 `/train`） | L234 `variants/<vid>/stop_status.json` → `variants/<vid>/train/stop_status.json` | **已修** ✓ — 与 probe 侧真实落盘路径一致（`po_probe/references/probe_protocol.md:61` 同路径）；与本文件 §0 的 `variants/*/train/train.pid` glob 同构 |

**回归检查**：4 hunks 均为纯增删被 flag 文本，未触及其余段落；修复引入文本无词表命中、无新考古/新悬空引用；stage enum 8 值、14 字段装配、终态表/收割/写回语义不受影响。未解决项：**无**。

---

VERDICT: PASS（F1/F2/F3 + 交叉备案 L230 四项全闭环于 `24eb711`；词表残留 0、v3.5 机制删净、SPEC §4 po_report 行 11 项逐条一致；工作树与 fix commit 零漂移）
