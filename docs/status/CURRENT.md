# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## ⚠ 并行协调协议（两 loop 必读，2026-08-27 生效）

**workflows/ 目录写权归「目录隔离改造」loop 直到其批 H 完成**（预计顺序：批 B kd 删除 → C 加载层 → D 大迁移 → E install → F skill → G web → H 验证）。
**prof-opt v5 loop**：spec 评审环（只读 workflows/）可并行；**Phase 3 实现必须等 `workflows/prof-opt/` 目录已存在**（批 D 落地标志），届时 SPEC 内所有 `workflows/prof-opt.yaml` / `workflows/agents/_po_scripts` / `workflows/subagents/prof-opt` 路径按新布局（`workflows/prof-opt/workflow.yaml` / `workflows/prof-opt/agents/_po_scripts` / `workflows/prof-opt/subagents`）换算——plan 级调整，fail loud 上报。迁移 loop 批 D 前须 `git log --oneline -3` 核对无 v5 写入 workflows/ 的提交；有 → fail loud 停。

---

## Workflows per-workflow 目录隔离改造 —— SDD loop 进行中（▶ 用户 2026-08-27 指令"go"提前续跑，18:05 定时已撤；批 E 进行中）

**暂停/续跑**：cron `8744fa15`（今天 18:05，durable 已验证落盘 `.claude/scheduled_tasks.json`）。万一丢失：任何新会话读本文件即可续跑——剩余批次 E install 重构 / F skill 同步 / G web（先 Plan agent 设计）/ H review+全链验证 / I 收尾，dispatch 模板见 SPEC 步骤 4-8 + 计划批 E-I 单。批 E 起验收口径：相对 pre-existing 集合（66 个：[v3] audit 13 / playwright libnspr4 33 / in_session+cli 17 / skill_md 1 / flaky 2）零新增。批 D commit 已落地（56d0db1）→ **v5 开工闸已开**（见下段，双方 commit 精确 add 限定触达清单）。

