# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：subagent point-to-file 协议——实现完成 + 单测全绿，待 headless e2e

**任务**：SPEC [`subagent-point-to-file-design-draft.md`](../specs/subagent-point-to-file-design-draft.md) v3——把 nas-supernet 子 agent 调用从 read+embed 改为 point-to-file：子 agent 自读 `{{ subagents_root }}/<name>.md`（render 期 inline 绝对路径），parent 只发短指针 + sentinel 回显约束。

**状态**：**实现完成 + 890 单测绿 + code-reviewer 一轮闭环（0 must-fix）**。Commit: 见 git log。
- 引擎：RunContext 加 `subagents_root` 字段；orchestrator `_compute_subagents_root` + 4 处 populate；step.py `_build_ctx(workflows_root=...)` 透传；render.py fail loud（引用但空串 → ExecError）；validator `_check_subagents_md`（frontmatter strict regex + Read 校验大小写无关）；install copytree v3 拓扑。
- 资产：`git mv workflows/_nas-supernet_subagents workflows/subagents/nas-supernet`；6 parent agent.md 协议段重写；5 子 agent md 加 frontmatter（sentinel: SE7K2A/WF3QP8/MM4ZR6/PT5NX2/PF8LK3）。

**待办**：
- [x] 引擎 + agent.md + 子 agent md + 测试 + code-review + commit。
- [ ] **headless nas-supernet e2e**（用户 main 跑）：tape 断言子 agent 首个 tool call 是 Read 绝对路径 / report 首行含 sentinel / `sentinel_stats.jsonl` 落地 / prompt 体量 < body 50%。

**必读**：
- 本任务 release note `docs/releases/2026-08-05-subagent-point-to-file.md`
- SPEC `docs/specs/subagent-point-to-file-design-draft.md`（§3.1 协议段 / §5.2 frontmatter strict regex / §7 compile 校验）

---

## 历史：PostToolUse 事后告警守卫——coder-agent 完成 + 单测全绿，待 test-agent 四前端真机 e2e

**任务**：SPEC [`posttooluse-rogue-guard.md`](../specs/posttooluse-rogue-guard.md)——给四前端（cc/cac + opencode/nga）加 PostToolUse 纯提示 hook（B 路径扩展）：主 session 在活跃 run 期间自己用 Edit/Write/跑 train → 事后注入文本提醒。不阻止、不推进、不捕 output。

**状态**：**coder-agent 完成 + code-reviewer 两轮闭环（一轮 4 should-fix + 二轮 fresh 1 must-fix + 2 should-fix）+ 138 单测绿 + install 四前端 dry-run 验证通过**。Commit: 见 git log。
- `tool-classification.json`（§5 单一真相源：writing/bash 工具集 + readonly 前缀 word-boundary + 复合分隔符（含 `>` / `>>` 重定向）+ guard_reason_template）
- `cc_nudge.sh`：hook_event_name 分支（Stop 字节级不变 / PostToolUse 新增 additionalContext 输出 + 30s guard 节流 + session_id fallback R5 + unbound 心跳 R1 + **marker 损坏 fail-open 不 exit 2（strict 参数）**）
- `install_cmds._install_cc_nudge`：合并 hooks.PostToolUse 条目（matcher 锚定 + 去重）+ 拷 classification；`_install_opencode` 同步拷 classification
- `orca.ts`：tool.execute.after 钩子（复用 listActiveRuns/nudgeAllowed；throttleFile 参数化；idle/guard 独立 mutex `injectingIdle`/`injectingGuard`；classification 候选路径穷举 user/project scope）
- 27 新测覆盖 §11.1/§11.2/§11.4 + Stop 字节级 golden fixture + malformed marker fail-open 回归

**待办**：
- [x] coder-agent 实现 + 单测（138 passed）
- [ ] **test-agent 四前端真机 e2e**（SPEC §11.2 CC 真机 + §11.3 opencode 真机 + R1/R5 spike 闭环）：注入证据（消息历史 / decision JSON）+ idle nudge 回归保护。

**必读**：
- 本任务 release note `docs/releases/2026-08-05-posttooluse-rogue-guard.md`
- SPEC `docs/specs/posttooluse-rogue-guard.md`（§3 与 A 路径退场区别、§10 R1/R5 spike fallback）

---

