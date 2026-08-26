# 洁净审查记录 — workflows/subagents/prof-opt/mfu-analyzer.md

- 审查对象：`D:\Projects\Orca\workflows\subagents\prof-opt\mfu-analyzer.md`（v1，sentinel MBA7K2，D-V4-20 新增）
- 审查方法：全文通读 + 受众翻转（假设读者 = 只懂 MFU/NPU 业务、不懂 Orca 内部与 workflow 历史的执行子代理）+ 禁词 grep + SPEC/草稿/骨架/脚本 docstring 事实核对
- 参照：洁净契约 `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`；SPEC `docs/specs/prof-opt-v4-spec.md` §5 mfu-analyzer 行；草稿 D-V4-20（`docs/specs/prof-opt-v4-design-draft.md` §0.2）；骨架对照 `workflows/subagents/prof-opt/memory-verifier.md`；事实基准 `workflows/agents/_po_scripts/mfu_benchmark.py` docstring CONTRACT
- 审查日期：2026-08-26

## ① 逐段受众翻转结论表

| 段（行号） | 内容 | 受众翻转结论 |
|---|---|---|
| L1-8 | frontmatter（subagent/version/sentinel）+ 首行哨兵回显指令 | **PASS** — 与 memory-verifier 骨架同构；哨兵机制自包含，回显串逐字给出 |
| L10-18 | purpose（4 步职责） | **PASS** — 纯 WHAT：调脚本→解析→识别瓶颈→报告落盘；无历史/出处叙事 |
| L20-29 | Inputs 六参表（onnx_path/profile_dir/report_path/chip/precision/core_num） | **PASS** — 与 SPEC §5 输入列逐项对齐；每参含类型/取值域，独立可执行 |
| L31-52 | 核心脚本用法 + 参数表 | **PASS** — `$ORCA_ARTIFACTS_DIR/scripts/mfu_benchmark.py` 是契约 §5 白名单 operational env 串（部署件路径非引擎源码）；CLI 实参样例完整可照抄 |
| L54-66 | 输出产物清单 + 原始产物只读令 | **PASS** — 产物路径模式与脚本 CONTRACT 一致；L65-66「下游的确定性适配器与分析器都要按路径读它们」是约束执行所需的最小 why（防 agent 自作主张搬移产物），非设计考古（无内部路径/代号） |
| L68-86 | 阶段 1 执行评测（幂等优先 + 失败路径） | **PASS** — 复用判定条件具体（schedule_result.json 等在场）；评测失败→仍进阶段 2 的分支与 H5 呼应，fail loud（无日志时如实写明失败，报告标注） |
| L87-107 | 阶段 2.1/2.2（macs.csv + 时延 csv + 瓶颈识别四规则） | **PASS** — 列名（cycles/MFU/delay_cycles）与脚本 csv 契约一致；「见知识库条目」「见 H4/H5」均为本文档内部自包含引用 |
| L109-127 | 阶段 2.3/2.4（subgraph json + schedule_result + log） | **PASS** — 字段归属事实核对通过（见④）；文件名模式 `6613_*.csv`/`1951_*.csv` 与 `<chip>_<stem>.csv` 契约一致 |
| L129-157 | 阶段 3 报告模板（写 report_path + 首行哨兵） | **PASS** — 模板逐段可填；「并行 cycles 即下游判定使用的 canonical makespan」与脚本 docstring CANONICAL MAKESPAN 条款一致，属跨工具 operational 词汇 |
| L159-196 | 优化建议知识库（6 条目） | **PASS** — 全部为 NPU/MFU 领域通用知识（Conv 分解/DMA/Reshape 碎片/注意力结构/L1D 4MB/MFU>100% 偏差）；**无项目内部方案代号**（ns3/psu/pure_cnn/feat_complex 等零命中）；建议为泛化句式（「可评估」「结构上」），非具体项目方案回放 |
| L198-209 | 硬规则 H1-H6 | **PASS** — 六条全部可机械遵守；H6 写盘协议与 Constraints 双重钉死（只写 report_path，profile_dir 只读） |
| L212-224 | Output（紧凑摘要 ≤10 行）+ Constraints | **PASS** — 返回协议明确（首行哨兵 + 状态 + 并行 cycles 一句话 + Top-3 + 根因 + 报告路径）；不产 PROFILER_CONTRACT 四件套（与 mfu_adapter.py 分工正交，见④） |

## ② Findings

**零 finding。** 受众翻转通读未发现任何开发期残留：无 plan/issue/§N.M 编号、无 Orca 引擎源码路径、无内部 examples 路径、无项目名/方案代号硬编码、无 v4 已删机制措辞、无迁移/版本考古、无事故复盘叙事、无确定性多行代码内联（唯一 bash 块是单次脚本调用实参样例，属合法 operational）。语气为产品说明书式指令体。

