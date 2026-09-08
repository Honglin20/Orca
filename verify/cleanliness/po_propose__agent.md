# 洁净审查记录：workflows/prof-opt/agents/po_propose/agent.md

- 审查对象：`D:\Projects\Orca\workflows\prof-opt\agents\po_propose\agent.md`（v7 现行，140 行，per-wf 自包含布局）
- 审查性质：mfu-analyzer v3 同批改动的洁净复审（2026-09-08）。本文件两处改动：Step1 硬件参考路径改 `{{ subagents_root }}/references/ascend.md`（L50）；Step3 mfu-analyzer 派发补 `<hardware_ref>`（L88-90）。plan：`docs/plans/2026-09-08-mfu-analyzer-v3.md` 改动清单 #3
- 审查方法：全文通读 + 受众翻转逐段裁决 + 五组禁词 grep + 派发参数与 mfu-analyzer.md Inputs 表逐项对账
- 参照：洁净契约 `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`；SPEC `docs/specs/prof-opt-v7-spec.md` §3.3/§3.4/§5；共享硬件参考 `workflows/prof-opt/subagents/references/ascend.md`；本目录 `references/structural-levers.md`（338 行）
- 审查日期：2026-09-08（本快照为 v7 全量重写版，取代 2026-08-26 的 v4 时代六轮记录——旧记录针对已不存在的平铺路径与 placeholder/mfu 双模机制，历史见 git）

## 一、逐段受众翻转结论表

| 行号 | 段 | 受众翻转结论 |
|---|---|---|
| 1-11 | frontmatter + 引言（每轮三假设→融合一只→仅这只走评估/实现/mfu-analyzer） | CLEAN。产品说明书式职责总述；「A measured improvement … enters po_probe; reaching the frozen origin target is disclosure only」为运行时判定规则，零历史 |
| 13-30 | Invariants（工作区边界 / 三份基线文档 / 写权限分域 / 禁 ONNX diff 作门 / lineage 字段集 / 禁孤立小改动 / 预测非准入） | CLEAN。全部可独立执行；「The MFU markdown is the only profiling analysis input; raw files listed by it are drill-down evidence」与 mfu-analyzer.md L66-68、SPEC §3.2 三方一致 |
| 32-37 | Step 0 round/re-entry | CLEAN。round_state.py + proposals.json 复用判据具体；「never regenerate a completed selector result」fail-loud 语义 |
| 39-56 | Step 1 三 candidate 并行派发（**本次改动 L50**） | CLEAN。`the shared hardware reference {{ subagents_root }}/references/ascend.md` 与 po_baseline L113 写法逐字一致；`{{ subagents_root }}` 是 render 层顶层变量（`orca/exec/render.py:61` 暴露、`orca/compile/layout.py:31-49` 双形态解析——新形态判据 `any(sub.glob("*.md"))` 在引入 `references/` 子目录后仍成立，路径可达）；candidate 侧自述吻合（hardware-architecture-proposer.md L11 "Read the Ascend hardware reference"、architecture-selector.md 同款泛指，具体路径由本文件派发时提供——point-to-file 协议一致）；「Each candidate must name the information invariant, measured root cause, …」与 v3 根因词汇表口径对齐 |
| 58-80 | Step 2 selector 融合 + 提案字段集 + 机械校验 + 一次重派后 fail loud | CLEAN。字段集逐一可校验；「It must not contain `op_delta`」负向约束显式；「Re-dispatch the selector once on invalid output, then fail loud」重试可见 |
| 82-119 | Step 3 实现与测量（**本次改动 L88-90**） | 见 FINDING-1（派发参数完备性）。L90 `pass <hardware_ref>={{ subagents_root }}/references/ascend.md` 本身 CLEAN：与 po_baseline 写法一致、与 mfu-analyzer.md Inputs 表 `<hardware_ref>` 占位名逐字对应；分析 stamp（key 三元组）/ 修复环（repair_count < 5 / == 5 终止写 direction.json）/「Never delete the fifth verdict or attempt a sixth measurement」全部可机械执行、fail loud |
| 121-140 | Step 4 artifacts + emit | CLEAN。`## architecture/## latency/## accuracy` 三节、generated_artifacts 只列实存文件、`status == executed iff error == ""` 判定式；「push the docs manifest best-effort」best-effort 语义显式 |

## 二、Findings

### FINDING-1（轻微 · 派发参数完备性；先于本轮存在，非本批改动引入）

