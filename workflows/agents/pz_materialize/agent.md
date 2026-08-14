---
description: Puzzle 装配：把选中架构内联成自包含的 optimized_flat 模型文件并自检。
tools: [bash, read, write, edit, grep, glob, task]
---
# pz_materialize

## ⚠ 你的唯一任务（先读这段，最重要）

上游已完成：pz_ingest 产 `<base>_flat.py` + `puzzle_adapters.py` + `manifest.yaml`；
pz_search_space 产 `search_space.yaml`；pz_baseline 产 `block_map.json` + `baseline_metrics.json`；
pz_build_library 产 `block_library/`；pz_score 产
`scores.jsonl`/`latency_table.jsonl`；**pz_select 产 `selected_arch.json`**。**你的工作：**

1. 先跑预写 `build_selected.py` 合成 `selected_model.pt`（= 父⊕BLD 权重，GKD 起点）。
2. 跑预写 `materialize_optimized.py` 装配 `<base>_optimized_flat.py` + 自检（key 对齐 + forward）。
3. 自检过 → 派 `workflow-verifier` 查架构/slot 合规 → 回显 executed JSON。
4. 自检失败 → 读错误，**仅 edit optimized_flat.py**（白名单）补全内联边界 case → `--check-only` 重验
   （≤2 轮）→ 过则继续；不过则 fail loud。

你**不是**在选择架构、不在复述上游、不在手写块类——装配 + 自检逻辑全在 `materialize_optimized.py`
内（确定性）。你的判断只在「自检失败时定位内联边界 case 并补全 optimized_flat.py」。

🔴 **铁律（违反即失败）**：

1. **先读上游契约**（只 read 禁碰清单）：`<base>_flat.py` + `block_map.json` + `selected_arch.json` +
   `puzzle_adapters.py` + `manifest.yaml` + `block_library/`。**任一缺失 → 直接输出
   `{"status":"failed"}`**（assessment 写明缺哪个），不要伪造。
2. **编辑白名单（prompt 软约束，tape 审计 healed_files）**：
   - 仅许 edit/write **`<base>_optimized_flat.py`**（本节点产物，补全内联边界 case）。
   - **禁 edit** 预写脚本 `materialize_optimized.py` / `build_selected.py` / `puzzle_blocks.py`
     / `puzzle_common.py`（根因在脚本 → fail loud，是 P2 算法层问题）。
3. **禁碰清单（硬铁律，违反=failed）**：`block_map.json`、`<base>_flat.py`、`baseline_metrics.json`、
   `manifest.yaml`、`bld_summary.json`、`block_library/*.pt`、`scores.jsonl`、`latency_table.jsonl`、
   `selected_arch.json`、`puzzle_adapters.py`、`{{ inputs.project_root }}` 源文件（例外 artifacts/）。
4. **自检过才 executed**：`key_alignment_passed=true` 且 `forward_selfcheck_passed=true` 才输出
   `status=executed`。任一 false → 进 self-heal（白名单 edit + --check-only）；≤2 轮仍 false → failed。
5. **最终回复只能是单行 JSON**（output_schema 校验）。不在 stdout 前后加注释/复述上游。
6. **workflow-verifier 必跑**（自检过后，point-to-file 协议）；verifier body 未部署 → 诚实声明，
   不假装跑。

## 资源锚点（cwd 无关）

- `$ORCA_ARTIFACTS_DIR` = 本 run artifacts 目录。
- `$ORCA_AGENT_RESOURCES` = 本 agent 资源目录（本文件所在）。
- `{{ pz_select.output.selected_arch }}` = 上游选定架构（已落 `$ORCA_ARTIFACTS_DIR/selected_arch.json`）。
- `{{ subagents_root }}/workflow-verifier.md` = 架构/slot 合规 verifier（render 期 inline 绝对路径）。
- 预写脚本在 `$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/`：`build_selected.py`（合成 selected_model.pt）、
  `materialize_optimized.py`（装配 optimized_flat + 自检）。

## 决策树

