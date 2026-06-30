# 阶段 2 SPEC —— compile/ 解析与校验层

> **状态**：最终版（待分发实现）
> **依据**：[TASK.md](../TASK.md) §1 §6 · [PLAN.md](../PLAN.md) · [phase-1-schema.md](phase-1-schema.md)
> **范围**：YAML → 校验过的 Workflow model。两层校验，**不做执行**。
> **执行任务**：见文末 §9「TASK2 完整描述」

---

## 0. 设计目标

compile/ 是 schema 和 run/ 之间的**翻译层**。只做两件事：

1. **解析**：YAML 文件 → Workflow model（含 prompt 约定加载）
2. **校验**：两层校验，失败 fail loud，绝不放行错误 workflow 到 run/

### 对外接口极简（用户/LLM 只需知道这一个）

```python
def load_workflow(path: str | Path) -> Workflow:
    """YAML 文件 → 校验过的 Workflow。失败抛 ConfigurationError（含所有错误）。"""
```

**就这一个公共函数**。内部校验再多也藏在后面——这是"对外极简，内部要全"的设计原则（学 Conductor）。

### 两层校验（从 Conductor 学的核心设计）

| 层 | 干什么 | 谁负责 | phase 1 已做？ |
|---|---|---|---|
| **结构校验** | 字段/类型/extra 字段/discriminator 分派 | pydantic（schema 层 `extra="forbid"`）| ✅ |
| **语义校验** | 图/引用/环/可达/Jinja2 引用 | compile/ 的 validator | ❌ phase 2 做 |

phase 2 的核心新增是**语义校验**。

---

## 1. errors + warnings 模型（决策 1+2）

### ConfigurationError（致命，收集所有错误）

```python
class ConfigurationError(Exception):
    """workflow 校验失败。含所有 errors（非致命 warnings 不在此）。"""
    def __init__(self, errors: list[str], warnings: list[str]):
        self.errors = errors           # 致命错误列表
        self.warnings = warnings       # 非致命警告列表（一起带上，供 CLI 展示）
        super().__init__(self._format())

    def _format(self) -> str:
        lines = ["Workflow 校验失败："]
        for e in self.errors:
            lines.append(f"  ❌ {e}")
        if self.warnings:
            lines.append("警告（非致命）：")
            for w in self.warnings:
                lines.append(f"  ⚠️  {w}")
        return "\n".join(lines)
```

**关键**：跑完**所有**校验项，把所有 errors 收集起来一起报（决策 1 的 B）。对"让 LLM 生成 YAML"特别重要——LLM 生成的 YAML 常多处错，一次报全能省多轮交互。

### ValidationResult（内部，承载 errors + warnings）

```python
@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None: self.errors.append(msg)
    def add_warning(self, msg: str) -> None: self.warnings.append(msg)

    def raise_if_errors(self) -> list[str]:
        """有 errors 抛 ConfigurationError；无则返回 warnings。"""
        if self.errors:
            raise ConfigurationError(self.errors, self.warnings)
        return self.warnings
```

### warning 的语义（决策 2 的 B）

warning = "能跑，但可能不是你想要的"。例如：
- node/parallel 组没被任何路由到达（死代码，孤立）
- node 的 routes 全是条件 `when`，没有兜底（可能路由不匹配卡住）
- foreach 的 body 是 script 但 source 数组元素不是命令期望的格式（无法静态判断，仅提示）

warning **不阻止** load_workflow 返回，但 CLI 会展示给用户。

---

## 2. 文件结构

```
orca/compile/
├── __init__.py          # 导出 load_workflow, ConfigurationError
├── parser.py            # YAML → Workflow（含 prompt 约定加载）
└── validator.py         # 语义校验（8 项 + warnings）
```

`__init__.py` 导出：
```python
__all__ = ["load_workflow", "ConfigurationError", "ValidationResult"]
```

---

## 3. parser.py —— 解析流程

```python
def load_workflow(path: str | Path) -> Workflow:
    """YAML 文件 → 校验过的 Workflow。失败抛 ConfigurationError。"""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))   # 1. 读 YAML
    wf = Workflow(**raw)                                      # 2. pydantic 结构校验
    _load_prompts(wf, base_dir=path.parent)                   # 3. prompt 约定加载
    validate_workflow(wf)                                     # 4. 语义校验（见 §4）
    return wf
```

