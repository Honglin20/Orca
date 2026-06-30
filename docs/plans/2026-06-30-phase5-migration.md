# 开发计划 —— 阶段 5-M：schema 单轨化迁移（破坏性变更专项）

> **状态**：待执行（**必须先于 5-R 完成，全绿后才能开始 run/ 开发**）
> **SPEC**：[`docs/specs/phase-5-run.md`](../specs/phase-5-run.md) §2
> **性质**：破坏性、不可逆。schema 字段删除 + 校验重排 + examples/fixtures/tests 全改 + 文档全覆盖。
> **铁律**：每个 TASK 完成后必须自检「零 after 残留」+ 跑全量测试。**严禁半迁移状态提交。**

---

## 0. 迁移原则与执行顺序

### 执行顺序（严格串行，不可跳步）

```
T1 schema 变更（workflow.py：删 after + 加 ParallelGroup + 改 Workflow）
   ↓ 此时 schema 已变，所有依赖 after 的代码立即报错（编译期/测试期）
T2 validator 重排（删 ③⑤ + 改 ④⑥ + 加 ⑩⑪⑫⑬）
   ↓ 校验层适配新 schema
T3 examples 重写（nas/batch/parallel_research）
   ↓ 示例用新 schema
T4 fixtures 重写（删 2 + 改 2 + 新增 4）
T5 测试重写（schema/compile/profiles 三处）
   ↓ 全量测试应在此阶段绿
T6 文档全覆盖（SPEC/plan/release/顶层文档）
   ↓ 文档与代码一致
T7 总验收（grep 残留 + 全量测试 + examples 验证）
```

### 铁律
1. **T1→T5 必须一气呵成**：schema 一变，整个依赖链断掉，必须全部修完才能跑通。
2. **每个 TASK 末尾自检**：`grep -rn '\bafter\b' <changed files>` 确认无残留。
3. **T7 前 commit 一次**：所有代码变更（T1-T5）作为迁移 commit；T6 文档可同 commit 或紧随。
4. **每步都要跑测试**：T5 完成前测试会是红的（schema 变了），这是预期；T5 完成后必须全绿。

---

## T1. schema 变更（`orca/schema/workflow.py`）

### T1.1 删除 `Node.after` 字段
- 删除 `Node` 类的 `after: list[str] = []` 字段（`:47`）及其注释。
- `Node` 基类保留 `name` + `routes` 两个字段。

### T1.2 新增 `ParallelGroup` 类
在 `ForeachNode` 定义之后、`AnnotatedNode` 之前插入：
```python
class ParallelGroup(BaseModel):
    """静态并行组（顶层独立列表项）。branches 并行执行，等全部完成后按 routes 推进。

    用于表达 DAG 分叉+合并（diamond）：A 的 route 指向 parallel 组，
    组的 branches 并行跑完后，组的 route 推进单指针。
    """
    model_config = ConfigDict(extra="forbid")
    name: str
    branches: list[str]
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    routes: list[Route] = []
```

### T1.3 修改 `Workflow` 类
- 新增字段 `parallel: list[ParallelGroup] = []`。
- 更新 docstring：说明 entry 必须是 node（不是 parallel 组），parallel 组独立列表。

### T1.4 更新模块 docstring
- `:7` 删除 `after·routes 引用合法 / DAG 无环`，改为 `routes 引用合法 / 死锁检测 / parallel 组校验`。
- 模块顶部设计说明更新：注明单轨 routes 控制流 + parallel 组并行。

### T1.5 更新 `orca/schema/__init__.py` 导出
- 导出 `ParallelGroup`（新增）。
- 检查是否有任何地方导出过 `after`（无，after 是字段非类）。

### T1.6 验收（T1）
- [ ] `python -c "from orca.schema import Workflow, ParallelGroup, Route"` 不报错
- [ ] `python -c "from orca.schema import Node; n = Node(); print(n.after)"` → AttributeError（after 已删）
- [ ] `python -c "from orca.schema import Node; n = Node(); print(n.routes)"` → `[]`（routes 还在）
- [ ] `ParallelGroup(name="g", branches=["a","b"])` 构造成功
- [ ] `ParallelGroup(name="g", branches=["a"], failure_mode="invalid")` → ValidationError
- [ ] **此时跑 `pytest` 会大面积失败**（after 引用残留）—— 这是预期，T2-T5 修复
- [ ] `grep -n '\bafter\b' orca/schema/workflow.py` → 仅在迁移历史注释里（若保留），无字段定义

