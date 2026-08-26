# Release: InputDef.enum —— workflow 输入枚举值校验（fail-loud at bootstrap）

- 日期：2026-08-11
- SPEC：[`docs/specs/2026-08-11-inputdef-enum.md`](../specs/2026-08-11-inputdef-enum.md)（spec-reviewer 12 项闭环：B1/B2/B7 必须 SPEC 修订 ✓、B4 采纳 ✓、B3/B5/B6 文档修订 ✓、R1/R2/R4/R5 驳回）
- 关联：超 [`2026-08-11-nas-supernet-latency-unit-and-subnet-display.md`](./2026-08-11-nas-supernet-latency-unit-and-subnet-display.md)（latency_unit 输入引入后，笔误值 `"MS"` 过 bootstrap 烧到 ns2_run_search emit 才 node_failed——本 release 在 bootstrap 即抓）。

---

## 做了什么

引入 `InputDef.enum` 字段（允许值集合），让 workflow 作者声明单字段值域约束。bootstrap 期对值不在 enum 的字段 fail-loud（CLI + Orchestrator 双入口），错误信息含字段名 + 非法值 + 合法集合。首个应用：三版本 `nas-supernet.yaml` 的 `latency_unit` 输入加 `enum: [ms, us, s]`，让笔误值 `"MS"` 在 bootstrap 即被截。

### 契约层（§3.1）

`orca/schema/workflow.py` `InputDef` 新增 `enum: list[Any] | None = None` + 3 个加载期 validator：

1. **`_enum_well_formed`（field_validator）**：空 list 拒（`enum: []` 是配置错）+ 非标量值拒（dict / nested-list 做 `in` 比较语义模糊，B4 采纳）。
2. **`_default_in_enum`（model_validator, mode="after"）**：`default ∈ enum` 自洽（B2 修订：`default is not None` 守卫——隐式 default=None 不触发 `None not in enum` 误报）。这是 in-session 模式 default 安全的唯一网（CLI bootstrap 不构造 Orchestrator）。

### 透出层（§3.2）

`orca/compile/catalog.py`：`inputs_schema_list`（CLI 消费）+ `_inputs_to_schema`（MCP describe_workflow 消费）均透出 `enum` 键（D2：始终透出，None 透出 None，消费者 `.get("enum")` 单态）。

### CLI 校验（§3.3 / B7 重构）

`orca/iface/in_session/cli.py` `_validate_inputs` 重构为 5 步线性：
1. 无 `ftype`（旧 loose-typed wf）→ 整字段 pass-through
2. optional-tag 检测（`[default]` / `[advanced]`）
3. required check（显式 type + 非标签字段缺省 → fail loud）
4. **enum check**（值不在 enum → fail loud；独立于 type 白名单）
5. type check（仅 `ftype ∈ _TYPE_MAP`；自定义 type → pass-through）

**B7 关键**：原 `check_fn is None: continue` 会连 enum check 一起跳过（自定义 type + enum 字段在 CLI 路径静默不查 enum）。重构后 enum check 上移到步 4（type check 之前，D7），`check_fn is None` 收窄为「只 skip type check」——自定义 type + enum 也查 enum。

### Orchestrator 镜像（§3.4）

`orca/run/orchestrator.py` `__init__`：紧随 default-fill 循环、在 invariant 镜像之前，新增 enum 遍历——`if idef.enum and merged_inputs.get(name) not in idef.enum: raise ValueError(...)`。覆盖 tars run / TUI / web 全新 bootstrap 入口；resume 经 `from_tape→_bare_instance` bypass `__init__`（复用 tape 已校验 inputs）；daemon.next 经 `advance_step` 推进、不构造 Orchestrator（靠原始 CLI bootstrap 守——同 F1 invariant 镜像模式，非新覆盖空洞）。

错误形态与 CLI 一致（字段名 + 非法值 + 合法集合），`ValueError` 直透（同 required-missing / invariant）。单字段错先于跨字段错（错误层次从窄到宽）。

### 应用落地（§3.6）

