# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：nas-supernet latency 单位透传 + full-supernet 真测量 + 子网结构展示——已提交（v1+v2+v3）

**任务**：用户反馈 4 个真机问题——① latency 单位被锁 ms（用户 µs 测度脚本被错标）；② compare_table Full Supernet latency 是 `max(候选)` 代理（"FP32 上限"）；③ latency_dist 别的服务器全 0；④ 选定子网结构从未展示。

**状态**：**已提交**。SPEC（spec-reviewer 12 项闭环 Pass）→ coder 实现 → 洁净度审查（逐 agent.md 受众翻转通读 PASS）→ `.py` SPEC breadcrumb 清零 → v3（v2 翻译）独立洁净审查 PASS（2 处 `(C3/C4)` lint 残留已清）。`test_ns_chart_scripts.py` 81 passed + yaml 语法 OK + ruff 干净。

**关键决策**：① 单位"声明不换算"——新增 `latency_unit` 输入端到端透传（默认 ms 向后兼容）；② F1 bootstrap 不变量：`latency_unit∈{us,s}` 必须搭配 `latency_script_path`（默认 estimator 恒 ms），否则 fail-loud；③ 子网展示用 `str(subnet)` module repr + 逐层结构化表；④ v3（ns3_*）是对 v2 的翻译，用户确认一起解决、同标准纳入提交。

**必读**：release note `docs/releases/2026-08-11-nas-supernet-latency-unit-and-subnet-display.md`；SPEC `docs/specs/2026-08-11-nas-supernet-latency-unit-and-subnet-display.md`；CHANGELOG 顶部索引。

**遗留 / 待办**：
- ℹ️ **v3 P0 后修（已推送 `0ca1b3b`）**：2b20663 提交的 v3（`ns3_search_pipeline/agent.md`）schema-gen 带与 v2 同根的 SyntaxError（`elif all isinstance`）+ `>file` 截空 + `latency_ms_field` 错值——洁净审查未逮功能性 bug（v3 跑起来同样 "missing latency_unit"）。已移植 v2 修法（isinstance 修正 + write-on-success + `__name__` + fail-loud + 值 `'latency'`）修复并推送。
- [x] **sentinel `full_supernet_latency.py`（ns/ns2/ns3）已提交 `5265e5c`**：2b20663 的 `default=""` 让 v1/v3 us-runs 回归；改 `default=None` sentinel（None→保旧行为 / 显式空串→强制 ms），三份 byte-identical + 81 测试过。
- [x] **Task 2（引擎）`latency_unit` 输入枚举**：`InputDef.enum` 字段 + 三层校验（schema 加载期 / cli bootstrap / orchestrator 镜像）+ 三版本 yaml 应用。spec-reviewer 12 项闭环（B1/B2/B4/B7 落地，B3/D8 defer）。`7b120ee`（framework+tests）+ `131b294`（yaml），25 测试覆盖 AC1-AC8，`tars validate` 0 error / 0 warning。详见 release note `docs/releases/2026-08-11-inputdef-enum.md`。
- [x] **v2 P1 两项已提交 `a57190b`**：① ns{,2,3}_run_search detach 改 setsid（PGID==leader，leader 自记 .search_pid）+ HEAL 死机 `kill -- -<pgid>` 杀整组——根治旧 `nohup`+`kill $!` 只杀 wrapper、python 搜索被 reparent 占 GPU 的 orphan；WSL 实测不跨 run/项目、不碰 chart daemon。② ns{2,3}_report `charts_summary` 改扫 `charts/`（静态文件真落处）+ 缺静态时回落 `.nas-supernet_charts.jsonl` marker，修漏扫 + 误抓 `runs/retrain/test_metrics.json`。验证：两 report heredoc py_compile clean + pytest 81 passed。
- [x] **S4a 三项小修已提交 `d768879`**：⑧ eta 负值→`max(0,...)` clamp（6 份）/ ⑦ pareto 冗余外层条件删除（3 份）/ ⑥ monitor grep 收紧（bare `error`→`error[:]`、加 traceback/exception、runtime*error 捕 RuntimeError；6 份）。行为对比 ⑥：old 3 false-pos+4 false-neg → new 0/0。
- [ ] **S4b 三项 SDD**（SPEC `docs/specs/2026-08-11-v2-audit-attribution-resume-ckpt.md` + spec-reviewer conditional-pass，待修订 3 Blocker Q1/Q2/Q5-Q6 后实现）：③ expand 归因 marker 化（**仅 v2+v3**，v1 无 report 节点；加 Step 0 rm 协议防残留）/ ④ Step R 两分支重建 N（在跑=latest-mtime 号 / 已死=max+1，禁一刀切 max+1 误杀在跑搜索）/ ⑤ ckpt 相对路径核验（status.sh+emit_result.py 已绝对路径防御，疑已修 → verify-and-close）。
- [ ] 真机 E2E（in-session headless `latency_unit: us` + 用户 script → 4 图 label=us / compare 真测量 / `subnet_structure.md` / A6 fail-loud）——属 test-agent 范围。

---

> 历史任务记录见 `CHANGELOG.md`（索引）+ 各 `docs/releases/*` release note。本文件仅保留当前任务快照。
