---
name: create-workflow
description: >-
  生成或转换一个 Orca workflow（YAML + agent md）。当用户想新建一个多 agent 编排、
  或把已有的一堆 agent prompt / 别的格式的 workflow 转成 Orca 形态时使用。
  产出后自动跑 orca validate 自校验（0 error 才算完成），画草 DAG 报告给用户，
  直接落盘到用户指定路径或默认 ./workflows/。
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# create-workflow

<purpose>
把用户的意图（自然语言描述）**或**已有素材（一个文件夹里的 agent md / 别的 workflow / 散落的 prompt）
归一化成一个可跑的 Orca workflow（YAML + 必要的 `agents/*.md`）。skill 自己消化 Orca 契约，
用户不碰 schema、不选 agent 声明方式、不写 routes——这些全由本 skill 判定。
</purpose>

## 核心规则：pivot 到归一化 DAG

无论哪种输入，先在脑子里建一个**归一化 DAG**，它是 skill 的唯一中间模型：

```
节点（命名）──每个节点带：prompt 文本 / 是否有脚本资源 / executor+model ──> 控制流边
```

两种输入都汇到它：

- **形态 A（描述意图）**：用户说"我要个 X workflow"。
  → 据 X 推断需要哪几个 agent、各自职责、串行还是并行、要不要分叉合并、要不要循环或条件分支。
  → 起草 DAG + 每个 agent 的 prompt。

- **形态 B（已有文件夹）**：用户给一个路径（"我的东西在 `xxx/`，agent 在 `yyy/`"）。
  → 用 `Glob`/`Read` 扫该文件夹：`.md`（当 prompt 抽）、`.yaml`/`.json`（抽节点和边）、纯文本 prompt。
    **不假定输入形态**——CCW/maestro-flow/手写草稿都按"读出 agent + 读出顺序"通用抽取。
  → 抽出的 agent 和边灌进同一个归一化 DAG。

输出永远从 DAG 派生：Orca YAML + 必要的 `agents/*.md`。

## agent 三态自动决策（用户不选）

判定**只看节点的 prompt 来源**，不看长短：

| prompt 来源 | 用法 |
|---|---|
| **skill 自己起草**（形态 A，或形态 B 里需要补的节点） | **内联 `prompt:`**——一律内联，**不要**为它单独建 agent md |
| **prompt 片段文件**（`.prompt` / 明显是单次 prompt 文本，非角色定义） | **内联**——把文件内容读进节点的 `prompt:`，**不**建 agent md |
| **用户提供了独立 agent 角色 md** / 从外部 skill 转换来（形态 B，可复用角色） | `agent: <name>` + 把该 md 落到 `agents/<name>.md`（**引用，不重写**） |
| agent 要带脚本/资源（.py/.sh/refs） | 文件夹 agent `agents/<name>/agent.md` + `agents/<name>/scripts/` |

🔴 **铁律：同一 workflow 内常混用 inline + agent-ref**——用户给的 agent 角色 md 用 `agent:` 引用，prompt 片段 / skill 起草的补全节点保持**内联**。**绝不**把 skill 起草的节点或 prompt 片段也落成 agent md + `agent:` 引用（那是过度物化）。区分关键：**角色 md = 可复用人设**（"你是研究员…"）；**prompt 片段 = 单次任务指令**（"调研 X，输出要点"）。

🔴 **名称一致性铁律**：`agent: <name>` 里的 `<name>` 必须**逐字等于**落盘的 agent 文件/文件夹名（`agents/<name>.md` 或 `agents/<name>/agent.md`）。拼写、前后缀（如 `analyze` vs `analyzer`）、单复数必须**两端一致**——不一致则 resolver 找不到、`orca validate` 直接失败。落盘前自查节点 `agent:` 值与写出的文件路径同名。

> executor 默认按用户环境（项目约定 opencode + deepseek-v4-flash；用户没指定就默认 opencode）。

## 硬规则（这些是常见坑，必须遵守）

