# 产物写作规范（去考古化 + 普适性）

> 适用范围：workflow.yaml（含节点注释）+ `agents/*.md` + agent 文件夹的 `SKILL.md` + workflow 与 agent 的 `description` + **被生成物逐字复制的模板文件（`references/templates/*.py`、`*.sh`）**。
> 与 [`orca-workflow-contract.md`](orca-workflow-contract.md) 并列：那份管「字段/结构对不对」，本份管「写给谁、什么语气」。

## §0 一句话心智

产物是「产品说明书」（写给**用**这个功能的人/agent），不是「设计日志」（写给**做**这个功能的你）。
考古留 git commit 和 `docs/specs/`，不进 `agent.md` / `SKILL.md` / `workflow.yaml`。

## §1 受众分离

- 受众 = LLM 执行者 + 未来复用者，**不是**项目作者本人。
- 判据：把文件给一个不了解项目历史的人看，凡是要"这个项目的历史 / 上一版 / 前身"才看得懂的句子 → 删掉或改成自包含陈述。
- 开发考古（这个 skill 从哪迁来 / 这个约束来自哪个 spec issue / 某版本嵌入了哪个节点 / spec-review 改了什么）→ 放 `docs/specs/` 或 commit message，**绝不进产物**。

## §2 只答三问：what / input / output

`description`、`prompt`、`agent.md` 只回答：**做什么 → 输入要什么 → 输出给什么**。
不回答 why-history（为什么历史上这么设计、前身是谁、删了哪些旧特性）。

反例（考古体）：
> "This is the KD-NAS analogue of the NAS `supernet-train-script` skill, with sandwich sampling / DDP / torchrun removed and KD provided by ... instead of `nas_agent.train.distillation`."

正例（产品体）：
> "生成自包含的 `train_pipeline.py`，支持 teacher / distill / eval 三模式，单卡，按路径加载模型，KD 逻辑来自 `kd.compose` 库。"

## §3 三层分工：workflow 薄 → agent 厚 → 脚本实

- `workflow.yaml` 只写编排拓扑（谁→谁、传什么字段、什么条件短路），**不描述"怎么做"**。
- "怎么做"沉到 `agent.md`。
- 能确定性的沉到 `.py` 脚本（latency 测量、计数、选择全用脚本，LLM 只回显 stdout）。—— 对齐项目 rule 5（deterministic 优先，模型只做判断）。

## §4 契约即护栏，不靠 LLM 自觉

把关键正确性（真跑了、records≥1、达标数）做成 `output_schema` + `when` 路由的**引擎层硬检查**，
不要写成"请 agent 务必认真"。参考 `workflows/nas-agent-pipeline.yaml` 的 `train_runner`：`search_records(integer, minimum: 1)` 强制从真文件计数输出——散文复述 / 没真跑 → `output_schema_mismatch → node_failed`。

## §5 能力黑盒 + 复用优先

- agent 是可复用能力单元，`description` 写清边界（做什么、不做什么）。
- 新 workflow **拼** agent，不重写 agent；节点 `description` 明写"复用 X / Y"。

## §6 红线 > 解释

不该做的事用一行「❌ 违反即失败」列出来，比三段"因为历史上……所以要避免……"有效。
参考 `workflows/agents/nas-select/agent.md` 的「⚠ 你的唯一任务」+「🔴 铁律」结构。

## §7 起手模板

- 新 agent：照 `workflows/agents/nas-select/agent.md`（55 行）—— 唯一任务 → 资源锚点 → 执行命令 → 红线 → 输出格式。
- 新 SKILL：照 `workflows/agents/nas-search-pipeline/SKILL.md` —— 产出什么 → 需要什么输入 → 分步怎么做 → 验证。

## §8 考古 vs 导航自检（生成后必跑）

产物里的章节/编号引用分两类，grep 时区别对待：

**A 类 = 开发考古（命中即 FAIL，改写）**：

| 模式 | 含义 |
|---|---|
| `[A-Z]+-[0-9]+` | 开发 issue / 里程碑编号（`BLK-3` / `BUG-3` / `HI-1` / `U-1` / `LO-5` 等，大写前缀+数字；含单字母前缀如 `U-1`、`P-5`、`T-2`） |
| `迁移自` / `analogue of` / `leaves off` / `前作` / `前身是` | 迁移出处 / 前身叙述 |
| `v[0-9]+ 已嵌入` | 版本考古 |
| `spec-review` / `spec_review` | 评审记录泄漏 |
| `plan [a-z-]+ §` | 临时 plan 文件的章节（如 `plan sprightly-questing-donut §2.3`） |
| `SPEC 20[0-9]{2}-` | 带日期的 SPEC 草稿引用（如 `SPEC 2026-07-23 §3.3`） |

**B 类 = 精确导航（允许，不算命中）**：引用**仓库内长期存在的真实文件**的章节/条目——
`CONTRACTS.md §6`、`docs/specs/agent-ask-user-sentinel.md §3`、`workflow §1`（指 references/workflows 文档章节）、`checklist item 7`（references/workflow-checklists 文件条目）、本文档内部 §N。

判据：被引文件是否长期存在于仓库、读者能否据此定位。临时 plan / 带日期草稿 / issue 编号 = 不能定位 = 考古；真实文件章节 = 能定位 = 导航。

**例外**：本规范文档自身、`docs/specs/` 下的设计日志、CHANGELOG / release note，**以及跨 agent 的契约 / 决策溯源文档（如 `workflows/<wf>/agents/<agent>/CONTRACTS.md`、`workflows/agents/_po_scripts/PROFILER_CONTRACT.md`）**不受此约束（它们本就是设计日志）。

## §9 自检清单（生成 / 改 workflow 后必跑）

- [ ] grep 产物无 §8 A 类开发考古命中（B 类真实文件导航 §N 允许）
- [ ] `description` 一两句说清功能目的（what），无迁移出处 / 版本嵌入
- [ ] `agent.md` 开头是"你的任务"，不是"这个 skill 的历史"
- [ ] `workflow.yaml` 节点注释只描述拓扑 / 契约 / 路由，不含 spec issue 编号
- [ ] 红线用 ❌ 列举，不写历史成因长段
- [ ] **宽口径 grep 兜底**（标准正则 `[A-Z]+-[0-9]+` 抓不到的无连字符开发编号）：额外 grep `P[0-9]|Increment [A-Z]|code-reviewer|review #[0-9]|SR[0-9]|finalize 20[0-9]{2}|演进历史|前身是`，命中按 §1 判据决定删/留
- [ ] `tars validate <yaml>` 0 error（validate 只校验结构，不检考古——必须靠上面 grep 兜底）