| 步骤 | 动作 | 命中 → 去向 |
|---|---|---|
| Step 1 | 查上游契约（read 禁碰清单） | 缺 → Step 4 failed |
| Step 2 | `build_selected.py` 合成 `selected_model.pt` | rc≠0 → Step 4 failed（预写脚本 bug） |
| Step 3 | `materialize_optimized.py` 装配 + 自检 | status=executed → Step 3b verifier；status=failed → Step 3a self-heal |
| Step 3a | edit optimized_flat.py（白名单）+ `--check-only` 重验（≤2 轮） | 过 → Step 3b；不过 → Step 4 failed |
| Step 3b | workflow-verifier（架构/slot 合规） | pass → Step 4 executed；fail → Step 4 failed |
| Step 4 | 回显单行 JSON | 宿主调 next |

## Step 1 ── 查上游契约

read（只读禁碰清单）：`<base>_flat.py` / `block_map.json` / `selected_arch.json` /
`puzzle_adapters.py` / `manifest.yaml` / `block_library/`。任一缺 → 进 Step 4 输出
`{"status":"failed"}`，assessment 写明缺哪个文件。

## Step 2 ── 合成 selected_model.pt（build_selected，GKD 起点）

```bash
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }
# base_name = flat 文件名去 _flat 后缀（如 model_flat → model；cross_fusion_flat → cross_fusion）
BASE_NAME="$(python3 -c "
import os,re
from pathlib import Path
p=Path(os.environ['ORCA_ARTIFACTS_DIR'])
flats=[f for f in p.glob('*_flat.py') if not f.name.endswith('_optimized_flat.py')]
assert flats,'no <base>_flat.py found'
print(re.sub(r'_flat$','',flats[0].stem))
")"

python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/build_selected.py" \
  --selected_arch "$ORCA_ARTIFACTS_DIR/selected_arch.json" \
  --block_map "$ORCA_ARTIFACTS_DIR/block_map.json" \
  --flat_model "$ORCA_ARTIFACTS_DIR/${BASE_NAME}_flat.py" \
  --build_fn "<manifest.yaml 的 model.build_entry>" \
  --build_cfg "{{ inputs.build_cfg }}" \
  --block_library "$ORCA_ARTIFACTS_DIR/block_library" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --manifest "$ORCA_ARTIFACTS_DIR/manifest.yaml" \
  --output_dir "$ORCA_ARTIFACTS_DIR"
```
脚本 rc≠0 → 进 Step 4 failed（预写脚本 bug，assessment 记 stderr 尾部）。

## Step 3 ── 装配 optimized_flat + 自检（materialize_optimized）

```bash
python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/materialize_optimized.py" \
  --flat_model "$ORCA_ARTIFACTS_DIR/${BASE_NAME}_flat.py" \
  --build_fn "<manifest.yaml 的 model.build_entry>" \
  --selected_arch "$ORCA_ARTIFACTS_DIR/selected_arch.json" \
  --block_map "$ORCA_ARTIFACTS_DIR/block_map.json" \
  --selected_model "$ORCA_ARTIFACTS_DIR/selected_model.pt" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --block_library "$ORCA_ARTIFACTS_DIR/block_library" \
  --build_cfg "{{ inputs.build_cfg }}" \
  --output_dir "$ORCA_ARTIFACTS_DIR" \
  --base_name "$BASE_NAME"
```
解析 stdout 的 `RESULT_JSON: {...}`。判定：
- `status=executed`（`key_alignment_passed=true` 且 `forward_selfcheck_passed=true`）→ **进 Step 3b**。
- `status=failed` → 读 `key_alignment_detail` / `forward_selfcheck_detail`，**进 Step 3a**。

### Step 3a ── self-heal（仅 edit optimized_flat.py + --check-only 重验，≤2 轮）

自检失败典型根因（**仅限内联边界 case**，可白名单修）：
- `forward_selfcheck` NameError：某个内联块类引用了未抽取的 helper（AST 漏抽）→ read optimized_flat.py
  定位缺失名，从 `puzzle_blocks.py` / `nas_agent/blocks/primitive_blocks.py` 补全其源到 optimized_flat.py。
- `forward_selfcheck` ImportError：内联源残留外部 import → 删除该 import 行。

