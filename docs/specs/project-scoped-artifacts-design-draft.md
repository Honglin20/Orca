# 设计草稿：project-scoped artifacts（跨 run 复用 + 多 workflow 隔离）

> 跨阶段设计议题草稿。实现前必读。聚焦 in-session 入口产物目录策略 + 两个 NAS workflow 的产物
> 落点统一与软跳过。**不改 executor / `tars run` 路径**。

## 1. 目标

让 in-session 驱动的 workflow 的**昂贵产物**落在一个项目级、跨 run 稳定的目录，使跨 run 复用成为
可能；并用 workflow_name 子目录隔离同项目下多个 workflow（nas-supernet / kd-nas）。

统一产物落点约定：

```
<project_root>/artifacts/<workflow_name>/
```

- nas-supernet → `<project>/artifacts/nas-supernet/`
- kd-nas → `<project>/artifacts/kd-nas/`

配套：nas-supernet 的项目根 input 由 `user_project_root` **改名 `project_root`**；两个 workflow 的昂贵
节点加**软跳过**（先查产物在不在，在则验证达标就跳过重做）。

**机制差异（结果统一）**：
- nas-supernet 经 in-session `$ORCA_ARTIFACTS_DIR` 解析（§2.1，因它有 `project_root` input）直接落子目录。
- kd-nas 没有 `project_root` input（它是 setup 节点输出）→ 不经 §2.1 解析；其 durable 产物经 setup 节点
  算的 `kd_artifacts_dir` 落子目录（§2.3，撤销既有"拍平"、改回 `kd-nas` 子目录）。

## 2. 契约

### 2.1 in-session 产物目录解析（仅对有 `project_root` input 的 workflow 生效）

in-session 入口（`orca/iface/in_session/cli.py`）派生 `$ORCA_ARTIFACTS_DIR`：

- 当 workflow 的 inputs 含 `project_root`（非空**绝对路径**）→
  `$ORCA_ARTIFACTS_DIR = <project_root>/artifacts/<workflow_name>/`。
- 否则 → 既有 per-run `runs/<run_id>/artifacts/`（向后兼容，旧 workflow 零回归）。

**仅 nas-supernet 命中第一条**（唯一有 `project_root` 顶层 input 的 workflow）。kd-nas / quant-* 等无此
input → 走 per-run 回落（kd-nas 的 durable 产物不经此变量，见 §2.3）。

helper 签名（**两处调用点都从 tape 读 inputs**——`next` 命令的 `--inputs` 默认 `"{}"`（`cli.py:1341`），
next 路径拿不到原始 inputs，必须从 tape 重构；bootstrap 路径 tape 也已有 `workflow_started`，统一读
tape 最简。**禁** import 私有 `orca.events.replay._replay_state_and_inputs`，也**不存在** 公开
`inputs_from_tape`——用本地 helper 镜像既有 `_read_workflow_name` 的 tape 头扫描模式）：

```python
def _read_workflow_inputs(tape_path: Path) -> dict:
    """镜像 _read_workflow_name（cli.py:831）的 tape 头扫描，取 workflow_started.data.inputs。
    无 ws / 损坏 → {}（调用方按「无 project_root」回落 per-run）。"""
    # 逐字复用 _read_workflow_name 的扫描骨架，return obj.get("data", {}).get("inputs", {})

def _resolve_artifacts_dir(tape_path: Path, run_id: str) -> Path:
    wf_name = _read_workflow_name(tape_path)            # 既有
    inputs = _read_workflow_inputs(tape_path)           # 新增本地 helper
    proj = (inputs or {}).get("project_root", "")
    if proj and wf_name:
        p = Path(proj)
        if not p.is_absolute():
            raise ValueError(f"project_root 必须绝对路径：{proj!r}")  # 防相对路径跨 run 解析漂移
        return (p / "artifacts" / wf_name).resolve()
    return artifacts_dir_for_run(tape_path.parent, run_id).resolve()
```

`workflow_name` + `inputs` 都从 tape 读（bootstrap 与 `next` 两处统一，单一真相源）。

