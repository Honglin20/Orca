#!/usr/bin/env python3
"""run_skill_benchmark.py —— headless 公平评测 create-workflow skill。

对每个 benchmark case：在干净 workspace 里放入 skill（项目级 .claude/skills/）+ 该 case 的
input/assets，用 opencode headless 跑 skill（只喂用户请求，不泄露 expected），收集产出的
workflow.yaml + agents/，跑 orca validate，写结构化报告。

公平性：本脚本只读 case 的 input.txt + assets/；绝不读 expected/。expected 比对交给独立子 agent。
用法：
  python scripts/run_skill_benchmark.py                 # 跑全部 case
  python scripts/run_skill_benchmark.py 01 03 11        # 跑指定 case（按目录前缀匹配）
  python scripts/run_skill_benchmark.py --run-id custom # 指定 run 目录名
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES = REPO / "orca" / "skills" / "create-workflow" / "benchmark" / "cases"
SKILL_SRC = REPO / "orca" / "skills" / "create-workflow"
# workspace 必须在 repo 之外：opencode 按 git root 定项目根，若 workspace 在 repo 内，
# 相对路径 ./workflows/ 会解析到仓库根、污染源码。放系统 temp（无 .git 祖先）→ 项目根=workspace。
RUNS = Path("/tmp") / "orca-skill-bench"

MODEL = os.environ.get("BENCH_MODEL", "deepseek/deepseek-v4-flash")
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT", "480"))


def run_case(case_dir: Path, run_root: Path) -> dict:
    name = case_dir.name
    ws = run_root / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)

    # 1) skill 放项目级 .claude/skills/（opencode 会发现并按 description 自动触发）
    #    🔴 公平性：必须排除 benchmark/（含 expected 答案 + case 不变量），否则 skill 能读自己的答案。
    skill_dst = ws / ".claude" / "skills" / "create-workflow"
    shutil.copytree(SKILL_SRC, skill_dst, ignore=shutil.ignore_patterns("benchmark"))
    assert not any(skill_dst.rglob("expected")), f"{name}: workspace skill 仍含 expected/（答案泄露！）"

    # 2) 该 case 的 input/assets（公平：只这些，无 expected）
    prompt = (case_dir / "input.txt").read_text().strip()
    assets = case_dir / "assets"
    if assets.exists():
        shutil.copytree(assets, ws / "assets")

    # 3) opencode headless 跑（cwd=workspace）
    log_path = ws.parent / f"{name}.opencode.log"
    started = time.time()
    timed_out = False
    try:
        with open(log_path, "w") as logf:
            proc = subprocess.run(
                ["opencode", "run", "-m", MODEL, "--dangerously-skip-permissions", prompt],
                cwd=ws, stdout=logf, stderr=subprocess.STDOUT, timeout=TIMEOUT_S,
            )
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        exit_code = -1
        timed_out = True
    elapsed = round(time.time() - started, 1)

    # 4) 定位产出的 workflow.yaml + agents/（排除 .claude/ skill 源、assets/ 输入）
    produced_yaml = None
    for p in sorted(ws.rglob("*.yaml")):
        rel = p.relative_to(ws)
        if rel.parts and rel.parts[0] in {".claude", "assets"}:
            continue
        produced_yaml = p
        break
    produced_agents = []
    for cand in [ws / "agents", ws / "workflows" / "agents"]:
        if cand.is_dir():
            produced_agents = [str(p.relative_to(ws)) for p in cand.rglob("*") if p.is_file()]

    # 5) validate 产出的 workflow（若存在）
    validate_ok = None
    validate_msg = ""
    if produced_yaml:
        v = subprocess.run(
            ["orca", "validate", str(produced_yaml)],
            capture_output=True, text=True,
        )
        validate_ok = v.returncode == 0
        validate_msg = (v.stdout + v.stderr).strip()

    return {
        "case": name,
        "workspace": str(ws),
        "log": str(log_path),
        "elapsed_s": elapsed,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "produced_workflow": str(produced_yaml.relative_to(ws)) if produced_yaml else None,
        "produced_agents": produced_agents,
        "validate_ok": validate_ok,
        "validate_msg": validate_msg,
    }


def main() -> int:
    # 解析 args：支持 --run-id=X / --run-id X 两种形式；其余非选项 token 是 case 选择器
    args = sys.argv[1:]
    run_id = "default"
    sel = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--run-id" and i + 1 < len(args):
            run_id = args[i + 1]
            i += 2
        elif a.startswith("--run-id="):
            run_id = a.split("=", 1)[1]
            i += 1
        elif a.startswith("--"):
            i += 1  # 忽略未知选项
        else:
            sel.append(a)
            i += 1
    run_root = RUNS / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    # 刷新全局 install：opencode 会加载 ~/.config/opencode/skills/ 的全局 skill（优先级高于
    # 项目级 .claude/skills/），故先同步全局 == 当前 repo 源，确保测的是最新 skill。
    # 🔴 公平性：orca skill install 已排除 benchmark/（install 侧 ignore），全局不含答案。
    print("⟳ 刷新全局 skill install (orca skill install)...", flush=True)
    inst = subprocess.run(["orca", "skill", "install"], capture_output=True, text=True)
    if inst.returncode != 0:
        print("✗ orca skill install 失败，中止：\n", inst.stdout + inst.stderr, file=sys.stderr)
        return 1

    cases = sorted(d for d in CASES.iterdir() if (d / "input.txt").exists())
    if sel:
        cases = [c for c in cases if any(c.name.startswith(s) or s in c.name for s in sel)]

    results = []
    for c in cases:
        print(f"▶ {c.name} ...", flush=True)
        r = run_case(c, run_root)
        results.append(r)
        tag = "✓" if (r["validate_ok"]) else ("?" if r["produced_workflow"] else "✗")
        print(f"  {tag} validate_ok={r['validate_ok']} wf={r['produced_workflow']} "
              f"agents={len(r['produced_agents'])} {r['elapsed_s']}s"
              f"{' TIMEOUT' if r['timed_out'] else ''}", flush=True)

    report = run_root / "report.json"
    report.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nreport → {report}")
    n_ok = sum(1 for r in results if r["validate_ok"])
    print(f"{n_ok}/{len(results)} produced a validating workflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
