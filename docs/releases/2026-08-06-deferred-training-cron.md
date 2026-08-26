# 2026-08-06 — ns_run_train deferred training via cron（原型）

## 做了什么

`nas-supernet` 的 `ns_run_train` 节点从「同节点内轮询到训练结束」改为「**deferred training via cron**」：
训练小时～天级，单 agent 节点无法 open 那么久。新机制：

1. **三分支 Step 0**（互斥）：
   - **reuse**：supernet ckpt 存在 + `torch.load` 可读 → `executed`（既有逻辑保留）。
   - **resume-pending**（新）：ckpt 缺**但** `runs/train/.train_pid` 活着 → 读 log 当前 epoch、重估
     剩余、重注册 cron（**禁重新 detach**）→ `detached`。
   - **fresh-launch**（新）：ckpt 缺 + 无训练在跑 → Step 2：detach + warmup + 估时 + cron + park →
     `detached`。

2. **Step 2 fresh-launch 五步**：2a detach（清旧 deferred markers，含 `.train_eta.txt` /
   `.cron_registered.flag`）→ 2b warmup 短轮询（前 1~2 epoch 标记 + loss 有限，~20min 上限）→
   2c 估时（python 解析 `--epochs` / `search_config.epochs` + log 时间戳测每 epoch 耗时）→
   2d cron 注册（`at now + T minutes` 优先；fallback crontab one-shot 带 marker 自清；写
   `.cron_registered.flag` 标记成功）→ 2e park（写 detached assessment）。warmup 失败 →
   既有 self-heal 白名单 + 重试 ≤3 次。

3. **Step 3 status 推导**（fail loud 关键）：
   `skipped`（脚本缺）< `executed`（ckpt 在，含 reuse marker 优先解析）< `detached`
   （`pid_alive AND .cron_registered.flag` 双条件）< `failed`。**双条件动机**：
   单看 `.train_eta.txt` 会在 fresh-launch self-heal 全败后误判 detached，掩盖 failed。

4. **YAML**：`ns_run_train.output_schema.status` enum 加 `detached`；`routes` 加
   `when status=='detached' → terminate_training_pending`；新 `terminate_training_pending`
   终态节点用 `status: success` + reason 文案明示「training deferred，cron 接力重跑」。

## 实现决策与已知限制

- **`tars nas-supernet` → `orca nas-supernet`**：SPEC §2.4 + §3.3 写 `tars nas-supernet --inputs ...`
  作 cron 重跑命令。但 CLAUDE.md 明示「TARS 是 SKILL，不是 CLI——不存在 `tars <wf> --inputs`」，
  驱动 workflow 走 `orca` CLI。本实现用 `orca nas-supernet --inputs ...`。SPEC 意图不变（重跑
  workflow），仅命令名修正。

- **TerminateNode.status 仅 `success` / `failed`**（schema `Literal["success", "failed"]`，无
  `pending`）。选 `status: success` + reason 文案明示 deferred 语义，避免被误判
  workflow 失败。**未**新增 `deferred` 枚举（schema 改动超出本次范围）。

- **`at` 路径无幂等清理**：crontab 路径有 marker 自清；`at` queue 无 comment marker，重注册
  （resume-pending）会留 stale entry。触发后两个 run 都跑，新 run Step 0a reuse 收敛（无副作用，
  仅浪费一次 cron 触发）。已知限制。

- **per_epoch 默认 60s**：log 无 epoch 标记时保守默认。早期 cron 偏早，靠 resume-pending 多轮
  重估收敛。

## 验证

- `tars validate workflows/nas-supernet.yaml`：**0 error / 0 warning**。
- 9 个 bash 块全过 `bash -n`；3 个 python heredoc 全过 `ast.parse`；dev-residue lint 0 命中。
- Step 3 status 推导 5 场景 smoke test 全过：scenario A（stale eta + dead pid + no flag → failed，
  验证 dual-signal 不掩盖失败）/ B（pid alive + flag → detached）/ C（pid alive 无 flag → failed，
  LLM 跳步防呆）/ D（flag 在 pid 死 → failed，cron 接力）/ E（ckpt marker 解析 → executed）。
- Step 2c 估时 smoke test：2 epoch log + timestamps → per_epoch=600s、total_epochs=10 解析正确。
- Step 2d cron 注册全流程 smoke test：T_MIN 提取、inputs JSON 校验、cron rerun 脚本生成 + 语法
  校验、FATAL 兜底路径清理 `.train_eta.txt`、`.cron_registered.flag` 不被误写。
