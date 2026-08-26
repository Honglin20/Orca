"""_identity.py —— web server 项目身份指纹（stdlib-only，无 fastapi/uvicorn/编排依赖）。

回答「跨项目复用 server 时，client 怎么判定 7428 上的 orca server 是不是本项目的？」：
``runs_dir_fingerprint(runs_dir)`` = ``sha1(str(resolve(runs_dir)))[:12]``。client（``orca open``
的 ``_runs_dir_fp``）与 server（``GET /api/health`` 的 ``runs_dir_fp`` 字段）同算法 → 同项目指纹
一致；不同项目（不同 resolved ``runs/``）→ 不同指纹 → client 不复用、改起本项目自己的 server。

放独立模块（spec-review B3）：``run_manager.py`` 顶层重依赖（``Orchestrator`` / ``EventBus`` /
``gates.*`` …，见 ``run_manager.py`` import），若指纹函数放彼处，client 的 ``open`` 路径 lazy import
会把整张依赖图拉进来（当前 ``open`` 不加载 ``run_manager``，是净回归）。故下沉到本无依赖模块——
``attach.py``（web 同层）与 ``commands.py``（cli，lazy import）都从此 import。

依赖单向：本模块只依赖 stdlib，可被 web 与 cli 任一层安全 import（不破铁律 5）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def runs_dir_fingerprint(runs_dir: Path | str) -> str:
    """``sha1(str(resolve(runs_dir)))[:12]`` —— 项目身份指纹。

    - **碰撞**：12 hex = 48bit；birthday paradox 对 ≤10^6 项目碰撞概率 < 10^-8
      （单机 / 团队场景远超够用）。
    - **隐私**：用指纹非明文——health 默认 bind ``0.0.0.0`` 网络可达，回明文项目绝对目录是信息
      泄漏；sha1 不可逆。**threat note（spec-review H5）**：指纹虽不可逆推路径，但同项目多次
      bootstrap 指纹稳定，bind ``0.0.0.0`` 时内网被动观察者可跨 session 关联同项目（威胁面：
      被动观察）。缓解（follow-up，非本 PR）：fp 仅在内部 header / loopback 下返回，或默认
      bind ``127.0.0.1``。
    - **鲁棒**：``resolve`` 失败（权限 / symlink loop）→ 退化为未 resolve 字面（仍稳定可比，不抛）。
    """
    try:
        resolved = str(Path(runs_dir).resolve())
    except (OSError, RuntimeError):
        resolved = str(runs_dir)
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]


def orca_home_fingerprint() -> str:
    """身份指纹 = ``sha1(ORCA_HOME)[:12]``（SPEC §13 D1 / U-2 / B-6）。

    **身份与存储路径解耦**：同用户（同 ``ORCA_HOME``）所有项目共享同一指纹 → 单端口。
    旧 ``runs_dir_fingerprint`` 把身份与 ``<project>/runs`` 耦合，不同项目不同指纹 → 永不复用。

    - 默认 ``ORCA_HOME = ~/.orca``；env ``ORCA_HOME`` 覆盖。
    - 与 ``runs_dir_fingerprint`` 同 sha1-truncate 算法，仅输入不同。

    SPEC §13.1 U-2：health 兼容期**同发** ``runs_dir_fp``（值=本函数结果）+ ``orca_home_fp``
    两字段；下个版本去旧名。client（``commands.py::_runs_dir_fp``）用本函数的值。
    """
    env = os.environ.get("ORCA_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".orca"
    return runs_dir_fingerprint(home)
