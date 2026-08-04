# Release Note — validate_contract 去同形 I/O 过约束（P4）

**日期**: 2026-08-05
**类型**: fix（架构层修正，非补丁）
**Commit**: 见 `git log`（commit message: `fix(kd-nas): validate_contract 去同形 I/O 过约束——支持分类器族（P4）`）

---

## 背景（test-agent 真实证据）

P0/P1 已修并验证生效（`ORCA_WORKFLOWS_ROOT` env 注入 + fail-loud 空 JSON 禁止）。test-agent
真实跑 `examples/mnist_kd/`（MNIST 分类器，输入 `[1,1,28,28]` 输出 `[1,10]`）在 **flatten 节点 fail**：

```
validate_contract.py:166-170  check 7:
  if actual_shape != expected_shape:  # expected = DUMMY_INPUT.shape
      return {"ok": False, "reason": f"forward shape [1,10] != DUMMY_INPUT.shape [1,1,28,28]"}
```

CONTRACTS §1 此前为 receiver / 自编码器族设计（I/O 同形）。但 **KD 本质只要求 teacher/student
共享输出 shape**（KD loss 比对两者输出），不要求 output == input。分类器族（输出 = num_classes）
是合法 KD 目标。

---

## 修复（架构层，向后兼容）

### 1. `validate_contract.py` check 7 重写（核心）

旧逻辑：`forward output shape == DUMMY_INPUT.shape` —— 把输入 shape 当输出契约，过约束。

新逻辑三层：
- **7a**：forward 一次捕获实测输出 shape。
- **7b**：determinism 自检——同输入再 forward 一次，输出 shape 须一致（防 stateful / 非确定性模型）。
- **7c**：`OUTPUT_SHAPE` 可选声明校验——契约声明了就校验 forward 实测 == 声明；不声明则用 forward 实测保底。

不再要求 `output == DUMMY_INPUT.shape`。同形族（receiver / 自编码器）output==input 自然满足；
分类器族 output≠input 合法。

### 2. CONTRACTS §1 加可选 `OUTPUT_SHAPE` 字段

```python
DUMMY_INPUT = {"shape": [...], "dtype": "float32"}   # 输入 shape
OUTPUT_SHAPE = [<非空 int list>]                       # 可选：声明 forward 输出 shape
BUILD_FN = "build_model"
KNOBS = {...}
```

I/O 契约改述——输入由 `DUMMY_INPUT` 定义；输出由 model forward 决定（或可选 `OUTPUT_SHAPE` 声明），
**不要求同形**；teacher/student 须共享输出 shape（KD 要求，不变）。

### 3. flatten 链路产出 OUTPUT_SHAPE

- `model-flatten/SKILL.md` Step 4 模板加 `OUTPUT_SHAPE` 字段。
- `__main__` 块 emit `OUTPUT_SHAPE_OBSERVED: <json>`（解析友好）。
- `model-flatten/agent.md` bash 块 parse `OUTPUT_SHAPE_OBSERVED` + **programmatic insertion**
  （在 DUMMY_INPUT 行后插 `OUTPUT_SHAPE = <observed>`，已声明则跳过）+ 二次 validate 闭环
  （forward 实测 vs 声明不一致 → exit 2 → LLM 修）。deterministic 优先（rule 5）。

### 4. teacher-gen / gen-student 防御性 OUTPUT_SHAPE 检查

- `validate_teacher.py` check 1b：双方都声明 OUTPUT_SHAPE 时校验逐字一致（catches teacher/student
  输出 shape 漂移早，KD loss 跑炸之前）。
- `gen-student/agent.md` step 3 deterministic check 扩展：双声明时校验 student.OUTPUT_SHAPE ==
  baseline.OUTPUT_SHAPE。

任一方未声明 → 跳过（由 validate_contract forward 实测保底）。

### 5. 下游同形假设审计（避免打地鼠）

grep 全 pipeline 下游对 `DUMMY_INPUT.shape` 的使用：

| 消费者 | 用法 | 假设 output==input？ |
|---|---|---|
| `tune_latency.py` | 构造 dummy **输入**张量 | 否 |
| `export_onnx.py` | 构造 dummy **输入**张量 | 否 |
| `gpu_probe.py` | 构造 dummy **输入** batch | 否 |
| `teacher_setup.py` | 构造 dummy **输入** + proxy dataset | 否 |
| `measure_latency.py` | 构造 dummy **输入**张量 | 否 |
| KD loss path（`kd/compose.py`） | 比对 teacher/student 输出（runtime fail-loud） | 否（不依赖 DUMMY_INPUT） |

