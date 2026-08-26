# 2026-08-06 — ns_run_train 重写：in-session CRON 定时自检（节点常驻到训练真正完成）

## 背景

用户逐轮对齐后确认的新模型，替代 `e8f7700` 的 deferred-training-cron（外部 cron + headless
重跑 workflow + park detached）。核心诉求：

1. **节点不 park、不挂起、不提前输出**——训练未完成前节点一直"执行中"，run 保持活跃，引擎零改动。
2. **checkpoint ≠ 训练完成**——ckpt 可能是中途中断残留，完成判定 = rc=0 + 进程已退出 + ckpt 有效，
   残留 ckpt 必须续训到真正完成。
3. **CRON 是 in-session 工具**（CC / OPENCODE / CAC / NGA 均有），agent 自己调用注册 1~2h 定时自检，
   到点唤醒**同一条 run 同一个 session**，检查状态；未完成 → 更新 `train_status.md` + 重注册；
   完成 → 输出最终 JSON，宿主 `orca next` 提交，下游继续。
4. **PID + 状态落 MD 文件**（`$ORCA_ARTIFACTS_DIR/runs/train/train_status.md`，跨唤醒真相源）。

## 改动

### `workflows/agents/ns_run_train/agent.md`（637 行 → 601 行，重写）

- 决策树：Step 0 自门控 → Step 1 完成判定（rc=0 + 进程退出 + ckpt 有效 + torch.load
  `weights_only=False`）→ Step 2 进程存活 → Step 2.1 健康检查（含假死判定）→ Step 3 启动/续训
  （3a 清 marker → 3b 尝试预算+detach → 3c warmup → 3d 估时 → 3e MD → 3f self-heal →
  3g fidelity → 3h CRON 注册 → 3i 状态说明收尾）→ Step 4 自校验 JSON。
- **尝试预算**：所有"启动/重跑"共享 N=1..3（`.train_attempt` 跨唤醒计数）——warmup 自愈、假死重启、
  中断续训、rc==0 无 ckpt 重跑共享预算，N>3 → failed（杜绝无限重启循环，收敛保证）。
- **CRON 生命周期**：一次性续期语义（每次唤醒重注册下一轮）；完成后取消/不再续期，防周期唤醒
  把 executed JSON 提交给下游节点连锁失败。
- 未完成路径：最终回复 = 状态说明（含"请勿调用 orca next"字样），宿主不调 next。
- 删除：估时器重估、`.train_eta.txt`、at/crontab 外部 cron、resume-pending、detached、三分支收敛。

### `workflows/nas-supernet.yaml`

- `ns_run_train` status 枚举 `[executed, skipped, failed, detached]` → `[executed, skipped, failed]`。
- 路由删 `status=='detached' → terminate_training_pending`，单出边 → ns_run_search。
- 删 `terminate_training_pending` 终态节点 + 节点注释块 + description 同步（ns_retrain 仍走旧模型，
  迁移待办）。4 terminate（3 fail-loud + 1 deferred success：ns_retrain）。

### `orca/iface/in_session/cli.py` —— `_drive_protocol` 文本加分支（唯一引擎侧改动，纯 prompt 文本）

- 步骤 2 拆两支：子代理产出**合法 JSON** → `orca next --output` 原样提交；产出**非 JSON 且含
  『请勿调用 orca next』**（长任务挂起标记）→ **不调 next**，节点保持执行中，等 CRON 唤醒后
  重读节点指令、重派子代理。逻辑零改动（引擎对 AgentNode status 不自动判败的既有契约不变）。

## 重构：确定性逻辑固化到 scripts/（用户确认后执行）

按 repo folder-agent 约定（`$ORCA_AGENT_RESOURCES` 锚定，`tars install` 随 agents 目录部署），
把 agent.md 内联的 10 个 bash/python 块抽成 **7 个脚本**：

