# 阶段 5 SPEC —— run/ 编排层 + schema 单轨化迁移

> **状态**：最终版（待分发实现）
> **依据**：[TASK.md](../TASK.md) §3 §6 · [PLAN.md](../PLAN.md) phase 5 · [phase-4-exec.md](phase-4-exec.md) §9
> **范围**：① schema 单轨化破坏性迁移（去 after / 加 parallel 组）；② run/ 编排层（Orchestrator + Router）
> **两份计划**：[migration.md](../plans/2026-06-30-phase5-migration.md)（schema 变更专项）+ [run.md](../plans/2026-06-30-phase5-run.md)（编排实现）

---

## 0. 阶段目标

phase 5 回答唯一一个问题：**「把校验过的 Workflow 跑起来——按拓扑推进、路由决策、循环终止、并行汇聚，事件写进 Tape」**

这是第一次让 `orca run nas.yaml` 真正跑通的阶段。但本阶段**首先**要做一次不可逆的 schema 破坏性变更（单轨化），因为现有 schema 的 `after` + 「入口候选」机制与 routes 单指针模型根本冲突，必须先迁移再做编排。

### 两阶段交付

| 阶段 | 目标 | 性质 |
|---|---|---|
| **5-M migration** | schema 单轨化（去 after、加 parallel、重写校验/example/测试/文档）| 破坏性，不可逆，全覆盖 |
| **5-R run** | Orchestrator + Router（拓扑推进、路由、循环、foreach、parallel）| 新功能 |

**铁律：5-M 必须先完成且全绿，才能开始 5-R。** 不允许半迁移状态。

---

## 1. 编排范式决策（routes 单轨制）

### 1.1 决策结论

采用 **routes 单指针模型**（参考 Conductor 验证过的范式），废除静态依赖双轨制。一种机制统一所有控制流。

```
调度主循环（单指针推进）：
  current = entry
  iterations = 0
  while current != "$end":
      iterations += 1
      if iterations > max_iterations: fail_loud(workflow_failed, "MaxIterations")
      node = wf.find(current)
      output = await execute_and_emit(node, ctx)    # 同步收完事件流拿 output
      ctx.outputs[current] = output                  # 累积 context
      current = router.resolve(node.routes, output, ctx)  # first-match-wins
  emit workflow_completed(evaluate_outputs(wf.outputs, ctx))
```

### 1.2 三原语职责划分（互不重叠，覆盖所有情况）

| 原语 | 表达 | 模型 |
|---|---|---|
| **routes**（节点出边）| 线性 / 条件分支 / 回环循环 | 单指针，每步只去一个 target |
| **parallel 组**（顶层独立列表）| 已知分支的并行 fan-out + 合并等待 | asyncio.gather，等全部完成才推进 |
| **foreach 组**（一种 node kind）| 运行时数组的分批并行 + 聚合 | asyncio.Semaphore 限并发，聚合 outputs[] |

### 1.3 为什么单轨（决策记录）

1. **统一不割裂**：一种机制表达所有串行控制流——线性/分支/回环/终止全是 routes。
2. **消除 join 歧义**：单指针每步只去一个地方，不可能出现「多前置汇聚时跑几次」的歧义。
3. **贴近心智**：人规划任务是顺序流（先做 A，看结果决定 B 还是 C），routes 模型直接对应。
4. **after 是冗余**：每个 node 的 routes 已经定义完整控制流，after 只在重复声明「我在谁后面」。
5. **Conductor 验证**：生产级框架就是纯单轨（无 after/depends_on/edges）。
6. **并行靠专门原语**：routes 的唯一硬伤（DAG 合并）由显式 parallel 组补全，职责分离。

### 1.4 diamond 语义（已确认）

`A→B, A→C, B→D, C→D`（D 等 B、C 都完成）在单指针模型里**不能**用 A 的 routes 表达（A 只能选一个 target）。必须用 parallel 组：