**失败路径（fail loud）**：
- `project_root` 非绝对 → helper raise（bootstrap fail loud，不静默 `.resolve()` 出漂移路径）。
- project-scoped 目录 `mkdir` 失败（只读项目根 / 权限）→ **bootstrap fail loud**（区别于 per-run 路径的
  fail-open：project-scoped 写不进去后续全崩，须起点暴露）。per-run 回落路径保留既有 fail-open 语义。

调用点（两处，替换现有 `artifacts_dir_for_run(...)` 直调）：
- bootstrap（`cli.py` `_start_run`，算 artifacts_dir + `mkdir -p` + `_write_orca_env`）。
- `_derive_artifacts_dir`（`cli.py`，`next` 重写 env 复用同一路径）——签名扩 `inputs` + `wf_name`。

### 2.2 nas-supernet input 改名 `user_project_root` → `project_root`

- `workflows/nas-supernet.yaml`：input 名 + 描述。
- 全 `workflows/agents/ns_*.md`（8 个）：所有 `{{ inputs.user_project_root }}` Jinja → `{{ inputs.project_root }}`。
- **nas-supernet 子 agent**（`workflows/subagents/nas-supernet/*.md`：project-porter / project-fidelity-verifier /
  memory-verifier / workflow-verifier / supernet-evaluator）：正文里散文参数名 `<user_project_root>` →
  `<project_root>`（这些是 parent↔subagent 的参数契约，如 memory-verifier.md "must match the caller-provided
  `<project_root>`"，rename 须两侧同步，否则 verifier 误报 mismatch）。
- `project_manifest.md` 的字段名 `source_project_root` **保留不改**（它是存储字段名，语义自洽，改名要迁移
  既有 manifest，不划算）。
- **kd-nas 子 agent（`workflows/subagents/kd-nas/project-fidelity-verifier-kd.md`）排除**——其 `<user_project_root>`
  散文指 kd-nas 的 flatten/setup 输出 `project_root`（不同语义），不属本次改名范围，单独 review 不动。

### 2.3 kd-nas 撤销拍平，durable 产物落 `<project>/artifacts/kd-nas/`

kd-nas 此前 `KD_ARTIFACTS_DIR="${PROJECT_ROOT}/artifacts/"`（无 kd-nas 子目录），并有
`migrate_flat.py`（520 行）专门把旧 `artifacts/kd-nas/` 拍平到 `artifacts/`，`kd-setup/agent.md:102-112`
检测旧子目录并迁移。本次改为 kd-nas 也进子目录，与 nas-supernet 统一隔离：

1. `kd-setup/agent.md:98`：`KD_ARTIFACTS_DIR="${PROJECT_ROOT}/artifacts/"` → `"${PROJECT_ROOT}/artifacts/kd-nas/"`
   （下游 CHECKPOINTS_DIR / STUDENT_MODELS_DIR / SCRIPTS_DIR / ONNX_DIR / META_DIR / REPORTS_DIR / WORKTREE_ROOT /
   LEDGER_PATH / CHAMPIONS_PATH 全由 `KD_ARTIFACTS_DIR` 派生，自动平移，不改相对结构）。
2. `kd-setup/agent.md:102-112`：**删除** migrate_flat.py 调用块（不再拍平；保留会自我误触发——`_validate_layout`
   期望 `kd_old = flat_new/kd-nas`，而改后两者同目录）。
3. **删除** `workflows/agents/_kd_scripts/migrate_flat.py` + `tests/workflows/test_migrate_flat.py`（方向反转，
   保留即错；sentinel/幂等逻辑无意义）。**既有 flat 布局 kd-nas durable 产物不自动迁移**（保持"小改"）：
   现 `<project>/artifacts/{ledger.jsonl,champions.jsonl,models/,checkpoints/,...}` 需用户手动
   `mv ... <project>/artifacts/kd-nas/`，或接受新布局首次 kd-nas run 从零开始（re-train）。开发期 dev/demo
   artifacts（`kd-nas-demo-artifacts/` 等）可弃，重跑即可。
4. `model-flatten`（`workflows/agents/model-flatten/agent.md:59,71-76,82,199` + `SKILL.md:31-36`）：独立派生的
   `flat_artifacts_dir` 从 `${PROJECT_ROOT}/artifacts/models/baseline/` → `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/`
   （model-flatten 是 kd-nas 专属 flatten agent，硬编码 `kd-nas` 可接受；它跑在 setup 之前，不能读 setup 输出）。
