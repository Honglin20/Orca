# 洁净审查记录 — workflows/subagents/prof-opt/bottleneck-analyst.md

- 审查对象：`D:\Projects\Orca\workflows\subagents\prof-opt\bottleneck-analyst.md`（80 行，frontmatter `subagent: bottleneck-analyst v1 BNA3Q8`）
- 审查方法：受众翻转通读（洁净契约 §8）+ 禁词表 grep + SPEC §5 契约一致性核对 + memory-verifier 骨架同构核对
- 交叉核对的实现文件（只读）：
  - `docs/specs/prof-opt-v4-spec.md` §5（subagent 契约表）+ 文件树（check_bottleneck.py 条目）
  - `workflows/agents/_po_scripts/check_bottleneck.py`（节点校验门）
  - `workflows/agents/_po_scripts/analyze.py`（hot_patterns 行结构、四件套文件名）
  - `workflows/agents/po_propose/agent.md`（Step 2 派发块：实际传入 inputs + 失败矩阵）
  - `workflows/subagents/prof-opt/memory-verifier.md`（骨架基准）+ 其余 4 个同组 subagent（哨兵约定对照）

## 1. 逐段受众翻转结论

| 段（行号） | 内容 | 结论 | 依据 |
|---|---|---|---|
| L1-5 frontmatter | subagent/version/sentinel | 通过 | 运行时机械消费的元数据，与 memory-verifier 骨架同构 |
| L7 哨兵指令 | echo `[subagent:bottleneck-analyst v1 BNA3Q8]` | 通过 | 运行时机械指令；格式与其余 5 个 subagent 逐字同构 |
| L9-15 角色段 | 机械报告是数字唯一来源；agent 只加解释不加数字 | 通过 | 角色即 WHAT；"produced by the deterministic analyzer" 是产物来源陈述（运行时需要知道的认知边界），非开发考古 |
| L17-28 Inputs | `<output_dir>`（workspace + bottleneck_report.json + base/profile/ 四件套 + 可选 business_logic.md）、`<analysis_path>` | 通过 | ① 每条可独立执行：文件名逐一与 analyze.py 加载器核对（taskgraph.json/schedule.json/profile_summary.json/ops.csv 均真实存在，L62-64/91）② `$ORCA_ARTIFACTS_DIR` 为契约 §5 允许的 operational env ③ 占位符名与 po_propose agent.md L110-111 实际派发逐字一致（`<output_dir>`/`<analysis_path>`）④ business_logic.md 属 SPEC §5「全部原始产物」范围且 "when present" 正确守卫 |
| L30-43 Method | 4 步：读报告→选瓶颈→写分析→数值逐字 | 通过 | Step 1 的行结构描述（pattern id / op type / count / total cycles / share of the critical path / onnx node names）与 analyze.py L207-212 hot_patterns 字段逐一对应，无虚构字段；Step 2 的「保序子集非前缀」与 SPEC「保序子集，非前缀」及 check_bottleneck.py L113-118（rank index 严格递增）一致；"the proposal stage does the concrete proposing" 是下游分工的运行时范围声明，非开发期引用 |
| L45-60 Output schema | 封闭 schema JSON 块 | 通过 | 与 check_bottleneck.py `_TOP_KEYS`/`_ENTRY_KEYS`（L31-32）逐键一致；示例值 P1/Erf/150 为领域通用占位（P{rank} 格式 = analyze.py L208），非测试夹具硬编码（词表零命中） |
| L62-69 Field contract | name=pattern_id 保序 / op_type·cycles 逐字 / analysis 非空 | 通过 | "the gate enforces every clause" 属实而非 overclaim——逐条对上脚本：封闭键 L53-56/85-88、referential L96-109、保序 L113-118、analysis 非空 L110-112、base_report 存在可解析 L65-72、summary 非空 L60-61 |
| L71-73 Return value | 哨兵行 + 一行摘要；文件为准 | 通过 | 与 JSON 产物类兄弟（structure-proposer L114-116 / variant-implementer L121-123）约定一致：哨兵在 return value 不在 JSON 文件（封闭 schema 不允许）——md 文档产物类兄弟才把哨兵落文件首行，分类正确 |
| L75-79 Constraints | 只写 `<analysis_path>`；零编造数字 | 通过 | 修改范围与骨架同构；零编造规则的逃逸口 "(or derived from the raw profile files)" 与 Inputs 授予的四件套读取权自洽 |

## 2. 词表 grep 结果

对 `run_verify|baseline_proxy_acc|baseline_ref|mfu_adapter|perturb_ckpt|playbook|ref-input|auto-trained|mnist_kd|playground|prof_opt_demo|docs/specs|D:\\Projects|/mnt/d|spec-review|SPEC-R1|ns3|psu|kd-nas|nas-supernet|prof-opt-design-draft`（大小写不敏感）全量扫描：**零命中**。

## 3. 四维判定汇总

- ① 每条指令可独立执行：是——所有路径/文件名/字段名均与真实实现核对存在，无悬空引用。
- ② 开发期残留：无——零 plan/issue/§N.M 编号、零 Orca 引擎源码路径、零内部 examples 路径、零迁移/考古措辞、零事故复盘叙事、零内联确定性代码（本 subagent 纯 LLM 判断，无需脚本，内联代码为零是正确的）。
- ③ v4 已删机制残留措辞：无（词表 + 人工通读双确认；全文无任何 proxy/补训/mfu/perturb/playbook 痕迹）。
- ④ 语气：产品说明书式——全程指令式 WHAT，自包含，无 WHY 论证。

## 4. SPEC §5 契约一致性

| SPEC §5 行要求 | 实现核对 | 结论 |
|---|---|---|
| 输入 = base/profile/ 四件套 + 全部原始产物 + bottleneck_report.json | md Inputs 覆盖四件套（文件名与 analyze.py 加载器一致）+ 报告 + 原始产物 business_logic.md（可选引用）；且 `<output_dir>` 授予全 workspace 读权 | 一致 |
| 输出 = base/bottleneck_analysis.json，零重复机械字段、保序子集映射 | 封闭 schema 仅含映射必需的 name/op_type/cycles + summary/analysis（纯增值字段），排除 count/share/onnx_nodes 等机械字段；保序子集语义与校验脚本一致 | 一致 |
| 节点校验 = check_bottleneck.py 失败重派 1 次仍败 → error | 落在调用方 po_propose agent.md（Step 2 校验 + 统一失败矩阵 L58-64：重派 ONCE，再败 → failed）——节点侧策略不进 subagent md，与 memory-verifier 骨架分工一致；md 以 "the caller's validation gate" 指称，准确 | 一致 |
| 骨架同构 memory-verifier | frontmatter → 哨兵行 → 标题 → 角色段 → Inputs（"The caller will provide:"）→ Method → Output（写盘路径强制）→ Return value → Constraints，逐段同构 | 一致 |

## 5. Findings

**零 finding。**

候选疑点复核后均判非问题（记录备考）：
- "Erf"（L56）：ONNX 通用算子类型（GELU 常见），非测试项目硬编码；词表零命中。
- `baseline/business_logic.md` 可选引用（L25-26）：SPEC 输入列「全部原始产物」所涵盖，且 Method Step 3 "grounded in the business logic when relevant" 与之自洽。
- "the proposal stage"（L42）：下游管线阶段指称（structure-proposer 消费本产物），运行时分工信息，非开发期文档导航。
- `schema_version` 在 md 示例中必含而校验脚本视为可选键（check_bottleneck.py L62 `if "schema_version" in data`）：md 要求是脚本接受的子集，按 md 写出的产物必过门，无冲突。

VERDICT: CLEAN