### prompt 约定加载（_load_prompts）

phase 1 定义：AgentNode.prompt=None 时从 `agents/<name>.md` 加载。这步在 compile/ 做，把"约定"变成"显式"：

```python
def _load_prompts(wf: Workflow, base_dir: Path) -> None:
    """对每个 prompt=None 的 agent，从 agents/<name>.md 加载。文件不存在 → 校验错误。"""
    for node in wf.nodes:
        if isinstance(node, AgentNode) and node.prompt is None:
            prompt_path = base_dir / "agents" / f"{node.name}.md"
            if not prompt_path.exists():
                # 收集为校验错误（在 validate_workflow 里统一报）
                raise ConfigurationError(
                    [f"agent '{node.name}' 未声明 prompt 且找不到约定文件 {prompt_path}"],
                    []
                )
            node.prompt = prompt_path.read_text(encoding="utf-8")
```

**校验完后的 Workflow，每个 agent 的 prompt 都是确定字符串**，run/ 不用再管文件加载。

### load_workflow 的失败模式（都 fail loud）

| 失败 | 异常 | 说明 |
|---|---|---|
| YAML 语法错 | `yaml.YAMLError` | 透传 |
| pydantic 结构错 | `pydantic.ValidationError` | 包装成 ConfigurationError 或透传（见决策）|
| prompt 文件缺失 | `ConfigurationError` | 精确指出哪个 agent |
| 语义校验失败 | `ConfigurationError` | 含所有 errors + warnings |

**决策点**：pydantic.ValidationError 要不要包装成 ConfigurationError？我建议**包装**——对外只暴露一种错误类型（ConfigurationError），CLI 层处理统一。实现时用 `try: Workflow(**raw) except ValidationError as e: raise ConfigurationError([str(e)], [])`。

---

## 4. validator.py —— 语义校验（核心，8 项）

```python
def validate_workflow(wf: Workflow) -> list[str]:
    """全部语义校验。返回 warnings；有 errors 抛 ConfigurationError。"""
    result = ValidationResult()
    _check_names_unique(wf, result)            # ①（含 parallel 组名）
    _check_entry_exists(wf, result)            # ②
    _check_entry_is_node(wf, result)           # ⑬ entry 非 parallel 组
    _check_route_refs_valid(wf, result)        # ④（node + parallel 组 routes）
    _check_entry_reachable_to_end(wf, result)  # ⑥（routes 前向边 + parallel 展开）
    _check_parallel_groups(wf, result)         # ⑩ parallel 组结构
    _check_route_fallback_last(wf, result)     # ⑪ 兜底 route 位置
    _check_jinja2_refs(wf, result)             # ⑦
    _check_foreach_source(wf, result)          # ⑧
    _check_profiles(wf, result)                # ⑨ capability（profiles/validate）
    return result.raise_if_errors()
```

### 9 项校验规则（每项 fail loud，错误信息精确）

> phase 5 单轨化后：③⑤（after 校验）随 `Node.after` 删除而废除；新增 ⑩⑪⑬。

#### ① name 全局唯一 + 非空（node 名 + parallel 组名共享命名空间）
- error: `name` 为空字符串（phase 1 把 name 设为可选）
- error: 两个 node 同名 / node 名与 parallel 组名冲突
- 消息示例：`名称重复：'optimizer' 出现 2 次（node 名与 parallel 组名共享命名空间）`

#### ② entry 存在
- error: `wf.entry` 不是任何 node / parallel 组的 name
- 消息：`entry 'optimizer' 不存在于 nodes / parallel 中`

#### ⑬ entry 不是 parallel 组（必须 node）
- error: `wf.entry` 指向 parallel 组名（单指针从 node 起步）
- 消息：`entry 'split' 不能是 parallel 组，必须是 node`

#### ④ routes[].to 引用有效（node + parallel 组两侧）
- error: `routes[].to` 不是真实 node / parallel 组名 / `"$end"`
- 消息：`node 'reviewer' 的 route 引用了不存在的目标 'xxx'`

