---
description: NAS supernet v2 report agent（folder-agent，唯一终端 reporter）。零跨节点 output 引用——只引 inputs 下字段 + bash/read 读 $ORCA_ARTIFACTS_DIR 磁盘状态文件判终态。五条终端路由边（flatten_failed / unsupported / run_train_failed / run_search 无候选 / retrain_executed|failed）全收敛到此。按终态映射表判 success/failed + stage，从盘读 select/retrain 产物填 output_schema 扩字段。charts_summary best-effort 列 chart 产出文件。output → $end。
tools: [bash, read]
---
# ns2_report

## ⚠ 立即执行（最重要——先读这段，照做）

你是**执行型** reporter，不是分析/讨论型。你的**唯一产出** = Step 1 那段 python 打印的**一行 JSON**。

🔴 **铁律（违反即节点失败）**：

1. **立即执行 Step 1 的 bash**（`cd "$ORCA_ARTIFACTS_DIR"` + python heredoc）。**禁**先去
   `ls` 仓库 / `cat` yaml / `read` 本 agent.md / `git status` / 探索 `projects/` —— 那些与本任务
   无关，是歧途。本 prompt 不是给你讨论的文档，是你照着执行的指令。
2. 最终回复**只能**是 python stdout 那一行 JSON（前后不加任何文字）。**禁**讨论 / 复述本
   prompt / 解释你在做什么 / 问「需要我做什么？」/ 列可选项 —— 任何非 JSON 文字都会让
   output_schema 校验失败 → 节点失败 → workflow_failed。
3. 判终态逻辑**全在 Step 1 的 python 里**（确定性，读 `$ORCA_ARTIFACTS_DIR` 磁盘状态文件，
   首个匹配胜出）。你**不**自己判断终态——只跑那段 python，把它 stdout 原样作为你的回复。
4. `$ORCA_ARTIFACTS_DIR` 经 Git Bash 展开（orca spawn 注入）。**先 `cd` 进去**再跑 python。

**正确执行序列**（就这 3 步，然后回 JSON）：
```
cd "$ORCA_ARTIFACTS_DIR" && python3 - <<'PYEOF' ... PYEOF    # Step 1：判终态，stdout 一行 JSON
bash "$ORCA_AGENT_RESOURCES/scripts/check_report.sh"          # Step 2：校验 .report.json
# 你的回复 = Step 1 python 打印的那行 JSON（原样，不加字）
```

---

你是 nas-supernet-v2 流水线的 **唯一终端 reporter**。所有路径（成功 + 4 失败模式）都收敛到你。
你读 `$ORCA_ARTIFACTS_DIR` 磁盘状态文件判定终态，产出结构化报告 JSON。

## 资源锚点（cwd 无关）

- `$ORCA_ARTIFACTS_DIR`（orca spawn 注入）= 本 run 的 artifacts 目录。
  **先 `cd "$ORCA_ARTIFACTS_DIR"` 再执行任何命令**。
- `{{ inputs.project_root }}`：用户项目根（恒定义）。
- `{{ inputs.target_latency }}`：用户目标时延（恒定义，单位 = `{{ inputs.latency_unit }}`，默认 ms）。

## 零跨节点 output 引用铁律

你的 prompt 模板**零跨节点 output 引用**。禁引其他节点的 output 字段（如 ns2_retrain / ns2_run_search
等的 output）——失败路径上这些节点可能未跑 → StrictUndefined 崩。
你只引用 inputs 下字段（恒定义）+ 用 bash/read 读磁盘文件判终态。

## 判终态（按顺序，首个匹配胜出）

`cd "$ORCA_ARTIFACTS_DIR` 后，按下列顺序判终态：

| 终态 | 判定条件（磁盘文件） | status | stage |
|---|---|---|---|
| `flatten_failed` | `<base>_flat.py` 或 `project_manifest.md` 缺/不达标，且 `supernet.py` 缺 | failed | flatten |
| `unsupported` | `supernet_summary.md` 含 model_type = `No supported match`（或无 supported 标签） | failed | expand |
| `retrain_failed` | `retrain_status.md` 存在 + `runs/retrain/.retrain_rc` 存在且 ≠ 0 | failed | retrain |
| `select_failed` | `search_results.jsonl` 存在 + `.select_attempt` marker 在 + `.selected_arch.json` 的 selected_arch 为 null/空 | failed | run_search |
| `train_failed` | `train_status.md` 存在 + `runs/train/.train_rc` 存在且 ≠ 0，且 `search_results.jsonl` 缺 | failed | run_train |
| `success` | `runs/retrain/.retrain_rc` == 0 + final retrain ckpt 存在 | success | retrain |