### T1.7 测试用例（`tests/schema/test_workflow.py` 增补，T5 统一跑）
```python
def test_node_has_no_after():
    n = AgentNode(name="a", prompt="p", routes=[{"to":"$end"}])
    assert not hasattr(n, "after")  # after 字段已删除

def test_parallel_group_basic():
    g = ParallelGroup(name="g", branches=["a", "b"])
    assert g.branches == ["a", "b"]
    assert g.failure_mode == "fail_fast"  # 默认
    assert g.routes == []

def test_workflow_has_parallel_field():
    wf = Workflow(name="w", entry="a", nodes=[AgentNode(name="a", prompt="p", routes=[{"to":"$end"}])])
    assert wf.parallel == []  # 默认空
```

---

## T2. validator 重排（`orca/compile/validator.py`）

### T2.1 删除校验 ③（`_check_after_refs_valid`）
- 删除 `:136-145` 整个函数。
- 删除 `validate_workflow` 里对它的调用（`:86`）。

### T2.2 删除校验 ⑤（`_check_after_acyclic` + `_find_cycle_paths`）
- 删除 `:166-233` 两个函数。
- 删除 `validate_workflow` 里对 ⑤ 的调用（`:88`）。

### T2.3 修改校验 ④（`_check_route_refs_valid`）
```python
def _check_route_refs_valid(wf: Workflow, result: ValidationResult) -> None:
    names = _name_set(wf) | {g.name for g in wf.parallel}  # node 名 + parallel 组名
    for node in wf.nodes:
        for route in node.routes:
            if route.to != "$end" and route.to not in names:
                result.add_error(f"node '{node.name}' 的 route 引用了不存在的目标 '{route.to}'")
    for group in wf.parallel:
        for route in group.routes:
            if route.to != "$end" and route.to not in names:
                result.add_error(f"parallel 组 '{group.name}' 的 route 引用了不存在的目标 '{route.to}'")
```

### T2.4 修改校验 ⑥（`_check_entry_reachable_to_end`）
- `successors(node)` 删除 after 反向边（`:251-256`），只保留 route 前向边：
```python
def successors(node) -> set[str]:
    out = set()
    for r in node.routes:
        if r.to == "$end": continue
        if r.to in parallel_group_names:  # parallel 组 → 展开为组内 branches
            out.update(group_for[r.to].branches)
        else:
            out.add(r.to)
    return out
```
- parallel 组也作为可达性节点：组的 successors = 组的 route.to。
- `is_terminal` 扩展：node 无 route 或有 `$end` route；parallel 组同样判定。

### T2.5 新增校验 ⑩ `_check_parallel_groups`
```python
def _check_parallel_groups(wf, result):
    node_names = _name_set(wf)
    group_names = [g.name for g in wf.parallel]
    # ⑫ 命名空间：node 名 + 组名全局唯一
    # ⑩-1 branches 长度 >= 2
    # ⑩-2 branches 每项 ∈ node_names
    # ⑩-3 branches 无重复
    # ⑩-4 组的 routes 走 ④（已含）
    # ⑩-5 组不自引用（route.to != group.name）
    # ⑩-6 entry 不是组（⑬，见 T2.6）
```

### T2.6 新增校验 ⑬ `_check_entry_is_node`
```python
def _check_entry_is_node(wf, result):
    group_names = {g.name for g in wf.parallel}
    if wf.entry in group_names:
        result.add_error(f"entry '{wf.entry}' 不能是 parallel 组，必须是 node")
```

### T2.7 修改校验 ①（`_check_names_unique`）
- 名称集合扩展为 node 名 + parallel 组名，全局唯一。

### T2.8 新增校验 ⑪ `_check_route_fallback_last`
```python
def _check_route_fallback_last(wf, result):
    """无 when 的兜底 route 必须是列表最后一条。"""
    for node in wf.nodes:
        for i, route in enumerate(node.routes):
            if route.when is None and i != len(node.routes) - 1:
                result.add_error(f"node '{node.name}' 的无条件 route 不是最后一条，其后的 route 不可达")
    for group in wf.parallel:
        # 同样校验
```

