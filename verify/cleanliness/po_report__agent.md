# 洁净审查记录 — workflows/agents/po_report/agent.md

- 审查对象：`D:\Projects\Orca\workflows\agents\po_report\agent.md`（170 行，v4 终态）
- 依据：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众翻转通读，§8）+ `docs/specs/prof-opt-v4-spec.md` §4 po_report 行 + `docs/specs/prof-opt-v4-design-draft.md` §4
- 交叉核对源：`workflows/agents/po_report/references/report_format.md`、`workflows/prof-opt.yaml`（inputs + output_schema）、`workflows/agents/_po_scripts/deploy_scripts.sh` / `dashboard_snapshot.py` / `push_curves.py`、`workflows/agents/po_baseline/scripts/run_baseline_chain.sh`
- 审查方式：只读；问题只记录不修复

## 一、逐段受众翻转结论表

假设读者 = 只懂本 workflow 业务、不懂 Orca 内部与开发史的运行时 LLM 执行者。

| 段（行号） | 结论 | 说明 |
|---|---|---|
| frontmatter + description（1-4） | 通过 | 产品说明书式一句话；tools 列表含 `task` 但 Subagent Call Protocol 正确声明零 dispatch——`task` 在全部 7 个 po_* agent 一致携带，属 workflow 级约定，非本文件残留 |
| Your only task（7-15） | 通过 | 指令式、自包含；"All paths converge on you" 是终态角色定位，无历史负担 |
| Resource Anchors（17-39） | **2 处 finding** | F1：`full_train_epoch_cap` 锚对 Fairness Note 轮数来源的表述与 SPEC/references 矛盾（见 findings）；F4：`report_dir` 落档"suffix the copy with the run id"未给 run id 来源。其余锚（artifacts/resources/`cd` 先行）均可独立执行；所有 `{{ inputs.* }}` 均在 yaml inputs 中存在且默认值一致（`report_dir` 默认 `docs/prof-opt`、空串=不落档 ✓） |
| Zero cross-node hard rule（41-45） | 通过 | 运行时约束（渲染崩溃防护），非事故复盘叙事 |
| Path Handling Rules（47-50） | 通过 | 可机械执行 |
| Subagent Call Protocol（52-54） | 通过 | 与 SPEC §3"probe/full_train/report 不派"一致 |
| Lazy Loading（56-61） | **1 处 finding** | F2："read it when Step 1 begins" 与 Step 0（先于一切）已需该文档的 harvest table 冲突——v4 插入 Step 0 留下的时序缝 |
| Step 0 Terminal harvest（65-80） | 通过 | 与 SPEC §4 行逐点一致：finalizer.pid 死直过 / 活 bounded 等 ≤60s（短 bash 调用内，turn 顶满 → status message + `do not call orca next` 再入——与 po_probe/po_full_train 既有约定一致，属 operational）/ 到点无终态 → 双组 kill（训练组读 `baseline/train.pid` + finalizer 组）+ 扫 `variants/*/train/train.pid` + 披露 `"aborted at terminal"`；/proc cmdline 归属守卫（误用 pid 跳过并披露、不signal）与 report_format.md §0 逐字一致 |
| Step 1 builder（82-94） | **1 处 finding** | F3："run it in derive-only mode first" 中 `derive-only mode` 全仓（workflows + docs）无定义——report_format.md 无此模式，执行者需自行猜义。其余：只读工作区 + 用户树只读例外、幂等、stdout 单行 JSON/stderr 全部与 report_format 一致 |
| Step 2 Write-back（96-101） | 通过 | 门 = `status == success` AND `write_back` 输入——与 report_format §3 相同；success ⇔ row6（final_acc within_budget）⇔ 必有 best.json（≥1 promoted），no-promotion 终态自然零写回 + 披露（report_format §2/§5），与 SPEC"≥1 promoted 全套 / no-promotion 零写回+披露"一致；五步链条（lock 复验→shadow diff→`<stem>_prof_optimized<suffix>`→冲突列示→字节校验）与 report_format §3 步序一致 |
| Step 3 Charts + ledger（103-129） | 通过 | `experiment_ledger.py` / `dashboard_snapshot.py` / `push_curves.py` 路径 `$ORCA_ARTIFACTS_DIR/scripts/` 真实（deploy_scripts.sh 把 `_po_scripts/*.py` 全量部署到 artifacts/scripts/；po_baseline 亦按同路径校验/调用）；finalize chart `--title "(final)"` + `\|\| true`（best-effort）+ `.chart_push.log` 审计行 = SPEC"push_curves (final) 终稿兜底"；`rounds_makespan_trend`（兼 best-effort live 趋势）+ `verdict_distribution` = SPEC"每轮 makespan 趋势"；`dashboard.json` + `dashboard.html` 是 dashboard_snapshot.py 真实输出对 |
| Step 4 Validate and relay（131-148） | 通过 | 14 字段清单与 yaml output_schema `required`（行 393）逐字段一致；fix-loop ≤ 3 + minimal-valid-JSON fail loud 与 report_format §6 一致 |
| Validation（150-154） | 通过 | 无盘面修数循环，"terminal state is what the workspace shows, never massaged"产品语气 |
| Supervision points（156-165） | 通过 | 全部为运行时守则；无开发期叙事 |
| Output（167-170） | 通过 | 与 Step 4 闭环 |

