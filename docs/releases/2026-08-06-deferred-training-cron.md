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

- `ns_retrain`（nas-supernet）：未迁移到 deferred 模式。
- `kd-nas` 的 `train-teacher` / `distill`：未迁移。
- 真机 E2E（SPEC §4 acceptance 1-5）：未跑——本 task 只交付原型实现 + tars validate + smoke test。

## Commit

- `e8f7700`

## 相关

- SPEC：[`docs/specs/deferred-training-cron-design-draft.md`](../specs/deferred-training-cron-design-draft.md)
- 长任务执行背景：[`docs/specs/long-task-execution-design-draft.md`](../specs/long-task-execution-design-draft.md)
