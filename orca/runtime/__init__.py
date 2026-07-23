"""orca.runtime —— 中立运行时工具层（SPEC §13 B-2 / P2）。

只放同时被 ``iface/cli`` 与 ``iface/web`` 复用的中立逻辑（项目身份/注册表），
**仅**依赖 stdlib + ``orca.schema``。禁止反向依赖 iface 层（铁律 5）。

为何不放在 ``iface/cli/``：``iface/web`` 反向 import cli 会破依赖单向
（SPEC §13.2 B-2 reviewer blocker）。下沉到中立层后两壳同源 import。
"""

from orca.runtime._project import (
    REGISTRY_FILE,
    RegistryCorruptError,
    detect_project_root,
    is_registered_runs_dir,
    list_registered,
    orca_home,
    project_id,
    register_project,
)

__all__ = [
    "REGISTRY_FILE",
    "RegistryCorruptError",
    "detect_project_root",
    "is_registered_runs_dir",
    "list_registered",
    "orca_home",
    "project_id",
    "register_project",
]
