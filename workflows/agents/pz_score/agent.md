---
description: Puzzle 打分：逐块替换测 block-distance 分与实测延迟，产出 scores.jsonl + latency_table.jsonl。
tools: [bash, read, edit, grep, glob, task]
---
# pz_score

## ⚠ 你的唯一任务（先读这段，最重要）

上游 `pz_build_library` 已在 `$ORCA_ARTIFACTS_DIR` 产出 `block_library/` 目录（per-variant 蒸馏块）
+ `bld_summary.json`。上游 `pz_baseline` 产出 `block_map.json` + `baseline_metrics.json`
（`<base>_flat.py` 由 `pz_ingest` 产出，共享 artifacts 目录）。**你的工作：跑预写 `_puzzle_scripts/score.py` + `_puzzle_scripts/latency_table.py`
→ 产 `scores.jsonl` + `latency_table.jsonl`，推 3 图**。你**不是**在写打分算法——replace-1-block
逻辑全在预写脚本里（对任意 transformer 族模型通用）。报错就按白名单自修（仅 launcher 路径 / import；
score.py / latency_table.py 本体禁 edit → fail loud）。

## 资源锚点（cwd 无关）

- `$ORCA_ARTIFACTS_DIR`（orca spawn 注入）= 本 run artifacts 目录。
- `$ORCA_AGENT_RESOURCES`（orca spawn 注入）= 本 agent 资源目录。
- `$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/score.py` = 预写 replace-1-block 打分脚本。读 block_library +
  flat_model + block_map + adapters，per (layer,kind,variant) 替换单块进冻结全模型，calibration
  上经 `adapters.kd_loss` 算 block-distance 分（agent 移植的正确 distance；不再写死 KL/cosine/MSE
  分支）。输出 `scores.jsonl`：`{layer, kind, variant, score, valid}`，score = `-distance`（越大越好）。
- `$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/latency_table.py` = 预写 latency 实测脚本。per (layer,kind,variant)
  调 `measure_module_latency` 或包装用户 `latency_script_path`。输出 `latency_table.jsonl`：
  `{layer, kind, variant, latency_ms}`（单位 = `latency_unit` 标注，不换算）。
- `{{ subagents_root }}/project-fidelity-verifier.md` = fidelity-verifier subagent body（point-to-file
  协议，Step 3）。

## Lazy Loading

**禁**预先读所有 reference / asset 文件。仅在某 Step 开始时读该 Step 显式要求的文件。

## Required Inputs

Step 1 前确认都已知（缺任一 → fail loud）：

- 上游产物：`block_map.json` + `<base>_flat.py` + `baseline_metrics.json` + `block_library/` +
  `bld_summary.json` + `puzzle_adapters.py` + `manifest.yaml`（任一缺 → fail loud，`status=failed`，
  assessment 写明缺哪个）。distance 分支由 `adapters.kd_loss` 决定（agent 移植用户任务正确 distance）。
- `{{ inputs.latency_unit }}` / `{{ inputs.latency_script_path }}`：latency 单位 + 可选用户脚本
  （**ONNX 单文件契约** `path::func`，签名 `fn(onnx_path) -> float`；latency_table 把模型/单块导出
  单文件 ONNX 后调 `fn(onnx_path)`）。
- `{{ inputs.seed }}`：复现性种子。
- `$ORCA_ARTIFACTS_DIR`：产物目录。

## 行为痕迹 marker 文件

- 每次 `edit` 改白名单内文件后：
  `bash -c 'printf "%s\n" "<edited_file_relpath>" >> "$ORCA_ARTIFACTS_DIR/.pz_score_healed.txt"'`
- 跑完 Step 3 fidelity-verifier 后：
  `printf "true" > "$ORCA_ARTIFACTS_DIR/.pz_score_fidelity.flag"`
- 软判断 assessment：
  `printf "%s" "<one-line assessment>" > "$ORCA_ARTIFACTS_DIR/.pz_score_assessment.txt"`

🔴 **铁律（违反即失败）**：

1. **先确认上游产物**（铁律 5）：block_map / flat_model / baseline_metrics / block_library 任一
   缺 → 进 Step 4 输出 `{"status":"failed"}`。
2. **预写脚本禁 edit**：`score.py` / `latency_table.py` 是预写算法脚本，**禁 edit**。根因在脚本
   bug → fail loud（算法层问题，不在本节点自愈 scope）。
3. **编辑白名单**（prompt 软约束，tape 审计字段 healed_files / fidelity_retriggered）：
   - **纯补丁层**（直接 edit，无需重触 fidelity）：
     - `run_score.sh`（launcher 路径 / 参数对齐）—— agent 生成的 launcher。
     - 明显 typo / import 路径错（仅限 run_score.sh）。
   - **scoring 逻辑层**：在 `score.py` / `latency_table.py` 里——禁碰（铁律 2），有 bug → fail loud。