## 二、词表 grep（任务词表 + SPEC §6 增补）

对 `workflows/agents/po_report/agent.md` 跑 `mnist_kd|playground|prof_opt_demo|run_verify|baseline_proxy_acc|baseline_ref|mfu_adapter|perturb_ckpt|playbook|ref-input|auto-trained|docs/specs|D:\Projects|/mnt/d|spec-review|SPEC-R1|ns3|psu|kd-nas|nas-supernet|prof-opt-design-draft`（-i）：**零命中**。

追加口径（epoch-only / lazy / finetune / retrain / proxy / implement / verify 等 v4 退役措辞）：仅命中合法运行时串（`Lazy Loading` 标题、`pretrained_ref_acc` 输出字段名 = yaml:421 合法字段、`experiment_ledger.py` 等），**无退役机制残留**。`pretrained_ref_acc` ≠ `baseline_ref` 残留（前者是 v3.5 起即有的 output 字段，report_format §2 有其三态语义）。

## 三、findings 清单

1. **F1（中，SPEC/契约一致性）** `workflows/agents/po_report/agent.md:30-32` — `full_train_epoch_cap` 锚写 "the Fairness Note derives the EFFECTIVE full-budget epoch count **from it**"，把 Fairness Note 轮数来源指向该输入；但 SPEC §4 po_report 行钉 "Fairness Note 轮数读 `full_train_budget.epochs`"，report_format.md §5（:236-238）钉 "epoch count cited here is `full_train_budget.epochs` read from `contracts.json` — never the raw argparse count"。cap 在 contract 阶段已被吸收进 contracts 的值级指纹（yaml input 描述：实际轮数取 min(cap, 全量轮数)，生效值记入 full_train_budget），report 节点从不消费原始 cap 输入（report_format 的占位符仅 `<project-root>/<write-back>/<accuracy-budget>`），且仅凭 cap 也推不出生效轮数。git 历史核实：HEAD~1 的 report_format.md 已是"读 contracts"，该锚属 v3.5 起的错位表述，v4 SPEC 明钉后成为显式不一致。**建议修法**：改为 "the cap is applied at the contract stage into `contracts.json`'s `full_train_budget.epochs`; the Fairness Note cites that recorded value (see the references file)"。
2. **F2（轻，内部时序缝）** `agent.md:24,58` vs `:65-80` — 资源锚/Lazy Loading 均说 Step 1 开始才读 `references/report_format.md`，但 Step 0（"before anything else"）已要求 "act per the format document's harvest table"。v4 新增 Step 0 后 "read it at Step 1" 指针过期。**建议修法**：两处改 "read it before Step 0"。
3. **F3（轻，未定义术语）** `agent.md:86` — "run it in derive-only mode first"：`derive-only mode` 在 workflows/ 与 docs/ 全仓零定义（grep 仅本文件命中），report_format.md 亦无此模式；执行者自写 builder 或可会意，但指令引用了不存在的接口。**建议修法**：内联一句定义（如 "a flag that stops after the terminal-state table"）或删掉该短语。
4. **F4（轻，可独立执行缺口）** `agent.md:37-38` — `report_dir` 落档规则 "suffix the copy with the run id instead" 未说明 run id 从何取（本文件及 report_format.md 均未在落档语境定义；`ORCA_RUN_ID` 仅出现在 chart env 语境）。**建议修法**：点名来源（如 `$ORCA_RUN_ID`）。

