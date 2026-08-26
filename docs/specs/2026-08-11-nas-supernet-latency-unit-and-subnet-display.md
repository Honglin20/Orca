# SPEC：nas-supernet / nas-supernet-v2 latency 单位透传 + full-supernet 真测量 + 子网结构展示

- 日期：2026-08-11
- 状态：**Reviewed — Pass（spec-reviewer 对抗审查闭环后定稿）**
- 适用 workflow：`nas-supernet`（v1，`ns_*`）、`nas-supernet-v2`（`ns2_*`）——同源，契约对称生效，实现双份同步。
- 前置设计计划：`C:\Users\mozzie\.claude\plans\optimized-weaving-flute.md`
- 审查报告：spec-reviewer 1 轮对抗（evaluator 33 次工具调用核真实代码），闭环 12 个有效发现（9 真问题 / 2 权衡 / 1 无效反驳）。

> 本文件是**契约**，不是建议。coder 逐字实现字段名/CLI/语义，不自作主张加字段。有歧义先问。

---

## Review 闭环摘要（v1→v2 SPEC 变更）

| ID | 级别 | 闭环动作 |
|---|---|---|
| F1 | P0 | 新增 bootstrap 不变量：`latency_unit ∈ {us,s}` ⇒ `latency_script_path` 必须非空，否则 fail-loud（默认 estimator 恒 ms，非 ms 声明必错标） |
| F5 | P1 | **放弃** schema key 改名 `latency_ms_field→latency_field`（消除一处破坏性改名 + §2.2 自相矛盾）；schema 仅新增 `latency_unit` |
| F3 | P1 | `discover_latency_unit` + 受影响改动的 `_common.py` 是**四份**（ns_run_search/ns2_run_search/ns_retrain/ns2_retrain），非两份 |
| F4 | P1 | `_parse_selected_from_caption` 正则 `\s*ms`→`\s*(?:ms\|us\|s)`；caption 保留 `, {metric}={val:.4f}` 子句，仅字面 ms→{unit} |
| F6 | P1 | latency_dist 全 sentinel/全 0：合成占位柱 `{"bin":"(no valid data)","count":0}` + 诊断 caption，走正常 push_chart（**非** skip_reason） |
| F7 | P1 | `selected_latency_ms→selected_latency` + `target_latency_ms→target_latency` 是 **15 点原子 checklist**（见 §2.8），禁半应用态 |
| N1 | P1 | A4 验收不可单测（select 运行期生成）→ 测试用 fixture/scaffold 重生机制 |
| N2 | P2 | E1 措辞改"v1/v2 byte-identical（含 label），改后保持；四份 `_common.py` md5 相等" |
| N6 | P2 | B1 `latency>0` 误杀合法全 0 → 改 `latency>=0 且 finite` |
| N3 | 权衡 | full_supernet_latency/subnet_profile 是 supernet-materialization 脚本（非纯图表脚本）；fail-soft 分捕 ImportError/CUDA/RuntimeError |
| N5 | 权衡 | 四份 `_common.py` DRY 味道 → 加 `test_common_py_byte_identical_across_4_copies` CI 闸门；长期提升另立 case |
| F2 | 无效 | retrain ckpt `load_state_dict` key 同构（`extract_subnet`=`get_active_subnet`）；仅留 P2：subnet_profile 透传 num_classes/in_channels |

---

## 0. 背景与根因（事实）

1. **同源**：v2 是 v1 重构。两套图表脚本（`latency_dist.py`/`search_table.py`/`pareto.py`/`_common.py`/`compare_table.py`）逐字节相同。
2. **单位锁死 ms**：契约级假设（无代码 `×1000`）。**默认 estimator 路径恒返回 ms**（`latency_estimator.py:36-51` → `nas_agent/latency/pytorch_latency_utils.py`：CUDA `elapsed_time`/CPU `perf_counter×1000`，源码已核）；**只有** `latency_script_path` 用户 onnx 脚本路径才可能返回非 ms。全链路把任意返回值当 ms 标注 ⇒ 用户脚本返 µs 时错标。
3. **Full Supernet latency = 代理**：`compare_table.py:62-64` `full_latency=max(lats)`，非真测量；`extract_numeric_values` 不过滤 sentinel。
4. **latency 全 0/全空静默**：`latency_dist.py` 无诊断。
5. **子网结构未展示**：reporter 只输出 `selected_arch` dict，未 materialize `nn.Module`。supernet 已具备 `set_sample_config`/`get_active_subnet`/`elastic_num_params`。

