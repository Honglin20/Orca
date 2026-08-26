# 2026-08-12 · nas-supernet-v3 retrain 拆分：ns3_retrain_script + ns3_retrain

> commit SHA 待回填（已 commit 为 `bd05ce9`，用户 commit U3 时统一回填三处文档）。

## 背景

`ns3_retrain`（v3）把两个正交职责揉进一个 agent：

- **职责 A：生成 retrain 脚本**（一次性，本该像 `ns3_train_script` 那样有完整校验闭环）
- **职责 B：驱动长任务执行到完成**（轮询 / HEAL-LOOP / 跨 turn 续接）

后果：B 的复杂执行模型（~460 行大半是轮询/自愈）淹没了 A 的生成校验，导致生成期校验明显弱于 `ns3_train_script` 树立的标准——**缺独立 workflow.md + checklist、缺 workflow-verifier 闭环、fidelity 只单次复查（不循环到 all-pass）、无持久化 self-check test**。同时 retrain 策略绑死上游 `evaluation_paradigm` 三分支，语义模糊。

## 决策（用户拍板）

1. **拆分**：`ns3_run_search → ns3_retrain_script（新，纯生成）→ ns3_retrain（瘦身，执行）→ ns3_report`
2. **策略定死二元**：`retrain_strategy` = `finetune-from-supernet`（supernet viability=Yes **且** ckpt 文件存在）/ `train-from-scratch`（fallback）。纯确定性判定（读 `supernet_summary.md` + 检查 ckpt），**不绑 `evaluation_paradigm`**，不需 LLM 判断。
3. **执行期 self-heal 收窄**：slimmed `ns3_retrain` 只允许 edit `run_retrain.sh`（launcher 参数/路径/import typo）；触碰 `retrain.py`/`finetune.py` 训练逻辑 → **fail loud** 回 `ns3_retrain_script` 重新生成，保护生成期 fidelity 闭环。
4. **策略字段**：新节点 output 用 `retrain_strategy` 枚举 + `strategy_reason`（不用 `viable`）。
5. **仅改 v3（ns3_*）**，v1/v2 不动。

## 改动

### 新节点 `ns3_retrain_script`（纯生成，镜像 `ns3_train_script`）

- `agent.md`：User-Paradigm Iron Rule / Path Handling / Sub-agent Protocol / Step 0 reuse-check / Step 1 load context / Step 2 策略判定 + 生成（porter + fidelity audit loop **循环到 all-pass** + workflow compliance loop **循环到 all-pass**）/ user-paradigm self-check / hard gate / 10 字段 output。
- `scripts/check_retrain_script.sh`（重构自旧 `check_retrain.sh`：拆 launcher 委托、多目标 OR 语义、`retrain.py` 强制 `is_distributed()` guard、py_compile `retrain.py`+`finetune.py`）+ `scripts/check_launcher.sh`（参数化，entry-point = `python3.*retrain\.py`，补旧版缺的 entry 检查）。
- `references/workflows/retrain_script_generation.md`（镜像 `train_supernet_script_generation.md`，去 sandwich/KD/Model Construction，加 Subnet Extraction + 两分支策略 spine）。
- `references/workflow-checklists/retrain_script_generation.md`（34 项 CRITICAL/MAJOR，含 strategy coherence / final-ckpt contract path / budget rule 等 retrain 特有项）。

### 瘦身 `ns3_retrain`（执行 only）

- 移除全部生成期内容（Step 3a 生成 / 3b fidelity / 3g 重触；铁律 1/4 训练逻辑层/5/9；`check_retrain.sh` 调用；生成期 marker 语义）。
- 新增 **Step 0 前置自门控**：`retrain.py`+`run_retrain.sh` 缺失 → 直奔 Step 4 `failed`。
- **HEAL-LOOP 收窄**：只补丁层（`run_retrain.sh`），训练逻辑 → fail loud。
- marker 重定义：`.ns_retrain_exec_healed.txt`（执行期补丁痕迹）；`fidelity_retriggered` 恒 `false`（执行节点不重触 fidelity）。

### yaml 路由（`nas-supernet-v3.yaml`）

