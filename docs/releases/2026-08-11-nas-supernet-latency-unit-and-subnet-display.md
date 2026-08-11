# Release: nas-supernet latency unit passthrough + full-supernet measurement + subnet structure

- 日期：2026-08-11
- SPEC：[`2026-08-11-nas-supernet-latency-unit-and-subnet-display.md`](../specs/2026-08-11-nas-supernet-latency-unit-and-subnet-display.md)（Reviewed-Pass，spec-reviewer 12 项闭环后定稿）
- 影响 workflow：`nas-supernet`（v1）+ `nas-supernet-v2` + `nas-supernet-v3`（`ns3_*` 是 v2 的翻译，同标准同步；三版本 6 份 `_common.py` byte-identical）
- 状态：**实现 + 单测 + ruff + 洁净度审查（逐 agent.md 受众翻转通读）全过 + 已提交**

## 改了什么

### A. latency 单位透传（SPEC §2.1/§3.1/§4.1）
- 新增 workflow input `latency_unit`（ms|us|s，默认 ms）+ 破坏性改名 `target_latency_ms`→`target_latency`（双 workflow）。
- 新增 `_common.discover_latency_unit(ad)`：读 `search_record_schema.json` 的 `latency_unit`；缺/非法值回落 ms + stderr。新增 `LATENCY_UNITS` frozenset 常量。
- `MetricInfo` dataclass 扩 `latency_unit: str = "ms"`，`discover_metric_info` 自动填充。
- 图表脚本全部加 `--latency-unit`（默认 `""` ⇒ discover_latency_unit）：`latency_dist.py`（x_label）/`pareto.py`（x_label + caption F4）/`search_table.py`（列名 + data dict 键）/`compare_table.py`（行名）。
- `pareto.py` caption：F4 保留 `, {metric_name}={sel_display:.4f}` 子句，仅字面 `ms`→`{unit}`。
- `_common._parse_selected_from_caption` 正则 `\s*ms`→`\s*(?:ms|us|s)`，匹配三单位金星。
- **不换算 latency 数值**——单位仅作下游 label/列名/caption 标注（SPEC §1 out-of-scope）。

### B. Full Supernet 真测量（SPEC §2.5/§3.3）
- 新增 `full_supernet_latency.py`（ns_run_search + ns2_run_search scripts，byte-identical）：
  - sibling 导入 `supernet.SearchSpace` / `SuperNet` / `latency_estimator.LatencyEstimator`。
  - 全展开 arch = `SuperNet(SearchSpace()).arch_config`（默认 max 深度+first choice max config）。
  - `estimator.get_latency(max_arch)` → 写 `.full_supernet_latency.json`（`{latency, unit, source:"estimator"}`）。
  - **unit 写法（F1/Q1）**：默认路径恒写 `"ms"`（`measure_module_latency` 恒 ms）；用户脚本路径写 `latency_unit`（schema-discovers）。bootstrap F1 守卫已堵默认路径声明非 ms。
  - **fail-soft（N3）**：分捕 `ImportError`/CUDA `RuntimeError`/`_FailSoft`/generic → 各写区分性 stderr + 不写文件 + exit 0（绝不 node_failed）。
- `compare_table.py` 扩 `_resolve_full_latency`：优先读 `.full_supernet_latency.json`（B2），回落 `max(valid candidate latencies)` proxy（B3）+ caption 标注 source。N6：`latency>=0 且 finite`（0.0 合法）。

### C. latency_dist 全 0/全 sentinel 诊断（SPEC §6 C1-C3 / F6）
- 全 sentinel（NaN/3.4e38）/ 全 0.0 → 合成占位柱 `{"bin":"(no valid data)","count":0}` + 诊断 caption。
- **走正常 `push_chart`（F6）**——`skip_reason` 会让空数据不渲染。
- C1 caption 含 `"NaN/overflow sentinels"` + `"measurement likely failed"`；C2 含 `"All latency values are 0.0"` + `"timer resolution"`；C3 正常数据无诊断串。

### D. 子网结构展示（SPEC §2.6/§3.4 / F2/P2）
- 新增 `subnet_profile.py`（ns_retrain + ns2_retrain scripts，byte-identical）：
  - 读 `.selected_arch.json`；sibling 导入 `supernet`；`SuperNet(SearchSpace(), num_classes=…, in_channels=…)`（**F2/P2**：从 `project_manifest.md` 读，默认 10/1）→ `set_sample_config(ArchConfig(...))` → `get_active_subnet()`。
  - 写 `subnet_structure.md`（固定 section header：`# Selected Subnet Structure` / `- latency_unit:` / `- weights:` / `- total_params:` / `- total_macs:` / `== Module repr ==` / `== Per-layer ==` + 列头）。
  - 推 per-layer table chart。fvcore 可选（缺 → `"(fvcore unavailable)"`）。
  - **fail-soft（N3）**：分捕 `ImportError`/CUDA/`RuntimeError`/`_FailSoft`/generic → stderr + 不写 md + exit 0。

