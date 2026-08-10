# Release Note: nas-supernet 图表正确性修复 + prompt 洁净 + train/retrain 监控改造 + search 无上限自愈

> 2026-08-10。SPEC `docs/specs/2026-08-10-nas-supernet-chart-cleanliness-monitoring.md`（两轮 spec-review pass）逐字实现。

## 改动点

### A 图表正确性（6 项）

| ID | 文件 | 改动 |
|---|---|---|
| A1 | `ns_run_search/scripts/search_table.py` | 无 pareto 字段 / 全行 pareto=no 时降级展示全部去重 + **title 切换**（"Pareto Front"↔"All Architectures (no Pareto labels)"），不空表不误报 skip_reason |
| A2 | `ns_run_train/scripts/progress_watcher.py` + `ns_retrain/scripts/progress_watcher.py` | 抽 `_drain(progress_path, offset, tail, series) -> (new_offset, new_tail, new_point)` helper；done-marker 退出前先 drain 末点（收敛最关键数据不再丢）；§2c 复用同一 helper（DRY） |
| A3 | `ns_retrain/scripts/metrics_bar.py` | caption "640" → `len(records)` 动态 |
| A4 | `ns_retrain/scripts/_common.py` + `ns_run_search/scripts/_common.py` | `best_val_metric_from_log` 加 `if not math.isfinite(val) or abs(val) >= 1e6: continue`（`math.isfinite` 拦 NaN/inf——`abs(NaN)>=1e6` 恒 False 是 IEEE-754 陷阱） |
| A5 | 删 `ns_run_train/scripts/live_loss_watcher.py` + `ns_retrain/scripts/live_loss_watcher.py` + `tests/workflows/test_live_loss_watcher.py` | 死代码清除（已被 progress_watcher 取代） |
| A6 | `ns_run_search/agent.md` Step 0/2.7 + `ns_retrain/agent.md` Step 3.5 | `>/dev/null 2>&1 \|\| true` → `> /dev/null \|\| true`（删 `2>&1`，stderr 不再被吞，共 8 处） |

### B prompt 洁净（5 项）

| ID | 文件 | 改动 |
|---|---|---|
| B1 | `supernet-evaluator.md` + `ns_expand_supernet/agent.md` Step 5 | 删 `.agents/skills/expand-to-supernet/` 死路径；caller bash 确定性写 `.supernet_specs_dir` marker（`printf '%s\n' "$ORCA_AGENT_RESOURCES/references/supernet_specs" > ...`），subagent 双路取 specs 目录（primary=prompt `<specs_dir>`，fallback=marker 文件）|
| B2 | `ns_run_search/agent.md` | 删 SPEC breadcrumb |
| B3 | `ns_retrain/agent.md` | 删跨节点 §3 引用 |
| B4 | `ns_search_pipeline/agent.md` | 删 `pytorch_latency_utils.py:94` 第三方库源码定位 |
| B5 | 合并入 C rewrite | — |

### C train/retrain 监控改造（CRON → 有界轮询 + 无上限自愈）

**C1 新增 `monitor_until_done.sh` ×2 镜像**（ns_run_train + ns_retrain，仅路径前缀不同）：
- cheap 活性（kill -0 + rc 文件，60s/次，进程活着不调 status.sh/不 torch.load）
- 进程退出才委托 status.sh 完整判定
- 后缀通配 `*COMPLETE*`/`*INCOMPLETE*`/`*STUCK*` 同时匹配 TRAIN_*/RETRAIN_*
- `set -u` 全分支 echo+exit 0（永不空 stdout）
- NaN/error 词法 suspect + log 停滞检测（`ORCA_TRAIN_STALL_POLLS` 默认 3=3min）
- STUCK regex 加通用 `error` token + 尾锚容忍 `[:.:]`（NEW-5）

**C2 agent.md 决策树改造**（ns_run_train + ns_retrain 镜像）：
- 删 frontmatter `cron` 工具（ns_retrain 保留 `write`）
- Step 3f 注册 CRON → C-loop（K=6 monitor 块/turn）+ HEAL-LOOP（无上限自愈，每 turn ≤2 轮 then C-end）
- 铁律 3 改述无上限；铁律 7 改述 monitor 块 ≤ bash 工具上限
- 唯一 failed = 禁碰-blocked（reason 进 `.ns_run_*_assessment.txt`，**不加 failed-marker**）
- 可续接契约（fresh sub-agent 经 Step 1 status.sh 续接）
- OOM→禁碰 hint（v2-3）

**C2 launch.sh 改动**（两份镜像）：
- 删 N>3 cap + `ATTEMPT_BUDGET_EXHAUSTED`
- rm 列表补 `.train_rc`/`.retrain_rc`（NEW-1：防 resume 时 stale rc 旁路 cheap 活性）

**C2 emit_result.py**：status 推导逻辑零改（N1：failed 走现有 else 分支）

**C3 nas-supernet.yaml**：description + 节点级注释 + status desc 全清 CRON/3次措辞

**NEW-2 update_status_md.sh**（两份）：echo 文本 "CRON 到点自检" → "monitor 轮询/turn 到顶换 sub-agent"

### D ns_run_search 无上限自愈

- D1：删 `max_retries=3`/"最多 3 次"/"N=1..3"/"N>3 放弃" → 无上限直到 `search_results.jsonl` ≥1（rc=0）
- D2：failed 仅留缺上游 supernet ckpt / 禁碰-blocked
- D3：新增 Step R resume guard（搜索在跑 → 跳 Step 0/1 直进轮询）+ Step 0 reuse-check 加固（pid 死 + rc 存在前置）+ turn 预算续接契约
- D4：mid-search 发散检测指引（warmup 后 objective NaN/stall → kill+HEAL）
- D-GAP-1：Step 3 内联 python 硬编码 `attempt3` → glob 最新 attempt log
- D-GAP-2：Step 0 reuse 加 `.search_pid` 死 + `.search_rc` 存在前置（防 incremental jsonl mid-flight reuse 残缺候选）

## 偏离 SPEC 记录

- **B5 先于 C 落地后合并**：ns_retrain FOREIGN 分支步号笔误在 C rewrite 时自然消解（HEAL-LOOP 段重写了 FOREIGN 措辞）
- **MUST-FIX 1（failed-marker 矛盾）**：以 §3 为准——不加 `.ns_run_train_failed.txt` marker；禁碰 reason 进 `.ns_run_*_assessment.txt`，emit_result status 逻辑零改

## 验证

- `tars validate workflows/nas-supernet.yaml` → 0 error / 0 warning
- 全部新/改 bash `bash -n` 通过
- ruff 干净（改动的 .py）
- grep 门全过：`cron`（agent.md/yaml 清零）/ `ATTEMPT_BUDGET_EXHAUSTED`（清零）/ `3 次\|尝试预算\|≤3\|N>3\|max_retries=3`（清零）/ update_status_md CRON（清零）/ `.agents/skills` supernet-evaluator（清零）
- tests/workflows 全量 559 passed + 3 skipped（pre-existing skips）
- 新增 41 测试（A1 四场景 + A2 drain + A4 sentinel + C1 结构 + C2 静态门 + 镜像同步）

## 待 test-agent E2E

- §4.3 headless 真机 E2E（playground/mnist_kd + opencode executor）
- B1 E2E 断言（supernet-evaluator report 体现真加载了 general_specs.md）
