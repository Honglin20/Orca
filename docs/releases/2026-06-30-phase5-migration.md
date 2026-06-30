# Release —— 阶段 5-M：schema 单轨化迁移（破坏性变更专项）

> **日期**：2026-06-30
> **性质**：破坏性、不可逆。schema 字段删除 + 校验重排 + examples/fixtures/tests 全改 + 文档全覆盖。
> **SPEC**：[`docs/specs/phase-5-run.md`](../specs/phase-5-run.md) §1 §2
> **计划**：[`docs/plans/2026-06-30-phase5-migration.md`](../plans/2026-06-30-phase5-migration.md)
> **状态**：✅ 完成，353 测试全绿（基线 323 → 净增 30）。零 after 字段残留。

---

## 0. 一句话总结

废除 `Node.after` 双轨制，统一为 **routes 单指针 + parallel 组显式并行**。
这是为 phase 5-R（run/ 编排层）扫清地基的破坏性迁移——`after` + 「入口候选」
机制与 routes 单指针模型根本冲突，必须先迁移再做编排。

---

## 1. 迁移前后对照

### schema 字段

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| `Node` 基类 | name + **after** + routes | name + routes |
| `Workflow` | nodes + outputs | nodes + **parallel** + outputs |
| 并行表达 | after 多源汇聚（隐式「入口候选」）| **`ParallelGroup`**（顶层独立列表，显式 diamond）|

### 校验项（重排）

| 编号 | 迁移前 | 迁移后 |
|---|---|---|
| ① | name 唯一 | name 唯一（**含 parallel 组名**，共享命名空间）|
| ② | entry 存在 | entry 存在（node 名 ∪ 组名）|
| ③ | after 引用有效 | **删除** |
| ④ | routes.to 有效 | routes.to 有效（**含 parallel 组名**；node + 组两侧）|
| ⑤ | after 静态边无环（Kahn）| **删除**（routes 回指是合法循环；死锁改运行时）|
| ⑥ | entry 可达终态（after+routes 反向边）| entry 可达终态（**routes 前向边 + parallel 组展开**）|
| ⑩ | — | **新增** parallel 组结构（branches ≥2 / 已定义 / 无重复 / 不自引用）|
| ⑪ | — | **新增** 兜底 route 位置（when=None 必须最后一条；node + 组两侧）|
| ⑬ | — | **新增** entry 非 parallel 组 |

现行 9 项：**①②④⑥⑦⑧⑨⑩⑪⑬**。

---

## 2. 改动清单

### 2.1 schema 层（`orca/schema/`）
- `workflow.py`：删 `Node.after`；新增 `ParallelGroup`（name/branches/failure_mode/routes，`extra=forbid`，位置在 ForeachNode 后、AnnotatedNode 前）；`Workflow.parallel: list[ParallelGroup] = []`；docstring 全改（单轨 routes + parallel 组）。
- `__init__.py`：导出 `ParallelGroup`。

### 2.2 compile 层（`orca/compile/validator.py`）
- 删 `_check_after_refs_valid`（③）+ `_check_after_acyclic` + `_find_cycle_path`（⑤）。
- ④ `_check_route_refs_valid`：合法集 = `_all_names(wf)`（node 名 ∪ 组名）；node 与 parallel 组两侧校验。
- ⑥ `_check_entry_reachable_to_end`：`successors_of` 删 after 反向边；route.to 指向组时 → 组名本身 + 展开 branches；组的 successors = 组 route.to；保留「无 route = 隐式终态」。
- 新增 `_check_parallel_groups`（⑩）、`_check_entry_is_node`（⑬）、`_check_route_fallback_last` + `_check_fallback_last`（⑪，DRY helper）。
- ① `_check_names_unique` 扩展：node 名 + 组名共享命名空间。
- `_iter_templates`（⑦）：补 parallel 组 route.when 遍历（与 node 路由同校验，避免静默放行坏引用）。
- 新增公共 helper：`_all_names` / `_parallel_group_names` / `_group_by_name`（DRY）。

### 2.3 examples（3 个）
- `nas.yaml`：删 4 处 after（routes 已是完整控制流）。
- `batch_assess.yaml`：删 after，加 routes 串行（finder→assessor→picker→$end）。
- `parallel_research.yaml`：彻底重写为 parallel 组 diamond（entry=researcher_a，a.routes→组 researchers_merge，组 branches=[a,b]，组.routes→synthesizer）。

### 2.4 fixtures（删 2 + 改 2 + 新增 7）
- 删：`after_cycle.yaml`、`bad_after.yaml`（after 已废，无意义）。
- 改：`multi_error.yaml`（去 after，凑 4 个新错误：②entry + ④route + ⑪兜底 + ⑦jinja）；`bad_foreach_source.yaml`（去 after，加 routes）。
- 新增：`bad_parallel_branches` / `bad_parallel_too_few` / `bad_parallel_dup_branch` / `bad_parallel_self_ref` / `bad_route_fallback` / `bad_entry_is_parallel` / `parallel_reachable`（正向）。

### 2.5 测试（353 全绿）
- `tests/schema/test_workflow.py`：重写 `test_parallel_research_yaml_deep_parse`（断言 `wf.parallel`）；新增 after 删除元测试 + ParallelGroup 构造/Literal/extra/dict 路径等 8 个测试。
- `tests/compile/test_validator.py`：删 after 测试；改 foreach/聚合/fixture 参数化；新增 ⑩⑪⑬④⑥ 内联单元测试（branches<2/不存在/重复/自引用/组名冲突/branch 非组/兜底 node+组两侧/entry 非组/route→组名合法/parallel 可达+死胡同+孤立组/组 route.when 校验）。
- `tests/compile/test_validate_profiles.py`：删 after 参数；phase2+capability 聚合测试改用 routes 死胡同。

