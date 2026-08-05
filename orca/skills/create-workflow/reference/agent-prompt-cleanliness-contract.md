# agent.md 洁净契约（受众分离）

> 配套 lint：`orca/compile/validator.py::_check_prompt_dev_residue`（`tars validate` 自动跑，warning 不阻断）。
> 配套 SKILL：`orca/skills/create-workflow/SKILL.md`（创建/改完 workflow 必做洁净检查）。

## 1. 受众分离原则

`agent.md` body 是给 **LLM agent 的运行时指令**（只含 WHAT to do：做什么、读什么、产出什么），
**不是**给 reviewer / 未来自己的设计论证（WHY 属 commit message / release-note / `docs/specs/`
plan）。两类受众看的文本完全不同：

| 受众 | 文本特征 | 落点 |
|---|---|---|
| **执行 agent**（运行时） | 指令式、可机械执行、零历史负担 | `agent.md` body |
| **reviewer / 未来自己**（开发期） | 论证、对照、迁移出处、issue 编号、spec 引用 | commit / release-note / `docs/specs/` / `docs/plans/` |

把开发期上下文塞进 prompt 等于让执行 agent 读不关心它的信息——污染注意力 + 长期看让 prompt
漂成考古日志（每次改 workflow 都添一笔"为什么这样改"，半年后 prompt 200 行里有 80 行是历史）。

## 2. 禁止写进 prompt 的开发期残留

`_check_prompt_dev_residue` 的 lint pattern 表（命中即 warning）：

| 类别 | 例 | 解释 |
|---|---|---|
| **plan 编号** | `plan §9.1`、`plan §N1`、`plan §B2` | 给 reviewer 的 plan 导航，agent 不需要 |
| **spec/plan 节号** | `§9.1`、`§2.3` | 同上，节号是文档导航（要求 `N.M` 形；单数字 `§9` 不抓，避免误报枚举编号） |
| **issue breadcrumb** | `（I10）`、`(N1`、`（B2` | 中英文括号 issue 引用，开发期追溯用 |
| **Orca 源码路径** | `orca/exec/env.py:91`、`orca/compile/validator.py` | 引擎实现细节非 agent 业务，agent 不该读引擎源码 |
| **内部 examples 路径作论据** | `examples/agents/plotter/agent.md` | 仓库内部路径，对外部用户不可见且无意义 |

另外（lint 不抓、契约禁——靠受众翻转通读兜底）：
- **SPEC / phase / ADR 编号**（`SPEC phase-14`、`ADR-007`、`v5 §4.3`）；
- **测试项目名硬编码**（`MNIST=accuracy 0.98`、`CIFAR baseline=92.x%`）——测试 fixture 只作
  workflow inputs 喂入，绝不写进 prompt（见 §4）；
- **迁移出处 / 版本嵌入**（`迁移自 KD-NAS phase-2`、`v3 已嵌入 setup 节点`）——已在
  `writing-style.md` §1 列禁。

## 3. 允许保留的 operational 串

这些是 agent 执行时**真正要用的**运行时 API / env / 工具名，**不属**开发期残留，lint 也不命中：

| 串 | 用途 |
|---|---|
| `$ORCA_AGENT_RESOURCES` / `$ORCA_ARTIFACTS_DIR` | `orca spawn` 注入的 env（folder-agent 资源根 / artifacts 目录） |
| `orca.chart.render_chart` | agent 调用的运行时 API（非源码路径——无 `.py` 后缀） |
| `Git Bash` / `bash` / `python` | shell 命令 |
| `tape` / `output_schema` / `validator` / `retry` | Orca 字段名（agent prompt 里讨论这些是合法的） |
| `Task(subagent_type=...)` | in-session 调用原语 |
| NAS block 库通用示例名（`swin_window` / `cswin` / `mbconv` 等） | references/ 字节一致内容，作 NAS 通用术语 |

判据：**这个串是 agent 执行时要读 / 调用 / 产出的吗？** 是 → 保留；否（只是给 reviewer 看的导航
/ 论据）→ 移走。

## 4. 测试夹具防火墙

workflow 的 agent prompt 永远用**泛化抽象**：

- 项目根：`{{ setup.output.project_root }}` / `<user_project_root>`（不写 `/home/me/mnist`）；
- 业务参数：`{{ inputs.accuracy_target }}` / `{{ inputs.max_rounds }}`（不写 `accuracy=0.98`）；
- 数据集名：泛化称「项目 dataset / calib_data_ref」（不写 `MNIST` / `CIFAR`）；
- metric 名：泛化称「项目 metric」（不写 `accuracy`）。

测试 fixture（如 MNIST）只作 workflow inputs 喂入，**绝不写进 prompt**。建 workflow 的顺序：

> **先 dry（无具体项目） → 再 fixture**——agent.md prompt 定稿后才引入 fixture。

这样保证 workflow 可移植到任意项目；fixture 是 inputs 的事，不是 prompt 的事。

## 5. 多 agent 共建洁净契约

派 coder-agent 写 agent.md 时，**派单 prompt 里带本契约的禁止项**，让共建者守同一标准：

> 你写的 agent.md body 是 LLM 运行时指令。禁开发期残留：plan/issue 编号（`§9.1`、`（I10`）、
> Orca 源码路径（`orca/exec/...`）、内部 examples 路径、测试项目名硬编码（`MNIST=...`）、
> SPEC/ADR/phase 编号。设计理由放 commit。详见
> `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`。

## 6. 审查法——受众翻转通读（替代 pattern-grep）

lint 的 regex 只能抓**你记得的**类别（pattern 表）；**忘了的类别**靠受众翻转通读 catch：

> 审查时假设「我是只懂 NAS / 业务、不懂 Orca 内部的 LLM agent」，逐句读 body，凡对执行任务
> 无帮助的开发上下文都 flag——包括 lint 没列的类别（如 SPEC 编号、ADR 编号、新出现的内部术语）。

grep 找你记得的；受众翻转抓你忘了的。**两者并用**才能覆盖。

## 7. deterministic 兜底 + 执行流程

`tars validate` 的 `_check_prompt_dev_residue` 会自动检测 §2 表中的 pattern（warning，不阻断
既有 workflow）。契约要求作者：

1. workflow 建完 / 改完后跑 `tars validate`；
2. 按 §6 受众翻转通读 agent.md body（catch lint 之外的残留）；
3. warning 清零 + 通读无疑 → workflow 视为「prompt 洁净」完成。

既有 workflow 残留（lint 报 warning）**不强制立即清理**——但新建 / 重构 workflow 必须 0 warning。
