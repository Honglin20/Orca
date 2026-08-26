# agent.md 洁净契约（受众分离）

> 配套 lint：`orca/compile/validator.py::_check_prompt_dev_residue`（`tars validate` 自动跑，warning 不阻断）。
> 配套 SKILL：`orca/skills/create-workflow/SKILL.md`（创建/改完 workflow 必做洁净检查）。

## §0 顶线（一句话心智）

**你写出来的 `agent.md` body 最终是给运行时受众（执行 LLM）看的——不是给开发期受众（reviewer / 未来自己）看的。**

凡是只有"了解这个项目历史 / 前身 / 设计讨论"才看得懂的句子，都是设计推理痕迹，不该进 prompt。设计理由（why / 对照 / 出处 / issue 编号）落 commit message / release-note / `docs/specs/` / `docs/plans/`，**绝不进产物**。

## §1 受众分离原则

`agent.md` body 是给 **LLM agent 的运行时指令**（只含 WHAT to do：做什么、读什么、产出什么），
**不是**给 reviewer / 未来自己的设计论证（WHY 属 commit / release-note / `docs/specs/` plan）。
两类受众看的文本完全不同：

| 受众 | 文本特征 | 落点 |
|---|---|---|
| **运行时执行 agent**（最终读者） | 指令式、可机械执行、零历史负担、自包含 | `agent.md` body |
| **开发期 reviewer / 未来自己** | 论证、对照、迁移出处、issue 编号、spec 引用 | commit / release-note / `docs/specs/` / `docs/plans/` |

把开发期上下文塞进 prompt 等于让执行 agent 读不关心它的信息——污染注意力 + 长期看让 prompt
漂成考古日志（每次改 workflow 都添一笔"为什么这样改"，半年后 prompt 200 行里有 80 行是历史）。

## §2 validate 的角色边界（只提醒，不裁决）

这是必须钉死的一条分工，理解错了就会误以为"validate 过了 = prompt 洁净"：

- **validate 只做一件事**：扫到 §3 表里的 pattern 时，报 warning **指给你看「这里有一处疑似残留」**。
- validate **不裁决**：warning 不阻断、不失败、不改你的 prompt、不替你判断该不该删。
- validate **不参与最终修改**：最终 prompt 洁不洁净、改不改，由作者按本契约（§6 受众翻转通读）决定，validate 不插手。
- validate **不完整**：它只能抓 §3 那张 deterministic pattern 表（你记得列的类别）；§4 那些靠通读兜底的类别，validate 一个都不报。

一句话：**validate 是手电筒（照位置），契约是判据（定去留），通读是裁决（做决定）。** 手电筒没照到，不代表没有；手电筒照到的，也不一定真要删（见 §4 operational 串）。

## §3 禁止写进 prompt 的开发期残留（validate 抓——deterministic 提醒）

`_check_prompt_dev_residue` 的 lint pattern 表（命中即 warning，仅提醒）：

| 类别 | 例 | 解释 |
|---|---|---|
| **plan 编号** | `plan §9.1`、`plan §N1`、`plan §B2` | 给 reviewer 的 plan 导航，agent 不需要 |
| **spec/plan 节号** | `§9.1`、`§2.3` | 同上，节号是文档导航（要求 `N.M` 形；单数字 `§9` 不抓，避免误报枚举编号） |
| **issue breadcrumb（带括号）** | `（I10）`、`(N1`、`（B2` | 中英文括号 issue 引用，开发期追溯用 |
| **Orca 源码路径** | `orca/exec/env.py:91`、`orca/compile/validator.py` | 引擎实现细节非 agent 业务，agent 不该读引擎源码 |
| **内部 examples 路径作论据** | `examples/agents/plotter/agent.md` | 仓库内部路径，对外部用户不可见且无意义 |

## §4 禁止写进 prompt 的开发期残留（validate 不抓——受众翻转通读兜底）

这些类别 deterministic 难以无歧义识别（要么误报率高，要么与合法串重叠），lint 一律不报，**全靠 §6 通读 flag**：

