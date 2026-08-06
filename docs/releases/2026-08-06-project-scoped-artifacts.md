# Project-scoped artifacts（跨 run 复用 + 多 workflow 隔离）

> SPEC：[`docs/specs/project-scoped-artifacts-design-draft.md`](../specs/project-scoped-artifacts-design-draft.md)（spec-review 14 issue 全闭 → 实现）

## 改了什么

### 1. 引擎面（仅 in-session 单文件）
`orca/iface/in_session/cli.py`：
- 新增 `_read_workflow_inputs(tape_path)`——镜像既有 `_read_workflow_name` 的 tape 头扫描骨架，return `workflow_started.data.inputs` dict（无 ws / 损坏 → `{}`）。**禁** import 私有 `orca.events.replay._replay_state_and_inputs`（不存在公开 `inputs_from_tape`）。
- 新增 `_resolve_artifacts_dir(tape_path, run_id) -> tuple[Path, bool]`——SPEC §2.1：
  - workflow inputs 含非空**绝对** `project_root` + 有 wf_name → `<project_root>/artifacts/<wf_name>/` + `is_project_scoped=True`；
  - 否则 → per-run `runs/<run_id>/artifacts/`（向后兼容）+ `is_project_scoped=False`。
  - **签名偏差**：SPEC 示例签名 `_resolve_artifacts_dir() -> Path`，实返 `tuple[Path, bool]`。理由：SPEC §2.1 明确要求 project-scoped mkdir fail loud 区别 per-run fail-open，调用方须知道路径性质；tuple 比 caller 用 `artifacts_dir_for_run` 字面比对更清晰、避免双算。
- bootstrap 站点（`_start_run` 算 artifacts_dir + `mkdir -p` + `_write_orca_env`）改调 `_resolve_artifacts_dir`：project-scoped mkdir 失败 → raise（fail loud）；per-run mkdir 失败 → 既有 warn 不 fail（fail-open）。
- `_derive_artifacts_dir(tape, run_id)` 改为透传 `_resolve_artifacts_dir(Path(tape.path), run_id)` 的 path（next 路径不重 mkdir）。

### 2. nas-supernet
- `workflows/nas-supernet.yaml`：input `user_project_root` → `project_root`（名 + 描述 + `$ORCA_ARTIFACTS_DIR` project-scoped 语义注释）。
- 8 个 `workflows/agents/ns_*/agent.md`：所有 `{{ inputs.user_project_root }}` Jinja 站点 → `{{ inputs.project_root }}`（sed 批量）+ 散文 `<user_project_root>` 同步。
- 6 个昂贵节点加 **Step 0: Reuse-Check**（SPEC §2.4 软跳过）：`ns_expand_supernet`（supernet.py + summary + manifest 三产物 + exec 验证）、`ns_train_script`（train_supernet.py + run_train_supernet.sh 双产物 + 语法 / 引用验证）、`ns_search_pipeline`（select_architecture.py + search_config.yaml + evaluator.py + arch_codec.py 四产物 + 语法 / YAML 验证）、`ns_run_train`（supernet ckpt + `torch.load` state_dict 验证）、`ns_run_search`（search_results.jsonl ≥1 行合法 JSON 验证）、`ns_retrain`（final retrain ckpt + `torch.load` state_dict 验证）。
- status 枚举不动 + 不加新字段：reused 走成功路径同一 status（`ns_run_train` / `ns_run_search` / `ns_retrain` → `executed`；生成节点无 status 字段）；`ns_run_train` 的 `skipped` 仅留 viability self-gate（脚本不存在），**不**用于 reused。
- 复用可观测性：assessment 前缀 `reused existing <产物>` + artifact mtime 早于本次 run 起点（机械可检，防 LLM 谎报 reused）。
- 既有"行为痕迹 marker 文件"section（ns_run_train / ns_run_search / ns_retrain 的原 Step 0）→ 改为非编号 `## 行为痕迹 marker 文件（self-heal 期间维护，约定）` heading，为 Step 0 reuse-check 让位。
- 3 个严格编辑 agent 的禁碰清单（ns_run_train / ns_run_search / ns_retrain）+ project-porter 子 agent 加 carve-out（SPEC §2.5）：`{{ inputs.project_root }}` 下**源文件**只读禁改；**例外**：`{{ inputs.project_root }}/artifacts/` 是本 workflow 产物目录树，可写。
- 3 个 nas-supernet 子 agent 散文参数名 `<user_project_root>` → `<project_root>`：`project-porter.md` / `project-fidelity-verifier.md` / `memory-verifier.md`（parent↔subagent 参数契约同步，防 verifier 误报 mismatch）。`workflow-verifier.md` / `supernet-evaluator.md` 无散文引用，不动。

