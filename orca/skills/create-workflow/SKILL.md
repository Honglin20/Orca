---
name: create-workflow
description: >-
  生成或转换一个 Orca workflow（YAML + agent md）。当用户想新建一个多 agent 编排、
  或把已有的一堆 agent prompt / 别的格式的 workflow 转成 Orca 形态时使用。
  产出后自动跑 tars validate 自校验（0 error 才算完成），画草 DAG 报告给用户，
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

🔴 **名称一致性铁律**：`agent: <name>` 里的 `<name>` 必须**逐字等于**落盘的 agent 文件/文件夹名（`agents/<name>.md` 或 `agents/<name>/agent.md`）。拼写、前后缀（如 `analyze` vs `analyzer`）、单复数必须**两端一致**——不一致则 resolver 找不到、`tars validate` 直接失败。落盘前自查节点 `agent:` 值与写出的文件路径同名。

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

## input 定义准则（三档原则）

> 权威 SPEC：`docs/specs/workflow-input-design-principle.md`。**inputs 只放「下游 agent 无法执行 / 会失控」的必须项**；其余按性质下沉到 Tier B（代码事实，agent 推断）或 Tier C（工程默认，固化）。

**判定总纲**：代码里能 grep 出来的是事实（→ Tier B）；代码里不存在的是意图（→ Tier A）。会静默产出错误交付物的回退路径必须 fail loud / 问用户（**永不 silent default**）。

### 三档分类 + 标签约定（每个 input 的 `description` 以标签起头，供 in-session 编排器/tars skill 读取）

| 档 | 标签 | 性质 | 应放在 |
|---|---|---|---|
| **Tier A** | `[ask]` | 业务决策（意图/预算/KPI/硬件/模型入口/业务命令）；agent 读不到、缺它 workflow 会失控 | **input**（必填） |
| **Tier B** | `[infer]` | 代码事实（agent 读用户代码可得）；缺失走 **ask-user 哨兵**（绝不造假） | **setup 节点 `output_schema` 字段**（不是 input） |
| **Tier C** | `[default]` | 有合理工程默认，99% 用户不该决策 | **固化**：yaml `default` / agent.md 模板 / 脚本默认 |
| **Tier C 子集** | `[advanced]` | 罕见 override，固化默认，文档可见但不暴露为主 input | yaml `default`（带 [advanced] 标签） |

**Tier A 子类（必填 input 的判据）**：模型入口（`model_path` / `teacher_model_path`）/ 业务命令原样执行（`train_command` / `test_command`）/ 业务 KPI（`target_latency_ms` / `accuracy_target` / `accuracy_gap_db` / `accuracy_tolerance`）/ 预算闸门（`max_rounds` / `max_evals`，被确定性脚本消费非 LLM 自决）/ 目标硬件（`target_hardware` / `device`）/ 复现性底座（`seed`，默认 0，**全部 workflow 必须有**）。

**Tier B 典型项**（setup 节点 infer-once + propagate，下游 `{{ setup.output.X }}` 取）：`project_root` / `build_fn` / `dummy_input` / `model_family` / 数据 loader dotted-path（`calib_data_ref` / `train_data_ref` / `eval_data_ref`）/ 评估函数（`eval_fn_ref`）/ 训练超参（`lr` / `batch_size` / `epochs`，**注意**：默认值是 smoke 不是生产，需 `smoke` 开关）。

**Tier C 典型项**（固化，绝不作 input）：`output_dir`（走引擎注入的 `$ORCA_ARTIFACTS_DIR`）/ `iterations`（由 `max_rounds × 每轮节点数` 自动算；用户要覆盖用 `--max-iter` CLI）/ 算法开关预设（`mode` / `recipes` / `scheme` / `bit_width(s)` / `granularity` / `method` / `ratio` / `bake` / `cage` / `proxy_dataset_spec`）/ 工程路径（`*_scripts_dir` / `kb_cache_dir`，落 setup output 字段向后传）。

### 反向判据（满足任一条 → 强制下沉，否决 KEEP 作 input）

- 能在 `model.py` / `train.py` / `config.yaml` grep 到 → Tier B（infer）
- 改它需要懂 workflow 内部 → Tier C（固化）
- 留空有合理默认且非业务 KPI → Tier C
- 与代码事实会漂移（用户改代码忘改 input）→ **必须** Tier B

### 「向后传」的唯一可靠模式：infer-once + propagate

在 setup 节点集中推断一次，写进 `output_schema`，下游用 Jinja `{{ setup.output.X }}` 取。**严禁「每个 agent 各自重新自找」同一事实**（违反 DRY、自找不一致时远端崩、破坏复现）。黄金模板：`workflows/agent-struct-exploration.yaml` 的 `setup` 节点（`project_root`/`build_fn`/`dummy_input`/`struct_scripts_dir` 全下沉为 output 字段）。

