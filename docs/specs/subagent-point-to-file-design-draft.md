# Design Draft v2 — Subagent Point-to-File + Echo Constraint

> 状态：v2（两轮 spec review 闭环后）。跨 phase 设计议题，agent prompt 改写前必读。
> 范围：**仅 nas-supernet workflow** 的 6 个 parent agent 节点 + `subagents/nas-supernet/` 5 个子 agent md。
> 日期：2026-08-05。v1→v2 diff 见 §11；两轮 review 的 closed-loop issue map 见 §10。

## 1. 背景与范围

nas-supernet 的 6 个 agent 节点（`ns_expand_supernet` / `ns_train_script` / `ns_search_pipeline`
/ `ns_run_train` / `ns_run_search` / `ns_retrain`）现行用 **read+embed 协议** 调 5 个子 agent
（`supernet-evaluator` / `workflow-verifier` / `memory-verifier` / `project-porter` /
`project-fidelity-verifier`）：parent `cat` 取 md body 再内联进 Task prompt，**子 agent 被投喂 body、
自己不读**（用户意图是子 agent 自读）。且规定路径 `~/.orca/nas-supernet/subagents/` 实际为空（install
从未跑过 nas-supernet），run 期靠 parent 自行 fallback 到 repo 绝对路径才 work（tape
`runs/nas-supernet-20260804-230305-b0c17d.jsonl` seq 51 `"no subagents dir"` → seq 55 fallback）。

**本草稿改成**：point-to-file（子 agent 自读 md）+ **render 期 Jinja inline 绝对路径**（不引新 env var）
+ sentinel 回显约束（E-1 软回显 + 阈值升级）。

**范围严格限定**（reviewer/evaluator issue #6/#6 闭环）：
- ✅ nas-supernet 6 个 parent agent.md + `subagents/nas-supernet/` 5 个子 agent md。
- ❌ kd-nas **不在范围**：实测 kd-nas 无独立 subagents 目录；kd-nas 的 workflow-verifier 是
  `kd-train-script/SKILL.md` 内联模板（line 217-260），与 `subagents/nas-supernet/workflow-verifier.md`
  **名同实异**。若未来统一另起 SPEC。
- ❌ 不改子 agent md 的 Procedure / 输出契约（只加 frontmatter sentinel 头）。
- ❌ 不改 workflow yaml 节点拓扑 / output_schema / 路由。

## 2. 目标 / 道德诚实度

**目标**

1. 子 agent 自己用 Read 读 md 拿身份 + Procedure + 输出契约；parent 不再内联 body（结构收益：
   子 agent 自持角色定义）。
2. md 路径 **render 期 inline 绝对值**，cwd 无关、shell 无关、与 `tars install` 解耦。
3. 「子 agent 真读了 md」可机检（sentinel 回显，E-1 起步）。
4. 依赖铁律不破（compile→exec→iface 单向）。

**诚实声明（evaluator #11 闭环）**：本设计的**主要收益是结构性的**（子 agent 自持身份定义、parent
context 不再夹带 24K body），**不是 token**。token 上：parent 端省一次 `cat`（24K × 1 次）；子 agent 端
fresh-Task loop n 轮下，自读 body 与被 embed body 各吃 n × body，**持平**。n 大时总账节省 <10%。
v1 §1.1「token 双倍付费」措辞误导，v2 删除——决策基于结构正当性，不基于夸大的 token 论据。

## 3. 设计：point-to-file 协议

### 3.1 协议段（替换各 parent agent.md 的「Subagent 调用协议（read+embed）」段）

