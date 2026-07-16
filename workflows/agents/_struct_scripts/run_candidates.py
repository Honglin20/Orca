"""run_candidates.py —— 并行候选评测启动器（model-file-fanout，并行最小验收）。

机制 = shallow-worktree（debate 综合：A 的可靠隔离 + B 的轻量，用 symlink 落地）：
  - 每个候选 = 一个 model.py 文件（**同 build_fn + 同类名**，workflow 保证）。
  - 对每个候选建 shallow-worktree（symlink project_root 全部文件 + 覆盖 model.py 为候选），
    train_command 原样跑、cwd=worktree → train.py 的 `import model` 解析到候选 model.py。
    **零改 train.py**（不变量2 保持），git 无关，symlink 故轻量（大 dataset 也只 symlink 不拷）。
  - 并行跑 K 个子进程（concurrent）。先并行导出 ONNX + 测时延（廉价 CPU）；
    可选 `--train_filter_latency` 只对时延门幸存者训练（§4 漏斗）。
  - （PYTHONPATH 影子对"train.py+model.py 同目录 + import model"无效——sys.path[0]=脚本目录
    优先于 PYTHONPATH，会静默训成 baseline——故 worktree 为可靠主路径，非影子。）

class-name 提取：确定性 AST 走（找 nn.Module 子类名），不是 LLM 判断。

stdout：每候选一行 JSON（id/model_file/class_name/latency_ms/accuracy/status）+ 末尾 SUMMARY。
fail loud：导出/解析异常 → 该候选 status=FAIL，不阻塞其他候选。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading


# torch.onnx.export 用 module-global 状态（GLOBALS.in_onnx_export），非线程安全——
# 并发导出会 AssertionError。导出+测时延是廉价 CPU 步骤，串行化即可（训练仍并行）。
_EXPORT_LOCK = threading.Lock()


# ── 复用同目录 export_onnx + 动态加载 latency_provider ─────────────────────────
def _here_import(name: str):
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib
    return importlib.import_module(name)


def _load_measure(latency_provider: str):
    if "::" not in latency_provider:
        raise ValueError(f"latency_provider 需 'path::func'，得到 {latency_provider!r}")
    path, func = latency_provider.split("::", 1)
    import importlib.util

    spec = importlib.util.spec_from_file_location("cost_model", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    measure = getattr(mod, func, None)
    if not callable(measure):
        raise TypeError(f"{path}::{func} 不是 callable")
    return measure


def extract_model_class(model_path: str) -> str | None:
    """确定性摘取 model.py 里 nn.Module 子类的类名（workflow 保证类名固定）。"""
    tree = ast.parse(Path(model_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                # nn.Module  → Attribute(attr='Module')；Module → Name(id='Module')
                if (isinstance(base, ast.Attribute) and base.attr == "Module") or (
                    isinstance(base, ast.Name) and base.id == "Module"
                ):
                    return node.name
    return None


_ACC_PATTERNS = [
    re.compile(r"FINAL_ACCURACY\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I),
    re.compile(r"\baccuracy\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I),
]


def _parse_accuracy(stdout: str) -> float | None:
    for line in stdout.splitlines()[::-1]:
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                for k in ("accuracy", "acc", "val_acc"):
                    if k in d and isinstance(d[k], (int, float)):
                        return float(d[k])
            except json.JSONDecodeError:
                pass
    for pat in _ACC_PATTERNS:
        m = pat.search(stdout)
        if m:
            return float(m.group(1))
    return None


def _shallow_worktree(project_root: str, candidate_model: str, dest: str) -> str:
    """影子失败时的 fallback：symlink project_root 全部 + 覆盖 model.py 为候选。"""
    os.makedirs(dest, exist_ok=True)
    for entry in os.listdir(project_root):
        src = os.path.join(project_root, entry)
        dst = os.path.join(dest, entry)
        if os.path.exists(dst) or os.path.islink(dst):
            continue
        try:
            os.symlink(os.path.abspath(src), dst)
        except OSError:
            pass
    # 覆盖 model.py 为候选（先删 symlink 再拷）
    model_link = os.path.join(dest, "model.py")
    if os.path.islink(model_link) or os.path.exists(model_link):
        os.remove(model_link)
    import shutil

    shutil.copy2(candidate_model, model_link)
    return dest


def evaluate_one(args, cand_idx: int, model_file: str) -> dict:
    """单候选：摘类名 → 导出ONNX+测时延 → 影子跑 train_command 解析 accuracy。"""
    rec = {
        "id": f"c{cand_idx}",
        "model_file": os.path.abspath(model_file),
        "class_name": None,
        "latency_ms": None,
        "accuracy": None,
        "status": "FAIL",
        "fail_reason": "",
        "isolation": "shadow",
    }
    export_onnx = _here_import("export_onnx")

    # 1. 类名摘取
    try:
        rec["class_name"] = extract_model_class(rec["model_file"])
    except Exception as e:
        rec["fail_reason"] = f"class_extract: {e}"

    out_dir = os.path.join(args.out_dir, f"c{cand_idx}")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    onnx_path = os.path.join(out_dir, "model.onnx")

    # 2. 导出 ONNX + 测时延（廉价 CPU；**串行化**——torch.onnx.export 非线程安全，见 _EXPORT_LOCK）
    try:
        with _EXPORT_LOCK:
            export_onnx.export_onnx(
                model_path=rec["model_file"], build_fn=args.build_fn,
                dummy_input=args.dummy_input, opset=args.opset, out=onnx_path,
            )
            measure = _load_measure(args.latency_provider)
            rec["latency_ms"] = float(measure(onnx_path))
    except Exception as e:
        rec["fail_reason"] = f"export/measure: {type(e).__name__}: {e}"
        rec["status"] = "FAIL_export"
        print(json.dumps(rec, ensure_ascii=False))
        return rec

    # 3. 时延门（可选）：超时延阈值的跳过训练
    if args.train_filter_latency and args.champion_latency_ms and rec["latency_ms"] >= float(args.champion_latency_ms):
        rec["status"] = "FAIL_latency"
        rec["accuracy"] = -1
        print(json.dumps(rec, ensure_ascii=False))
        return rec

    # 4. 训练：shallow-worktree（symlink project 全部文件 + 覆盖 model.py 为候选）→
    #    train.py 原样跑、cwd=worktree、`import model` 解析到候选 model.py。
    #    **零改 train.py**（不变量2），git 无关，symlink 故轻量（大 dataset 也只 symlink 不拷）。
    #    （注：PYTHONPATH 影子对"train.py 与 model.py 同目录 + import model"无效——
    #     sys.path[0]=脚本目录优先于 PYTHONPATH，会静默训成 baseline。故 worktree 为可靠主路径。）
    try:
        env = os.environ.copy()
        if args.gpus:
            gpus = [g for g in re.split(r"[,\s]+", args.gpus) if g]
            env["CUDA_VISIBLE_DEVICES"] = gpus[cand_idx % len(gpus)]
        wt = _shallow_worktree(args.project_root, rec["model_file"], os.path.join(out_dir, "wt"))
        rec["isolation"] = "shallow_worktree"
        proc = subprocess.run(
            args.train_command, shell=True, cwd=wt,
            capture_output=True, text=True, env=env,
        )
        acc = _parse_accuracy(proc.stdout) if proc.returncode == 0 else None
        if acc is None:
            raise RuntimeError(f"train exit={proc.returncode}; stderr tail: {proc.stderr[-400:]}")
        rec["accuracy"] = acc
        rec["status"] = "SUCCESS"
    except Exception as e:
        rec["fail_reason"] = f"train: {type(e).__name__}: {e}"
        rec["status"] = "FAIL_train"

    print(json.dumps(rec, ensure_ascii=False))
    return rec


def _main() -> int:
    p = argparse.ArgumentParser(description="并行候选评测启动器（PYTHONPATH 影子 + worktree-lite fallback）")
    p.add_argument("--candidates", required=True, help="逗号分隔或 glob 的候选 model.py 路径")
    p.add_argument("--train_command", required=True)
    p.add_argument("--project_root", required=True, help="train_command 的 cwd")
    p.add_argument("--build_fn", default="build_model")
    p.add_argument("--dummy_input", required=True)
    p.add_argument("--opset", type=int, default=17)
    p.add_argument("--latency_provider", required=True)
    p.add_argument("--out_dir", required=True)
    p.add_argument("--gpus", default="", help="逗号分隔 GPU id，按候选轮转 pin")
    p.add_argument("--max_parallel", type=int, default=0, help="0=全部并行")
    p.add_argument("--train_filter_latency", default="false", help="true=时延门过滤后再训练")
    p.add_argument("--champion_latency_ms", default="", help="时延门基准")
    args = p.parse_args()

    # 解析候选列表（逗号分隔，支持 glob）
    import glob
    cands = []
    for tok in re.split(r"\s*,\s*", args.candidates):
        tok = tok.strip()
        if not tok:
            continue
        cands.extend(glob.glob(tok) if any(c in tok for c in "*?[") else [tok])
    if not cands:
        print("[]", file=sys.stderr)
        return 2
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)

    # 类名一致性校验（workflow 保证同类名；不一致则 warn 不阻塞）
    classes = {extract_model_class(c) for c in cands}
    if len(classes) > 1:
        print(f"[warn] 候选类名不一致 {classes}（workflow 约定同类名）", file=sys.stderr)

    n = len(cands)
    workers = n if args.max_parallel <= 0 else min(args.max_parallel, n)
    results = []
    t0 = __import__("time").perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(evaluate_one, args, i, c): i for i, c in enumerate(cands)}
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                traceback.print_exc()
                results.append({"id": f"c{futs[f]}", "status": "FAIL", "fail_reason": str(e)})
    dt = __import__("time").perf_counter() - t0

    results.sort(key=lambda r: int(r.get("id", "c0")[1:]))
    summary = {
        "n_candidates": n,
        "wall_clock_s": round(dt, 2),
        "successes": sum(1 for r in results if r.get("status") == "SUCCESS"),
        "best_latency_ms": min((r["latency_ms"] for r in results if r.get("latency_ms") is not None), default=None),
        "class_names": sorted(classes),
    }
    for r in results:
        print(f"CANDIDATE: {json.dumps(r, ensure_ascii=False)}")
    print(f"SUMMARY: {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