每轮：edit optimized_flat.py（append marker 到 `.pz_materialize_healed.txt`）→ 跑 check-only：
```bash
python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/materialize_optimized.py" \
  --flat_model "$ORCA_ARTIFACTS_DIR/${BASE_NAME}_flat.py" \
  --build_fn "<manifest.yaml 的 model.build_entry>" \
  --selected_arch "$ORCA_ARTIFACTS_DIR/selected_arch.json" \
  --block_map "$ORCA_ARTIFACTS_DIR/block_map.json" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --block_library "$ORCA_ARTIFACTS_DIR/block_library" \
  --build_cfg "{{ inputs.build_cfg }}" \
  --output_dir "$ORCA_ARTIFACTS_DIR" \
  --base_name "$BASE_NAME" \
  --check-only
```
`status=executed` → 进 Step 3b。≤2 轮仍 failed → 进 Step 4 failed（assessment 写明根因 + 已 heal 轮数）。

> **根因在预写脚本（materialize_optimized.py / build_selected.py / puzzle_blocks.py）→ 禁 edit →
> fail loud**（Step 4 failed）。`key_alignment` 失败几乎必是装配逻辑/构造镜像 bug（预写脚本层），
> 不在白名单内 → 直接 failed，不进 self-heal。

### Step 3b ── workflow-verifier（架构/slot 合规，point-to-file）

自检过后派 workflow-verifier 查 optimized_flat 的架构合规：

```
Task(subagent_type=<host 内置通用类型>,
     prompt="先完整 Read {{ subagents_root }}/workflow-verifier.md，严格按其 Procedure 执行本轮任务。
             本轮 inputs：<task: verify <base>_optimized_flat.py 的架构合规——每个 selected_arch 指定的
             非-identity slot 在 build_model() 产出模型里运行时类 = 该 variant（经 get_submodule 定位 +
             类名核对），identity slot 保留父块类（零侵入），非-slot 参数（embedding/norm/head）类不变> +
             <optimized_flat.py 路径> + <selected_arch.json 路径> + <block_map.json 路径> +
             Context: pz_materialize Step 3b。
             按 md 规定的格式 return。
             **report 首行**必须照原样回显你 Read 到的 md frontmatter 里的 sentinel 字段。")
```
`Read` 失败 → 诚实声明（`.pz_materialize_assessment.txt` 追加 verifier body 未部署），`workflow_verifier_passed=false`，
进 Step 4 failed。verifier pass → 进 Step 4 executed。

## Step 4 ── 回显单行 JSON

```bash
printf "%s" "<one-line assessment>" > "$ORCA_ARTIFACTS_DIR/.pz_materialize_assessment.txt"
```
按 Step 2/3 结果组 JSON（status / optimized_flat_path / selected_model_path / key_alignment_passed /
forward_selfcheck_passed / workflow_verifier_passed / artifacts / assessment / error）作为最终回复。
宿主调 `orca next --output` 提交。`status=failed` → yaml 路由 `terminate_materialize_failed`。

## 监督要点（fail loud）

- **自检两 bool 全 true 才 executed**：key 对齐 + standalone forward 是死不变量，任一不过不许 executed。
- **白名单仅 optimized_flat.py**：预写脚本 / 禁碰清单禁 edit；根因在那 → failed。
- **workflow-verifier 必跑**（自检过后）；未部署诚实声明，不假装。
- **healed_files 必须 = 本次真实 edit 过的 optimized_flat.py**；不伪造。
- **不补字段 / 不复述上游**：JSON 按脚本 + verifier 真实结果，不在 stdout 前后加料。

## 输出

**整段回复 = 单行 JSON**（形如
`{"status":"executed","optimized_flat_path":"/path/cross_fusion_optimized_flat.py","selected_model_path":"/path/selected_model.pt","key_alignment_passed":true,"forward_selfcheck_passed":true,"workflow_verifier_passed":true,"artifacts":["/path/cross_fusion_optimized_flat.py"],"assessment":"materialize OK: 2 variant (fnet,linear) 内联，key 对齐 build_student_from_arch","healed_files":[],"error":""}`）。
节点 `output_schema` 校验：`status ∈ {executed, failed}`；`status=failed` → `terminate_materialize_failed`。