```yaml
parallel:
  - name: split
    branches: [B, C]        # B、C 并行
    routes: [{to: D}]       # 组完成后（B、C 都完成）单指针去 D
```

**parallel 组语义 = asyncio.gather**：等组内所有分支完成后才推进。**绝不存在「B 完成就开始 D」的歧义**——要并行就得显式声明 parallel 组，否则就是串行单指针。

---

## 2. schema 变更清单（破坏性，5-M 全覆盖）

> 以下每一处变更都有精确的 file:line 引用（基于当前 master）。**实现必须 100% 覆盖，不得遗漏。**

### 2.1 `orca/schema/workflow.py`（核心）

#### 删除

| 位置 | 变更 |
|---|---|
| `:7`（模块 docstring）| 删除 `after·routes 引用合法 / DAG 无环` 中 `after` 相关表述，改为 `routes 引用合法 / 死锁检测` |
| `:47`（`Node.after`）| **删除整个 `after: list[str]` 字段** + 注释「静态依赖（默认空 = 入口候选）」 |

#### 新增：`ParallelGroup`

在 `ForeachNode` 后、`AnnotatedNode` 前新增：

```python
class ParallelGroup(BaseModel):
    """静态并行组（顶层独立列表项）。

    branches 是已知 node 名列表（必须在 nodes 里定义），全部并行执行，
    等全部完成后（asyncio.gather）按 routes 推进单指针。
    用于表达 DAG 分叉+合并（diamond）。
    """
    model_config = ConfigDict(extra="forbid")
    name: str                       # 组名（全局唯一，与 node 名共享命名空间）
    branches: list[str]             # 并行分支的 node 名（≥2，必须在 nodes 中已定义）
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    routes: list[Route] = []        # 组完成后路由（同 node.routes 语义）
```

#### 修改：`Node` 基类

```python
class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""                  # 唯一标识；顶层非空唯一由 compile 强制
    routes: list[Route] = []        # 条件路由（first-match-wins）；唯一控制流
    # after 字段：删除（不再支持静态依赖）
```

#### 修改：`Workflow`

```python
class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    entry: str                      # 起始 node 名（唯一入口）
    inputs: dict[str, InputDef] = {}
    nodes: list[AnnotatedNode]
    parallel: list[ParallelGroup] = []   # 新增：静态并行组（独立列表）
    outputs: dict[str, str] = {}
```

**注意**：`entry` 仍指向一个 **node**（不是 parallel 组）。parallel 组只能被路由到达（某 node 的 routes 指向 parallel 组名），不能作为 entry。

### 2.2 `orca/compile/validator.py`（9 项校验重排）

#### 删除的校验

| 位置 | 校验 | 原因 |
|---|---|---|
| `:136-145`（`_check_after_refs_valid` ③）| after 引用有效 | after 字段已删除 |
| `:166-200`（`_check_after_acyclic` ⑤ + `_find_cycle_paths`）| after 静态边无环 | after 字段已删除 |
| `validate_workflow` 里对 ③⑤ 的调用（`:86,:88`）| — | — |

#### 修改的校验

**④ `_check_route_refs_valid`**（`:151-160`）：
- 扩展 target 合法集：`names ∪ parallel_group_names ∪ {"$end"}`。
- route.to 可以指向 node 名**或** parallel 组名。

**⑥ `_check_entry_reachable_to_end`**（`:239-298`）：
- `successors()` 函数：删除 `after` 反向边（`:251-256`），只保留 route 前向边 + parallel 组展开。
- `successors(node)` = `{route.to for route in node.routes if to != "$end"}`，且若 `route.to` 是 parallel 组名，展开为该组的 `branches`（组的分支是 node，分支完成后组才完成，所以可达性里组→其分支）。
- parallel 组本身也有 routes，组的 successors = 组的 route.to。

#### 新增的校验

