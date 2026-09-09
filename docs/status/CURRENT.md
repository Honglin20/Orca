# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

当前无进行中任务。

**最近完成**：Windows 原生兼容（2026-09-09，commits `4a00177` + `083cfa0`，release note：`docs/releases/2026-09-09-windows-native-compat.md`）——chart TCP 分支/flock shim/探活零杀伤/熔断限流/UTF-8；WSL 零回归 + E2E 验收 1-3 全 PASS。

**挂账小项（非阻塞，下次顺手）**：
- **in-session inline script 推 chart 自死锁**（跨平台既有，POSIX 同现）：`next` 持 tape flock 临界区内联跑 script → daemon `_FlockSafeTape.append` 等同锁；架构级专项待立项（复现证据 `.e2e_win/`）
- **CC hooks Windows 静默死**：注册命令 `bash`/`python3` 命中 Store stub——nudge/审批桥需 python 化 + `sys.executable` 绝对路径注册
- 三份 msvcrt 锁实现归并（`web_registry`/`_project`/`_flock`）；pidfile 镜像名校验；opencode→tars skill 自动编排全链补跑待 deepseek 余额
- Windows 上 ScriptNode 走 cmd.exe，`$VAR` 不展开——product gap 待立项
- **构建流程缺口**：前端源码变更未强制重建 static——考虑提交流程加检查
- HEAD 既有 pytest 失败 3 个：`test_gate_node_sh_parses_after_quote_fix`（陈旧断言）、`test_baseline_chain_*` ×2
- **测试卫生债**：部分 tests/iface/web 套件不隔离 ORCA_HOME/注册表——根治 = conftest autouse 隔离
- 仓库根 `_e2e_playground` MSYS 破损 symlink 阻断 Windows python 跑全量 pytest（环境残留，建议移出或换 junction）