`workflows/nas-supernet.yaml` + `nas-supernet-v2.yaml` + `nas-supernet-v3.yaml` 的 `inputs.latency_unit` 加 `enum: [ms, us, s]`（位于 `type: string` 与 `description:` 之间）。`default: "ms" ∈ enum` ✓。F1 invariant（`latency_unit∈{us,s} ⇒ latency_script_path`）正交保留——enum 管值合法，invariant 管跨字段依赖，两者叠加。

---

## 测试覆盖（§6 矩阵）

| 层 | 文件 | 用例 |
|---|---|---|
| schema | `tests/schema/test_workflow.py` | 8 例（AC1 + AC8 + B2 + B4 + backcompat） |
| cli 直调 | `tests/iface/in_session/test_in_session_cli.py` | 10 例（AC2 + B7 + D7 + §7 explicit-null） |
| cli bootstrap | 同上 | `test_bootstrap_inputs_enum_violation_fails_loud`（AC3 真入口信封） |
| orchestrator | `tests/run/test_orchestrator.py` | 6 例（AC4 + AC5 + AC6 + §3.4 enum-before-invariant + §7 explicit-null） |
| yaml 应用 | `tests/workflows/test_nas_supernet_enum_gate.py`（新建） | schema 解析层 + yaml 字串层双契（AC7） |

新增 25 个测试全过；`tars validate workflows/nas-supernet{,-v2,-v3}.yaml` 三份均 0 error / 0 warning。

测试质量（Rule 9）：
- 用真笔误 `"MS"` 验大小写敏感（D4 核心动机），非任意字符串。
- `test_validate_inputs_enum_custom_type_still_checked` 直接验 B7 重构意图（关闭原 `check_fn is None: continue` 的静默绕过漏洞）。
- `test_orchestrator_enum_before_invariant_ordering` 锁 SPEC §3.4 决策（单字段错先于跨字段错）——wf 同时声明 enum + invariant，断言 enum 错先抛。
- `test_nas_supernet_enum_gate.py` 拒绝 grep-only 闸门，走 `Workflow(**yaml.safe_load(...))` 真解析路径（schema 3 validator 全跑）+ yaml 字串层双契。

---

## 偏差 / 取舍

- **B3（D8 defer）**：核心不实现 compile 期 enum 类型一致校验（如 `type: int, enum: [1,2,"three"]` 报 `"three"` 非 int）。spec-reviewer 裁决：若日后实现须先把 `_TYPE_MAP` 从 `orca/iface/in_session/cli.py` 提升到 `orca/compile/type_predicates.py`（禁 compile→iface 反向 import，违铁律）。核心交付不含 D8；§3.1 标量校验已拒绝最明显的作者错（dict/nested-list 混入 enum）。
- **R1（bool/int 隐式相等）驳回**：`enum:[0,1] + 输入 true → true in [0,1]` 为 True 是 edge case，latency_unit（string）不受影响，YAGNI 不额外守卫。
- **schema 层错误信息不含字段名**：`_default_in_enum` 错误消息只含 `default=... enum=...`，无 `input <name>`（InputDef 模型内不可见 dict key）。pydantic ValidationError.loc 带 `inputs.<name>` 前缀，定位可达——code-reviewer 🟢#3 评估后建议保持现状（成本/收益不高）。

---

## Commit SHAs

- `7b120ee` — feat(schema): InputDef.enum —— workflow 输入枚举值校验（framework + tests）
- `131b294` — feat(nas-supernet): latency_unit 输入加 enum: [ms, us, s]（v1+v2+v3 yaml 应用）

---

## 验证结果

- `tests/schema/test_workflow.py`：18 enum-related passed
- `tests/iface/in_session/test_in_session_cli.py`：50 enum + validate_inputs passed
- `tests/run/test_orchestrator.py`：enum-related passed
- `tests/workflows/test_nas_supernet_enum_gate.py`：2 passed
- `tars validate workflows/nas-supernet{,-v2,-v3}.yaml`：3 份均 ✓ 校验通过
- `ruff check` 改动文件：零新增 error（基线 pre-existing F401/F821 不在改动行）
- code-reviewer 审查：0 🔴 / 0 🟡 / 3 🟢（其中 2 🟢 已补为契约锁定测试，1 🟢 保持现状有理由）
