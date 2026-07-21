"""web_registry.py —— per-project web server 端口登记（``<runs_dir>/.orca-web.json``）。

回答「7428 被别项目 orca 占用时，``orca open`` 怎么找到本项目已起的 server？」：每个项目
（resolved ``runs_dir``）一份登记文件 ``{port, runs_dir_fp}``，``orca open`` spawn 后写、复用前读。

设计（spec-review 闭环）：
  - **探测权威，registry 仅 hint**：``_lookup_my_registered_port``（在 ``commands.py``）读到 port 后
    **仍** probe + 指纹校验（``_health_is_my_project``）；registry 陈旧（port 探测非 orca / 指纹不
    匹配 / 文件损坏）→ 忽略，下次 spawn 覆盖（**自愈**，无主动清理，YAGNI）。
  - **不存 pid**（H1）：``Popen.pid`` 可能是 ``tars`` wrapper pid 而非 server pid，是潜在错误数据；
    且 pid 不 gate 任何决策（探测权威）。未来 stop 功能需先解决 wrapper-fork（经 health 自报 pid）。
  - **不存 tape 路径**：tape 真相源是文件本身（``bg_runner.default_tape_path``）；registry 只缓存
    server 端口，**不是** tape 真相源（唯一真相源铁律）。
  - **原子写** tmp + ``os.replace``（与 ``marker.py`` 同模式，防半写）。
  - **并发**（B5 降级）：无 flock——``orca open`` 的 spawn 窗口窄，且 probe-权威保证正确性不受损
    （孤儿 server 不被引用也不污染复用），极小概率产生 1 个闲置孤儿由下次成功写自愈。

依赖单向：纯 stdlib（json/os/pathlib），不 import commands（避免环；探测/比对由 caller 做）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REGISTRY_NAME = ".orca-web.json"


def registry_path(runs_dir: Path | str) -> Path:
    """登记文件路径：``<runs_dir>/.orca-web.json``。"""
    return Path(runs_dir) / REGISTRY_NAME


def read_registry(runs_dir: Path | str) -> dict | None:
    """读登记。缺失 / 损坏 JSON / 非 dict → ``None``（调用方按「无登记」处理，自愈）。"""
    try:
        data = json.loads(registry_path(runs_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_registry(
    runs_dir: Path | str, *, port: int, runs_dir_fp: str
) -> None:
    """原子写登记（tmp + ``os.replace``，防半写）。

    ``mkdir`` 兜底：``--tape`` 指向 ``runs/`` 外时 ``Path("runs")`` 可能不存在（边界），建之。
    失败抛 ``OSError`` 给调用方（``_register_my_port`` loud warn，不阻断本次 open）。
    """
    path = registry_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"port": port, "runs_dir_fp": runs_dir_fp}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