- Jinja `{{ inputs | tojson }}` 经 Orca render 层验证：5 个 inputs 安全序列化（float/int/string
  类型保留、空串默认值正确）。

## self-review 加固明细

经一轮 self-review（含 must-fix / should-fix / optional 三档）闭环。

- **关键修复（must-fix）**：
  - `detached_signal = pid_alive OR eta_marker` 掩盖 failed → 改 2a 清 deferred markers +
    Step 3 用 `pid_alive AND cron_registered.flag` 双条件。
  - Step 2e `python3 -c '...f\"...\"...'` 是 Python 语法错（bash 单引号下 `\"` 字面到 python
    无效）→ 改 `python3 - <<'PY'` heredoc + 文件读 eta。
  - Step 0a（多名字扫描）vs Step 3（search_config 单路径）ckpt 解析漂移 → 0a 写
    `.ns_run_train_ckpt_resolved.txt` marker，Step 3 优先读此 marker。
- **边界加固（should-fix）**：cron PATH 注入、`at` 限制文档化、Step 2c/2a 加 `set -e`、
  cron_registered.flag 原子耦合、resume-pending 显式 LOG_PATH、修 `2>/dev/null 2>/dev/null`
  typo、T_MIN 提取后显式校验。
- **采纳与未采纳（optional）**：改用 `{{ inputs | tojson }}` 替代硬编码 5 字段（DRY）；未采纳——
  (a) Step 3 ckpt torch.load 校验（0a reuse 已校验 + 下游 ns_run_search 缺 ckpt fail loud 双兜底）、
  (b) yaml description 缩短（保完整语义）。per_epoch=60s 默认是设计选择（详见上文实现决策）。

## 不在范围（后续 task）

- 真机 E2E（SPEC §4 acceptance 1-5）：未跑——本 task 只交付原型 + 迁移 + tars validate + smoke test。
- `distill`（kd-nas 迭代节点）：评估后判定不迁移，详见下方「迁移」段。

## 迁移（2026-08-06 同日）

原型验证后，按 SPEC §5 把 deferred-cron 模式迁移到其它 training agent。

### ns_retrain（nas-supernet）

- `ns_retrain/agent.md`：Step 0 扩三分支（reuse / resume-pending / fresh-launch）+ Step 4 替换
  原「detach + 无上限轮询到 DONE」为 fresh-launch 五步（4a detach + 4b warmup + 4c 估时 + 4d cron +
  4e park + 4f self-heal）；Step 5 加 detached dual-signal 推导（`pid_alive AND
  .cron_registered_retrain.flag`）。
- marker 隔离：与 ns_run_train 共享 `$ORCA_ARTIFACTS_DIR`，故独立命名（`.retrain_pid` /
  `.retrain_rc` / `.retrain_eta.txt` / `.cron_registered_retrain.flag` / `.cron_rerun_retrain.sh` /
  `.cron_rerun_retrain_inputs.json`）—— `ns_run_train` 用裸 `.train_*` / `.cron_*`，不碰撞。
- MARKER `ORCA_CRON_NS_SUPERNET_RETRAIN`；cron 重跑命令 `orca nas-supernet --inputs ...`。
- `nas-supernet.yaml`：`ns_retrain.output_schema.status` enum 加 `detached`；route 加
  `when status=='detached' → terminate_retrain_pending`（在既有 executed / failed 路由前）；
  新增 `terminate_retrain_pending` 终态节点（status=success + reason 文案明示 deferred 语义）。

### train-teacher（kd-nas）

- `train-teacher/agent.md`：Step 0 三分支（reuse / resume-pending / fresh-launch）+ Step 1 五步
  （1a wrapper 生成 + detach / 1b warmup / 1c 估时 / 1d cron / 1e park / 1f fail loud）；Step 2
  metrics_tail（仅 executed 走）；Step 3 emit JSON（status + 全字段）。
- **wrapper 串 train_pipeline.py --mode teacher + teacher_setup.py**：捕获本 run 的 `$PER_RUN` 快照
  （cron 重跑换 run 后 PER_RUN 变，但 wrapper 已 detach 用旧值跑通的训练不需新 run 干预；产物
  teacher_cache.pt / teacher_ckpt.pt 落 `$KD_ARTIFACTS_DIR` 稳定路径，新 run 0a reuse 命中）。
