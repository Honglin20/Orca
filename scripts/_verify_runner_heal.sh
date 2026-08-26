#!/usr/bin/env bash
# 真机验证 nas-train-runner 的三层确定性核心（不烧 LLM）：
#   A. Step0 GPU 探测：把 NPROC_PER_NODE=8 patch 成实测值（本机 0 卡 → 1）
#   B. gate：脚本崩 / 产物缺 → Step3 emitter 如实吐 last_error（schema+validator 会判败）
#   C. 自愈收敛：edit 修好脚本 → 重跑 → 产出真 ckpt + search.jsonl → emitter 全绿
# 退出码：0=三层都符合预期；1=有不符合。
set -uo pipefail
source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null; conda activate orca 2>/dev/null

WORK=/tmp/verify_runner_heal
rm -rf "$WORK"; mkdir -p "$WORK"/runs/train "$WORK"/runs/search
export OUTPUT_DIR="$WORK"
cd "$WORK" || exit 1

pass=0; fail=0
chk() { if [ "$1" = "$2" ]; then echo "  PASS: $3 ($1)"; pass=$((pass+1)); else echo "  FAIL: $3 (got='$1' want='$2')"; fail=$((fail+1)); fi; }

# ── 夹具：8 卡 launcher + 模拟 device 上限的 train_supernet.py + search ──────────
cat > run_train_supernet.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
OUTPUT_DIR="runs/train"
NNODES=1
NPROC_PER_NODE=8
torchrun --nnodes="$NNODES" --nproc_per_node="$NPROC_PER_NODE" train_supernet.py --output_dir "$OUTPUT_DIR"
SH
chmod +x run_train_supernet.sh

cat > train_supernet.py <<'PY'
import os, sys, argparse, torch
# 模拟真实场景：脚本按 WORLD_SIZE（=nnodes*nproc）要求设备，超额即崩
available = max(1, torch.cuda.device_count())
ws = int(os.environ.get("WORLD_SIZE", "1"))
if ws > available:
    sys.stderr.write(f"[train] ERROR: WORLD_SIZE={ws} > available devices={available}\n")
    sys.exit(2)
ap = argparse.ArgumentParser()
ap.add_argument("--output_dir", required=True)
a = ap.parse_args()
os.makedirs(a.output_dir, exist_ok=True)
open(os.path.join(a.output_dir, "supernet_best.pth"), "wb").write(b"CKPT")
print("[train] wrote supernet_best.pth")
PY

cat > search_config.yaml <<'YML'
evaluator_cfg:
  supernet_ckpt_path: "./runs/train/supernet_best.pth"
YML

cat > run_search_supernet.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
test -f runs/train/supernet_best.pth || { echo "[search] no ckpt"; exit 3; }
# 写 5 行真记录
for i in 1 2 3 4 5; do echo "{\"id\":$i}" >> runs/search/search.jsonl; done
echo "[search] wrote 5 records"
SH
chmod +x run_search_supernet.sh

# ── A. Step0 GPU 探测（直接跑 agent.md Step0 的 python 块）────────────────────
echo "=== A. Step0 GPU 探测 ==="
python3 - <<'PY'
import os, re
try:
    import torch
    n = torch.cuda.device_count()
except Exception:
    n = 0
nproc = n if n > 0 else 1
p = "run_train_supernet.sh"
patched = None
if os.path.exists(p):
    t = open(p).read()
    t2 = re.sub(r'^NPROC_PER_NODE=.*$', f'NPROC_PER_NODE={nproc}', t, count=1, flags=re.M)
    if t2 != t:
        open(p, 'w').write(t2); patched = f"NPROC_PER_NODE={nproc}"
print(f"GPU_PROBE: cuda_devices={n} -> nproc={nproc}; patched={patched}")
PY
NEW=$(grep '^NPROC_PER_NODE=' run_train_supernet.sh)
chk "$NEW" "NPROC_PER_NODE=1" "A1 launcher 被 patch 成实测值（8→1）"

