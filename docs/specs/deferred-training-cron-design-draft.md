# 设计草稿：deferred training via cron（warmup 估时 → cron 定时恢复）

> 多天训练解耦方案。先在 `ns_run_train` 落地原型，测通后迁移到所有 training agent（`ns_retrain` +
> kd-nas 的 `train-teacher` / `distill`）。

## 1. 问题

训练耗时小时～天级。agent 节点无法 open 那么久：bash 工具单调用超时（~10min）+ 节点 turn / wall-clock
预算烧不起。即便用 detach + 跨多次短调用轮询（已落地），多天训练要烧数百次 agent turn，不可持续。

## 2. 模式：warmup 估时 → cron 定时 → park → cron 重跑 → 软跳过

Orca 的 `wait` 节点是同步 `asyncio.sleep`（阻塞 drive loop，不适合多天）；`resume` 是崩溃恢复（不支持
"节点故意 park 后重入"）。故走 **terminate + cron 重跑 + 软跳过**（无需新 pause 语义）：

1. `ns_run_train` detach 训练（nohup，跨 session 存活）。
2. **warmup**：轮询训练 log，等前 1~2 个 epoch 标记出现，**测每 epoch wall-clock** + 确认 loss 有限 /
   无崩（确认能跑通）。
3. **估时**：剩余 epoch（总数见 `run_train_supernet.sh` 的 `--epochs` / search_config）× 每 epoch 耗时
   = 剩余时间 T。
4. **cron 注册**：登记一个 one-shot 定时任务（`at now + T` 或自清理 crontab 条目），到点**重跑整个
   workflow**（`cd {{ inputs.project_root }} && tars nas-supernet --inputs '<json>'`），条目触发后自清。
5. **park**：`ns_run_train` 返回 `status=detached`（assessment 写 "training detached, ~<T> remaining,
   cron 将于 <time> 重跑 workflow"）。
6. **yaml 路由** `status==detached` → 终态 `terminate_training_pending`（**非失败**："训练后台跑着，
   cron 会重跑 workflow 接力"）。
7. **cron 到点重跑** → 新 run：`ns_run_train` Step 0 见 ckpt（训练完成）→ 软跳过 `executed` → 下游继续。

### 三分支 Step 0（复用 / 续 pending / 全新发起）

- **reuse**（既有）：ckpt 存在 + 可 load → `executed`（reused），跳过。
- **resume-pending**（新）：ckpt 缺**但** `.train_pid` 存在 + `kill -0` 活着（前次 detach 的训练还在跑）
  → 读 log 当前 epoch，重新估剩余，重注册 cron，返回 `detached`。**禁重新 detach**（会起第二个训练）。
- **fresh-launch**（新）：ckpt 缺 + 无训练在跑 → 上面的 1-5（detach + warmup + 估时 + cron + detached）。

resume-pending 分支让估计不准也能收敛：cron 早到（ckpt 没好）→ 重跑的 ns_run_train 走 resume-pending
→ 重估 + 重 cron；cron 晚到（ckpt 已好）→ reuse → 继续。

## 3. 改动（ns_run_train 原型）

### 3.1 `ns_run_train/agent.md`
- Step 0 扩三分支（reuse / resume-pending / fresh-launch）。
- Step 2 重写：fresh-launch 的 detach + warmup（短调用轮询前 1~2 epoch 测时 + 确认跑通）+ 估时 +
  cron 注册 + 返回 detached。**移除**当前的"无上限轮询到 DONE"（被本模式取代——训练不再在同一节点内
  等完成）。warmup 仍用短调用轮询（仅前几个 epoch，~分钟级，不撞工具超时）。
- warmup 失败（无 epoch / loss 发散 / 训练崩）→ 走既有 self-heal（白名单修 + 重试，≤3 次）。

### 3.2 `nas-supernet.yaml`
- `ns_run_train` output_schema：`status` 枚举加 `detached`（`executed`/`skipped`/`failed`/`detached`）。
  `detached` = 训练已 detach + cron 已注册，ckpt 待就绪。
- `ns_run_train` 路由：`status==detached` → `terminate_training_pending`；其余（executed/skipped/failed）
  → 既有（→ ns_run_search；engine 对 agent node status 不自动判败，failed 由下游 ns_run_search 缺 ckpt
  兜底，既有契约不变）。
- 新增 `terminate_training_pending` 终态节点（`status: failed`? 否——**非失败**：用 `status: success` +
  reason，或评估 Orca 是否支持非失败非成功的"pending"终态；若只支持 success/failed，用 success + reason
  文案明示"pending，cron 接力"，避免被误判为 workflow 失败）。

### 3.3 cron 注册（agent 在 fresh-launch / resume-pending 里执行）
- 写一个自包含重跑脚本到 `$ORCA_ARTIFACTS_DIR/.cron_rerun.sh`：`#!/bin/bash\ncd "{{ inputs.project_root }}"\ntars nas-supernet --inputs '<inputs json>'\n`（inputs 从 `{{ inputs.* }}` 拼）。
- 注册 one-shot：优先 `echo "bash <script>" | at now + <T> minutes`；`at` 不可用 → crontab 条目（带唯一
  marker，触发后 `crontab -l | grep -v <marker> | crontab -` 自清）。
- 估时 T 落 assessment + 一个 `.train_eta.txt` marker（resume-pending 重估时参照）。
- cron 重跑命令用 `tars`（headless）；**注**：原 run 若是 in-session，cron headless 重跑是上下文切换——
  本设计假定训练机用 headless `tars`（用户的 GPU 机典型如此）；in-session 场景文档化为限制。

## 4. 验收（小测试）

造一个**小训练任务**（如 MNIST 2-epoch 快训，或 mock 训练脚本每 epoch sleep + 写 epoch log + 末写 ckpt），
跑 `ns_run_train`，断言行为序列：
1. **先 warmup 确认能跑通**：detach 后轮询 log，见到 epoch 标记 + loss 有限（不直接 cron）。
2. **测每 epoch 耗时 + 估计剩余**：assessment / marker 含 per-epoch 时间 + 剩余估计。
3. **设 cron**：`at`/crontab 里有登记的重跑条目（或 `.cron_rerun.sh` 存在 + 定时已注册）。
4. **park**：节点返回 `status=detached`，workflow 到 `terminate_training_pending`。
5. （可选）等到 cron 触发或手动跑重跑脚本 → 新 run 的 ns_run_train reuse ckpt → executed → 下游继续。

行为符合 1-4 即**通过**（5 验证接力，可选）。通过后才迁移。

## 5. 迁移（通过后）

- `ns_retrain`（nas-supernet）：同三分支 + warmup + cron + detached + route。
- kd-nas `train-teacher` / `distill`：同模式（kd-nas 重跑命令 `tars kd-nas --inputs ...`；distill 是每轮
  迭代节点，评估 deferred 是否适用——迭代内单次蒸馏若也小时级则同样适用）。

## 6. 非目标

- 不引入 Orca pause/resume-of-node 新语义（用 terminate + cron 重跑 + 软跳过）。
- 不改 ns_run_search（搜索通常较短；若也需 deferred 另案）。
- cron 机制限 Linux（训练机）；Windows / in-session 重跑为已知限制。
- 不改既有 self-heal 白名单 / fidelity retrigger / output_schema 其它字段。
