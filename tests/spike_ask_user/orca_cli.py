"""orca_cli.py —— ``orca bootstrap`` / ``orca next`` 的薄壳封装（spec spike 专用）。

**为什么需要封装**：
1. 子进程调用 + JSON 解析 + 错误分类是确定性逻辑，不该散落在 driver 里。
2. SPEC §2 driver 循环对 ``done`` / ``prompt`` / ``busy`` 三态的处理固定，统一在此封装。
3. 测试可注入 fake（``FakeOrcaCLI``）跑纯 driver 逻辑（不真启动 workflow）。

**不做的事**：不重试 ``busy``（SPEC 把 busy 重试交给主 session；spike 一次性 run 几乎不会
撞锁）；不做 timeout（交给调用方）；不改 orca 代码。

**依赖单向**：只依赖 stdlib；可被 driver、test 复用。
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class OrcaCLIError(RuntimeError):
    """orca CLI 非零退出 / 输出非 JSON / JSON 缺关键字段。fail loud。"""


@dataclass(frozen=True)
class BootstrapResult:
    """``orca <wf> --inputs ...`` 的解析结果。

    - ``run_id``：后续 ``next`` 的句柄（SPEC §2「--run-id 是唯一句柄」）。
    - ``tape``：tape 文件路径（诊断用，driver 不直接读）。
    - ``prompt``：首节点指令 + 驱动协议（传给子 agent）。
    - ``prompt_file``：首节点指令文件路径（子 agent Read 它）。
    - ``node``：首节点名（诊断）。
    - ``done``：恒 False（bootstrap 必有首节点）。
    """

    run_id: str
    tape: str
    node: str
    prompt: str
    prompt_file: str
    done: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class NextResult:
    """``orca next --run-id ... --output ...`` 的解析结果。

    - ``done``：True → workflow 结束（driver 停）。
    - ``prompt``：下一节点指令（done=False 时必填）。
    - ``node``：下一节点名（诊断）。
    - ``busy``：撞 tape flock 的稀有情况；driver 据 ``retry_after_ms`` 等待重试同一条命令。
    - ``retry_after_ms``：busy 时的退避毫秒。
    - ``raw``：原始 JSON（诊断 / 断言）。
    """

    done: bool
    prompt: str
    node: str
    busy: bool
    retry_after_ms: int
    raw: dict[str, Any]


def _run_orca(argv: list[str], *, timeout_s: float = 120.0) -> dict[str, Any]:
    """跑 ``argv``，返回解析后的 JSON dict。失败 fail loud。

    timeout 默认 120s：``orca bootstrap`` / ``orca next`` 会 detach spawn
    sidechain / chart / open-web 三个守护（每个 ~1-3s）+ 落 tape + marker RMW，
    实测冷启 8-12s。60s 偶尔不够（WSL2 + detach spawn 慢），120s 留足余量。
    """
    logger.debug("orca-cli run: %s", " ".join(argv))
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as e:
        raise OrcaCLIError(f"orca 超时 argv={argv}") from e

    stdout = proc.stdout
    if proc.returncode != 0:
        raise OrcaCLIError(
            f"orca 非零退出 rc={proc.returncode} argv={argv}\n"
            f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{stdout}"
        )
    if not stdout.strip():
        raise OrcaCLIError(f"orca stdout 空 argv={argv} stderr={proc.stderr}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise OrcaCLIError(
            f"orca stdout 非 JSON argv={argv}\nstdout={stdout[:400]!r}"
        ) from e


def bootstrap(
    wf: str,
    inputs: dict[str, Any] | None,
    *,
    orca_bin: str = "orca",
    cwd: str | None = None,
) -> BootstrapResult:
    """``orca <wf> --inputs '<json>'``——启动 run，返回首节点指令。

    ``wf`` 可以是 catalog 名（``spike_ask_user``）或 yaml 路径（``tests/.../x.yaml``）。
    ``inputs=None`` → 不启动，返 schema（本 driver 用不到，仅供诊断；本方法假设你已
    决定启动，传 {} 或真实 inputs）。
    """
    argv = [orca_bin, wf]
    if inputs is not None:
        argv += ["--inputs", json.dumps(inputs, ensure_ascii=False)]
    raw = _run_orca(argv)
    # 必须字段校验——缺则 fail loud（避免 driver 后续 KeyError 误判）。
    for key in ("run_id", "prompt", "done"):
        if key not in raw:
            raise OrcaCLIError(
                f"bootstrap 结果缺字段 {key!r}；raw keys={list(raw.keys())}；raw={raw}"
            )
    return BootstrapResult(
        run_id=raw["run_id"],
        tape=raw.get("tape", ""),
        node=raw.get("node", ""),
        prompt=raw["prompt"],
        prompt_file=raw.get("prompt_file", ""),
        done=bool(raw["done"]),
        raw=raw,
    )


def next_step(
    run_id: str,
    output: str,
    *,
    orca_bin: str = "orca",
    inputs: dict[str, Any] | None = None,
) -> NextResult:
    """``orca next --run-id <id> --output '<output>'``——推进当前节点。

    返回 ``NextResult``，driver 据 ``done`` / ``busy`` 决策。

    SPEC §2 驱动协议：busy → 等待 retry_after_ms 后**原样**重试同一条命令（不重派子 agent）；
    driver 把 busy 上抛或原地等待由调用方决定。本封装仅识别 busy，不自动重试（spike 路径
    撞锁概率低，且重试策略属于上层职责）。
    """
    argv = [orca_bin, "next", "--run-id", run_id, "--output", output]
    if inputs is not None:
        argv += ["--inputs", json.dumps(inputs, ensure_ascii=False)]
    raw = _run_orca(argv)
    reason = raw.get("reason", "")
    busy = reason == "busy"
    return NextResult(
        done=bool(raw.get("done", False)),
        prompt=raw.get("prompt", ""),
        node=raw.get("node", ""),
        busy=busy,
        retry_after_ms=int(raw.get("retry_after_ms", 0)),
        raw=raw,
    )


def stop(run_id: str, *, orca_bin: str = "orca") -> dict[str, Any]:
    """``orca stop --run-id <id>``——spike 结束时清理（避免 marker 残留）。"""
    argv = [orca_bin, "stop", "--run-id", run_id]
    return _run_orca(argv)