**⑩ parallel 组校验**（新函数 `_check_parallel_groups`）：
1. 每个 parallel 组的 `name` 全局唯一（与 node 名共享命名空间，去重）。
2. `branches` 长度 ≥ 2（少于 2 不是并行）。
3. `branches` 中每个名字 ∈ node 名集合（必须在 nodes 里已定义）。
4. `branches` 内无重复（同一 node 不能在同一组里出现两次）。
5. parallel 组的 `routes` 走与 node 相同的 ④ 校验（target 合法）。
6. parallel 组不能被 entry 直接指向（entry 只能是 node）；不能自引用（组的 route 不能指向自己）。

**⑪ 兜底 route 位置校验**（新函数 `_check_route_fallback_last`）：
- 对每个 node 和 parallel 组的 routes：无 `when` 的 route（catch-all）必须是列表**最后一条**，否则它后面的 route 永远不可达（死代码）→ error。
- 消息：`node 'X' 的无条件 route 不是最后一条，其后的 route 永远不可达`。

**⑫ 命名空间唯一性扩展**（修改 `_check_names_unique` ①）：
- node 名 + parallel 组名共享一个命名空间，全局唯一。

**⑬ entry 不是 parallel 组**（新函数 `_check_entry_is_node`）：
- `wf.entry` 必须是 node 名，不能是 parallel 组名。否则 error：`entry 不能是 parallel 组，必须是 node`。

#### 重排后的校验项编号

```
① name（含 parallel 组名）非空 + 全局唯一
② entry 存在 + entry 是 node（不是 parallel 组）
④ routes.to 引用有效（node 名 / parallel 组名 / $end）
⑥ entry 可达终态（沿 routes + parallel 展开）
⑦ Jinja2 引用浅校验
⑧ foreach.source 首段是真实 node
⑨ profiles capability 校验
⑩ parallel 组结构校验（branches ≥2 / 已定义 / 无重复 / routes 有效 / 不自引用）
⑪ 兜底 route 位置校验
```
（③⑤ 删除，⑫⑬ 合并进 ①②）

### 2.3 examples 重写（3 个）

#### `examples/nas.yaml`
- **删除所有 `after:` 字段**（`:30,:41,:51,:66` 共 4 处）。
- routes 不变（nas.yaml 的 routes 已是完整控制流，after 是冗余）。
- 验证：`load_workflow("examples/nas.yaml")` 成功 + 9 项校验全过。

#### `examples/batch_assess.yaml`
- **删除 `assessor` 和 `picker` 的 `after`**（`:15,:26`）。
- finder 加 `routes: [{to: assessor}]`，assessor 加 `routes: [{to: picker}]`，picker 加 `routes: [{to: $end}]`。
- 验证：解析 + 校验通过。

#### `examples/parallel_research.yaml`
- **彻底重写**（这是变更最大的 example，因为依赖多入口隐式并行）：
```yaml
name: parallel_research
entry: researcher_a
nodes:
  - name: researcher_a
    kind: agent
    routes: [{to: researchers_merge}]    # a 完成后路由到 parallel 组
  - name: researcher_b
    kind: agent
    # b 不再是隐式入口；它由 parallel 组驱动执行
  - name: synthesizer
    kind: agent
    prompt: |
      综合：{{ researcher_a.output }} / {{ researcher_b.output }}
    routes: [{to: $end}]
parallel:
  - name: researchers_merge
    branches: [researcher_a, researcher_b]   # a、b 并行执行（注意：entry 路由到这里会触发并行）
    failure_mode: continue_on_error
    routes: [{to: synthesizer}]
```
- **关键语义**：entry=researcher_a。但 researcher_a 完成后 routes 到 parallel 组 `researchers_merge`，组会执行其 branches（含 researcher_a 本身——**已执行则跳过**，见 §4.4）。组完成后 routes 到 synthesizer。
- **设计裁决（写进 SPEC）**：parallel 组执行 branches 时，**已执行过的 node 跳过**（基于 ctx.outputs 已有记录）。这避免「researcher_a 被执行两次」。

> ⚠️ 这个 example 的重写是迁移最微妙的部分。实现时必须在 §4.4「parallel 组幂等执行」有对应测试。