- **位置**：`workflows/prof-opt/agents/po_propose/agent.md:88-90`
- **问题**：Step 3 的 mfu-analyzer 派发只显式给出两个产物路径 + `<hardware_ref>`，而 `mfu-analyzer.md` Inputs 表（L29-37）要求七个输入——`<chip>/<precision>/<core_num>` 的取值来源（workflow inputs → contracts.json `profile` block）在本文件全文无交代。对照方 `po_baseline/agent.md:108-113` 有完整对应交代（「substitute the chip / precision / core_num values you read from `contracts.json`'s `profile` block」）；SPEC §3.3 变体 profiling 行同样从简（未写参数来源）。执行 LLM 仍可从 subagent md 的 Inputs 表反推缺参，但取值出处无指引，存在猜测空间。
- **引入性判定**：v3 改动只做了加法（补 `<hardware_ref>`），未删除任何既有说明；缺参出处说明是 v7 重写时已有的从简，SPEC 层同构。故不归咎本批改动。
- **建议修法（一行级）**：L88-90 括注扩为 `(pass <hardware_ref>={{ subagents_root }}/references/ascend.md and chip/precision/core_num from contracts.json's profile block, as po_baseline does)`。
- **处置**：🟢 非阻塞；待创作方与 F1/F2（见 subagent__mfu-analyzer.md 快照）一并顺手修。

除 FINDING-1 外零 finding。两处改动 hunk（L50、L88-90）受众翻转通过：均为运行时路径/占位传参，无开发考古；`references/ascend.md` 相对 `{{ subagents_root }}` 的子目录引用是运行时可达路径（非仓库源码路径泄漏），契约 §5 判据 = operational。

## 三、词表 grep（五组，2026-09-08 口径）

对 `agents/po_propose/agent.md` + `references/structural-levers.md` 跑：

| 组 | 词表 | 结果 |
|---|---|---|
| A 退役机制/测试夹具 | `mnist` `MNIST` `CIFAR` `playground` `model8` `pure_cnn` `feat_complex` `wireless` `mnist_kd` `profile_script_path` `placeholder_profiler` `bottleneck-analyst` `check_bottleneck` `predict_delta` `PROFILER_CONTRACT` `mfu_adapter` `baseline_proxy` `run_verify` `perturb_ckpt` `playbook` | **0 命中** |
| B 迁移/版本考古 | `迁移` `前身` `前作` `legacy` `deprecated` `formerly` `used to` `analogue` `stall` `deepseek` `kill+retry` `TODO` `FIXME` | **0 命中** |
| C 引擎源码/内部路径 | `orca/(exec\|compile\|run\|schema\|iface\|events\|gates)` `examples/` `D:\Projects` `/mnt/d` `docs/specs` `docs/plans` | **0 命中** |
| D spec/plan 节号 | `§N.M` 形、`SPEC `、`ADR-N`、`phase-N`、`plan §` | **0 命中** |
| E v2 残留/修订措辞 | `\bv2\b`、`修订记录`、`历史版本`、`不再` | **0 命中** |

（`structural-levers.md` 的 MobileNetV3/ALBERT 等含 `V3`/`V2` 子串为合法模型名，契约 §4 自注不伤；该文件本轮仅 L12-14 词汇表 4→5 同步，与 mfu-analyzer.md 五类逐字一致。）

## 四、单一真相源核对（ascend.md 共享）

- 唯一活跃副本：`workflows/prof-opt/subagents/references/ascend.md`；旧 `agents/po_propose/references/hardware/ascend.md` 已删（`find references -type f` 仅剩 structural-levers.md）；旧路径字串仅存于 `docs/specs/prof-opt-v7-spec.md:119`（删除记录）与 `docs/plans/2026-09-08-mfu-analyzer-v3.md:41`（改动清单）——均为开发文档，允许。
- 本文件两处引用（L50、L90）与 po_baseline L113 三处写法逐字一致。
- 职责边界：structural-levers L11-15「Structural priors live HERE and only here」与 ascend.md §3（设计倾向，proposer 用）实质不重叠——ascend.md 只陈述硬件事实与硬件映射先验（不点名 catalog 任何 lever 条目），structural-levers 只收结构药方（不陈述硬件事实，其 Shared hardware rationale 为泛化一句）；mfu-analyzer.md L199-201 三刀切（词汇表=诊断 / ascend §3=事前先验 / 结构方案=proposer）把三方职责钉死。措辞级微张力（"only here" 严格读 vs ascend §3 的先验地位）并入 mfu-analyzer 快照 F2 一并润色即可，不单列。

## 五、裁决

v3 同批两处改动（L50 硬件参考路径、L88-90 补传 `<hardware_ref>`）受众翻转通过、与派发对方/校验门/共享参考四方一致；全文五组禁词零命中。一条轻微 finding（FINDING-1：变体 mfu 派发的 chip/precision/core_num 出处未写，先于本轮存在）非阻塞，建议随下批顺手修。无洁净契约违规。

VERDICT: CLEAN（附 1 条 🟢 非阻塞完备性项，2026-09-08 v3 复审）