---

## 1. 范围

### In scope
- A. latency 单位端到端可声明（新增 `latency_unit` 输入，默认 ms，不换算数值）。
- B. Full Supernet latency 用搜索同款 estimator 真测量，替换 `max(候选)` 代理。
- C. `latency_dist.py` 全 0/全 sentinel 时 fail-loud 诊断（占位柱 + caption）。
- D. 选定子网结构展示（`str(subnet)` module repr + 逐层结构化表）。
- A–D 对 v1/v2 双份同步（四份 `_common.py`）。

### Out of scope（显式禁止）
- **不改 `nas_agent`**（外部包，只读）。
- **不做 latency 数值单位换算**（不 ×1000/÷1000）。单位只用于**标注**；数值原样存储，单位作独立元数据。
- **不为默认 PyTorch 路径声明非 ms 单位**——默认路径恒 ms，声明非 ms 即错标（F1 因此 bootstrap 拒绝）。
- **不**在 `search_results.jsonl` 每行加 `latency_unit`（run 级常量写 schema）。
- 不改搜索框架（NASProblem/worker/SearchLogger）objective 写入契约。
- 不重构 v1/v2 合并（仅同步本次契约）。
- 不消化四份 `_common.py` 重复（仅加 CI 闸门防漂移；提取到 `workflows/_shared/` 另立 case）。

---

## 2. 数据契约（逐字实现）

### 2.1 workflow inputs（`workflows/nas-supernet.yaml` + `nas-supernet-v2.yaml`）

| 字段 | 类型 | 必填 | 默认 | 语义 |
|---|---|---|---|---|
| `target_latency` | number | 是（`[ask]`） | — | 选架构时延靶值。**单位由 `latency_unit` 决定**。**破坏性改名**自 `target_latency_ms`。 |
| `latency_unit` | string enum `[ms, us, s]` | 否（`[advanced]`） | `"ms"` | latency 测量值声明单位。全链路 label/列名/caption/比较按此单位。**不改数值**。 |

**🔴 F1 bootstrap 不变量（P0）**：`latency_unit ∈ {us, s}` 时 `latency_script_path` **必须**非空，否则 workflow bootstrap **fail-loud**（结构化错误：声明非 ms 单位但无用户测度脚本——默认 estimator 恒 ms，非 ms 声明必错标）。`latency_unit=ms` 不受约束（默认路径即 ms）。此不变量在 workflow bootstrap（in-session 入口 / `orca <wf> --inputs` 解析）处校验。

### 2.2 `search_record_schema.json`（`ns2_search_pipeline`/`ns_search_pipeline` agent.md 模板生成）

**F5 定稿**：key 名**不动**，仅新增一字段：

| 键 | 状态 | 值 |
|---|---|---|
| `latency_ms_field` | **不变**（保留原名，消除破坏性改名） | `"latency"` |
| `latency_unit` | **新增** | `"{{ inputs.latency_unit }}"`（默认 `"ms"`） |

- 兼容回落（消费侧）：`schema.get("latency_unit", "ms")`；`schema["latency_ms_field"]` 直读。
- 旧 run schema 无 `latency_unit` ⇒ ms，不崩。

### 2.3 `search_results.jsonl`

**不变**。每行 `objs.latency` = `LatencyEstimator.get_latency()` 原样返回值。单位元数据只在 schema。

### 2.4 `.selected_arch.json`

| 字段 | 旧 | 新 | 语义 |
|---|---|---|---|
| `selected_latency` | `selected_latency_ms` | `selected_latency` | latency 数值（单位 = `latency_unit`） |
| `latency_unit` | — | 新增 | 从 schema 透传，默认 `"ms"` |
| `selected_arch` / `selected_acc` / `pareto_size` / `select_reason` | — | 不变 | |

