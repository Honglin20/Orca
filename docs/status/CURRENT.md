# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

**当前任务**：Windows 原生兼容（chart TCP 分支 + in-session 锁 shim + CLI 修复）——sdd-loop 进行中

- **Spec**：`docs/plans/2026-09-08-windows-native-compat.md`（D1~D7 + 工作树对账 + 双环境验收；轮 2 复审中）
- **所处 Phase**：1（spec 评审环，轮 2 复审进行中）
- **spec 评审轮**：1（轮 1 CONDITIONAL_PASS + 5 项用户决策已拍板落盘）｜ **E2E 轮**：0 ｜ **全循环回退**：0
- **已拍板**：U1=A 补 in-session chart 改造；U2=A OpenProcess-only；U3=A 全 Windows 原生矩阵；U4=B 双环境分工（WSL 平台无关 + orca-win 真跑）；U5=B 三份锁不归并（follow-up）
- **重要状态**：spec 评审前已有**部分实现落在工作树未提交**（chart/_paths、chart_ingestor、_render、exec 两 executor、_flock.py 新建）——Phase 2 coder 须对账完成并全量 review，不得假设从零开始
- **验收口径**（用户拍板）：不跑完整 E2E；opencode + tars skill in-session 模式测 Windows；web REST 可见输出；WSL pytest 定向子集证明 Linux 不回归；不跑 TUI
- **Phase 2 进度（2026-09-09）**：coder 实现对账完成（D1~D7 + 6 已知 bug + code-reviewer 2 轮闭环，round2 CONDITIONAL_PASS 零必修；m-2/m-6 显式 defer）；WSL 子集绿（events/chart/web 非 playwright/script/exec；playwright 34 失败经 stash 基线证明 pre-existing）+ orca-win 真跑绿（in_session + bg_runner，4 residual 已分类：v3_step1/permission_hook/push_chain_smoke×2 pre-existing 或 D1-D7 外）；E2E 验收 1-3 待跑

**必读文件**（≤5）：
1. `docs/plans/2026-09-08-windows-native-compat.md`
2. `orca/chart/_paths.py`（D1/D2 端点单一真相源）
3. `orca/events/chart_ingestor.py`（D1 server 分支 + D3 熔断）
4. `orca/iface/in_session/_flock.py`（D4 shim，新建）
5. `.e2e_win/`（实测复现脚本与证据）

**实测根因**（2026-09-08，conda env `orca-win`）：AF_UNIX crash 循环饿死事件循环（90s 40 万次重起）；`orca` CLI 模块级 fcntl 崩；`tars ps/wait/logs` os.fork 默认参数崩；CLI GBK 乱码；CC hook 静默死（本轮 out-of-scope）。
