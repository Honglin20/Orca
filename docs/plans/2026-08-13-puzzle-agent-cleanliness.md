# Puzzle agent.md 洁净重构（对齐 agent-prompt-cleanliness-contract）

## 背景 / 判据

puzzle 7 个 agent.md 违反洁净契约两条底线：
1. **确定性逻辑内联**（§4）：`REPO_ROOT` 父目录回爬 python 块、Step 0 reuse-check 的
   loop+assert、zero-LLM 节点的整段 launcher bash，全部内联在 agent.md。
2. **受众分离**（§1）：`scripts/` 11 行枚举、`脚本契约` 文档块，是给 reviewer 看的开发文档，
   却写进运行时 prompt。

## 核心事实（改法依据）

- **引擎已注入 `$ORCA_WORKFLOWS_ROOT`**（`orca/exec/env.py:111` = workflow yaml 所在目录），
  其它 workflow（kd-setup / teacher-gen / kd-train-script）已用
  `$ORCA_WORKFLOWS_ROOT/agents/_kd_scripts` 定位共享脚本。puzzle 的
  `REPO_ROOT="$(python3 -c '…parent.name=="workflows"…')"` 回爬是冗余且脆弱（依赖目录名恰好
  叫 workflows）。
- **scripts/ 文件不被 Jinja 渲染**（`orca/exec/render.py` 只渲染 `node.prompt`）。inputs 只能
  经 agent.md 内联（渲染后）传参给脚本。故 zero-LLM 节点保留一条内联命令，把渲染值作 argv
  传给静态 `scripts/run.sh`。
- **mip_select.py / gate_report.py 的 `--output_dir` 是 required**（`mip_select.py:228`、
  `gate_report.py:97`）。当前 pz_select agent.md 命令漏传 `--output_dir`（会 argparse 崩）、漏传
  `--latency-unit`（us/s 声明下输出错标 ms）——**提取 run.sh 时一并修正**（对齐 smoke test 的
  规范调用 `tests/test_puzzle_scripts_smoke.py:156-162,215-222`）。
- **`gate_report.py` 需 `--optimized_flat` 绝对路径**；当前 pz_report 留 `<base_name>` 占位符让
  LLM 自己填（脆弱）。run.sh 直接 glob `*_optimized_flat.py`。

## 改动清单（F1–F6）

### F1 `REPO_ROOT` → `$ORCA_WORKFLOWS_ROOT`（7 处）
所有 `REPO_ROOT="$(python3 -c …)"` 块删除，`$REPO_ROOT/workflows/agents/_puzzle_scripts/<f>`
统一改为 `$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/<f>`。含 run_bld.sh / run_retrain.sh /
run_score.sh 三个 launcher 模板里的引用。

### F2 scripts/ 枚举压缩
pz_build_library / pz_retrain 资源锚点里 11 行 scripts 清单压成一行「确定性逻辑全在 scripts/，
只跑不读」。

### F3 zero-LLM 节点塌缩（pz_select / pz_report）
抽 `scripts/run.sh`（内容见下），agent.md 塌缩为 ~15 行：唯一任务 + 铁律 + 一条
`bash "$ORCA_AGENT_RESOURCES/scripts/run.sh" <rendered args>`。删「脚本契约」段、删重复监督要点。

### F4 「脚本契约」文档块
- pz_select / pz_report：整段删（zero-LLM 不读）。
- pz_expand Step 2：只留「exit 0/2/3 → 路由」一条判据，删产物字段 / smoke 清单 /
  MIP 形式化文档。

### F5 Step 0 reuse-check 抽脚本
pz_expand / pz_score 各抽 `scripts/reuse_check.sh`（loop+assert 逻辑）。

### F6 BASE_NAME 占位符
pz_report 的 `<base_name>_optimized_flat.py` 占位符由 run.sh glob 消解；pz_materialize 的内联
BASE_NAME python 一行保留（它是本节点产物命名的确定性前置，或抽进 build_selected 调用）。

## 新脚本内容

### pz_select/scripts/run.sh
```bash
#!/bin/bash
# 跑预写 mip_select.py 恰好一次；stdout 单行 JSON 即最终回复。
# 依赖：ORCA_ARTIFACTS_DIR / ORCA_WORKFLOWS_ROOT（orca spawn 注入）。
set -euo pipefail
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

TARGET_LAT="${1:-}"
REDUCTION="${2:-0.5}"
UNIT="${3:-ms}"

TARGET_LAT_ARG=""
if [ -n "$TARGET_LAT" ]; then
  TARGET_LAT_ARG="--target-latency $TARGET_LAT"
fi

python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/mip_select.py" \
  --scores "$ORCA_ARTIFACTS_DIR/scores.jsonl" \
  --latency-table "$ORCA_ARTIFACTS_DIR/latency_table.jsonl" \
  --baseline-metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
  --latency_reduction_target "$REDUCTION" \
  --latency-unit "$UNIT" \
  --output_dir "$ORCA_ARTIFACTS_DIR" \
  $TARGET_LAT_ARG
```

### pz_report/scripts/run.sh
```bash
#!/bin/bash
# 跑预写 gate_report.py 恰好一次；stdout 单行 JSON 即最终回复。
# 依赖：ORCA_ARTIFACTS_DIR / ORCA_WORKFLOWS_ROOT（orca spawn 注入）。
set -euo pipefail
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

UNIT="${1:-ms}"
LATENCY_SCRIPT="${2:-}"
REDUCTION="${3:-0.5}"

OPT_FLAT="$(ls "$ORCA_ARTIFACTS_DIR/"*_optimized_flat.py 2>/dev/null | head -1)"
[ -n "$OPT_FLAT" ] || { echo "FATAL: no *_optimized_flat.py in $ORCA_ARTIFACTS_DIR" >&2; exit 1; }

python3 "$ORCA_WORKFLOWS_ROOT/agents/_puzzle_scripts/gate_report.py" \
  --final_model "$ORCA_ARTIFACTS_DIR/runs/retrain/final_model.pt" \
  --baseline_metrics "$ORCA_ARTIFACTS_DIR/baseline_metrics.json" \
  --optimized_flat "$OPT_FLAT" \
  --adapters "$ORCA_ARTIFACTS_DIR/puzzle_adapters.py" \
  --manifest "$ORCA_ARTIFACTS_DIR/manifest.yaml" \
  --latency_unit "$UNIT" \
  --latency_script_path "$LATENCY_SCRIPT" \
  --latency_reduction_target "$REDUCTION" \
  --output_dir "$ORCA_ARTIFACTS_DIR"
```

## 验收
- `tars validate workflows/puzzle.yaml` 0 error / 0 warning。
- 受众翻转通读：7 个 agent.md 无过程描述、无开发期残留、无冗余 scripts 枚举。
- 逻辑不变：路由守卫、output_schema 字段、subagent sentinel 协议、禁碰清单、铁律全保留。