## 四、核对通过项（SPEC §4 po_report 行逐条）

- 终态收割三分支 + 双组 kill + variants 扫杀 + "aborted at terminal" 披露 + cmdline 守卫：agent.md Step 0 与 SPEC/report_format §0 三方一致 ✓
- row3 三态 / ref_acc 直读 baseline_full_acc / 内归因并入 propose（DONE/verdict/history 三态）/ baseline.proxy_acc = 曲线@k 不足 null+披露：正确下沉至 report_format.md（该文件另有独立审查），agent.md 指针不与之矛盾 ✓
- Fairness Note：**不一致 → F1** ✗
- finalize chart（push_curves `(final)` best-effort + makespan 趋势）✓
- 写回断言（≥1 promoted 全套 / no-promotion 零写回+披露非失败）✓
- stage enum 8 值：agent.md 不枚举（下沉 report_format，其表覆盖 8 值全集），不冲突 ✓
- 输出字段清单 = yaml output_schema required（14 字段逐一致）✓
- 语气：通篇产品说明书式；无事故复盘/迁移出处/issue 编号/测试项目名/SPEC-ADR 编号 ✓

VERDICT: ISSUES (4)

---

## 复验（commit 24eb711，2026-08-26）

- 复验范围：`git diff 2de195e..24eb711 -- workflows/agents/po_report/agent.md`（工作树与 24eb711 对该文件零漂移，已核）；修复后全文件重跑词表 grep（任务词表 + SPEC §6 增补 + derive-only/epoch-only 追加口径）——**零命中**。
- **F1（full_train_epoch_cap 锚）已闭环** — 新措辞 "the cap already took effect at the contract stage and is recorded in `contracts.json` `full_train_budget.epochs`; the Fairness Note cites that recorded value"：与 SPEC §4 行"Fairness Note 轮数读 full_train_budget.epochs"及 report_format.md §5（:236-238）逐点对齐，与建议修法一致（原 :30-32 → 现 :32-36）。
- **F2（Step 1 读指针过期）已闭环** — Resource Anchors（现 :24-26）与 Lazy Loading（现 :62-63）两处均改为 "read it BEFORE Step 0"，并显式注明 Step 0 依赖其 harvest table。时序缝消除。
- **F3（derive-only 未定义）已闭环** — 未定义短语整体删除，替换为 "re-runs are safe — the builder is idempotent"（与 report_format.md 的 safe-to-re-run 契约一致，未引入新未定义术语）。
- **F4（run id 来源）已闭环，新增事实声明逐项核实为真** — "the run id is `$ORCA_RUN_ID`, the engine-injected run identity the workspace `.run_lock` records; a run-metadata value, not tied to the chart env"：①`ORCA_RUN_ID` 确为引擎注入（`orca/exec/env.py:93` `overlay["ORCA_RUN_ID"] = run_id`）；②`.run_lock` 为本 workflow 真实工作区文件（`po_flatten/scripts/check_flatten.sh:39-41` 写入 `{run_id, pid, ts}`，`po_flatten/agent.md:62` 文档化——workflow 级文件故首轮 orca/ 源码 grep 未命中，本轮已核实）；③"not tied to the chart env" 正确（env 注入无条件，chart env 仅额外要求 4 键齐全）。
- 修复未引入新残留/新不一致：diff 仅涉 4 处修复点，语气仍产品说明书式，无新增开发期引用。

VERDICT: CLEAN
