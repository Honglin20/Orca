# 节点记忆(Node Memory)设计草稿 / 准 SPEC

> **状态**:准 SPEC(2026-07-18,经 spec-reviewer conditional-pass 后收敛)——可直接实现。
> **依据**:2026-07-18 设计讨论 + Claude Code `~/.claude/projects/<enc>/memory/` 参照 + spec-reviewer 对抗审查闭环。
> **范围**:记忆的存储位置 / 读写分工 / 启用方式 / 改动面 / 边界 / 验收。
> ** reviewer 关键事实修正(已吸收)**:`apply_step_result` 在 `orca/iface/in_session/_step_io.py:68`(非 cli.py);tape 是用户级 `~/.orca/runs`(非项目级)→ project_root 必须独立 `Path.cwd()` resolve;advance_step 是纯决策(step.py:278 docstring);v1 仅 agent 节点(`_check_agent_node`)。

---

## 0. 已锁定决策(收敛,不再讨论)

1. **不做确定性指纹缓存**(已否决)。记忆 = 上一轮 output 的覆盖式快照,不提炼。
2. **记忆内容 = 上一轮 `node_completed.data.output` 的覆盖式快照**。output 本就是「给下游的信息」,存它即满足需求;覆盖写天然单份 + 天然过期清除。
3. **写记忆 = 引擎确定性**(`apply_step_result` emit_batch 成功后必然写);**读记忆 + 跳过判断 = agent**(prompt 注入上轮 output,agent 自己决定,走正常推进路径,引擎**不加** skip 分支)。
4. **存储 = 项目内 `.orca/memory/<workflow-name>/<node-name>.md`**;`.orca/` 默认 gitignore。
5. **启用 = `AgentNode.memory: bool = False`**,opt-in。**仅 AgentNode**(D1 决策:ScriptNode output 是 stdout/exit_code 复用价值低,YAGNI;foreach body 内 AgentNode 自动继承,但 foreach 走 `orca run` 非 in-session,本特性 scope 外)。
6. **空 output = 写空 MD**(D2 决策:frontmatter + 空 body,保持覆盖语义确定;「上轮 output 为空」本身是注入信号)。
7. **MD frontmatter = 4 字段 `{run_id, timestamp, workflow, node}`**(D3 决策:溯源必要,成本低;YAML front matter)。
8. **数据真相层级**:`tape.node_completed.data.output` 是唯一真相源;`.orca/memory/<node>.md` 是**派生缓存**。丢/坏 MD 不影响正确性;replay/恢复**不读** MD;冲突 tape 赢。
9. **特性 scope = in-session shell only**。注入与写记忆只挂 in-session 推进路径(`step.py:_deliver` / `_step_io.py:apply_step_result`);`orca run`(drive_loop)路径不吃(YAGNI)。

---

## 1. 机制总览

```
                  ┌─────────────────────────────────────┐
_deliver(step.py)─→│ render_prompt → [注入上轮 MD+协议]   │ ← 读侧(agent 判断)
                  │ → _write_prompt_file                │   仅 node.memory 且 MD 存在
                  └─────────────────────────────────────┘
                              ↓ 宿主执行 agent
                  ┌─────────────────────────────────────┐
                  │ agent:复用上轮 / 正常执行 → output   │
                  └─────────────────────────────────────┘
                              ↓ orca next --output
                  ┌─────────────────────────────────────┐
advance_step(纯决策)│ emit node_completed(不变)          │
                  └─────────────────────────────────────┘
                              ↓ emit_batch 成功后
                  ┌─────────────────────────────────────┐
apply_step_result ─→│ 若 node.memory: 覆盖写 <node>.md   │ ← 写侧(引擎确定性)
                  └─────────────────────────────────────┘
```

**核心**:跳过不走引擎 skip 分支。agent 复用 → 产出 output → `orca next`,走正常路径。EventType / reducer / tape / advance_step 决策 / Status 语义**全不改**。

---

## 2. 存储与路径

### 2.1 目录

```
<project-root>/.orca/memory/<workflow-name>/<node-name>.md
```

- `<workflow-name>` = `wf.name`(YAML `name:` 字段,**非文件名**)。
- `<node-name>` = `node.name`(schema 字段)。
- `project-root` = `Path.cwd()`(与 in-session 现有 `_env_file_path` / `_write_orca_env` 同源;tape 在用户级 `~/.orca/runs`,不能从 tape_path 反推 project)。