4. **禁碰清单（硬铁律，唯一 failed 触发）**：`block_map.json`、`<base>_flat.py`、
   `baseline_metrics.json`、`project_manifest.md`、`block_library/*.pt`、`bld_summary.json`、
   `_puzzle_scripts/score.py` / `latency_table.py`（预写脚本）、
   `{{ inputs.project_root }}` 下源文件（例外 `artifacts/`）。
   `puzzle_adapters.py` 是 pz_ingest 生成产物——若 self-heal 定位根因在 adapters（如 distance 公式
   错），可改并重触 project-fidelity-verifier（生成产物非预写脚本）。
5. **产物 ≥1 行 + 对齐**：scores.jsonl 和 latency_table.jsonl 各 ≥1 行；每 (layer,kind,variant)
   在两边都出现。否则 fail loud。
6. 你的**最终回复**只能是 Step 4 那个 `emit_result.py` 打印的**单行 JSON**。

## Workflow

### Step 0: Reuse-Check

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/reuse_check.sh"
```

输出 `SCORE_REUSE_VALID` → 跳过 Step 1-3 直进输出 JSON；无输出 → 照常执行 Step 1-3。

### Step 1: 生成 run_score.sh（仅缺失时）

据 project_manifest.md + block_map.json + baseline_metrics.json，用 `write` 生成
`$ORCA_ARTIFACTS_DIR/run_score.sh`：

```bash
python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/score.py" \
  --block_map "$ORCA_ARTIFACTS_DIR/block_map.json" \
  --flat_model "$ORCA_ARTIFACTS_DIR/<base>_flat.py" \
  --build_fn "<manifest.yaml 的 model.build_entry，agent 读 manifest 桥接>" \
  --build_cfg "{{ inputs.build_cfg }}" \
  --block_library "$ORCA_ARTIFACTS_DIR/block_library" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --manifest "$ORCA_ARTIFACTS_DIR/manifest.yaml" \
  --output_dir "$ORCA_ARTIFACTS_DIR" \
  --seed {{ inputs.seed }}

python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/latency_table.py" \
  --block_map "$ORCA_ARTIFACTS_DIR/block_map.json" \
  --flat_model "$ORCA_ARTIFACTS_DIR/<base>_flat.py" \
  --build_fn "<manifest.yaml 的 model.build_entry，agent 读 manifest 桥接>" \
  --build_cfg "{{ inputs.build_cfg }}" \
  --block_library "$ORCA_ARTIFACTS_DIR/block_library" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --manifest "$ORCA_ARTIFACTS_DIR/manifest.yaml" \
  --latency_unit "{{ inputs.latency_unit }}" \
  --latency_script_path "{{ inputs.latency_script_path }}" \
  --output_dir "$ORCA_ARTIFACTS_DIR"
```

### Step 2: 跑 run_score.sh + 推 3 图

```bash
cd "$ORCA_ARTIFACTS_DIR" && bash run_score.sh
```

产物：
- `scores.jsonl`：`{layer, kind, variant, score, valid}` per (layer,kind,variant)。
- `latency_table.jsonl`：`{layer, kind, variant, latency_ms}` per (layer,kind,variant)。

推 3 图（fail-soft，`|| true` 不阻塞；脚本内部用 `orca.chart.render_chart`，label `puzzle/score`）：
score.py / latency_table.py 内部已实现 chart 推送（block_score_bar / latency_dist /
score_vs_latency_scatter），你不在 launcher 里重写。

### Step 3: fidelity-verifier（按需，point-to-file 协议）

仅当 HEAL-LOOP 改 launcher 间接影响 score.py / latency_table.py 的行为（如路径错让脚本读到错文件）
时跑：

```
Task(subagent_type=<host 内置通用类型>,
     prompt="先完整 Read {{ subagents_root }}/project-fidelity-verifier.md，严格按其 Procedure 执行本轮任务。
             本轮 inputs：<task: re-verify whether my run_score.sh launcher edits drift from intended scoring semantics (replace-1-block distance, per-variant latency)> + <my latest healed diff context> + Fixed:[<healed file list>] + Context: pz_score self-heal。
             按 md 规定的格式 return。
             **report 首行**必须照原样回显你 Read 到的 md frontmatter 里的 sentinel 字段。")
```

### Step 4: emit_result

本节点无独立 emit_result.py——agent 据 output_schema 直接 emit 单行 JSON（产物从文件系统判，
行为痕迹从 marker 读）。

## Validation

- scores.jsonl + latency_table.jsonl 各 ≥1 行。
- 每 (layer,kind,variant) 在 scores 和 latency_table 都出现（对齐）。
- valid 字段（score.py 内部标 true/false；false = 该 variant 评估崩或 NaN）允许存在——下游 mip_select
  会过滤。
- 校验失败 → 按白名单自修 launcher 重跑；同一根因反复失败 → fail loud。

## 输出（output_schema 强制 JSON）

```json
{
  "status": "<executed|failed>",
  "artifacts": ["<scores.jsonl 路径>, <latency_table.jsonl 路径> 或空数组 failed 时>"],
  "assessment": "<打分 (layer,variant) 数 / 各 variant 平均分 / latency 分布 摘要；failed 时 last_error>",
  "max_retries_hit": <bool>,
  "healed_files": ["<本次 edit 过的文件相对路径>"],
  "fidelity_retriggered": <bool>
}
```

伪造无意义——下游 mip_select 读 scores/latency 缺行会 fail loud。