| 脚本 | 替代 | 职责 |
|---|---|---|
| `scripts/status.sh` | Step 0+1+2 | gate / 完成（rc=0+进程退出+ckpt 有效）/ 存活 三合一判定，输出一行 verdict |
| `scripts/health.sh` | Step 2.1 | 健康检查（epoch / loss / log 尾部） |
| `scripts/launch.sh` | 3a+3b | 尝试预算 N=1..3（`.train_attempt` 跨唤醒）+ 清 marker + detach |
| `scripts/warmup_poll.sh` | 3c | 单轮轮询（4min sleep），WARMUP_OK/RUNNING/FAIL 判据 |
| `scripts/eta.py` | 3d | 估时（落 `.train_eta.json`，信息用） |
| `scripts/update_status_md.sh` | 3e | 写 `train_status.md`（epoch 从 log 重算，ETA 参考估时文件） |
| `scripts/emit_result.py` | Step 4 | 最终 JSON（skipped/executed/failed + 审计字段） |

agent.md **601 → 320 行**：只留铁律、决策树、self-heal 白名单、fidelity、CRON 生命周期、收尾
（LLM 判断部分），脚本只跑不改（监督要点加"scripts/ 只跑不改"铁律）。收益：CRON 唤醒重读
agent.md 的 token 成本腰斩；确定性逻辑进代码可测试（Rule 5）；与全 repo 约定一致。

验证：`bash -n` 5 脚本 + `ast.parse` 2 python 全过；**22 项 smoke test 全过**（mock 训练场景：
GATE_SKIP / TRAIN_INCOMPLETE / launch 预算 1..3→耗尽 / warmup process-exit / eta 解析 /
MD 重算 epoch / health / emit failed+last_error / skipped / 真 torch ckpt → TRAIN_COMPLETE→
executed）；agent.md 残留扫描零命中；`tars validate` 0/0；`tars install` 部署 7 脚本到
`~/.orca/workflows/agents/ns_run_train/scripts/`。

## 补：log 格式契约 + 超时兜底（"只跑不读"前提补齐）

review 确认"只跑不读"成立的前提是 log 解析脚本（health / warmup_poll / eta / update_status_md）
的格式假设成立——上游 `train_supernet.py` 是 LLM 生成的，格式不保证。两补：

1. **生成契约（治本）**：`ns_train_script/references/workflows/train_supernet_script_generation.md`
   Logging 节加 **machine-parseable progress line 契约**——每 progress unit 必打一行
   `epoch <cur>/<total> loss <v>`（epoch-based）或 `step <cur>/<total> loss <v>`（step-based），
   total 以 `--epochs N` / `--max_steps N` 暴露；tqdm 不算数，禁裸 `epoch`/`step` 词的歧义行。
2. **超时兜底（治标）**：脚本正则兼容 `(epoch|step)`（warmup_poll / health / eta / update_status_md），
   warmup_poll / health 加 `LOG_MTIME`/`LOG_SIZE` 输出；agent.md 判分支分流——warmup 5 次无进度
   标记但 **log 在增长** → 不烧 self-heal 预算，agent 读 log 人工判健康 → eta unknown 照常注册
   CRON；log 无内容/mtime 不涨 → 真卡死 → self-heal。Step 2 假死判定同步加"无标记 → 用 mtime/size"
   兜底。

验证：smoke test 扩到 **27 项全过**（新增 step 格式：eta total/cur 解析 + MD step 行 + health
step grep）；语法全过；`tars validate` 0/0；`tars install` 重新部署。

## 补：code-reviewer 二轮复查（脚本固化后）

1 MUST-FIX + 7 SHOULD-FIX + 8 MINOR 全修：

