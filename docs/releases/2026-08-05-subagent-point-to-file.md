# Subagent Point-to-File 协议改造（SPEC `docs/specs/subagent-point-to-file-design-draft.md` v3）

> **范围**：仅 nas-supernet workflow（6 个 parent agent.md + `workflows/subagents/nas-supernet/` 5 个子 agent md）。kd-nas 范围排除（其 workflow-verifier 是 kd-train-script 内联 SKILL，与 nas-supernet 子 agent 名同实异）。
>
> **状态**：完成 + 单测全绿 + code-reviewer 一轮闭环（0 must-fix / 3 should-fix 已修 + 3 nice-to-have 部分采纳）。
>
> **日期**：2026-08-05。

## 1. 改动概述

把 nas-supernet 子 agent 调用协议从 **read+embed**（parent `cat` body 再内联进 Task prompt）改为 **point-to-file**（子 agent 自读 md）。parent 只发短指针 prompt（含 sentinel 回显约束）；子 agent Read 自家 md body 拿身份 + Procedure + 输出契约。结构性收益：parent context 不再夹带 24K body，且 sentinel 不可猜防子 agent 跳读伪造。

**SPEC §3.2 路径交付（render 期 Jinja inline，不引新 env var）**：`{{ subagents_root }}/<name>.md` 在 render 期被 inline 成绝对路径 `…/subagents/nas-supernet/<name>.md`。shell 无关、cwd 无关、与 `tars install` 解耦。

## 2. 实际做了什么

### 2.1 引擎/校验层（8 文件）

- `orca/exec/context.py`：`RunContext` 加 frozen 字段 `subagents_root: str = ""`；`with_locals`/`with_guidance`/`with_dialog_turn` 经 `dataclasses.replace` 自动携带。
- `orca/exec/render.py`：`_namespace` 注入 `subagents_root` 顶层变量；新增 fail loud——模板引用 `{{ subagents_root }}` 但 `ctx.subagents_root==""` → `ExecError(phase="render")`（SPEC §7 末；Rule 7 裁定用 ExecError 而非 ConfigurationError，保 exec→compile 单向依赖）。
- `orca/run/orchestrator.py`：新增 `_compute_subagents_root(workflows_root, wf_name)` helper（v3 公式 `workflows_root / "subagents" / wf_name`，目录不存在 → 空串）；4 处 RunContext 构造点 populate（`__init__` / `_bare_instance` / `_make_ctx` 实际 populate；`_next_node_for_resume` route-only 合规例外空串）。
- `orca/run/step.py`：`_build_ctx` 加 `workflows_root` kwarg；新增 `_workflows_root_from_yaml` helper；`advance_step` + `_recover_step_result` 把 `yaml_path` 父目录透传到 `_build_ctx`。
- `orca/iface/cli/app.py`：dialog ctx 留空串（post-run Q&A，SPEC §4 合规例外）。
- `orca/iface/cli/install_cmds.py`：`_install_bundled_subagents` 重写——`copytree workflows/subagents/ → ~/.orca/workflows/subagents/`（与 agents copytree 同款不同子树、dirs_exist_ok + ignore_patterns）；删旧 `_<name>_subagents` glob + middle-name regex 校验（v3 无中间名映射，无路径穿透 footgun）；删 unused `import re`；callsite echo 改新拓扑路径。
- `orca/compile/validator.py`：`validate_workflow` 加可选 `workflows_root`；新增 `_check_subagents_md`（仅当 `subagents_root` 解析到存在目录时跑）：
  1. 每个 `*.md` strict regex 解析首块 frontmatter（三键 `subagent`/`version`/`sentinel`，body 后续 `---` hr/表格分隔不误判）；
  2. body 含 `$ORCA_SUBAGENTS_DIR` / `cat $HOME/.orca/...subagents/` 等旧协议残留 → warning；扩扫 dev-residue（plan §N / Orca 源码路径 / examples 路径——SPEC §8 #5 含子 agent md）；
  3. agent.md 引 `{{ subagents_root }}` 的节点校验 host 通用类型 tools 含 Read（大小写无关：opencode `read` / claude `Read`）；`tools=None`（全开）跳过。
