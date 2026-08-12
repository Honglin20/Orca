#!/usr/bin/env python3
"""v3 single-node in-session driver (opencode+deepseek backend, stall-detected).

Drives ONE node of nas-supernet-v3 run: sources orca_env.sh, spawns `opencode run`
that Reads the node prompt file + works + emits one-line JSON, with db-wal stall
detection + retry. Prints the captured node output JSON to stdout (for feeding to
`orca next`). Full opencode log saved to <log_dir>/<node>_<attempt>.log.

Usage: python3 v3_drive_node.py <run_id> <node> [prompt_file]
  If prompt_file omitted, defaults to runs/<run_id>/prompts/<node>.md.

Env: STALL_SEC (default 540), MAX_ATTEMPTS (default 4).
"""
import json, os, subprocess, sys, time, pathlib, re

RUN_ID = sys.argv[1]
NODE = sys.argv[2]
PROMPT_FILE = sys.argv[3] if len(sys.argv) > 3 else f"/mnt/d/Projects/Orca/runs/{RUN_ID}/prompts/{NODE}.md"
STALL_SEC = int(os.environ.get("STALL_SEC", "540"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "4"))
OPENCODE_MODEL = "deepseek/deepseek-v4-flash"
DB_WAL = pathlib.Path("/home/mozzie/.local/share/opencode/opencode.db-wal")
RUNS_DIR = pathlib.Path("/mnt/d/Projects/Orca/runs")
LOG_DIR = pathlib.Path("/tmp/ns3-insession")
LOG_DIR.mkdir(parents=True, exist_ok=True)

def log(m): print(f"[drive:{NODE}] {m}", flush=True)

def source_env(run_id):
    env = os.environ.copy()
    sh = RUNS_DIR / run_id / "orca_env.sh"
    if sh.exists():
        # Use bash explicitly: `source` is a bashism, /bin/sh (shell=True) silently ignores it.
        r = subprocess.run(["bash", "-c", f"set -a; source {sh}; env"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    else:
        log(f"WARN: {sh} missing")
    return env

def run_once(env, log_file):
    directive = (
        f"你是 nas-supernet-v3 的一个节点 agent（节点：{NODE}）。"
        f" 先 Read 节点指令文件 {PROMPT_FILE} 并严格按它执行（该文件含完整步骤 + output_schema）。"
        " 先 source 运行环境（若指令要求），cd \"$ORCA_ARTIFACTS_DIR\"。按节点指令完成全部工作（生成/校验/执行/自愈）。"
        " 你的最终回复必须**只是一行合法 JSON**——即该节点 output_schema 的产出（前后不加任何文字/解释/代码围栏）。"
        " 若节点是长任务（训练/搜索）且未完成，按指令返回挂起标记（含『请勿调用 orca next』）。"
        " 非指令工作（探索仓库/读 yaml/git status）禁做。"
    )
    cmd = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions",
           "--auto", "--model", OPENCODE_MODEL, directive]
    proc = subprocess.Popen(cmd, stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
                            cwd="/mnt/d/Projects/Orca", env=env, start_new_session=True)
    last_mtime = DB_WAL.stat().st_mtime if DB_WAL.exists() else time.time()
    since_change = time.time()
    while True:
        try:
            proc.wait(timeout=30); break
        except subprocess.TimeoutExpired:
            if proc.poll() is not None: break
            try: m = DB_WAL.stat().st_mtime
            except Exception: m = last_mtime
            if m != last_mtime:
                last_mtime = m; since_change = time.time()
            elif time.time() - since_change > STALL_SEC:
                log(f"  STALL: db-wal unchanged {int(time.time()-since_change)}s → kill+retry")
                try: proc.kill()
                except Exception:
                    try: os.kill(proc.pid, 9)
                    except Exception: pass
                proc.wait(timeout=10)
                return (-1, "STALL")
    rc = proc.returncode
    final_text = ""
    try:
        for line in open(log_file, errors="replace"):
            line = line.strip()
            if not line: continue
            try: ev = json.loads(line)
            except Exception: continue
            part = ev.get("part") or {}
            if ev.get("type") == "text" or part.get("type") == "text":
                final_text = part.get("text", final_text)
    except Exception as e:
        log(f"  log parse err: {e}")
    return (rc, final_text)

def main():
    env = source_env(RUN_ID)
    log(f"prompt={PROMPT_FILE} ORCA_CHART_SOCK set={'ORCA_CHART_SOCK' in env} artifacts={env.get('ORCA_ARTIFACTS_DIR','?')}")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log_file = str(LOG_DIR / f"{NODE}_{attempt}.log")
        log(f"attempt {attempt}/{MAX_ATTEMPTS} → {log_file}")
        rc, final_text = run_once(env, log_file)
        log(f"rc={rc} final_text_len={len(final_text)} preview={final_text[:160]!r}")
        if rc == 0 and final_text.strip():
            mm = re.search(r"\{[\s\S]*\}", final_text)
            out = mm.group(0) if mm else final_text.strip()
            # write captured output to a file for orca next
            out_file = LOG_DIR / f"{NODE}_output.json"
            out_file.write_text(out)
            log(f"CAPTURED ({len(out)} chars) → {out_file}")
            print("===NODE_OUTPUT_BEGIN===")
            print(out)
            print("===NODE_OUTPUT_END===")
            return 0
        if rc != -1:  # not a stall; opencode exited cleanly but no text → retry
            log(f"clean exit no text, retrying")
    log(f"HARD-FAILED after {MAX_ATTEMPTS}")
    return 1

if __name__ == "__main__":
    sys.exit(main())