```
## Subagent 调用协议（point-to-file）

本节点调以下子 agent（全名，禁简写）：<列出该节点实际调的子 agent>。
它们的 body 存 {{ subagents_root }}/<name>.md（render 期 inline 为绝对路径，cwd 无关）。
host 无需注册——子 agent 自读 body + 执行。

调用 <name>（首轮）：
Task(subagent_type=<host 内置通用类型>,
     prompt="先完整 Read {{ subagents_root }}/<name>.md，严格按其 Procedure 执行本轮任务。
             本轮 inputs：<具体 inputs>。
             按 md 规定的格式 return。
             **report 首行**必须照原样回显你 Read 到的 md frontmatter 里的 sentinel 字段
             （格式见 md 顶部；不要猜，不要从本 prompt 推——必须来自你 Read 的文件）。")

调用 <name>（多轮 verifier loop 续轮）：在首轮 prompt 末尾追加
     <上一轮完整 report 原文> + Fixed:[ids]/Context:[id]。
```

**关键设计点**：
- **parent 全程不碰 body**；fresh-Task loop「每轮重 embed body」消失（body 由子 agent 每轮自读）。
- **每次 Task 是 fresh subagent**（opencode `task` 工具语义：stateless，每次新建上下文，tape seq 184
  实证 `subagent_type="general"`）。子 agent 单轮单次 Read body，不跨轮累积。
- **续轮 report 不视为 body**，不计入 §8 验收 #4 体量阈值（#4 仅约束「md body 不进 parent context」）。
- **sentinel 字面量绝不出现在 parent prompt**（evaluator #9 闭环）：parent 只说「回显你 Read 到的
  frontmatter sentinel」，不给出具体值。这样跳读的子 agent 无法从 prompt 推出 sentinel，无法伪造回显。

### 3.2 路径交付：render 期 Jinja inline（evaluator #1/#2 闭环——**不引新 env var**）

- `orca/exec/render.py` 的 render namespace（`_namespace(ctx)`，仅见 `RunContext`）增顶层变量
  `subagents_root`（绝对路径字符串）。
- **解析发生在 runtime**（非 compile 期——注意 `compile/agents.py:101 context.workflow_dir` 是 compile 期
  `ResolveContext`，与 render 期 `RunContext` 是不同对象，勿混）：orchestrator 持有
  `self._workflows_root`（= yaml_path.parent）+ `wf.name`，计算
  `subagents_root = str(p) if (p := workflows_root / "subagents" / wf.name).is_dir() else ""`，
  经 **RunContext 新字段 `subagents_root`** 透传到 render 层（字段明细见 §4）。
- agent.md body 写 `{{ subagents_root }}/<name>.md`，render 期被替换为绝对路径（如
  `/d/Projects/Orca/workflows/subagents/nas-supernet/supernet-evaluator.md`）。
- **shell 无关**：render 发生在 exec 层、在任何壳（CLI / Web / in_session）拿到 body 之前。render 后的
  body 已是绝对路径文本，不依赖任何 runtime env——彻底绕开 evaluator #2 指出的「in_session 壳不透
  `$ORCA_WORKFLOWS_ROOT`」既有缺口（该缺口是 kd_scripts 的先在 bug，本 SPEC 不修，仅标注）。
- **目录拓扑（v3，用户裁决）**：subagent 是 workflow 特有资源，统一收纳在
  `workflows/subagents/<wf-name>/`（不再用 `_<wf>_subagents/` 平铺 sibling——多 workflow 会污染 workflows/
  根目录）。结构：
  ```
  workflows/
    nas-supernet.yaml ...（根目录永远只有 yaml）
    agents/                    ← 现有全局 agent 池，不动
    subagents/                 ← 统一收纳，按 workflow 分家
      nas-supernet/
        supernet-evaluator.md
        workflow-verifier.md
        ...
      <其它wf>/                 ← 仅该 wf 真有 subagent 才出现
  ```
- **install 关系（reviewer Q1/Q5 闭环）**：`_install_bundled_subagents`（`install_cmds.py:548`）从旧的
  glob `_*_subagents` → `~/.orca/<wf>/subagents/`（非 sibling，导致现行断链）改为 **copytree 整树**
  `workflows/subagents/` → `~/.orca/workflows/subagents/`（与 `_install_bundled_workflows` 复制 `agents/`
  同款 copytree，L505-517）。dev 态 `subagents_root` = `<repo>/workflows/subagents/nas-supernet`，零 install
  依赖。**注意（reviewer3 B1 加固）**：仅当 yaml 在 repo `workflows/` 内时零 install 依赖；把
  nas-supernet.yaml 拷出 repo 再 `orca run` 需先 `tars install`（`subagents/` 随 install 落到新 yaml 同级）。