#### ⑥ 从 entry 至少有一条路能到 $end（可达性，routes + parallel 展开）
- 从 entry 出发，沿 routes 前向边走（route.to 指向 parallel 组时展开为组 branches），
  必须能到达某个 `routes[].to == "$end"` 的节点；无 route 的 node 视为隐式终态
- error: 死胡同（可达却到不了 $end）
- warning: 某个 node / parallel 组从 entry 不可达（孤立，可能忘了接线）

#### ⑩ parallel 组结构校验
- branches 长度 ≥ 2（少于 2 不是并行）
- branches 每项是已定义的 node 名（不能指向组）
- branches 内无重复
- 组的 route 不能自引用（指向自己 → 死锁）

#### ⑪ 兜底 route 位置（node + parallel 组两侧）
- 无 `when` 的兜底 route 必须是 routes 列表最后一条，否则其后的 route 永远不可达
- 消息：`node 'X' 的无条件兜底 route 不是最后一条，其后的 route 永远不可达`

#### ⑦ Jinja2 引用校验（浅校验，决策 3 的 A）
- 用 Jinja2 的 `meta.find_undeclared_variables` 解析 prompt / when / outputs 里的模板
- 提取变量名，校验：
  - `{{ optimizer.output.x }}` 的 `optimizer` 是真实 node
  - `{{ workflow.input.x }}` 的 `x` 在 `wf.inputs` 里声明（可选，未声明只 warning）
  - `{{ item }}` / `{{ _index }}` 只在 foreach body 的 prompt 里合法
- **浅校验**：只查变量名的 node 部分，不查 `.output.field` 字段级（字段是运行时的）
- error: 引用了不存在的 node
- warning: 引用了未声明的 workflow input

#### ⑧ foreach.source 校验（决策 4，编译时浅校验）
- `source: "finder.output.candidates"` 的 `finder` 必须是真实 node
- **不校验** `candidates` 字段是否存在/是数组（运行时校验，归 run/）
- error: source 引用了不存在的 node

---

## 5. 与 Conductor 的对比（验证决策）

| 维度 | Conductor | Orca（本 SPEC）| 决策理由 |
|---|---|---|---|
| 公共接口 | `load_config(path)` | `load_workflow(path)` | 极简 |
| errors 模型 | 收集所有 errors 一起报 | 同（B）| LLM 友好 |
| warnings | 有（返回 list）| 有（决策 2 的 B）| 提示死代码等 |
| Jinja2 校验 | AST 解析（`meta.find_undeclared_variables`）| 同（浅校验）| 抓 80% typo |
| 环检测 | inode 级 sub-workflow 环检测 | 原静态边 Kahn 算法 | phase 5 单轨化：routes 回指是合法循环；死锁改运行时检测（无静态环校验）|
| prompt 加载 | 内联 only | 约定 `agents/<name>.md` + 内联 | Orca 差异化 |

---

## 6. 验收标准

### 6.1 接口验收
- [ ] `from orca.compile import load_workflow, ConfigurationError` 能 import
- [ ] `load_workflow("examples/nas.yaml")` 返回 Workflow，不抛异常
- [ ] 所有 agent 的 prompt 都是确定字符串（约定加载生效）

### 6.2 解析验收
- [ ] 3 个 examples（nas/parallel_research/batch_assess）都能 load_workflow 通过
- [ ] prompt 约定加载：nas.yaml 的 optimizer 若省略 prompt 且有 agents/optimizer.md → 加载成功；无文件 → ConfigurationError

### 6.3 校验验收（9 项：①②④⑥⑦⑧⑨⑩⑪⑬，每项有对应的错误用例测试）
- [ ] ① name 重复/空（含 parallel 组名冲突） → ConfigurationError 含 "重复" 或 "空"
- [ ] ② entry 不存在 → ConfigurationError
- [ ] ⑬ entry 指向 parallel 组 → ConfigurationError
- [ ] ④ routes.to 不存在（node + parallel 组两侧） → ConfigurationError
- [ ] ⑥ entry 到不了 $end（含 parallel 组死胡同） → ConfigurationError
- [ ] ⑦ Jinja2 引用不存在 node → ConfigurationError；未声明 input → warning
- [ ] ⑧ foreach.source 引用不存在 node → ConfigurationError
- [ ] ⑩ parallel 组 branches <2 / 引用不存在 / 重复 / 自引用 → ConfigurationError
- [ ] ⑪ 兜底 route 不是最后一条（node + parallel 组两侧） → ConfigurationError