### T2.9 更新 `validate_workflow` 调用顺序
```python
def validate_workflow(wf):
    result = ValidationResult()
    _check_names_unique(wf, result)            # ①（含 parallel 组名）
    _check_entry_exists(wf, result)            # ②
    _check_entry_is_node(wf, result)           # ⑬
    _check_route_refs_valid(wf, result)        # ④（含 parallel 组 routes）
    _check_entry_reachable_to_end(wf, result)  # ⑥（routes + parallel 展开）
    _check_parallel_groups(wf, result)         # ⑩
    _check_route_fallback_last(wf, result)     # ⑪
    _check_jinja2_refs(wf, result)             # ⑦
    _check_foreach_source(wf, result)          # ⑧
    _check_profiles(wf, result)                # ⑨
    return result.raise_if_errors()
```

### T2.10 更新 docstring
- 模块 docstring 的「9 项校验」改为「11 项校验（①②④⑥⑦⑧⑨⑩⑪⑬，③⑤ 已废）」。

### T2.11 验收（T2）
- [ ] ③⑤ 函数已删，`grep -n '_check_after' orca/compile/validator.py` 零匹配
- [ ] ④ 对 parallel 组 routes 也校验
- [ ] ⑥ successors 不含 after 反向边
- [ ] ⑩⑪⑬ 函数存在且被调用
- [ ] `python -c "from orca.compile.validator import validate_workflow"` 不报错

### T2.12 测试用例（`tests/compile/test_validator.py`，T5 统一跑）
- ⑩：branches 缺失 / <2 / 重复 / 组自引用 → 各一个 error 测试
- ⑪：兜底不在最后 → error
- ⑬：entry 是 parallel 组 → error
- ④：route.to 指向 parallel 组名 → 合法（不报错）
- ⑥：parallel 组可达性（组 → branches → 组 routes → $end）

---

## T3. examples 重写

### T3.1 `examples/nas.yaml`
- 删除 4 处 `after:`（optimizer 无；trainer:30 / evaluator:41 / reviewer:51 / record_best:66）。
- routes 保持不变（已完整）。
- 确认每个有 after 的 node 改后仍可达 $end（routes 已覆盖）。

### T3.2 `examples/batch_assess.yaml`
- finder：加 `routes: [{to: assessor}]`
- assessor：删 `after: [finder]`（已是 foreach，自带 source 依赖）
- picker：删 `after: [assessor]`，加 `routes: [{to: $end}]`
- assessor：加 `routes: [{to: picker}]`

### T3.3 `examples/parallel_research.yaml`（彻底重写）
按 SPEC §2.3 的范式重写：entry=researcher_a，a.routes→parallel 组 `researchers_merge`，组 branches=[a,b]，组.routes→synthesizer。

### T3.4 验收（T3）
- [ ] `python -c "from orca.compile.parser import load_workflow; load_workflow('examples/nas.yaml')"` 成功且校验通过
- [ ] `load_workflow('examples/batch_assess.yaml')` 成功且校验通过
- [ ] `load_workflow('examples/parallel_research.yaml')` 成功且校验通过
- [ ] `grep -n 'after' examples/*.yaml` → 零匹配

### T3.5 测试用例
```python
def test_examples_load_and_validate():
    for name in ["nas", "batch_assess", "parallel_research"]:
        wf = load_workflow(f"examples/{name}.yaml")
        validate_workflow(wf)  # 不抛

def test_parallel_research_has_parallel_group():
    wf = load_workflow("examples/parallel_research.yaml")
    assert len(wf.parallel) == 1
    assert wf.parallel[0].branches == ["researcher_a", "researcher_b"]
```

---

## T4. fixtures 重写

### T4.1 删除
- `tests/compile/fixtures/after_cycle.yaml`（after 环已无意义）
- `tests/compile/fixtures/bad_after.yaml`（after 引用已无意义）

### T4.2 修改
- `tests/compile/fixtures/multi_error.yaml`：删 `after: [ghost]`；改用新错误凑多错误（建议：兜底 route 不在最后 + entry 不存在 + route nowhere + jinja nope）。
- `tests/compile/fixtures/bad_foreach_source.yaml`：删 `after: [f]`，finder 加 `routes: [{to: fe}]`，foreach 加 `routes: [{to: $end}]`。

### T4.3 新增
- `tests/compile/fixtures/bad_parallel_branches.yaml`：branches 引用不存在的 node。
- `tests/compile/fixtures/bad_parallel_too_few.yaml`：branches 长度 1。
- `tests/compile/fixtures/bad_parallel_dup_branch.yaml`：branches 有重复。
- `tests/compile/fixtures/bad_parallel_self_ref.yaml`：组的 route.to 指向自己。
- `tests/compile/fixtures/bad_route_fallback.yaml`：兜底 route 不在最后。
- `tests/compile/fixtures/bad_entry_is_parallel.yaml`：entry 指向 parallel 组名。
- `tests/compile/fixtures/parallel_reachable.yaml`：合法 parallel 组 + 可达性（正向用例）。