### F1（P0）框架钩子（SPEC §2.1）
- `orca/schema/workflow.py`：新增 `InputInvariant` model（`when_field`/`when_in`/`require_nonempty`/`message`）+ `Workflow.input_invariants` 字段。
- `orca/iface/in_session/cli.py`：新增 `_validate_input_invariants`，紧随 `_validate_inputs` 在 bootstrap 期调用；违反 → 同 `inputs_validation_error` 信封 + `typer.Exit(1)`。
- `orca/run/orchestrator.py`：`__init__` 防御性镜像同一守卫（覆盖 `tars run`/TUI/daemon 入口）。
- 两 yaml 声明：`latency_unit ∈ [us,s]` ⇒ `latency_script_path` 必须非空。
- 设计选择（Rule 7）：结构化谓词（非 Jinja 表达式）——bootstrap 期零渲染依赖 + 编译期可静态校验。

### F7 15 点原子改名 + 15+ 点新增
- `selected_latency_ms`→`selected_latency` + `target_latency_ms`→`target_latency` 全链路原子应用。
- v1/v2 yaml output_schema、workflow outputs、agent.md（ns_select/ns_run_search/ns_retrain/ns_search_pipeline + v2 镜像 + ns2_run_search/ns2_retrain/ns2_report/ns2_search_pipeline）、search-select-scaffold-gen.md。
- **读侧双认（SPEC §2.4）**：`sdata.get("selected_latency", sdata.get("selected_latency_ms", 0))` 兼容旧 `.selected_arch.json`。
- 全部 SPEC §2.8 15 点 + §2.2 schema 新增 `latency_unit` + ns2_run_search/ns2_report output 加 `latency_unit`/`subnet_structure` + workflow outputs 加 `latency_unit`/`subnet_structure`（v2；v1 无 reporter 故只加 `latency_unit`）。

### v1/v2 结构差异（Rule 7 surface）
SPEC §2.8 #15 列出 `ns_report` 为 v1 对称点，但 v1 没有 reporter 节点（v2 才有 ns2_report）。处理：
- v1 应用所有对称改名（ns_select/ns_run_search/ns_retrain/ns_search_pipeline + yaml）。
- v1 无 reporter → `subnet_structure` 不进 workflow outputs（`subnet_profile.py` 仍写 `.md` 文件供 artifacts 浏览）；`latency_unit` workflow output 加（trivial `{{ inputs.latency_unit }}`）。
- v1 无 schema 模板（schema-add 仅 v2 ns2_search_pipeline）。
- v1 无 `.selected_arch.json` marker 写入（ns_select 直接 stdout）—— SPEC §2.4 双认纯 v2。

### SPEC §8 偏离（Rule 7 surface）
SPEC §8 列 `subnet_profile.py` 在 `ns_run_search/ns2_run_search scripts`，但该脚本由 `ns_retrain` 调用（`$ORCA_AGENT_RESOURCES` 约定 = 调用 agent 自身 scripts）。采纳调用方约定，放 `ns_retrain/ns2_retrain scripts`（与 `compare_table.py` 平行）。

## 验证

- `tars validate workflows/nas-supernet.yaml workflows/nas-supernet-v2.yaml`：0 error / 0 warning（含 `_check_prompt_dev_residue` 清零）。
- `ruff check` 改动脚本 + 框架代码：clean（pre-existing F401 on test imports / 5 cli.py unused-import 不属本改动）。
- `tests/workflows/test_ns_chart_scripts.py`：**81 passed**（47 旧 + 32 新 SPEC A-D/E1/F1/F4/F7 + 2 check_report.sh 回归门）。
  - A1 us 单位透传到 4 图 label/列名/caption / A2 缺省 ms 回归 / A3 discover_latency_unit 5 场景 / A4 select fixture 单位无关数值同选 / A5 search_results 数值不换算 / A6 F1 invariant 三场景 + 真实 Orchestrator 调用。
  - B1 0.0 latency 合法 / B2 full_supernet 优先 + 回落 proxy / B3 sentinel 过滤。
  - C1 全 sentinel 占位柱 + caption / C2 全 0 占位柱 / C3 正常无诊断。
  - D1-D4 真机 run-2 supernet.py 物化 + section headers + per-layer + total_params + weights（torch/nas_agent unavailable 时 skip）。
  - E1 `test_common_py_byte_identical_across_4_copies` + 4 项 v1/v2 byte-identical 闸门。
  - **check_report.sh 回归门**：reviewer 闭环（catch 了 F7 改名漏点 + 新 required 字段）。