### 3. kd-nas（撤销拍平，落 `<project>/artifacts/kd-nas/`）
- `workflows/agents/kd-setup/agent.md`：`KD_ARTIFACTS_DIR="${PROJECT_ROOT}/artifacts/"` → `"${PROJECT_ROOT}/artifacts/kd-nas/"`（下游 CHECKPOINTS_DIR / STUDENT_MODELS_DIR / SCRIPTS_DIR / ONNX_DIR / META_DIR / REPORTS_DIR / WORKTREE_ROOT / LEDGER_PATH / CHAMPIONS_PATH 全由它派生，自动平移）。
- 删除 `kd-setup/agent.md` 内 migrate_flat.py 调用块（不再拍平；保留会自我误触发——`_validate_layout` 期望 `kd_old = flat_new/kd-nas`，而改后两者同目录）。
- 删除 `workflows/agents/_kd_scripts/migrate_flat.py`（520 行）+ `tests/workflows/test_migrate_flat.py`（方向反转，保留即错）。
- `workflows/agents/model-flatten/{agent.md,SKILL.md}`：`${PROJECT_ROOT}/artifacts/models/baseline/` → `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/`（agent.md 输入段 + 准备 step3 bash + low-confidence 边缘 prose；SKILL.md `<output_dir>` 段同步）。model-flatten 是 kd-nas 专属 flatten agent，硬编码 `kd-nas` 可接受（它跑在 setup 之前，不能读 setup 输出）。
- `workflows/kd-nas.yaml:73,87,122`：描述同步（`flat_artifacts_dir` → `<project>/artifacts/kd-nas/models/baseline/`；`kd_artifacts_dir` → `<project>/artifacts/kd-nas/`）。
- `tests/workflows/test_model_flatten.py:630-672`：`test_flatten_agent_md_output_dir_co_rooted_with_setup` 反转 6 条 pin 断言（`${PROJECT_ROOT}/artifacts/models/baseline/` → `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/`；`artifacts/kd-nas/ NOT in` → `artifacts/models/baseline/ NOT in` 反向校验）。
- `workflows/agents/_kd_scripts/CONTRACTS.md`：删 migrate_flat 段 + 路径树 `<project>/artifacts/` → `<project>/artifacts/kd-nas/`。
- kd-nas 软跳过 Step 0：
  - `model-flatten/agent.md`：扫 OUTPUT_DIR 下既有 `*_flat.py`，跑 `validate_contract.py` PASS → 复用（project-scoped path 跨 run 稳定，真复用）。
  - `train-teacher/agent.md`：既有 step1 sha256 幂等 check 已等价 Step 0，加 framing note 指明 step1 即 Step 0（不重复实现，DRY）。
  - `kd-train-script/agent.md`：4 叶子 + run_config 当前落 per-run（SPEC §2.3 明示无 project_root input → §2.1 per-run 回落），cross-run reuse 受限；Step 0 主要覆盖同 run re-arm 场景（fidelity audit fail → 同节点重派）。

## 一轮 code-review 闭环（commit `1cb377f`）

实现后做了一轮 code-review，surgical 修正：