5. `workflows/kd-nas.yaml:73,87,122`：描述同步（`kd_artifacts_dir` → `<project>/artifacts/kd-nas/`；
   `flat_artifacts_dir` → `<project>/artifacts/kd-nas/models/baseline/`）。
6. `tests/workflows/test_model_flatten.py:631-660`：反转 6 条 pin 断言（`artifacts/models/baseline/` →
   `artifacts/kd-nas/models/baseline/`；`artifacts/kd-nas/ NOT in` → 反向）。

**kd-nas 的 `$ORCA_ARTIFACTS_DIR` 不变**（无 `project_root` input → §2.1 回落 per-run）；其 per-run 产物
（teacher wrapper .py / 日志）继续走 per-run，durable 产物走 `kd_artifacts_dir`（project-scoped）。`PER_RUN_ARTIFACTS_DIR`
别名（kd-setup:79）是 kd-nas 专属、仍指 per-run `$ORCA_ARTIFACTS_DIR`，名字不撒谎，**不改**。

### 2.4 软跳过机制（两 workflow 通用协议）

每个**有确定性、可复用产物**的昂贵节点，在其 agent.md 工作流开头加 **Step 0: reuse-check**：

1. 去产物目录查本节点权威产物是否存在。
2. 存在则**验证达标**（import / smoke / schema / 可 load，**非**盲目跳过）。
3. 达标 → 按既有 output_schema 回显（**emit 与成功执行相同的 status / 字段值**），assessment 前缀
   `reused existing <产物>`，不重做。
4. 不存在 / 不达标 → 照常执行本节点。

**status 枚举语义（不动 output_schema、不加新字段）**：reused 结果 emit 该节点**成功路径的同一 status**
（nas-supernet：`ns_run_train`/`ns_run_search`/`ns_retrain` → `executed`；生成节点填同字段）。理由：
路由守卫读 status 真值（如 `ns_retrain.output.status == 'executed'`），reused 必须命中成功分支才不误路由
terminate。`ns_run_train` 的 `skipped` 枚举保留给"viability self-gate（脚本不存在）"，**不**用于 reused
（语义不同：skipped=上游不可行，reused=产物已存在）。复用可观测性靠 assessment 文本前缀
`reused existing ...` + **artifact mtime 早于本次 run 起点**（机械可检，防 LLM 谎报 reused）。

不适用：每轮迭代节点（kd-nas 的 gen_student / distill / decide）；廉价节点（ns_select / ns_visualize）。

各 workflow 软跳过节点：
- **nas-supernet**：`ns_expand_supernet`（supernet.py + supernet_summary.md + project_manifest.md）；
  `ns_train_script`（train_supernet.py + run_train_supernet.sh）；`ns_run_train`（supernet ckpt）；
  `ns_search_pipeline`（select_architecture.py + search_config.yaml + evaluator.py + arch_codec.py）；
  `ns_run_search`（search_results.jsonl ≥1 行 + 合法 JSON）；`ns_retrain`（final retrain ckpt）。
- **kd-nas**：`model-flatten`（flat 契约）；`train-teacher`（teacher ckpt）；`gen-train-script`（4 叶子 +
  run_config）。`kd-setup` 已自带幂等（champions/ledger 仅首次），不额外加 Step 0。

### 2.5 禁碰清单 carve-out（nas-supernet）

产物目录现在落在 `{{ inputs.project_root }}/artifacts/` 下。nas-supernet 各 agent + 子 agent 现有禁碰规则
"禁 edit/write `{{ inputs.user_project_root }}` 下任何文件"改为：

> `{{ inputs.project_root }}` 下**源文件**只读禁改；**例外**：`{{ inputs.project_root }}/artifacts/` 是本
> workflow 产物目录，可写。

覆盖：nas-supernet 8 agent + 5 子 agent（project-fidelity-verifier / project-porter / memory-verifier /
workflow-verifier / supernet-evaluator）。kd-nas agent 用 `kd_artifacts_dir`/`project_root` 派生路径，其
禁碰规则对源文件保留（kd-nas 的 project_root 是 setup 推断、artifacts 已是独立子树，无 carve-out 需要）。

