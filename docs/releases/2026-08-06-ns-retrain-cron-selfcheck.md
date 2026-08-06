# 2026-08-06 — ns_retrain 迁移：in-session CRON 定时自检（与 ns_run_train 同模型）

## 背景

CURRENT.md 待办之一：ns_retrain 仍走 `658f85c` 的 deferred-training-cron（detached + 外部
at/crontab + headless 重跑 workflow），与 ns_run_train 的 in-session CRON 模型不一致。本任务把
ns_retrain 按 ns_run_train 同一模型迁移（洁净契约对齐）。

## 改动

### `workflows/agents/ns_retrain/agent.md`（686 行 → 365 行，重写）

- 决策树：Step 1 `status.sh`（完成 + 存活二合一）→ Step 2 `health.sh`（含假死判定）→ Step 3
  生成/启动/续训 → Step 4 `emit_result.py`。删 Step 0 三分支（reuse / resume-pending /
  fresh-launch）、detached、外部 cron 注册（at/crontab + `.cron_rerun_retrain.sh`）。
- **生成阶段保留**（本节点特有，ns_run_train 没有）：3a 按 AGENTS.md scaffold 生成
  retrain.py / finetune.py / run_retrain.sh → 3b fidelity 复查（必跑，point-to-file 协议）。
  生成只做一次（`run_retrain.sh` 存在即跳过）。
- **生成契约新增**（scripts 解析前提治本，对齐 ns_train_script 的 log 契约）：每 progress
  unit 必打 `epoch <cur>/<total> loss <v>` / `step <cur>/<total> loss <v>`；总进度以
  `--epochs N`（或 `EPOCHS=N` 变量形态）暴露；final ckpt 固定写
  `runs/retrain/retrain_best.pth`。
- 尝试预算 N=1..3 跨唤醒统一（`.retrain_attempt`）；CRON 生命周期（一次性续期 + 完成后
  取消/不续期）；未完成 → 状态说明（含"请勿调用 orca next"）；完成 → Step 4 单行 JSON。

### `workflows/agents/ns_retrain/scripts/`（新建 7 脚本，镜像 ns_run_train 契约）

| 脚本 | 职责 |
|---|---|
| `status.sh` | RETRAIN_COMPLETE（rc=0 + 进程退出 + ckpt 有效 + torch.load 可读）/ RETRAIN_ALIVE / RETRAIN_INCOMPLETE；ckpt marker 落绝对路径（`.ns_retrain_ckpt_resolved.txt`） |
| `health.sh` | 唤醒健康检查（epoch / loss / LOG_MTIME / LOG_SIZE） |
| `launch.sh` | 尝试预算 + setsid detach（进程组首领，整组 kill 防孤儿） |
| `warmup_poll.sh` | 单轮轮询（4min sleep），WARMUP_OK/RUNNING/FAIL + 前导零归并 |
| `eta.py` | 估时（total 解析 run_retrain.sh `--epochs`/`EPOCHS=` + retrain.py argparse 回落） |
| `update_status_md.sh` | 写 `retrain_status.md`（epoch 从 log 重算；`stuck` 参数） |
| `emit_result.py` | 最终 JSON：AGENTS.md 缺 → failed；完成 → executed；否则 failed + last_error |

### `workflows/nas-supernet.yaml`

- `ns_retrain` status 枚举 `[executed, failed, detached]` → `[executed, failed]`。
- 路由删 `status=='detached' → terminate_retrain_pending`；executed → ns_visualize；
  catch-all → terminate_retrain_failed。
- 删 `terminate_retrain_pending` 终态节点（4 terminate → 3，全 fail-loud）+ 节点注释块 +
  description 同步（"ns_retrain 仍走 deferred training via cron（迁移待办）" → 同 in-session 模型）。
- `terminate_retrain_failed` reason 补"尝试预算耗尽"。

## 验证

- `bash -n` 5 脚本 + `ast.parse` 2 python 全过；`tars validate` nas-supernet 0 error / 0 warning。
- **34 项 smoke test 全过**（mock 训练场景）：status 五态（no-state / rc0-invalid-ckpt /
  complete-valid / ckpt-marker 绝对路径 / alive-stale-rc）、launch 预算 1..3→耗尽、
  warmup 五态（process-exit / loss 发散全文检测 / epoch / step / 前导零归并）、eta 三形态
  （`EPOCHS=` 变量 / `--epochs` flag / argparse default 含 add_argument 形态）、MD（重算
  epoch / stuck / header）、emit 六态（AGENTS.md 缺 / executed + marker artifacts /
  stale-rc-alive 防误判 / healed / fidelity / last_error tail）、health、setsid 新会话回归。
- 残留扫描：detached / resume-pending / terminate_retrain_pending / `.cron_rerun_retrain` /
  `.retrain_eta.txt` / at now / crontab 全清（仅存"不要用系统 crontab/at"说明与 yaml 注释
  "无 detached"）。

## 待办 / 已知

- **kd-nas train-teacher 仍走旧 deferred-training-cron 模型**——按本模型迁移（用户另行定夺）。
- 未完成路径依赖宿主模型识别"请勿调用 orca next"字样（prompt 文本约束，无引擎强制）；
  引擎侧 `_drive_protocol` 分支已随 ns_run_train 任务落地，本任务零引擎改动。
- 真机验证：CRON 唤醒 → 检查 → 完成 → `orca next` 提交 → 下游继续的完整闭环（与 ns_run_train
  同一待办）。

## 补：code-reviewer 独立洁净审查闭环（用户派发）

0 MUST-FIX + 2 SHOULD-FIX + 6 MINOR，全修：

| # | 问题 | 修法 |
|---|---|---|
| S1 | **3b 首启 fidelity 先于 launch 写 flag → launch 清 marker 抹掉 → 成功路径 `fidelity_retriggered=false` 失真**（与 yaml/agent.md 审计语义矛盾；ns_retrain 特有——ns_run_train 无 launch 前 fidelity） | launch.sh rm 列表去 `.ns_retrain_fidelity.flag`（3b/3g 覆盖写无 stale，语义改"对当前脚本已跑过 fidelity"，跨 attempt/run 保留仍准确）；marker 节同步说明 |
| S2 | 3a 生成落点未声明（launch/eta/emit 都按 artifacts 根解析，落错目录白烧预算）+ gate 单文件防不了"写中断残留" | 3a 补"写到 `$ORCA_ARTIFACTS_DIR/` 根"；gate 改双文件（`run_retrain.sh` **且** `retrain.py`；finetune.py 条件生成不做 gate）；3f 允许 write 重写自产文件（禁碰清单除外） |
| M3 | emit_result else 分支无条件 `max_retries_hit=True`，3b 禁碰清单拒改前置失败（N=1）误报"耗尽" | `max_retries_hit` 改从 `.retrain_attempt` 推导（≥3 才 true），docstring 同步 |
| M4 | health.sh / warmup_poll.sh 注释"契约见 Step 2"实际在 Step 3a | 两处注释改 Step 3a |
| M5 | 资源锚点 `.retrain_eta.json` 缺 `runs/retrain/` 前缀 | 补全（与 3e 一致） |
| M6 | 铁律 4"可改任何 .py import 行"与禁碰清单字面张力 | 括注"禁碰清单除外" |

验证：smoke test 扩到 **37 项全过**（新增 S1 回归：launch 保留 fidelity flag / 清 healed；M3 回归：
attempt=1 前置失败 → max_retries_hit=false）；`tars validate` 0/0；bash -n + ast.parse 全过；
残留扫描仍全清。
