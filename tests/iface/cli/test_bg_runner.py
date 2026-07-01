"""test_bg_runner.py —— daemon ``--background`` 模式单测（SPEC §8 P3.2 daemon）。

测试策略（SPEC §10.2 item11 / 测试约束）：

  **不真 fork detached**：CI 留孤儿进程 flaky + 不可重现。``daemonize`` 把所有副作用
  原语（fork/setsid/execv/redirect/time）抽成可注入 callable。单测：
    - parent 分支：``fork_fn=lambda: 12345`` → 返回 12345，metadata 已写，setsid/execv 未调。
    - child 分支：``fork_fn=lambda: 0`` → setsid/redirect/execv 都是 spy/mock，验证被调顺序 +
      env 设置，不真 detach / 不真 execv（避免 CI 留孤儿）。

  覆盖 intent（非仅 behavior）：
    - metadata roundtrip：``write_meta`` → ``read_meta`` 字段全保留。
    - run_id 传播：detached child 经 ``ENV_BG_RUN_ID`` 拿同一 run_id（确定性）。
    - dead pid 检测：``effective_status`` 把 status=running + pid 死 → crashed（fail loud）。
    - argv 构造：``build_child_argv`` 重 exec ``python -m orca run <yaml>``（不带 --background）。
    - 非 Unix 拒绝：无 ``os.fork`` → RuntimeError。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from orca.iface.cli.bg_runner import (
    ENV_BG_RUN_ID,
    ORCA_RUNS_DIR,
    TERMINAL_STATUSES,
    BgRunMeta,
    build_child_argv,
    daemonize,
    default_tape_path,
    effective_status,
    list_all_meta,
    log_dir,
    log_path,
    mark_terminal_status,
    pid_alive,
    read_meta,
    wait_for_terminal,
    write_meta,
)


# ── fixture：把 ORCA_RUNS_DIR 指到 tmp_path（隔离 ~/.orca 污染）─────────────────


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path, monkeypatch):
    """把 ``ORCA_RUNS_DIR`` 重定向到 tmp_path/runs，单测不污染用户 ~/.orca。

    ``daemonize`` / ``write_meta`` 等都从模块级 ``ORCA_RUNS_DIR`` 读路径；monkeypatch
    替换模块属性即可隔离。每个 test 独立 tmp_path，无交叉。
    """
    fake_dir = tmp_path / "orca_runs"
    monkeypatch.setattr("orca.iface.cli.bg_runner.ORCA_RUNS_DIR", fake_dir)
    return fake_dir


# ── BgRunMeta 序列化 roundtrip ─────────────────────────────────────────────────


class TestBgRunMetaSerialization:
    """``BgRunMeta.to_dict`` → ``from_dict`` 字段全保留 + 状态派生。"""

    def test_metadata_write_read_roundtrip(self, _isolated_runs_dir):
        """INTENT：write_meta 落盘的字段 read_meta 能完整还原（schema 不漂移）。

        roundtrip 必须保留 run_id/pid/yaml_path/started_at/log_path/tape_path/status 全 7 字段，
        任何一个丢失都是 metadata schema 回归（``ps``/``wait`` 据此判状态）。
        """
        meta = BgRunMeta(
            run_id="demo-20260701-120000-abc123",
            pid=12345,
            yaml_path="/abs/examples/demo.yaml",
            started_at=1782904868.0,
            log_path=str(_isolated_runs_dir / "demo-20260701-120000-abc123" / "log"),
            tape_path="runs/demo-20260701-120000-abc123.jsonl",
            status="running",
        )
        write_meta(meta)

        restored = read_meta(meta.run_id)
        assert restored is not None
        assert restored.run_id == meta.run_id
        assert restored.pid == meta.pid
        assert restored.yaml_path == meta.yaml_path
        assert restored.started_at == meta.started_at
        assert restored.log_path == meta.log_path
        assert restored.tape_path == meta.tape_path
        assert restored.status == "running"

    def test_to_dict_from_dict_all_fields_preserved(self):
        """to_dict/from_dict 字段级全保留（不依赖文件 IO，纯序列化层）。"""
        meta = BgRunMeta(
            run_id="r1", pid=42, yaml_path="/x.yaml", started_at=1.0,
            log_path="/l", tape_path="/t", status="completed",
        )
        restored = BgRunMeta.from_dict(meta.to_dict())
        assert restored == meta

    def test_from_dict_tolerates_unknown_fields(self):
        """未知字段忽略（宽容老 metadata 升级后不崩 ``ps``）。"""
        obj = {
            "run_id": "r1", "pid": 1, "yaml_path": "/x", "started_at": 1.0,
            "log_path": "/l", "tape_path": "/t", "status": "running",
            "future_field": "ignore me",
        }
        meta = BgRunMeta.from_dict(obj)
        assert meta.run_id == "r1"

    def test_with_status_derives_new_instance(self):
        """``with_status`` 派生新实例，原实例不变（frozen 语义）。"""
        meta = BgRunMeta(
            run_id="r", pid=1, yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",
        )
        done = meta.with_status("completed")
        assert done.status == "completed"
        assert meta.status == "running"  # 原不变
        assert done.run_id == meta.run_id  # 其余字段透传


# ── 路径解析 ──────────────────────────────────────────────────────────────────


class TestPathResolution:
    """run_id → metadata/log/tape 路径解析（与 commands._resolve_tape_path 一致）。"""

    def test_meta_path_under_runs_dir(self, _isolated_runs_dir):
        """meta_path(run_id) = ORCA_RUNS_DIR/<run_id>.json（含 monkeypatch 后的 runs dir）。"""
        from orca.iface.cli.bg_runner import meta_path

        assert meta_path("r1") == _isolated_runs_dir / "r1.json"

    def test_log_path_under_run_id_subdir(self, _isolated_runs_dir):
        """log_path(run_id) = ORCA_RUNS_DIR/<run_id>/log（子目录隔离每个 run 的日志）。"""
        assert log_path("r1") == _isolated_runs_dir / "r1" / "log"
        assert log_dir("r1") == _isolated_runs_dir / "r1"

    def test_default_tape_path_matches_orca_app_convention(self):
        """default_tape_path 与 OrcaApp 的 ``runs/<run_id>.jsonl`` 约定一致（DRY）。

        INTENT：detached child 必须把 tape 写到 ``ps``/``resume`` 都能找到的标准位置，
        否则 ``resume`` 后续接不上。这是 SPEC §10.2 item10 的硬约束。
        """
        assert default_tape_path("r1") == Path("runs") / "r1.jsonl"


# ── metadata 读写 ─────────────────────────────────────────────────────────────


class TestMetaReadWrite:
    """write_meta / read_meta / list_all_meta / mark_terminal_status。"""

    def test_read_meta_missing_returns_none(self, _isolated_runs_dir):
        """文件不存在 → None（不 raise，``ps`` 调用方判 None 走 not-found 路径）。"""
        assert read_meta("nonexistent") is None

    def test_read_meta_corrupt_returns_none(self, _isolated_runs_dir, caplog):
        """损坏 JSON → None + warning（不 crash，``ps`` 跳过坏文件继续列其它 run）。"""
        path = _isolated_runs_dir / "bad.json"
        _isolated_runs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert read_meta("bad") is None
        assert "损坏" in caplog.text

    def test_list_all_meta_empty_when_dir_missing(self, _isolated_runs_dir):
        """目录不存在 → 空列表（``ps`` 显示「无 background run」）。"""
        assert list_all_meta() == []

    def test_list_all_meta_lists_all_json(self, _isolated_runs_dir):
        """``~/.orca/runs/*.json`` 全部列出（``ps`` 扫目录的核心契约）。"""
        _isolated_runs_dir.mkdir(parents=True, exist_ok=True)
        for rid in ["run-a", "run-b", "run-c"]:
            write_meta(BgRunMeta(
                run_id=rid, pid=1, yaml_path="/x", started_at=1.0,
                log_path="/l", tape_path="/t", status="running",
            ))
        # 写一个 .txt 干扰文件（不应被列）+ 一个坏 .json（应被跳过）。
        (_isolated_runs_dir / "noise.txt").write_text("x")
        (_isolated_runs_dir / "broken.json").write_text("{bad")

        metas = list_all_meta()
        ids = sorted(m.run_id for m in metas)
        assert ids == ["run-a", "run-b", "run-c"]

    def test_write_meta_is_atomic_via_rename(self, _isolated_runs_dir):
        """原子写：tmp 文件写完后 rename，不留 .tmp 残留（``ps`` 不半读）。"""
        meta = BgRunMeta(
            run_id="r1", pid=1, yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",
        )
        write_meta(meta)
        assert (_isolated_runs_dir / "r1.json").is_file()
        # 无 .tmp 残留（os.replace 后 tmp 已 rename 走）。
        assert not list(_isolated_runs_dir.glob("*.tmp"))

    def test_mark_terminal_status_updates_existing(self, _isolated_runs_dir):
        """child 终结调 mark_terminal_status → metadata.status 变 completed。"""
        meta = BgRunMeta(
            run_id="r1", pid=999, yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",
        )
        write_meta(meta)
        mark_terminal_status("r1", "completed")
        restored = read_meta("r1")
        assert restored.status == "completed"
        assert restored.pid == 999  # pid 等字段不变

    def test_mark_terminal_status_missing_meta_silent(self, _isolated_runs_dir, caplog):
        """metadata 不存在 → 静默跳过 + warning（child 已在退出路径，再 raise 掩盖 exit code）。"""
        with caplog.at_level("WARNING"):
            mark_terminal_status("nonexistent", "completed")
        assert "metadata 不存在" in caplog.text or "跳过" in caplog.text


# ── pid 存活检测 + effective_status ────────────────────────────────────────────


class TestPidAliveAndEffectiveStatus:
    """``pid_alive`` + ``effective_status`` —— ``ps`` 标 crashed 的核心逻辑。"""

    def test_pid_alive_self_pid_exists(self):
        """当前进程的 pid 当然存活（os.kill(self, 0) 不抛）。"""
        assert pid_alive(os.getpid()) is True

    def test_pid_alive_invalid_pid_false(self):
        """pid <= 0 → False（占位值不可能存活）。"""
        assert pid_alive(0) is False
        assert pid_alive(-1) is False

    def test_pid_alive_dead_pid_false(self):
        """一个几乎不可能在用的超大 pid → ProcessLookupError → False。"""
        # 取一个远超 /proc/sys/kernel/pid_max 的值（Linux 默认 4194304）。
        assert pid_alive(99999999) is False

    def test_effective_status_terminal_unchanged(self):
        """status 已 terminal（completed/failed/crashed）→ 原样返回，不查 pid。"""
        meta = BgRunMeta(
            run_id="r", pid=os.getpid(), yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="completed",
        )
        assert effective_status(meta) == "completed"

    def test_effective_status_running_pid_dead_marks_crashed(self):
        """INTENT（fail loud）：status=running 但 pid 已死 → crashed。

        这是 SPEC §10.2 item11 的硬约束——child 崩溃未及更新 metadata 时，``ps`` 必须
        把它标 crashed（而非静默显示 running 误导用户）。不能静默吞错（铁律 4）。
        """
        meta = BgRunMeta(
            run_id="r", pid=99999999, yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",  # meta 说 running
        )
        assert effective_status(meta) == "crashed"

    def test_effective_status_running_pid_alive_running(self):
        """status=running + pid 还在 → running（正常情况）。"""
        meta = BgRunMeta(
            run_id="r", pid=os.getpid(), yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",
        )
        assert effective_status(meta) == "running"

    def test_terminal_statuses_constant(self):
        """TERMINAL_STATUSES 含 completed/failed/crashed（``wait`` 据此判可退出）。"""
        assert set(TERMINAL_STATUSES) == {"completed", "failed", "crashed"}


# ── build_child_argv ──────────────────────────────────────────────────────────


class TestBuildChildArgv:
    """``build_child_argv`` 构造 detached child 重新 exec 的 argv。"""

    def test_argv_contains_run_subcommand_and_yaml(self):
        """argv 含 ``run`` 子命令 + yaml 路径（不带 ``--background``）。

        入口可能是 ``orca`` console script 或 ``python -m orca.iface.cli.commands``
        （据 ``shutil.which`` 决定）；只锁契约：含 ``run`` + yaml + 不含 ``--background``。
        """
        argv = build_child_argv(Path("/abs/x.yaml"), [])
        assert "run" in argv
        assert "/abs/x.yaml" in argv
        # 关键：argv 不含 --background / -b（child 不再 detach）。
        assert "--background" not in argv
        assert "-b" not in argv

    def test_argv_prefers_orca_script_when_installed(self, monkeypatch):
        """``orca`` 在 PATH → 用 ``orca`` console script（真安装态入口）。"""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/orca")
        argv = build_child_argv(Path("x.yaml"), [])
        assert argv[0] == "/fake/bin/orca"
        assert argv[1] == "run"

    def test_argv_falls_back_to_python_module_when_orca_missing(self, monkeypatch):
        """``orca`` 不在 PATH → fallback ``python -m orca.iface.cli.commands``。"""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        argv = build_child_argv(Path("x.yaml"), [])
        assert argv[0] == sys.executable
        assert argv[1:4] == ["-m", "orca.iface.cli.commands", "run"]

    def test_argv_passes_extra_flags(self):
        """``-i`` / ``--max-iter`` 等非 --background flag 原样透传给 child。"""
        argv = build_child_argv(
            Path("x.yaml"),
            ["-i", "k=v", "--max-iter", "5"],
        )
        assert "-i" in argv and "k=v" in argv
        assert "--max-iter" in argv and "5" in argv
        # 关键：argv 不含 --background（child 不再 detach）。
        assert "--background" not in argv and "-b" not in argv


# ── daemonize seam：parent 分支 ────────────────────────────────────────────────


class TestDaemonizeParentBranch:
    """parent 分支：fork 返回 pid>0 → 立即返回 pid + 写 metadata（含真 pid）。"""

    def test_daemonize_parent_returns_pid_and_writes_meta(self, _isolated_runs_dir):
        """fork_fn 返回 12345 → daemonize 返回 12345 + metadata.pid=12345 + status=running。"""
        # parent 分支：fork 立即返回 pid，不进 child 路径。
        meta = daemonize(
            Path("/abs/x.yaml"),
            run_id="r-parent",
            extra_argv=[],
            fork_fn=lambda: 12345,
            setsid_fn=lambda: None,  # parent 不该调 setsid
            execv_fn=lambda *a: pytest.fail(
                "parent 分支不应调 execv"
            ),
            redirect_stdio_fn=lambda p: pytest.fail(
                "parent 分支不应 redirect stdio"
            ),
            time_fn=lambda: 1000.0,
        )
        assert meta == 12345
        restored = read_meta("r-parent")
        assert restored is not None
        assert restored.pid == 12345
        assert restored.status == "running"
        assert restored.started_at == 1000.0
        assert restored.yaml_path == "/abs/x.yaml"

    def test_daemonize_parent_creates_log_dir(self, _isolated_runs_dir):
        """parent 在 fork 前就 mkdir log_dir（保证 child 一 setsid 就能 open log）。"""
        daemonize(
            Path("x.yaml"), run_id="r-logdir", extra_argv=[],
            fork_fn=lambda: 1,
            setsid_fn=lambda: None,
            execv_fn=lambda *a: None,
            time_fn=lambda: 0.0,
        )
        # log_dir 已被创建（daemonize 内 log_file.parent.mkdir）。
        assert log_dir("r-logdir").is_dir()

    def test_daemonize_rejects_non_unix(self, _isolated_runs_dir, monkeypatch):
        """无 os.fork → RuntimeError（Windows 拒绝，SPEC §8.2）。"""
        monkeypatch.delattr(os, "fork", raising=False)
        with pytest.raises(RuntimeError, match="Unix"):
            daemonize(Path("x.yaml"), run_id="r-win", extra_argv=[])


# ── daemonize seam：child 分支 ─────────────────────────────────────────────────


class TestDaemonizeChildBranch:
    """child 分支：fork 返回 0 → setsid → redirect stdio → set env → execv。"""

    def test_daemonize_child_calls_setsid_redirect_env_execv(self, _isolated_runs_dir, monkeypatch):
        """fork 返回 0（child）→ setsid/redirect/execv 都被调，env 含 ORCA_BG_RUN_ID。

        INTENT：验证 child 路径的副作用顺序与 env 传播（确定性 run_id 经 env 传 child）。
        不真 detach（全部 fn 是 spy），故 CI 不留孤儿。

        ``execv`` 语义：真实 execv 成功则**永不返回**（进程镜像被替换）。spy 模拟此语义——
        调完副作用后 raise 一个 sentinel exception，让 daemonize 不走到 ``raise RuntimeError``
        （那个是 execv 失败才到，语义不同）。
        """
        calls: list[str] = []
        monkeypatch.setenv(ENV_BG_RUN_ID, "")  # 清空，验证 daemonize 自己设

        class _ExecvCalled(Exception):
            """sentinel：模拟 execv 成功替换进程镜像后控制流不再返回。"""

        def spy_execv(exe, argv):
            calls.append(f"execv:{exe}")
            calls.append(f"argv:{argv}")
            raise _ExecvCalled  # 进程镜像已换，控制流止于此

        with pytest.raises(_ExecvCalled):
            daemonize(
                Path("/abs/x.yaml"),
                run_id="r-child",
                extra_argv=["-i", "k=v"],
                fork_fn=lambda: 0,  # child 分支
                setsid_fn=lambda: calls.append("setsid"),
                redirect_stdio_fn=lambda p: calls.append(f"redirect:{p.name}"),
                execv_fn=spy_execv,
                time_fn=lambda: 0.0,
            )

        # setsid 必须被调（detach controlling terminal），且先于 execv（detach 完才 execv）。
        assert "setsid" in calls
        assert any(c.startswith("redirect:") for c in calls)
        execv_idx = next(i for i, c in enumerate(calls) if c.startswith("execv:"))
        setsid_idx = calls.index("setsid")
        redirect_idx = next(i for i, c in enumerate(calls) if c.startswith("redirect:"))
        # 语义顺序（非硬锁实现细节）：setsid → redirect → execv（detach → 重定向 → 换镜像）。
        assert setsid_idx < redirect_idx < execv_idx

        # env 已设成 run_id（child 经 env 拿到，OrcaApp 复用）。
        assert os.environ.get(ENV_BG_RUN_ID) == "r-child"

        # argv 透传 extra_argv（-i k=v）+ 含 yaml。
        argv_call = next(c for c in calls if c.startswith("argv:"))
        argv = argv_call.split(":", 1)[1]
        assert "/abs/x.yaml" in argv and "k=v" in argv

    def test_daemonize_child_env_propagates_run_id(self, _isolated_runs_dir, monkeypatch):
        """run_id 经 ENV_BG_RUN_ID 传 child —— OrcaApp 据 env 复用，保 tape/metadata 一致。"""
        monkeypatch.setenv(ENV_BG_RUN_ID, "stale-should-be-overwritten")

        class _ExecvCalled(Exception):
            pass

        with pytest.raises(_ExecvCalled):
            daemonize(
                Path("x.yaml"), run_id="fresh-id", extra_argv=[],
                fork_fn=lambda: 0,
                setsid_fn=lambda: None,
                redirect_stdio_fn=lambda p: None,
                execv_fn=lambda *a: (_ for _ in ()).throw(_ExecvCalled),
            )
        assert os.environ[ENV_BG_RUN_ID] == "fresh-id"


# ── wait_for_terminal ─────────────────────────────────────────────────────────


class TestWaitForTerminal:
    """``wait_for_terminal`` 轮询逻辑（``wait`` 命令核心）。"""

    def test_wait_returns_immediately_if_already_terminal(self, _isolated_runs_dir):
        """meta.status 已 terminal → 立即返回，不轮询。"""
        write_meta(BgRunMeta(
            run_id="r-done", pid=1, yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="completed",
        ))
        slept: list[float] = []

        status, meta = wait_for_terminal(
            "r-done", sleep_fn=lambda s: slept.append(s),
        )
        assert status == "completed"
        assert meta is not None and meta.run_id == "r-done"
        assert slept == []  # 已 terminal，没 sleep

    def test_wait_returns_none_meta_if_not_found(self, _isolated_runs_dir):
        """run_id 无 metadata → 返回 (crashed, None)；调用方据 meta is None 判 not-found。"""
        status, meta = wait_for_terminal("nonexistent", sleep_fn=lambda s: None)
        assert meta is None
        assert status == "crashed"  # not-found 也归 crashed（terminal）

    def test_wait_polls_until_terminal(self, _isolated_runs_dir):
        """status=running → 轮询，第 3 次时 meta 变 completed → 返回。

        模拟：sleep_fn 第 2 次被调时把 metadata 改成 completed（代表 child 跑完更新了）。
        验证 wait 真的轮询了（sleep 被调），且最终拿到 terminal。
        """
        write_meta(BgRunMeta(
            run_id="r-poll", pid=os.getpid(), yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",
        ))
        call_count = {"n": 0}

        def fake_sleep(s):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # 第 2 次 sleep 后，child 跑完了，更新 metadata。
                mark_terminal_status("r-poll", "completed")

        status, meta = wait_for_terminal("r-poll", sleep_fn=fake_sleep)
        assert status == "completed"
        assert call_count["n"] >= 2  # 至少轮询了 2 次

    def test_wait_timeout_returns_current_status(self, _isolated_runs_dir):
        """``--timeout`` 到了仍在 running → 返回当前 status（非 terminal）。"""
        write_meta(BgRunMeta(
            run_id="r-timeout", pid=os.getpid(), yaml_path="/x", started_at=1.0,
            log_path="/l", tape_path="/t", status="running",
        ))
        # time_fn 递增模拟时间流逝；让它第 2 次就超时。
        t = {"v": 0.0}

        def fake_time():
            t["v"] += 100.0
            return t["v"]

        status, meta = wait_for_terminal(
            "r-timeout", timeout=50.0,
            sleep_fn=lambda s: None, time_fn=fake_time,
        )
        assert status == "running"  # 还在 running（未 terminal）
        assert meta is not None


# ── OrcaApp run_id 复用（ENV_BG_RUN_ID）────────────────────────────────────────


class TestOrcaAppReusesBgRunId:
    """OrcaApp 读 ``ORCA_BG_RUN_ID`` 复用 run_id（detached child 确定性）。"""

    def test_orca_app_uses_env_run_id_when_set(self, monkeypatch):
        """ENV_BG_RUN_ID 存在 → OrcaApp.run_id 用它，不重新 gen（保一致性）。"""
        from orca.iface.cli.app import OrcaApp
        from orca.schema import Route, ScriptNode, Workflow

        monkeypatch.setenv(ENV_BG_RUN_ID, "bg-fixed-id-123")
        wf = Workflow(
            name="t", entry="a",
            nodes=[ScriptNode(name="a", command="echo", routes=[Route(to="$end")])],
        )
        app = OrcaApp(wf, inputs={}, tape_path=Path("/tmp/fake.jsonl"))
        assert app.run_id == "bg-fixed-id-123"

    def test_orca_app_gens_run_id_when_env_absent(self, monkeypatch):
        """ENV_BG_RUN_ID 不存在 → 走 gen_run_id（foreground 正常路径，向后兼容）。"""
        from orca.iface.cli.app import OrcaApp
        from orca.run.lifecycle import gen_run_id
        from orca.schema import Route, ScriptNode, Workflow

        monkeypatch.delenv(ENV_BG_RUN_ID, raising=False)
        wf = Workflow(
            name="t", entry="a",
            nodes=[ScriptNode(name="a", command="echo", routes=[Route(to="$end")])],
        )
        app = OrcaApp(wf, inputs={}, tape_path=Path("/tmp/fake.jsonl"))
        # run_id 应是 gen_run_id("t") 的产物（含 slug + 时间戳 + nanoid）。
        assert app.run_id.startswith("t-")
        assert app.run_id != "bg-fixed-id-123"