- **迁移**：把现有 `workflows/_nas-supernet_subagents/`（5 个 md）整体移到
  `workflows/subagents/nas-supernet/`，删旧 `_nas-supernet_subagents/` 目录。唯一现存受影响目录。

### 3.3 默认行为（reviewer #5 闭环）

workflow 无 `subagents/<wf>/` 目录（如 quant-ptq-sweep / agent-struct-exploration）→ `subagents_root`
= 空串 → 该 workflow 的 agent.md 不应引用 `{{ subagents_root }}`（无子 agent 可调）。render 期不报错；
§6 Q4 的 compile 校验也只在目录存在时跑。

## 4. 引擎改动面（reviewer3 B1 修订：6 行准确清单）

> reviewer3 发现：`_namespace(ctx)` 仅见 `RunContext`，而 `RunContext` 无 workflow_dir 字段 → `subagents_root`
> 必须经 RunContext 新字段透传。v2 早先「2 行」清单漏算，此为准确版。

| 文件 | 改动 |
|---|---|
| `orca/exec/context.py` | `RunContext`（frozen dataclass）加字段 `subagents_root: str = ""`（默认空串向后兼容；`dataclasses.replace` 自动携带，node 间派生不丢字段） |
| `orca/exec/render.py` `_namespace` | 注入 `ns["subagents_root"] = ctx.subagents_root` |
| `orca/run/orchestrator.py`（4 处 RunContext 构造：L155/523/554/850） | populate `subagents_root = str(p) if (p := self._workflows_root / "subagents" / wf.name).is_dir() else ""`（v3 公式；`_workflows_root` 已存于 L184） |
| `orca/run/step.py:248` + `orca/iface/cli/app.py:1065`（另两处 RunContext 构造） | populate 同上；orchestrator 上下文可达则算绝对路径，否则空串（无 subagents 的 workflow 走空串分支，§3.3） |
| `orca/iface/cli/install_cmds.py:548` `_install_bundled_subagents` | 改 copytree 整树 `workflows/subagents/` → `~/.orca/workflows/subagents/`（替代旧 glob `_*_subagents` → `~/.orca/<wf>/subagents/`）。与 `_install_bundled_workflows` 的 agents copytree（L505-517）同款、不同子树（`agents/` vs `subagents/`）、互不覆盖；**不合并**进 `_install_bundled_workflows`。 |

**Web（phase 9）/ MCP（phase 10）/ in_session 壳：零改动**（reviewer #7 闭环）。三壳都消费 render 后的
body，绝对路径已 inline，不依赖壳侧 env 透传。

依赖方向：`exec/context.py`（加字段）← `exec/render.py`（读字段）；`run/orchestrator.py`（populate）→
`exec/context.py`。单向，无反向调用。`install_cmds.py` 改复制目的地，不涉依赖方向。

**populate 一致性风险（reviewer3 新风险 #2）**：6 处 RunContext 构造点任一漏 populate → agent.md 引用
`{{ subagents_root }}` 渲染成空串、子 agent Read 失败，且 `StrictUndefined` 不兜底（字段有默认空串）。
**缓解**：§9 加 unit 断言「nas-supernet 场景 6 处构造点 `subagents_root` 非空」；§7 compile 校验补「agent.md
引用 `{{ subagents_root }}` 但 render 期为空串 → fail loud」。

## 5. Sentinel 回显约束（E-1 软回显 + 阈值升级）——详细设计

> 用户裁决（Q3）：E-1 起步 + 阈值升级。本节落地 evaluator #4/#9/#10/#13 的全部修正。

### 5.1 要防的退化