兼容：读侧双认 `selected_latency` 与 `selected_latency_ms`（旧 marker 回落）。

### 2.5 `.full_supernet_latency.json`（**新**，`full_supernet_latency.py` 写）

```json
{"latency": 12.34, "unit": "ms", "source": "estimator"}
```
| 字段 | 类型 | 语义 |
|---|---|---|
| `latency` | number | 全展开超网真实 latency（单位 = `unit`）。 |
| `unit` | string | **默认路径恒 `"ms"`；用户脚本路径 = `latency_unit`**。不得无条件写 `latency_unit`（F1/Q1）。 |
| `source` | string enum `[estimator, proxy]` | `estimator`=搜索同款 LatencyEstimator 真测；`proxy`=回落 max(候选)（sentinel 已滤）。 |

文件缺失 ⇒ compare_table 回落 proxy + 标注。

### 2.6 `subnet_structure.md`（**新**，`subnet_profile.py` 写）

固定 section header（逐字，便于下游/测试解析）：
```
# Selected Subnet Structure
- latency_unit: <unit>
- weights: <retrain ckpt 路径 | search-time (no retrain ckpt)>
- total_params: <int>
- total_macs: <int|"(fvcore unavailable)">
== Module repr ==
<str(subnet)>
== Per-layer ==
layer_name | type | params | out_shape
<逐 named_modules 行>
```
- `out_shape` 捕失败 → `?`；`total_macs` 无 fvcore → `"(fvcore unavailable)"`。均不崩。

### 2.7 workflow outputs（两 yaml `outputs:` 段）

| 输出键 | 来源 |
|---|---|
| `latency_unit` | `"{{ inputs.latency_unit }}"` |
| `selected_latency` | reporter output `selected_latency`（原 `selected_latency_ms` 改名） |
| `subnet_structure` | reporter output `subnet_structure`（`subnet_structure.md` 相对路径或空串，新增） |
| `selected_latency_ms` | **移除** |

### 2.8 破坏性改名原子 checklist（F7，**15 点，禁半应用态**）

`selected_latency_ms→selected_latency` + `target_latency_ms→target_latency`（F5 已删 schema key 改名，故 15 点非 16）。任一 Jinja `*_latency_ms` 残留 → `StrictUndefined` 崩，故必须原子全改：

1. `select_architecture.py`（生成模板）输出键 + CLI `--target-latency-ms`→`--target-latency`
2. `.selected_arch.json` 字段（`selected_latency_ms`→`selected_latency`）
3. `ns2_run_search/agent.md:291`（select CLI + Jinja `inputs.target_latency_ms`）
4. `ns2_run_search/agent.md:297` bash sentinel JSON 字面量 `selected_latency_ms`
5. `ns2_run_search/agent.md:373,397` Step3 python `select_defaults` + emit dict
6. `nas-supernet-v2.yaml:210,228`（ns2_run_search output_schema required + property）
7. `nas-supernet-v2.yaml:285,292`（ns2_report output_schema）
8. `nas-supernet-v2.yaml:318`（workflow outputs）
9. `ns2_retrain/agent.md` compare 调用处（CLI `--selected-latency` + Jinja `ns2_run_search.output.selected_latency`）
10. `ns2_report/agent.md`（python 局部变量 + `sdata.get` + emit + 注释）
11. `ns2_search_pipeline/agent.md`（stdout 契约文档 + emit 文档 + reference）
12. `pareto.py`（`--selected-latency-ms`→`--selected-latency`）
13. `compare_table.py`（同上）
14. `search-select-scaffold-gen.md`（生成器指令）
15. **v1 对称点**：`ns_select`/`ns_run_search`/`ns_retrain`/`ns_report`/`ns_search_pipeline` agent.md + `nas-supernet.yaml`

---

## 3. CLI 契约（逐字实现）

### 3.1 `select_architecture.py`（生成）
- `--target-latency`（float，默认 None；改名自 `--target-latency-ms`）
- `--latency-unit`（string，默认 `"ms"`）
- `--search-results`（Path，required）/ `--schema`（Path，default 同目录 `search_record_schema.json`）
- 行为：靶值 `latency <= target` 同单位数值直比。结果 JSON 加 `latency_unit`。schema 无 `latency_unit` ⇒ `"ms"`。

