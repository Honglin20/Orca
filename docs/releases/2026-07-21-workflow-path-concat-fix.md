# 2026-07-21 P2 (Phase 4-B) — workflow 产物路径拼接漏斜杠 BUG 修复

> 计划：[`docs/plans/2026-07-21-workflow-redesign.md`](../plans/2026-07-21-workflow-redesign.md) §4-B
> 任务派发：coder-agent P2（批1，与 P1 render_chart 轴标签 / P3 0-b spike 并行）

## 根因

模板用字符串拼接 `{{ family_detect.output.output_dir }}.worktrees/<candidate>/` / `{{ teacher_setup.output.output_dir }}snapshots/...` 赌 LLM 输出 `output_dir` **带尾 `/`**；LLM 偶尔忘尾斜杠 → 拼出 `<run>snapshots/`、`<run>.worktrees/` 兄弟孤儿目录（与正确的 `<run>/snapshots/`、`<run>/.worktrees/` 子目录并存），账本 / worktree / snapshot 分裂成两套，下游读路径漂移。

证据（修前 file:line）：
- `workflows/agents/struct-engineer/agent.md:31,37` —— worktree / snapshots 拼接
- `workflows/agents/kd-engineer/agent.md:74,89-90` —— 同上
- `workflows/agents/kd-teacher-setup/agent.md:43-44` —— profile 目录拼接
- `workflows/kd-nas.yaml:116,121` —— profile_report.json 拼接
- `workflows/agent-struct-exploration.yaml:225,314,315,325,326,368,409,410,421,422` —— structure_gate / viz_round / finalize / viz_finalize 内联 prompt 的 ledger/champions 拼接
- `workflows/kd-nas.yaml:236,337,338,429,430` —— kd_trainer / viz_round / viz_finalize 内联 prompt 的 ckpts/ledger/champions 拼接

## 改法：单一真相源 = setup 节点 output_schema 一次计算显式字段

核心约束：**目录路径只在 setup 节点算一次，下游只读字段、不自己拼根。**

### 1. setup 节点 output_schema 新增显式字段（additive，原字段全部保留）

**struct `family_detect`** 新增 5 字段（`workflows/agent-struct-exploration.yaml:71,79-83`）：
- `snapshots_dir` —— `<output_dir>/snapshots/`（末尾带 `/`）
- `worktree_root` —— `<output_dir>/.worktrees/`（末尾带 `/`，隐藏目录）
- `viz_dir` —— `<output_dir>/viz/`（末尾带 `/`）
- `ledger_path` —— `<output_dir>/ledger.jsonl`（完整路径）
- `champions_path` —— `<output_dir>/champions.jsonl`（完整路径）

**kd `teacher_setup`** 新增 6 字段（`workflows/kd-nas.yaml:88,93-99`）：
- 上述 5 个 + `ckpts_dir` + `profile_report_path`

全部进 `required` 列表 → LLM 漏输出 → `output_schema_mismatch` fail loud（运行时守护已被 `tests/iface/in_session/test_in_session_cli.py:705` + `tests/exec/claude/test_result_extractor.py:91` 双路径覆盖）。

### 2. setup 节点 prompt 强制 `os.path.abspath(...) + "/"` 双保险

统一采用 `OUTPUT_DIR=$(python3 -c "...")` 模式（`workflows/agent-struct-exploration.yaml:99-122` / `workflows/agents/kd-teacher-setup/agent.md:46-69`）：
- `os.path.abspath` 消去 `..` / `.` / 重复 `/`
- 末尾 `+ "/"` 显式拼尾斜杠
- `${OUTPUT_DIR}<suffix>` 在 setup 节点**内部**是安全拼接（OUTPUT_DIR 末尾已带 `/`，由 setup 节点自己保证）
- 派生字段经 `echo "KEY=value"` 打印 → LLM 原样填进 JSON output → 下游经 `{{ ...output.<field> }}` 读，**零**字符串拼根

`export OUTPUT_DIR SNAPSHOTS_DIR ...`（m1 修复）：让同会话后续 bash block 沿用 shell 变量，避免「step 2 设、step 3 失」的跨 shell fragility。

### 3. 下游 agent.md / yaml inline prompt 改读字段

| 文件 | 改动 |
|---|---|
| `workflows/agents/struct-engineer/agent.md:17,19-20,31,37,39` | `champions_path` / `snapshots_dir` / `worktree_root` 字段替代拼接 |
| `workflows/agents/kd-engineer/agent.md:32-34,76,77,83,91,92` | 同上（worktree_root + snapshots_dir） |
| `workflows/agents/kd-teacher-setup/agent.md:41-72` | 重写 step 2 为 `OUTPUT_DIR=$(...)` + 派生 7 个 shell 变量 + export |
| `workflows/agent-struct-exploration.yaml:225,314,315,325,326,368,409,410,421,422` | structure_gate / viz_round / finalize / viz_finalize 改读字段 |
| `workflows/kd-nas.yaml:116,121,236,337,338,429,430` | profile_gate / kd_trainer / viz_round / viz_finalize 改读字段 |

### 4. m1 修复：kb_cache/ 局部回归