### 2.2 MD 文件格式

```markdown
---
run_id: <run_id>
timestamp: <unix float>
workflow: <wf.name>
node: <node.name>
---

<body>
```

- `body`:`node.output_schema is None` → output 原文;非 None → `json.dumps(parsed_output, ensure_ascii=False, indent=2)`(deterministic 序列化)。
- 空 output → body 为空(仅 frontmatter)。
- 写:`tmp + os.replace` 原子覆盖(与 `_write_prompt_file` / marker 同模式)。

### 2.3 不暴露 env 变量

砍掉原草案的 `ORCA_NODE_MEMORY`(YAGNI,reviewer B8):prompt 注入已塞 MD body 全文,agent 无需再 Read 文件;写回被禁(写=引擎确定性)。

---

## 3. 写记忆(引擎侧,确定性)

### 3.1 新模块 `orca/run/memory.py`

```python
def write_node_memory(
    wf: Workflow, node: Any, output: Any, *, run_id: str, project_root: Path,
) -> None:
    """覆盖写 .orca/memory/<wf.name>/<node.name>.md。best-effort:失败 → 结构化 warn,不抛。"""
```

- 构造 frontmatter(run_id / timestamp / wf.name / node.name)+ body(按 §2.2 序列化)。
- `mkdir parents=True, exist_ok=True`;`tmp + os.replace` 原子写。
- 失败(OSError)→ `logger.warning(..., extra={"event":"memory_write_failed","run_id":...,"node":...})`,**不阻断 run**(deviation 登记:memory 是派生缓存,best-effort)。

### 3.2 唯一调用方:`apply_step_result`(`_step_io.py:68`)

签名加 `wf` 参数(调用方 cli / daemon 都持有 wf):

```python
async def apply_step_result(bus, result, *, wf=None, run_id=None, no_memory=False, project_root=None):
    await bus.emit_batch(_emits_to_event_datas(result.emits))
    if wf is not None and not no_memory:
        for e in result.emits:
            if e.type == "node_completed":
                node_obj = _node_obj_by_name(wf, e.node)  # helper
                if node_obj is not None and getattr(node_obj, "memory", False):
                    write_node_memory(wf, node_obj, e.data.get("output"),
                                      run_id=run_id or "", project_root=project_root or Path.cwd())
    ...  # 原 reply 构造不变
```

- **仅 `node_completed` 触发**(`node_failed` / `workflow_failed` / `workflow_cancelled` 不触发;含 `workflow_completed` 出口前最后一个 `node_completed`)。
- `no_memory=True` 时整 run 跳过写(测试隔离)。

### 3.3 不做

不做指纹 / TTL / dedup(无条件覆盖最新)。

---

## 4. 读记忆 + 跳过协议(agent 侧)

### 4.1 注入点:`_deliver`(`step.py:209`)

在 `rendered = _render_or_fail(node, ctx)` 之后、`_write_prompt_file` 之前:

```python
rendered = _render_or_fail(node, ctx)
if _memory_enabled(node):  # getattr(node,"memory",False) and not no_memory
    rendered = _inject_memory(node, wf, rendered, project_root)  # MD 不存在则原样返回
```

- `_inject_memory`:读 `<project-root>/.orca/memory/<wf.name>/<node.name>.md`,strip frontmatter 取 body;若存在,拼到 rendered 末尾:
  ```
  <原 rendered prompt>

  ---
  【上一轮记忆】(本节点上一次执行的 output 快照)
  <body>

  【复用协议】
  若上述上轮结果仍适用于本轮输入,直接基于它产出本轮 output(可原样或微调),不必重跑完整流程;否则正常执行,忽略上轮结果。
  ```
- MD 不存在 / 读失败 → 静默原样返回(首跑 / 文件损坏;fail silent 此处合理,因 tape 才是真相)。
- `render_prompt`(exec 层)**不动**(保持零文件 I/O 契约)。
- `advance_step` 需把 `wf` / `project_root` / `no_memory` 透到 `_deliver`(新增 kwargs,默认值保持单测 inline 路径不变)。

### 4.2 跳过的发生

