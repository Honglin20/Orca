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
