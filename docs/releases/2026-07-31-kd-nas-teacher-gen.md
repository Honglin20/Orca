# Release Note: teacher-gen folder-agent（KD-NAS teacher 纯调参派生）

**日期**: 2026-07-31
**范围**: 新建 `workflows/agents/teacher-gen/`（folder-agent）+ 独立测试 `tests/workflows/test_teacher_gen.py`。**未嵌入** workflow yaml（统一阶段；未来嵌入时 setup 改读 `teacher_gen.output.teacher_model_path`）。

## 背景

teacher = baseline 的 `build_model` **调大 cfg**（深度轴 ×3 / 宽度轴 ×2），**不改架构、不改 block 类型**（纯调参派生，选项 1）。teacher 文件 `__main__` 逐字照 `model-flatten/SKILL.md` Step 3 的模板（正确性 + latency），所以 teacher 自带 latency 测试——teacher latency 在 teacher-gen 阶段测掉，不留给 setup。

## 实际产出

### 新建文件

- `workflows/agents/teacher-gen/agent.md` — folder-agent 入口（强执行指令 + output schema 前置 + 双门 bash 块），结构对齐 `model-flatten/agent.md`。
- `workflows/agents/teacher-gen/SKILL.md` — 4-step 派生工作流：
  - Step 1: 读 baseline 契约（DUMMY_INPUT / KNOBS / build_model）
  - Step 2: LLM 识别深度轴 + 宽度轴（KNOBS 名字语义匹配：`block/layer/stage/depth/num_layers` → 深度；`channel/embed_dim/hidden/width/feature` → 宽度）
  - Step 3: 写 teacher wrapper 文件（含完整模板 + `<FILL: ...>` 占位符 + self-review）
  - Step 4: 双重硬校验 + teacher-gen-verifier 子 agent 迭代（仿 flatten-verifier）
- `workflows/agents/teacher-gen/scripts/validate_teacher.py` — **新**：teacher 专属硬校验（确定性脚本，fail loud，exit 0=PASS / exit 2=FAIL）：
  1. DUMMY_INPUT 逐字一致（KD 硬约束——teacher/student 必须同 I/O shape）
  2. 派生轴声明存在（`DEPTH_AXIS` / `WIDTH_AXIS` 字符串常量，可审计）
  3. 深度轴 ×3：`teacher.KNOBS[DEPTH_AXIS].default >= baseline.default × 3`（向上取整容忍）
  4. 宽度轴 ×2：`teacher.KNOBS[WIDTH_AXIS].default >= baseline.default × 2`
  5. 其余 KNOBS 不变（非轴 knob 的 default / min / step / leverage 逐字继承 baseline）
  6. 容量上升（wrapper bug 防护）：teacher 默认实例参数总数 > baseline 默认实例参数总数
- `workflows/agents/teacher-gen/scripts/measure_latency.py` — **复制** `model-flatten/scripts/measure_latency.py`（保持各 folder-agent 独立——`$ORCA_AGENT_RESOURCES` 锚定时 teacher-gen 自带 helper；同步由 `test_measure_latency_copy_sync_with_flatten` 守门，防漂移）。
- `tests/workflows/test_teacher_gen.py` — **新**：35 个测试（独立），覆盖 `validate_teacher.py` PASS/FAIL 矩阵、measure_latency 副本字节对齐、agent.md / SKILL.md 结构契约、E2E（真实 `baseline_model.py` 派生 teacher wrapper + 跑 `__main__` 测 latency）。

### 未修改的文件（范围隔离）

- `workflows/kd-nas.yaml`（不嵌入 workflow）
- `workflows/agents/kd-setup/` / `kd-gate/` / `kd-train/` agent.md（不改核心）
- `workflows/agents/kd-train-script/`（不存在；task 描述里的占位）
- `workflows/agents/model-flatten/`（**只读复用**：`validate_contract.py` 跨 agent 路径调用，`measure_latency.py` 复制不修改原版）
- `workflows/agents/_kd_scripts/teacher_model.py`（现状 teacher，参考契约形态，**未照抄其架构**）
- `docs/status/CURRENT.md`（task 明确范围隔离）

## 关键设计决策

### 1. Wrapper 模式（teacher = baseline 调大 cfg，不拷贝架构代码）