### 2.4 fixtures 重写（3 个）

| fixture | 原状态 | 迁移后 |
|---|---|---|
| `tests/compile/fixtures/after_cycle.yaml` | 测 after 环 | **删除**（after 不存在，无环可测）。或改为测「route 环可达 $end」的合法场景（但 dead_end.yaml 已覆盖）。**建议删除**。 |
| `tests/compile/fixtures/bad_after.yaml` | 测 after 引用不存在 | **删除**（after 不存在）。 |
| `tests/compile/fixtures/multi_error.yaml` | 多错误聚合（含 after ghost）| **修改**：去掉 `after: [ghost]`，换成新错误（如 `bad_route_fallback`：兜底 route 不在最后）。保留 entry 不存在 + route nowhere + jinja nope 三个错误，加一个新的，凑多错误聚合测试。 |
| `tests/compile/fixtures/bad_foreach_source.yaml` | foreach source 错（含 after）| **修改**：删除 after，finder 加 routes 到 foreach。 |
| 新增 `bad_parallel_branches.yaml` | — | branches 引用不存在的 node → error |
| 新增 `bad_parallel_too_few.yaml` | — | branches 长度 < 2 → error |
| 新增 `bad_route_fallback.yaml` | — | 兜底 route 不在最后 → error |
| 新增 `bad_entry_is_parallel.yaml` | — | entry 指向 parallel 组 → error |

### 2.5 测试重写

#### `tests/schema/test_workflow.py`
- `:280-284`（`test_parallel_research_yaml_deep_parse`）：重写，断言新 parallel 结构（`wf.parallel[0].branches == ["researcher_a","researcher_b"]`）。
- 新增：parallel 组解析、parallel 字段默认空、name 与 node 共享命名空间。

#### `tests/compile/test_validator.py`
- `:83-95`（after 引用有效测试）：**删除**。
- `:114-132`（after 环 + route 回指测试）：**删除** after 环测试；保留 route 回指合法测试（改为不依赖 after 的纯 route 环）。
- `:177`（`after=["a"]`）：删除 after，改用 routes 串行。
- `:231,:254,:268`（foreach 的 after）：删除 after，改 routes 串行。
- `:283-288`（multi_error）：对齐新 fixture。
- `:324,:326`（fixture 参数化）：删除 after_cycle / bad_after，加新 fixture。
- 新增：⑩⑪⑫⑬ 四组新校验的测试（见 §2.2）。

#### `tests/compile/test_validate_profiles.py`
- `:159,:161`（`after=["b"]`,`after=["a"]`）：删除 after 参数。

### 2.6 文档修正（全覆盖，不得遗漏）

#### 必改的 SPEC
| 文档 | 位置 | 变更 |
|---|---|---|
| `phase-1-schema.md` | `:32,:44,:45,:47`（决策表「after vs depends_on」）| 改为「routes 单轨 vs after 双轨」决策记录，注明 phase 5 已迁移到单轨 |
| `phase-1-schema.md` | `:90`（Node 模型伪代码）| 删除 after 行 |
| `phase-1-schema.md` | `:173-175`（校验约束）| 删除 after 相关，改 routes 死锁检测 |
| `phase-1-schema.md` | `:353-447`（nas/batch/parallel 示例）| 全部去 after，parallel_research 改 parallel 组 |
| `phase-1-schema.md` | `:401-414`（§6.2 静态并行）| 重写为 parallel 组范式 |
| `phase-1-schema.md` | `:496-498`（对比表）| 更新并行对比行 |
| `phase-2-compile.md` | `:83`（孤立节点）、`:182-194`（③⑤）、`:226,:245-247`（环检测）、`:271-272`、`:288-289,:326-347` | 全部去 after，改 routes 死锁 + parallel 校验 |