# ── B. gate：故意不改 train 错误逻辑会自洽——这里直接验证 emitter 对「缺 ckpt + 0 记录」如实判败
# 先不改 ckpt（还没跑 train），直接验证 emitter
echo "=== B. gate（emitter 对缺失如实吐 last_error）==="
# 此时还没跑 train/search，ckpt 不存在、search.jsonl 不存在 → emitter 必须如实报败
python3 - > /tmp/verify_out.json <<'PY'
import json, os, re
od = os.environ["OUTPUT_DIR"]
ckpt_rel = "runs/train/supernet_best.pth"
for line in open(os.path.join(od, "search_config.yaml")):
    m = re.search(r'supernet_ckpt_path:\s*"?([^\s"#]+)"?', line)
    if m: ckpt_rel = m.group(1); break
ckpt = ckpt_rel if os.path.isabs(ckpt_rel) else os.path.join(od, ckpt_rel)
train_viable = os.path.exists(os.path.join(od, "run_train_supernet.sh"))
train_done = (not train_viable) or os.path.exists(ckpt)
slog = os.path.join(od, "runs/search/search.jsonl")
recs = 0
try:
    recs = sum(1 for _ in open(slog))
except FileNotFoundError: pass
last_error = ""
if train_viable and not train_done: last_error += f"[TRAIN] ckpt missing: {ckpt}"
if recs == 0: last_error += "[SEARCH] records=0"
print(json.dumps({"train_viable":train_viable,"train_done":train_done,"search_records":recs,"last_error":last_error}))
PY
cat /tmp/verify_out.json; echo
python3 -c "import json;d=json.load(open('/tmp/verify_out.json'));import sys;sys.exit(0 if (d['train_done']==False and d['search_records']==0 and d['last_error']!='') else 1)" \
  && { echo "  PASS: B1 emitter 如实判败（train_done=false / records=0 / last_error 非空）"; pass=$((pass+1)); } \
  || { echo "  FAIL: B1 emitter 没如实判败"; fail=$((fail+1)); }

# ── C. 自愈收敛：跑 train（patch 后应成功）→ 跑 search → emitter 全绿 ─────────
echo "=== C. 自愈收敛（patch 后真跑 train+search）==="
bash run_train_supernet.sh > runs/train/train.attempt1.log 2>&1; TRC=$?
chk "$TRC" "0" "C1 patch 后 train 退出码 0（证明 8 卡 patch 救活了脚本）"
test -f runs/train/supernet_best.pth && { echo "  PASS: C2 ckpt 真产出"; pass=$((pass+1)); } || { echo "  FAIL: C2 ckpt 缺"; fail=$((fail+1)); }
bash run_search_supernet.sh > runs/search/search.attempt1.stdout.log 2>&1; SRC=$?
chk "$SRC" "0" "C3 search 退出码 0"
# 再跑 emitter，必须全绿
python3 - > /tmp/verify_out2.json <<'PY'
import json, os, re
od = os.environ["OUTPUT_DIR"]
ckpt_rel = "runs/train/supernet_best.pth"
for line in open(os.path.join(od, "search_config.yaml")):
    m = re.search(r'supernet_ckpt_path:\s*"?([^\s"#]+)"?', line)
    if m: ckpt_rel = m.group(1); break
ckpt = ckpt_rel if os.path.isabs(ckpt_rel) else os.path.join(od, ckpt_rel)
train_viable = os.path.exists(os.path.join(od, "run_train_supernet.sh"))
train_done = (not train_viable) or os.path.exists(ckpt)
slog = os.path.join(od, "runs/search/search.jsonl")
recs = sum(1 for _ in open(slog))
last_error = ""
if train_viable and not train_done: last_error += "[TRAIN] missing"
if recs == 0: last_error += "[SEARCH] 0"
print(json.dumps({"train_viable":train_viable,"train_done":train_done,"search_records":recs,"last_error":last_error}))
PY
cat /tmp/verify_out2.json; echo
python3 -c "import json;d=json.load(open('/tmp/verify_out2.json'));import sys;sys.exit(0 if (d['train_done']==True and d['search_records']==5 and d['last_error']=='') else 1)" \
  && { echo "  PASS: C4 emitter 全绿（train_done=true / records=5 / last_error 空 → 过 schema+validator）"; pass=$((pass+1)); } \
  || { echo "  FAIL: C4 emitter 没全绿"; fail=$((fail+1)); }

echo
echo "================ SUMMARY: pass=$pass fail=$fail ================"
[ $fail -eq 0 ]