**任务**：workflows/ 平铺 → per-wf 自包含目录（`<wf>/workflow.yaml + agents/ + subagents/ + knowledge_base/ + scripts/`）；双形态加载兼容；kd-nas 净删除；create-workflow skill 同步；web 显示 sub-agents + 脚本资产。
**SPEC**：`C:\Users\mozzie\.claude\plans\crystalline-chasing-dewdrop.md`（PASS）｜**计划**：`docs/plans/2026-08-27-workflow-per-dir-layout-plan.md`（READY）
**Phase**：Phase 3 实现——**批 A 完成**（commit `a379375`，v2 固化 + 基线 `.layout_baseline_list.txt` 源态口径 15 wf 含 kd-nas；注意 `orca list` CLI 混扫安装态多 po-probe 尸体，diff 一律用源态口径）；**批 B 完成**（kd-nas 净删除：58 文件删 + 混合测试改判 test_struct_kd_p7/test_receiver_variants 保留 kd 外用例 + e2e_redesign 契约 kd 条目清零 + 注释死例换 _po_scripts；源态 catalog 14 wf；deferred 待批 D/H 裁决：knowledge_base kd 专属卡死链 / workflows 内 KD-NAS 大写死例文本 / examples/kd-nas-demo README 死链）；**批 C 完成**（加载层双形态：新 `orca/compile/layout.py` 单一真相源〔`scan_workflow_yamls` 平铺优先 + `resolve_subagents_dir` 双形态含误命中守卫 any(*.md)〕由 catalog/orchestrator/validator 三处 import 复用；KB per-wf 来源〔R4'：env > config > per-wf〔判据含 index.json〕> ~/.orca > cwd + `_INJECTED_KB_ENV` 防进程级 env 伪显式〕；render/validator 错误文案双形态化；单测四组 +14 用例；code-reviewer 3 轮 CLEAN；源态探针 14 wf 与批 A 基线逐字段一致〔除批 B 删的 kd-nas〕，平铺源态行为零变化）；**批 D 完成**（commit `56d0db1`：`scripts/_migrate_per_dir.py` 一次成型——14 wf 目录 + 69 agent 分流〔parser 引用 + pz_expand 显式补充〕+ 共享副本〔4 agent ×2 + _quant_scripts ×4，sha256 全一致〕+ KB/kb_graph 收编 + psu parents[4]→[5] 等 5+2 白名单修正；md 铁律 364 个全 R100/A；测试路径 48 文件同步；验收：catalog 14 wf 与基线 diff 一致〔仅 kd-nas 消失〕、全量非 e2e 4170 passed 且 66 失败全归因 pre-existing〔批 C worktree 对照铁证 17 + [v3] audit 13 + playwright libnspr4 33 + skill_md 1 + 顺序泄漏 flaky 2〕；code-reviewer 2 MAJOR 已修闭环〔po_scripts 变量 join 漏改 + config_kb cwd fallback 合成化〕、3 MINOR 记录不修〔一次性脚本〕；**workflows/prof-opt/ 已存在——v5 loop 可按新布局开工**；遗留入批 H REPORT 待决策区：struct yaml :20/:87/:108/:239 描述性旧路径文本〔运行期走 $ORCA_WORKFLOWS_ROOT 零断链〕、verifier.md:34 白名单断链、env.py/install_cmds/routes 旧布局注释死例、kb_graph gitignore 旧条目、e2e_redesign/contract.py 冻结）
**基线 diff 口径**：源态直扫（load_workflow 逐 yaml），勿直接 diff `orca list` 输出
**批 E 完成**（commit `aeb22b0`：install 三函数合一 per-wf 整树 sync + 旧平铺 UD-1 四分支 backup 清理〔①非随包名→整目录 backup ②名同内容异〔逐文件 sha256，共享 agent 多副本 any-match〕→backup ③平铺 yaml 一律 backup〔同名 shadow 源/未知尸体，非随包名集合轻量判据保模块零业务逻辑〕④完全一致→直接删；未知非 yaml 只 warn〕+ CLI per-wf 打印 + run_skill_benchmark 探测加 workflows/*/agents；比对/部署 ignore 口径 _IGNORE_PATTERNS 单一真相源；tests/iface/cli 636 passed 零新增失败〔批前后均恰 2 pre-existing：bg_integration/web_does_not_import_cli〕；code-reviewer 2 轮终 CLEAN〔4 MINOR+5 NIT 全修〕；批 D deferred 的 install_cmds 旧布局注释死例已随批改写；真机 install 验证留批 H）
**批 F 完成**（commit `e6acda2`：create-workflow skill 产出布局 per-wf 化——SKILL.md 落盘 `./workflows/<name>/workflow.yaml` + 新增「产出布局」目录树节 + design.md 落点入 wf 目录；reference 三文件锚定/范例路径同步〔cleanliness-contract 实测无死例零改动〕；benchmark README per-wf 化；16 case expected 重排为 `expected/<wf-name>/`〔目录名=name 字段，30 纯 rename 零内容改动〕，case 14 平铺例外钉死不动；examples/charts 三注释死例 per-wf 化；test_skill_benchmark 双形态 glob 断言 + 目录命名契约断言；验收 101+43+88 passed 零新增、skill 目录死例 grep 零命中；code-reviewer 2 轮闭环〔1 MAJOR=design.md「不随产物分发」假声明改写裁决 + 1 MINOR + 2 NIT 全修〕）
**无人值守**：计划外问题 fail loud 停下写 `LAYOUT_MIGRATION_REPORT.md`；pytest/tars 走 WSL .venv；不 push

---

## Prof-opt v5 —— SDD loop 进行中（并行，见顶部协调协议）

**SPEC**：`docs/specs/prof-opt-v5-spec.md`（**PASS** 3 轮；U1/U2/U3 已裁决回填；errata 两处已回填：§2.3 比对集四字段 / §6.1 撕裂恢复读法）｜**计划**：`docs/plans/2026-08-27-prof-opt-v5-plan.md`（**READY**，adversary 3 轮 18 质疑全闭环）｜**Phase**：Phase 3 实现——**coder-agent 进行中**（SDD-ORCHESTRATOR，2026-08-27 晚 dispatch；批 D 56d0db1 落地开工；按 plan S0/C1..C7 序；外环回退 0/2）
**竞态约束**：只写 workflows/prof-opt/ 子树 + tests/test_po_* + v5 自有文档；精确 git add；基线 7 脏文件不卷入；E2E 未过不算完成（收尾归编排者）
**P8 裁决（v5 侧，宽读法）**：批 D 即 v5 开工门槛；v5 只写 `workflows/prof-opt/` 子树不碰迁移 E-H 触达面；撞车 fail loud 双停
**进度（2026-08-27 晚，coder-agent）**：S0 断言过 + C1..C7 七 commit 落地（fdd7a52 脚本层 / 203fbe7 推进门控 / f7f4add 戳·模式·规则池 / 49a1d50 per-agent 读盘 / ff86ef0 workflow+prompt 契约 / 4c8c6f1 smoke 收口 / d46e9d5 内环评审修复）；内环 code-reviewer 2 轮闭环（第 1 轮 1 MAJOR+6 MINOR 全处置——round_state 单一来源收编等；第 2 轮定向复核 CLEAN，m7 显式 deferred 有 SPEC/plan 依据）；pytest 两文件 197 passed 0 failed、tars validate 零 error 零 warning、洁净元层脚本 exit 0；E2E 未跑不算完成（真机 §11.4 清单归用户，收尾归编排者）；一起竞态事故已修复（C5 首 commit 误卷迁移 loop 已 staged 的 31 个 benchmark 文件，soft reset 后精确重提交为 ff86ef0，对方磁盘内容完整保留为其未暂存改动）

---

## 已完成（勿重复）

- create-workflow skill v2 固化（commit `a379375`，2026-08-27）
- prof-opt v4 重构完成（2026-08-26，13 commits，CHANGELOG [2026-08-26]）

## 工作区遗留（非本任务，不动）
- `.e2e_po/`、`.e2e_spe2e/` scratch；`docs/specs/prof-opt-v5-spec.md`（v5 loop 资产，其自行处置）；`.layout_baseline_list.txt`（本任务基线，不提交）
