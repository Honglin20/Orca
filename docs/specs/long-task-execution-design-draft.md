# 设计草稿：长任务执行（detach + 跨多次短调用轮询）

> 解决 nas-supernet auto-run 节点（`ns_run_train` / `ns_run_search` / `ns_retrain`）跑长训练/搜索时
> bash 工具单调用超时导致进程被杀的问题。

## 1. 根因

这些节点原把 `nohup ... &; while kill -0 $PID; do sleep 30; done; wait $PID` 放在**单个 bash 工具调用**
里。训练/搜索耗时分钟～小时 → 单调用超过 bash 工具超时上限（约 10 min）→ 整调用被杀 → 训练被终止
（即用户报告的 training 频繁终止、"tool time out"）。detach+poll 没逃脱超时，因为 poll 循环本身在
同一个超时调用内。

## 2. 契约：detach + 跨多次短调用轮询

bash 工具单调用有超时上限。长任务必须拆成多次短调用，靠 agent 多轮 loop 提供轮询间隔：

1. **detach 调用**（秒级返回）：`nohup` 起后台 wrapper——`bash <script> > <log> 2>&1; echo $? > <rcfile>`，
   `echo $! > <pidfile>`，立即返回。**禁在此调用 wait/sleep**。
2. **轮询调用**（**重复发**，每次 < 工具超时）：
   `PID=$(cat <pidfile>); kill -0 $PID && { sleep 240; echo RUNNING; tail; } || { echo "DONE rc=$(cat <rcfile>)"; tail; }`。
   stdout `RUNNING` → 再发一次（每次轮询是独立短调用，**禁**在同一调用里 while 循环）；`DONE` → 判成功。
3. **无轮询上限**：训练/搜索/retrain 可能跑很久（小时～天级）。重复发轮询调用直到 `DONE`——**不设次数
   上限**。仅 warmup（下条）检测早期假死；过了 warmup 即信任进程在跑，持续轮询到结束。
4. **warmup 健康检查**：前 2~3 次 `RUNNING` 轮询的 log 应出现 epoch 标记 + loss 有限（非 NaN/inf）。
   无 epoch 标记 / loss 发散 → 训练假死或静默崩 → `kill` + self-heal，**不空等**。

**跨 shell RC 捕获**：detach 调用的子 shell 末尾 `echo $? > <rcfile>` 把进程 RC 落文件；轮询调用是
**不同** bash 子 shell（`wait` 只对当前 shell 的子进程有效，跨 shell 不可用）→ 从 `<rcfile>` 读 RC。

每次 `sleep 240`（4 min）远低于工具超时；**禁**改成更大值。

## 3. 适用节点

| 节点 | step | 脚本 | pidfile / rcfile | 成功判据（DONE 后） |
|---|---|---|---|---|
| `ns_run_train` | Step 2 | `run_train_supernet.sh` | `runs/train/.train_pid` / `.train_rc` | `rc=0` + supernet ckpt 存在 |
| `ns_run_search` | Step 2 | `run_search_supernet.sh` | `runs/search/.search_pid` / `.search_rc` | `rc=0` + `search_results.jsonl` ≥1 行 |
| `ns_retrain` | Step 4 | `run_retrain.sh` | `runs/retrain/.retrain_pid` / `.retrain_rc` | `rc=0` + `.ns_retrain_ckpt_path.txt` 指向的 ckpt 存在 |

各节点 frontmatter description 里"detach + 轮询进程到结束"改为"detach + 跨多次短调用轮询"。

## 4. 非目标

不改各节点的 self-heal 编辑白名单、fidelity retrigger（Step 2.5/4.5）、output_schema、软跳过 Step 0、
判成功后的软判断 assessment。**仅**改 detach+poll 的执行方式（单调用 → 多次短调用）+ 加 warmup/上限。