### Tier B 缺失：ask-user 哨兵（绝不造假）

Tier B 项读代码无果时，agent **不要**造假（`torch.randn` / 复用 train 当 eval / 静默默认空 loader / 套常见 shape 默认），以**最终消息**返回轻量哨兵 JSON：

```json
{"_orca_ask_user": "<一句话问题>",
 "options": ["<候选 1>", "<候选 2>"],
 "context": "<已 grep 过什么、看到了什么、缺哪项>",
 "_sentinel": "orca_ask_user_v1"}
```

（**两键必填**：`_orca_ask_user` + `_sentinel:"orca_ask_user_v1"`；TARS skill strict 识别魔键 → 问用户 → SendMessage/Task(task_id) 恢复**同一**子 agent → MAX_ASK=3 兜底；哨兵**不进 `orca next`**，引擎零改动。详 `docs/specs/agent-ask-user-sentinel.md`。）

**每个含 Tier B 项的 agent.md 必加「## 缺失必填输入时（严禁造假）—— ask-user 哨兵」段**（紧贴 `## 输出` 之前）：列本节点 Tier B 项 + 不造假禁令 + 哨兵 JSON 示例 + 会被恢复说明 + fail_loud fallback。

### 生成模板时的默认动作

- workflow YAML 默认**只含 Tier A inputs**（含 `seed` 默认 0）；
- Tier B 写成 setup 节点 `output_schema` 字段 + agent.md「读代码→哨兵→fail loud」段；
- Tier C 固化：`output_dir` 走 `$ORCA_ARTIFACTS_DIR`、`iterations` 不作 input、算法开关固化默认；
- 每个 input 的 `description` 以 `[ask]` / `[infer]` / `[default]` / `[advanced]` 标签起头。

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
   tars validate <最终yaml路径>
   ```
   - 退出码非 0 → 读 stderr，**自己改**，再验。循环直到 0 error。warnings 可接受但要跟用户提一句。
   - （`orca` 是 in-session shell，无 validate 子命令；校验一律走 `tars validate`。）
   - validate 通过后**必跑 input 三档 checklist**（详 SPEC §6）：
     - [ ] 每个 input 归类到 Tier A 四子类之一（模型入口/业务命令/KPI/硬件/seed），否则下沉
     - [ ] Tier B 项有 setup 节点 `output_schema` 字段承接（infer-once + propagate，链不破）
     - [ ] Tier B 项在 agent.md 有「读代码→哨兵→fail loud」契约段
     - [ ] `output_dir` 不作 input（走 `$ORCA_ARTIFACTS_DIR`）
     - [ ] `iterations` 不作 input（自动算 / 用户 `--max-iter` 覆盖）
     - [ ] 算法开关 / 预设（mode/recipes/scheme/bit_width/bake/granularity 等）都不作 input（固化）
     - [ ] 业务 KPI 不缺（latency / accuracy / max_rounds / target_hardware 至少齐其相关项）
     - [ ] **workflow 有 `seed`（默认 0）**
     - [ ] 每个 input 的 `description` 以 `[ask]`/`[infer]`/`[default]`/`[advanced]` 标签起头
     - [ ] 移除任何 input 时，同步更新所有引用 `{{ inputs.X }}` 的 agent.md Jinja（避免 StrictUndefined 崩）
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
- [ ] 产出的 YAML 通过 `tars validate`（0 error）
- [ ] 每个可达路径都终止（`$end` 或 `terminate`）
- [ ] workflow 有 `outputs`（H4）
- [ ] skill 起草的节点保持**内联**；只有用户给的/外部转换的 agent 才用 `agent:` 引用（铁律）
- [ ] 文件夹 agent：脚本在 `scripts/` 子目录、引用用 `$ORCA_AGENT_RESOURCES/scripts/...`、agent.md 带 frontmatter（H1）
- [ ] 合并节点按是否需推理选 `set`/`agent`，且引用 `<组>.output.outputs.<分支>`（H2）
- [ ] 校验/重试用原生 `validator`/`retry` 字段，非手搓编排（H3）
- [ ] 已落盘到最终路径 + 画了草 DAG 报告（非阻塞）
- [ ] `description` 一两句说清功能目的，且与 `orca list` 现有 workflow 有明确区别（无区别则问了用户）（H8）
- [ ] **input 三档**：每个 input 归 Tier A 四子类之一，`description` 以 `[ask]`/`[infer]`/`[default]`/`[advanced]` 标签起头；Tier B 下沉为 setup output；Tier C 固化（`output_dir`→`$ORCA_ARTIFACTS_DIR`、`iterations` 不作 input、算法开关固化）；workflow 有 `seed` 默认 0；含 Tier B 的 agent.md 有「读代码→哨兵→fail loud」段
</success_criteria>