### 信息性备注（非 finding，不阻断）

- **N1（L61-63）**：输出清单列出的 `gantt_chart_optimized.html` / `memory_usage_optimized.html` / `memory_allocation.html` 三个 HTML 产物不在当前部署的 placeholder `mfu_benchmark.py` docstring CONTRACT 内（grep 该脚本无任何 html 写出）。运行时零影响——阶段 1 复用判定与阶段 2.1-2.4 全部只依赖 CSV/JSON/LOG，不读 HTML；且 D-V4-20 明确该脚本是「用户真评测脚本载体，内容随用户提供替换」，清单描述的是真脚本行为。若追求与 placeholder 态严格一致可加「（真评测模式）」标注或删除三行，属可选润色。
- **N2（L145）**：报告模板「串行 MFU / 并行 MFU / 内存占用」三字段未显式给出推导来源（数据在场：csv 的 mfu 列、macs.csv、subgraph json 的 memory；聚合公式留给判断）。非洁净问题（六输入/脚本路径/三阶段/H1-H6/写盘协议五项均明确），仅记录可执行性锐度。

## ③ Grep 词表结果

对目标文件跑（不区分大小写）：`mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / profile_script_path / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / D:/Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft / pure_cnn / feat_complex`

**全部零命中。** 补充宽口径（懒补训 / epoch-only / proxy / baseline / perturb / ckpt / § / issue / TODO / FIXME / D-V4 / v2 / v3 / v4 / 历史 / 迁移 / 前身 / 前作 / Orca）：唯一命中为 L65-66 的「适配器/分析器」约束说明（见①，判定合法 operational）。

## ④ 契约一致性核对

| 核对项 | 基准 | 结论 |
|---|---|---|
| 输入六参 | SPEC §5：onnx 路径 + profile_dir + report_path + chip/precision/core_num | **一致**（Inputs 表 L20-29 逐项对应） |
| 输出 = 原始产物只读落 profile_dir + 报告首行哨兵 | SPEC §5 输出列；D-V4-20 | **一致**（L54-66 产物留置 + H6 只读 + L131-135 报告首行哨兵；报告文件名 `mfu_bottleneck_report.md` 由调用方经 `<report_path>` 传入，子代理侧正确不写死） |
| 评测失败仍出报告（H5） | SPEC §5 输出列括注 | **一致**（L83-86 阶段 1 失败路径 + H5 L207-209） |
| 紧凑摘要返回 | 草稿 D-V4-20（LLM 只做执行编排与定性分析） | **一致**（Output §2 ≤10 行摘要） |
| 不产四件套（与 mfu_adapter.py 分工） | D-V4-20（analyzer=执行层，adapter=四件套转换） | **一致**——全文零提及四件套/PROFILER_CONTRACT/summary 产出；唯一写盘 = report_path |
| 节点侧校验条款不在子代理文件 | SPEC §5 第 4 列（归 po_baseline/po_propose agent.md） | **一致**——本文件正确不含节点侧重派/error 逻辑 |
| 骨架同构 memory-verifier.md | frontmatter 三键 + 首行哨兵回显 + Inputs + 报告落盘优先 + Constraints | **一致** |
| **事实核对：§2.3 serial/parallel cycles 字段归属** | `mfu_benchmark.py` docstring：`schedule_result.json = {"schema_version":1,"chip":..,"precision":..,"core_num":..,"serial_cycles":S,"parallel_cycles":M,"subgraph_count":1}`；CANONICAL MAKESPAN = parallel_cycles | **一致**——L117-120：`serial_cycles`/`parallel_cycles` 归属 schedule_result.json、chip/precision/core_num 同文件可核对、并行 cycles = canonical makespan，全部与 docstring 逐字对上；§2.3 subgraph json 字段（cycles/delay_cycles/flops/memory/op_type）亦为 docstring tasks[] 契约子集 |
| §2.1/2.2 CSV 列名与文件名模式 | docstring：`<chip>_<stem>.csv`（name,op_type,cycles,mfu,delay_cycles）、`<stem>.macs.csv`（name,op_type,macs） | **一致** |

## 结论

mfu-analyzer.md 受众翻转通读通过、禁词零命中、SPEC §5 / D-V4-20 / 骨架 / 脚本 docstring 四方核对一致，两条信息性备注（N1 HTML 产物清单、N2 MFU 聚合来源）不构成洁净 finding。

VERDICT: CLEAN