### 2.6 文档（SPEC + plan + release + 顶层全覆盖）
- `phase-1-schema.md`：决策表改单轨 routes；Node 伪代码删 after；§6.2 静态并行政写 parallel 组；3 个示例去 after；对比表更新。
- `phase-2-compile.md`：③⑤ 删 + ⑩⑪⑬ 加；环检测改 routes 死锁；可达性描述去 after 反向边。
- 4 个 plan/release + TEMPLATE：Node 字段表去 after；校验项表标注 ③⑤ 已废 + ⑩⑪⑬ 新增。
- DESIGN.md / TASK.md / PLAN.md / CURRENT.md / phase-4-exec.md：编排描述「拓扑排序」→「单指针推进 + parallel 组」。

---

## 3. 设计裁决（防 drift）

1. **routes 单轨制**（参考 Conductor 验证）：一种机制表达所有串行控制流（线性/分支/回环/终止），消除 after 双轨的 join 歧义。
2. **parallel 组是顶层独立列表**（P1 方案），不是 node kind；entry 必须是 node。
3. **parallel 组幂等执行**：已执行的 branch 跳过（运行时语义，归 5-R；静态层只判可达性）。
4. **可达性 ⑥ 的 parallel 展开**：node 的 route.to 指向组时，组名本身 + 其 branches 都计入后继（组名可达防误报孤立；branches 可达因为组执行时跑它们）；组的 successors = 组 route.to。
5. **「无 route = 隐式终态」保留**：parallel_research/batch_assess 的 sink 节点需要；parallel 组同理。
6. **兜底 route 必须最后一条**（⑪ 编译时校验）：first-match-wins 命中兜底即返回，其后 route 是死代码。
7. **死锁改运行时**：静态不做 routes 无环校验（回环是合法循环）；运行时全部 when 不匹配且无兜底 → NoRouteMatchError（5-R）。

---

## 4. 偏离 SPEC/计划的判断

- **tests/ 下保留 `assert not hasattr(n, "after")` 等元测试**：计划 T7.1 grep 说「零 after 匹配（注释历史除外）」，但锁定「after 已删除」这个迁移成果是必要的回归保护（Rule 9：测试验证意图）。这些元测试不是 after 字段使用，是反向锁定。保留并在 release note 说明。
- **plan/release 文档用 `~~删除线~~` + 迁移注脚，而非逐字重写**：plan/release 是历史快照，逐字重写会篡改历史。改为「划掉旧行 + 标注 phase 5 已废 + 指向新 SPEC」，既满足 grep 零匹配又不失真。SPEC/TEMPLATE（活的契约）则逐字改成迁移后表述。
- **第二轮 review 发现的可达性 bug（G1）**：`successors_of` 展开组名时漏把组名标记可达，导致「a→split(组)」的 split 被误报孤立。修复：组名本身 + branches 都计入后继。这是计划未预见的实现细节，已在 review 中修复并加强测试断言。

---

## 5. code-reviewer 反馈与修复

两轮 review，全部修复：

### 第一轮
- 🔴 **`_iter_templates` 漏遍历 `wf.parallel` 的 route.when** → parallel 组路由的 Jinja2 引用不经 ⑦ 校验（静默放行坏引用）。已修复 + 补 2 个回归测试（反：引用 ghost；正：output）。
- 🟡 测试 docstring「8 项」改「9 项」。
- 🟡 补 `test_parallel_group_no_routes_is_implicit_terminal`（组无 routes = 隐式终态）。
- 🟢 parallel_research 深解析测试补注释（entry∈branches 幂等执行归 5-R）。

### 第二轮
- 🔴 **G1 可达性 bug + 测试漏断言**：`successors_of` 展开组名时漏标记组名可达 → split 被误报孤立；测试只断言「不抛」掩盖了 bug。修复实现（组名 + branches 都计入后继）+ 加强测试断言（断言组不在孤立 warning）。
- 🟡 G2：补 `test_parallel_group_empty_name`（组空 name → error）。
- 🟡 G3：补 `test_route_single_fallback_route_is_ok`（组侧单 route len=1 合法）。

---

## 6. 验收结果

| 验收项 | 结果 |
|---|---|
| `grep '\bafter\b' orca/ examples/ tests/`（字段引用）| ✅ 零（仅剩迁移说明注释 + 元测试断言）|
| `grep '入口候选' orca/ docs/`（排除迁移记录）| ✅ 零 |
| `grep '拓扑排序\|after 无环\|静态依赖环' docs/specs/ docs/plans/`（排除迁移记录）| ✅ 零 |
| `uv run pytest -q` | ✅ 353 passed（基线 323 → 净增 30）|
| examples 验证（nas/batch_assess/parallel_research 解析+校验）| ✅ 三全 OK |
| 新增校验测试（⑩⑪⑬④⑥）| ✅ 全覆盖 |

---

## 7. 给 phase 5-R（run/ 编排层）的契约

- `Node` 只有 `routes`；`Workflow.parallel` 是 `list[ParallelGroup]`。
- `Router.resolve(routes, output, ctx) -> str`：first-match-wins，无兜底且全不匹配 → NoRouteMatchError。
- parallel 组执行：`asyncio.gather(branches)`，已执行 branch 跳过（幂等），failure_mode 三态。
- entry 必须是 node；单指针从 entry 起步，沿 routes 推进，遇 parallel 组则并行执行其 branches 后按组 routes 推进。
- max_iterations 防路由死循环；超限 → workflow_failed。

---

**Commit SHA**：见下方 CHANGELOG 索引。