- **R1 跳读**：子 agent 没执行 Read，凭 prompt 碎片 + 通用知识幻觉 report。
- **R2 读错路径**：Read 了别的文件（路径解析错 / 旧缓存）。
- **R3 版本漂移**：部署旧版 md，子 agent 读到的与预期不一致。

### 5.2 frontmatter（S-a，reviewer Q2 已采纳）+ 严格解析（evaluator #13）

每个子 agent md 顶部加 frontmatter：
```markdown
---
subagent: supernet-evaluator
version: 1
sentinel: se-7K2a9
---
```
- `subagent`：身份名（= 文件名 stem）。
- `version`：整数，md 不重构不变（默认 1）。
- `sentinel`：**每文件唯一短 token**（如 6 位 base32），一次性生成、写入 frontmatter 后不动。**这是
  R1 伪造回显的防线**——parent prompt 不含此值，跳读者无从猜。

**严格解析（evaluator #13）**：frontmatter 仅取首块 `^---\n(.+?\n)---\n`（strict regex，非整文件 yaml
parse）。body 后续 `---`（markdown hr / 表格分隔）不解析。consumer（parent 校验回显、lint 扩展）统一
用此 regex。需 unit 覆盖「body 含 `---` hr 不被误判」。

### 5.3 回显规则与校验（E-1）

子 agent report **首行**必须为（值来自其 Read 的 frontmatter，非 parent 给）：
```
[subagent:supernet-evaluator v1 se-7K2a9]
```

parent 读 report 时：
- **E-1（现行）**：regex 匹配首行 `^\[subagent:(.+?) v(\d+) ([A-Za-z0-9]{4,})\]$`。命中 → 正常处理 report
  正文（strip 首行后判 all-pass / LGTM）；**不命中 → 写一条 sentinel-miss 记录**：
  - 落地：`runs/<run_id>/sentinel_stats.jsonl` 追加一行 `{node, subagent, hit: false, ts}`（reviewer
    #4「warn 落地点」闭环），同时 logger.warning。**不阻断** workflow。
  - parent 仍按 report 正文正常推进（E-1 不 fail）。
- **R3 版本漂移（evaluator #10 闭环）**：parent **不预设期望 version/sentinel**（避免 prompt 漂移源）。
  仅校验首行格式合法 + 非空。version/sentinel 的「与部署一致」由 git/部署流程保证，不进 prompt——v1 §5.4
  「version 编译进 agent prompt」方案废弃（它本身就是漂移源）。

### 5.4 E-2 升级触发（reviewer Q3 + evaluator #4 闭环）

累计跑 nas-supernet e2e **≥5 次**后，统计 `sentinel_stats.jsonl`：sentinel 缺失率（`hit:false` 占总
子 agent 调用比）**> 15%** → 升级到 E-2 硬闸门：
- 首行不命中 → parent 重发一次 fresh Task，prompt 加粗 `你必须先 Read .../<name>.md 并在 report 首行
  照原样回显 frontmatter sentinel`。
- 二次仍不命中 → fail loud：parent 在自身 `output_schema.error` 写明 `<name> 未回显 sentinel，疑似未读
  md`，output_schema + validator 双层兜底判败。

**预期**：指针 prompt 短且显式（「先完整 Read」）+ sentinel token 不可猜，跳读 + 伪造回显同时受阻，
E-1 应足够。但阈值机制保证数据驱动升级，不永久搁置。

### 5.5 host subagent_type（reviewer Q7 + evaluator #3 闭环）

- agent.md 写 `subagent_type=<host 内置通用类型>`（**meta 描述，非字面量**）。运行态由 host 内 LLM
  解析为当前壳的通用类型（opencode = `general`，tape seq 184 实证；claude 壳 = claude 通用类型）。
- **禁止**在 6 份 agent.md 各自硬编码 `general`（一改全改 + 单壳耦合，违背三壳共用契约）。
- **Read 工具前置校验**：host 通用类型须含 Read。compile 期 validator 增一条：agent.md 引用
  `{{ subagents_root }}` 的节点，其 host 通用类型 tools 须含 Read——缺则 ConfigurationError（fail loud
  前移 compile）。

