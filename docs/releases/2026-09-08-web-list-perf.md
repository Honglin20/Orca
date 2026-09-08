# 2026-09-08 web 列表性能：止血清理 + 治本 P1~P4

## 背景

用户报告 web UI（7428）加载极慢：列表页 1.7~3.9s、点进已完成 profiling run 卡顿。实测定位两条独立慢路径（plan：`docs/plans/2026-09-08-web-runs-list-perf.md`）：

1. `/api/runs?scope=all` 每请求全量扫所有注册项目的 runs 目录（逐文件 stat，drvfs 1~3ms/次；当时 1626 runs + 2045 条死注册条目）→ 每请求 1.7~3.9s / 460KB。
2. run 详情首屏全量 tape replay（huge 阈值 5 万事件/5MB，prof-opt 多轮 docs/chart run 根本够不着 → 永远全量）。

## 止血（数据清理，可回滚）

- 删 1612 个测试残留 run（demo 847、`__probe__` 606、linear/cyclic/slow/p13_*/po-probe 等），保留 prof-opt/puzzle-supernet/mxint_analysis/nas 共 14 个。备份：`.e2e_scratch/runs-cleanup-backup-20260908.tar.gz`。
- 注册表 2049 → 4 条（删 2045 条 /tmp/pytest 死条目 + 复清 pytest 全量套件二次污染）。备份：`~/.orca/projects.json.pre-perf-clean-20260908.bak`。
- 效果：`scope=all` 1.7~3.9s → **0.05s**；`projects/stale` 424KB → 294B（424KB 即死项目记录）。

## 治本（P1~P4）

- **P1**：`scope=all` 稳定排序 `(started_at, run_id)` desc + 默认 `limit=200`（显式覆盖，`<0` 不限制）+ keyset 游标 `before_ts`/`before_id`（offset 删除场景跳行，游标免疫）。前端 run-list-store 头部权威刷新（保留已加载更早页）+ 「加载更早的 run」按钮 + 失败可见（`loadMoreError`）。
- **P2**：`discover_runs` 目录指纹粗判快速路径——tape 文件名集合 sha1 命中即零 per-file stat 直构；**非 terminal run 仍逐文件验证**（tape 追加不改目录内容，粗判看不见）；指纹刻意不含目录 mtime（flush 缓存 os.replace 会自失效）。已知取舍：terminal 尾窗竞态的显示级陈旧（亚秒窗口、自愈有界，注释披露）。
- **P3**：run 详情首屏一律 `?tail=500`（后端 O(窗口) 反向扫描）；窗口态一律启用 `serverOverview` 补偿（/meta 对所有 run 返回 overview；selector 门去 huge 前提；workflowName 由 overview 补偿）；「加载更早的事件」条带 + loadEarlierChunk gate 放开到窗口态；`loadFromEvents`（resume fallback）回整窗口簿记。
- **P4**：lifespan startup 后台预热 catalog + runs 索引（事件循环线程内，单线程前提不破）——重启后首访不再吃 3.2s 冷扫。

## 测试

- 后端：`tests/iface/web/test_web_perf_p1p2.py` 新增 8 测（排序/默认分页/游标走页/粗判零慢路径/非 terminal 追加感知/新增 tape 失效）；`test_attach.py` overview 契约同步；受影响套件 105 passed。全量 web 套件 22 failed 均为 Playwright 环境问题（干净树 stash 复现，与本批无关）。
- 前端：vitest **643 passed**（+8：分页合并/游标/失败可见 + huge-mode 新契约同步 + MAJOR-1/2 意图钉）；`tsc --noEmit` 干净；静态产物已重建。

## 已知边界（挂账）

- 窗口态拓扑补偿未做：AgentsRail 分组/DAG 在窗口态退化 flat，「加载全部/加载更早到顶」后恢复——需 overview schema 扩展 capture topology。
- q/status 搜索仍为客户端过滤（只覆盖已加载页，零命中已有范围提示）；服务端下推未做。
- 测试卫生债：部分 web 套件不隔离 ORCA_HOME/注册表，跑全量即污染真实 runs + 注册表（本批已三度清理）；根治 = conftest autouse 隔离。
