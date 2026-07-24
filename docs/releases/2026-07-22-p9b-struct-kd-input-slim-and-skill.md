# Release: P9b struct/kd 按 input 三档原则精简 + 切 $ORCA_ARTIFACTS_DIR + setup 哨兵化 + create-workflow-skill 编码三档

**日期**: 2026-07-22
**SPEC**: [`docs/specs/workflow-input-design-principle.md`](../specs/workflow-input-design-principle.md)（三档原则）+ [`docs/specs/agent-ask-user-sentinel.md`](../specs/agent-ask-user-sentinel.md) §3（agent.md 哨兵契约）+ [`docs/releases/2026-07-22-p8-engine-artifacts-dir-and-gc.md`](2026-07-22-p8-engine-artifacts-dir-and-gc.md)（`$ORCA_ARTIFACTS_DIR` 接口）
**范围**: struct/kd 两 workflow yaml + 6 agent.md + create-workflow-skill（SKILL.md + reference + 新增 demo example）+ struct workflow doc 输入表。零 `orca/` 引擎改动、零脚本本体改动、零 quant/nas（P9a 范围）。
**Commit**: `8a1e5f0`

---

## 结论

按 SPEC 三档原则（A [ask] / B [infer] / C [default/advanced]）把 struct（11→9）+ kd（17→9）inputs 精简到位；struct/kd setup 节点切引擎注入的 `$ORCA_ARTIFACTS_DIR`（P8 接口）；补齐 P4b 遗留——struct setup（yaml 内联）+ kd-setup（agent.md）的 Tier B（build_fn / dummy_input）缺失走 ask-user 哨兵（不再「低置信猜测」/「报错」）；把三档原则 + §6 checklist 编进 create-workflow-skill（SKILL.md 新增专节 + reference 新增 §6 + 新 demo example `tier-discipline.yaml`）。

## 改了什么

### 1. struct.yaml + kd-nas.yaml：inputs 精简

**struct 11 → 9**（drop `struct_scripts_dir`、`iterations`）：
- **Tier A [ask] 主 6**：`model_path` / `train_command` / `test_command` / `target_latency_ms` / `accuracy_target` / `max_rounds`
- **Tier C [advanced] 固化 3**：`device` / `latency_provider` / `seed`（默认 0）

**kd 17 → 9**（drop `struct_scripts_dir` / `kd_scripts_dir` / `iterations` / `proxy_dataset_spec` / `eval_dataset` / `teacher_layers` / `short_epochs` / `full_epochs`）：
- **Tier A [ask] 主 6**：`teacher_model_path` / `teacher_train_command` / `test_command` / `target_latency_ms` / `accuracy_gap_db` / `max_rounds`
- **Tier C [advanced] 固化 3**：`device` / `latency_provider` / `seed`

固化去向：
- `struct_scripts_dir` / `kd_scripts_dir` → **setup output_schema 字段**（默认 `workflows/agents/_{struct,kd}_scripts`），下游 `{{ setup.output.{struct,kd}_scripts_dir }}` 取（infer-once + propagate，DRY 单一真相源）。
- `iterations` → **完全移除**（引擎兜底 100；长 run 用 `--max-iter` CLI 覆盖，详 §「Rule 7 surface」a）。
- `teacher_layers=6` / `short_epochs=10` / `full_epochs=50` / `proxy_dataset_spec=""` / `eval_dataset=""` → **固化进 prompt 文本**（setup / candidate_eval / finalize inline）。
- `output_dir` → **切 `$ORCA_ARTIFACTS_DIR`**（见下）。

每个保留 input 的 `description` 以 `[ask]` / `[infer]` / `[default]` / `[advanced]` 标签起头（SPEC §3 标签约定）。

### 2. setup 节点切 $ORCA_ARTIFACTS_DIR（P8 接口）

struct.yaml setup 内联 prompt + kd-setup/agent.md Step 2 的 OUTPUT_DIR 计算：

```bash
# P9b：优先 $ORCA_ARTIFACTS_DIR（P8 引擎注入），回退 llm_artifacts/...
if [ -n "$ORCA_ARTIFACTS_DIR" ]; then
  OUTPUT_DIR="$ORCA_ARTIFACTS_DIR"
  [ "${OUTPUT_DIR: -1}" != "/" ] && OUTPUT_DIR="$OUTPUT_DIR/"  # P8 注入可能不带尾斜杠 → 补齐
else
  OUTPUT_DIR=$(python3 -c "...llm_artifacts/...")  # headless / spike / 非 orca run 回退
fi
```