teacher 文件的 `build_model(**cfg)` 通过 `importlib.util.spec_from_file_location`（按绝对路径）加载 baseline 模块，再委托其 `build_model`：

```python
_BASELINE_CONTRACT_PATH = "<abs baseline path>"  # teacher-gen 时渲染

def _load_baseline_module() -> Any:
    p = os.path.abspath(_BASELINE_CONTRACT_PATH)
    # ... baseline 自身的 sibling import 由其顶层自管（如 _demo_blocks）
    spec = importlib.util.spec_from_file_location(...)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_baseline = _load_baseline_module()
_baseline_build_model = _baseline.build_model

def build_model(**cfg) -> "nn.Module":
    return _baseline_build_model(**cfg)  # 纯委托
```

**为什么选 wrapper 而非自包含拷贝**：
- **DRY**：baseline 代码（`SignalProcessingTransformer` 等，可能 100+ 行）不复制——teacher 是"同一模型不同 cfg"语义
- **同步**：baseline 更新/regenerate 时，teacher 自动跟随
- **架构不变保证**：委托而非重写，物理上不可能引入架构改动（verifier 复核 wrapper purity）

**Wrapper 路径加载**（不用 sys.path + `from baseline_flat import ...`）：
- 不污染 sys.modules（unique spec name `_teacher_baseline_<basename>`）
- 不依赖 cwd / sys.path 状态（绝对路径加载，baseline 的 sibling 由 baseline 顶层自管）
- 同一进程多次加载安全（每次创建 fresh module 对象）

### 2. 派生轴声明（`DEPTH_AXIS` / `WIDTH_AXIS` 字符串常量）

teacher 文件顶层暴露 `DEPTH_AXIS` / `WIDTH_AXIS` 字符串常量。**LLM 做语义识别**（KNOBS 名字匹配模式），**脚本做 ×N 数学**（用声明的轴名查 KNOBS，强制 default >= baseline × N）：

| 维度 | LLM 识别（判断） | 脚本校验（确定性） |
|---|---|---|
| 深度轴名 | KNOBS 名字含 `block/layer/stage/depth/num_layers` 等 | `teacher.KNOBS[DEPTH_AXIS].default >= baseline.default × 3` |
| 宽度轴名 | KNOBS 名字含 `channel/embed_dim/hidden/width/feature` 等 | `teacher.KNOBS[WIDTH_AXIS].default >= baseline.default × 2` |
| 轴识别正确性 | 名字语义匹配 | teacher-gen-verifier 子 agent 复核 |

**输出 schema** 含 `depth_axis` / `width_axis`（可审计——能追查 LLM 识别的轴名）。

### 3. 双重硬校验门（确定性 + LLM 复核）

| 校验层 | 工具 | 职责 | 类型 |
|---|---|---|---|
| 契约格式 | `model-flatten/scripts/validate_contract.py`（**复用不复制**，跨 agent 路径调用） | import / BUILD_FN / KNOBS schema / forward shape == DUMMY_INPUT.shape / `build_model(**mins)` forward | 确定性 |
| teacher 专属 | `teacher-gen/scripts/validate_teacher.py`（新） | DUMMY_INPUT 逐字一致 / 深度×3 / 宽度×2 / 其余 KNOBS 不变 / 容量上升 | 确定性 |
| 派生忠实度 | `teacher-gen-verifier` 子 agent（仿 flatten-verifier） | 轴识别语义正确 / wrapper 纯度（无架构拷贝）/ `__main__` 用了用户 latency_provider | LLM 判断 |

**边界清晰**：确定性逻辑（数学、dict 比较、参数计数）走脚本；语义判断（KNOBS 名字是否真表示深度/宽度、teacher 是否真未拷架构）走 LLM verifier（rule 5）。

### 4. KNOBS schema 完全继承 baseline（只动 default）

teacher.KNOBS：
- **键集合**：与 baseline 完全相同（不增不减 knob）
- **每个 knob**：`min` / `step` / `leverage` 逐字继承 baseline；只有 `default` 按轴规则调大（深度 ×3 / 宽度 ×2 / 其余不变）

这样 teacher 走同一套 `validate_contract.py` 无需改校验脚本；下游 gate / setup / train 消费 teacher.KNOBS 时 schema 一致。

### 5. DUMMY_INPUT 硬编码字面量（不用引用）