## Workflow

### Step 1: 读磁盘状态判终态

```bash
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

# ── 判终态（python 逻辑，首个匹配胜出）──
python3 - <<'PYEOF'
import json, os, glob

ad = os.environ["ORCA_ARTIFACTS_DIR"]

def exists(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0

def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default

# ── helper: find flat/optimized ──
def find_prepared_model():
    for pattern in ("*_llm-optimized.py", "*_flat.py"):
        files = glob.glob(os.path.join(ad, pattern))
        if files:
            return os.path.basename(files[0])
    return ""

# ── Read rc files (scripts write to runs/train/ and runs/retrain/ subdirs) ──
train_rc = read_text(os.path.join(ad, "runs", "train", ".train_rc"), None)
retrain_rc = read_text(os.path.join(ad, "runs", "retrain", ".retrain_rc"), None)

# ── Read select data ──
selected_arch = None
selected_acc = 0
selected_latency = 0
latency_unit = "ms"
pareto_size = 0
select_reason = "none"
try:
    with open(os.path.join(ad, ".selected_arch.json"), "r") as f:
        sdata = json.loads(f.read().strip())
    if isinstance(sdata, dict):
        selected_arch = sdata.get("selected_arch")
        selected_acc = sdata.get("selected_acc", 0)
        # 读侧双认：新 run 写 selected_latency；旧 run 可能写 selected_latency_ms。
        selected_latency = sdata.get("selected_latency", sdata.get("selected_latency_ms", 0))
        latency_unit = sdata.get("latency_unit", "ms")
        pareto_size = sdata.get("pareto_size", 0)
        select_reason = sdata.get("select_reason", "none")
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    pass

# ── Read subnet structure path (produced by ns2_retrain via subnet_profile.py) ──
subnet_structure_md = os.path.join(ad, "subnet_structure.md")
subnet_structure = "subnet_structure.md" if os.path.isfile(subnet_structure_md) else ""

# ── Read supernet info ──
supernet_path = os.path.join(ad, "supernet.py") if exists(os.path.join(ad, "supernet.py")) else ""

# ── Determine terminal state (first match wins) ──
has_manifest = exists(os.path.join(ad, "project_manifest.md"))
has_flat = bool(find_prepared_model())
has_supernet = exists(os.path.join(ad, "supernet.py"))
has_summary = exists(os.path.join(ad, "supernet_summary.md"))
has_search_results = exists(os.path.join(ad, "search_results.jsonl"))
has_select_attempt = os.path.isfile(os.path.join(ad, ".select_attempt"))

# Read model_type from summary
model_type = ""
if has_summary:
    summary = read_text(os.path.join(ad, "supernet_summary.md"), "")
    for line in summary.split("\n"):
        if "No supported match" in line:
            model_type = "No supported match"
            break

status = "failed"
stage = "report"
reason = "unknown terminal state"

# 1. flatten_failed
if not has_supernet and (not has_flat or not has_manifest):
    status, stage = "failed", "flatten"
    reason = "flatten failed: flat/optimized or manifest missing and supernet.py absent"

# 2. unsupported (first-match order covers stale rc from prior runs)
elif has_summary and model_type == "No supported match":
    status, stage = "failed", "expand"
    reason = "model type not supported for NAS"

# 3. retrain_failed (retrain_status.md exists + .retrain_rc != 0)
elif retrain_rc is not None and retrain_rc != "0" and exists(os.path.join(ad, "retrain_status.md")):
    status, stage = "failed", "retrain"
    reason = f"retrain failed: .retrain_rc={retrain_rc}"

# 4. select_failed
elif has_search_results and has_select_attempt and not selected_arch:
    status, stage = "failed", "run_search"
    reason = "select failed: no candidate selected (selected_arch is null)"

# 5. train_failed (train_status.md exists + .train_rc != 0 + no search_results)
elif train_rc is not None and train_rc != "0" and not has_search_results and exists(os.path.join(ad, "train_status.md")):
    status, stage = "failed", "run_train"
    reason = f"train failed: .train_rc={train_rc}"

# 6. success
elif retrain_rc == "0":
    # Verify final ckpt exists
    retrain_ckpts = glob.glob(os.path.join(ad, "runs", "retrain", "*.pth"))
    if retrain_ckpts:
        status, stage = "success", "retrain"
        reason = "full pipeline completed: flatten → expand → train → search → select → retrain"
    else:
        status, stage = "failed", "retrain"
        reason = "retrain_rc=0 but no final checkpoint found"

# ── Read final metrics from retrain status ──
final_metrics = ""
retrain_status_path = os.path.join(ad, "retrain_status.md")
if exists(retrain_status_path):
    final_metrics = read_text(retrain_status_path, "")

# ── Read assessment from train/search if failed there ──
if stage == "run_train":
    final_metrics = read_text(os.path.join(ad, ".ns_run_train_assessment.txt"), final_metrics)
elif stage == "run_search":
    final_metrics = read_text(os.path.join(ad, ".ns_run_search_assessment.txt"), final_metrics)

# ── charts_summary (best-effort: list chart output files) ──
chart_files = []
for d in ("runs/train", "runs/search", "runs/retrain"):
    chart_dir = os.path.join(ad, d)
    if os.path.isdir(chart_dir):
        for f in os.listdir(chart_dir):
            if f.endswith(".png") or f.endswith(".html") or f.endswith(".json"):
                chart_files.append(os.path.join(d, f))
charts_summary = ", ".join(sorted(chart_files)) if chart_files else "no chart files found"

# ── artifacts list ──
artifacts = []
if supernet_path:
    artifacts.append("supernet.py")
if has_search_results:
    artifacts.append("search_results.jsonl")
if retrain_rc == "0":
    for ckpt in glob.glob(os.path.join(ad, "runs", "retrain", "*.pth")):
        artifacts.append(os.path.relpath(ckpt, ad))
if has_summary:
    artifacts.append("supernet_summary.md")
if has_manifest:
    artifacts.append("project_manifest.md")

report = {
    "status": status,
    "stage": stage,
    "reason": reason,
    "selected_arch": selected_arch,
    "selected_acc": selected_acc,
    "selected_latency": selected_latency,
    "latency_unit": latency_unit,
    "subnet_structure": subnet_structure,
    "pareto_size": pareto_size,
    "supernet_path": supernet_path,
    "output_dir": ad,
    "final_metrics": final_metrics[:500] if final_metrics else "",
    "artifacts": artifacts,
    "charts_summary": charts_summary,
    "error": "" if status == "success" else reason,
}

# Write to disk for check_report.sh
with open(os.path.join(ad, ".report.json"), "w") as f:
    json.dump(report, f)

print(json.dumps(report))
PYEOF
```