- env 优先 → 兼容 P8 引擎注入；回退 `llm_artifacts/...` → 兼容 headless / spike / 手跑 / 旧路径。
- **尾斜杠补齐逻辑**：P8 注入的路径可能不带尾斜杠，下游拼接依赖此约定（P2 单一真相源），故显式 `[ "${OUTPUT_DIR: -1}" != "/" ] && OUTPUT_DIR="$OUTPUT_DIR/"`。
- 下游全部读 `{{ setup.output.output_dir }}`，**不**自己拼根——P2 路径字段单一真相源不变。

### 3. struct/kd setup 哨兵化（P4b 遗留收口）

P4b 因「不碰 yaml」硬约束，未做 struct setup（yaml 内联 prompt）+ kd-setup Step 1 的哨兵化。P9b 补齐：

**struct setup（yaml 内联）**：新增「## 缺失必填输入时（严禁造假）— ask-user 哨兵」段。
- Step 3 build_fn：唯一明确 → 用；多个候选 / 推不出 → 返回哨兵 JSON（原「取最像的并在输出注明低置信」是潜在造假风险——多个候选瞎挑会导出错误 ONNX）。
- Step 4 dummy_input：能可靠推断 → 用；无法可靠推断 → 返回哨兵 JSON（原「按族给常见默认 `[1,3,224,224]`」是造假——错则 evaluator FAIL_export fail loud）。
- **范围外**：`project_root` dirname fallback + `family` 浅层判族低置信标注（不阻塞，不造假）；measure_baseline.py 脚本非零退出 = 确定性 fail loud。

**kd-setup agent.md**：原 P4b 哨兵段（仅 Step 6 train.py 结构抽取）**扩展到 Step 1 build_fn / dummy_input**。
- Step 1 build_fn：多个候选 → 哨兵（原「报错让用户指定」）；推不出 dummy_input → 哨兵（原「报错」；不再套常见 `[1,4,48,64,1]` 默认造假）。
- 哨兵段 Tier B 项列表合并 Step 1 + Step 6 全集。
- **范围外**：Step 1 `project_root` dirname fallback（不阻塞，合理 fallback）；Step 3/4/5 脚本非零退出 = 确定性 fail loud。

两处哨兵段均逐字遵循 SPEC §3 四要素：不造假 / 返回哨兵 JSON（2 必填键 `_orca_ask_user` + `_sentinel:"orca_ask_user_v1"`，可选 `options` / `context`）/ 会被恢复（不是重跑）/ fail_loud fallback。

### 4. create-workflow-skill 编码三档原则

**`orca/skills/create-workflow/SKILL.md`**：
- 新增「## input 定义准则（三档原则）」节（位于 H8 之后、「产出过程」之前）：三档分类 + 标签约定表 / Tier A 四子类 / Tier B 典型项 / Tier C 典型项 / 反向判据（否决 KEEP）/ infer-once + propagate 模式 / Tier B 缺失 = ask-user 哨兵（含 JSON schema）/ 生成模板默认动作。
- 「产出过程」step 3（强制自校验）加 SPEC §6 checklist（10 项，`tars validate` 后必跑）。
- `<success_criteria>` 加 input 三档项。

**`orca/skills/create-workflow/reference/orca-workflow-contract.md`**：
- 新增「## 6. input 三档原则与标签约定」节（权威表 + Tier A 子类 + Tier B 典型 + Tier C 固化 + 反向判据 + 哨兵 schema + infer-once 黄金模板 + §6 checklist）。原「## 6. 正确性 cheatsheet」重编号为 §7。
- `InputDef` 描述要求 `description` 以标签起头。

**`orca/skills/create-workflow/examples/tier-discipline.yaml`**（新增 demo）：
- 虚构「模型量化校准」workflow 演示三档合规：3 个 `[ask]` input（model_path / target_hardware / seed）+ Tier B `calib_loader` 下沉 setup output_schema + agent.md 哨兵段示例 + Tier C 固化。
- `tars validate` 通过；用作 skill 生成模板时的「抄作业」参考。

### 5. 下游 agent.md：`inputs.{struct,kd}_scripts_dir` → `setup.output.{struct,kd}_scripts_dir`