### 3.2 图表脚本（canonical，四份 `_common.py` + 双份各脚本）
所有脚本新增 `--latency-unit`（默认 `""` ⇒ `discover_latency_unit(ad)`）。

| 脚本 | 单位影响 |
|---|---|
| `latency_dist.py` | `x_label=f"Latency bin ({unit})"`；caption 带单位 |
| `pareto.py` | `x_label=f"Latency ({unit}, lower is better)"`；caption **保留 `, {metric_name}={sel_display:.4f}` 子句**，仅字面 `ms`→`{unit}`：`f"Selected arch: latency={sel_lat:.2f}{unit}, {info.name}={sel_display:.4f}."`；`--selected-latency-ms`→`--selected-latency` |
| `search_table.py` | 列名 `latency_ms` → `f"latency_{unit}"`；**data dict 键与 `columns` 列表同改** |
| `compare_table.py` | 行名 `Latency (ms)` → `f"Latency ({unit})"`；`--selected-latency-ms`→`--selected-latency` |

### 3.3 `full_supernet_latency.py`（**新**，双份：ns_run_search/ns2_run_search scripts）
- `--artifacts-dir`（required）/ `--latency-unit`（默认 `""` ⇒ schema 回落）
- 行为：sibling 导入 `latency_estimator.LatencyEstimator` + `supernet`；全展开 arch = `SuperNet(SearchSpace()).arch_config`；从 `search_config.yaml` 读 `latency_cfg`（warmup/repetitions/batch_size）；`estimator.get_latency(max_arch)` → 写 `.full_supernet_latency.json`。
- **`unit` 写法（F1/Q1）**：默认路径（无 user script）恒写 `"ms"`；用户脚本路径写 `latency_unit`。判定：`latency_script_path` 非空 ⇒ 用 `latency_unit`，否则 `"ms"`。
- **fail-soft（N3）**：分捕 `ImportError`/CUDA error/`RuntimeError` → 各写区分性 stderr + 不写文件 + exit 0。

### 3.4 `subnet_profile.py`（**新**，双份）
- `--artifacts-dir` / `--selected-arch-json`（默认 `<ad>/.selected_arch.json`）/ `--latency-unit`（默认 `""` ⇒ schema 回落）
- 行为：读 `selected_arch`；sibling 导入 `supernet`；`SuperNet(SearchSpace(), num_classes=…, in_channels=…)` → `set_sample_config(ArchConfig(**selected_arch))` → `get_active_subnet()`；可选加载 `runs/retrain/*.pth`（mtime 最新）`load_state_dict`；写 `subnet_structure.md`（§2.6）；推 `table` chart。
- **F2/P2**：`num_classes`/`in_channels` 从 manifest/schema 读，默认 10/1（非 MNIST 项目须 manifest 提供，否则结构错）。
- **fail-soft（N3）**：分捕 `ImportError`/CUDA/`RuntimeError` + materialize 失败 → stderr + 不写 + exit 0。

---

## 4. 函数契约

### 4.1 `_common.discover_latency_unit(ad) -> str`（**新，四份 `_common.py`**）
- 读 `<ad>/search_record_schema.json` 的 `latency_unit`；缺失/非 `{ms,us,s}` → `"ms"`（非法值 + stderr）。纯函数。

### 4.2 `_common.MetricInfo` 扩 `latency_unit: str`（默认 `"ms"`）；`discover_metric_info` 填充。frozen dataclass。

### 4.3 `_common._parse_selected_from_caption` 正则（F4）
- 现 `\s*ms`（`_common.py:627`）→ `\s*(?:ms|us|s)`，匹配三单位 caption 的 selected 金星。

### 4.4 复用（不重写）
`push_chart`/`read_jsonl`/`discover_metric_info`/`extract_numeric_values`/`flatten_record`/`find_field`/`safe_float`/`run_inspect_supernet`；`SuperNet.arch_config`/`get_active_subnet`/`elastic_num_params`/`set_sample_config`；`nas_agent.latency.measure_module_latency`。

