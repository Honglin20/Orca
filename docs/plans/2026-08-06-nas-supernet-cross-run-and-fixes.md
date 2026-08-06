# 计划：nas-supernet 跨 run 复用 + 4 项修复（2026-08-06）

## 背景 / 诊断结论

用户跑 nas-supernet workflow 发现 4 个问题。诊断（含两路并行 agent + 真实 run `runs/nas-supernet-20260804-230305-b0c17d/` 实证）：

1. **产物路径 per-run**：`$ORCA_ARTIFACTS_DIR = <runs_dir>/<run_id>/artifacts/`（`orca/chart/_paths.py:54` + `orca/exec/env.py:104`），per-run by design。用户要稳定路径 + 跨 run 复用。
2. **超网只向下张开**：前提与证据冲突——NAS-AGENT 与 Orca 的 spec `references/`+`assets/` **逐字节相同**（递归 diff 仅 `SKILL.md` vs `agent.md` 差），方向是 spec 软散文约束（"central value / expand nearby"）无 validator → LLM 漂移。真实 MNIST run 反而**向上**张开（baseline 极小）。用户真实项目（baseline 大）漂移成 baseline-as-max（向下）。
3. **training-agent 提前退出**：`ns_run_train` detach+poll `wait $TRAIN_PID` + 检 ckpt。根因：(A) 生成的 `run_train_supernet.sh` 若 background 训练进程不 wait，bash 启动器提前退出 → wait 拿启动器 PID；(B) `kill -0` 只测存活不测进度。
4. **schema 不匹配**（acc/latency vs objs.acc/objs.latency）+ 接口缓冲：`nas_agent/search/logger.py:111-122` 写固定 6 键 record `{generation,gene,objs:{...},cached,pareto,arch}`，objective 嵌套 `objs` 下。`ns_search_pipeline` Step 2b spec（`agent.md:291-294,339`）只模糊描述未钉死 → LLM flaky（本次 run 猜对，用户撞到猜错）。`search_log_path` 同源 flaky。

## 用户决策

- **Artifacts**：稳定路径 = **`{{ inputs.user_project_root }}/artifacts`**（`user_project_root` 已是 required input，直接派生，**零引擎改动**——无视引擎注入的 per-run `$ORCA_ARTIFACTS_DIR`）。agent **智能软跳过**（自查目标产出存在且达标则跳过，**不加硬门控**）。
- **方向**：硬化 spec + validator。
- **训练**：A（启动器 exec/wait 契约）+ B（warmup 前 2 epoch 监控）都做。

> **为何不动架构**：`$ORCA_ARTIFACTS_DIR` 是 executor 从 run_id 派生注入的 per-run 变量
> （`artifacts_dir_for_run(runs_dir, run_id)`），agent 改不到它的值。但 agent **不必用它**——
> `user_project_root` 是 workflow input，所有节点经 `{{ inputs.user_project_root }}` 可得（Jinja
> render 期 inline 绝对路径）。故本 workflow 自定产物目录 = `user_project_root/artifacts/`，
> 全 agent 用此路径，引擎注入的 `$ORCA_ARTIFACTS_DIR` 弃用（每 run 仍建空 `runs/<run_id>/artifacts/`，
> 无害；引擎无任何处读 nas-supernet 的产物内容，故弃用安全）。

---

## 改动 1：跨 run 复用（统一产物目录 = `user_project_root/artifacts` + agent 软跳过，零引擎改动）

### 1a. 统一产物目录约定（workflow + 全 agent）

产物目录定死 = `{{ inputs.user_project_root }}/artifacts/`（跨 run 稳定，同项目复用）。

- `nas-supernet.yaml`：`user_project_root` input description 增补"本 workflow 产物目录 = `user_project_root/artifacts/`（跨 run 稳定复用）"；yaml 内所有 `$ORCA_ARTIFACTS_DIR` 描述/注释改指 `{{ inputs.user_project_root }}/artifacts`。
- 全 8 agent.md：`$ORCA_ARTIFACTS_DIR` 引用 → `{{ inputs.user_project_root }}/artifacts`（bash 用变量 `ARTIFACTS_DIR="{{ inputs.user_project_root }}/artifacts"` 统一；python 块 inline Jinja 字面量）。`cd "$ORCA_ARTIFACTS_DIR"` → `cd "$ARTIFACTS_DIR"`；marker 文件 / 日志 / ckpt 路径全切。
- `mkdir -p "$ARTIFACTS_DIR"` 由首节点（ns_expand_supernet）保证；下游节点假定存在（或各自 `mkdir -p` 兜底）。