| # | 问题 | 修法 |
|---|---|---|
| M1 | loss 发散检测死分支（先 grep 数字行 → `loss nan` 永远漏）→ 发散防线静默失效 | 发散检测对 log **全文**；LOSS_LINE 正则去 `-` 排除（支持负 loss） |
| S1 | ckpt 快照名 `supernet_epoch_0005.pth` 污染进度计数（`0005`≠`5`） | 计数管线 `sed 's/^0*//'` 归并 + spec 契约补"保存消息禁裸 `epoch_<digits>`" |
| S2 | eta.py 解析不到 launcher 模板变量形态 `EPOCHS=100` | 加 `^EPOCHS=` / `^MAX_STEPS=` 分支 |
| S3 | update_status_md.sh 恒写 running，假死路径"status: stuck"无法执行 | 支持 `$1` 覆盖（`update_status_md.sh stuck`） |
| S4 | kill 只杀 wrapper → 孤儿训练进程 → 重复 detach（铁律 6 盲区） | launch.sh `setsid` 起进程组；kill 改 `kill -- -PID` 整组杀（agent.md 两处） |
| S5 | warmup 窗口内正常跑完（rc=0）被误判 FAIL 白烧预算 | agent.md 加分支：`process-exit rc=0` → 重跑 status.sh，TRAIN_COMPLETE 直进 Step 4 |
| S6/S7 | `stat -c` GNU-only + 环境假设未声明 | 三处双平台兼容（`stat -c` / `stat -f %m/%z`）+ agent.md 环境依赖声明 |
| MINOR | 步骤号悬空（3f/3h）、3a 标题重复、warmup-timeout 自拟信号表述、yaml 注释残留 | 全清 |

验证：**35 项 smoke test 全过**（新增 M1 发散检测 / S2 变量形态 / S3 stuck / S4 setsid 整组杀
回归）；语法全过；`tars validate` 0/0；`tars install` 重新部署。

## code-reviewer 一轮闭环（用户要求洁净度审查）

3 MUST-FIX + 5 SHOULD-FIX + 4 MINOR 全修：

| # | 问题 | 修法 |
|---|---|---|
| M1 | `_drive_protocol` 无条件要求提交产出 → 状态说明触发 schema mismatch → workflow_failed | 协议文本加"非 JSON 含挂起标记 → 不调 next"分支 |
| M2 | CRON 无终止语义 → 周期唤醒杀 run | 3h 生命周期规则（续期语义 + 完成后取消/不续期） |
| M3 | ckpt marker 写入整句而非路径 → marker 优先契约静默失效 | `printf '%s' "$CKPT"` 纯路径 |
| S4 | 完成判定不看进程存活 → stale rc 提前 executed + 孤儿进程 | Step 1 / Step 4 加 `kill -0` 排除 |
| S5 | 3e 读 stale `.train_eta.json` → MD epoch 冻结 → 假死判定失真 | 3e 从 log 重算 current_epoch |
| S6 | 假死 kill→Step 3 不计预算 → 无限循环 | 尝试预算 N=1..3 统一（3b） |
| S7 | rc==0 无 ckpt → 无限续训 | 共享预算，N>3 → failed |
| S8 | "引擎双层判败"表述与 yaml 路由不符 | 改"下游缺 ckpt fail loud 兜底"真实契约 |
| m9 | N 跨唤醒无定义 | `.train_attempt` 落盘计数 |
| m10 | torch.load 默认 weights_only 炸自定义对象 | `weights_only=False` |
| m11 | 假死措辞矛盾 | 统一"卡死 → 判失败处理" |
| m12 | eta None 打印 Nonemin | `eta: unknown` |

## 验证

- `tars validate`（`orca.iface.cli.commands:main`）nas-supernet 0 error / 0 warning。
- agent.md 10 bash 块 `bash -n` 全过 + 3 python heredoc `ast.parse` 全过。
- 残留扫描：detached / .train_eta.txt / resume-pending / terminate_training_pending / at now /
  crontab 全清（仅存"不要用系统 crontab/at"说明）。
- `tests/iface/in_session/test_in_session_cli.py + test_v3_step1.py`：**170 passed**（含 3 个
  `_drive_protocol` 断言测试，无回归）。

## 待办 / 已知

- **ns_retrain（nas-supernet）与 kd-nas train-teacher 仍走旧 deferred-training-cron 模型**
  （detached + 外部 cron + headless 重跑）——需按本模型迁移（用户另行定夺）。
- 未完成路径依赖宿主模型识别"请勿调用 orca next"字样（prompt 文本约束，无引擎强制）；
  合规计数（no_output_count）不受影响——等待期间宿主不调 next。
- 交互 session 关闭则 CRON 唤醒丢失、run 卡在执行中（resume 恢复属既有机制，未验证本模型下行为）。