### T4.4 验收（T4）
- [ ] 删除的 2 个 fixture 不存在
- [ ] 新增 7 个 fixture 存在且各自触发预期错误
- [ ] `grep -n 'after' tests/compile/fixtures/*.yaml` → 仅 multi_error 注释历史（若有），无字段

### T4.5 测试用例
```python
@pytest.mark.parametrize("fixture,expected_msg", [
    ("bad_parallel_branches", "branches"),
    ("bad_parallel_too_few", "branches"),
    ("bad_parallel_dup_branch", "重复"),
    ("bad_parallel_self_ref", "自引用"),
    ("bad_route_fallback", "最后一条"),
    ("bad_entry_is_parallel", "parallel 组"),
])
def test_new_fixtures_raise(fixture, expected_msg):
    wf = load_fixture(fixture)
    with pytest.raises(ConfigurationError) as e:
        validate_workflow(wf)
    assert any(expected_msg in err for err in e.value.errors)
```

---

## T5. 测试重写

### T5.1 `tests/schema/test_workflow.py`
- `:280-284`（test_parallel_research_yaml_deep_parse）：重写为断言 `wf.parallel[0].branches`。
- 删除任何 `assert ... .after == [...]` 断言。
- 新增 T1.7 的 parallel 测试。

### T5.2 `tests/compile/test_validator.py`
- `:83-95`（after 引用有效）：**删除**。
- `:114-132`（after 环 + route 回指）：
  - `test_after_cycle_detected`：**删除**。
  - `test_route_backedge_is_not_after_cycle`：保留但重写为纯 route 环（去 after），改名 `test_route_backedge_is_legal_loop`。
- `:177`：`after=["a"]` → 删 after，用 routes 串行（`routes=[{"to":"$end"}]` 配合前置 node）。
- `:231,:254,:268`（foreach after）：删 after，加 routes 串行。
- `:283-288`（multi_error）：对齐新 fixture。
- `:324-326`（fixture 参数化）：删 after_cycle/bad_after，加新 fixture。
- 新增 T2.12 的全部新校验测试。

### T5.3 `tests/compile/test_validate_profiles.py`
- `:159,:161`：删除构造 `AgentNode` 时的 `after=["b"]`/`after=["a"]` 参数。

### T5.4 验收（T5）— **关键里程碑：全量测试绿**
- [ ] `uv run pytest -q` 全绿（含 phase 1+2+3+4 不回归）
- [ ] 测试数量 ≥ 迁移前（删除的 after 测试被新 parallel/死锁测试替代）
- [ ] 无 skip / xfail 残留（除非原本就有）
- [ ] `grep -rn '\bafter\b' tests/` → 零 after 字段引用（注释历史除外）

### T5.5 commit 检查点
**T5 完成且全绿后，做一次 commit**（schema + validator + examples + fixtures + tests）。此时代码层面迁移完成，进入文档修正 T6。

---

## T6. 文档全覆盖

### T6.1 SPEC 修正（`docs/specs/`）
| 文件 | 改动 |
|---|---|
| `phase-1-schema.md` | 决策表「after vs depends_on」改为「单轨 routes 决策记录」；Node 伪代码删 after；§6.2 静态并行政写为 parallel 组；3 个示例（nas/batch/parallel_research）去 after；对比表更新 |
| `phase-2-compile.md` | 删 ③⑤ 校验描述；加 ⑩⑪⑫⑬；环检测改为「routes 死锁」；可达性描述去 after 反向边 |

### T6.2 plan/release 修正（`docs/plans/` + `docs/releases/`）
| 文件 | 改动 |
|---|---|
| `plans/2026-06-29-phase1-schema.md:21` | Node 字段表删 after，加 parallel |
| `plans/2026-06-30-phase2-compile.md:52,54,63,65,110,111` | 校验项表删 ③⑤ 加 ⑩⑪⑫⑬ |
| `plans/_TEMPLATE.md:24` | Node 字段删 after |
| `releases/2026-06-29-phase1-schema.md:53` | E2E 测试描述去 after |
| `releases/2026-06-30-phase2-compile.md:23,24,34` | 校验项去 after |

