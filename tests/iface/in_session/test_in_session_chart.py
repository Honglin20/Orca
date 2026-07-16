"""tests/iface/in_session/test_in_session_chart.py —— in-session 路径 chart 端到端集成测试。

覆盖意图（SPEC phase-13 §3 in-session 衔接，验收标准 1/3/4/5）：
  - bootstrap detach 起守护 + 写 env 文件（5 var）+ socket bind 就绪
  - 节点子代理（测试用 subprocess 模拟宿主派发：source env 文件 → 跑 script）调
    ``render_chart`` → 守护收 → tape 出 ``custom(chart)`` 事件 + node/session_id 正确
  - 并行两 run（不同 run_id → 不同 socket + 不同守护）互不串台
  - folder-agent 节点的 env 文件含 ``ORCA_AGENT_RESOURCES``，子代理 source 后可访问资源
  - 守护在 run 终态自退 + socket 清理

测试模型：in-session 是「主 session 派子代理」模型，本测试用 ``subprocess.run(['bash','-c',
'source <env>; python <script>'])`` 模拟宿主派的子代理侧（fresh shell + source env + 跑 script）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.chart._paths import chart_sock_path
from orca.events.tape import Tape
from orca.iface.in_session.cli import app

# in-session 路径的子代理模拟用此 Python（保证 ``from orca.chart import render_chart`` 可 import）。
_ORCA_PY = sys.executable


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir 到 tmp_path，让 runs/ 写到临时目录（隔离真实 repo 的 runs/）。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def chart_push_script(tmp_path: Path) -> Path:
    """script：调 ``render_chart`` 推一张 line chart（模拟 subagent 跑的 viz/训练脚本）。"""
    p = tmp_path / "push_chart.py"
    p.write_text(textwrap.dedent('''
        from orca.chart import render_chart
        seq = render_chart(
            chart_type="line",
            data=[{"x": 1, "y": 2.0}, {"x": 2, "y": 1.5}],
            label="training", title="loss", x="x", y="y",
        )
        print(f"PUSHED seq={seq}")
    '''), encoding="utf-8")
    return p


@pytest.fixture
def cleanup_leftover_sockets():
    """测试前后清理 ``/tmp/orca-*.sock`` 残留（防跨测试污染 / daemon 未退干净）。"""
    import glob
    before = set(glob.glob("/tmp/orca-*.sock"))
    yield
    after = glob.glob("/tmp/orca-*.sock")
    # 不动 before 已有的（其它进程的），只清理本测试进程新出现且仍存在 30s+ 的（保守）
    # 实际依赖：每个测试终态时 daemon 自退 → socket 自动清；此 fixture 是兜底。
    for sock in after:
        if sock not in before:
            try:
                os.unlink(sock)
            except OSError:
                pass


def _bootstrap(runner: CliRunner, wf: Path) -> dict:
    r = runner.invoke(app, ["bootstrap", str(wf)])
    assert r.exit_code == 0, f"bootstrap exit {r.exit_code}: {r.output}"
    # r.output 可能含 logging 噪音（CliRunner 多 invoke 的 stderr 关闭 artifacts）；取首行 JSON。
    return json.loads(_first_json_line(r.output))


def _next(runner: CliRunner, run_id: str, output: str | None, *, expect_exit: int = 0) -> dict:
    args = ["next", "--run-id", run_id]
    if output is not None:
        args += ["--output", output]
    r = runner.invoke(app, args)
    assert r.exit_code == expect_exit, f"next exit {r.exit_code} (expected {expect_exit}): {r.output}"
    return json.loads(_first_json_line(r.output))


def _first_json_line(s: str) -> str:
    """取输出里第一个看起来像 JSON 的行（跳过 logging error 噪音 / traceback）。"""
    for line in s.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return line
    raise AssertionError(f"未在输出中找到 JSON 行：{s!r}")


def _simulate_subagent(env_path: Path, script: Path) -> subprocess.CompletedProcess:
    """模拟宿主派的子代理：fresh shell + source env + 跑 script（subagent 侧的 viz/训练脚本）。

    子代理照抄 prompt 里的 ``source <env>`` 一行（字面），shell 即获 ORCA_* 身份；script 调
    ``render_chart`` → 连自己 run 的 socket → 守护 emit custom(chart) → tape。
    """
    return subprocess.run(
        ["bash", "-c", f"set -e; source {env_path}; {_ORCA_PY} {script}"],
        capture_output=True, text=True, timeout=30,
    )


def _wait_sock_ready(env_path: Path, *, timeout: float = 10.0) -> None:
    """测试侧等守护 socket 就绪（防 bootstrap 的 5s ``_SOCK_READY_TIMEOUT`` 在 CI 高负载下不够）。

    读 env 文件里的 ``ORCA_CHART_SOCK`` 路径，poll exists 到 timeout。若超时仍无 socket，
    让后续 subagent 调 ``render_chart`` 时 fail loud（test assert 会捕获）—— 这是真实生产
    失败模式，不应在测试里静默吞掉。
    """
    import time as _time
    env_content = env_path.read_text(encoding="utf-8")
    sock_str = None
    for line in env_content.splitlines():
        if line.startswith("export ORCA_CHART_SOCK="):
            # 形如 ``export ORCA_CHART_SOCK=/tmp/orca-abc.sock``；剥前缀 + shlex.quote
            sock_str = line.split("=", 1)[1].strip().strip("'\"")
            break
    assert sock_str, f"env 文件未含 ORCA_CHART_SOCK：{env_content!r}"
    sock_path = Path(sock_str)
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if sock_path.exists():
            return
        _time.sleep(0.05)
    # 超时不 fail —— 让 subagent 调用本身 fail loud（更接近真实失败模式）


# ── 基础：bootstrap 起 daemon + 写 env 文件 + socket 就绪 ─────────────────────


def test_bootstrap_spawns_daemon_and_writes_env(cwd_tmp, cleanup_leftover_sockets):
    """bootstrap 后：env 文件含 5 var（folder-agent 含 ORCA_AGENT_RESOURCES）+ socket 存在。"""
    # folder-agent workflow（验证 ORCA_AGENT_RESOURCES 也写进 env 文件）
    (cwd_tmp / "agents" / "worker").mkdir(parents=True)
    (cwd_tmp / "agents" / "worker" / "agent.md").write_text(
        "---\ndescription: worker\n---\n你是 worker。\n", encoding="utf-8",
    )
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: env_check_wf
        description: env file check
        entry: worker
        nodes:
          - name: worker
            kind: agent
            agent: worker
            model: deepseek/deepseek-v4-flash
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    reply = _bootstrap(runner, wf)

    run_id = reply["run_id"]
    env_path = cwd_tmp / "runs" / run_id / "orca_env.sh"
    assert env_path.is_file(), f"env 文件未写出：{env_path}"

    env_content = env_path.read_text(encoding="utf-8")
    assert f"ORCA_RUN_ID={run_id}" in env_content
    assert "ORCA_NODE=worker" in env_content
    assert "ORCA_SESSION_ID=" in env_content
    sock_path = chart_sock_path(run_id)
    assert f"ORCA_CHART_SOCK={sock_path}" in env_content
    # folder-agent → ORCA_AGENT_RESOURCES 指向 agents/worker 绝对路径
    assert f"ORCA_AGENT_RESOURCES=" in env_content
    assert str((cwd_tmp / "agents" / "worker").resolve()) in env_content

    # socket 应已在 bootstrap 等 _wait_for_sock 期间就绪
    assert sock_path.exists(), f"socket 未 bind：{sock_path}"

    # 收尾：让 daemon 自退（推进到 workflow_completed）
    _next(runner, run_id, "worker output")
    # 等 daemon 终态感知 + 退出（poll 2s 一次，给 5s 余量）
    _wait_sock_gone(sock_path, timeout=8.0)


def test_inline_prompt_node_unsets_resources(cwd_tmp, cleanup_leftover_sockets):
    """inline-prompt 节点（resources_root=None）→ env 文件 ``unset ORCA_AGENT_RESOURCES``
    （清潜在 stale，防同一 shell 内前一次 source 残留）。"""
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: inline_wf
        description: inline prompt
        entry: a
        nodes:
          - name: a
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "做 A。"
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    reply = _bootstrap(runner, wf)
    env_path = cwd_tmp / "runs" / reply["run_id"] / "orca_env.sh"
    env_content = env_path.read_text(encoding="utf-8")
    assert "unset ORCA_AGENT_RESOURCES" in env_content
    assert "export ORCA_AGENT_RESOURCES" not in env_content

    # 收尾
    _next(runner, reply["run_id"], "A done")
    _wait_sock_gone(chart_sock_path(reply["run_id"]), timeout=8.0)


# ── 核心验收 1：render_chart → tape ─────────────────────────────────────────


def test_in_session_chart_lands_in_tape(
    cwd_tmp, chart_push_script, cleanup_leftover_sockets,
):
    """in-session 路径下节点子代理调 render_chart → tape 出 custom(chart) 事件。

    SPEC phase-13 §3 in-session 衔接核心验收：bootstrap 起 daemon + 写 env →
    模拟 subagent（source env + 跑 script）→ render_chart → 守护 emit → tape。
    """
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: chart_wf
        description: in-session chart e2e
        entry: worker
        nodes:
          - name: worker
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "推一张图。"
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    reply = _bootstrap(runner, wf)
    run_id = reply["run_id"]
    env_path = cwd_tmp / "runs" / run_id / "orca_env.sh"

    # 等守护 socket 就绪（CI 高负载下 bootstrap 的 5s 可能不够）
    _wait_sock_ready(env_path)
    # 模拟 subagent：source env + 跑 push_chart script
    res = _simulate_subagent(env_path, chart_push_script)
    assert res.returncode == 0, (
        f"subagent script 失败：stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "PUSHED seq=" in res.stdout

    # 推进 workflow 到终态
    reply_done = _next(runner, run_id, "worker output")
    assert reply_done["done"] is True, f"workflow 未完成：{reply_done}"

    # 断言 tape 含 custom(chart)
    tape_path = cwd_tmp / "runs" / f"{run_id}.jsonl"
    tape = Tape(tape_path, run_id=run_id)
    events = list(tape.replay())
    chart_events = [
        e for e in events if e.type == "custom" and e.data.get("kind") == "chart"
    ]
    assert len(chart_events) == 1, (
        f"应只有 1 个 chart 事件；got {len(chart_events)}；"
        f"event types={[e.type for e in events]}"
    )
    ev = chart_events[0]
    # node / session_id 路由正确（来自 env 文件）
    assert ev.node == "worker"
    assert ev.session_id and len(ev.session_id) >= 16
    # chart payload 字段对
    chart = ev.data["chart"]
    assert chart["chart_type"] == "line"
    assert chart["label"] == "training"
    assert chart["title"] == "loss"
    assert len(chart["data"]) == 2

    # 守护应已自退（终态事件被 _watch_terminal 捕获）+ socket 清理
    _wait_sock_gone(chart_sock_path(run_id), timeout=8.0)


# ── 核心验收 3：并行 run 不串台 ───────────────────────────────────────────────


def test_parallel_in_session_runs_no_cross_talk(
    cwd_tmp, chart_push_script, cleanup_leftover_sockets,
):
    """两并行 in-session run（不同 run_id → 不同 socket + 不同守护）chart 不串台。

    意图：run_id 键控 socket / 守护 / env 文件；run A 的 subagent 只能连 runA.sock，
    不可能误推到 run B 的 tape。SPEC §2.4 铁律 #2 兑现（in-session 衔接层）。
    """
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: parallel_wf
        description: parallel cross-talk check
        entry: worker
        nodes:
          - name: worker
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "推图。"
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    # 起 run A
    reply_a = _bootstrap(runner, wf)
    run_a = reply_a["run_id"]
    env_a = cwd_tmp / "runs" / run_a / "orca_env.sh"
    # 起 run B（同 wf 但不同 run_id，dupe-check 不拒：第一条未终态，但 in-session dupe-check
    # 按同 wf 已活跃 run 拒；故先让 A 进 mid-flight 后再起 B 不现实——直接用两个不同 wf 名）
    wf2 = cwd_tmp / "wf2.yaml"
    wf2.write_text(wf.read_text().replace("parallel_wf", "parallel_wf_b"), encoding="utf-8")
    reply_b = _bootstrap(runner, wf2)
    run_b = reply_b["run_id"]
    env_b = cwd_tmp / "runs" / run_b / "orca_env.sh"

    assert run_a != run_b
    assert chart_sock_path(run_a) != chart_sock_path(run_b)

    # 等两守护 socket 就绪（CI 高负载下 bootstrap 的 5s 可能不够）
    _wait_sock_ready(env_a)
    _wait_sock_ready(env_b)
    # 两 subagent 各推一张（用各自 env 文件）
    res_a = _simulate_subagent(env_a, chart_push_script)
    res_b = _simulate_subagent(env_b, chart_push_script)
    assert res_a.returncode == 0 and res_b.returncode == 0
    assert "PUSHED" in res_a.stdout and "PUSHED" in res_b.stdout

    # 各自推进到终态
    _next(runner, run_a, "a out")
    _next(runner, run_b, "b out")

    # 断言：A tape 只含 A 的 chart；B 同理
    def _charts(run_id):
        tape = Tape(cwd_tmp / "runs" / f"{run_id}.jsonl", run_id=run_id)
        return [
            e for e in tape.replay()
            if e.type == "custom" and e.data.get("kind") == "chart"
        ]

    charts_a = _charts(run_a)
    charts_b = _charts(run_b)
    assert len(charts_a) == 1, f"run A 应 1 chart；got {len(charts_a)}"
    assert len(charts_b) == 1, f"run B 应 1 chart；got {len(charts_b)}"

    # 收尾
    _wait_sock_gone(chart_sock_path(run_a), timeout=8.0)
    _wait_sock_gone(chart_sock_path(run_b), timeout=8.0)


# ── 核心验收 5：folder-agent + ORCA_AGENT_RESOURCES 资源定位 ─────────────────────


def test_folder_agent_resources_accessible_via_env(
    cwd_tmp, cleanup_leftover_sockets,
):
    """folder-agent 的 subagent 经 ``$ORCA_AGENT_RESOURCES`` 访问自带脚本。

    SPEC phase-14：folder-agent 的 agent.md body 引用 ``$ORCA_AGENT_RESOURCES/scripts/x.py``；
    web 路径 executor spawn 时注入 env。in-session 路径没人注入 → env 文件补此缺口。
    本测试验证：env 文件 ``ORCA_AGENT_RESOURCES`` 指向 folder-agent 根，subagent source 后
    能真访问 ``$ORCA_AGENT_RESOURCES/scripts/<file>`` 并推 chart。
    """
    # folder-agent：agents/viz/ + scripts/demo.py（demo.py 调 render_chart，引用环境身份）
    viz_dir = cwd_tmp / "agents" / "viz"
    viz_scripts = viz_dir / "scripts"
    viz_scripts.mkdir(parents=True)
    (viz_dir / "agent.md").write_text(
        "---\ndescription: viz agent\n---\n你是 viz agent，运行 $ORCA_AGENT_RESOURCES/scripts/demo.py 推图。\n",
        encoding="utf-8",
    )
    demo = viz_scripts / "demo.py"
    demo.write_text(textwrap.dedent('''
        from orca.chart import render_chart
        render_chart(
            chart_type="bar",
            data=[{"k": "a", "v": 3}, {"k": "b", "v": 5}],
            label="viz", title="bars", x="k", y="v",
        )
        print("VIZ_OK")
    '''), encoding="utf-8")

    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: folder_viz_wf
        description: folder-agent + ORCA_AGENT_RESOURCES
        entry: viz
        nodes:
          - name: viz
            kind: agent
            agent: viz
            model: deepseek/deepseek-v4-flash
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    reply = _bootstrap(runner, wf)
    run_id = reply["run_id"]
    env_path = cwd_tmp / "runs" / run_id / "orca_env.sh"

    # env 文件含 ORCA_AGENT_RESOURCES 指向 agents/viz 绝对路径
    env_content = env_path.read_text(encoding="utf-8")
    assert "ORCA_AGENT_RESOURCES=" in env_content
    assert str(viz_dir.resolve()) in env_content

    # 等守护 socket 就绪（CI 高负载下 bootstrap 的 5s 可能不够）
    _wait_sock_ready(env_path)
    # 模拟 subagent：source env + 用 $ORCA_AGENT_RESOURCES/scripts/demo.py 跑（与 agent.md body 一致）
    res = subprocess.run(
        ["bash", "-c",
         f"set -e; source {env_path}; {_ORCA_PY} \"$ORCA_AGENT_RESOURCES/scripts/demo.py\""],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, (
        f"subagent 经 $ORCA_AGENT_RESOURCES 跑 demo 失败："
        f"stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "VIZ_OK" in res.stdout

    # 推进 + 断言 chart 落 tape（chart_type=bar，title=bars）
    _next(runner, run_id, "viz done")
    tape = Tape(cwd_tmp / "runs" / f"{run_id}.jsonl", run_id=run_id)
    charts = [e for e in tape.replay()
              if e.type == "custom" and e.data.get("kind") == "chart"]
    assert len(charts) == 1, f"应 1 chart；got {len(charts)}"
    assert charts[0].data["chart"]["chart_type"] == "bar"
    assert charts[0].data["chart"]["title"] == "bars"
    assert charts[0].node == "viz"

    _wait_sock_gone(chart_sock_path(run_id), timeout=8.0)


# ── 核心验收：run 中途守护被杀 → next respawn ─────────────────────────────────


def _kill_chart_daemon_for_run(run_id: str) -> bool:
    """SIGKILL 本 run 的 chart 守护（测试专用，模拟 ``pkill opencode`` 误杀 detached 守护）。

    按 ``/proc/<pid>/cmdline`` 匹配 ``orca.iface.in_session.chart_daemon`` + 本 run_id
    （``--run-id <run_id>`` 是 cmdline 里的唯一 arg）。SIGKILL 不跑 finally unlink → socket
    文件残留（stale），正是 respawn 补丁要处理的场景。返是否杀了至少一个。

    非 Linux（无 ``/proc``）→ skip 返 False（项目 POSIX-only，CI 必 Linux；本地跳过由测试
    本身的 ``skipif`` 守）。
    """
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return False
    run_id_b = run_id.encode("utf-8")
    killed = False
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if b"orca.iface.in_session.chart_daemon" not in cmdline:
            continue
        if run_id_b not in cmdline:
            continue
        try:
            os.kill(int(entry.name), signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed


def test_next_respawns_killed_chart_daemon(
    cwd_tmp, chart_push_script, cleanup_leftover_sockets,
):
    """守护在 run 中途被 SIGKILL → 下次 ``orca next`` 探到无监听者 → respawn → 后续 chart 落 tape。

    意图（SPEC phase-13 §3 in-session 衔接 respawn 验收）：bootstrap spawn 守护**一次**即退出；
    run 中途守护被杀（``pkill opencode`` 顺带 SIGTERM/SIGKILL 了 detached 守护）后，恢复 run 的
    ``orca next`` 必须探到守护不在并 respawn，否则后续节点 subagent 的 ``render_chart`` 连不上
    socket、chart 全丢（实测一次 run 0 chart）。

    本测试用 SIGKILL（最严：不跑 finally unlink → stale socket 残留）验：
      1. 杀前守护活（connect 探 True）；
      2. 杀后守护死（connect 探 False，socket stale）；
      3. ``orca next`` 推进 → 探到死 → respawn → 守护重新活（connect 探 True）；
      4. respawn 后的守护真能收 chart（subagent push → tape 出 custom(chart)）。
    """
    if not Path("/proc").is_dir():
        pytest.skip("respawn 测试依赖 /proc 扫描定位守护（CI Linux 必过，本地非 Linux 跳过）")

    from orca.iface.in_session.cli import _chart_daemon_alive

    # 2 节点 wf：worker → checker → $end（next 推进 worker 后还有 checker 节点 → 触发 respawn 守卫）。
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: respawn_wf
        description: daemon respawn after kill
        entry: worker
        nodes:
          - name: worker
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "做 W。"
            routes:
              - to: checker
          - name: checker
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "做 C。"
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    reply = _bootstrap(runner, wf)
    run_id = reply["run_id"]
    env_path = cwd_tmp / "runs" / run_id / "orca_env.sh"
    sock = chart_sock_path(run_id)

    # 等守护 socket 就绪（CI 高负载下 bootstrap 的 5s 可能不够）
    _wait_sock_ready(env_path)
    assert _chart_daemon_alive(sock), "bootstrap 后守护应活"

    # 模拟 run 中途守护被 SIGKILL（pkill opencode 误杀）
    assert _kill_chart_daemon_for_run(run_id), "未找到本 run 的守护进程（/proc 扫描失败）"
    # 等进程真退：SIGKILL 是异步的（os.kill 返回后 kernel 仍需调度回收 + 关 listening fd）。
    # poll connect 探判死，而非等 ``sock.exists()``（SIGKILL 不跑 finally unlink → 文件残留 stale，
    # exists() 一直 True）。connect 转 refused 才标志 listener fd 真被 kernel 收掉。
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _chart_daemon_alive(sock):
            break
        time.sleep(0.05)
    # 关键断言 1：杀后 connect 探判 dead（socket 文件 stale 残留但无监听者）
    assert not _chart_daemon_alive(sock), "SIGKILL 后守护应判 dead（connect refused）"

    # orca next 推进 worker → checker：next 应探到守护死并 respawn
    reply2 = _next(runner, run_id, "worker output")
    assert reply2.get("node") == "checker", f"next 应推进到 checker；got {reply2}"
    assert not reply2["done"], "workflow 不应结束（还有 checker）"

    # 关键断言 2：respawn 后守护重新活（next 的 _ensure_chart_daemon 拉起）
    _wait_sock_ready(env_path)
    assert _chart_daemon_alive(sock), "next 后守护应被 respawn（connect 探 True）"

    # 关键断言 3：respawn 的守护真能收 chart（下一节点 subagent push → tape）
    res = _simulate_subagent(env_path, chart_push_script)
    assert res.returncode == 0, (
        f"respawn 后 subagent 推 chart 失败：stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "PUSHED" in res.stdout, "render_chart 应成功（守护已 respawn）"

    # 推进到终态
    _next(runner, run_id, "checker output")

    # 断言 tape 含 custom(chart)（respawn 的守护真写了）
    tape_path = cwd_tmp / "runs" / f"{run_id}.jsonl"
    tape = Tape(tape_path, run_id=run_id)
    charts = [e for e in tape.replay()
              if e.type == "custom" and e.data.get("kind") == "chart"]
    assert len(charts) == 1, f"respawn 后应 1 chart 落 tape；got {len(charts)}"
    assert charts[0].node == "checker", "chart 路由到 checker 节点（env 文件按 checker 身份）"

    _wait_sock_gone(sock, timeout=8.0)


# ── 守卫生存检查的负向守卫（终态 / no-marker 不 respawn）──────────────────────


def test_next_does_not_respawn_when_terminal(
    cwd_tmp, cleanup_leftover_sockets, monkeypatch,
):
    """next 推到终态（done）时不调 ``_ensure_chart_daemon``（无下一节点；守护由终态事件自退）。

    意图：调用点守卫 ``result.node is not None and not (result.done or compliance_failed)``。
    终态 next（done=True）应跳过 respawn。monkeypatch 记录 ``_ensure_chart_daemon`` 调用，
    断言终态 next 不触发 —— 固化「终态 / no-marker 不该 respawn」防回归（守护会叠进程）。
    """
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: terminal_guard_wf
        description: terminal next no respawn
        entry: worker
        nodes:
          - name: worker
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "做 W。"
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    reply = _bootstrap(runner, wf)
    run_id = reply["run_id"]
    env_path = cwd_tmp / "runs" / run_id / "orca_env.sh"
    _wait_sock_ready(env_path)

    respawn_calls: list = []
    import orca.iface.in_session.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_ensure_chart_daemon",
                        lambda *a, **kw: respawn_calls.append((a, kw)))

    reply_done = _next(runner, run_id, "worker output")
    assert reply_done["done"] is True, f"应终态；got {reply_done}"
    assert respawn_calls == [], "终态 next 不应调 _ensure_chart_daemon（守护会由终态事件自退）"

    _wait_sock_gone(chart_sock_path(run_id), timeout=8.0)


# ── 守护自退 + socket 清理 ────────────────────────────────────────────────────


def _wait_sock_gone(sock_path: Path, *, timeout: float = 10.0) -> None:
    """等守护自退后 socket 文件消失（终态事件触发 _watch_terminal 退出 → ingestor cancel →
    finally unlink）。容忍 10s（_WATCH_POLL_SECONDS=2s 的 ~5 个 poll 周期 + cleanup 余量）。

    **超时即 fail 测试**：守护自退是 SPEC phase-13 §3.1 in-session 衔接的硬契约（防泄漏）。
    静默 unlink 会掩盖回归（如 _watch_terminal partial-line race 漏检终态 → 守护 6h 才 TTL 退）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not sock_path.exists():
            return
        time.sleep(0.2)
    # 兜底清理（防跨测试污染）+ fail loud
    try:
        sock_path.unlink()
    except OSError:
        pass
    pytest.fail(
        f"chart 守护在终态后 {timeout}s 内未自退 + 清理 socket（{sock_path}）。"
        f"可能原因：_watch_terminal 漏检终态事件 / partial-line race / daemon crash。"
    )
