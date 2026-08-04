# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前：KD-NAS P5 修复完成（opencode events result 取末条消息），headless e2e 进行中

**任务**：修 engine 核心 result 抽取 bug（opencode events 模式下中间叙述里的 `[shape]` 字面量被当成
result 抢过末条合法 JSON），打通 KD-NAS e2e 的最后一个阻塞。

**状态**：**P5 修复完成 + 单测全绿 + 真实 e2e flatten PASS**（commit `269e288` + `f9fe02c`）。
- `orca/exec/claude/accumulator.py` `events_result_text` 改取末条 `agent_message`（`_last_text`），
  对齐 SDD 契约「agent 最终消息 = JSON result」。`result_extractor.py` 不动（blast radius 最小）。
- 决定性回归门：`test_p5_tape_replay_kd_nas_flatten_extracts_final_json` 用真实失败 tape（101 events）
  replay，断言抽出 seq 98 合法 JSON object，`"[1,1,28,28]" not in result_text`。
- tests/exec/ 440 passed + 1 skipped；tests/profiles/ 89 passed；code-reviewer 一轮闭环（0 BLOCKER）。
- 真实 e2e `kd-nas-20260805-011253-6c2ebe`：flatten 节点 seq 92 `node_completed` PASS（P5 原失败点），
  setup / gen_teacher / gen_train_script 依次 PASS，train_script_verify 进行中。

**待办**：
- [ ] **P6（新发现，非本次范围）**：finalize agent 末条 JSON 结构性畸形（缺根级 `}`）→
  `result_extractor` fail loud。需调 finalize agent prompt（属 workflow 层，不在 P5 engine 范围）。
- [ ] Phase 5 E2E（KD-NAS Trainer 引擎化）遗留——详见下方。

### 真实 e2e 终态（run_id `kd-nas-20260805-011253-6c2ebe`）

- **12/13 节点 PASS**：flatten（P5 原失败点直接验证）→ setup → gen_teacher → gen_train_script →
  train_script_verify → train_teacher（真 10 epoch CPU）→ gen_student/distill/decide × 2 轮 → finalize FAIL。
- **finalize 失败**：agent 末条 ```json fence 内 JSON 畸形（depth=1，缺根 `}`）。**与 P5 无关**
  （旧「全串接」语义对同一 fixture 同样失败，已验证）。属 workflow-agent bug。
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
- P5 release note `docs/releases/2026-08-05-opencode-events-result-last-message-p5.md`
- 计划 `docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`（§5 Phase 5 checklist + §11 v3.2）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§3.1 flag diff + 调用点矩阵 + §6 叶子契约 + migrate_flat CLI）
- Phase 1-5 release note `docs/releases/2026-08-04-kd-nas-trainer-engine-phase{1,2,3,4,5}.md`
- 引擎源：`workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`
