# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

**当前任务**：prof-opt v8（同基前沿探索）——代码已实现并提交，**E2E 实测进行中**

- 已完成：SPEC（`docs/specs/prof-opt-v8-spec.md`）+ 实现 + 282 green + tars validate 零 warning + code-reviewer 自检（3 major + 5 minor 全部修复，含 gate_node stdout 泄漏真 bug）
- release note：`docs/releases/2026-09-10-profopt-v8-frontier.md`
- **进行中**：E2E（WSL in-session：claude -p 驱动 tars skill + orca CLI，target 项目，max_rounds=2，epoch 帽 10）——启动脚本 `.e2e_scratch/e2e_v8_launch.sh`
- 已拍板（用户 2026-09-10）：①永不晋升 ②被支配变体照旧放行 ③历史 = 机械层 + frontier 派生快照
- 注意：`playground/target/artifacts/prof-opt` 旧工作区 2026-09-10 11:33 被外部清除（非本会话），E2E 为干净首跑

**挂账小项（非阻塞，下次顺手）**：
- **in-session inline script 推 chart 自死锁**（跨平台既有）：架构级专项待立项
- **CC hooks Windows 静默死**：需 python 化 + `sys.executable` 绝对路径注册
- 三份 msvcrt 锁实现归并；Windows ScriptNode `$VAR` 不展开；前端 static 未强制重建
- HEAD 既有 pytest 失败 2 个：`test_baseline_chain_*` ×2（本次 review 顺手修活了 `test_gate_node_sh_parses_after_quote_fix`）
- **测试卫生债**：部分 tests/iface/web 套件不隔离 ORCA_HOME
- 仓库根 `_e2e_playground` MSYS 破损 symlink 阻断 Windows 全量 pytest