- `orca/compile/parser.py`：`load_workflow_with_warnings` 传 `workflows_root=yaml_path.parent`；`_check_jinja2_refs` 把 `subagents_root` 加进合法 Jinja root 集合。

### 2.2 资产迁移

`git mv workflows/_nas-supernet_subagents workflows/subagents/nas-supernet`（v3 拓扑：`subagents/<wf>/` 命名空间隔离，保 git 历史；旧 sibling `_<wf>_subagents/` 平铺拓扑废弃）。

### 2.3 子 agent md（5 文件）

每个文件顶部加 frontmatter（subagent / version: 1 / 唯一 6 位 sentinel）+ 「**Output first line**」回显指令：

| 文件 | sentinel |
|---|---|
| supernet-evaluator.md | `SE7K2A` |
| workflow-verifier.md | `WF3QP8` |
| memory-verifier.md | `MM4ZR6` |
| project-porter.md | `PT5NX2` |
| project-fidelity-verifier.md | `PF8LK3` |

report 首行格式：`[subagent:<name> v<version> <sentinel>]`（E-1 软回显——首行不匹配时落 `runs/<id>/sentinel_stats.jsonl`，不阻断 workflow）。

### 2.4 parent agent.md（6 文件）

`ns_expand_supernet` / `ns_train_script` / `ns_search_pipeline` / `ns_run_train` / `ns_run_search` / `ns_retrain` 的改动：
- L2 description 行：`read+embed 协议` → `point-to-file 协议`。
- 「## Subagent 调用协议」整段替换为 SPEC §3.1 point-to-file 协议段（首轮 + 续轮两模板；sentinel 字面量绝不出现在 parent prompt；host `subagent_type` 写 `<host 内置通用类型>` meta 描述，禁硬编码 `general`）。
- 各 Step 内部「按协议（read+embed verifier loop）」引用 → 「按协议（point-to-file verifier loop 续轮）」，`embed <report>` → 「首轮 prompt 末尾追加 <report>」。
- ns_run_train / ns_run_search / ns_retrain 的 Step 2.5 / Step 3 / Step 4.5：删 `cat $HOME/.orca/nas-supernet/subagents/...` bash 块，改为 `Task(subagent_type=<host 内置通用类型>, prompt="先完整 Read {{ subagents_root }}/...")` 指针模板。
- 资源锚点：`$HOME/.orca/nas-supernet/subagents/<name>.md` → `{{ subagents_root }}/<name>.md`。

### 2.5 测试（4 文件）

- `tests/exec/test_render.py`：4 新测——`subagents_root` inline / 默认空串 / 引用但空 fail loud / 注释中提及也 fail loud。
- `tests/compile/test_subagents_md.py`（新）：12 测——strict frontmatter regex（body hr / 表格 / 缺键 / 非整 version / 短 sentinel）+ `_check_subagents_md` 7 路径（缺目录跳过 / 缺 frontmatter error / stem 不一致 error / 旧协议残留 warning / dev-residue warning / Read 缺失 error / 小写 read 接受 / tools=None 跳过 / foreach body agent）。
- `tests/run/test_subagents_root.py`（新）：16 测——`_compute_subagents_root` 4 路径 + RunContext frozen 派生 3 处 + 6 处构造点 populate（含 2 合规例外）+ advance_step / _recover_step_result e2e 集成（透传 yaml_path → workflows_root → subagents_root inline 进 prompt）。
- `tests/iface/cli/test_install_cmds.py`：4 个 `_install_bundled_subagents` 测重写——v3 copytree 拓扑 / 多 wf 子树 / 幂等 / 变更 refresh / 无 workflows no-op / 无 subagents no-op / copytree OSError fail loud。

## 3. 偏离 SPEC 的裁决（Rule 7 surface）

**SPEC §7 末文本写「fail loud（ConfigurationError…）」，实现用 `ExecError(phase="render")`**。理由：