- **无括号 issue / 里程碑编号**（`BLK-3`、`HI-1`、`U-1`、`P5`、`SR3`、`review #7`）——不带括号的纯文本编号；lint 不抓是因为正则 `[A-Z]+-[0-9]+` 会误伤 `ResNet-50` / `ViT-14` / `GPT-4` 等合法模型名；
- **英文迁移出处词**（`analogue of` / `leaves off` / `the KD-NAS analogue of ...`）——与中文 `迁移自` 同类，写 KD 仿制时高频出现；
- **中文迁移 / 版本考古**（`迁移自 KD-NAS phase-2`、`前身是`、`前作`、`v3 已嵌入 setup 节点`）——已在 `writing-style.md` §1/§8 列禁；
- **SPEC / phase / ADR 编号**（`SPEC phase-14`、`ADR-007`、`v5 §4.3`、`SPEC 2026-07-23 §3.3`）；
- **spec-review 评审记录泄漏**（`spec-review 改了` / `spec_review 结论`）；
- **测试项目名硬编码**（`MNIST=accuracy 0.98`、`CIFAR baseline=92.x%`）——测试 fixture 只作 workflow inputs 喂入，绝不写进 prompt（见 §5）；
- **事故复盘 / 运行时基础设施叙事**（`deepseek's intermittent stalls make the external per-node driver kill+retry this node`、`produced by a previous stalled attempt`、`resume across stall-restart`）——解释**运行时环境**（模型卡顿 / 驱动 kill+retry / 上轮中断）的"为什么"叙述，对执行 agent 零可执行价值：它不是指令，是事故复盘。落 commit / release-note，不落 prompt；
- **确定性代码内联在 agent.md**（多行 bash / `python3 -c` 的循环·分支·assert 逻辑）——可机械执行的确定性逻辑应抽到 `scripts/<name>.sh`，agent.md 只留 `bash "$ORCA_AGENT_RESOURCES/scripts/<name>.sh"` 一行调用（对齐 `writing-style.md` §3「能确定性的沉到脚本」）。**单行 operational 命令**（`jq` / `ruff` / `python <file>`）允许内联，不算违规。

> 考古类残留的完整宽口径 grep 表见 [`writing-style.md`](writing-style.md) §8 A 类（A 类 = 命中即 FAIL 的开发考古）。本契约 §3+§4 与它口径一致，两处合起来用。

## §5 允许保留的 operational 串

这些是 agent 执行时**真正要用的**运行时 API / env / 工具名 / 字段名，**不属**开发期残留，lint 也不命中，**通读时不要误删**：

| 串 | 用途 |
|---|---|
| `$ORCA_AGENT_RESOURCES` / `$ORCA_ARTIFACTS_DIR` | `orca spawn` 注入的 env（folder-agent 资源根 / artifacts 目录） |
| `orca.chart.render_chart` | agent 调用的运行时 API（非源码路径——无 `.py` 后缀） |
| `orca/skills/...`（如 `orca/skills/tars/SKILL.md`） | agent 可合法 `Read` 的 skill 资源——operational 引用，**非引擎源码**（lint 源码路径白名单不含 `skills`，故不命中；别当成 §3 残留删掉） |
| `Git Bash` / `bash` / `python` | shell 命令 |
| `tape` / `output_schema` / `validator` / `retry` | Orca 字段名（agent prompt 里讨论这些是合法的） |
| `Task(subagent_type=...)` | in-session 调用原语 |
| NAS block 库名（`swin_window` / `cswin` / `mbconv` 等） | NAS 领域通用术语，agent 执行时要引用；也常出现在 references/ 字节一致内容里 |

判据：**这个串是 agent 执行时要读 / 调用 / 产出的吗？** 是 → 保留；否（只是给 reviewer 看的导航
/ 论据）→ 移走。

## §6 测试夹具防火墙

workflow 的 agent prompt 永远用**泛化抽象**：

- 项目根：`{{ setup.output.project_root }}` / `<user_project_root>`（不写 `/home/me/mnist`）；
- 业务参数：`{{ inputs.accuracy_target }}` / `{{ inputs.max_rounds }}`（不写 `accuracy=0.98`）；
- 数据集名：泛化称「项目 dataset / calib_data_ref」（不写 `MNIST` / `CIFAR`）；
- metric 名：泛化称「项目 metric」（不写 `accuracy`）。

测试 fixture（如 MNIST）只作 workflow inputs 喂入，**绝不写进 prompt**。建 workflow 的顺序：

> **先 dry（无具体项目） → 再 fixture**——agent.md prompt 定稿后才引入 fixture。