## 3. 改动清单

### 3.1 引擎面（仅 in-session，单文件）

`orca/iface/in_session/cli.py`：
- 新增 `_resolve_artifacts_dir(inputs, wf_name, run_id, runs_dir)` helper（§2.1）。
- bootstrap 站点 + `_derive_artifacts_dir` 改调它（传 in-scope `inp` + `wf_name`）。
- project-scoped mkdir 失败 → fail loud（区别 per-run fail-open）。
- docstring/注释同步（project_root input 存在 → project-scoped；否则 per-run 回落）。

**不改**：`orca/exec/*`、`orca/chart/_paths.py`、`orca/run/*`（`tars run` 路径 per-run 不变）。

### 3.2 nas-supernet

- `nas-supernet.yaml`：input 改名 + 描述 + `$ORCA_ARTIFACTS_DIR` 语义注释。
- `ns_*.md`（8）+ nas-supernet 子 agent（5）：input 改名（§2.2）+ 禁碰 carve-out（§2.5）+ 昂贵节点 Step 0（§2.4）。

### 3.3 kd-nas

- `kd-setup/agent.md`：`KD_ARTIFACTS_DIR` 加 `kd-nas` 子目录；删 migrate_flat 调用块。
- 删 `migrate_flat.py` + `test_migrate_flat.py`。
- `model-flatten/agent.md` + `SKILL.md`：flat 路径加 `kd-nas` 子目录。
- `kd-nas.yaml`：描述同步。
- `test_model_flatten.py`：反转 6 条 pin 断言。
- kd-nas 昂贵节点（model-flatten / train-teacher / gen-train-script）加 Step 0 软跳过。

## 4. 验收

- `tars validate nas-supernet` + `tars validate kd-nas`：0 error / 0 warning。
- grep `user_project_root` 在 `workflows/agents/ns_*.md` + `workflows/subagents/nas-supernet/*.md` +
  `nas-supernet.yaml`：零命中（kd-nas 子 agent 排除）。
- in-session 单测（新增 `tests/iface/in_session/test_resolve_artifacts_dir.py`）：`project_root` 绝对路径 +
  wf_name → `<proj>/artifacts/<wf>/`；相对路径 → raise；无 project_root → per-run 回落。
- 删除的测试不再被 collect（`test_migrate_flat.py`）；`test_model_flatten.py` 6 条断言反转后过。
- kd-nas setup E2E smoke：新布局下 `kd-setup` 不崩（`KD_ARTIFACTS_DIR=.../artifacts/kd-nas/` mkdir + 下游
  派生路径自洽，无 migrate_flat 自毁）；flatten 与 setup 的 `baseline_dir` 同根（`artifacts/kd-nas/models/baseline/`）。
- 复用可观测性：二次 run 后，被软跳过节点的权威产物 mtime 早于第二次 run 起点（机械可检）。

## 5. 非目标（scope 边界）

- **不改 executor / `tars run`**（用户明确）：`tars run nas-supernet` 仍落 per-run `runs/<run_id>/artifacts/`，
  无跨 run 复用——复用仅 in-session。文档化此不对称。
- **并发写竞争**：同 CWD 同项目同 workflow 的并发 bootstrap 由既有 dupe-check（`cli.py` `_find_active_run_for_wf`）
  串行化（安全）；**跨 CWD / 跨机共享同一 `project_root`（如 NFS）无锁**，可能互相 clobber 产物——记为
  out-of-scope（单机单 CWD 串行是预期使用方式）。
- **GC 不清理 project-scoped 产物**（`_delete_candidate` 拒绝 rundir 外路径）：产物跨 run 持久是预期，
  由用户管理；gc 仅清 per-run `runs/<run_id>/`。
- 不引入核心 schema 新字段（`project_root` 是普通 workflow input；node output_schema 不加新字段）。
- 不改 kd-nas 的 setup 派生体系结构（仅落点加 wf_name 子目录 + 撤销拍平）。
- 不含 schema 不匹配 / 超网张开方向 / 训练提前退出等其它议题（另案）。