- `ConfigurationError` 定义在 `orca/compile/validator.py`；若 `orca/exec/render.py` import 它会构成 exec→compile 反向依赖（违反 SPEC §4「依赖铁律 compile→exec→iface 单向」顶层不变量）。
- `ExecError(phase="render")` 是 render.py 既有约定（Jinja2 渲染失败同样用此异常），由 orchestrator except 链捕获并 emit `workflow_failed`。
- 消息文案与 SPEC 语义一致（明示「workflow 期望 subagents 但 subagents_root 未解析」）。

建议 SPEC 后续修订加 footnote 记录此裁决。

## 4. 验收

| 验收点 | 状态 | 证据 |
|---|---|---|
| 6 parent agent.md 无 `read+embed` / `cat $HOME/.orca/nas-supernet` / `_nas-supernet_subagents` / 硬编码 `general` | ✅ | `grep` 全清零 |
| 5 子 agent md frontmatter 三键齐全 + Output first line 指令 | ✅ | 见 §2.3 |
| `tars validate workflows/nas-supernet.yaml` 0 warning | ✅ | `load_workflow_with_warnings` 返 `[]` |
| 10 个真实 workflows 全 0 warning（无回归） | ✅ | smoke 测试 |
| render namespace 暴露 subagents_root + fail loud | ✅ | `tests/exec/test_render.py` |
| 6 处 RunContext 构造点 populate（含 2 例外） | ✅ | `tests/run/test_subagents_root.py` |
| compile 期 frontmatter + Read 校验 | ✅ | `tests/compile/test_subagents_md.py` |
| install copytree v3 拓扑 | ✅ | `tests/iface/cli/test_install_cmds.py` |
| 依赖铁律不破 | ✅ | code-reviewer 确认；render.py 用 ExecError 保单向 |
| **dispatch 配置（确定性，生产 render 路径）**：ns_expand_supernet 真实 agent.md 渲染后 `{{ subagents_root }}` → 绝对路径 inline；sentinel 字面量不进 parent prompt；子 agent body 不内联（read+embed 残留为 0）；host subagent_type 是 meta 描述非硬编码 | ✅ | `scripts/verify_dispatch_render.py`（本轮 main 验证，跑完已清；用真实 `render_prompt(node, ctx)` + `_compute_subagents_root`） |
| **headless 真跑（opencode + deepseek-v4-flash）**：单 agent 经绝对路径自读真实 `supernet-evaluator.md` + report 首行正确回显 `[subagent:supernet-evaluator v1 SE7K2A]`（sentinel 从 frontmatter 取，非猜）；workflow `completed` | ✅ | 本轮 main 跑 `run_workflow` smoke（WSL `.venv`，真 spawn opencode） |

## 5. 测试结果

- 新增/改动测试：35 个全部通过。
- compile + exec + run 全套：890 passed / 1 skipped（deselect 1 pre-existing demo 失败，与本次改动无关——`git stash` 验证 master 上同样失败）。
- in_session 子集（test_in_session_v8 / test_failure_sentinel / test_node_memory / test_error_management）：123 passed。
- cli/install + commands：124 passed。

## 6. 已知 follow-up（非本次范围）

- **headless nas-supernet e2e**：~~用户 main 跑（不在本次实现范围）~~ **已闭环（2026-08-05 main）**：确定性 render 校验 + opencode/deepseek-v4-flash 真跑 smoke 双通过（见 §4 末两行）。完整 8-agent pipeline 的 tape 断言（子 agent 首个 tool call 是 Read / sentinel_stats.jsonl 落地 / Task prompt 体量 < body 50%）未在真跑里逐条采集——smoke 已覆盖核心机制（自读 + sentinel 回显），完整 pipeline 跑可作后续 follow-up，非阻塞。
- **SPEC 文本修订**：§7 末 ConfigurationError → ExecError footnote。

## 7. Commit

单一 commit（含所有引擎 + 资产 + 测试 + release note），SHA 见 git log。