#### 必改的计划/release
| 文档 | 变更 |
|---|---|
| `plans/2026-06-29-phase1-schema.md:21` | Node 字段表去 after，加 parallel |
| `plans/2026-06-30-phase2-compile.md:52,54,63,65,110,111` | 校验项表去 ③⑤，加 ⑩⑪⑫⑬ |
| `plans/_TEMPLATE.md:24` | Node 字段去 after |
| `releases/2026-06-29-phase1-schema.md:53` | E2E 测试描述去 after |
| `releases/2026-06-30-phase2-compile.md:23,24,34` | 校验项去 after |

#### 必改的顶层文档
| 文档 | 位置 | 变更 |
|---|--- 幂 | ---|
| `CLAUDE.md` | schema 章节如有 after 提及 | 去 after |
| `DESIGN.md:11,62` | run/ 描述「拓扑/并行/路由」 | 改为「单指针推进/parallel 组/路由」 |
| `TASK.md:24,260,276,307` | 「拓扑」措辞 | 统一为「单指针推进 + parallel 组」 |
| `PLAN.md:46` | phase 2 描述「after 无环」 | 改「routes 死锁检测」 |
| `PLAN.md:62` | phase 5 描述「拓扑排序」 | 改「单指针推进」 |
| `status/CURRENT.md:19` | orchestrator 描述 | 改单指针 |
| `phase-4-exec.md:24,402,535,667` | 「拓扑」措辞 | 改「单指针推进」 |

#### 验证手段（SPEC 强制）
```bash
# migration 完成后必须零匹配（除本 SPEC/计划文档的历史记录外）
grep -rn '\bafter\b' orca/ examples/ tests/   # 必须零 after 字段引用
grep -rn '入口候选' orca/ docs/                # 必须零匹配
grep -rn '隐式并行' docs/                       # 仅在迁移记录里出现
```

---

## 3. Router 语义（routes 求值）

### 3.1 求值规则（first-match-wins）

```python
def resolve(routes: list[Route], output: Any, ctx: RunContext) -> str:
    """返回 target（node 名 / parallel 组名 / $end）。无匹配 fail loud。"""
    eval_ctx = build_route_eval_context(output, ctx)
    for route in routes:
        if route.when is None:
            return route.to    # 兜底（catch-all）
        if eval_jinja2_bool(route.when, eval_ctx):
            return route.to
    raise NoRouteMatchError(...)   # 全部 when 不匹配且无兜底 → 死锁，fail loud
```

### 3.2 路由条件变量命名（统一）

| 变量 | 含义 |
|---|---|
| `output` | 本节点刚完成的输出（裸引用，如 `output.exit_code == 0`）|
| `inputs` | workflow 输入（`inputs.iterations`）|
| `<node_name>` | 任意已完成节点的输出（`optimizer.output.structure`）|
| `<parallel_group>.outputs` | parallel 组的聚合输出 |

**不引入** Conductor 的 `context.iteration`（用 `inputs.iterations` + 显式 set 计数代替，更显式）。when 表达式求值为 bool（Jinja2 渲染后 truthy 判定）。

### 3.3 求值时机

节点完成后**同步求值**（收完事件流拿到 output 再判 routes）。放弃「执行中途看下一个 node」的能力，换简化逻辑。

### 3.4 死锁检测

- **静态**（compile）：route.to 引用不存在 → error（⑨）。
- **运行时**（run）：所有 when 不匹配且无兜底 route → `NoRouteMatchError` → emit `workflow_failed`（error_type=`NoRouteMatch`）。

---

## 4. Orchestrator 设计（run/）

### 4.1 文件结构

```
orca/run/
├── __init__.py          # 导出 Orchestrator, run_workflow, Router, RouteError
├── router.py            # Router.resolve（first-match-wins，纯函数）
├── context.py           # RunContext（扩展 phase 4 的，加 parallel/foreach 聚合）
├── orchestrator.py      # Orchestrator 主循环（单指针推进）
├── parallel.py          # 并行组执行（asyncio.gather + failure_mode）
├── foreach.py           # foreach 分批执行（Semaphore + 聚合）
├── executor_adapter.py  # 把 executor.exec() 的 AsyncIterator 桥接到 bus.emit
└── lifecycle.py         # run_id 生成 + workflow_started/completed/failed 事件
```

