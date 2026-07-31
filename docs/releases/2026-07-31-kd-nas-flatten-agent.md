# 2026-07-31 — KD-NAS: 新增独立 flatten agent（model-flatten）+ 输入瘦身

## 上下文

KD-NAS 此前要求用户自己准备「契约文件」（`baseline_model_path` 必须暴露 `build_model` +
`DUMMY_INPUT`，KB 里 spt_*.py 也是契约）。而 `nas-agent-pipeline` 的 `pytorch-model-optimizer`
有「展平」能力（SKILL.md Step 1），能把任意模型入口展平成可运行文件。

本次把展平能力**抽成独立 agent**（`model-flatten`），让用户只给任意模型入口，agent 自动
展平成 KD 变体契约；同时砍掉 5 个 advanced inputs（下游 CLI 本就有默认值，不再从 input 注入）。

精确任务（与用户已对齐）：

1. 新建 `workflows/agents/model-flatten/`（folder-agent）：`agent.md` + `SKILL.md` +
   `scripts/validate_contract.py`（脚本硬校验）
2. 嵌入 workflow：`entry: flatten`，新增 flatten 节点（routes → setup）
3. 输入瘦身：砍 `seed` / `kd_artifacts_dir` / `accuracy_baseline_kind` / `latency_tune_budget` /
   `kd_force_rerun`
4. 改 `kd-setup/agent.md`：baseline 从 `inputs.baseline_model_path` → `flatten.output.baseline_contract_path`
5. 边界：不动 gate / train 核心逻辑（只清理被砍 input CLI 引用）；KB 里 spt_*.py 已是契约，不经过 flatten

## 实际做了什么

### 新增文件

- **`workflows/agents/model-flatten/agent.md`** —— folder-agent 入口。强执行指令（BUG-1 抗
  spec-审查）+ output JSON schema 前置 + bash 块标「执行：」。产出 4 字段：
  `baseline_contract_path` / `project_root` / `model_name` / `flat_artifacts_dir`。
- **`workflows/agents/model-flatten/SKILL.md`** —— 6 步工作流：
  Step 1 collect task context → Step 2 flatten local deps → Step 3 加 `__main__` + device
  可移植 → Step 4 推断 `<base_name>` + 写 `<base_name>_flat.py` → Step 5 KNOBS 识别
  （LLM 判断 —— 读结构推断可调维度 + default/min/step/leverage）→ Step 6 脚本硬校验 +
  `flatten-verifier` 子 agent 迭代到 PASS。
  - 只搬 `pytorch-model-optimizer/SKILL.md` 的 Step 1（展平）；**剥掉 Step 2-7**
    （optimize_rules / 分类 / supernet / SearchSpace —— NAS 专用，KD 用不到）。
  - KNOBS 识别写成 LLM 判断步骤（含表格式启发：哪个维度是 knob / default / min / step / leverage）。
  - flatten-verifier 子 agent prompt 框架（scaffold）：检查展平忠实度 + KNOBS 合理性，
    severity tags `[BLOCKER] / [MAJOR] / [MINOR]`，迭代到 PASS（cap 3 轮，未 PASS 不阻塞但标后缀）。
- **`workflows/agents/model-flatten/scripts/validate_contract.py`** —— 确定性硬校验脚本
  （rule 5）。CLI：`--contract <path> [--device auto] [--seed 0]`，emit `KEY: value` 行。
  exit 0 = PASS / exit 2 = FAIL（fail loud）。
  - 校验项：import 成功 + `BUILD_FN == "build_model"` + callable `build_model` + `DUMMY_INPUT.shape`
    非空 list + `KNOBS` 非空 dict（每 knob 含 default/min/step/leverage，step<0，
    leverage∈{high,medium,low}）+ `build_model(**defaults)` 实例化 + forward shape `== DUMMY_INPUT["shape"]`。
  - 校验规则与 `pick_variant._validate_variant` 对齐（同源 CONTRACTS §1），但额外强制
    `KNOBS` 非空（flatten 必须识别至少一个可调维度）+ 跑真 forward（pick_variant 只静态校验）。

### 修改文件

