#!/usr/bin/env python3
"""Per-node in-session driver for nas-supernet-v2 with stall detection + retry.

Drives one orca run node-by-node. Each node = one opencode invocation (small
context = less stall risk). Monitors opencode db-wal mtime; on stall (>STALL_SEC
no db-wal update), kills + retries the node (up to MAX_NODE_RETRIES). Captures
the opencode final message as the node output, feeds to orca next to advance.

Usage: python3 per_node_driver.py <run_id> <start_output>
  run_id: orca run id (already bootstrapped)
  start_output: output JSON string to feed the FIRST orca next (empty string for
                the very first node = no --output; the bootstrap already armed it)

Env: sources <run_dir>/orca_env.sh per node for ORCA_* (chart socket, artifacts).
"""
import json, os, subprocess, sys, time, pathlib, re

RUN_ID = sys.argv[1]
START_OUTPUT = sys.argv[2] if len(sys.argv) > 2 else ""
STALL_SEC = int(os.environ.get("STALL_SEC", "480"))   # 8min no db-wal update = stall
MAX_NODE_RETRIES = int(os.environ.get("MAX_NODE_RETRIES", "5"))
OPENCODE_MODEL = "deepseek/deepseek-v4-flash"
DB_WAL = pathlib.Path("/home/mozzie/.local/share/opencode/opencode.db-wal")
RUNS_DIR = pathlib.Path("/mnt/d/Projects/Orca/runs")

def log(m):
    print(f"[driver] {m}", flush=True)

def orca_next(output: str):
    """Call orca next; return parsed JSON dict (or None on error).
    orca emits INFO log lines before the JSON; find the JSON line."""
    cmd = ["orca", "next", "--run-id", RUN_ID]
    if output:
        cmd += ["--output", output]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd="/mnt/d/Projects/Orca")
    if r.returncode != 0:
        log(f"orca next rc={r.returncode} stderr={r.stderr[:300]}")
    # find the JSON object line in stdout (skip INFO log lines)
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    # fallback: try whole stdout
    try:
        return json.loads(r.stdout)
    except Exception as e:
        log(f"orca next JSON parse fail: {e}; stdout[:200]={r.stdout[:200]}")
        return None

def source_env(run_id):
    """Source the run's orca_env.sh (updated per node by orca) → env dict."""
    env = os.environ.copy()
    # find orca_env.sh in run dir
    for d in [RUNS_DIR / run_id, pathlib.Path("/mnt/d/Projects/runs") / run_id]:
        sh = d / "orca_env.sh"
        if sh.exists():
            r = subprocess.run(f"set -a; source {sh}; env", shell=True, capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
            break
    return env

def run_opencode_once(prompt_text, prompt_path, env, log_file):
    """One opencode invocation executing the node. Returns (rc, final_text)."""
    read_clause = (f" 先 Read 节点指令文件 {prompt_path} 并严格按它执行。" if prompt_path else "")
    directive = (
        "你是 nas-supernet-v2 的一个节点 agent。"
        + read_clause
        + " 先 cd \"$ORCA_ARTIFACTS_DIR\"。按节点指令完成全部工作（生成/校验/执行）。"
        " 你的最终回复必须**只是一行合法 JSON**——即该节点 output_schema 的产出（前后不加任何文字/解释/代码围栏）。"
        " 非指令工作（探索仓库/读 yaml/git status）禁做。"
    )
    cmd = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions",
           "--auto", "--model", OPENCODE_MODEL, directive]
    proc = subprocess.Popen(cmd, stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
                            cwd="/mnt/d/Projects/Orca", env=env, start_new_session=True)
    # monitor db-wal for stall
    last_mtime = DB_WAL.stat().st_mtime
    since_change = time.time()
    while True:
        try:
            rc = proc.wait(timeout=30)
            # process exited
            break
        except subprocess.TimeoutExpired:
            if proc.poll() is not None:
                break
            # check db-wal
            try:
                m = DB_WAL.stat().st_mtime
            except Exception:
                m = last_mtime
            if m != last_mtime:
                last_mtime = m
                since_change = time.time()
            elif time.time() - since_change > STALL_SEC:
                log(f"  STALL: db-wal unchanged {int(time.time()-since_change)}s → kill+retry")
                # 🔴 定向 kill：只杀 opencode PID（SIGKILL），NOT os.killpg（杀进程组会连带杀
                # chart daemon → live custom(chart) 事件断流）。opencode 的子进程（含 chart daemon）
                # 成孤儿被 init 收养、继续跑——chart daemon 存活，charts 正常推。
                try:
                    proc.kill()
                except Exception:
                    try:
                        os.kill(proc.pid, 9)
                    except Exception:
                        pass
                proc.wait(timeout=10)
                return (-1, "STALL")
    rc = proc.returncode
    # extract final agent text message from opencode --format json output
    final_text = ""
    try:
        for line in open(log_file, errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            # opencode json: agent text emitted as {"type":"text","part":{"type":"text","text":...}}
            part = ev.get("part") or {}
            if ev.get("type") == "text" or part.get("type") == "text":
                final_text = part.get("text", final_text)
    except Exception as e:
        log(f"  log parse err: {e}")
    return (rc, final_text)

def main():
    output = START_OUTPUT
    node_count = 0
    while True:
        resp = orca_next(output)
        if resp is None:
            log("orca next failed; aborting"); return 2
        if resp.get("done"):
            log(f"WORKFLOW DONE: {json.dumps(resp)[:300]}")
            return 0
        # recoverable = previous output bad; re-arm gives a new prompt with context
        node = resp.get("node") or "?"
        # node prompt file is at predictable path
        prompt_path = f"/mnt/d/Projects/Orca/runs/{RUN_ID}/prompts/{node}.md"
        if not pathlib.Path(prompt_path).exists():
            # fallback: regex search the response
            m = re.search(r"(/mnt/d/Projects/Orca/runs/[^\s'\"]+\.md)", str(resp))
            prompt_path = m.group(1) if m else ""
        log(f"--- NODE {node_count}: {node} (prompt_path={str(prompt_path)[:80]}) ---")
        env = source_env(RUN_ID)
        for attempt in range(1, MAX_NODE_RETRIES + 1):
            log(f"  attempt {attempt}/{MAX_NODE_RETRIES}")
            log_file = f"/tmp/ns2-insession/node_{node_count}_attempt{attempt}.log"
            rc, final_text = run_opencode_once(None, prompt_path, env, log_file)
            log(f"  rc={rc} final_text_len={len(final_text)} preview={final_text[:120]!r}")
            if rc == 0 and final_text.strip():
                output = final_text.strip()
                # extract JSON object if wrapped
                mm = re.search(r"\{[\s\S]*\}", final_text)
                if mm:
                    output = mm.group(0)
                log(f"  captured node output ({len(output)} chars)")
                node_count += 1
                break
            log(f"  retrying (rc={rc})...")
        else:
            log(f"NODE {node} HARD-FAILED after {MAX_NODE_RETRIES} retries")
            return 1
        # loop continues: feed output to next orca next

if __name__ == "__main__":
    sys.exit(main())