**H1 文件夹 agent 的目录契约**（有脚本资源时）：
- 布局固定：`agents/<name>/agent.md` + `agents/<name>/scripts/<file>`（脚本**必须**放 `scripts/` 子目录，不要平铺到 agent 根）。
- `agent.md` = frontmatter（`description`/`model`/`tools`）+ body prompt（**不要**写散文标题，要 frontmatter 契约形态）。
- 🔴 body 里引用自带脚本**必须**用 `$ORCA_AGENT_RESOURCES/scripts/<file>`（spawn 时 executor 注入该 env）。
  从 CC/opencode skill 转换时，原 skill 里的相对引用（如 `scripts/gen.py` / `python scripts/gen.py`）**必须重写**成 `$ORCA_AGENT_RESOURCES/scripts/gen.py`。脚原本体原样迁到 `scripts/`。

**H2 fan-in / 合并节点**（parallel 组之后、或多路汇合）：
- 🔴 **默认 `set`**：用户说"合并/汇总/汇聚/merge"但**没**明确要 LLM 理解 → 用 `set` 节点（无 token、确定）。只有用户**明确**要"综合/归纳/理解后合并/提炼"才用 `agent`。
- 取 parallel 分支输出**必须**用 `<组名>.output.outputs.<分支名>`，例：
  `summary: "A={{ fanout.output.outputs.researcher_a }} || B={{ fanout.output.outputs.researcher_b }}"`。

**H3 优先用 AgentNode 原生字段，别手搓编排**：
- 结构化输出 → `output_schema:`（agent 直接产 JSON），路由据此 `output.json.xxx` 判分支。
- 语义校验 + 不合规重跑 → `validator:`（`criteria` + `max_retries`），**不要**手写 script 校验 + when 分支循环。
- 瞬时失败重试 → `retry:`（`max_attempts`），**不要**自建重试编排。
- 🔴 **`validator` 与 `retry` 正交，别混**：`validator.max_retries` 管"语义校验失败 → 重跑 agent"；`retry.max_attempts` 管"transport/瞬时失败 → 重试"。用户要"不合规重跑"→ 加 `validator`；用户要"瞬时失败重试"→ 加 `retry`；**两者都要 → 两者都加**，绝不用一个替代另一个。
- 🔴 `outputs:` 模板里取 agent 整段输出用 `{{ node.output }}`，**不要**加 `.json`（`.json` 只在 `when:` 路由里访问结构化字段时用，如 `output.json.kind`；`outputs:` 里 `.output` 已是整段结果）。

**H4 workflow 必须有 `outputs`**：终态输出映射至少暴露末节点产物，例 `result: "{{ last.output }}"`（script 节点用 `.stdout`）。无 `outputs` 的 workflow 链尾无出口，下游无法消费——**始终补上**。

**H5 散 agent md → 引用而非重写**（形态 B 组装）：用户给了 agent md 文件 → 用 `agent: <name>` 引用 + 原样落到 `agents/`；**不要**把它们的 prompt 改写成内联、也不要擅自加 `{{ 上游.output }}` 数据传递（用户只给顺序就只连控制流）。

**H6 节点最小化 / entry 即分支**：别加冗余节点。parallel 的某个分支若能直接当入口（无需前置准备），就**以它为 `entry`**（Orca 对已执行节点幂等跳过），不要额外加 starter/set 启动节点。每个 agent 节点显式写 `executor` + `model`，**model 用 `provider/name` 全名**（如 `deepseek/deepseek-v4-flash`，别只写 `deepseek-v4-flash`）。

**H7 script 节点 vs 文件夹 agent（别混）**：
- `script` 节点 = 在 **cwd** 跑 shell 命令，**不迁移脚本、不打包资源**。用户给散脚本串成链 → 用 `script` 节点 + `command: "python fetch.py"`（引用 cwd 下脚本），脚本**留原位**（用户给的 assets/ 或 cwd），**绝不**把脚本 copy 到 `workflows/scripts/`。
- 只有**文件夹 agent**才迁移脚本到 `agents/<name>/scripts/` + `$ORCA_AGENT_RESOURCES` 引用（H1）。
- 判据：用户说"串脚本/跑脚本"且脚本无 agent 人设 → script 节点链；用户说"封成 agent 跑某脚本"→ 文件夹 agent。