### 1b. 禁碰清单 carve-out（关键一致性修正）

当前所有 agent 硬规则"禁 edit/write `{{ inputs.user_project_root }}` 下任何文件"与产物目录搬进 `user_project_root/artifacts/` 冲突。改为：

> 禁碰清单：`{{ inputs.user_project_root }}` 下**源文件**只读禁改；**例外**：`{{ inputs.user_project_root }}/artifacts/` 是本 workflow 产物目录，可写。

全 8 agent.md + 子 agent（project-fidelity-verifier / project-porter 等）的禁碰清单同步改。

### 1c. agent 智能软跳过（Step 0 reuse-check，不加硬门控）

昂贵生成/执行节点 Step 1 前加 **Step 0: reuse-check**：自查本节点权威产物在 `user_project_root/artifacts/` 是否已存在 → 存在则**验证达标**（非盲目跳过）→ 达标 emit 既有 output_schema（assessment 标 "reused existing <artifact>"）跳过；不达标照常重做。

- `ns_expand_supernet`：查 `supernet.py` + `supernet_summary.md` + `project_manifest.md` → 验 `python -c "import supernet"` + SearchSpace 结构非空 + summary model_type_supported。
- `ns_train_script`：查 `train_supernet.py` + `run_train_supernet.sh` → `bash -n` + py_compile + viable 字段。
- `ns_run_train`：既有自门控扩展——`run_train_supernet.sh` 存在**且** ckpt 存在**且** `torch.load` 不抛 → `status=executed` reused；ckpt 缺才跑。
- `ns_search_pipeline`：查 `select_architecture.py` + `search_config.yaml` + `evaluator.py` + `arch_codec.py` → py_compile + 4b schema 契约（见改动 4）。
- `ns_run_search`：查 `search_results.jsonl` ≥1 行 + 合法 JSON + objs 嵌套 → `status=executed` reused。
- `ns_retrain`：final ckpt 存在 + 可 load → reused。
- `ns_select` / `ns_visualize`：廉价，**不**加跳过。

**软约束语义**：prompt 写「验证达标才跳过」——LLM 判断而非 `if exists` 硬门控。output_schema 不变（reused 也填同样字段），tape 可追溯（assessment 标 reused）。

### 1d. e2e 测试同步

`tests/e2e_nas_supernet/` 若断言产物在 `$ORCA_ARTIFACTS_DIR` → 改断言 `user_project_root/artifacts/`。e2e fixture 的 `user_project_root` 须可写隔离。

---

## 改动 2：超网双向张开（硬化 spec + validator）

`workflows/agents/ns_expand_supernet/references/supernet_specs/`（与 NAS-AGENT 同源，改 Orca 副本）：

- `cnn/spec.md` (~131-137)、`isotropic_transformer/spec.md` (~65,68-71)、`hierarchical_transformer/spec.md` (~127,129-133)：把 "central value / expand nearby when sensible" 软约束升级为 **MUST**：每个可搜维度的候选元组必须**同时含 ≥1 个 > baseline 与 ≥1 个 < baseline** 的候选（baseline = 用户模型原值，作中点）；给 `[min, base, max]` 字面量示例（如 baseline depth=4 → `(2,4,6)`；baseline channels=64 → `(48,64,96)`）。结构约束例外明示（如 CNN 首块 stride-2 强制 min depth=1，无向下空间时记例外）。
- `isotropic_transformer/search_space.py:67`：占位 `num_heads: (16,8,4)` 改对称双向 `(4,8,16)` + 注释 baseline=8 居中（纠 few-shot 反向示范）。
- `workflows/agents/ns_expand_supernet/references/workflows/search_space_refinement.md` + 对应 `workflow-checklists/search_space_refinement.md`：加 checklist item「每个可搜维度候选元组 max > baseline 且 baseline ≠ 该元组最大值（双向张开）」→ workflow-verifier Step 6 自动 fail 不合规空间。