- bootstrap post-lock raise 原会留 orphan marker + 裸 traceback → `_resolve_artifacts_dir` 调用包 `try/except ValueError` → emit `workflow_failed` (kind=`invalid_inputs`) + `clear_marker` + JSON 错误信封 + `typer.Exit(1)`，对齐 `InSessionError` 路径。
- `_resolve_artifacts_dir` docstring 补 Rule 7 surfacing（签名偏离 SPEC 示例 `-> Path`，本实现 `tuple[Path, bool]`，理由：fail-loud mkdir discriminator）。
- 6 个 Step 0 bash block 删 `EXEC_REUSE*` dead variables（跳过信号已通过 echo 文本 + disk marker 传递）。
- ns_run_train / ns_run_search / ns_retrain Step 0 stale-marker 清理统一为 `rm -f` only（Step 3/5 python 对缺文件 read 默认 `"false"` / `[]`，无需空文件占位）。
- model-flatten Step 0 物理位置移到「Step 3 确定 OUTPUT_DIR」bash 块之后（order-by-position 比 order-by-prose 可信）。

**遗留**：bootstrap 集成测试（`CliRunner` 驱动 `orca <wf> --inputs '{"project_root":"/abs"}'` 断言 mkdir + env 注入 + 相对路径 raise 走结构化错误信封）——测试补全，另立 case。

## 偏离 SPEC 的地方（先记下）

1. **`_resolve_artifacts_dir` 返 `tuple[Path, bool]`**（SPEC §2.1 示例签名 `-> Path`）——为支持同函数调用点（bootstrap）区分 project-scoped fail-loud vs per-run fail-open mkdir 语义。最小必要扩展。
2. **train-teacher Step 0 是 framing note**（非新 bash 块）——既有 step1 的 sha256 幂等 check 已覆盖 SPEC §2.4 reuse-check 语义（确定性查 + sha256 验证 + 命中跳过），重写即 DRY 违反。
3. **kd-train-script Step 0 scope 受限**——SPEC §2.3 明示 kd-nas 4 叶子 + run_config 落 per-run，cross-run reuse 不稳定。Step 0 主要覆盖同 run re-arm（fidelity audit fail → 同节点重派）。文档化此限制，不禁用 Step 0（SPEC §2.4 列了 gen-train-script）。

## 验收

- `tars validate workflows/nas-supernet.yaml` → ✓ 0 error / 0 warning。
- `tars validate workflows/kd-nas.yaml` → ✓ 0 error / 0 warning。
- grep `user_project_root` in `workflows/agents/ns_*/agent.md` + `workflows/subagents/nas-supernet/` + `workflows/nas-supernet.yaml` → 0 命中（kd-nas 子 agent 排除，未动）。
- 新增 `tests/iface/in_session/test_resolve_artifacts_dir.py` 15/15 通过（绝对 project_root → project-scoped / 相对 → raise / 无 → per-run 回落 / wf 隔离 / 空串 / 非 dict / 头部坏 tape / scan 上限 / 一致性）。
- `tests/iface/in_session/test_in_session_cli.py` 126/126 通过（既有 bootstrap + next 路径无回归）。
- `tests/workflows/test_model_flatten.py` 57/57 通过（6 条 pin 反转后过）。
- 删除的 `tests/workflows/test_migrate_flat.py` 不再 collect。

## 非目标（SPEC §5）

- 不改 executor / `tars run`（`tars run nas-supernet` 仍落 per-run `runs/<run_id>/artifacts/`，复用仅 in-session）。
- 不引入核心 schema 字段（`project_root` 是普通 workflow input；node output_schema 不加新字段）。
- 不动 chart / tape / marker per-run 机制。
- 不含 schema 不匹配 / 超网张开方向 / 训练提前退出等其它议题（另案）。

## Commit

- `797a6c8` —— feat: project-scoped artifacts + nas-supernet input rename + kd-nas 撤销拍平（核心三块）。
- `1cb377f` —— fix: code-reviewer 闭环（bootstrap raise 结构化 + Step 0 dead code 清理 + docstring Rule 7）。
- `77013e4` —— docs: CHANGELOG + CURRENT.md 索引更新。