teacher 文件的 `DUMMY_INPUT = {"shape": [...], "dtype": "float32"}` 是**硬编码字面量**（LLM 从 baseline 逐字复制），不是 `_baseline.DUMMY_INPUT` 引用。原因：
- **避免 mutable 共享**：dict 是 mutable，引用会让 teacher 和 baseline 共享同一 dict 对象，任一方改动影响另一方
- **便于审计**：读 teacher 源码直接看到 I/O shape，无需追溯 baseline
- **validate_teacher.py 强制校验**：`teacher.DUMMY_INPUT == baseline.DUMMY_INPUT`（dict value 比较，KD 硬约束）

### 6. `__main__` 模板逐字照搬 flatten Step 3

teacher 文件的 `__main__` 块**逐字照抄** `model-flatten/SKILL.md` Step 3 模板：
- 正确性：`build_model(**KNOBS.defaults)` → dummy input → forward → 打印 `CORRECTNESS: OK | input=... output=...`
- Latency：`measure_contract_latency(contract_path=__file__, latency_provider=..., ...)` via `$ORCA_AGENT_RESOURCES/scripts/measure_latency.py`
- 失败不伪造：helper 未找到 → `LATENCY_SKIPPED`（不产出 `LATENCY_MS`）

所以 teacher 自带 latency 测试，teacher latency 在 teacher-gen 阶段就测掉（agent.md bash 块解析 `LATENCY_MS:` 填入 output JSON 的 `teacher_latency_ms`）。

### 7. measure_latency 复制（不抽共享）

`teacher-gen/scripts/measure_latency.py` 是 `model-flatten/scripts/measure_latency.py` 的**字节对齐副本**（除 docstring 头部 + CLI description 串的 agent 标识）。

**为什么复制而非抽共享**：
- **folder-agent 独立性**：`$ORCA_AGENT_RESOURCES` 锚定时各 agent 自带 helper，不跨 agent 引用（与 flatten 的 standalone 原则一致）
- **包装/部署单元**：orca package agent 时各 agent 自包含，不依赖 sibling agent 存在
- **同步守门**：`test_measure_latency_copy_sync_with_flatten` 防漂移（类似 flatten 已有的 `RANK` 同步测试）

## 偏离计划 / 取舍

无显著偏离。task spec 字面实现。一处微调：

- **CLI description 串保留 agent 标识差异**：副本的 `--help` description 是 "teacher-gen 契约默认 cfg latency 测量"，不是 "model-flatten..."。原因：`--help` 是用户可见串，保留 teacher-gen 标识更准确（否则用户在 teacher-gen 上下文看到 "model-flatten" 描述会困惑）。同步测试显式 mask 此差异，不破坏字节对齐守门。

## 验证结果

```
tests/workflows/test_teacher_gen.py: 46 passed in 37.79s
tests/workflows/test_model_flatten.py: 52 passed in 41.30s（回归——跨 agent 复用 validate_contract.py 不破坏 flatten）
tests/workflows/test_kd_redesign.py + test_kd_train_script.py + test_receiver_variants.py: 131 passed（更广 KD 回归）
```

E2E 测试用真实 `examples/kd-nas-demo/baseline_model.py`（KNOBS: num_blocks=4, embed_dim=12）作 baseline：
- 派生 teacher wrapper（num_blocks=12=4×3, embed_dim=24=12×2）
- `validate_contract.py` PASS（teacher 是合规 KD 变体契约）
- `validate_teacher.py` PASS（DUMMY_INPUT 一致 + 深度/宽度 ×N 算对 + 容量上升 ratio > 5.0）
- teacher `__main__` 跑出 `CORRECTNESS: OK` + `LATENCY_MS: <正数>` + `LATENCY_SOURCE: provider`（用 demo `latency_provider.py::measure`）

## Code Review 迭代

两路 `code-reviewer` 并行审查（代码质量 + 测试覆盖率），共发现：