---

## 5. 失败路径

| 场景 | 行为 |
|---|---|
| `latency_unit∈{us,s}` + 空 `latency_script_path` | **bootstrap fail-loud**（F1，非零退出 + 结构化错误） |
| schema 缺 `latency_unit` | 消费方回落 ms，不崩 |
| `.selected_arch.json` 用旧键 `selected_latency_ms` | 读侧双认，不崩 |
| `latency_dist` 全 sentinel 滤空 | **合成占位柱** `{"bin":"(no valid data)","count":0}` + 诊断 caption，走正常 push_chart（**非** skip_reason，F6）；exit 0 |
| `latency_dist` 全 0.0 | 占位柱 + 诊断 caption；exit 0 |
| `full_supernet_latency.py` torch/CUDA/测量失败 | 不写文件 + stderr；exit 0；compare 回落 proxy |
| `subnet_profile.py` materialize 失败 | 不写 md + stderr；exit 0；reporter `subnet_structure=""` |
| `target_latency` 缺失 | workflow bootstrap fail loud（required=true） |
| select 无候选 | 现有 falsy sentinel（加 `latency_unit` 字段） |

**铁律**：新脚本全 fail-soft（exit 0 + stderr），**绝不** node_failed。

---

## 6. 验收标准（可验证）

### A. 单位透传
- A1. `latency_unit=us`：`latency_dist`/`pareto`/`search_table`/`compare_table` label/列名含 `us`。
- A2. 缺省 → 全部 `ms`（回归）。
- A3. `discover_latency_unit`：schema `us`→`"us"`；无 schema/无键→`"ms"`；非法 `"xyz"`→`"ms"`+stderr。
- A4. select `--target-latency 500 --latency-unit us` + schema `latency_unit=us`：选架构结果与旧 `--target-latency-ms 500`（ms 数据）同数值下一致。**测试机制（N1）**：测试内手写符合新契约的 fixture `select_architecture.py`，或经 `search-select-scaffold-gen.md` 重生一份。
- A5. `search_results.jsonl` 每行 `objs.latency` 数值**未换算**（A1 前后字节级相同）。
- **A6（F1）**. `latency_unit=us` + 空 `latency_script_path` → bootstrap 非零退出 + 结构化错误。

### B. Full Supernet 真测量
- B1. `full_supernet_latency.py` 成功 → `.full_supernet_latency.json` 存在，`source="estimator"`，`latency>=0 且 finite`（N6：`==0.0` 合法，caption 注 "estimator returned 0.0"）；默认路径 `unit=="ms"`，用户脚本路径 `unit==latency_unit`（F1/Q1）。
- B2. compare_table：`.full_supernet_latency.json` 在 → 用其值；缺 → max(候选) + sentinel 滤 + caption 标 proxy。
- B3. `latency=3.4e38` 混入候选 → proxy 不显示 3.4e38。
- B4. torch 缺失 → 文件缺 + stderr（fail-soft，N3）。

### C. latency 全 0 诊断
- C1. 全 sentinel → 占位柱图存在（status 为 `pushed`/`rendered_static`，**非** `skipped`）+ caption 含 `"NaN/overflow sentinels"`+`"measurement likely failed"`（F6）。
- C2. 全 0.0 → 占位柱 + caption 含 `"All latency values are 0.0"`+`"timer resolution"`。
- C3. 正常数据 → 无诊断串。

### D. 子网结构
- D1. `subnet_structure.md` 含 `== Module repr ==` + `== Per-layer ==`。
- D2. repr 段含 `str(subnet)`（≥1 layer 类名）。
- D3. 逐层表 ≥1 行；`total_params` 正整数。
- D4. retrain ckpt 在 → `weights: <ckpt>`；缺 → `weights: search-time (no retrain ckpt)`。
- D5. reporter `subnet_structure` 非空（成功时）。