## 6. 闭环裁决表（reviewer Q1–Q7 + evaluator #1–#13）

| 问题 | 裁决 | 落地 |
|---|---|---|
| Q1 路径拓扑 | **`subagents/<wf>/` 命名空间（用户 v3 裁决）** | §3.2 统一收纳 + render inline |
| Q2 sentinel 放置 | **S-a frontmatter** | §5.2 |
| Q3 回显硬度 | **E-1 + 阈值升级（用户选）** | §5.3/§5.4 |
| Q4 compile 校验 | **加，触发条件：目录存在** | §7 validator 仅 `subagents_root` 非空时校验 md 完整 |
| Q5 install 关系 | **copytree subagents/ 整树** | §3.2 install copytree `subagents/` |
| Q6 kd-nas 复用 | **范围排除 kd-nas** | §1 声明 |
| Q7 host subagent_type | **meta 描述 + Read 校验** | §5.5 |
| eval #1 新 env var 冗余 | **采纳：不引 env var，render inline** | §3.2 |
| eval #2 in_session env 缺口 | **render inline 绕开（不修先在 bug）** | §3.2 |
| eval #3 subagent_type 契约 | **同 Q7** | §5.5 |
| eval #4 E-1 不可 fail | **warn 落 sentinel_stats.jsonl + 阈值升级 + e2e 反向用例** | §5.3/§5.4/§8 |
| eval #5 默认行为空白 | **空目录→空串→不引用** | §3.3 |
| eval #6 kd-nas 误判 | **同 Q6** | §1 |
| eval #7 Q4 触发条件 | **同 Q4** | §7 |
| eval #8 tape schema 未核实 | **tape 实证存全 prompt + 全 tool call args** | §8 验收 #4 引 seq 184 |
| eval #9 Read 失败/幻觉绕过 | **sentinel 不入 prompt，不可猜** | §3.1/§5.2 |
| eval #10 R3 version 注入漂移 | **parent 不预设 version，仅校验格式** | §5.3 |
| eval #11 token 收益夸大 | **改结构性收益，删 token 论据** | §2 |
| eval #12 lint 不扫子 agent md | **validator 扩扫 subagents_root/\*.md** | §7 |
| eval #13 frontmatter 解析 | **strict regex + unit** | §5.2 |

## 7. compile 期校验扩展（evaluator #7/#12）

`orca/compile/validator.py` 增 `_check_subagents_md`（仅当 `subagents_root` 解析到存在目录时跑）：
1. 目录内每个 `*.md` 必有合法 frontmatter（`subagent` / `version` / `sentinel` 三键）——strict regex 解析。
2. body 含 `$ORCA_SUBAGENTS_DIR` / `cat $HOME/.orca/...subagents/` 等旧协议残留 → warning（dev-residue）。
3. agent.md body 引用 `{{ subagents_root }}` 的节点 → 校验 host 通用类型 tools 含 Read（静态可知则校验，
   否则降级 warning）。

**run 期 render 兜底（reviewer3 新风险 #2，实现在 render.py 而非 validator）**：agent.md 引用
`{{ subagents_root }}` 但 render 后为空串 → fail loud（ConfigurationError，明示「workflow 期望 subagents
但 subagents_root 未解析，检查 yaml 位置 / 是否需 `tars install`」），不静默渲染空串让子 agent Read 失败。

**不存在的 `subagents/<wf>/` 目录 = workflow 不用 subagents，正常，不报错**（evaluator #7 闭环）。

## 8. 验收标准（全部可证伪）

1. 改后任一 parent agent.md 正文**不出现**字面 `cat $HOME/.orca/...subagents/` / `cat ~/.orca/...subagents/`
   / `prompt=<body>`；Task prompt 渲染后字节数 ≤ 2KB（指针 + inputs 量级），**≤ 对应 md body 字节数的
   50%**。