- **关键适配**：kd-nas 的 `$ORCA_ARTIFACTS_DIR` 是 per-run（不持久），所有 deferred marker 必须落
  `$KD_ARTIFACTS_DIR`（project-scoped，跨 run 持久）：`.train_teacher_pid` / `.train_teacher_rc` /
  `.train_teacher_eta.txt` / `.cron_registered_train_teacher.flag` / `.cron_rerun_train_teacher.sh` /
  `.cron_rerun_train_teacher_inputs.json` / `.teacher_deferred_wrapper.sh`。wrapper 重定向 train log
  到 `$KD_ARTIFACTS_DIR/runs/teacher/train.log`（稳定路径，跨 run 可读—— resume-pending + Step 2
  metrics_tail 都读此路径）。
- MARKER `ORCA_CRON_KD_NAS_TRAIN_TEACHER`；cron 重跑命令 `orca kd-nas --inputs ...`。
- `kd-nas.yaml`：`train_teacher.output_schema` 加 `status` field（required，enum
  `[executed, failed, detached]`）+ `assessment` field；routes 加 `when status=='detached' →
  terminate_train_teacher_pending` + `when status=='failed' → terminate_train_teacher_failed`；
  新增 2 终态节点（pending: success + reason / failed: failed + reason）。

### distill（kd-nas 迭代节点）——评估后**不**迁移

distill 是每轮迭代的 student 训练。评估结论：deferred-cron 对迭代节点**不适用**，硬上会跨节点耦合。
阻碍点：

1. **gen_student 跨 run 状态依赖**：gen_student 据 `ledger.jsonl` 行数算 round + LLM 生成下一轮
   student。distill 在 round N 中途 detached → cron 重跑新 run 的 gen_student 见 ledger 仍只到
   N-1 行 → 生成 round N（覆盖上一 run 的 `r<N>_student_model.py`，LLM 非确定性 → 不同 student）。
   上一 run 的 in-progress student 训练被废弃。
2. **decide 同步依赖 distill output**：decide 从 distill output 拿 latency/accuracy append ledger。
   distill detached → decide 无 metrics → 无法 append / 决策。需要新「deferred row」ledger schema +
   decide 改造。
3. **无 project-scoped 单 round ckpt marker**：distill 每轮 ckpt 路径 `checkpoints/r<N>_student.pt`
   是 per-round，没有跨 run 持久的「本轮 in-progress」marker 可作 0a reuse 校验。

迁移 distill 需要：(a) 加持久「round in-progress」marker；(b) gen_student 跨 run soft-skip 该轮；
(c) decide 处理 deferred ledger row；(d) ledger schema 扩展。**跨 4 节点的状态耦合改造，超出「同模式
迁移」范围**，按 SPEC §5「若复杂/有歧义→不要硬上，留用户定夺」处理。当前 distill 仍走原「detach +
多次短轮询到 DONE」模式（长任务但单轮内可收敛）。

### 偏差记录

- train-teacher 加 `status` required field（原 schema 无 status）；下游 gen_student / distill / decide
  / finalize 不读 `train_teacher.output.status`（只读 teacher_cache / teacher_meta / teacher_ckpt），
  schema 加字段对下游零影响。
- train-teacher 加 `terminate_train_teacher_failed` 显式 failed 终态（peer review 抓到的隐性 bug：
  原本 status=failed 会落 catch-all → gen_student 静默带空 teacher_cache 跑蒸馏；显式 failed 路由
  防 catch-all 误放行——比 ns_run_train 的「failed 靠下游缺 ckpt 兜底」更干净）。
- train-teacher Step 3 emit JSON 后**不**退 2（不像原 train-teacher 的 fail loud `exit 2`）——
  failed 由路由 `terminate_train_teacher_failed` 触发 workflow_failed；保留 emit JSON 让 tape 留痕。
- ns_retrain Step 5 ckpt 解析：marker 优先（与 ns_run_train 一致），回落 search_config.yaml 不适用
  （retrain 无 search_config），改为回落 `.ns_retrain_ckpt_path.txt`（生成阶段写的 agent-owned marker）。