### 4.2 主循环（orchestrator.py）

```python
class Orchestrator:
    def __init__(self, wf: Workflow, bus: EventBus, inputs: dict, task: str | None):
        self.wf, self.bus, self.router = wf, bus, Router()
        self.ctx = RunContext(inputs=inputs, outputs={}, run_id=gen_run_id(), task=task)
        self.max_iter = resolve_max_iter(wf, inputs)  # §5

    async def run(self) -> RunState:
        emit workflow_started(self.ctx.run_id, wf.name)
        current = self.wf.entry
        try:
            iterations = 0
            while current != "$end":
                iterations += 1
                if iterations > self.max_iter:
                    raise MaxIterationsError(self.max_iter)
                if current is parallel_group:
                    output = await self._run_parallel_group(current)
                elif current is foreach_node:
                    output = await self._run_foreach(current)
                else:
                    output = await self._run_node(current)
                self.ctx.outputs[current] = output
                current = self.router.resolve(node.routes, output, self.ctx)
            final_output = evaluate_outputs(self.wf.outputs, self.ctx)
            emit workflow_completed(final_output)
            return replay_state(tape)
        except (ExecError, RouteError, MaxIterationsError) as e:
            emit workflow_failed(e.error_type, e.message)
            return replay_state(tape)
```

### 4.3 node 执行桥接（executor_adapter.py）

```python
async def execute_and_emit(executor, node, ctx, bus) -> Any:
    """拿 executor.exec() 的 AsyncIterator，逐个 bus.emit 写 Tape，返回 output。"""
    output = None
    async for event in executor.exec(node, ctx):
        bus.emit(event, session_id=event.session_id)   # 唯一写 Tape 处
        if event.type == "node_completed":
            output = event.data["output"]
        elif event.type == "node_failed":
            raise ExecError(...)   # 触发上层 workflow_failed
    return output
```

### 4.4 parallel 组执行（parallel.py）

```python
async def _run_parallel_group(self, group: ParallelGroup) -> dict:
    async def run_one(branch_name: str) -> tuple[str, Any]:
        if branch_name in self.ctx.outputs:
            return branch_name, self.ctx.outputs[branch_name]   # 已执行则跳过（幂等）
        node = self.wf.find(branch_name)
        executor = make_executor(node)
        output = await execute_and_emit(executor, node, self.ctx, self.bus)
        return branch_name, output

    results = await asyncio.gather(
        *[run_one(b) for b in group.branches],
        return_exceptions=True
    )
    return self._aggregate_parallel(results, group.failure_mode)  # {outputs:{}, errors:{}, count}
```

- **幂等执行**：branch 已在 ctx.outputs（如 researcher_a 作为 entry 已跑过）则跳过，不重复执行。
- **failure_mode 三态**：fail_fast（首个失败抛）/ continue_on_error（仅全失败抛）/ all_or_nothing（任一失败抛）。
- **聚合输出**：`{outputs: {branch_name: output}, errors: {}, count: N}`。

### 4.5 foreach 执行（foreach.py）

```python
async def _run_foreach(self, node: ForeachNode) -> dict:
    source_array = eval_jinja2(node.source, self.ctx)   # 运行时取数组
    sem = asyncio.Semaphore(node.max_concurrent)
    async def run_one(idx, item):
        async with sem:
            body_ctx = self.ctx.with_locals({node.item_var: item, node.index_var: idx})
            executor = make_executor(node.body)   # body 是 agent/script
            return await execute_and_emit(executor, node.body, body_ctx, self.bus)
    results = await asyncio.gather(*[run_one(i, x) for i, x in enumerate(source_array)])
    return self._aggregate_foreach(results, node.failure_mode)  # {outputs:[...], errors:{}, count}
```

### 4.6 max_iterations 解析（lifecycle.py）