- **`workflows/kd-nas.yaml`**：
  - `entry: setup` → `entry: flatten`
  - 新增 flatten 节点（kind: agent, executor: opencode, model: deepseek/deepseek-v4-flash,
    agent: model-flatten, output_schema 4 字段, routes: to setup）
  - 砍 5 个 advanced inputs：`seed` / `kd_artifacts_dir` / `accuracy_baseline_kind` /
    `latency_tune_budget` / `kd_force_rerun`
  - `baseline_model_path` description 改为「[ask] 用户 PyTorch 模型入口文件（任意 .py / yaml /
    config 入口；flatten agent 会展平成 KD 变体契约 build_model + DUMMY_INPUT + KNOBS，
    不再要求用户自带契约）」
  - setup 节点的 `baseline_latency_ms` description：`(4层)` → `(flatten 产出契约)`
- **`workflows/agents/kd-setup/agent.md`**：
  - 输入段：`baseline_model_path = {{ inputs.baseline_model_path }}` →
    `baseline_contract_path = {{ flatten.output.baseline_contract_path }}` + 新增
    `project_root = {{ flatten.output.project_root }}` 引用
  - step1：`BASELINE` 取 flatten output；`PROJECT_ROOT` 优先取 flatten 推断（python 剥
    ` (low-confidence: ...)` 后缀），fallback 从 BASELINE 向上走；`KD_ARTIFACTS_DIR` 改用
    固定默认 `<repo>/kd-nas-artifacts/`（不再读 inputs.kd_artifacts_dir）
  - step2：保留 build_model + DUMMY_INPUT assert 作 fail-loud 复核（flatten 已保证，但多一道
    防御）；`tune_latency.py` 不再传 `--seed`（用脚本默认 0）
  - step5：`teacher_setup.py` 不再传 `--seed`（用脚本默认 0）
  - step8：`gpu_probe.py` 不再传 `--seed`（用脚本默认 0）
  - frontmatter description：`(4层)` → `(flatten 产出契约)`
- **`workflows/agents/kd-gate/agent.md`**：
  - 输入段：移除 `latency_tune_budget` / `seed` / `kd_force_rerun` 行 + 加「已下沉」说明
  - bash 块：`gate_all.py` 不再传 `--latency_tune_budget` / `--seed` / `--force_rerun`
    （gate_all.py 的 argparse 默认：latency_tune_budget=40 / seed=0 / force_rerun=False）
- **`workflows/agents/kd-train/agent.md`**：
  - 输入段：移除 `accuracy_baseline_kind` / `seed` 行 + 加「已下沉」说明
  - bash 块：`train_pool.py` 不再传 `--accuracy_baseline_kind` / `--seed`
    （train_pool.py 的 argparse 默认：accuracy_baseline_kind="" / seed=0）

### 测试

- **`tests/workflows/test_model_flatten.py`（新文件）** —— 14 个测试：
  - `validate_contract.py` PASS 路径（最小契约 / 多 knob）
  - FAIL 路径（文件不存在 / import 异常 / BUILD_FN 错 / build_model 缺 / DUMMY_INPUT 无 shape /
    空 KNOBS / knob 缺字段 / step>=0 / leverage 非法 / forward shape 不匹配）
  - 纯函数 unit（in-process，不 spawn subprocess）
  - model-flatten/agent.md 结构契约（强执行指令 / 不引上游 node output / output_schema 前置）
  - SKILL.md 仅 Step 1（不含 supernet/SearchSpace/optimize_rules/model_type.json）
  - SKILL.md 含 flatten-verifier prompt 框架（BLOCKER/MAJOR/MINOR）
  - kd-nas.yaml DAG：entry==flatten，flatten agent=model-flatten，routes→setup
  - kd-nas.yaml inputs：baseline_model_path description 含 "flatten"
- **`tests/workflows/test_kd_redesign.py`（修改）**：
  - `test_kd_dag_setup_gate_train` → `test_kd_dag_flatten_setup_gate_train`：4 节点断言
    （flatten → setup → gate → train → $end）+ entry==flatten
  - 新增 `test_kd_dag_flatten_output_schema_contract`：flatten output_schema 4 字段
  - 新增 `test_kd_inputs_slammed_remove_advanced_defaults`：5 个被砍 input 不回潮
  - 新增 `test_kd_setup_agent_md_consumes_flatten_output`：kd-setup 从 flatten.output 取 baseline
  - `test_kd_setup_emits_concurrency_fields`：用 `next(n for n in wf.nodes if n.name=='setup')`
    替代 `wf.nodes[0]`（node[0] 现在是 flatten）
  - `test_kd_agent_md_output_refs_in_schema` 自动适配（动态扫所有 node 的 schema_fields），
    新增 flatten node + kd-setup 引用 flatten.output.* 都被覆盖

