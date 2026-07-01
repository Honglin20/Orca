"""bg_runner.py —— daemon ``--background`` 模式（SPEC §8 / phase 11 P3.2）。

回答「长跑 workflow 怎么不占终端？」：``daemonize`` fork 出一个脱离终端的子进程，
父进程立即返回 run_id + pid；子进程 ``setsid`` 后重定向 stdin/stdout/stderr 到日志文件，
再次 exec ``orca run <yaml>``（不带 ``--background``）—— 子进程跑的就是普通的 foreground run，
只是 parentless 且 stdio 落到日志文件。

设计（SPEC §8.2 + 决策 D2）：

  - **Unix-only**：``os.fork`` + ``os.setsid``。CI 跑 ubuntu，dev 跑 darwin，都是 Unix；
    Windows 不在 phase 11 目标内（``RuntimeError`` fail loud，SPEC §8 未列 Windows）。
  - **复用 gen_run_id + Tape 路径**（DRY，SPEC §10.2 item10）：父进程生成 run_id，
    经 ``ORCA_BG_RUN_ID`` 环境变量传给子进程；子进程的 ``OrcaApp`` 看到 env 就用它，
    不重新 gen —— 保证 metadata 的 run_id 与 tape 文件名 / run_id 三者一致（确定性）。
  - **metadata 单文件 per run**：``~/.orca/runs/<run_id>.json`` 记 ``{run_id, pid, yaml_path,
    started_at, log_path, tape_path, status}``。``ps`` 扫目录列全部；``wait`` 读它判终态。
    child 完成时把 ``status`` 改成 completed/failed；若 child 崩溃未及更新，``ps`` 用
    ``pid_alive(pid)`` 检测出 dead，把显示状态标 ``crashed``（fail loud，SPEC §10.2 item11）。
  - **可测 seam**：``daemonize`` 把「写 metadata / fork / redirect / exec」拆成独立步骤；
    单测 mock ``os.fork`` 模拟 parent-return（立即返回 pid）vs child-path（不真 detach），
    不在 CI 留孤儿进程（SPEC §10.2 item11 / 测试约束）。

依赖单向：本模块在 ``iface/cli`` 层，只 import stdlib + ``orca.run.lifecycle.gen_run_id``
（同层 commands 依赖同模块）。不反向 import ``orca.run.orchestrator`` —— detached child 是
经 ``os.execv`` 重启一个全新 ``orca`` 进程跑的，不 in-process 调 orchestrator（彻底隔离）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from orca.run.lifecycle import gen_run_id

logger = __import__("logging").getLogger(__name__)

# ── 路径约定（与 commands._resolve_tape_path / OrcaApp tape_path 一致）─────────

#: 用户级 run 元数据根目录（``~/.orca/runs``）。production 路径，与 CWD 无关。
ORCA_RUNS_DIR = Path.home() / ".orca" / "runs"

#: 子进程拿到的 run_id 经此 env 传入（避免 OrcaApp 重新 gen，保确定性一致）。
ENV_BG_RUN_ID = "ORCA_BG_RUN_ID"

#: ``status`` 字段取值。running=子进程还在；completed/failed=子进程已正常终结并更新；
#: crashed=子进程未及更新 metadata 就死了（``ps`` 用 pid 检测出 dead 后置此值，仅显示用）。
RunStatus = Literal["running", "completed", "failed", "crashed"]

#: terminal status（``wait`` 据此判可退出）。crashed 也算 terminal（进程已没了）。
TERMINAL_STATUSES: tuple[RunStatus, ...] = ("completed", "failed", "crashed")


@dataclass(frozen=True)
class BgRunMeta:
    """单个 background run 的元数据（写 ``~/.orca/runs/<run_id>.json``）。

    frozen：``daemonize`` 构造 running 实例写盘，child 终结时读+改 status 重写
    （``with_status`` 派生新实例）。dataclass 而非 dict —— 字段显式、IDE 可发现、
    额外字段拒绝（``additionalProperties`` 等价，防 metadata schema 漂移）。
    """

    run_id: str
    pid: int
    yaml_path: str
    started_at: float
    log_path: str
    tape_path: str
    status: RunStatus = "running"
    # finished_at：进入 terminal status（completed/failed）时的时间戳。running 时 None。
    # ``ps`` 据此算 elapsed：running → now - started_at（实时增长）；terminal →
    # finished_at - started_at（固定，不再增长，避免「已完成的 run elapsed 还在涨」误导）。
    # 老 metadata（无此字段）→ None，``ps`` fallback 到 now - started_at（向后兼容）。
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "run_id": self.run_id,
            "pid": self.pid,
            "yaml_path": self.yaml_path,
            "started_at": self.started_at,
            "log_path": self.log_path,
            "tape_path": self.tape_path,
            "status": self.status,
        }
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at
        return d

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "BgRunMeta":
        """反序列化 + 宽容校验（未知字段忽略，避免老 metadata 升级后读崩）。"""
        return cls(
            run_id=str(obj["run_id"]),
            pid=int(obj["pid"]),
            yaml_path=str(obj["yaml_path"]),
            started_at=float(obj["started_at"]),
            log_path=str(obj["log_path"]),
            tape_path=str(obj["tape_path"]),
            status=obj.get("status", "running"),
            finished_at=obj.get("finished_at"),
        )

    def with_status(self, status: RunStatus, *, at: float | None = None) -> "BgRunMeta":
        """派生新实例（status 改写，可附带 finished_at）。

        用 ``dataclasses.replace`` —— 单一真相源派生机制，未来加字段（如 exit_code）
        只需改 dataclass 定义，所有 ``with_*`` 自动透传，无需逐个手写拷贝（DRY，铁律 6）。

        ``at``：标记终态的时间戳（``time.time()``）；进入 terminal status 时建议传，
        让 ``ps`` 显示固定 elapsed（而非随墙钟增长）。省略 → finished_at 不变（None 时
        ``ps`` fallback 到 now - started_at，向后兼容老 metadata）。
        """
        updates: dict[str, Any] = {"status": status}
        if at is not None:
            updates["finished_at"] = at
        return dataclasses.replace(self, **updates)


# ── 纯路径解析（DRY：commands._resolve_tape_path 同约定）─────────────────────────


def runs_meta_dir() -> Path:
    """``~/.orca/runs`` —— background run metadata 根目录。"""
    return ORCA_RUNS_DIR


def meta_path(run_id: str) -> Path:
    """单个 run 的 metadata 文件路径：``~/.orca/runs/<run_id>.json``。"""
    return ORCA_RUNS_DIR / f"{run_id}.json"


def log_dir(run_id: str) -> Path:
    """单个 run 的日志目录：``~/.orca/runs/<run_id>/``（子进程 stdout/stderr 落此）。"""
    return ORCA_RUNS_DIR / run_id


def log_path(run_id: str) -> Path:
    """单个 run 的日志文件：``~/.orca/runs/<run_id>/log``。"""
    return log_dir(run_id) / "log"


def default_tape_path(run_id: str) -> Path:
    """默认 tape 路径（与 OrcaApp / commands._resolve_tape_path 约定一致）。

    返回 ``runs/<run_id>.jsonl``（CWD 相对，production 用户在 repo 根跑，写到 ./runs/）。
    child process 经 env 拿同一 run_id，OrcaApp 看到 env 用同一 tape_path —— 三处一致。
    """
    return Path("runs") / f"{run_id}.jsonl"


# ── metadata 原子读写 ─────────────────────────────────────────────────────────


def write_meta(meta: BgRunMeta) -> None:
    """写 metadata 到 ``~/.orca/runs/<run_id>.json``（覆盖式，原子写）。

    覆盖语义：daemonize 时首次写（status=running），child 终结时再次写（status=终态）。
    原子写（tmp + rename）避免 ``ps`` 半读 —— ``BgRunMeta`` 字段固定，文件小，
    rename 在同 filesystem 内原子。
    """
    path = meta_path(meta.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def read_meta(run_id: str) -> BgRunMeta | None:
    """读单个 run 的 metadata。文件不存在 / 损坏 → None（宽容，``ps`` 跳过坏文件）。"""
    path = meta_path(run_id)
    if not path.is_file():
        return None
    try:
        return BgRunMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        # 损坏文件不阻断 ``ps`` —— 跳过并在上层 stderr warn（fail loud 但不 crash）。
        logger.warning("background run metadata 损坏，已跳过：%s", path)
        return None


def list_all_meta() -> list[BgRunMeta]:
    """扫 ``~/.orca/runs/*.json`` 列全部 background run metadata。"""
    if not ORCA_RUNS_DIR.is_dir():
        return []
    out: list[BgRunMeta] = []
    for p in sorted(ORCA_RUNS_DIR.glob("*.json")):
        try:
            out.append(BgRunMeta.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            logger.warning("background run metadata 损坏，已跳过：%s", p)
            continue
    return out


# ── pid 存活检测（``ps`` 标 crashed / ``wait`` 判进程已没）──────────────────────


def pid_alive(pid: int) -> bool:
    """检测 pid 是否还在跑。

    - pid <= 0 → 不可能存活（占位值），返回 False。
    - ``os.kill(pid, 0)`` 不抛 → 存在（存活或 zombie；zombie 仍占 pid slot，
      对 ``wait`` 而言进程已终结但 metadata 可能未及更新，调用方据语义再判）。
    - 抛 ``ProcessLookupError`` → 进程没了。
    - 抛 ``PermissionError`` → 进程在但不归当前用户管（仍算存活）。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 不是当前用户的进程，但确实存在 → 算存活。
        return True


def effective_status(meta: BgRunMeta) -> RunStatus:
    """结合 metadata.status + pid 存活判真实显示状态。

    - status 已是 terminal（completed/failed/crashed）→ 原样返回（child 已自报终态）。
    - status=running 且 pid 已死 → ``crashed``（child 未及更新 metadata 就崩，fail loud）。
    - status=running 且 pid 还在 → ``running``。
    """
    if meta.status in TERMINAL_STATUSES:
        return meta.status
    if not pid_alive(meta.pid):
        return "crashed"
    return "running"


# ── daemonize（fork detached child + 重定向 + 重 exec）─────────────────────────


def _assert_unix() -> None:
    """fork/setsid 是 Unix-only —— Windows 上立即 fail loud。"""
    if not hasattr(os, "fork"):
        raise RuntimeError(
            "background mode 需要 os.fork（Unix-only）；当前平台不支持。"
            "请在 Linux/macOS 跑 ``orca run --background``。"
        )


def build_child_argv(yaml_path: Path, extra_argv: list[str]) -> list[str]:
    """构造 detached child 重新 exec 的 argv。

    child 重跑 ``orca run <yaml> [extra...]``（**不带** ``--background``），
    经 ``ENV_BG_RUN_ID`` 拿到同一 run_id，故 OrcaApp 用同一 tape_path（确定性）。

    ``extra_argv`` 是父 ``orca run`` 收到的非 ``--background`` flag（如 ``-i`` / ``--max-iter``），
    原样透传给 child（保留用户全部 run 参数）。调用方已剥掉 ``--background``。

    入口选择（鲁棒性，两者都跑 ``orca.iface.cli.commands:main``）：
      1. 优先 ``orca`` console script（``shutil.which``，pip install 后在 venv bin）—— 真安装态，
         跑起来就是用户日常的 ``orca run``。
      2. fallback ``python -m orca.iface.cli.commands`` —— 模块入口稳定，``orca`` 不在 PATH
         时（dev ``pip install -e .`` 的某些场景 / 容器）也能跑。**不**用 ``python -m orca``
         （orca 包无 ``__main__.py``，``-m orca`` 报 ``'orca' is a package``）。
    """
    import shutil

    orca_script = shutil.which("orca")
    if orca_script is not None:
        return [orca_script, "run", str(yaml_path), *extra_argv]
    # fallback：直接跑含 main 的模块（不依赖 console script 安装态）。
    # 记 warning 让运维可定位「为何 detached child 用了 python -m 而非 orca 脚本」
    # （通常是 PATH 不含 venv/bin，或 pip install -e . 未跑过——fail-loud 信号，铁律 4）。
    logger.warning(
        "build_child_argv: orca console script 不在 PATH，fallback 到 "
        "``python -m orca.iface.cli.commands``（sys.executable=%s）。"
        "若 detached child 启动失败，检查 PATH 是否含 venv/bin。",
        sys.executable,
    )
    return [
        sys.executable, "-m", "orca.iface.cli.commands", "run", str(yaml_path), *extra_argv,
    ]


def daemonize(
    yaml_path: Path,
    run_id: str,
    extra_argv: list[str],
    *,
    fork_fn: Callable[[], int] = os.fork,
    setsid_fn: Callable[[], None] = os.setsid,
    execv_fn: Callable[[str, list[str]], None] = os.execv,
    redirect_stdio_fn: Callable[[Path], None] | None = None,
    time_fn: Callable[[], float] = time.time,
) -> int:
    """fork 出 detached child 跑 ``orca run <yaml>``；父进程立即返回 child pid。

    步骤（parent / child 分支）：
      1. 写 metadata（status=running）到 ``~/.orca/runs/<run_id>.json``。
      2. ``fork``：父进程（fork 返回 >0）直接返回 pid。
      3. 子进程（fork 返回 0）：
         a. ``setsid`` 脱离 controlling terminal（变 session leader）。
         b. 重定向 fd 0/1/2 到 ``~/.orca/runs/<run_id>/log``。
         c. 设 ``ENV_BG_RUN_ID=<run_id>`` env（OrcaApp 读 env 复用 run_id）。
         d. ``execv`` 重启成 ``python -m orca run <yaml>`` —— 当前进程镜像被替换，
            从此 child 就是普通的 foreground ``orca run``（只是 parentless + stdio 落日志）。

    **可测 seam**：所有副作用原语（``fork_fn`` / ``setsid_fn`` / ``execv_fn`` /
    ``redirect_stdio_fn`` / ``time_fn``）都是可注入的 callable。单测：
      - parent 分支：``fork_fn=lambda: 12345`` → 不真 fork，返回 12345，metadata 已写。
      - child 分支：``fork_fn=lambda: 0`` → 走 setsid + redirect + execv，但三个 fn 都是
        spy/mock，不真 detach / 不真 execv（避免 CI 留孤儿）。

    返回：父进程返回 child pid（>0）。

    raises:
      RuntimeError: 当前平台不支持 fork（Windows）。
    """
    _assert_unix()

    # 1) 构造 + 写 metadata（parent 在 fork 前写，保证 fork 后 parent/child 都能读到）。
    log_file = log_path(run_id)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    meta = BgRunMeta(
        run_id=run_id,
        pid=-1,  # 占位：fork 后才有真 pid，更新一次。
        yaml_path=str(yaml_path),
        started_at=time_fn(),
        log_path=str(log_file),
        tape_path=str(default_tape_path(run_id)),
        status="running",
    )
    write_meta(meta)

    # 2) fork。
    pid = fork_fn()

    if pid > 0:
        # 父进程：更新 metadata 的 pid（fork 前占位 -1），立即返回。status 仍是 running。
        write_meta(dataclasses.replace(meta, pid=pid))
        return pid

    # ── 以下仅 child 执行（fork 返回 0）──────────────────────────────────────
    # 3a. setsid：脱离 controlling terminal，成为新 session + process group leader。
    setsid_fn()

    # 3b. 重定向 stdio 到日志文件（fd 0/1/2 全指过去，子进程的 print / claude -p 的
    #     stream-json 全部落日志，``orca logs`` tail 它）。
    redirect = redirect_stdio_fn or _redirect_stdio_to_log
    redirect(log_file)

    # 3c. env 传 run_id（OrcaApp 读 env 复用，不重新 gen，保确定性一致）。
    os.environ[ENV_BG_RUN_ID] = run_id

    # 3d. execv：当前进程镜像替换成 ``python -m orca run <yaml>``。
    #     execv 成功则本函数永不 return（进程镜像已被替换）；失败才 return/raise。
    argv = build_child_argv(yaml_path, extra_argv)
    execv_fn(argv[0], argv)

    # execv 失败才会走到这（execv 成功时进程镜像已换，此行不执行）。fail loud。
    raise RuntimeError(
        f"daemonize: execv 返回（应永不返回）——argv={argv!r}。"
        "通常 sys.executable 路径错或 ``-m orca`` 找不到模块。"
    )


def _redirect_stdio_to_log(log_file: Path) -> None:
    """把 fd 0/1/2 重定向到 ``log_file``（append 模式，保留 child crash 时的最后输出）。

    分离 ``open`` 与 ``dup2``：先 open 拿新 fd，再 dup2 复制到 0/1/2，最后关新 fd
    （避免 fd 泄漏）。fd 0（stdin）也指过去 —— detached child 无 terminal，stdin 读
    到 EOF（claude -p 不读 stdin 走 prompt，故无影响；防误读挂起）。
    """
    # O_APPEND：child 多次 print 不覆盖（与 ``logs`` tail 语义一致）。
    # O_CREAT | O_WRONLY：不存在则创建（log_dir 已 mkdir，但 open 自带更鲁棒）。
    fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.dup2(fd, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        if fd > 2:
            os.close(fd)


# ── detached child 终结时更新 metadata（child 进程内调）────────────────────────


def mark_terminal_status(run_id: str, status: Literal["completed", "failed"]) -> None:
    """detached child 跑完 workflow 后调：把 metadata.status 更新成终态 + 戳 finished_at。

    child 进程退出前调（``commands._run_workflow_headless`` 返回后）；若 child 崩溃未及调，
    ``ps``/``wait`` 用 ``effective_status`` 检测 pid 死 → 标 crashed。

    ``finished_at`` 戳当前 ``time.time()`` —— 让 ``ps`` 的 ELAPSED 列对终态 run 显示固定值
    （``finished_at - started_at``），而非随墙钟增长（避免「已完成的 run elapsed 还在涨」误导）。

    宽容：metadata 文件不存在 / 损坏 → 静默跳过（child 已在退出路径，再 raise 反而
    掩盖 workflow 的真实 exit code；记 warning 即可）。
    """
    meta = read_meta(run_id)
    if meta is None:
        logger.warning(
            "mark_terminal_status: metadata 不存在或损坏（run_id=%s），跳过更新。",
            run_id,
        )
        return
    write_meta(meta.with_status(status, at=time.time()))


# ── ``wait`` 子命令的轮询辅助 ─────────────────────────────────────────────────


def wait_for_terminal(
    run_id: str,
    *,
    poll_interval: float = 0.5,
    timeout: float | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> tuple[RunStatus, BgRunMeta | None]:
    """阻塞轮询直到 run 进入 terminal status（completed/failed/crashed）。

    返回 ``(status, meta_or_none)``：meta 缺失（run_id 不存在）→ ``(?, None)``，
    调用方据此判 ``not-found``（CLI exit 2）。

    ``poll_interval`` 默认 0.5s：tape flush 频率远高于此，足够实时；过短空转浪费 CPU。
    ``timeout`` None=无限等；给值则超时返当前 status（调用方判是否还在 running）。
    """
    start = time_fn()
    while True:
        meta = read_meta(run_id)
        if meta is None:
            return ("crashed", None)  # not-found 由调用方据 meta is None 判
        status = effective_status(meta)
        if status in TERMINAL_STATUSES:
            return (status, meta)
        if timeout is not None and (time_fn() - start) >= timeout:
            return (status, meta)
        sleep_fn(poll_interval)