**H8 `description` 可区分（tars 靠它选 wf）**：
- 🔴 `description` 用**一两句话**说清这个 workflow 的**功能与目的**——它是 `orca list` 里 tars 语义匹配意图、用户识别 wf 的唯一信息。
- 生成前先 `orca list` 看现有 description，确保新的与它们**有明确区别**。
- 若与某个已有 workflow **无明确区别**（描述撞车或只是换皮）→ **问用户**本质区别（1-2 个业务问题，参上文「模糊就问业务」），据此写可区分的 description，**不要**闷头生成含糊或撞车的描述。

## 产出过程（通用，非死步骤）

0. **素材就近读、别派探索子任务**：契约参考 + crib 例子就在本 skill 同目录（`reference/` + `examples/`），**直接 `Read` 它们**——不要 spawn explore/search 子任务去翻用户代码库（慢且无关）。用户提供的素材在 `assets/` 或指定路径，`Read` 即可。
1. **归一化**：按上面建 DAG。模糊就问业务问题（最多 1-2 个），别问 schema。
2. **定落盘路径 + 直接写**：
   - 路径规则：用户在请求里指定了路径 → 用它；否则默认 `./workflows/<name>.yaml`（agent md 落同级 `./workflows/agents/` 或 `<workflow_dir>/agents/`）。
   - **直接 `Write` 到该最终路径**——不要先写 `/tmp` 再搬（避免落盘到工作区外、避免多段搬运出错）。
   - **不要用 `AskUserQuestion` 阻塞问路径**：默认就写 `./workflows/`，写完告诉用户路径，ta 想改自己移。
     非交互/headless 环境下任何 y/n 确认都无法应答，会卡死——故**全程不阻塞等待确认**。
3. **强制自校验**（不可跳过）：对**最终路径**的 yaml 跑
   ```bash
   orca validate <最终yaml路径>
   ```
   - 退出码非 0 → 读 stderr，**自己改**，再验。循环直到 0 error。warnings 可接受但要跟用户提一句。
   - 拿不到 `orca` 命令就退而用 `tars validate`（同入口）。
4. **画草 DAG 报告给用户**（非阻塞，已落盘）：节点名 + 箭头，不美化。parallel 用括号组，`$end` 收尾。例：
   ```
   finder → [researcher_a | researcher_b] → merger → $end
   ```
   条件分支标在箭头上：`decide ──(output.ok)──> go ──(else)──> terminate(failed)`。
   最后一句告诉用户最终落盘路径 + 校验结果。**不要问"是否确认"**。

## 输出语言

YAML 字段名、`kind`、`executor` 等是固定契约（见参考，别改）。prompt 文本、`description`、
`name` 用用户的语言（中文就中文）。Jinja2 占位符 `{{ inputs.x }}` / `{{ <node>.output }}` 照抄。

## 契约在哪

完整字段表 / routes 语义 / agent md 格式 / validate 错误类别 / 12 条正确性 cheatsheet 在：
**`reference/orca-workflow-contract.md`**（与本 SKILL.md 同目录）。生成前读它，schema 改了只动那个文件。

<success_criteria>
- [ ] 产出的 YAML 通过 `orca validate`（0 error）
- [ ] 每个可达路径都终止（`$end` 或 `terminate`）
- [ ] workflow 有 `outputs`（H4）
- [ ] skill 起草的节点保持**内联**；只有用户给的/外部转换的 agent 才用 `agent:` 引用（铁律）
- [ ] 文件夹 agent：脚本在 `scripts/` 子目录、引用用 `$ORCA_AGENT_RESOURCES/scripts/...`、agent.md 带 frontmatter（H1）
- [ ] 合并节点按是否需推理选 `set`/`agent`，且引用 `<组>.output.outputs.<分支>`（H2）
- [ ] 校验/重试用原生 `validator`/`retry` 字段，非手搓编排（H3）
- [ ] 已落盘到最终路径 + 画了草 DAG 报告（非阻塞）
- [ ] `description` 一两句说清功能目的，且与 `orca list` 现有 workflow 有明确区别（无区别则问了用户）（H8）
</success_criteria>