## 偏离计划之处（Open Questions）

1. **测试执行未跑**：本会话是 Windows Git Bash 环境，venv 在 WSL（`/home/mozzie/miniconda3/envs/orca/bin/python`），
   从 Windows 端无法直接执行 python。所有 Python 文件已通过**逐行静态审查**（syntax + 逻辑 trace），
   但**未跑 pytest**。主 session commit 前请在 WSL 跑：
   ```bash
   pytest tests/workflows/test_model_flatten.py tests/workflows/test_kd_redesign.py tests/workflows/test_struct_kd_p7.py tests/e2e_redesign/test_workflow_contracts.py -v
   ```
2. **flatten-verifier 子 agent 的 prompt 是 scaffold**（用户已定：「先 scaffold prompt 框架，
   不强求完美实现」）。Step 6b 的 prompt 框架在 SKILL.md 内联，运行时由 flatten agent（opencode）
   通过 task 工具调用。具体子 agent 的注册 / 路由不在本次范围——若需独立 verifier agent.md，
   下一轮再补。
3. **KNOBS 识别是 LLM 判断步骤**（不是确定性脚本）。validate_contract.py 只校验 KNOBS 字段
   齐全 + 类型合法，不校验「是否覆盖了模型所有可调维度」——这是 flatten-verifier 子 agent
   的职责（LLM 判断）。这是 rule 5（deterministic 走代码，judgment 走 LLM）的明确分工。
4. **`accuracy_baseline_kind` 下沉后的 auto-detect**：原 inputs 默认 `""`，train_pool.py
   走 auto 检测 + WARN。下沉后行为不变（脚本默认仍是 `""`）。如果用户需要锁方向（如 nmse），
   现在只能改 train_pool.py argparse 默认或 train/agent.md 常量，不能从 input 注入了——这是
   用户已确认的瘦身决策。
5. **`kd_force_rerun` 下沉后用户失去 input 入口**：force rerun 现在只能改 gate_all.py
   argparse 默认或 gate/agent.md 加常量。如果实际有用户场景需要按 run 重扫，下一轮可考虑
   加 workflow flag（不在本次范围）。
6. **CONTRACTS.md §0/§4 已同步**（v2 → v3）：加 flatten 节点行 + 目录布局加 model-flatten/。
   §1（变体 I/O 契约）未改 —— flatten 产出对齐 §1 字面量（DUMMY_INPUT + BUILD_FN + KNOBS +
   build_model），不引入新字段。
7. **code-reviewer 子 agent 越权**：本次 review 过程中，code-reviewer 自行创建了
   `workflows/agents/kd-train-script/` + `tests/workflows/test_kd_train_script.py`（与本任务无关的
   「训练脚本生成」folder-agent）。这是 reviewer 越权（应只 review，不应创建实现）。已删除这些
   文件保持 diff 聚焦。若主 session 认为该 agent 有价值，可作为独立任务重做。

## 验证结果

- 所有 Python 文件静态审查通过（syntax + 逻辑 trace）。
- 所有 bash 块逐行 trace 通过（变量赋值 / 路径处理 / python -c 引号）。
- yaml 结构通过 Read 逐行核对（flatten 节点 + routes + output_schema 完整）。
- **pytest 未跑**（见 Open Questions 1）。
- **code-reviewer 已分派**（implementation + test coverage 两路并行），所有 🔴 blocker /
  🟡 major / 🟢 minor finding 已闭环（见下方「Reviewer 反馈闭环」段）。

## Reviewer 反馈闭环

### Test coverage reviewer（a5e11995b3e722f77）