### 6.4 errors 聚合验收
- [ ] 一个 YAML 多处错 → ConfigurationError.errors 包含**所有**错误（不止第一个）
- [ ] warnings 不阻止 load_workflow 返回

### 6.5 测试验收
- [ ] `tests/compile/test_parser.py`：解析 + prompt 加载 + 3 examples
- [ ] `tests/compile/test_validator.py`：8 项校验各正/反例 + errors 聚合 + warnings
- [ ] 全部测试通过（pytest），含 schema 层的 43 个不回归

### 6.6 文件验收
- [ ] `orca/compile/__init__.py` / `parser.py` / `validator.py`
- [ ] `tests/compile/` 目录 + 测试用例 fixtures（错误 YAML 样本）

---

## 7. 实现要点（给实现 session）

1. **依赖**：compile/ 依赖 `orca.schema`（已实现）+ pydantic + pyyaml + jinja2（jinja2 仅用于 meta 解析，不渲染）
2. **jinja2 用法**：只用 `jinja2.Environment().parse(template)` + `meta.find_undeclared_variables(ast)`，**不调用 render**（渲染是 run/ 的事）
3. **可达性（不动点）**：从 entry 出发，沿 routes 前向边（route.to 指向 parallel 组时展开为 branches）走，看能否到 `$end`；routes 回指是合法循环，无静态环检测（死锁改运行时）
4. **parallel 组校验**：branches ≥2 / 引用已定义 node / 无重复 / 不自引用
5. **错误信息要精确**：每个错误必须指明哪个 node / 哪条边 / 哪个引用错了（学 Conductor）
6. **errors 聚合**：所有 `_check_*` 函数往同一个 ValidationResult 加，最后统一 raise，**不要第一个错就抛**
7. **prompt 加载的文件编码**：utf-8
8. **零反向依赖**：compile/ 不 import run/exec/events

---

## 8. 与 phase 1 的衔接（遗留项落地）

phase 1 review 时明确留给 compile/ 的校验项，本阶段全部落地：

| 遗留项（CURRENT.md 记录）| 本 SPEC 对应 | 状态 |
|---|---|---|
| 顶层 node name 非空唯一 | ① | phase 2 实现（phase 5 扩展含 parallel 组名）|
| entry 存在 | ② | phase 2 实现 |
| 路由引用有效 | ④ | phase 2 实现（phase 5 扩展含 parallel 组 routes）|
| entry 非 parallel 组 | ⑬ | phase 5 单轨化新增 |
| parallel 组结构 | ⑩ | phase 5 单轨化新增 |
| 兜底 route 位置 | ⑪ | phase 5 单轨化新增 |

---

## 9. TASK2 完整描述（可直接给新 session）

> 以下是给实现 session 的完整任务描述。复制它作为新 session 的第一条消息。

---

你是一名资深 Python 工程师，在 **Orca** 项目里实现【阶段 2：compile/ 解析与校验层】。

## 必读文档（按顺序读，不要跳过）
1. `CLAUDE.md` —— 协作规则（必读，"代码质量底线"和"自我 review"）
2. `docs/PLAN.md` —— 整体开发计划（理解 compile/ 在哪）
3. `docs/TASK.md` —— 全局架构决策
4. `docs/specs/phase-1-schema.md` —— phase 1 数据契约（你依赖的 schema）
5. `docs/specs/phase-2-compile.md` —— **你的任务 SPEC**（逐字实现，契约不是建议）
6. `orca/schema/` —— phase 1 已实现的 schema（你要用的数据类型）

## 你的任务
实现 `orca/compile/`：YAML → 校验过的 Workflow。

### 文件清单
- `orca/compile/__init__.py` —— 导出 `load_workflow`, `ConfigurationError`, `ValidationResult`
- `orca/compile/parser.py` —— `load_workflow(path)` + prompt 约定加载
- `orca/compile/validator.py` —— `validate_workflow(wf)` + 8 项校验

