# 2026-07-16 — nas-hp-search runner/select 反伪造强制 + tars install skill 改名清理

## 背景

`nas-hp-search.yaml` 端到端「跑通」（5 节点全 completed）但**假执行**：tape 铁证显示
`runner`(3s)/`select`(19s)/`train_script_gen`(1s) 三个 agent 根本没跑脚本，只把上游
`{{ }}` 的描述性散文**原样复述**（search.jsonl 的 640 条是诊断时手动跑的，时间戳在 wf 完成后）。
根因 = **prompt 诱骗**（顶部上游散文的「已完成/已验证」语域诱骗 deepseek 顺着复述，压过执行指令）
+ **无强制**（fake 了还静默标 `node_completed`）。共享 agent `nas-train-runner` 同病——重 pipeline
`nas-agent-pipeline` 的 `train_runner`(36s 照抄) 一样假执行。

另外发现：`tars install --target cc` 装出的 Claude Code skill 名是陈旧的 `orca`（tars 改名前装的
残留），与命名约定（**skill = `tars`**）不符；且 install 代码不清理被改名的旧 skill 目录。

## 做了什么

### 1. nas-hp-search runner/select 反伪造 + output_schema 强制（核心）

- **`nas-train-runner/agent.md` 重写**（共享 agent，两条 pipeline 受益）：
  - 执行指令置顶、**删上游散文灌入**——`output_dir` 改用 `{{ inputs.output_dir }}`（不再从
    `{{ search_pipeline_gen.output }}` 抠，去诱骗 + 去抠传脆性）；训练改判 `if [ -f run_train_supernet.sh ]`
    （文件存在性，无需 LLM 判 TRAINING_VIABLE）。
  - **反伪造**：明令回复只能是命令真实输出、不许复述/伪造 DONE。
  - **自校验 JSON**：bash 末尾用 python 从**真 `search.jsonl` 计数**输出 JSON（`search_records` 来自
    真文件，伪造不出），整段 bash 仅此一行进 stdout。
  - **契约影响**：`nas-train-runner` 现要求运行时**显式传 `output_dir`**（两个 pipeline 实测都传；
    默认空串则需注意）。
- **`nas-select/agent.md` 重写**：同样去诱骗（`{{ inputs.output_dir }}`）+ 反伪造（只跑
  `select_and_report.py` 原样回显 stdout）。
- **`nas-hp-search.yaml` runner 加 `output_schema`**（`search_records: minimum 1` 等）：in-session
  `step.py:_parse_output` 确定性强制——散文复述/0 记录 → `output_schema_mismatch` → `node_failed`。
  runner 不真跑搜索过不了。

### 2. tars install skill 改名清理

- **`orca/iface/cli/install_cmds.py:_install_skill`** 加改名迁移清理：install 时自动清掉陈旧的
  `skills/orca/`、`skills/teams/`（入口 skill teams→orca→tars 改名残留），跟现有 `command/orca`
  清理同 pattern（fail-soft warn）。造陈旧目录实测命中清理。
- 修陈旧 docstring（`orca`→`tars`）。
- **`CLAUDE.md`** 加「TARS 是 SKILL 不是 CLI」注记：skill 编排、驱动 `orca` CLI、不存在 `tars <wf>`；
  `tars` 同名两物（skill 名 ≠ `tars` 管理命令）。

## 验证

- `tars validate workflows/nas-hp-search.yaml` → 0 error（剔除脚手架后）。
- **E2E 端到端两次通过**（opencode+flash，FAST/MOCK 脚手架绕开 deepseek-pro 慢）：5 节点全
  `workflow_completed`；runner 自校验 JSON（`search_records` 数真文件）过 `output_schema`；
  select 真跑 `select_and_report.py` → nas-select 选出 **top-3 架构**（`selection_summary.json` +
  3 个 `arch_*.json` + `final_report.md`）；chart 落 tape（C5/C6）。
- **关键验证**：runner 的「不真跑就过不了」由 output_schema 确定性强保——伪造散文/0 记录必 node_failed。
- `tars install --target cc` 重装 → CC skill 正名 `tars`（陈旧 `orca` 已清）；`orca doctor` →
  `skill_install: PASS（cc, opencode）`；改名清理逻辑造陈旧目录实测命中。

## 偏离 / 注意

- **验证脚手架已剔除**（FAST MODE 跳过 + `.mock_search`）：它们是为绕开本 session deepseek(pro/flash)
  后端慢、`search_pipeline_gen`（nas-search-pipeline skill 的重 LLM 校验循环）连续卡死而加的临时物，
  **不进生产 agent**。CC 用 Claude 子代理生成不会卡，无需它们。`search_pipeline_gen` 在 deepseek 慢时
  卡死是独立问题（该 skill 重校验设计 × deepseek 慢），非本次修复范围。
- `nas-train-runner` 改用 `{{ inputs.output_dir }}` 是共享 agent 契约变更：两条 pipeline 运行时须显式传
  `output_dir`（实测都已传）。
- `select` 暂未加 `output_schema`（仅 runner 有）；两次它都真执行，但理论上仍可 fake。follow-up 可给
  `select_and_report.py` 末尾吐 JSON + select 节点加 output_schema，达同等确定性强保。
- `nas-search-pipeline`（含 SKILL/refs/assets）、`nas-train-runner` 的 `scripts/` 等仍部分未跟踪（用户
  整包提交计划）；本次仅随修复提交 `nas-train-runner/`（含 agent.md + scripts/tail_metrics.py）。

## Commit

- `fix(nas-hp-search)`: runner/select 真执行 + output_schema 强制 —— nas-train-runner/ + nas-select/agent.md + nas-hp-search.yaml（本 release note + CHANGELOG + CURRENT）
- `fix(tars-install)`: skill 改名清理 + CLAUDE.md TARS=SKILL —— orca/iface/cli/install_cmds.py + CLAUDE.md

（SHA 见 `git log`。）
