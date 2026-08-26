#!/usr/bin/env bash
# Script-level equivalent E2E for kd-nas v4 (setup -> gate -> train -> select
# measurement contracts). Drives the REAL _kd_scripts + the v4 train_pipeline.py
# artifact that the headless run's train_script_gen node produced, on the real
# WSL conda python (torch 2.13.0+cpu / onnxruntime 1.27.0).
#
# This proves the node plumbing the headless run could NOT reach (it was blocked
# at setup by the Windows-opencode/Git-Bash env mismatch). Real measurements,
# real ledger-equivalent rows. NOT a mocked test.
set -uo pipefail   # NOTE: not -e; we capture each step's rc explicitly.

REPO="/mnt/d/Projects/Orca"
cd "$REPO"
source ~/miniconda3/etc/profile.d/conda.sh
conda activate orca
export ORCA_KB_DIR="$REPO/examples/kd-nas-demo/knowledge_base"
export ORCA_KD_SCRIPTS_DIR="$REPO/workflows/agents/_kd_scripts"
export ORCA_PROJECT_ROOT="$REPO"

WORK=/tmp/kd_v4_e2e
rm -rf "$WORK"; mkdir -p "$WORK"

TEACHER_PY="$REPO/llm_artifacts/receiver_net_baseline_teacher/receiver_net_baseline_teacher.py"
TRAIN_PIPELINE="$REPO/llm_artifacts/kd_train_script/train_pipeline.py"
LAT="$REPO/examples/kd-nas-demo/latency_provider.py::measure"
DEMO="$REPO/examples/kd-nas-demo"
DUMMY='{"shape":[1,4,48,64,1],"dtype":"float32"}'

pass=0; fail=0
step() { echo; echo "========================================================"; echo "STEP $1: $2"; echo "========================================================"; }

# ── 1. train_pipeline.py --mode teacher (v4 setup step5) ─────────────────
step 1 "train_pipeline.py --mode teacher  (v4 setup step5: real teacher training)"
python3 "$TRAIN_PIPELINE" --mode teacher \
  --model_path "$TEACHER_PY" --build_fn build_model \
  --out_ckpt "$WORK/teacher.pt" --epochs 1 --device cpu --seed 0 \
  --variant_id receiver_net_baseline_teacher \
  --project_root "$REPO" 2>&1 | tail -15
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ] && [ -f "$WORK/teacher.pt" ]; then echo "STEP1=PASS (teacher.pt exists, rc=0)"; pass=$((pass+1)); else echo "STEP1=FAIL rc=$rc"; fail=$((fail+1)); fi

# ── 2. teacher_setup.py (setup step5b: teacher_cache + ONNX + real latency) ─
step 2 "teacher_setup.py  (setup step5b: teacher_cache + real ONNX latency)"
python3 "$REPO/workflows/agents/_kd_scripts/teacher_setup.py" \
  --teacher_model_path "$TEACHER_PY" --teacher_ckpt "$WORK/teacher.pt" \
  --build_fn build_model --dummy_input "$DUMMY" \
  --output_dir "$WORK/teacher_setup" --opset 17 \
  --latency_provider "$LAT" --device cpu --seed 0 2>&1 | tail -12
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ] && [ -f "$WORK/teacher_setup/teacher_cache.pt" ]; then echo "STEP2=PASS (teacher_cache.pt exists)"; pass=$((pass+1)); else echo "STEP2=FAIL rc=$rc"; fail=$((fail+1)); fi

# ── 3. gpu_probe.py (setup step8: device=cpu -> concurrency=1) ─────────────
step 3 "gpu_probe.py  (setup step8: fail-soft concurrency on CPU)"
python3 "$REPO/workflows/agents/_kd_scripts/gpu_probe.py" \
  --teacher_cache "$WORK/teacher_setup/teacher_cache.pt" \
  --representative_variant "$DEMO/baseline_model.py" \
  --variants_count 4 --device cpu 2>&1 | tail -8
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "STEP3=PASS"; pass=$((pass+1)); else echo "STEP3=FAIL rc=$rc"; fail=$((fail+1)); fi

# ── 4. tune_latency.py (gate core: real latency gate on demo_tiny_tf) ──────
step 4 "tune_latency.py  (gate core: real latency gate, demo_tiny_tf)"
python3 "$REPO/workflows/agents/_kd_scripts/tune_latency.py" \
  --variant_path "$DEMO/knowledge_base/families/receiver/demo_tiny_tf.py" \
  --build_fn build_model --dummy_input "$DUMMY" \
  --knobs '{"num_blocks":{"default":2,"min":2,"step":-1,"leverage":"high"},"embed_dim":{"default":8,"min":4,"step":-2,"leverage":"medium"}}' \
  --target_latency_ms 5.0 --latency_provider "$LAT" \
  --artifacts_dir "$WORK/tune_test" --device cpu --max_measurements 5 --measure_repeats 2 2>&1 | tail -10
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "STEP4=PASS"; pass=$((pass+1)); else echo "STEP4=FAIL rc=$rc"; fail=$((fail+1)); fi

# ── 5. train_pipeline.py --mode distill (v4 train core: real KD distill) ──
step 5 "train_pipeline.py --mode distill  (v4 train core: real KD distill)"
python3 "$TRAIN_PIPELINE" --mode distill \
  --student_model_path "$DEMO/knowledge_base/families/receiver/demo_tiny_tf.py" \
  --teacher_cache "$WORK/teacher_setup/teacher_cache.pt" \
  --build_fn build_model --build_cfg '{"num_blocks":2,"embed_dim":8}' \
  --kd_config '{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}' \
  --variant_id demo_tiny_tf --out_ckpt "$WORK/demo_tiny_tf.pt" \
  --epochs 1 --device cpu --seed 0 \
  --project_root "$REPO" 2>&1 | tail -12
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ] && [ -f "$WORK/demo_tiny_tf.pt" ]; then echo "STEP5=PASS (student ckpt exists)"; pass=$((pass+1)); else echo "STEP5=FAIL rc=$rc"; fail=$((fail+1)); fi

# ── 6. train_pipeline.py --mode eval (v4: real NMSE via ported user eval metric) ─
# NOTE: KD 精度测量已从 measure_student --eval_command 迁到 train_pipeline.py --mode eval
# （eval 指标由 kd-train-script 从用户仓 test_student.py 移植、固化进 train_pipeline.py）。
# 需 $TRAIN_PIPELINE 由新 kd-train-script 生成（含 eval mode）；旧 artifact 无 eval mode 会 FAIL。
step 6 "train_pipeline.py --mode eval  (read-only NMSE via ported eval metric -> STUDENT_ACCURACY)"
python3 "$TRAIN_PIPELINE" --mode eval \
  --student_model_path "$DEMO/knowledge_base/families/receiver/demo_tiny_tf.py" \
  --student_ckpt "$WORK/demo_tiny_tf.pt" --build_fn build_model \
  --build_cfg '{"num_blocks":2,"embed_dim":8}' \
  --accuracy_baseline 1.5 --accuracy_baseline_kind nmse \
  --device cpu 2>&1 | tail -15
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "STEP6=PASS"; pass=$((pass+1)); else echo "STEP6=FAIL rc=$rc"; fail=$((fail+1)); fi

echo; echo "========================================================"
echo "SCRIPT-LEVEL E2E SUMMARY: pass=$pass fail=$fail"
echo "========================================================"