- struct-curator / struct-evaluator：`{{ inputs.struct_scripts_dir }}` → `{{ setup.output.struct_scripts_dir }}`（4 + 2 处替换）。
- kd-curator / kd-engineer / kd-hypothesizer：`{{ inputs.{kd,struct}_scripts_dir }}` → `{{ setup.output.{kd,struct}_scripts_dir }}`（4 + 7 + 3 处替换）。
- struct-hypothesizer / struct-engineer / kd-setup：无引用变化（不引用 dropped inputs）。

### 6. docs/workflows/agent-struct-exploration.md：输入表更新

输入表改为三档形态（Tier 标签列），加 Tier B 下沉 + Tier C 固化说明。其余 P5/P6/P7 文档漂移（11 节点 → 5 节点等）留后续 doc-refresh 任务。

## Rule 7 surface（在任务约束下选一条路 + 说明 why）

### a. `iterations` 选择「完全移除」而非「保留 [advanced] 默认 300」

SPEC §6 checklist 明文「iterations 不作 input（自动算）」。引擎 `orca/run/lifecycle.py:158 resolve_max_iter` 优先级：`--max-iter (cli_override) > inputs["iterations"] > wf.inputs["iterations"].default > 100`。

**选择**：完全移除 `iterations`（不保留 [advanced] default 300）。
**Why**：
- 不移除（保留 [advanced] default）→ SPEC §6 不达标；且 `iterations` 不像 `seed`/`device` 是用户决策面，留着只增噪音。
- 移除后引擎兜底 100 → struct/kd 默认 max_rounds=20 × 4 节点/轮 + setup/finalize 2 = 82 visits < 100，安全。
- 长 run（如 max_rounds > 25）用户用 `--max-iter` CLI flag 覆盖（引擎优先级最高，正合 SPEC §6「自动算」+ 用户兜底语义）。
- 不碰引擎（hard constraint），只能选「保留 default」或「完全移除」二选一；前者违反 SPEC，后者合规且有 CLI 兜底。

### b. `device` 不重命名为 SPEC §1 的 `target_hardware`

SPEC §1 Tier A 表用 `target_hardware`（cuda/npu/cpu）；P7 给 struct/kd 选了 `device`（含 `auto` 探测）。

**选择**：不重命名。
**Why**：
- P7 选 `device` 是有意——`auto` 探测（cuda→npu→cpu）是核心功能，`target_hardware` 语义偏向「部署目标」单一值，不及 `device` 表意。
- 重命名要改：yaml input / 6 agent.md / `_device.py::resolve_device` 入参 / `measure_baseline.py` + `export_onnx.py` + `latency_onnxrt.py` + `measure_student.py` + `teacher_setup.py` + `profile_onnx.py` 等 6+ 脚本的 argparse + 行为测试。invasive 程度远超 P9b 范围。
- SPEC §1 是 illustrative 表，不是 strict 命名契约；P5 的 quant workflows 用了 `target_hardware`，P7 的 struct/kd 用了 `device`——两套命名并存，后续如需统一可起单独的 naming-normalization 任务。

### c. struct/kd 最终 9 inputs 而非 SPEC §5 的「目标 6」

SPEC §5 表「agent-struct-exploration 现 8 → 目标 6」「kd-nas 现 14 → 目标 6」写于 P7 之前（不计 P7 后加的 device/latency_provider/seed）。任务 hard constraint 明文「Tier A 保留 [ask]:...target_hardware、seed、latency_provider([advanced])」——这三必须留。

**选择**：9 = 6 主 [ask] + 3 [advanced] 固化默认。
**Why**：
- 6 主 input 是用户决策面（SPEC §5「目标 6」的精神）；3 [advanced] 是 P7 加的工程默认（固化、99% 用户不碰）。
- 「9」与「6」的差距是 SPEC §5 表写于 P7 前的历史漂移，非 SPEC §5 失败。
- 验收按 task hard constraint 的「keep list」为准（任务是最新的、最具体的指令）。

## 验收