- train-teacher wrapper 内 DUMMY_INPUT 通过 bash 变量捕获（`'` 包围保 JSON）——因 wrapper 是
  `<<EOF`（无 quote）的 bash heredoc，DUMMY_INPUT 含 JSON `{}` 需单引号字面化，避免 shell 干扰。

### 验证（迁移部分）

- `tars validate workflows/nas-supernet.yaml` + `tars validate workflows/kd-nas.yaml`：**0 error / 0 warning**。
- ns_retrain + train-teacher 各 9 个 bash 块全过 `bash -n`；python heredoc 全过 `ast.parse`。
- ns_retrain Step 5 status 推导 6 场景 smoke test 全过：AGENTS.md 缺 / ckpt 存（executed）/ pid 活 +
  flag（detached）/ pid 死 + flag stale（failed — dual-signal 防 stale flag 误判）/ pid 活无 flag
  （failed — dual-signal 防 eta-only 误判）/ stale eta + dead pid（failed — fresh-launch self-heal
  耗尽不被误判）。
- train-teacher Step 3 status 推导 6 场景 smoke test 全过：3 artifacts 存（executed）/ pid 活 + flag
  （detached）/ pid 死 + flag stale（failed）/ pid 活无 flag（failed）/ nothing（failed）/ pid 死 +
  eta stale（failed）。

## self-review 加固明细（迁移部分）

经一轮 self-review（含 must-fix / should-fix / optional 三档）闭环：

- **关键修复（self-caught must-fix）**：train-teacher 加 status=failed enum 后，原 catch-all → gen_student
  路由会让 status=failed 静默带空 teacher_cache 进蒸馏（fail loud 失效）。加显式
  `when status=='failed' → terminate_train_teacher_failed` + 新 terminal 节点（status=failed）；同时
  从 Step 3 python 移除 `sys.exit(2)`（agent 内部 bash 工具的 exit code 不传到引擎，路由才是 fail-loud
  机制）。
- **边界加固（should-fix，code-reviewer 闭环）**：
  - 清「退 2 → workflow_failed」文档残留（status field description + 1b WARMUP_FAIL 分支 + 1f prose
    + 监督要点 + 输出段——全统一为「emit JSON → 路由 terminate_train_teacher_failed」）。
  - Step 2 metrics_tail log path 改 `$KD_ARTIFACTS_DIR/runs/teacher/train.log`（稳定路径，与 wrapper
    重定向一致），reuse / cron-rerun 场景下能读到 log（原指 per_run_artifacts_dir 在 cron 重跑时是
    新 run 的 PER_RUN → 找不到旧训练 log，charts 静默丢失）。
- **采纳与未采纳（optional）**：
  - 未采纳——marker 命名风格统一（ns_run_train 裸名 vs ns_retrain/train-teacher 后缀），属未来重构
    项（当前不碰撞，待第 4 个 deferred agent 共享目录时再统一）。
  - 未采纳——train-teacher wrapper 启动时清 stale teacher_cache.pt（teacher_setup.py 会覆盖；0a reuse
    sha256 校验拦 stale；非必要加固）。
  - 未采纳——kd-nas.yaml 全节点外层 `additionalProperties: false`（项目级 follow-up，非本次回归）。

### 测试覆盖（迁移部分）

- 已覆盖：`tars validate` schema + route 引用合法性；status 推导 smoke test 12 场景（ns_retrain 6 +
  train-teacher 6）；bash + python 语法。
- 未覆盖（留用户）：真机 E2E 三场景——(a) cron 触发新 run 的 ns_retrain / train-teacher 跨 run reuse
  收敛（reuse / resume-pending / fresh-launch 三分支端到端）；(b) warmup 失败 → failed terminal →
  workflow_failed；(c) train-teacher wrapper 跨 run 用旧 PER_RUN 跑通 + 新 run reuse cache。这三个
  真机 E2E 与原型 ns_run_train 的 SPEC §4 acceptance 1-5 同批留待用户。

## Commit（迁移）

- `658f85c`（迁移 ns_retrain + kd-nas train-teacher；distill 评估后不迁移）
- 原型 commit：`e8f7700`（ns_run_train deferred-cron 原型实现）

## 相关

- SPEC：[`docs/specs/deferred-training-cron-design-draft.md`](../specs/deferred-training-cron-design-draft.md)
- 长任务执行背景：[`docs/specs/long-task-execution-design-draft.md`](../specs/long-task-execution-design-draft.md)