这样保证 workflow 可移植到任意项目；fixture 是 inputs 的事，不是 prompt 的事。

## §7 多 agent 共建洁净契约

派 coder-agent 写 agent.md 时，**派单 prompt 里带本契约的禁止项**，让共建者守同一标准：

> 你写的 agent.md body 是 LLM 运行时指令，最终读者是执行 agent，不是 reviewer。禁开发期残留：
> plan/issue 编号（`§9.1`、`（I10）`、`BLK-3`）、Orca 引擎源码路径（`orca/exec/...`，但 `orca/skills/...`
> 可保留）、内部 examples 路径、英文/中文迁移出处（`analogue of` / `迁移自`）、测试项目名硬编码
> （`MNIST=...`）、SPEC/ADR/phase 编号、运行时基础设施叙事（`deepseek 卡顿` / `driver kill+retry` 等
> 事故复盘）。确定性代码（多行 bash/python 逻辑）抽到 `scripts/`，body 只留
> `bash "$ORCA_AGENT_RESOURCES/scripts/<name>.sh"` 一行调用。设计理由放 commit。详见
> `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`。

## §8 审查法——受众翻转通读（真正的裁决）

**通读是洁净的唯一裁决者**（validate 是手电筒，见 §2）。lint 的 regex 只能抓 §3 那张你记得列的表；
**§4 那些忘了列的、新冒出来的类别**，全靠受众翻转通读 catch：

> 审查时假设「我是只懂业务（如 NAS）、完全不懂 Orca 内部 / 不懂这个 workflow 历史的 LLM agent」，
> 逐句读 body，凡对执行任务无帮助的开发上下文都 flag——包括 lint 没列的类别（如无括号 issue 编号、
> SPEC/ADR 编号、英文迁移词、新出现的内部术语）。

grep 找你记得的（§3）；受众翻转抓你忘了的（§4）。**两者并用**才能覆盖。

## §9 执行流程 + lint 扫描范围

作者建完 / 改完 workflow 后：

1. 跑 `tars validate <yaml>`——看 §3 deterministic 提醒（warning 仅指位置，不裁决）；
2. 按 §8 受众翻转通读 **agent.md body + SKILL.md + prompt-adjacent references/ prose**（见下「lint 扫描范围 ≠ 契约适用范围」）——裁决哪些真要删（catch §4 + lint 之外的残留）；
3. warning 清零 + 通读无疑 → workflow 视为「prompt 洁净」完成。

既有 workflow 残留（lint 报 warning）**不强制立即清理**——但新建 / 重构 workflow 必须 0 warning + 通读通过。

**lint 扫描范围 ≠ 契约适用范围**（两件不同的事，别混）：

- **lint 扫描范围**（机械，窄）——deterministic 只扫：
  - ✅ 扫：`AgentNode.prompt`（folder-agent / file-agent，即 `resources_root` 已物化）+ foreach body agent；
  - ❌ 不扫：inline 短 prompt、`references/`、`assets/`、subagent 文件、SKILL.md。
  lint 从不扫 references/——这不代表 references/ 不受契约约束（见下）。
- **契约适用范围**（概念，宽）——本契约管「**LLM 运行时读的指令性 prose**」，**不论文件在哪**。只要某个 LLM（主 agent 或 subagent）运行时被指示去 Read 一个文件当指令 / 指南 / checklist 用，那个文件的 prose 就必须洁净——哪怕它住在 `references/` 下。判据不是「文件在不在 references/」，而是「**它是 LLM 读的指令，还是被复制 / 改写的代码 / 数据**」。

**`references/` 二分（关键——上一版契约把整条 references/ 当惰性数据豁免，是错的）**：

| 子类 | 例 | 契约 |
|---|---|---|
| **prompt-adjacent prose**（agent / 子 agent 被指示 Read 的 `.md` 指南 / checklist / spec） | `references/workflows/*.md`、`*guide.md`、`*cheatsheet.md`、`*paradigm.md`、`supernet_specs/**/*.md`、`workflow-checklists/**/*.md` | ✅ **适用**——必须按 §3/§4/§6 洁净（这些文件被 SKILL.md / agent.md 显式 `Read` 后跟从，等价于扩展 prompt） |
| **惰性 code / data 资产**（被逐字复制或改写成生成代码的模板 / 示例） | `*.py`、`*.py.skel`、`*.yaml`、`*.json`（leaf 骨架、example 实现、search_config 模板等） | ❌ 真豁免——是代码 / 数据，非 LLM 指令 |

