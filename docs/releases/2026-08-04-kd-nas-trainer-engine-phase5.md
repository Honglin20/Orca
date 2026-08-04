# KD-NAS Trainer 引擎化重构 · Phase 5 —— 任务纯净度清扫（决策标签 / 历史叙事清除 + 守门测试强化）

> 计划：[`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md)（§5 Phase 5）
> 前序：[Phase 4 release note](./2026-08-04-kd-nas-trainer-engine-phase4.md)

## 目标

Phase 4 已把 `SPEC §x` / `cleanup` / `Phase N` 类来源叙事清除，但**审查过程决策标签**
（D2 / D8 / D10 / E1-E13 / M1-M8 / N3-N21 / Q6-Q10 / B4 / R1-R3 / F3 / A4-A8 等审查过程 ID）
以**非括号形式** pervasive 残留，3 个 agent description 含历史对比叙事，CONTRACTS / yaml
夹带迁移叙事。用户硬线：workflow agent 必须只关注任务本身，零来源 / 过程叙事。

**本次只做文本层清扫 + 守门测试强化——零逻辑 / 契约 / CLI / 字段改动。**

## 改动

### 1. agent prompt 决策标签清扫（保留行为描述，删过程 ID）

- `kd-train-script/agent.md`：description 删「下游节点不再 import 生成脚本——它们调固定引擎」
  改述当前契约；`Step 2 — D8 AST 检测` → `Step 2 — AST 检测`；红线节同步删 `D8`。
- `train-script-verify/agent.md`：`（Q6：禁 sibling/相对 import...）` → `（禁 sibling/相对 import...）`；
  `（E9：函数名 + 必填位置参数集...）` → `（函数名 + 必填位置参数集...）`。
- `distill/agent.md:214`：`# ★ E13/M1：redirect stdout...` → `# ★ redirect stdout...`。
- `kd-train-script/references/templates/leaves/eval.py.skel:8`：`(the single source of truth, D2)`
  → `(the single source of truth)`。

### 2. 引擎 .py 决策标签清扫（保留设计 why，删过程 ID + 审查归属）

- `migrate_flat.py`（重灾区 ~15 处）：`（Q10/N3/E1/E2/E3）` / `（R2：...）` / `（E1/D-B）` /
  `**幂等（E1）**` / `（E3）` / `（A4/E2）` / `**fail loud 适配（code-reviewer R3）**` /
  `# 数据安全契约（code-reviewer R3）` / `（D10）` 等——括号内 / 尾部过程 ID + `code-reviewer Rx`
  归属全删，保留设计说明文字。
- `kd/trainer.py`：`# M3 None guard` → `# None guard`（×2）；`R1: schema keys only` →
  `Schema keys only`；`# B4 — scheduler state drop` → `# scheduler state drop`；
  `(F3)` 删；line 384/727 的 `Rule 12` 通用 fail-loud 提示保留；**line 396 `CLAUDE.md Rule 12`
  外部文档归属改写**——该处在 `raise SystemExit(...)` 的 stderr 字面量内（运行时可见文案），
  非注释/docstring，但用户规格明确点名该行须改写外部归属（`CLAUDE.md Rule 12` 是过程性引用），
  且 fail-loud 行为契约（非零退出 + 报因）零改动——stderr 文案变更在 release note 此处显式记录。
- `kd/_resume.py`：`* **R1**:` → `* **Schema keys only**:`；`"""B4 — scheduler_state..."""`
  → `"""scheduler_state..."""`。
- `kd_reducer.py`：3 处 `（N12）` / `tie 不 ratchet，N12` 删 `N12`。
- `finalize_kd.py:307`：`FIFO tiebreak N12` 删 `N12`。
- `fidelity_check.py` / `kd/_leaves.py`：grep 确认已干净。

### 3. description 历史叙事纯化（改述当前职责，删迁移对比）

- `kd-setup/agent.md` description：`**不含 teacher 训练**（已拆到独立 train_teacher 节点）` →
  `setup 只做路径探测 + baseline seed + device（teacher 训练在 train_teacher 节点）`；
  红线节同步改述。
- `kd-train-script/agent.md` description：`下游节点不再 import 生成脚本——它们调固定引擎...` →
  `下游节点调固定引擎...（不 import 生成脚本）`。
- `gen-student/agent.md` description：`kd-nas 串行版 gen-student（合并 hypothesizer+engineer 为一节点）` →
  `kd-nas 串行版 gen-student`（删合并历史）。

### 4. CONTRACTS.md + yaml 迁移叙事清扫

- `CONTRACTS.md:41`：`<project>/artifacts/` 注释删「旧 artifacts/kd-nas/ 由 migrate_flat.py 原子迁移」
  （迁移机制在 migrate_flat.py 自身 docstring 说，不进当前契约布局描述）。
- `CONTRACTS.md:57`：`_*.py` 注释删「旧 pick_variant glob 排除，现...」对照。
- `CONTRACTS.md` §3.1 flag-diff 表：`单体 inline user_* slot / --user_* flag | **删除** | 旧运行时注入机制随骨架化移除` →
  `| **已移除** | 用户逻辑经 kd-train-script codegen 产 4 叶子承载 |`。
- `kd-nas.yaml:111`：setup 节点注释删「拆到」。
- `kd-nas.yaml:203`：`D8 AST 检测` → `AST 检测`。
- `kd-nas.yaml:331`：distill 节点注释 stale 修正——`distill 训练（--kd_config recipe 必传）`
  与实现矛盾（实际 read→patch run_config.yaml，禁 inline --kd_config），改为
  `distill 训练（read→patch run_config.yaml 的 kd_config 字段）`；同时删 `（新写，单 student KD）` 的「新写」叙事。
- `kd-nas.yaml:419`：`champion 是真 student 时用 champion ckpt 跑 eval（不重训，N10 省 GPU）` 删 `N10`。

### 5. ★ 守门测试 deny-list 强化（堵假阴性）

`tests/workflows/test_kd_prompt_no_source_narrative.py` 原只锁括号形式 `(E4)`。本次扩展：

- **分层 deny-list**：agent prompt 层（`.md` / `.yaml` / `.skel`）应用全量规则（共享 + prompt 专属）；
  引擎 `.py` 仅应用共享规则（D7 边界——`.py` 允许设计注释）。
- **PROMPT_DECISION_TAG**：非括号决策标签——`[DEMNQBRAF]\d{1,2}` 后跟 `：` / `:` / `）` / 空白，
  或斜线复合 `E13/M1` 形式。
- **HISTORICAL_NARRATIVE**：`已拆到` / `不再 import` / `合并...为一节点` / `旧...现...`（30 字内对照）/ `随骨架化移除` / `拆到独立` / `拆到 train_teacher`。
- **ALLOW_LINE_SUBSTRINGS** 加 `noqa`（flake8 行内豁免 `# noqa: E402` 不误伤）。
- 失败消息指引改指向 phase5 release note（D7 边界）。

## 验收

| 项 | 结果 |
|---|---|
| agent prompt 层 grep 决策标签 + 历史叙事 | 0 命中 |
| 引擎 .py 决策标签过程 ID | 全清（设计注释保留） |
| 守门测试 deny-list 强化 | 通过（agent prompt 层对新增模式 0 命中） |
| `pytest tests/workflows/ tests/workflows/test_struct_kd_p7.py` | **468 passed, 3 skipped**（基线 467 +1 强化测试计数无回归；3 skipped 同基线） |
| 抽样对照 nas-search-pipeline/agent.md 范本 | kd-setup / kd-train-script / gen-student description 均达「一句话角色 + 任务 + 契约 + fail 条件，零历史对比」纯净态 |

## 偏差

- 计划只列了 `D[2-9]` 模式，实际 grep 发现 `D10`（`migrate_flat.py:29` 磁盘峰值说明）——同样属过程 ID，
  Rule 7 选「一并清」路径：D10 是审查过程编号不是固定概念，删除。
- `kd-setup/agent.md` 红线节 `已拆到 train_teacher 节点` 在 description 改述后仍残留——一并改述为
  `teacher 训练属 train_teacher 节点`（否则新增的 `拆到 train_teacher` 守门模式会立即红）。

## Commit

`e3c2c2b`

## code-reviewer

自检 dispatched（agent prompt 任务必要信息未误删 / 无语义改动 / 守门测试真堵假阴性 / D7 边界正确 / 范本对照）。
反馈：0 must-fix / 1 nice-to-have（trainer.py:396 stderr 字面量改动已在上面「引擎 .py」节显式记录，
非语义变更，行为契约零改动）+ 2 optional（HISTORICAL_NARRATIVE 精简 / 单边 `旧 X` 锁）——非用户规格要求，surface 不采纳。