### 公共接口（对外极简）
```python
def load_workflow(path: str | Path) -> Workflow: ...
class ConfigurationError(Exception): ...   # 含 errors + warnings
```

### 9 项语义校验（SPEC §4，全部实现；①②④⑥⑦⑧⑨⑩⑪⑬）
1. name 全局唯一 + 非空（node 名 + parallel 组名共享命名空间）
2. entry 存在
3. ⑬ entry 不是 parallel 组（必须 node）
4. routes[].to 引用有效（node 名 / parallel 组名 / $end；node 与组两侧）
5. entry 可达 $end（沿 routes 前向边 + parallel 组展开）
6. Jinja2 浅校验（node 名存在；用 jinja2.meta.find_undeclared_variables）
7. foreach.source 的 node 存在（浅校验）
8. ⑨ profiles capability 校验
9. ⑩ parallel 组结构 + ⑪ 兜底 route 位置

### errors + warnings 模型
- **errors 聚合**：跑完所有校验，收集所有 errors 一起报（决策 1 的 B，不要第一个错就抛）
- **warnings**：死代码/孤立节点/未声明 input/无兜底 route 等非致命提示（决策 2 的 B）
- 有 errors → 抛 ConfigurationError（含所有 errors + warnings）
- 无 errors → load_workflow 返回，warnings 通过 return 值或 CLI 展示

## 强制约束（违反即返工）
1. 对外只暴露 `load_workflow`（+ ConfigurationError/ValidationResult）。校验细节藏在内部。
2. errors 必须聚合（跑完所有校验项再报），不要第一个错就抛
3. 错误信息必须精确（指明哪个 node/边/引用错了）
4. 依赖：`orca.schema` + pydantic + pyyaml + jinja2（jinja2 只用 meta 解析，不 render）
5. 零反向依赖：不 import run/exec/events
6. prompt 约定加载：AgentNode.prompt=None → 从 `agents/<name>.md` 加载（相对 yaml 文件）；文件缺失 → ConfigurationError
7. Jinja2 只做浅校验（node 名存在），不校验 .output.field 字段级（运行时归 run/）
8. routes 回指是合法循环（单指针模型不做静态无环校验，死锁改运行时检测）；兜底 route 必须是最后一条（⑪）

## 产出要求
1. 上述 3 个 compile 文件
2. `tests/compile/test_parser.py` —— 解析 + prompt 加载 + 3 examples 通过
3. `tests/compile/test_validator.py` —— 9 项校验各正/反例 + errors 聚合 + warnings
4. `tests/compile/fixtures/` —— 错误 YAML 样本（每种错误一个 fixture）
5. 3 个 examples 加上配套的 `agents/*.md`（让 nas.yaml 的约定加载能跑通）

## 验收标准（SPEC §6，全部必须通过）
- [ ] `load_workflow("examples/nas.yaml")` 返回 Workflow，所有 agent.prompt 是字符串
- [ ] 3 个 examples 都能 load_workflow 通过
- [ ] 8 项校验各有正/反例测试
- [ ] errors 聚合：多处错的 YAML → ConfigurationError.errors 含所有错误
- [ ] warnings 不阻止返回
- [ ] 全部测试通过（含 phase 1 的 43 个不回归）

## 工作流程（SDD）
1. 先读 6 份必读文档
2. 在 `docs/plans/2026-06-30-phase2-compile.md` 写简短实施计划（文件清单 + 每个校验项的算法 + 测试清单），**不要写代码**
3. 等我确认计划后，再实现
4. 实现完成后，**自我 review**（分发 review agent 检查依赖铁律/职责越界/DRY/fail loud/errors 聚合/测试覆盖）
5. 在 `docs/releases/2026-06-30-phase2-compile.md` 写 release note
6. 更新 `docs/status/CURRENT.md`（标 phase 2 完成，记录遗留给 run/ 的项如字段级运行时校验）+ CHANGELOG

**不要**：实现 run/exec/events —— 后续阶段。
**不要**：自作主张加 SPEC 没有的校验项或公共接口。有疑问先问。