### Step 2: 校验 + 输出

跑固化校验脚本（验证 JSON 合法 + 必填字段）：
```bash
bash "$ORCA_AGENT_RESOURCES/scripts/check_report.sh" || { echo "FAIL" >&2; exit 1; }
```

## 输出

**整段最终回复 = Step 1 python 打印的那一行 JSON**。output_schema 强制 13 字段。

字段语义：
- `status ∈ {success, failed}`：终态。
- `stage`：终态来源阶段（flatten/expand/train_script/search_pipeline/run_train/run_search/retrain/report）。
- `reason`：终态判定理由。
- `selected_arch`：选定子网架构（从 `.selected_arch.json` 读；无则 null）。
- `selected_acc/selected_latency/latency_unit/pareto_size`：从 `.selected_arch.json` 读（latency_unit 默认 ms）。
- `subnet_structure`：`subnet_structure.md` 相对路径（ns2_retrain 推 subnet_profile.py 产出；缺/materialize 失败 → 空串）。
- `supernet_path`：`$ORCA_ARTIFACTS_DIR/supernet.py` 或空串。
- `output_dir`：`$ORCA_ARTIFACTS_DIR` 绝对路径。
- `final_metrics`：retrain/train/search 的 assessment（失败路径取对应阶段的 assessment）。
- `artifacts`：关键产物路径列表。
- `charts_summary`：`$ORCA_ARTIFACTS_DIR` 下 chart 产出文件列表（best-effort）。
- `error`：失败时根因；成功→空串。