```python
def resolve_max_iter(wf, inputs) -> int:
    """优先级：CLI --max-iter > inputs.iterations > wf default > 全局兜底(100)"""
    if "iterations" in inputs:
        return int(inputs["iterations"])
    if "iterations" in wf.inputs and wf.inputs["iterations"].default is not None:
        return int(wf.inputs["iterations"].default)
    return 100   # 全局硬上限
```

---

## 5. CLI 参数（task 注入 + input 覆盖）

phase 5 CLI 层（最小实现，phase 6 完善）：

```
orca run <yaml> [task] [-i key=value]... [--max-iter N]
```

- `<yaml>`：workflow 文件路径（位置参数，必需）。
- `[task]`：可选位置参数，注入为 `inputs.task`（workflow 未声明 task input 则 warn）。
- `-i key=value`：覆盖 inputs，带类型推断（`true/false`→bool，数字→int/float，`[...]/{...}`→JSON，其他 str）。
- `--max-iter N`：覆盖 max_iterations（最高优先）。

**优先级**：`--max-iter` > `-i iterations=N` > `inputs.iterations`（yaml default）> 全局兜底。

---

## 6. 验收标准（5-M migration）

### 6.0 迁移验收总则
1. **零 after 残留**：`grep -rn '\bafter\b' orca/ examples/ tests/` 零匹配（除注释历史记录）。
2. **零「入口候选」残留**：`grep -rn '入口候选' orca/ docs/` 零匹配。
3. **全量测试绿**：所有现有测试通过（迁移后重写的那部分）+ 新增 parallel/死锁校验测试。
4. **examples 解析+校验通过**：nas/batch/parallel_research 三个 example 全过。

### 6.1 schema 变更
- [ ] `Node.after` 字段删除
- [ ] `ParallelGroup` 类新增（name/branches/failure_mode/routes）
- [ ] `Workflow.parallel` 字段新增（默认 []）
- [ ] docstring 全部更新（去 after，加 parallel）

### 6.2 validator 重排
- [ ] ③⑤ 删除（after 校验全删）
- [ ] ④ 扩展（target 含 parallel 组名）
- [ ] ⑥ 修改（successors 去 after 反向边）
- [ ] ⑩ parallel 组校验（6 子项）
- [ ] ⑪ 兜底 route 位置校验
- [ ] ⑫⑬ 命名空间唯一 + entry 非 parallel（合并进 ①②）
- [ ] 校验项编号注释更新

### 6.3 examples
- [ ] nas.yaml：去 after，校验通过
- [ ] batch_assess.yaml：去 after + 加 routes，校验通过
- [ ] parallel_research.yaml：重写为 parallel 组，校验通过

### 6.4 fixtures
- [ ] after_cycle.yaml / bad_after.yaml 删除
- [ ] multi_error.yaml / bad_foreach_source.yaml 修改（去 after）
- [ ] bad_parallel_branches / bad_parallel_too_few / bad_route_fallback / bad_entry_is_parallel 新增

### 6.5 测试重写
- [ ] test_workflow.py：parallel_research 重写 + parallel 新测试
- [ ] test_validator.py：after 测试删除 + ⑩⑪⑫⑬ 新测试
- [ ] test_validate_profiles.py：去 after

### 6.6 文档
- [ ] phase-1-schema.md：决策表 + 伪代码 + 示例 + §6.2 全改
- [ ] phase-2-compile.md：③⑤ 删除 + ⑩⑪⑫⑬ 加
- [ ] 4 个 plan/release + TEMPLATE
- [ ] CLAUDE.md / DESIGN.md / TASK.md / PLAN.md / CURRENT.md：拓扑→单指针
- [ ] phase-4-exec.md：「拓扑」措辞改

---

## 7. 验收标准（5-R run）

### 7.0 编排验收总则（5 条铁律）
1. **executor 不写 tape**：orchestrator 是唯一 `bus.emit` 写 Tape 处。
2. **单指针模型**：主循环一个 current 指针推进，无拓扑排序、无就绪集。
3. **依赖单向**：`run→compile+exec+events+schema`；run 不被任何模块 import。
4. **fail loud**：MaxIterations / NoRouteMatch / executor error → workflow_failed。
5. **Router 纯函数**：`resolve(routes, output, ctx) -> str`，无副作用。