agent 判断可复用 → 基于 MD 产出 output → `orca next`。**正常推进路径,引擎无 skip 分支。** 复用决策是手工观察项,非阻断验收。

---

## 5. CLI 接线

- `orca next --run-id ...`:`--no-memory` flag(默认 False)。透传 `advance_step(no_memory=...)` 与 `apply_step_result(no_memory=...)`。
- `orca <wf> --inputs`(bootstrap):同 `--no-memory`。
- `wf` / `run_id` / `project_root=Path.cwd()` 传入 `apply_step_result`。
- daemon.next 路径同步(避免两路分叉)。
- **`orca memory clear` CLI:延后(follow-up)**。孤立 MD(重命名场景)手动 `rm -rf .orca/memory/<old>` 即可,YAGNI。

---

## 6. 改动面

| 改动 | 位置 | 性质 |
|---|---|---|
| `AgentNode` 加 `memory: bool = False` | `orca/schema/workflow.py:AgentNode`(约 L112-141) | schema 一 bool,`extra="forbid"` 不变 |
| 新模块 | `orca/run/memory.py` | `write_node_memory` + `read_node_memory_body` + `inject` helper |
| 写记忆调用 | `orca/iface/in_session/_step_io.py:apply_step_result`(加 wf/run_id/no_memory/project_root kwargs) | emit_batch 后一处后置动作 |
| prompt 注入 | `orca/run/step.py:_deliver`(加 wf/project_root/no_memory kwargs) + `advance_step` 透传 | 渲染期注入 |
| CLI flag | `orca/iface/in_session/cli.py` next/bootstrap 加 `--no-memory` | 测试隔离 |
| daemon 同步 | `orca/iface/in_session/daemon.py`(若 next 走 daemon) | 避免 two-path 分叉 |
| `.gitignore` | 项目根(存在则 append `.orca/`,不存在 skip) | 一行 |

**不碰**:`EventType` / reducer / tape 格式 / `advance_step` 决策分支 / `Status` 语义 / `render_prompt` / chart_sock / sidechain_daemon / host_session binding。

依赖方向:`schema ← run/memory ← (run/step + iface/_step_io)`,单向。

---

## 7. 边界与已知限制

1. **副作用节点不应开 `memory`**:写代码 / 改文件节点光读 MD 不能复现副作用。文档 warn,不强制。
2. **超大 output 臃肿**:大段 dump 节点不适合;后续按需升级提炼版(YAGNI)。
3. **worktree 隔离**:项目内 `.orca/memory/` 每个 worktree 各一份,不跨 worktree 共享(通常合理)。
4. **跨 host_session 共享**:MD 故意跨 host_session 共享(项目级沉淀,与 tape host_session 维度正交)。
5. **重命名孤立**:`wf.name` / `node.name` 改后旧 MD 不再读写,引擎不清理(手动 rm)。
6. **cross-run last-writer-wins**:同 project 并发 run 同名节点 → `os.replace` 原子后写覆盖前写(派生缓存语义,tape 才是真相)。
7. **scope = in-session only**:`orca run` drive_loop 不吃本特性。

---

## 8. 验收(可测在前,阻断项)

1. **非 memory 节点零行为**:行为与改动前 100% 一致,零 MD 产出、零 prompt 注入(回归红线,grep `.orca/memory` 不命中)。
2. **首跑写 MD**:`memory=True` 节点完成后 `.orca/memory/<wf>/<node>.md` 存在,body = output(`output_schema=None`)或 `json.dumps`(非 None)。
3. **二跑注入**:`memory=True` 节点第二次执行,渲染后 prompt 含「上一轮记忆」段 + body = 上轮 MD body。
4. **frontmatter 4 字段**:MD 含 `run_id` / `timestamp` / `workflow` / `node`。
5. **`--no-memory`**:整 run 不写 MD、不注入,即使节点 `memory=True`。
6. **空 output**:写空 body MD(仅 frontmatter)。
7. **跨 project-root**:两个 cwd 跑同 wf,注入互不可见(mock project_root)。
8. **写失败不阻断**:mock OSError → run 正常完成,tape 含 `node_completed`,日志含 `event=memory_write_failed`。
9. **(手工观察)** agent 复用 → 走正常推进,不引入 skip 语义。
