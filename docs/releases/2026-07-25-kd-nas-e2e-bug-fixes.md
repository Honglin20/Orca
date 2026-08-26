# Release Note —— kd-nas workflow E2E 暴露的真 bug + reviewer findings 修复

> 日期：2026-07-25。分支：`in-session-unified-backend`。
> 前置：[KD-NAS v2 setup→gate→train DAG](./2026-07-25-kd-nas-gate-train-dag.md) + [kd-nas-demo E2E 靶子](./2026-07-25-kd-nas-demo.md)。

## 做了什么

E2E 真跑 `tars run workflows/kd-nas.yaml --background`（demo inputs，9m40s 完成、4/4 变体 SUCCESS）
暴露了若干真 bug + reviewer 6 项 finding。本次全部修复 + 守门测试。

### 🔴 BUG-2：`tars run --background` 用错 binary

**症状**：`bg_runner.build_child_argv` 用 `shutil.which("orca")` 拿到的是 in-session CLI
（无 `run` 子命令）→ 子进程 `run` 被当 wf-name → bootstrap → `No such option: -i` 崩。

**修复**：改用 `shutil.which("tars")`（workflow 启动 CLI，有 `run` 子命令）。`commands.py`
user-visible help text 同步把 `orca ps/logs/wait` 改 `tars ps/logs/wait`（同 bug class）。

**文件**：
- `orca/iface/cli/bg_runner.py`（docstring + `build_child_argv`）
- `orca/iface/cli/commands.py`（`--background` help + `run` docstring）

### 🔴 BUG-1：agent.md 被 deepseek-v4-flash 当 spec 审查，不执行

**症状**：deepseek-v4-flash 读 kd-setup/agent.md 后**跑 pytest/validate 写 markdown 验证报告**，
没执行 bash、没 emit JSON → schema 校验崩。

**根因**：agent.md 写得太像 spec（「职责/契约/fail loud」段在前），模型默认「审查」而非「执行」。

**修复**（重写 kd-setup / kd-gate / kd-train 三个 agent.md）：
1. 开头加 `## ⚠️ 你的唯一职责` 段：明确「唯一产出 = JSON 对象」+ 产出步骤 + **❌ 红线列表**
   （严禁审查/评判指令、跑 pytest、跑 validate、写验证报告、解释代码）+ 「失败 = fail loud」契约。
2. JSON schema 段前置（offset < 第一个 bash 块）——让模型一开始就知道终点是 JSON。
3. bash 块明确标 `执行：` + 「整段原样照抄为一条 bash 调用（不要拆开、不要改参数、不要加 echo 调试）」。
4. 保留确定性逻辑在脚本（rule 5），agent 只调脚本——不变。

**E2E 验证**：4/4 变体 SUCCESS；agent 真执行 bash + emit 合法 JSON（不再写验证报告）；
step2 偶发 nested-quote copy 错时**自纠正**切 heredoc，说明 prompt 鲁棒性也提升。

### 🟡 BUG-3：`train_pool` `VARIANTS_TOTAL: 0`

**症状**：train_pool 末尾 `os.listdir($ORCA_KB_DIR/families/receiver)`，但 ORCA_KB_DIR 在
in-session `orca next` 链里被重置为默认 `~/.orca/knowledge_base`（不存在）→ glob 0。

**修复**（闭环到 setup + gate + train 三节点，跨节点契约一致）：
1. setup output_schema 加 `receiver_dir`，kd-setup/agent.md step1 探测绝对路径并 emit。
2. kd-gate / kd-train agent.md `--receiver_dir` 从 `setup.output.receiver_dir` 取（不再依赖 env）。
3. train_pool 三级 fallback：`--receiver_dir` → `$ORCA_KB_DIR/families/receiver` →
   `len(ledger) + n_accepted`（仅诊断字段，stderr WARN 不静默 0）。

### 🟡 R4：setup teacher_ckpt + user_train 留给 LLM

**症状**：kd-setup step5 teacher_ckpt 路径 + step6 user_train_import/loss 靠 LLM grep，
违反 rule 5（确定性逻辑用代码）。

**修复**：新增 `workflows/agents/_kd_scripts/setup_helpers.py`（确定性后端，CLI 子命令）：
- `find-teacher-ckpt`：解析 `teacher_train_command` 的 `--out`（无歧义首选）；失败扫 project_root
  最新 `.pt/.ckpt/.pth`（排除 kd-nas-artifacts/ckpts/.venv 等），拷到 `$TEACHER_CKPT`。
- `grep-user-train`：`_find_train_py` + AST 解析找 loss callable（`def compute_loss` / `def <name>` 含 loss），
  抽到 emit `USER_TRAIN_IMPORT` + `USER_LOSS_FN`；抽不到 emit ask-user 哨兵（不编造）。
- **关键**：`_walk_with_prune` 用 `os.walk` + 硬剪枝 `.venv/site-packages/.git/node_modules/llm_artifacts`
  等不可能含用户 train.py 的目录。实测 Orca repo 33k 文件下 `Path.rglob` 卡 >30s（WSL2 stat 慢），
  剪枝后 <2s。

### 🟡 R1：`gpu_probe` NPU VRAM 探测失败沉默估算