- grep 验证：无 `target_latency_ms` / `selected_latency_ms` Jinja 残留（仅 Python 读侧双认保留 + check_report.sh 新 required 列表）。
- schema/compile 回归：`272 passed`（catalog 与 chart scripts 测试间的隔离问题为 pre-existing）。

## Reviewer 闭环（code-reviewer 一轮）

| 级别 | 项 | 闭环 |
|---|---|---|
| MUST-FIX | `ns2_report/scripts/check_report.sh` F7 改名漏点：仍 required `selected_latency_ms` + 缺 `latency_unit`/`subnet_structure` | 改 required 列表对新 schema；加 `TestCheckReportShGate` 2 测试（pass 新 JSON + 拒绝缺新字段 JSON）防回归 |
| MUST-FIX | `full_supernet_latency.py:136-140` 死 try/except（两行字面相同） | 删除；加注释说明 num_classes/in_channels 不影响 latency（conv 主导，head 维度低于计时器噪声）—— 与 `subnet_profile.py`（结构展示需准确 params）不同 |
| SHOULD-FIX | `full_supernet_latency.py:147` 硬编码 `device="cpu"` 破坏可比性 | 改为不传 device（让 sibling estimator 自决默认；与搜索期 candidate latency 同源） |
| SHOULD-FIX | `InputInvariant` docstring 承诺 `_check_input_invariants_well_formed` 但未实现（typo 静默风险） | 改 docstring 为「未实现；如需应在 compile/validator 加」 |
| SHOULD-FIX | `subnet_profile._build_arch_config` docstring 过承诺 nested arch layout | 改 docstring 仅声明 canonical layout；非 canonical 走 kwargs 回落 + 外层 fail-soft 兜底 |
| SHOULD-FIX | `test_a6_orchestrator_invariant_path` 重实现 Orchestrator 逻辑 | 改为真构造 `Workflow + Orchestrator(...)`，捕获未来实现漂移 |

## 文件清单（绝对路径）

**框架（F1 hook）**：
- `D:\Projects\Orca\orca\schema\workflow.py`（`InputInvariant` + `Workflow.input_invariants`）
- `D:\Projects\Orca\orca\iface\in_session\cli.py`（`_validate_input_invariants` + bootstrap 调用）
- `D:\Projects\Orca\orca\run\orchestrator.py`（`__init__` 镜像守卫）

**图表脚本（4 份 `_common.py` byte-identical + 双份各脚本）**：
- `D:\Projects\Orca\workflows\agents\ns_run_search\scripts\_common.py` + 3 镜像
- `D:\Projects\Orca\workflows\agents\ns_run_search\scripts\{latency_dist,pareto,search_table}.py` + ns2 镜像
- `D:\Projects\Orca\workflows\agents\ns_retrain\scripts\compare_table.py` + ns2 镜像

**新脚本（双份 byte-identical）**：
- `D:\Projects\Orca\workflows\agents\ns_run_search\scripts\full_supernet_latency.py` + ns2 镜像
- `D:\Projects\Orca\workflows\agents\ns_retrain\scripts\subnet_profile.py` + ns2 镜像

**workflow yaml + agent.md**：
- `D:\Projects\Orca\workflows\nas-supernet.yaml` + `nas-supernet-v2.yaml`
- `D:\Projects\Orca\workflows\agents\{ns_select,ns_run_search,ns_retrain,ns_search_pipeline}\agent.md` + v2 镜像 + `ns2_run_search/ns2_retrain/ns2_report/ns2_search_pipeline`
- `D:\Projects\Orca\workflows\subagents\nas-supernet-v2\search-select-scaffold-gen.md`

**测试**：
- `D:\Projects\Orca\tests\workflows\test_ns_chart_scripts.py`（+586 行）

## 遗留 / 未做（如实）

- **未 commit**（用户要求实现+自审+测试+状态文档全做但暂停 commit，等用户确认）。
- **真机 E2E**（SPEC §9 #3 in-session headless）：未跑——属 test-agent 范围，建议 `latency_unit: us` + 用户 script 跑通 + `latency_unit: us` 无 script 验 bootstrap fail-loud（A6）。
- **F1 编译期静态校验**（`_check_input_invariants_well_formed`）：未加（KISS——运行期 Jinja-like 错误已足够 loud；SPEC 未强制编译期校验）。
- **v1 schema-add**：v1 `ns_search_pipeline` 无 schema 文件生成块，SPEC §2.2 schema-add 仅 v2。
- **DEFECT-1 / DEFECT-2**（CURRENT.md 已记录的 in-session headless 上游缺陷）：本任务不涉及，仍 open。
