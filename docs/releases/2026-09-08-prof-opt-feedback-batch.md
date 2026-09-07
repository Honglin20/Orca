# Release Note：prof-opt 反馈批（web UX + 文档内容通道 + rounds 留档）

- **日期**：2026-09-08
- **Commit**：`39f9214`（分支 `puzzle-supernet`）
- **来源**：用户 2026-09-07 八条使用反馈；SDD loop 交付（spec 评审 2 轮收敛 → 实现内环 2 轮闭环 → 独立验证 PASS）
- **SPEC**：[`docs/specs/2026-09-07-profopt-feedback-batch-spec.md`](../specs/2026-09-07-profopt-feedback-batch-spec.md)

## 背景

部署事实（用户确认）：`tars serve` 与 workflow 常态**异机**，run 产出的 artifacts 只在跑 workflow 那台机器的本地盘上。此前文档面板能看到清单（走 chart 事件）但点开必 404（走本地文件系统读取）。

## 交付内容（对应八条反馈）

| 反馈 | 交付 |
|---|---|
| #1 模型文件只在 variants 下 | 新增 `_po_scripts/archive_round_shadow.py`，挂在 `gate_node.sh`（deploy 校验后、promote 前）：每轮 gate 时把各 variant 的 `shadow/` 源码幂等留档到 `rounds/<NNN>/<vid>/shadow/`（仅源码，排除 `__pycache__/*.pyc`；tmp+rename 原子；失败落 `shadow_archive_error.json` + stderr，不阻断 gate） |
| #2 docs 清单表没必要推送 | 图表区整组滤除 `prof-opt/docs`（valid + huge 两分支；docs-only run 显示诚实空态）。**推送本身不停**——清单是文档面板的数据源 |
| #4 异机文档 404（主修复） | 文档内容随清单走既有 chart 事件通道：`push_curves --docs` 行新增 `content` / `content_omitted` 字段（单文档 256KB 上限 + 整包 1.5MB utf-8 聚合预算；`ensure_ascii=False` 防中文膨胀击穿 2MB 通道；per-run state 文件只推变更、仅成功发送后写回、预算截断行不入 state）。前端合并 selector 跨推送按 path 补最近非空 content → **任何机器的浏览器都能读文档正文，零文件系统依赖**。辅修复：`resolve_artifacts_root` 回退读 run 目录 `orca_env.sh` 记录的 `ORCA_ARTIFACTS_DIR`（同机 project-scoped 场景） |
| #5 文档独立 TAB + 图标管理 | 「文档」独立页签；面板重构为按组（基线/变体/轮次/规则）图标卡片网格（文件类型图标 + 状态点），点卡片看正文 |
| #6 单图过大 | 组内唯一图表 max-width 560px，多图保持既有自适应网格 |
| #7 改进呈现太分散（拍板：只留帕累托图） | 帕累托图补 baseline 锚点行（x=0；gap basis 下 y=0，metric basis 下 y=基线最新 metric，色 `#0ea5e9`）——「相对基线」有原点可对 |
| #8 节点 R 数字虚高 | R 改为「节点自身执行次数」（node_started 去重 session 数，仅非空 session_id 计入）：po_propose 显示真实轮数，子代理数继续走「N subs」；in-session 无 session_id 的路径诚实回退旧口径 |
| #3（疑问，无改动） | 已答：帕累托图用自带 `pareto` chart type；verdict distribution = 各 variant 最新终局 outcome 计数柱状图（保留不动） |

## 披露与遗留

1. **A1 信任边界**：`orca_env.sh` 记录根与 run 目录同信任域（均由引擎 bootstrap 写入），无新增攻击面；越界/symlink/二进制守卫不变，双根绝对路径不泄露进响应（有测试钉死）。
2. **单测盲区（首跑人工核验一次）**：多次 1.5MB 推送累计可推 run 过 5MB huge 阈值（详情页全量加载变慢，不丢数据）；内容通道跨 run state 作用域。首次真机 in-session 短跑时核对：第二次推送后面板内容仍可读、`rounds/<NNN>/<vid>/shadow` 存在。
3. **HEAD 既有失败（非本批，已 HEAD~1 对照实证）**：`test_gate_node_sh_parses_after_quote_fix`（陈旧断言）、`test_baseline_chain_*` ×2（状态机 `'failed' != 'running'`）；playwright 浏览器用例 33 个本环境未跑（二进制在场，可运行性未证实）。
4. **后续小项**：run 卡片 chart_count 口径含 docs 清单（本批范围外，已记录）。

## 验证证据

- 后端 pytest（WSL `.venv`）：`tests/test_po_scripts.py` + `tests/test_web_artifacts_docs.py` → **121 passed**（3 失败为 HEAD 既有）
- 前端：vitest **635 passed（36 文件）** + `tsc --noEmit` clean
- `bash -n gate_node.sh` ok；C1 脚本另做生产方式直驱实测（幂等 / 排除字节码 / error 文件 / 恒 exit 0）
- SPEC §1-§8 验收标准逐条映射具名测试并定点复跑通过；证据日志：`%TEMP%/orca-verify-39f9214/`