- 新增 `ns3_retrain_script` 节点（无 `status` 字段 / 无条件单出边，纯生成节点签名，镜像 `ns3_train_script`）；`output_schema` 10 字段含 `retrain_strategy` enum。
- `ns3_run_search` 守卫边目标 `ns3_retrain` → **`ns3_retrain_script`**（守卫不变：`selected_arch and pareto_size > 0`）。
- 新增 `ns3_retrain_script` → `ns3_retrain` 无条件单边。
- `ns3_retrain` 的 `output_schema`（6 执行字段）+ `routes`（两条到 ns3_report）不变。

### `agents_template.md`（v3 副本）Final Weight Acquisition 三 paradigm → 两 branch

- intro dispatch：`{{EVALUATION_PARADIGM}}` 绑定 → supernet 可用性 dispatch（保留 `{{EVALUATION_PARADIGM}}` 作信息记录，不破坏 `ns3_search_pipeline` Step 3 的 placeholder 替换）。
- `validate` + `finetune` 两 paradigm → 合并 Branch (a) `finetune-from-supernet`；`train_from_scratch` → Branch (b) fallback。
- **Script Requirements / Launcher Skeleton / Validation 段对齐新节点契约**（code-reviewer 抓的架构漂移）：`subnet_best.pth`→`retrain_best.pth`、`retrain_pareto.py`→`retrain.py`、`--arch_file`→Jinja `SELECTED_ARCH` 字面量、`OUTPUT_DIR=runs/retrain/selected`→`runs/retrain`、`NUM_WORKERS=4`→`0`、`AMP=true`→`false`。其中 ckpt 名/output dir/`--arch_file` 三项**无硬门兜底**（silent contract break 风险：生成器写 `subnet_best.pth` → 下游 `emit_result.py` 永远找不到 ckpt），本次收口。

### scripts / markers

- `emit_result.py`：self-gate `AGENTS.md` 存在 → `retrain.py`+`run_retrain.sh` 存在且非空（`exists`+`getsize>0`，与 Step 0 的 `[ ! -s ]` 语义统一）；healed marker `.ns_retrain_healed.txt` → `.ns_retrain_exec_healed.txt`；`fidelity_retriggered` 恒 `False`。
- `launch.sh`：marker 清理行适配新名；清理 v3 弃用 marker（`.ns_retrain_generated.txt`/`.ns_retrain_fidelity.flag`）的误导注释。
- 删 `ns3_retrain/scripts/check_retrain.sh`（重构迁移到 `ns3_retrain_script/`）。

## 验证

- `tars validate workflows/nas-supernet-v3.yaml` ✓（schema 合法 + prompt 洁净零 warning）。
- `pytest tests/workflows/` = **687 passed, 4 skipped**（skip 均为预存 obsolete/real-artifacts-absent，与本次无关），无回归。
- `tests/workflows/test_check_retrain_script.py` = **18 passed**（含 `test_unguarded_ddp_fails` / `test_unguarded_sync_random_seed_fails` 两个负例，覆盖之前无负例触发的 DDP/sync_random_seed 守卫分支）。
- **code-reviewer 闭环**：职责拆分干净彻底（生成节点双 verifier 闭环到 all-pass + 执行节点 iron rules 一致把训练逻辑踢回上游 fail loud，无残留"在 ns3_retrain 改 retrain.py"路径）；v1/v2 零改动（git diff 核验）；策略真二元；跨节点 ckpt/progress 契约路径四处文件字面一致；洁净契约过。发现 1 个 🟡 架构漂移（agents_template 旧契约名）+ 4 个 🟢（emit_result 谓词 / launch.sh 注释 / test 负例 / yaml 字段描述）**全修**。

## 仅 v3

v1（`ns_*`）/ v2（`ns2_*`）**未触动**（code-reviewer `git diff --name-only HEAD` 核验零 v1/v2 文件改动）。v1 的 `agents_template.md` 仍保留旧三范式设计直到单独迁移。v3 的 `agents_template.md` 当前与 v1 字节相同（改 v3 不影响 v1/v2，二者独立文件）。

## 相关文件

- 新：`workflows/agents/ns3_retrain_script/`（agent.md + 2 scripts + 2 references）
- 改：`workflows/nas-supernet-v3.yaml`、`workflows/agents/ns3_search_pipeline/assets/agents_template.md`、`workflows/agents/ns3_retrain/agent.md`、`workflows/agents/ns3_retrain/scripts/{emit_result.py,launch.sh}`、`tests/workflows/test_check_retrain_script.py`
- 删：`workflows/agents/ns3_retrain/scripts/check_retrain.sh`
