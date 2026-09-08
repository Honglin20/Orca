# 2026-09-08 web 列表性能治本计划

> 状态：**已实现**（P1~P4 全部落地 + code-reviewer 2 轮修订，见文末「review 修订记录」）。止血清理已完成（2026-09-08，删 1612 残留 run + 2045 死注册条目，备份在 `.e2e_scratch/runs-cleanup-backup-20260908.tar.gz` 与 `~/.orca/projects.json.pre-perf-clean-20260908.bak`）。

## 背景（实测基线，win32 + WSL serve + /mnt/d drvfs）

| 指标 | 清理前（1626 runs + 2045 死注册条目） | 清理后（14 runs + 4 条目） |
|---|---|---|
| `GET /api/runs?scope=all` | 1.7~3.9s / 460KB | 0.04~0.05s / 4.3KB |
| `GET /api/projects/stale` | 424KB（=2045 条死项目记录） | 294B |
| `GET /api/workflows` | 冷 3.2s（catalog 全扫）/ 热 0.09s | 同左（缓存有效） |

慢的根因是**成本随数据线性增长且落在每请求路径上**，清理只是归零当前数据，结构不变会再长回来。

## 根因

1. `discover_runs()`（`orca/iface/web/run_manager.py:1450`）每请求：全注册表逐条目 `is_dir` + 每个 run 文件一次 stat（drvfs 单次 1~3ms × N）。持久 meta-cache 只省 tape fold，**不省 stat 校验**。
2. `scope=all` 无分页默认（`orca/iface/web/routes/runs.py:45`）：全量返回所有 RunSummary，响应体随 run 数线性膨胀。
3. run 详情首屏全量路径：`GET /api/runs/<id>/events` 全量 replay，前端全量 fold + 渲染。huge-tail 窗口机制已存在（`workflow-store.ts:280` tail + `?since=&limit=` 增量 prepend，`loadEarlierChunk`/`loadFull` 已预留）但仅 huge run 生效。
4. （次要）catalog 冷扫 3.2s 落在 server 重启后首个用户请求上。

## 改动项

### P1 `scope=all` 默认分页
- 后端：默认 `limit=200`，显式 `?limit=` 覆盖；**稳定排序**（按 `started_at` desc，legacy run 无时间戳兜底 0）——当前返回是扫描序，无稳定排序的分页会重复/丢行，排序键是本项前置。
- 前端：`run-list-store` 滚动加载更多（offset 递增），到底提示。
- 消费面已核实仅 web 前端（无 CLI/MCP 调用方）；测试同步修订。

### P2 meta-cache 校验 O(N) stat → 目录级粗判
- runs 目录 stat 一次：目录 `(mtime,size)` 未变 → 信任缓存跳过逐文件校验；变了才全量校验。
- **live 例外**：attached/in-memory live run 的 tape 在追加（只改文件 mtime，不改目录 mtime）→ live 集合仍逐文件 stat（小集合，通常 ≤ 个位数）。
- 失配方向保守：粗判漏报（同窗口增删）→ 退回全量校验，正确性不受损。
- 注册表死条目自动 prune：**不做**（YAGNI；已手动清理，产品语义另议）。

### P3 run 详情首屏窗口化
- 所有 run 首屏默认 tail 窗口（目标 ~500 事件，具体值实现时按体验定），复用既有 huge-tail 机制；`loadEarlierChunk` 接上 UI（向上翻页加载更早事件），`load full` 保留。
- huge 判定保留（超大 run 自动进 huge 模式）。

### P4 冷启动预热（小）
- `create_app` lifespan startup 派一次性后台 task：`discover_runs()` + catalog `_catalog_entries()` 各跑一遍，把冷扫成本移出用户首访。失败 log warning 不阻断（预热是优化不是依赖）。

## 验收

- 模拟 1 万 run 目录：`scope=all` 首屏（默认 limit）warm <100ms。
- 打开大 tape run：首屏网络面板只见 tail 窗口请求；"加载更早"逐步回溯。
- 既有 pytest / vitest 全绿（断言随契约同步修订，不静默放宽）。

## 涉及文件（预估）

`routes/runs.py`、`run_manager.py`、`frontend/src/stores/run-list-store.ts` + 测试；`frontend/src/stores/workflow-store.ts` + 相关组件；`server.py`（P4）。

## 不做

- tape 压缩/归档存储、runs 目录分片
- 注册表自动 prune（见 P2）

## review 修订记录（code-reviewer 2 轮，2026-09-08）

- **P1 翻页改 keyset 游标**（`before_ts`/`before_id`）替代纯 offset——offset 在两次翻页之间发生删除时会跳行，游标免疫；`offset` 参数保留向后兼容。
- **P3 补偿通道（MAJOR-1，方案 A）**：后端 `/meta` 对**所有** run 返回 `overview`（原 gate 在 huge；`overview_data` 本就对每个 run 算过，零额外成本）；前端窗口态一律启用 `serverOverview`（selector 门去掉 `huge` 前提）+ `workflowName` 由 overview 补偿（seq 1 的 workflow_started 必在窗外）。**未做（挂账）**：窗口态拓扑补偿（AgentsRail 分组/DAG 退化 flat，加载全部后恢复）——需 overview schema 扩展 capture topology。
- **P3 fallback 回整（MAJOR-2）**：`loadFromEvents`（WS resume-fallback 全量重拉）回整窗口簿记（hugeFullyLoaded=true / oldestSeqInWindow=1 / serverOverview=null）；`loadEarlierChunk` 合并加数组级 seq 去重保险。
- **P2 terminal 陈旧显式化（MAJOR-3，取舍 (a)）**：终态写入与 chart ingestor 尾部追加的竞态窗口（亚秒级）可能固化略旧的 chart_count/event_count，至该 runs/ 下任一 tape 增删自愈——接受并在 `run_manager.py` 注释披露；根治方向是 ingestor 终态屏障而非缓存。
- **P1 失败可见（MAJOR-4）+ epoch 守卫（MINOR-1）**：`loadMore` 失败写 `loadMoreError` 单开关（UI 行内提示，不复用全局 error 语义）；`reset` 后迟到响应不写回。
- **P1 范围提示（MAJOR-5，部分）**：`hasMore` 时零命中行提示「仅搜索已加载的 N 条」+ footer「共」改「已加载」；q/status 服务端下推未做（挂账）。
- **P4 warmup task**：shutdown `cancel()` 后 `await`（防 loop 关闭噪音）。