- 🔴 **B1** `test_struct_kd_p7.py:423` 断言旧 3 节点 DAG → 改为 4 节点 + entry==flatten 断言（重命名 `test_kd_workflow_has_four_nodes_flatten_first`）。
- 🔴 **B2** `tests/e2e_redesign/contract.py:72` `HARDWARE_INPUT_EXPECTED["kd-nas"]={"device","seed"}` → `{"device"}`（seed 已下沉）+ 同步更新注释。
- 🟡 **M1** 真 shape-mismatch 分支无覆盖 → 加 `test_validate_contract_fail_forward_shape_mismatch`（Conv2d 4D forward OK 但输出 shape 与声明不符）；原测试改名为 `test_validate_contract_fail_forward_exception`（5D Conv2d 触发 RuntimeError）。
- 🟡 **M2** `build_model(**defaults)` 实例化失败路径无覆盖 → 加 `test_validate_contract_fail_build_model_instantiation`（knob 名与构造参数不一致 → TypeError）。
- 🟡 **M3** 非数值 default/min 无覆盖 → 加 `test_validate_contract_fail_non_numeric_default`（`default='3'`）。
- 🟡 **M4** KNOBS[k] 非 dict 无覆盖 → 加 `test_validate_contract_fail_knobs_value_not_dict`（`KNOBS={'n': 'string'}`）。
- 🟡 **M5** dtype 静默默认（违反 fail loud）→ **决策选 (a) fail loud**：validate_contract.py 改为 dtype 必须显式声明 + 必须是合法 torch dtype 名；加 `test_validate_contract_fail_dummy_input_missing_dtype` + `test_validate_contract_fail_bad_dtype_name`。
- 🟢 **m2** `assert "PASS" in text and "iteration" in text.lower() or "迭代" in text` 优先级歧义 → 拆为两条独立 assert。
- 🟢 **m3** flatten 不消费 `inputs.baseline_model_path` 的反向回归 → 加 `test_model_flatten_agent_md_consumes_baseline_model_path_input`。
- 🔵 **n1** 见 m2。

### Implementation reviewer（a6abb145cc8e4e980）

- 🟡 **M1** CONTRACTS.md §0/§4 文档漂移 → 已同步（v2 → v3，加 flatten 节点 + 目录 + DAG 描述）。
- 🟡 **M2** validate_contract.py 不测 `min` 字段能否 forward（与 SKILL.md 宣称不符）→ **决策选 (a) 加 min 自检**：validate_contract.py 加 Step 8 `build_model(**mins)` forward（mins != defaults 时）；加 `test_validate_contract_fail_min_breaks_forward`（min=0 触发 build_model 内部 raise）；SKILL.md Step 5「Step 6 hard-validation will catch invalid min」闭环。
- 🟡 **M3** 测试名误导（已被 test coverage M1 覆盖）。
- 🟡 **M4** agent.md bash 块缺「执行：」标签 → `## 末尾硬校验 执行：validate_contract.py 必 PASS`；SKILL.md Step 6a 加 `执行：` 前缀；测试去掉 `or 兜底` 强制要求 `text.count("执行：") >= 1`。
- 🟡 **M5** `_resolve_device` 不支持 NPU（与 workflow device 顺位不一致）→ validate_contract.py 加 torch_npu 探测分支（auto → cuda → npu → cpu，对齐 `_device.py` 顺位，不 import 跨包）。
- 🟢 **m1** RANK 跨包复制无同步测试 → 加 `test_validate_contract_rank_sync_with_kd_common`（断言 validate_contract.RANK == kd_common.RANK）。
- 🟢 **m2** flat 文件 KNOBS 当前无下游消费者 → SKILL.md Step 5 加「Downstream consumer transparency」段（明确 gate 读 KB 变体，flat 的 KNOBS 是契约格式一致性 + 未来扩展）。
- 🟢 **m4** device 解析失败被错误归因为「实例化失败」→ validate_contract.py 拆三段 try（device 解析 / build_model 实例化 / forward）独立归因。
- 🔵 **n1** `isinstance(True, int)==True` → validate_contract.py 排除 bool（`isinstance(v, bool) or not isinstance(v, (int, float))`）；加 `test_validate_contract_fail_bool_default`。
- 🔵 **n2** agent.md placeholder 未机械化 → 保留（LLM 替换 `<output_dir>` / `<base_name>` 在 SKILL.md Step 4 已明确）。
- 🟢 **m3/m5** reviewer 提及的边角覆盖缺口（`default < min` 语义错配等）→ 当前 validator 与 `pick_variant._validate_variant` 一致（都不查），保持契约对齐，不强加额外约束。

## Commit

待主 session 与用户确认后 commit；CHANGELOG 索引留 commit SHA 待补。
