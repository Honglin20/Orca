# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-08-03）：kd-nas 串行迭代重写 — 待 E2E 真机

### 干了什么
按 SPEC v3（`docs/specs/kd-nas-serial-iteration-rework.md`，spec-reviewer 三轮对抗 PASS）实现 kd-nas
串行迭代重写：批量并发 → 串行迭代 DAG（10 节点 + decide back-route）。新写 4 脚本 + 5 agent；
精简 kd-setup（拆出 train_teacher）；扩展 model-flatten/teacher-gen/kd-train-script；重写 yaml。
code-reviewer 一轮反馈闭环（2 FATAL + 5 MAJOR + 2 MINOR 全修）。

### 状态
- 实现 + 单测 + tars validate + code-reviewer 反馈闭环：**完成**。
- E2E 真机（task #6）：**待用户跑**（本环境无 GPU + opencode + deepseek 真后端）。

### 必读文件（≤5）
1. `docs/specs/kd-nas-serial-iteration-rework.md`（SPEC v3 契约）
2. `docs/releases/2026-08-03-kd-nas-serial-iteration.md`（本次 release note）
3. `workflows/kd-nas.yaml`（10 节点串行 DAG）
4. `workflows/agents/_kd_scripts/kd_reducer.py`（KD 决策 reducer 真相源）
5. `workflows/agents/distill/agent.md`（catch 协议 + 命令 flag 完整性范式）

### 待办
- [用户] E2E 真机：用 baseline 不达标 fixture（target=baseline×0.7 / acc_baseline=baseline+margin）
  强制 ≥2 轮 distill→decide 循环 → finalize；验收 max_rounds 终止 + champion ratchet + 各轮 DUMMY_INPUT 字节级 == baseline。
- [用户] 旧 yaml 批量节点文件（gate_all/train_pool/pick_variant 等）保留供回滚；如确认不回滚可清理。