> 信号：SKILL.md / agent.md 里出现「Read `<skill_dir>/references/.../*.md` before starting this step」「Follow it」「see ... for the detailed flow」→ 该 `.md` 是 prompt-adjacent，**适用**。出现「Adapt / Use ... as an implementation example」「copy verbatim into the generated file」且后缀是 `.py`/`.yaml` → 惰性资产，**豁免**。

**已知盲区**（lint 完全不检，必须靠 §6 通读——prompt-adjacent prose 也在内）：
- §4 全部类别（无括号 issue 编号、迁移词、SPEC/ADR 编号等）；
- §6 测试夹具硬编码（`MNIST` / `accuracy=0.98` / `/home/me/mnist`）——deterministic 检测误报率太高，刻意不抓，通读是唯一兜底；
- **prompt-adjacent references/ prose**（lint 不扫 references/，但这些文件 agent 运行时会读，必须人工通读洁净）——这是最易漏的盲区：作者以为「references/ 是数据」就放过，实则里面塞了 `run 6c2ebe` / `audit-found` / `ln(10)` / `MNIST` / `CONTRACTS §6` 等考古。

## §10 用户输入权威——从错误提取的运行时规则（agent 该恪守什么）

§1-§9 讲「agent.md body 不该写什么」（开发期残留）。本节讲互补的另一面——「该恪守什么」：一条
从真实漂移错误中提取的通用运行时规则。**当某类错误在一个 workflow 复现，提取通用规则写进本节，
让未来所有 agent 天然遵循——而不是逐 workflow 打补丁、针对具体错误定制。**

### 规则：用户提供的范式 / 数据 / 脚本 / 指标是不可替代权威

凡是 agent 要 port / 复用 / 包装**用户原项目的逻辑**（训练范式、loss / optimizer / scheduler、
评价 metric 的名 / 方向 / 变换、数据管道、用户提供的脚本如 latency / 评测 / 数据处理脚本），
都必须把它当作**不可替代权威逐字保留**，不得擅自替换为 agent 自选的"更标准 / 更可复现"的代理。

典型漂移（从中提取本规则的错误样本，**是 illustrative，不是让 agent 照单全收的定制清单**）：

- 用确定性更强的代理测度替换用户测度（如 FLOPs / MACs / params 替换用户提供的时延脚本）；
- 擅自换训练范式构件（optimizer 类、loss 公式 / 常量、scheduler）；
- 改变用户的评价标准（把 higher-better metric 取负展示、还原用户刻意施加的 dB / 归一化变换、
  loss↔acc 互换）。

允许的改造仅限"为达成 workflow 目标所必需"的结构性变换（如 NAS 的 subnet 采样 / 共享权重
forward / 预算压缩；量化的精度约束），**不得触及用户测度本身**。

### 打造 agent 时如何落地（通用三件套）

当 agent 涉及用户逻辑 port / 包装时，在 agent.md 落这三层——缺一就容易漂移：

1. **铁律 + 用户输入清单**：生成前要求 agent 显式列举「用户输入清单」（从 manifest / 用户源码
   提取，缺字段则补全），声明逐字保留 + 改造边界（哪些是 NAS 化 / 量化等必需改造）。
2. **fidelity 审计维度**：在 fidelity-verifier 类 subagent 加一个维度，专门查"是否擅自替换了
   用户的范式 / 测度 / 脚本 / 指标方向"。
3. **deterministic 自检**：生成后用机械检查（grep / 解析结构化配置）拦"引入了用户未声明的代理"
   这类可机器判定的漂移；语义层（方向 / 变换忠实性）归 fidelity 审计——deterministic 自检只做可
   机械验证的，不 overclaim。

> 判据：规则要从具体错误**抽象**到"用户输入即权威"这一层，可跨 workflow 复用；具体错误样本（如
> FLOPs 代时延）只作 illustrative，不作硬编码定制。`nas-supernet` workflow（生成节点
> `ns_train_script` / `ns_search_pipeline` / `ns_retrain` + `project-fidelity-verifier`）是本通用
> 规则在 NAS 场景的落地样本。
