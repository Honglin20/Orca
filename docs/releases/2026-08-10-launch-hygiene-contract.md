# Release: launch hygiene 契约 + 残留进程清理（防历史 run 训练失败复现）

**日期**: 2026-08-10
**类型**: fix（nas-supernet 训练启动卫生：DataLoader fork/pin 崩溃 + rendezvous 端口冲突 + 残留进程叠加）
**分支**: in-session-unified-backend
**Commit**: 待用户 commit

## 背景 / 根因

另一台服务器用较弱模型实跑 nas-supernet 的历史 run 反复死于训练启动卫生问题（本仓库未
commit，实况复盘）：
1. `num_workers=4` + fork 启动模式：父进程先初始化 CUDA 再 fork worker → `CUDA
   initialization error`（PyTorch 经典陷阱，与分布式无关）。
2. `pin_memory=True` → `CUDA tensors cannot be pinned`。
3. torchrun 未指定 `--master_port` → 固定默认 29500，多实例/残留进程并存时 rendezvous
   `EADDRINUSE`（`static_tcp_rendezvous` 报错）——两层重试（wrapper 内部 attempt 循环 +
   Orca re-arm）叠加放大。
4. 失败后无残留进程清理，下次 detach 又起第二份训练，端口/ckpt 互踩。

判定：全是**确定性 launch hygiene** 问题，应在生成契约（源头）固化，而非运行期靠 LLM
self-heal 救火。属 bug 级修复，不做架构改动（单进程/引擎轮询方案已讨论、另行决策）。

## 改动（5 个文件）

### A. 生成契约（ns_train_script）

- `references/workflows/train_supernet_script_generation.md`：
  - 新增 **§4 DataLoader Launch Hygiene**（mandatory）：所有 DataLoader 一律 `num_workers=0`
    + `pin_memory=False`，launcher 变量 `NUM_WORKERS` 默认 0 且禁改。
  - launcher 骨架：`NUM_WORKERS=4` → `0`（注释注明事故）；新增
    `MASTER_PORT=$((20000 + RANDOM % 20000))` + `--master_port="$MASTER_PORT"`（每次启动随机，
    attempt 间换端口防 TIME_WAIT）。
  - torchrun cross-check 措辞：`--nnodes`/`--nproc_per_node`/`--master_port` 为 torchrun 自身
    flag，排除在 train_supernet.py argparse 交叉核对之外。
- `references/workflow-checklists/train_supernet_script_generation.md`：
  - [M]30 变量清单加 `MASTER_PORT`、`NUM_WORKERS` 默认 0；[C]33 排除 torchrun 自身 flag。
  - 新增 [C]36 DataLoader Launch Hygiene、[C]37 Rendezvous Port Uniqueness（追加编号 36/37，
    **不重排** 1-35，避免 workflow-verifier companion checklist 编号连锁）。

### B. retrain 生成契约（ns_retrain/agent.md）

- 生成契约新增 **DataLoader 卫生铁律**：retrain.py / finetune.py 所有 DataLoader
  `num_workers=0` + `pin_memory=False`（retrain 走 `python3 retrain.py` 直调无 torchrun，
  端口问题不适用，仅数据管道卫生）。

### C. 运行期残留进程清理（launch.sh × 2）

- `ns_run_train/scripts/launch.sh` + `ns_retrain/scripts/launch.sh`：detach 前检查
  `.train_pid`/`.retrain_pid` 记录的前次进程，`kill -0` 存活 **且** `/proc/<pid>/cmdline`
  匹配 `run_train_supernet`/`run_retrain` 才整组杀（`kill -- -PID`，setsid 进程组，连训练
  python 一起清）+ 短等待退出。cmdline 校验防 PID 复用误杀无关进程。
- 与 status.sh 完成判定兼容：launch.sh 仅在 `TRAIN_INCOMPLETE`（无活进程）路径被调，清理
  不破坏 rc/ckpt 判定（status.sh 已完成判定已排除活进程，stale rc 有防御）。

## 验证

- `bash -n` 两个 launch.sh 通过。
- 实测 launch.sh 清理逻辑（临时 artifacts 目录 + 假 wrapper）：
  - 无关进程（cmdline 不匹配）→ 保留不误杀 ✓
  - 匹配残留 wrapper → `KILLED_STALE_PID` 整组杀 + 新训练正常 detach ✓
- `tests/workflows` 506 passed（4 个预存在失败与本改动无关：隔离验证——stash 掉本改动后
  3 个 `test_ns_chart_scripts` 失败依旧，根因是工作区未提交的 `ns_run_search/scripts/_common.py:251`
  NameError）。
- `tests/compile` 212 passed。
- `tars validate workflows/nas-supernet.yaml` 通过。

## 生效范围

生成契约改动作用于**下次** ns_train_script / ns_retrain 生成；launch.sh 清理立即生效。
历史 run 事故类别（fork 崩溃 / pin 报错 / 端口冲突 / 残留叠加）在契约层被封死，运行期
self-heal 预算不再被确定性卫生问题消耗。