**症状**：`gpu_probe.py` `_probe_per_variant_vram` 在某些 NPU 后端 `max_memory_allocated` 返 0，
原实现 `per_variant = total_free // 4` 沉默估算驱动并发（破坏 fail loud；昇腾部署 relevant）。

**修复**：探测返 0 时走 cpu 同款 fail-soft：`PER_VARIANT_VRAM_BYTES: 0` + `CONCURRENCY: 1` +
`GPU_REPORT` 标 `[probe failed]` + stderr WARN 「不估算驱动并发」。

### 🟡 R2：`viz_kd` rc!=0 沉默

**症状**：`train_pool.py` 末尾 `subprocess.run(viz_argv, check=False)` 吞 viz 失败。

**修复**：rc!=0 时 `print(f"[train_pool] WARN: viz_kd rc={rc}（不阻断）：{stderr[-300:]}")`。

### 🟡 R3：`gate_all` 空 KB 沉默 SUCCESS

**症状**：`gate_all.py` 收到空 receiver_dir → 静默 `N_ACCEPTED: 0` → workflow 路由 `$end`
跳过 train。用户 99% 是 ORCA_KB_DIR 指错 / families/receiver/ 无 .py，但表面看是「成功」。

**修复**：`len(all_variants)==0` 时 stderr WARN 含 receiver_dir + 提示原因。

### reviewer 测试缺口（补齐）

新增 24 个单测（`tests/workflows/test_kd_redesign.py` + `tests/iface/cli/test_bg_runner.py`）：
- R1：NPU `per_variant=0` fail-soft mock 测试（不需要真 NPU 硬件）。
- R2：viz_kd rc!=0 WARN mock 测试。
- R3：空 KB WARN 断言。
- BUG-1：agent.md 结构不变量（schema 前置 offset < bash 块 / `❌` 红线 / `执行：` count ≥1）。
- BUG-2：3 个测试（`shutil.which("tars")` 命中 / `orca` 命中但 fallback / `tars` 缺失 fallback）。
- BUG-3：gate + train 两个 agent.md 都断言用 `setup.output.receiver_dir`（不依赖 env）。
- R4：setup_helpers 7 个测试（`--out` 解析 / 扫描 / fail loud / AST 抽 loss / sentinel / 剪枝 venv / 死代码已删）。
- `gate_all`：tune rc!=0 / dispatch rc!=0 / 变体 import exception 三条 FAIL_train 路径。
- `train_pool._train_one`：SUCCESS 字段齐 / BLK-11 ckpt 缺失 / measure rc!=0 / train rc!=0 /
  measure rc=0+met_acc=false 五条路径。
- reviewer found 测试 bug：`rows[0]["fail_reason"]()` 改 `rows[0]["fail_reason"]`（去 `()`）。

## 验证

- ✅ `tars validate workflows/kd-nas.yaml`：0 error
- ✅ `pytest tests/workflows/test_kd_redesign.py tests/workflows/test_struct_kd_p7.py tests/iface/cli/test_bg_runner.py`：
  **139 passed**（新增 24）
- ✅ **BUG-1 判据（最关键）**：`tars run workflows/kd-nas.yaml --background` 真驱动 setup→gate→train
  三节点，4/4 变体 SUCCESS（demo_tiny_alt / demo_tiny_cnn_dil / demo_tiny_cnn_pw / demo_tiny_tf，
  latency 0.35~1.11ms 全 < 5ms target，NMSE 1.07~1.20 < 1.5 baseline）。agent 真执行 bash
  + emit 合法 JSON，不再写验证报告。
- ✅ code-reviewer 双分发（代码质量 + 测试覆盖）全闭环。

## 关键决策

1. **agent.md 开头加 `## ⚠️ 你的唯一职责`**：deepseek-v4-flash 是小模型，spec-审查框架
   会触发「评判」倾向。强执行指令（唯一产出 = JSON + ❌ 红线 + 整段原样照抄）是必要的硬约束。
2. **`_walk_with_prune` 用 tuple yield 而非 Path**：WSL2 + Windows mount 上 `Path.rglob` 对
   Orca repo（33k 文件含 .venv）卡 >30s。`os.walk` + 硬剪枝 + 不构造 Path（避免 stat 调用）
   降到 <2s。caller 只在文件名匹配候选时才构造 Path。
3. **R1 fail-soft 不估算并发**：旧 `total_free // 4` 是沉默估算驱动并发，破坏 fail loud。
   NPU max_memory_allocated 返 0 时改 emit `PER_VARIANT_VRAM_BYTES: 0` + `CONCURRENCY: 1`
   （与 device=cpu 同款 fail-soft），不估算。
4. **BUG-3 三级 fallback**：`--receiver_dir` 是首选；env fallback 是兼容；ledger+manifest
   推断是诊断兜底（仅诊断字段，stderr WARN 不静默 0）。
5. **setup_helpers.py 不用 train_command 的 .py 当 student train.py**：semantic 细分——
   `teacher_train_command` 跑的是 `train_teacher.py`（teacher 训练），**不是** student train.py
   （KD 消费的 loss+dataloader）。train_command 仅作 project_root 定位线索。

## 偏差

无。

## Commit SHA

见 CHANGELOG 索引。