### E. 契约/双份
- E1. v1/v2 对应脚本**当前 byte-identical（含 label）**，改后保持；四份 `_common.py` md5 相等；CI 闸门 `test_common_py_byte_identical_across_4_copies`（N2/N5）。
- E2. `tars validate` 两 workflow 通过，`_check_prompt_dev_residue` warning 清零。
- E3. `ruff check` 干净。
- E4（F7）. 改名 15 点原子全应用，无半应用态（grep 无残留 `selected_latency_ms`/`target_latency_ms` Jinja）。

---

## 7. 向后兼容

| 旧情 | 新行为 |
|---|---|
| 调用方传 `target_latency_ms` | **破坏**（改名）——SPEC 接受 |
| 旧 schema 无 `latency_unit` | 回落 ms，可重渲旧 run 图 |
| 旧 `.selected_arch.json` 用 `selected_latency_ms` | 读侧双认 |
| `latency_unit` 缺省 | = ms，现有行为不变 |
| `latency_unit=us` 无 script | **新约束**：bootstrap 拒绝（F1） |

---

## 8. 受影响文件（representative）

**canonical 图表脚本（四份 `_common.py` + 双份各脚本）**：
- `workflows/agents/{ns_run_search,ns2_run_search,ns_retrain,ns2_retrain}/scripts/_common.py`（F3：四份）
- `workflows/agents/{ns_run_search,ns2_run_search}/scripts/{latency_dist,pareto,search_table}.py`
- `workflows/agents/{ns_run_search,ns2_run_search}/scripts/{full_supernet_latency,subnet_profile}.py`（新）
- `workflows/agents/{ns_retrain,ns2_retrain}/scripts/compare_table.py`

**workflow yaml**：`workflows/{nas-supernet,nas-supernet-v2}.yaml`（含 F1 bootstrap 校验逻辑落点说明）

**agent.md**：`{ns_search_pipeline,ns2_search_pipeline}`（schema 模板，**仅加 `latency_unit`**）/ `{ns_run_search,ns2_run_search}`（3 图 `--latency-unit`；select CLI；调 `full_supernet_latency.py`）/ `ns_select`（v1 select）/ `{ns_retrain,ns2_retrain}`（compare `--latency-unit` + 调 `subnet_profile.py`）/ `{ns_report,ns2_report}`（output 加 `latency_unit`+`subnet_structure`，python 多点）/ `subagents/nas-supernet-v2/{search-select-scaffold-gen,search-core-gen}.md`

**生成引用文档**：`references/workflows/measure_latency_script_generation.md`（用户脚本契约：单位由 `latency_unit` 声明）/ `references/supernet_workflow_examples/latency_estimator.py`（docstring）

**测试**：`tests/workflows/test_ns_chart_scripts.py`（扩 A–D）+ 新 `test_common_py_byte_identical_across_4_copies`（N5）

---

## 9. 验证

1. `ruff check` + `tars validate`（两 workflow，dev-residue 清零）。
2. `tests/workflows/test_ns_chart_scripts.py` 扩 A1–A6/B1–B4/C1–C3/D1–D5 + `test_common_py_byte_identical_across_4_copies` CI 闸门。
3. in-session headless E2E（CLAUDE.md 模式）：`latency_unit: us` + 用户 script 跑通 → 4 图 label=us、compare=真测量、`subnet_structure.md` 完整、reporter output 含新字段；`latency_unit: us` 无 script → bootstrap fail-loud（A6）。
4. 回归：`latency_unit` 缺省（ms）→ 现有行为不变。

---

## 10. Review 开放问题结论

- **Q1（full_supernet_latency 可行性 + unit 写法）**：两路径可行；**默认路径写 `unit="ms"`，用户脚本路径写 `latency_unit`**；`source="estimator"` 两路径通用；bootstrap 强制非 ms unit 须 script（F1）。
- **Q2（fvcore）**：**可选**（与 §2.6/§3.4 一致，必装硬拉 torchvision，可选匹配 fail-soft）。
- **Q3（破坏性改名）**：**有条件可接受**——F1 闭环 + §2.8 落 15 点 checklist + F5 选 Option A（弃 schema key 改名）。最终仅 `target_latency_ms→target_latency` + `selected_latency_ms→selected_latency` 两处。