**结论**：无其它「output==input」硬假设。修复局部即可，无需下游改动。

### 6. 测试

新增 / 重写 8 个测试用例（`test_model_flatten.py` + `test_teacher_gen.py`）：

| 测试 | 期望 | 覆盖意图 |
|---|---|---|
| `test_validate_contract_pass_minimal`（更新） | PASS + SHAPE_MATCH: true | 同形族 + 声明 OUTPUT_SHAPE |
| `test_validate_contract_pass_classifier_no_output_shape` | PASS + SHAPE_MATCH: not_declared | **P4 核心：分类器族不声明也合法** |
| `test_validate_contract_pass_classifier_with_output_shape` | PASS + SHAPE_MATCH: true | 分类器族 + 声明 OUTPUT_SHAPE=[1,10] |
| `test_validate_contract_fail_output_shape_mismatch`（重写） | FAIL | 声明 OUTPUT_SHAPE 与 forward 实测不符 |
| `test_validate_contract_fail_output_shape_non_deterministic` | FAIL | stateful 模型，二次 forward shape 不一致 |
| `test_validate_contract_fail_output_shape_wrong_type` | FAIL | OUTPUT_SHAPE='not-a-list' |
| `test_validate_contract_fail_output_shape_empty_list` | FAIL | OUTPUT_SHAPE=[] |
| `test_validate_teacher_fail_output_shape_mismatch` | FAIL | teacher 双声明不一致 |
| `test_validate_teacher_pass_output_shape_both_declared_matching` | PASS | teacher 分类器族派生共享输出 shape |
| `test_validate_teacher_pass_output_shape_neither_declared` | PASS | 同形族双不声明（向后兼容） |

---

## 验收

1. **validate_contract 对 MNIST 分类器契约（output≠input）PASS**；对同形契约仍 PASS。✅（测试覆盖）
2. **全 pipeline 同形假设审计无残留**。✅（上表）
3. **单测 + 守门测试全绿零回归**。✅（274 passed + receiver_variants 64 passed + gate 绿）
4. **headless e2e**：test-agent 自跑 `tars run workflows/kd-nas.yaml` 对 `examples/mnist_kd/`
   —— 留待 test-agent 验证（本次修复范围：单测层闭环 + 守门绿；真实 e2e 由 user 复跑）。

---

## 守门

- `test_kd_prompt_no_source_narrative.py` PASS —— agent.md / SKILL.md / CONTRACTS.md 改动
  零源叙事 / 零决策标签（grep `P4` / `去同形约束` 在 prompt 文件中零命中）。

---

## code-reviewer 闭环

一轮 code-reviewer 闭环：**0 BLOCKER / 0 MAJOR / 3 MINOR**。
- MINOR 1（已修）：bash 正则依赖 SKILL 模板单行 DUMMY_INPUT 约定 → 加注释说明 + 二次 validate 兜底。
- MINOR 2（已修）：`OUTPUT_SHAPE = []` 独立测试 → 已加 `test_validate_contract_fail_output_shape_empty_list`。
- MINOR 3（保留，reviewer 同意）：teacher-validator (Python) 与 gen-student bash 内嵌 Python 的
  双声明检查重复 3 行——gen-student 须 standalone（禁 import _kd_scripts），不宜抽取。

---

## 铁律符合

- ✅ 向后兼容：receiver 族（kd-nas-demo 同形）必须仍全过；既有 KD suite 测试零回归。
- ✅ 不破坏 Phase 1-5 + P0/P1 已落地（引擎 / 接口 / 拍平 / 去源化 / env 注入）。
- ✅ fail loud / 单向依赖（schema → exec → iface；validator 是生产者，teacher/student 是消费者）。
- ✅ commit immediately（细粒度）。

---

## 涉及文件（绝对路径）

- `/mnt/d/Projects/Orca/workflows/agents/model-flatten/scripts/validate_contract.py`
- `/mnt/d/Projects/Orca/workflows/agents/teacher-gen/scripts/validate_teacher.py`
- `/mnt/d/Projects/Orca/workflows/agents/_kd_scripts/CONTRACTS.md`
- `/mnt/d/Projects/Orca/workflows/agents/model-flatten/SKILL.md`
- `/mnt/d/Projects/Orca/workflows/agents/model-flatten/agent.md`
- `/mnt/d/Projects/Orca/workflows/agents/gen-student/agent.md`
- `/mnt/d/Projects/Orca/tests/workflows/test_model_flatten.py`
- `/mnt/d/Projects/Orca/tests/workflows/test_teacher_gen.py`
