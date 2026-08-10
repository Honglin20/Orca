# fix(nas-supernet): 图表链路修复——progress 每指标一图 / compare_table NaN / search 3 图 reuse 补推 / `_common` NameError

## 背景

真机跑 nas-supernet 全流程后，前端图表存在 5 类问题：

1. **progress_watcher 把 loss 和 acc 画进一张图**——用户希望每指标一图（且不假设指标名）。
2. **metrics_bar 的 "Acc Across Phases" bar 图看不懂**——acc 各阶段近饱和，等高 bar 无信息量，标题不清。
3. **compare_table 的 Full Supernet Acc 显示 `3.4e38G`**——search_results 有 NaN acc（float32 max），`max(vals)` 选中它。
4. **search 阶段无 pareto/搜索表/latency 分布图**——reuse 路径跳过 Step 2.7，chart 脚本从未执行。
5. **`ns_run_search/_common.py` 引用未定义的 `_read_objective`**——c211804 重构半成品，任何 chart 脚本一调用即 NameError。

## 改动

- **progress_watcher.py**（ns_run_train + ns_retrain 镜像）：`_push` 从「全量 series 一张 hue 图」改为
  「遍历 `metrics` dict，**每指标推一张独立图**」（title 带真实指标名 + y_label=指标名）。前端 dedup
  语义：同 label+title 实时替换、不同 title 独立图。零硬编码指标名（loss/acc 均非假设）。
- **metrics_bar.py**：bar → **table**（title `Acc by Pipeline Phase`，columns=[phase,value]，caption 中文
  解释 4 阶段含义）。Selected Arch 不再二次取负（select stdout 已是自然正值）。
- **compare_table.py**：Full Supernet Acc 改用**训练 log 真实 best 验证精度**（`best_val_metric_from_log`），
  不再用候选极值；train log 缺失 fallback「search best candidate」（min 负值 + 过滤 NaN）；Selected 不二次
  取负；caption 注明 full_metric 来源。
- **_common.py**（ns_retrain + ns_run_search 镜像）：方向判定过滤 NaN 哨兵（`abs<1e6`，兼容 reward/BLEU 等
  >1 指标；valid 空回退原逻辑）；新增共享函数 `best_val_metric_from_log` / `final_metric_from_json`（从
  metrics_bar 抽出复用）。ns_run_search 副本整体替换为已修版本（消除 `_read_objective` NameError）。
- **pareto.py / search_table.py / latency_dist.py**（ns_run_search）：NaN/溢出哨兵过滤（`abs<1e6` 正向包含，
  拦真 NaN）；search_table 的 latency 列也过滤；pareto 删未用 import。
- **ns_run_search/agent.md**：Step 0 reuse 分支内联推 3 个 chart 脚本（reuse 也出图，`|| true` fail-soft）。
- **nas-supernet.yaml**：修复 description 中 JSON 片段未转义双引号导致 YAML 解析失败。

## 测试

- 新增 `tests/workflows/test_progress_watcher.py`（每指标一图、空 series、render 失败 fail-soft、done 驱动退出）。
- `test_ns_chart_scripts.py`：新增 NaN 极性回归（含 3.4e38 混入 / >1 合法指标 / 全 NaN）+ compare_table
  fallback 与方向测试；import 指向已修副本；修复模块缓存污染。
- 验证：`tests/workflows` 相关 120 passed；ruff 干净；`tars validate` 通过。

## 真机验证

清 retrain ckpt 强制重训一轮：tape 确认 `Retrain Metrics (attempt N): loss` 与 `...: test_acc` 两张独立
line 图（数据完整），search 3 图 + compare_table + metrics_bar 全部推送。

## 已知限制

- 前端图表为 run 活跃期实时推送 + tape custom(chart) 事件持久化；run 终态后 Web UI 需从 tape 重建图表。
