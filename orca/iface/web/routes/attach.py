"""attach.py —— POST /api/runs/attach + GET /api/health（SPEC web-attach §2.1 / §5）。

回答「web 怎么 attach 一个外部 run？」「同 host 既有 orca server 怎么探测？」：
  - ``POST /api/runs/attach`` body ``{tape_path, run_id?}`` → 安全校验（§6）→
    ``manager.attach_run`` → ``{run_id, status}``。安全 / 碰撞 / 幂等失败映射到 HTTP：
    ``PermissionError`` → 403；``FileNotFoundError`` → 404；``ValueError(run_id_collision)``
    → 409；其它 → 500 fail loud。
  - ``GET /api/health`` → ``{app:"orca", version, pid, runs_dir_fp}``。``orca open`` / 端口探测
    用它判定「既有 server 是 orca」+ ``runs_dir_fp`` 判定是否**同项目**（spec-review B1/B3）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager`` + ``orca.__init__.__version__`` +
fastapi，不含编排逻辑（attach_run 才是注册入口）。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import orca
from orca.iface.web._identity import orca_home_fingerprint

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunManager


class AttachRequest(BaseModel):
    """``POST /api/runs/attach`` body（SPEC §2.1）。

    ``tape_path`` 必填（相对 CWD 或绝对，受 ``resolve_tape_path`` 守卫）；``run_id`` 可选
    （缺省时从首行 workflow_started 或文件名推导）。
    """

    tape_path: str
    run_id: str | None = None


def build_router(manager: RunManager) -> APIRouter:
    """构造 ``/api`` 顶层辅助路由：``POST /api/runs/attach`` + ``GET /api/health``。

    与 ``/api/runs`` 路由分开，因 ``attach`` 是动词（``/runs/attach``），``health`` 顶层
    独立（不属 runs）。两者都不带 ``/api/runs`` 前缀——本 router 直接挂 ``/api``。
    """
    router = APIRouter(prefix="/api", tags=["attach"])

    @router.post("/runs/attach")
    async def attach_run(body: AttachRequest) -> dict[str, Any]:
        """attach a run by tape path（SPEC §2.1）。

        安全 / 碰撞 / 幂等失败 → HTTP：
          - ``PermissionError`` → 403 ``tape_path out of bounds`` / TOCTOU / symlink
          - ``FileNotFoundError`` → 404 tape 不存在
          - ``ValueError(run_id_collision)`` → 409（不覆盖既有 run）
          - 幂等：同 tape_path 重复 attach → 200 既有 ``run_id``（不重起 follow）
        """
        try:
            run_id = await manager.attach_run(body.tape_path, body.run_id)
        except PermissionError as e:
            # 安全失败统一 403（不暴露 fs 细节）
            raise HTTPException(status_code=403, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("run_id_collision"):
                raise HTTPException(status_code=409, detail=msg)
            # 其它 ValueError（如 limit/tail 非法，不属本路径）→ 400
            raise HTTPException(status_code=400, detail=msg)
        handle = manager.get_handle(run_id)
        status = handle.status if handle is not None else "running"
        return {"run_id": run_id, "status": status}

    @router.get("/health")
    async def health() -> dict[str, Any]:
        """health 探测（SPEC §5 + §13.1 U-2）。

        身份指纹 = ``sha1(ORCA_HOME)[:12]``（D1 / U-2，身份与存储路径解耦）。
        **兼容期同发**两字段：
          - ``orca_home_fp``（新权威）：``sha1(ORCA_HOME)[:12]``。
          - ``runs_dir_fp``（兼容）：值 = ``orca_home_fp``（旧 client 比对此字段，下版本删）。

        client（``commands.py::_runs_dir_fp``）迁移到 ``orca_home_fp`` 后，同用户所有项目
        共享指纹 → 单端口复用（D13）。
        """
        fp = orca_home_fingerprint()
        return {
            "app": "orca",
            "version": orca.__version__,
            "pid": os.getpid(),
            "orca_home_fp": fp,
            "runs_dir_fp": fp,  # 兼容期：旧 client 比对此字段（值=orca_home_fp）
        }

    return router