## 历史：KD-NAS codegen 反造假修复完成，待 headless e2e 验 teacher 真训练 acc

**任务**：修审计 run `6c2ebe` 发现的 KD-NAS codegen 数据造假真根因（最严重）——`torchvision`
不在叶子 import 白名单 → codegen 用 `torch.rand`+`torch.randint` 冒充 MNIST → teacher acc=0.12
锁死 ln 10 / student acc=0.09（零学习）。

**状态**：**修复完成 + 单测全绿 + code-reviewer 一轮闭环 + 已 commit**（`f22568b`，待 e2e）。
- 扩 import 白名单（`_leaves.py` + `fidelity_check.py`）：含 torchvision/torchaudio/scipy/sklearn/PIL + stdlib；禁用户项目模块保留。
- `fidelity_check.py::_check_no_random_fabrication`：AST 扫 data.py/eval.py，捕 `torch.rand/randn/randint/normal/rand_like/randn_like` + `numpy.random.*` + `random.*` + in-place `uniform_/normal_/...`；`torch.randperm` / seed 类合法跳过；用户 train.py 自身用 random → 视为 verbatim port。
- SKILL.md / agent.md / workflow doc / 2 checklist / 4 leaf skel / CONTRACTS §6 / kd-nas.yaml：全套加反造假硬规则（port 真实 loader / 不可得时 fail loud + ask-user 哨兵）。
- CONTRACTS 删迁移叙事行；守门 regex 加 `已移除`/`相对单体`。
- fail-loud：`--user_train/eval` 缺失 → rc=2 + stderr（原裸 traceback rc=1）。
- code-reviewer 一轮闭环：3 must-fix（np.random.seed 误判 / torch.randperm shuffle 误判 / 缺失文件 fail-loud）+ 1 nice-to-have（factory 变体覆盖）已全修；新增 6 测覆盖。

**测试**：175 passed / 2 skipped（原 169 + 6 新）；audit-run 6c2ebe artifact 经新 fidelity_check 复测准确 4 处造假捕获。

**待办**：
- [x] commit `f22568b`。
- [ ] **关键 headless e2e**（`tars run workflows/kd-nas.yaml` 对 `examples/mnist_kd/`，max_rounds=2 full_epochs=2 device=cpu）：**核心验证 teacher 真训练 acc > 0.90**（非 0.12）；data.py 应 port 真实 torchvision MNIST（非 torch.rand）+ loss 真下降（非锁死 ln 10）+ ≥1 轮蒸馏 student acc 合理 + finalize（P6）过。
- [ ] 如实报 teacher/student 真实精度。
- [ ] Phase 5 E2E（KD-NAS Trainer 引擎化）遗留——详见下方。

**必读**：
- 本任务 release note `docs/releases/2026-08-05-kd-nas-codegen-anti-fabrication.md`
- `workflows/agents/_kd_scripts/CONTRACTS.md` §6（叶子契约 + 反造假）
- `workflows/agents/kd-train-script/scripts/fidelity_check.py`（`_check_no_random_fabrication`）

---

## 历史：KD-NAS P6 修复完成（finalize JSON 改 json.dumps 发射）

**状态**：已完成（commit `4cd2428`）。详见 [release note](../releases/2026-08-05-kd-nas-finalize-json-dumps-p6.md)。

