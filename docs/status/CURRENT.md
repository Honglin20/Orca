# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

**当前任务**：prof-opt v8（同基前沿探索）——实现/审查/提交完成，E2E 全链验证**待有卡宿主**

- 已完成：SPEC + 实现 + 282 green + tars validate 零 warning + code-reviewer 自检（3 major + 5 minor 全修，含 gate_node stdout 泄漏真 bug）；commits `1694e4b` + `291d7d4`
- release note：`docs/releases/2026-09-10-profopt-v8-frontier.md`
- **E2E 结论（run `prof-opt-20260910-130812-1af1ea`，tars skill + orca 真链）**：flatten 即 fail loud——本机 WSL 无 NPU/CUDA（torch 2.13.0+cpu），resolver enum npu|cuda 按设计无 CPU 回退；失败路径全链合规（诚实报告/零写回/web 面板）。**v8 轮环实战验证待有卡宿主重跑**（同 inputs，max_rounds=2 + epoch 帽 10；启动脚本 `.e2e_scratch/e2e_v8_launch.sh`，注意 WSL 需原生 claude：`~/miniconda3/bin/claude`）
- 2026-09-11 追加：baseline chain 新增必填 `--accuracy-budget`（anchor 冻结内联进 chain，见 CHANGELOG 同日条目）——重跑 E2E 时无需改启动脚本（inputs 不变，agent 按 agent.md 传参）
- 已拍板（用户 2026-09-10）：①永不晋升 ②被支配变体照旧放行 ③历史 = 机械层 + frontier 派生快照
- **v8.1 追加（2026-09-10，commit `dff5456`）**：po_propose 叙事层 history digest——`history-curator` 子代理每轮收尾重写 `base/history_digest.md`（跨轮因果，替代上轮 analysis.md 直读）+ `digest_stamp.py` 机械戳（Step 0 校新鲜 stale fail loud、emit gate 纳 seal 入磁盘契约）。实现+独立审查（1 MAJOR 3 minor 全修）+ 洁净三件套全过；po 全套件绿。release note：`docs/releases/2026-09-10-profopt-history-digest.md`。**随 v8 轮环 E2E 同车验证**
- 注意：`playground/target/artifacts/prof-opt` 旧工作区 2026-09-10 11:33 被外部清除（非本会话）

**挂账小项（非阻塞，下次顺手）**：
- **in-session inline script 推 chart 自死锁**（跨平台既有）：架构级专项待立项
- **CC hooks Windows 静默死**：需 python 化 + `sys.executable` 绝对路径注册
- 三份 msvcrt 锁实现归并；Windows ScriptNode `$VAR` 不展开；前端 static 无强制重建机制（2026-09-11 已手动重建）
- ~~HEAD 既有 pytest 失败 2 个~~：`test_baseline_chain_*` ×2 已于 2026-09-11 修活（`test_gate_node_sh_parses_after_quote_fix` 仍挂）
- **测试卫生债**：部分 tests/iface/web 套件不隔离 ORCA_HOME
- 仓库根 `_e2e_playground` MSYS 破损 symlink 阻断 Windows 全量 pytest