2. **仅对 nas-supernet**：render 后 agent.md 里 `{{ subagents_root }}` 被替换为存在的绝对目录路径（unit
   断言 render 产物）。无 subagents 的 workflow（quant-*）→ 该 token 不出现 / 为空。
3. E-1：子 agent report 首行命中 sentinel regex；不命中 → `runs/<run_id>/sentinel_stats.jsonl` 有
   `hit:false` 记录。e2e 反向用例：人工 mock 一个「跳读」report（无 sentinel）→ 验收能从 jsonl 统计出。
4. parent context 不含子 agent md body：tape 里 Task 调用的 prompt 渲染体量 = 短指针 + inputs，**不达**
   24K / 10K / 4K body 量级。证据 tape schema 支持全 prompt 存储（`runs/nas-supernet-...jsonl` seq 184
   存了 24929 字全 prompt，可证）。
5. `_check_prompt_dev_residue` 对改后 **6 个 parent agent.md + 5 个子 agent md**（validator 扩扫，§7）
   warning 清零。
6. 依赖铁律不破（code-reviewer 检 import 方向：render.py 自包含、install_cmds 改目的地）。
7. compile 期：host 通用类型 tools 含 Read（静态可知则校验）。

## 9. 测试计划

1. **unit**（`tests/orca/exec/`）：render namespace 暴露 `subagents_root`；nas-supernet → 存在目录绝对
   路径；quant-* → 空串。frontmatter strict regex 解析 + body 含 `---` hr 不误判。**6 处 RunContext 构造点
   在 nas-supernet 场景下 `subagents_root` 非空**（防 populate 漏点滑过 compile 校验——reviewer3 新风险 #2）。
2. **compile 期**：`tars validate nas-supernet.yaml` 过；缺 frontmatter 键 → 报错；body 含旧协议残留 →
   warning。
3. **install**：`_install_bundled_subagents` copytree `workflows/subagents/` → `~/.orca/workflows/subagents/`（unit 断言路径 + nas-supernet 子树落点）。
4. **e2e**（`tests/e2e_nas_supernet/` mnist fixture，opencode + deepseek-v4-flash）：跑 `ns_expand_supernet`，
   tape 断言：
   - 子 agent Task prompt 渲染体量 < body 50%（point-to-file 生效，无内联）；
   - 子 agent 首个 tool call 是 Read `<绝对路径>/supernet-evaluator.md`；
   - report 首行含 sentinel；
   - `sentinel_stats.jsonl` 记录 `hit:true`；
   - workflow 推进到 ns_train_script（功能不退化）。
5. **e2e 反向**（evaluator #4）：mock 跳读 report → `sentinel_stats.jsonl` 记 `hit:false` 可统计。
6. **洁净契约**：受众翻转通读 + `_check_prompt_dev_residue` warning 清零（含子 agent md）。

## 10. 两轮 review closed-loop issue map

见 §6 裁决表（reviewer 12 条 + evaluator 13 条，去重后 21 条全部闭环）。

## 11. v1 → v2 主要 diff

- **交付**：v1 新 `$ORCA_SUBAGENTS_DIR` env（5 文件改动）→ v2 render 期 Jinja inline `{{ subagents_root }}`
  （2 文件改动，shell 无关）。【evaluator #1/#2】
- **收益论据**：v1 「token 双倍付费」→ v2 「结构性收益，token 持平」。【evaluator #11】
- **sentinel**：v1 parent 可知 version → v2 parent **不知** version/sentinel（防伪造 + 防漂移）。每 md
  加唯一 `sentinel` token。【evaluator #9/#10】
- **范围**：v1 含 kd-nas → v2 仅 nas-supernet。【reviewer/evaluator #6】
- **E-1 落地**：v1 「warn 记录」 unspecified → v2 `sentinel_stats.jsonl` + e2e 反向用例 + 阈值升级。
  【evaluator #4】
- **frontmatter 解析**：v1 隐含 yaml parse → v2 strict regex + unit。【evaluator #13】
- **lint 覆盖**：v1 仅 agent.md → v2 扩扫子 agent md。【evaluator #12】