P6 修复要点：`workflows/kd-nas.yaml` finalize inline prompt 新增 Step 3 `python3 -c json.dumps({...})`
发射 + viz 解析合并进单 try/except + stderr 显式告警；output_schema / Step 1（finalize_kd.py）/
Step 2 viz_kd_stage 调用 / routes / outputs 零改动。tars validate 通过；守门测试绿；kd-nas 测试套件
169 passed / 2 skipped；code-reviewer 一轮闭环 0 must-fix / 2 nice-to-have 已合并。
- `workflows/kd-nas.yaml` finalize 节点 inline prompt 新增 Step 3 `python3 -c json.dumps({...})`
  发射（对齐 distill/decide 模式）；删手写 ```` ```json ```` 模板；viz 解析合并进 Step 3 单
  try/except + stderr 显式告警。
- output_schema / Step 1（finalize_kd.py）/ Step 2 viz_kd_stage 调用 / routes / outputs 零改动。
- tars validate 通过；守门测试绿；kd-nas 测试套件 169 passed / 2 skipped。
- code-reviewer 一轮闭环：0 must-fix / 2 nice-to-have（stderr 日志 + Step 2/3 兜底对称合并）已合并。

**待办**：
- [ ] **真实 headless e2e 被 opencode 环境破坏阻塞**（非 P6 问题）：opencode 1.14.22 binary +
      `~/.local/share/opencode/opencode.db` 在 2026-08-05 03:28 被改动（疑似 auto-update），
      `__drizzle_migrations` 表清空（0 rows）但 `project` 表残留迁移后 schema（多
      `icon_url_override` / `commands` 列）→ 新 binary 启动时跑初始 CREATE TABLE `project`
      撞已有表 → exit_code=1 → spawn phase 失败。已用 `opencode run "say hi"` 直跑复现（同样
      错误，非 orca 路径问题）。**需用户决策**：备选恢复路径（a. 回滚 opencode binary 到
      03:28 前版本；(b. 手工补 __drizzle_migrations 哈希；(c. 重置 db 但丢失会话历史）。
      orca 代码侧零改动。
- [ ] 真实 headless e2e `kd-nas-20260805-033107-04e861` 已 fail（flatten spawn 阶段）。
      待 opencode 环境恢复后重跑——目标 13/13 节点全 PASS。
- [ ] Phase 5 E2E（KD-NAS Trainer 引擎化）遗留——详见下方。

### 真实 e2e 终态（run_id `kd-nas-20260805-033107-04e861`，P6 验证）

- **fail**：opencode 子进程 spawn 阶段 exit_code=1（详见上文待办）。**未触达 finalize**，
  无法验证 P6。需先修 opencode 环境。

### 历史 e2e（run_id `kd-nas-20260805-011253-6c2ebe`，P5 验证 + 暴露 P6）

- **12/13 节点 PASS**：flatten（P5 原失败点直接验证）→ setup → gen_teacher → gen_train_script →
  train_script_verify → train_teacher（真 10 epoch CPU）→ gen_student/distill/decide × 2 轮 → finalize FAIL。
- **finalize 失败**：agent 末条 ```json fence 内 JSON 畸形（depth=1，缺根 `}`）。**与 P5 无关**
  （旧「全串接」语义对同一 fixture 同样失败，已验证）。属 workflow-agent bug → 本次 P6 修复。
- **产物**：ledger 2 行 / champion=baseline / latency=42.26µs / accuracy=0.9 / final_report.md 已写。

---

## 历史：KD-NAS Trainer 引擎化重构——Phase 5 纯净度清扫完成，Phase 5 E2E 待开工

**任务**：把 `kd-train-script` 单体 codegen 重构为「固定 `KDTrainer` 引擎 + 4 叶子（loss/data/eval/optim）+ run.sh」；产物拍平到 `artifacts/`（去 kd-nas 层）；agent prompt 去 SPEC 源化；补 resume / 早停 / 循环内 evaluator。

**计划**：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（5 Phase，独立可 commit；v3.2 经 spec-reviewer 四轮闭环）。

**状态**：**Phase 5 纯净度清扫完成**（commit `e3c2c2b`，零回归 + code-reviewer 一轮闭环：0 must-fix / 1 nice-to-have 已在 release note 显式说明）。

### Phase 5 E2E 待办（端到端验证）
- [ ] E2E `examples/kd-nas-demo` 全链路 + resume 多时点 smoke + 早停 patience 触发 + 拍平迁移 smoke（旧 `kd-nas/` 真迁）
- [ ] headless e2e 全链路（P5 修复后正在重跑）

### 已知 follow-up（非 kd-nas 重构范围）
- **P2**: `~/.orca/runs/<id>/log` 空文件（executor 日志 bug）—— executor 层
- **P3**: flatten 9m46s+ deepseek 重读文件延迟 —— 优化层（本 e2e 实测命中）

**必读**：
- P6 release note `docs/releases/2026-08-05-kd-nas-finalize-json-dumps-p6.md`
- P5 release note `docs/releases/2026-08-05-opencode-events-result-last-message-p5.md`
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§5 Phase 5 checklist + §11 v3.2）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§3.1 flag diff + 调用点矩阵 + §6 叶子契约 + migrate_flat CLI）
- Phase 1-5 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase{1,2,3,4,5}.md`
- 引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`