首次实现时把 `kd-teacher-setup/agent.md` 原 `mkdir -p "$OUTPUT_DIR"{snapshots,ckpts,kb_cache}` 中的 `kb_cache/` 漏了（prose line 15 仍提它）。code-reviewer MAJOR-1 抓到 → 在 mkdir 列表里补回 `"$KB_CACHE_DIR"`（`workflows/agents/kd-teacher-setup/agent.md:55,59`），避免 out-of-scope `kd-analyst/agent.md:36` 读 `{{ teacher_setup.output.output_dir }}kb_cache/` 时找不到目录 fail loud。

## 验证

- `tars validate workflows/agent-struct-exploration.yaml` → 0 error
- `tars validate workflows/kd-nas.yaml` → 0 error
- `pytest tests/compile/` → 127 passed（含 Jinja 语法 / 引用校验）
- `pytest tests/iface/in_session/test_in_session_cli.py::test_failure_output_schema_field_violation tests/exec/claude/test_result_extractor.py` → 21 passed（output_schema_mismatch 双路径守护）
- Jinja 全量渲染：5 个改后文件 + 2 个 yaml 全部 inline prompt，stub ctx 渲染 → 0 orphan 拼接（grep 验证无 `/tmp/run1snapshots` / `/tmp/run1.worktrees` / `/tmp/kdrunprofile_report` 等孤儿）
- bash smoke：struct 与 kd setup block 真跑 → `ls -a $OUTPUT_DIR` 显示 `.worktrees/ snapshots/ viz/ kb_cache/ ckpts/ ledger.jsonl champions.jsonl` **全部为子目录/子文件**，父目录 `ls` 显示零孤儿兄弟

## 决策与偏离

### 决策记录（Rule 7 surface）

1. **范围外 agent.md 留给 Phase 3 P7**：以下 7 个下游 agent 仍有同款 `{{ ...output.output_dir }}<suffix>` 拼接（code-reviewer MAJOR-2 / test-coverage 附带发现 1 标注）：
   - `workflows/agents/struct-evaluator/agent.md:13,31`（31 行是 directory concat，理论上仍有孤儿风险）
   - `workflows/agents/struct-curator/agent.md:21,41,63,64`
   - `workflows/agents/struct-analyst/agent.md:19`
   - `workflows/agents/kd-curator/agent.md:31`
   - `workflows/agents/kd-analyst/agent.md:15,35,36,53,88`
   - `workflows/agents/kd-hypothesizer/agent.md:29,41,44,56,75`
   - `workflows/agents/kd-train-script/agent.md:71,82,86,94,99,106`

   **理由**：任务派发明确列出 5 个 in-scope 文件，且计划 §3-b 显示 Phase 3 P7 会合并/删除大部分这些节点（kd 13→7、struct 11→7）。本批 P2 只做 setup 节点 single-source-of-truth + 5 个 evidence 列出的下游。

   **风险缓解**：setup 节点 `os.path.abspath(...) + "/"` 双保险从**源头**杜绝孤儿（即便下游仍用 `{{ output_dir }}<suffix>`，产出的也是 `<run>/snapshots/` 子目录，不是 `<run>snapshots/` 兄弟孤儿）。所以验收标准「不再产生孤儿」已满足，只是代码风格未完全统一（DRY/Rule 11）。

   **建议**：P7 重构时统一收口（包括把 `train_kd_path` / `kd_recipe_path` / `selection_spec_root` 也加进 setup output_schema）。

2. **CONTRACTS.md 不更新**：`workflows/agents/_kd_scripts/CONTRACTS.md:201` 节点 I/O 表只列 `teacher_cache, teacher_meta{...}`，连原 `output_dir` / `build_fn` 都没列（pre-existing incomplete），新增 6 字段也未列入。
   - **理由**：任务派发显式列 5 个 in-scope 文件，CONTRACTS.md 不在列表；且文件 pre-existing 不完整，单条补丁价值低。
   - **建议**：P7 重构时整体重写 CONTRACTS.md 节点 I/O 表。

3. **`viz_dir` 字段保留**：plan §3-a 标 `viz/` 为「死目录」要在 Phase 3 P7 删；但本任务（Phase 4-B）只修拼接 bug，按 additive 原则保留 `viz_dir` 字段；P7 删 viz/ 时连同字段一起删。

### 已知遗留（非阻塞，登记给 P7）

- M2 / 附带发现 1：7 个 out-of-scope agent.md 仍有拼接（见上「决策 1」）
- 附带发现 2：CONTRACTS.md 节点 I/O 表 stale（见上「决策 2」）
- 附带发现 3：无 CI 守门防真实 workflow 失效（可加 `test_real_workflows_validate` parametrize 测试 glob `workflows/*.yaml` 跑 `validate_workflow`，~10 行代码）

## Commit

- `e41974f` `fix(workflows): 修 struct/kd 路径拼接漏斜杠——setup 节点 output_schema 显式带尾斜杠字段`
- `<docs-commit-sha>` `docs(workflows): P2 release note + CHANGELOG + CURRENT`

## 相关文件（绝对路径）

- `/mnt/d/Projects/Orca/workflows/agent-struct-exploration.yaml`
- `/mnt/d/Projects/Orca/workflows/kd-nas.yaml`
- `/mnt/d/Projects/Orca/workflows/agents/struct-engineer/agent.md`
- `/mnt/d/Projects/Orca/workflows/agents/kd-engineer/agent.md`
- `/mnt/d/Projects/Orca/workflows/agents/kd-teacher-setup/agent.md`