### 7.1 Router
- [ ] first-match-wins 正确
- [ ] when=None 兜底匹配
- [ ] 全部不匹配 → NoRouteMatchError
- [ ] output / inputs / node_name 变量解析正确
- [ ] 纯函数（同输入同输出）

### 7.2 Orchestrator 主循环
- [ ] entry → 推进 → $end 完整流程
- [ ] max_iterations 命中 → workflow_failed
- [ ] workflow_started/completed/failed 事件正确
- [ ] outputs 求值正确

### 7.3 node 执行桥接
- [ ] executor.exec() 的 AsyncIterator 逐个 bus.emit
- [ ] node_completed 取 output 累积 ctx
- [ ] node_failed → workflow_failed

### 7.4 parallel 组
- [ ] branches 并行执行（asyncio.gather）
- [ ] 已执行 branch 跳过（幂等）
- [ ] failure_mode 三态正确
- [ ] 聚合输出 {outputs, errors, count}

### 7.5 foreach
- [ ] source 数组运行时取值
- [ ] Semaphore 限并发
- [ ] failure_mode 三态
- [ ] 聚合 outputs[]

### 7.6 demo workflow 端到端（真 claude，见 plan）
- [ ] 7 个 demo workflow 全跑通（linear/conditional/loop/foreach/mixed/task/failure）
- [ ] 事件流完整 + outputs 正确

### 7.7 max_iterations
- [ ] --max-iter > -i iterations > yaml default > 全局兜底 优先级正确

### 7.8 task 注入
- [ ] 位置参数 task → inputs.task
- [ ] 未声明 task input → warn（不阻断）
- [ ] prompt 模板 `{{ inputs.task }}` 渲染正确

---

## 8. 给后续阶段的契约

| 后续 | phase 5 提供 |
|---|---|
| phase 6 cli | `run_workflow(wf, inputs, task) -> RunState`；事件流经 bus |
| phase 7 web | 订阅 Subscription → WS 推；GET /api/state 读 tape |
| phase 8 gates | 在 node 执行前后插 HumanGate（parallel/foreach 内部也可插）|

---

## 9. 不做的事

- ❌ **拓扑排序 / 就绪集**（单指针模型不需要）—— 显式废除
- ❌ **interrupt**（中途打断）—— 后续
- ❌ **checkpoint_resume** —— 后续
- ❌ **retry**（跨 node 重试 / EvalJudge）—— 后续
- ❌ **MCP 配置 / HMIL hook** —— phase 9/8
- ❌ **WebSocket / Rich 渲染** —— phase 7/6
- ❌ **`orca graph` 命令**（DAG 可视化）—— phase 6（但 schema 已支持渲染）

---

## 10. 关键决策备忘（防 drift）

1. **routes 单轨制**，废除 after 双轨（理由 §1.3）
2. **三原语职责划分**：routes（串行决策）/ parallel 组（并行汇聚）/ foreach（数组分批）
3. **parallel 组是顶层独立列表**（P1 方案），不是 node kind
4. **entry 必须是 node**，不能是 parallel 组
5. **parallel 组幂等执行**：已执行 branch 跳过
6. **diamond 靠 parallel 组**，routes 不承担合并（每步只去一个地方）
7. **路由同步求值**（节点完成后），不流式
8. **max_iterations 优先级**：CLI > inputs > yaml default > 兜底 100
9. **task 位置参数 = `-i task="..."` 语法糖**
10. **兜底 route 必须最后一条**（编译时校验，⑪）
11. **死锁 = 全部 when 不匹配且无兜底**（运行时 fail loud）
12. **5-M 先于 5-R**，半迁移状态禁止
13. **schema 变更全覆盖**：code + example + fixture + test + 文档（SPEC + plan + release + 顶层文档）
