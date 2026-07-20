"""_render.py —— render_chart 主逻辑（phase-13 SPEC §4.2，client lib 入口）。

回答「script 子进程怎么把 ChartPayload 推到 Orca run？」：6 步流程
（读 env → 长度检查 → 降采样 → 校验 → 大小检查 → socket send + ack）。

铁律 #2 兑现：API **不接收** run_id/node/session_id 参数（防 agent 诱导传错），全部从 env 读。

fail loud 9 处（SPEC §7）：env 缺、sock path 过长、payload 校验失败、超限、socket 不可达、
ack timeout、ack ok=False、socket 关闭无 ack、解析 ack JSON 失败——全部 raise，无静默。

依赖单向：仅依赖 stdlib（json/os/socket/sys）+ ``_validate`` + ``_downsample`` + ``_limits``。
**不依赖** events/exec/schema/run/iface——script 端 light-touch 客户端，零 Orca runtime 依赖。
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

from orca.chart._downsample import downsample
from orca.chart._limits import (
    ACK_TIMEOUT_SECONDS,
    DEFAULT_MAX_POINTS,
    MAX_MESSAGE_BYTES,
    SOCK_PATH_MAX,
)
from orca.chart._validate import validate_payload

# 4 个必需的 env 变量名（缺任一 → fail loud，SPEC §7.1）。
_REQUIRED_ENV = ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK")


def render_chart(
    *,
    chart_type: str,
    data: list[dict[str, Any]],
    label: str,
    title: str,
    x: str = "",
    y: str = "",
    hue: str = "",
    color: str = "",
    columns: list[str] | None = None,
    pareto_direction: str = "",
    pareto_x_direction: str = "",
    pareto_y_direction: str = "",
    value: str = "",
    max_points: int = DEFAULT_MAX_POINTS,
) -> int:
    """向当前 Orca run 推送一张图（SPEC §4.1 / §4.2）。返回分配的 seq。

    必须在 Orca 编排的 script 子进程内调用（env 含 ``ORCA_*``）。直接 ``python foo.py`` 跑
    会 raise RuntimeError（SPEC §7.1 fail loud）。

    同 ``label + title`` 的后续调用 → 旧图被前端替换（实时更新语义，phase-9d §2.7 dedup）。

    Args:
        chart_type: ``line`` / ``bar`` / ``area`` / ``scatter`` / ``pareto`` / ``radar`` / ``table``
            / ``heatmap``。
        data: 扁平 record array。
        label: 分组键（dedup 维度 1）。
        title: 图标题（dedup 维度 2，同 label 下唯一）。
        x / y / hue: 坐标轴 / 着色字段名。
            heatmap：``x`` = 列轴字段（如 bitwidth）、``y`` = 行轴字段（如 recipe），均**必填**
            （``validate_payload`` fail loud 拒收空 x/y）。
        color: per-row fill 颜色字段名（bar/scatter 每行该字段值为合法 CSS 色串，渲染时逐行着色）。
            **hue 优先**：hue 非空时 color 被忽略（hue → 分组并排，color → 单 series 内逐行着色）。
            着色逻辑在调用脚本（每行写死合法 CSS 色串），前端 dumb 渲染。
        columns: table 列名（派生用）。
        pareto_direction / pareto_x_direction / pareto_y_direction: pareto 前沿方向
            （``max`` / ``min`` / 空）。
        value: heatmap cell 着色字段名（如 accuracy）。**chart_type='heatmap' 时必填**——
            ``validate_payload`` fail loud 拒收空 value。其它 chart_type 忽略此参数。
        max_points: 自动降采样阈值（默认 2000）。

    Returns:
        Orca 分配的 event seq（ack 携带，对账用）。

    Raises:
        RuntimeError: env 缺 / sock path 过长 / socket 不可达 / ack timeout / ack ok=False /
            socket 关闭无 ack（SPEC §7 fail loud）。
        ValueError: payload 校验失败 / 大小超限（SPEC §7.2 / §5.2）。
    """
    # 1. 读 env（身份路由，铁律 #2：单调信息流，agent 无法干扰）
    env = {k: os.environ.get(k, "") for k in _REQUIRED_ENV}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise RuntimeError(
            "render_chart 不在 Orca run 上下文中（缺 ORCA_* env: "
            + ", ".join(missing)
            + "）。本函数仅可由 Orca 编排的 script 子进程调用。"
        )

    sock_path = env["ORCA_CHART_SOCK"]

    # 2. sock path 长度检查（SPEC §7.7 防御：> 90 字节 → raise）
    if len(sock_path) > SOCK_PATH_MAX:
        raise RuntimeError(
            f"socket path 过长（{len(sock_path)} > {SOCK_PATH_MAX} 字节）："
            f"{sock_path!r}。请改 ORCA_RUNS_DIR env 到短路径（如 /tmp/orca-runs/）。"
        )

    # 3. 降采样（SPEC §5.1 透明降采样，不 raise，仅 stderr warning）
    if len(data) > max_points:
        sys.stderr.write(
            f"[orca.chart] chart {title!r} 数据从 {len(data)} 降采样到 "
            f"max_points={max_points}（原数据保留在 script）\n"
        )
        data = downsample(chart_type, data, max_points, hue)

    # 4. 构造 ChartPayload + 校验（SPEC §7.2，fail loud）
    payload: dict[str, Any] = {
        "chart_type": chart_type,
        "data": data,
        "label": label,
        "title": title,
        "x": x,
        "y": y,
        "hue": hue,
        "color": color,
        "value": value,
    }
    if columns is not None:
        payload["columns"] = columns
    # pareto 系列仅在非空时塞进 payload（保持 ChartPayload 干净）
    for k, v in (
        ("pareto_direction", pareto_direction),
        ("pareto_x_direction", pareto_x_direction),
        ("pareto_y_direction", pareto_y_direction),
    ):
        if v:
            payload[k] = v
    validate_payload(payload)  # fail loud：缺字段 / 类型错 / 未知 chart_type → raise

    # 5. 大小硬上限（SPEC §5.2，post-downsample 整条消息 > 2 MB → raise）
    msg = {
        "node": env["ORCA_NODE"],
        "session_id": env["ORCA_SESSION_ID"],
        "payload": payload,
    }
    encoded = (json.dumps(msg) + "\n").encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"chart payload 过大（{len(encoded)} > {MAX_MESSAGE_BYTES} 字节）。"
            f"减少 data 行数或显式调低 max_points（当前 max_points={max_points}）。"
        )

    # 6. 连 socket + 发 + 等 ack（SPEC §4.2 step 5-6，fail loud 7 处）
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(ACK_TIMEOUT_SECONDS)
            s.connect(sock_path)  # FileNotFoundError / ConnectionRefusedError → §7.3
            s.sendall(encoded)
            # makefile("rb").readline() 读到 EOF 返回 b""（socket 关闭）；正常返回单行 bytes。
            # 显式 "rb" 二进制模式：与测试 helper 一致 + 避免文本模式默认 buffering 与
            # socket timeout 交互的边界问题。file 显式 close（防 socket fd 泄漏）。
            with s.makefile("rb") as f:
                ack_raw = f.readline()  # socket.timeout → §7.6
    except FileNotFoundError as e:
        raise RuntimeError(
            f"无法连接 Orca chart socket（{sock_path}）：文件不存在。"
            f"Orca 进程可能已退出或 run 已结束。"
        ) from e
    except ConnectionRefusedError as e:
        raise RuntimeError(
            f"无法连接 Orca chart socket（{sock_path}）：连接被拒。"
            f"Orca 进程可能已退出或 run 已结束。"
        ) from e
    except socket.timeout as e:
        raise RuntimeError(
            f"Orca chart socket ack 超时（{ACK_TIMEOUT_SECONDS}s）。"
            f"Orca ingestor 可能卡住或 tape fs 慢。"
        ) from e

    if not ack_raw:
        raise RuntimeError(
            f"Orca chart socket 关闭，未收到 ack（sock={sock_path}）。"
            f"Orca 进程可能 crash 后被 done_callback 重起中。"
        )

    try:
        ack = json.loads(ack_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Orca chart ack 非 JSON：{ack_raw!r}"
        ) from e

    if not ack.get("ok"):
        raise RuntimeError(
            f"Orca 拒收 chart：{ack.get('error', '<无错误信息>')}"
        )

    seq = ack.get("seq")
    if not isinstance(seq, int):
        raise RuntimeError(
            f"Orca chart ack 缺 seq 字段（ack={ack!r}）"
        )
    return seq