- 🔴 **BLOCKER ×1**（测试覆盖率 reviewer）：wrapper 委托失败路径（`validate_teacher.py` L215-218）无回归测试——该路径有专属诊断 reason「wrapper 是否正确委托？」，是架构级失败模式（LLM 忘了 `return _baseline_build_model(**cfg)` / `_BASELINE_CONTRACT_PATH` 错指向）。**已修**：加 `test_validate_teacher_fail_wrapper_instantiation_broken`（typo 漏 `_model` 后缀 → NameError）+ `test_validate_teacher_fail_wrapper_degenerates_to_identity`（退化为 nn.Identity → 容量未上升）。
- 🟡 **MAJOR ×5**（测试覆盖率 reviewer）：空轴 happy path / teacher.default 非数值 / WIDTH_AXIS 错名 / teacher-gen measure_latency CLI 单点测试 / CLI belt-and-suspenders 兜底——**全部已修**（5 个新测试 + 1 个参数化测试覆盖 min/step/leverage 三字段）。
- 🟢 **MINOR ×6**（两位 reviewer）：keyset mismatch 断言过宽（OR 分支误判风险）/ E2E capacity ratio 过松（`>1.0` → `>5.0`）/ measure_latency 副本 `_emit` docstring 仍说 "flatten agent" / `validate_teacher.py` 借用 helpers 同步策略未声明 / agent.md `<base_name>` 命名不一致——**全部已修**。
- 无任何 [BLOCKER] 来自代码质量 reviewer（实现层评估为「高质量、可直接合入」）。

 reviewer 的整体评价：测试覆盖密度高、fail loud 彻底（27 条错误路径全 emit FAIL_REASON + exit 2）、folder-agent standalone 铁律严格遵循、wrapper 模式经 E2E 真实 baseline 验证。

## Open Questions（未覆盖决策，留待用户拍板）

1. **measure_latency 共享位置**：当前 model-flatten 与 teacher-gen 各持一份字节对齐副本（同步测试守门）。未来若再加 folder-agent（如 student-gen），副本数会增长。建议抽到共享位置（如 `workflows/agents/_kd_scripts/measure_latency.py` 或新建 `_kd_shared/`），各 agent `__main__` 走确定性路径加载（或 orca 在 `$ORCA_AGENT_RESOURCES` 之外注入 `$ORCA_SHARED_RESOURCES`）。当前不抽——YAGNI（仅 2 个 agent，同步测试已守门）。

2. **validate_contract.py 的跨 agent 复用 vs 复制**：teacher-gen 通过 `$PROJECT_ROOT/workflows/agents/model-flatten/scripts/validate_contract.py` 跨 agent 路径调用（**复用不复制**，无漂移风险）。代价：teacher-gen 依赖 model-flatten 在仓库内的相对路径（`<repo>/workflows/agents/model-flatten/scripts/`）。若未来 orca 把 agent 打包成独立 unit（每个 agent 自包含），此跨 agent 引用会断。当前可接受（folder-agent 仍在仓库内）；打包时再决定（复制 + 同步测试，或抽共享）。

3. **Teacher 是否嵌入 kd-nas workflow**：当前 teacher-gen 独立（不嵌入 yaml）。若嵌入，建议：workflow 加 `teacher_gen` 节点（输入 baseline_contract_path = `flatten.output.baseline_contract_path`），输出 `teacher_model_path` 给 setup。setup 改读 `teacher_gen.output.teacher_model_path` 作 teacher_model_path 来源（不再硬编码 `_kd_scripts/teacher_model.py`）。本任务范围外，未实现。

4. **多深度轴 / 多宽度轴的场景**：当前 `DEPTH_AXIS` / `WIDTH_AXIS` 各声明一个 knob 名。若 baseline 有多个深度轴（如 `num_blocks` + `num_stages`），当前只识别一个（`leverage=high` 优先）。task spec 字面是"深度×3"（单轴），未要求多轴支持。若实际 baseline 多轴，建议未来扩展为 `DEPTH_AXES: list[str]`（每个轴都 ×3）。当前 YAGNI。

5. **`__main__` 模板去重**：flatten 和 teacher-gen 的 SKILL.md 各自维护一份 `__main__` 模板（latency 测量块）。两份目前一致（teacher-gen 逐字照搬 flatten Step 3）。未来若 flatten 改模板，teacher-gen 不会自动跟随。建议加一个 `test_skill_md_main_template_sync_with_flatten` 守门（类似 measure_latency 同步测试）。当前未加（template 是文档非代码，byte-align 测试 fragile——LLM 渲染时可能微调注释）。如用户要求严格同步，可后续补测试。
