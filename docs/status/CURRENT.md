# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：nas-supernet latency 单位透传 + full-supernet 真测量 + 子网结构展示——已提交（v1+v2+v3）

**任务**：用户反馈 4 个真机问题——① latency 单位被锁 ms（用户 µs 测度脚本被错标）；② compare_table Full Supernet latency 是 `max(候选)` 代理（"FP32 上限"）；③ latency_dist 别的服务器全 0；④ 选定子网结构从未展示。

**状态**：**已提交**。SPEC（spec-reviewer 12 项闭环 Pass）→ coder 实现 → 洁净度审查（逐 agent.md 受众翻转通读 PASS）→ `.py` SPEC breadcrumb 清零 → v3（v2 翻译）独立洁净审查 PASS（2 处 `(C3/C4)` lint 残留已清）。`test_ns_chart_scripts.py` 81 passed + yaml 语法 OK + ruff 干净。

**关键决策**：① 单位"声明不换算"——新增 `latency_unit` 输入端到端透传（默认 ms 向后兼容）；② F1 bootstrap 不变量：`latency_unit∈{us,s}` 必须搭配 `latency_script_path`（默认 estimator 恒 ms），否则 fail-loud；③ 子网展示用 `str(subnet)` module repr + 逐层结构化表；④ v3（ns3_*）是对 v2 的翻译，用户确认一起解决、同标准纳入提交。

**必读**：release note `docs/releases/2026-08-11-nas-supernet-latency-unit-and-subnet-display.md`；SPEC `docs/specs/2026-08-11-nas-supernet-latency-unit-and-subnet-display.md`；CHANGELOG 顶部索引。

**遗留**：真机 E2E（in-session headless，`latency_unit: us` + 用户 script 验 4 图 label=us / compare 真测量 / `subnet_structure.md` / A6 fail-loud）未跑——属 test-agent 范围。

---

> 历史任务记录见 `CHANGELOG.md`（索引）+ 各 `docs/releases/*` release note。本文件仅保留当前任务快照。
