# 2026-08-06 — nas-supernet 可视化分散化：边训练边推送到前端（删 ns_visualize 单独节点）

## 背景

用户要求：nas-supernet 的可视化不该等**全链跑完**才出（原 ns_visualize 是最后一个节点），
尤其训练是小时～天级长任务；**不要单独的可视化 agent**，把可视化固化进前面节点的确定性
逻辑，**边训练边推送**到前端。

## 机制可行性（已核实）

- `orca.chart.render_chart` 是纯 stdlib 轻客户端（`orca/chart/_render.py`）：任何进程带 4 个
  env（`ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK`）即可推图到 per-run socket。**同 label+title
  重复推送 = 前端替换旧图**（实时更新语义，phase-9d §2.7 dedup）——天然支持边训练边刷新。
- chart daemon 是 per-run、run 活跃期间一直活着（`_watch_terminal` 只在终态事件 / TTL 时退出）；
  CRON 模型下训练期间 run 一直活跃 → socket 全程可达。
- 训练进程 detach 后继承 agent bash 的 env（agent 按宿主 prompt 指令先 `source orca_env.sh`）
  → 伴生进程可直接调 `render_chart`，零引擎改动。

## 改动

### 1. 实时 loss 曲线：`live_loss_watcher.py`（ns_run_train + ns_retrain 镜像）

- 由 `launch.sh` 在 setsid wrapper 内、训练脚本**之前**后台启动（同进程组 → self-heal
  整组 `kill -- -PID` 一并清理；stdout/stderr 全丢）。
- 解析生成契约进度行（逐字）：`epoch <cur>/<total> loss <v>` / `step <cur>/<total> loss <v>`
  （严格整行锚定正则，防 log 里歧义行误伤）。每出现新点，把**全量**累计点经 render_chart
  推 line 图（同 title → 前端实时刷新）。x 轴名由首个匹配行自动定为 epoch / step。
- **退出时机**（不依赖 idle 空等）：
  - `--done-marker`（`.train_rc` / `.retrain_rc`）mtime **晚于** watcher 启动 = 本次 attempt
    真结束 → 最后一次推图后退出。stale marker（前次 attempt 残留）不误杀续训 watcher。
  - 已推过点后 log 超过 `--max-idle`（默认 120s）无增长 → 兜底退出；**首点前不启用 idle**
    （首个 epoch 可能极慢，防误杀）。
  - log 文件未出现（wrapper 重定向稍晚）→ `--max-wait-log` 超时退。
- **fail-soft 铁律**：缺 env / orca.chart 不可用 / socket 断 → stderr 一次 + exit 0，绝不碰
  训练 rc / log / 进程（断更不轰炸）。

### 2. 其余 5 图按产数据节点分散（`scripts/` 确定性脚本，`|| true` 不阻塞）

| 图 | 推送节点 | 时机 |
|---|---|---|
| pareto / search_table / latency_dist | ns_run_search（新 Step 2.7） | 搜索成功后立即（不用等 retrain） |
| metrics_bar / compare_table | ns_retrain（新 Step 3.5） | 训练真正完成、进 Step 4 前（selected 坐标经 agent.md Jinja 注入） |
| loss 曲线 | live_loss_watcher | 训练中实时 |

- 脚本从 `ns_visualize/scripts/` 原样迁移（`_common.py` + 5 个 chart 脚本），按消费者分别
  拷入 `ns_run_search/scripts/`（_common/pareto/search_table/latency_dist）与
  `ns_retrain/scripts/`（_common/metrics_bar/compare_table）——遵循 ns_run_train/ns_retrain
  既有镜像 scripts 惯例；`$ORCA_AGENT_RESOURCES/scripts/` 定位 + validator 存在性检查。
- 3 个 agent.md 更新（description + 资源锚点 + 新 Step）；推图命令 `>/dev/null 2>&1 || true`
  保最终回复只含 emit_result 的输出。

### 3. yaml + 目录

- 删 `ns_visualize` 节点（agent 8 → 7）；`ns_retrain` executed 路由 → `$end`
  （`terminate_retrain_failed` 保留）；outputs 删 `visualization`；description 同步。
- 删 `workflows/agents/ns_visualize/` 目录（`loss_curve.py` / `generate_charts.py` /
  `report.py` 随 watcher 取代而删除）。

### 4. chart daemon TTL 6h → 72h

覆盖天级训练（用户拍板，非无限制——保留防泄漏兜底语义）。respawn 机制不变（bootstrap +
每次 `orca next` probe+spawn，socket 路径按 run_id 确定性派生，bind 同路径）。

## 测试

- `tars validate`：全 10 个 workflow yaml 0 error / 0 warning。
- `tests/compile + tests/workflows`：**717 passed / 3 skipped**（含新 watcher 单测 11 +
  chart scripts 测试 28，`test_ns_visualize_scripts.py` 改名 `test_ns_chart_scripts.py`）。
- `tests/iface/in_session` chart 相关：84 passed。
- **测试隔离修复**（新测试引入）：`test_ns_chart_scripts.py` 顶层 `from _common import ...`
  把 chart 版 `_common` 注册进 `sys.modules`，截胡后续 `test_workflow_viz_audit_fixes.py`
  P2-5 对 `_quant_scripts/_common` 的解析（同名跨目录模块）→ import 后立即
  `sys.modules.pop("_common"/"compare_table"/"latency_dist")` + 移除 sys.path 插入 +
  watcher 测试 `os.chdir` 改 `monkeypatch.chdir`（cwd 泄漏）。修复后全绿。
- 预存失败（与本次无关，git stash 验证）：in-session 8 项（用户 WIP `_drive_protocol`
  "请勿调用 orca next" 相关）。

## 已知边界

- chart daemon TTL 72h：超过 72h 的训练实时推送会断更（watcher fail-soft 静默退出，
  训练不受影响）；daemon 由下一次 `orca next` respawn 后，节点完成的图仍正常推。
- 实时曲线按 attempt 分 title（`... (attempt N)`）：self-heal 重跑保留各 attempt 独立曲线，
  同 attempt 内实时刷新。
