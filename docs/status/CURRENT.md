# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-27）：五议题方案设计 —— 辩论 workflow 已出方案，待用户拍板

5 议题经 24-agent 辩论 workflow（议题1 走 4 策略×3 评委 judge panel；其余各 2 视角审视）→ 5 份综合 spec → 5 路 spec-reviewer 对抗审视。方案 + review **全 conditional-pass**（无 fail、无纯 pass，每份都被抓到实施前必修 blocker）。**待拍板 4 个 P0 后进入修订 + 实现**。

> ⚠️ spec 当前在**临时 job 目录**（session 级，job 清理后失效）：`/home/mozzie/.claude/jobs/305ff919/tmp/specs/`。落地时建议迁入 `docs/specs/<topic>-design-draft.md`。

| # | 议题 | 推荐方案 | spec |
|---|---|---|---|
| 1 | workflow 被主 session 误停 | **S1 goal-gate 硬 Stop hook**（goal≡run 终态，零引擎/schema 改动，演进 cc_nudge.sh） | `stop/spec.md` |
| 2 | NAS 超网可视化 | Mode A（recharts 原生通道覆盖 V5/V6/V7）→ Mode B（`custom(kind=supernet)` + 第三 tab） | `supernet-viz/spec.md` |
| 3 | workflow 文档可视化 | ASCII/mermaid 分治，10 gap 排 P0–P7 | `doc-viz/spec.md` |
| 4 | workflow 普适性 | 定位 B：通用编排引擎 + DL-first 模板 + 新 vertical 按需 | `generalizability/spec.md` |
| 5 | 前端 web 优化 | Tier1 红线（ws 谎报 caught-up / viz 静默）→ Tier2 性能 → Tier3 超网 tab | `web/spec.md` |

汇总：`specs/SUMMARY.md`（完整 verdict/blocker/拍板清单）+ `specs/scout/`（4 份事实基线）。

**待用户拍板 P0**：① stop scope 是否追加 S3 无人值守自动化 ② gen 定位 B 须产品层 ratify ③ supernet Mode A 确认砍 Plotly 走纯 recharts ④ supernet Mode B ingestor P1 参数化 vs P2 并行新 ingestor。

---

## 必读文件（开工前按需）

- `specs/SUMMARY.md`（临时路径见上⚠️）
- [CHANGELOG](CHANGELOG.md)

---

> 上一任务「KD-NAS 并行 workflow 全流程」已完成（commits `e14f775`/`02b927b`/`902457d`/`ee44b4b`，CHANGELOG 2026-07-25 四条索引），已从本文件清理。