### T6.3 顶层文档修正
| 文件 | 改动 |
|---|---|
| `CLAUDE.md` | schema 章节去 after（若有） |
| `DESIGN.md:11,62` | run/ 描述「拓扑」→「单指针推进 + parallel 组」 |
| `TASK.md:24,260,276,307` | 「拓扑」→「单指针推进」 |
| `PLAN.md:46` | phase 2「after 无环」→「routes 死锁检测」 |
| `PLAN.md:62` | phase 5「拓扑排序」→「单指针推进」 |
| `status/CURRENT.md:19` | orchestrator 描述改单指针 |
| `specs/phase-4-exec.md:24,402,535,667` | 「拓扑」→「单指针推进」 |

### T6.4 新增 release note
`docs/releases/2026-06-30-phase5-migration.md`：记录此次破坏性迁移（去 after、加 parallel、单轨 routes、影响面、迁移前后对照）。

### T6.5 验收（T6）
- [ ] `grep -rn '拓扑排序\|after 无环\|静态依赖' docs/specs/ docs/plans/` → 零匹配（除迁移 release 记录历史）
- [ ] `grep -rn '入口候选' docs/ orca/` → 零匹配
- [ ] 所有 SPEC 的校验项编号与代码一致（①②④⑥⑦⑧⑨⑩⑪⑬）

---

## T7. 总验收（Definition of Done）

### T7.1 残留检查（必须全部零匹配）
```bash
grep -rn '\bafter\b' orca/ examples/ tests/                           # 零 after 字段
grep -rn '入口候选\|隐式并行' orca/ docs/                              # 零匹配
grep -rn '拓扑排序\|after 无环\|静态依赖环' docs/specs/ docs/plans/      # 零匹配
```

### T7.2 测试全绿
```bash
uv run pytest -q   # 全绿，无回归
```

### T7.3 examples 验证
```bash
python -c "
from orca.compile.parser import load_workflow
from orca.compile.validator import validate_workflow
for name in ['nas', 'batch_assess', 'parallel_research']:
    wf = load_workflow(f'examples/{name}.yaml')
    validate_workflow(wf)
    print(f'{name}: OK')
"
```

### T7.4 验收 checklist
- [ ] T1 schema：after 删 + ParallelGroup 加 + Workflow.parallel 加
- [ ] T2 validator：③⑤ 删 + ④⑥ 改 + ⑩⑪⑬ 加 + 调用顺序更新
- [ ] T3 examples：3 个全部去 after/重写，校验通过
- [ ] T4 fixtures：删 2 + 改 2 + 新增 7
- [ ] T5 测试：全绿，无 after 残留
- [ ] T6 文档：SPEC/plan/release/顶层全覆盖
- [ ] T7.1 残留检查：三组 grep 零匹配
- [ ] T7.2 全量测试绿
- [ ] T7.3 examples 全通过
- [ ] release note（phase5-migration）写完
- [ ] CURRENT.md + CHANGELOG 更新

---

## 附录：迁移前后对照表（给实现者速查）

### schema 字段
| 项 | 迁移前 | 迁移后 |
|---|---|---|
| Node 基类 | name + after + routes | name + routes |
| Workflow | nodes + outputs | nodes + parallel + outputs |
| 并行表达 | after 多源汇聚（隐式）| ParallelGroup（显式顶层列表）|

### 校验项
| 编号 | 迁移前 | 迁移后 |
|---|---|---|
| ① | name 唯一 | name 唯一（含 parallel 组名）|
| ③ | after 引用有效 | **删除** |
| ④ | routes.to 有效 | routes.to 有效（含 parallel 组名）|
| ⑤ | after 无环 | **删除** |
| ⑥ | entry 可达终态（after+routes）| entry 可达终态（routes+parallel 展开）|
| ⑩ | — | **新增** parallel 组校验 |
| ⑪ | — | **新增** 兜底 route 位置 |
| ⑬ | — | **新增** entry 非 parallel 组 |

### examples
| example | 迁移前 | 迁移后 |
|---|---|---|
| nas | routes + 冗余 after | 仅 routes（删 after）|
| batch_assess | after 串行 | routes 串行 |
| parallel_research | 多入口 + after 汇聚 | entry + parallel 组 |

### fixtures
| fixture | 处理 |
|---|---|
| after_cycle / bad_after | 删除 |
| multi_error / bad_foreach_source | 修改（去 after）|
| bad_parallel_branches/too_few/dup_branch/self_ref/route_fallback/entry_is_parallel/reachable | 新增 |
