# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

当前无进行中任务。

**最近完成**：web 列表性能止血 + 治本 P1~P4（2026-09-08，release note：`docs/releases/2026-09-08-web-list-perf.md`，plan：`docs/plans/2026-09-08-web-runs-list-perf.md`）——后端改动需**重启 `tars serve`** 生效。

**挂账小项（非阻塞，下次顺手）**：
- **构建流程缺口**：39f9214 改前端源码未重建 static（静态产物靠独立 build 提交，易漏）——考虑提交流程加「前端源码变更 → 必须 build」检查或 CI 化
- HEAD 既有 pytest 失败 3 个：`test_gate_node_sh_parses_after_quote_fix`（陈旧断言）、`test_baseline_chain_*` ×2（状态机 failed≠running）
- run 卡片 chart_count 口径含 docs 清单
- web 列表性能挂账（见 release note）：窗口态拓扑补偿（overview 需 capture topology）、q/status 服务端下推、P2 terminal 尾窗竞态的显示级陈旧（已显式化取舍）
- **测试卫生债**：部分 tests/iface/web 套件不隔离 ORCA_HOME/注册表——跑全量即往真实 runs 写 demo/slow 残留 + 往真实注册表写 /tmp 项目（本批已三度清理）；根治 = conftest autouse 隔离
- prof-opt 首次真机 in-session 短跑时人工核验：第二次 docs 推送后面板内容仍可读、`rounds/<NNN>/<vid>/shadow` 存在（单测盲区，见 2026-09-08 release note）