---

## 改动 3：training-agent 提前退出（A 契约 + B 监控）

### 3a. `ns_train_script/agent.md`：钉死启动器 exec/wait 契约

生成 `run_train_supernet.sh` 的 spec 加铁律：训练进程必须**前台**跑（`exec python/torchrun ...` 或 launcher 末尾 `wait` 训练 PID），**禁** background 训练进程不 wait。理由：`ns_run_train` 的 `wait $TRAIN_PID` 必须追踪到真实训练进程退出，不是 launcher。

### 3b. `ns_run_train/agent.md`：warmup health-check（前 2 epoch）

Step 2 detach 后、进入纯 `kill -0` 存活轮询前，加 **warmup 阶段**：轮询 `runs/train/train.attempt${N}.log` 等前 2 个 epoch 标记出现（regex 匹配 epoch/loss 行）+ loss 有限（非 NaN/inf）→ 标记「training confirmed running」转正常 wait。超时（可配，默认如 20 min）无 epoch 标记 → 判 stalled/假死，kill 进程 + self-heal（读 log 尾部定位）。warmup 不改成功闸门（仍是 RC=0 + ckpt 存在），只防假死早退。

---

## 改动 4：schema 契约钉死（单文件）

`workflows/agents/ns_search_pipeline/agent.md` Step 2b（~287-348）：

- line 291-294 后插 record schema 字面量：
  ```
  {"generation": <int>, "gene": [<int>...], "objs": {<name>: <float>...},
   "cached": <bool>, "pareto": <bool>, "arch": {<ArchConfig dict>}}
  ```
  + 明示「objective 一律读 `record["objs"]["<name>"]`，arch 读 `record["arch"]`，**禁**读顶层 `record["acc"]`/`record["latency"]`（不存在）；objs 键名 = `search_config.yaml` `objs` 列表项；所有 objective smaller-is-better（higher-better 已取负）」。
- line 339 改写为「6 固定顶层键 `generation/gene/objs/cached/pareto/arch`」。
- 加权威源引用：`nas_agent/search/logger.py:log_generation`（写）+ `nas_agent/cli/select_architecture.py:load_pareto_jsonl`（读 + 严格 key 校验）。
- `search_log_path` 契约钉死：必须解析为 `$ORCA_ARTIFACTS_DIR/search_results.jsonl`（即 `search_log_path` 设成相对 cwd 的 `./search_results.log`，`.with_suffix(".jsonl")` 落权威名）。

---

## 实施顺序（按风险隔离，全部零引擎改动）

1. **改动 4**（schema，单文件 spec）→ `tars validate`。
2. **改动 2**（方向 spec + validator，references 多文件）→ `tars validate`。
3. **改动 3**（训练契约 + 监控，2 agent.md）→ `tars validate`。
4. **改动 1a/1b**（统一产物目录 + 禁碰清单 carve-out，8 agent.md + yaml + 子 agent）→ `tars validate`。
5. **改动 1c**（agent 软跳过，6 agent.md Step 0）→ `tars validate`。
6. **改动 1d**（e2e 断言同步）。

每步后 code-reviewer 自检（依赖铁律 / fail loud / DRY）。

## 验收

- `tars validate nas-supernet` 0 error / 0 warning。
- 全 agent 无残留 `$ORCA_ARTIFACTS_DIR` 引用（grep 零命中）。
- 真机（用户后续）：用真实项目跑 nas-supernet → 二次 run 验昂贵节点软跳过（产物落 `user_project_root/artifacts/`）+ SearchSpace 双向 + select 读 objs + 训练 warmup。

## 不做

- **不动引擎**（orchestrator / executor / factory / chart._paths 零改动）——`$ORCA_ARTIFACTS_DIR` per-run 注入保留，本 workflow 弃用之。
- 不改 NAS-AGENT（parent projects 目录，只读参考）。
- 不加硬 skip 门控（用户明确要智能软跳过）。
- 不恢复 NAS-AGENT 的 optional optimize rules A/B 审批（与方向无关，徒增负担）。