- **`tars validate` 0 error**：struct / kd / 新增 demo tier-discipline 全部 `✓ 校验通过`；8 workflow 全量 validate 0 error。
- **inputs 数符合预期**：struct 9（6 主 + 3 advanced）、kd 9（6 主 + 3 advanced），与 task keep list 一致。
- **Jinja2 StrictUndefined**：python 脚本扫描 `{{ inputs.X }}` 全部引用已声明 input（移除的 7 个 kd inputs + 2 个 struct inputs 零残留引用）。
- **既有测试无回归**：`tests/compile/` 127 passed + `tests/workflows/` 61 passed（含 `test_struct_kd_p7.py` 24 passed）= 188 passed；spike 39 passed（哨兵机制层，未触）。
- **三档 checklist 通过**：每保留 input 归 Tier A 四子类之一；Tier B 项有 setup output_schema 承接；Tier C 固化（output_dir → $ORCA_ARTIFACTS_DIR / iterations 移除 / 算法开关固化）；workflow 有 seed 默认 0；agent.md 哨兵段齐。
- **code-reviewer 闭环**：impl + coverage 两 review 并行，0 🔴，共 4 🟡 + 7 🟢，**全部已修或登记**：
  - **🟡 修**：① iterations 移除的 max_rounds>24 fail-mode 文档化（struct + kd 的 `max_rounds.description` 加 iterations 阈值耦合说明 + `--max-iter` CLI 覆盖提示）；② tier-discipline.yaml grep 正则 `\\w+` 双反斜杠 → 单反斜杠（YAML literal block 不处理转义，双反斜杠 grep 失效）；③ tier-discipline.yaml 头注释 seed 错归 [ask] → 改 [advanced]；④ setup output_schema 字段断言扩 (`test_struct_kd_p7.py` 加 `struct_scripts_dir` / `kd_scripts_dir` 守门)。
  - **🟢 修**：① typo `哮兵`→`哨兵` (contract.md)；② typo `MAX_AASK`→`MAX_ASK` (SKILL.md)；③ kd-setup 加确定性块数断言 `assert open(...).count('SignalTransformerBlock(') == 6`（防 LLM 误改结构，原仅 AST 语法校验不够）；④ kd-setup 加「固化前提」注释（baseline 用 SignalTransformerBlock × 4，用户换 model 族时需手改层数硬编码）。
  - **🟡 新增守门测试**：`test_no_jinja_ref_to_undeclared_input`（parametrize 8 production workflows）—— compile validator 对未声明 input 只 warn 不 error（设计如此），故 `load_workflow` 不捕获「移除 input 漏改 agent.md Jinja」；本测试用正则扫 yaml + agent.md 的 `{{ inputs.X }}` 引用并断言 X 在 declared inputs 内，未来同类 slim 改动漏改 Jinja 时当场红（避免到 render 期 StrictUndefined 才崩）。
  - **登记给后续**：① 三档标签前缀机器校验（SPEC §6 checklist 项「description 以标签起头」从人工变 lint）；② tier-discipline.yaml 转 benchmark case（让 skill 生成器对照 expected 产物）；③ 生产 agent.md 哨兵段语法 smoke test（归批 3 headless TARS-SKILL E2E harness）；④ SPEC §1 `seed` Tier A vs `[advanced]` 标签张力澄清（SPEC 改动跨 phase，quant/nas 都用同模式）；⑤ SPEC §1/§5 `target_hardware`↔`device` / 「6」↔「9」命名漂移 SPEC 加注合法化（SPEC 改动）。
- **回归**：`tests/compile/` + `tests/workflows/` = **196 passed**（原 188 + 8 新 parametrized 守门）；`tests/spike_ask_user/` 39 passed 未触；8 production workflows + 4 skill examples 全 `tars validate` 0 error。

## 范围外（登记给后续）

- **生产 agent 哨兵路径零直接 E2E 覆盖**：struct setup（yaml 内联）+ kd-setup Step 1 的「读代码无果 → 返哨兵」分支无测试驱动（P4b 已登记）。归「批 3 统一 headless TARS-SKILL E2E harness」。
- **三档 checklist 的机器校验**：SPEC §6 各项目前靠人肉 / code-reviewer 把关，无 CI 守门（如「每个 input description 以标签起头」「Tier B 必有 setup output 承接」是可 lint 的）。登记给后续 workflow-lint 任务。
- **`device` vs `target_hardware` 命名统一**：quant 用 `target_hardware`、struct/kd 用 `device`，两套并存。后续可起 naming-normalization。
- **docs/workflows/agent-struct-exploration.md 全量刷新**：仅更新了输入表；其余 P5/P6/P7 漂移（11 节点 → 5 节点、chart 字段、KD viz 等）留 doc-refresh 任务。无 kd-nas.md 文档（从未写过）。
- **struct_scripts_dir / kd_scripts_dir 的 `orca install` 路径解析**：固化默认 `workflows/agents/_{struct,kd}_scripts` 是相对路径，cwd=Orca 仓库根时 OK；`~/.orca/workflows/` 安装后需用户在 cwd 或加 symlink（与 P9b 前同样的限制，非回归）。
