"""_project.py —— 轻量项目注册表（SPEC §13 D2/D4, B-2, P1, M-15/M-16）。

**职责**：回答「本项目根是哪个？」「哪些项目曾注册过（discovery 枚举根 / attach allowlist）」。

**中立层铁律（§13.2 B-2）**：本模块**只**依赖 stdlib + ``orca.schema``。禁止 import
``iface/cli`` 或 ``iface/web``（反向依赖破铁律 5）。cli 与 web 都从此 import。

**设计要点**：
  - ``ORCA_HOME``（默认 ``~/.orca``）下维护一份注册表 ``projects.json``：``{version, projects}``，
    每个 entry = ``{path, name, first_seen, last_seen}``。**电话簿**——只存路径指针，不索引 run 数据
    （R1：tape 唯一真相源，注册表非 run 数据库）。
  - ``project_id = sha256(resolve(project_root))[:16]`` —— path 的**派生指纹**，path 才是真实身份；
    禁止跨重命名做去重/合并（P2）。
  - ``detect_project_root()`` 优先级链：``ORCA_PROJECT_ROOT`` env > 向上找含 ``workflows/`` 或
    ``.orca/config.json`` 的目录 > git root > ``Path.cwd()``。
  - ``register_project`` 拒绝 OS 顶层目录（``/``/``/etc``/``/usr``/``/bin``/``/var``/``/sys``/
    ``/home``/``/Users``/``/tmp``/``C:\\``/``C:\\Windows`` 等）+ 要求 path 下含 ``workflows/`` 或
    ``.orca/config.json`` 之一（M-15/M-16 防 poisoning）。
  - **并发**：单一 ``_with_lock()`` helper（SPEC §13.3 P1），``fcntl.flock`` (POSIX) /
    ``msvcrt.locking`` (Windows) exclusive 锁 ``~/.orca/.projects.lock``；**公开 API 禁嵌套调用**
    （防 Windows msvcrt 死锁）。
  - **原子写**：``tmp + os.replace`` + 保留 ``.bak``（P1）。读时 parse 失败 → 读 ``.bak`` →
    仍坏 → raise ``RegistryCorruptError`` + 提示 ``orca project rebuild``（fail loud）。
  - ``orca_home == resolve(project_root)`` → fail loud（P2：防 cwd=ORCA_HOME 锚定）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# ── 常量 ──────────────────────────────────────────────────────────────────────

REGISTRY_FILE = "projects.json"
_REGISTRY_VERSION = 1
_LOCK_FILE = ".projects.lock"
_BAK_FILE = REGISTRY_FILE + ".bak"

# OS 顶层目录黑名单（M-15）：绝对路径 normalize 后比对。覆盖 POSIX + Windows 常见顶层。
# ``/home`` / ``/Users`` 也拒（在 home 下应建专门的项目子目录，而非把整个 home 当项目）。
_TOPLEVEL_DIRS: frozenset[str] = frozenset(
    {
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/var",
        "/sys",
        "/lib",
        "/lib64",
        "/opt",
        "/home",
        "/Users",
        "/tmp",
        "/root",
        # Windows 盘符根（大小写不敏感比较，见 _normalize_root）
        "c:\\",
        "c:/",
        "c:\\windows",
        "c:/windows",
        "c:\\program files",
        "c:/program files",
        "c:\\program files (x86)",
        "c:/program files (x86)",
    }
)


class RegistryCorruptError(RuntimeError):
    """注册表 JSON 损坏且 ``.bak`` 也坏 → fail loud（提示 ``orca project rebuild``）。"""


# ── ORCA_HOME / project_root 检测 ─────────────────────────────────────────────


def orca_home() -> Path:
    """``ORCA_HOME`` env（默认 ``~/.orca``）。SPEC §13 D1/D12 身份与状态根。

    不 ``mkdir``（由调用方按需创建；本模块只读 env 路径）。
    """
    env = os.environ.get("ORCA_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".orca"


def _resolve_strict(p: Path | str) -> Path:
    """``resolve()`` 失败 → raise ``ValueError``（fail loud，不静默退化）。

    与 ``_identity.runs_dir_fingerprint`` 的 try/except 退化不同：注册表写入需绝对路径稳定，
    resolve 失败属异常状态，应让调用方知道。
    """
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"path resolve 失败：{p} ({e})") from e


def detect_project_root() -> Path:
    """检测当前项目根。SPEC §13 D2 优先级链 + AC15。

    优先级：
      1. ``ORCA_PROJECT_ROOT`` env（显式钉死，最高优先级——检测歧义时用户逃逸口）。
      2. 从 cwd 向上找含 ``workflows/`` 或 ``.orca/config.json`` 的目录。
      3. git root（向上找 ``.git``）。
      4. ``Path.cwd()`` 兜底。

    返回 resolved absolute Path。``ORCA_HOME`` 本身**不可**作为项目根（P2 防锚定）——
    若检测结果落在 ``orca_home()`` 上，跳到下一优先级。
    """
    home = orca_home()

    env_root = os.environ.get("ORCA_PROJECT_ROOT")
    if env_root:
        resolved = _resolve_strict(env_root)
        if resolved != home:
            return resolved

    # 向上找 workflows/ 或 .orca/config.json
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if candidate == home:
            continue
        if (candidate / "workflows").is_dir() or (
            candidate / ".orca" / "config.json"
        ).is_file():
            return _resolve_strict(candidate)

    # git root
    for candidate in [cwd, *cwd.parents]:
        if candidate == home:
            continue
        if (candidate / ".git").exists():
            return _resolve_strict(candidate)

    return _resolve_strict(cwd)


def project_id(project_root: Path | str) -> str:
    """``project_id = sha256(resolve(project_root))[:16]``（SPEC §13 D2/P2）。

    path 是真实身份；id 是 path 的派生指纹，不跨重命名去重。
    """
    resolved = _resolve_strict(project_root)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]


# ── 注册表读写 ────────────────────────────────────────────────────────────────


def _registry_path() -> Path:
    return orca_home() / REGISTRY_FILE


def _normalize_root(p: str) -> str:
    """路径归一化用于顶层目录黑名单比较（POSIX 化 + 大小写不敏感比对 Windows 路径）。"""
    # 先把反斜杠 → 正斜杠，再去**重复**尾部 slash（保留根 ``/``，不让 ``/`` → ````）。
    norm = p.replace("\\", "/")
    if len(norm) > 1:
        norm = norm.rstrip("/")
    # Windows 盘符路径做小写比对（C:\\ → c:/）
    if len(norm) >= 2 and norm[1] == ":":
        return norm[0].lower() + norm[1:]
    return norm


def _is_toplevel(path: Path) -> bool:
    """是否 OS 顶层目录（M-15 黑名单 + 盘符根 robust 判定，code-reviewer m-2）。

    优先用 ``path.parent == path`` 判定盘符根（``/`` / ``C:\\`` / ``D:\\`` 等都成立，
    覆盖所有 Windows 盘符），再叠加显式黑名单（``/usr`` / ``/etc`` / ``/home`` 等）。
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path
    # 盘符根 / POSIX 根：parent == self
    if resolved.parent == resolved:
        return True
    norm = _normalize_root(str(path))
    return norm in _TOPLEVEL_DIRS


def _has_project_marker(path: Path) -> bool:
    """含 ``workflows/`` 或 ``.orca/config.json`` 之一（M-16）。"""
    return (path / "workflows").is_dir() or (
        path / ".orca" / "config.json"
    ).is_file()


@contextmanager
def _with_lock() -> Iterator[None]:
    """单一 flock 临界区 helper（SPEC §13.3 P1）。

    - POSIX：``fcntl.flock`` exclusive。
    - Windows：``msvcrt.locking`` exclusive（``LK_LOCK`` 阻塞式，失败 raise）。
    - **公开 API 禁嵌套调用**：嵌套 ``msvcrt.locking`` 同 fd 会死锁；公开 API（register/list）
      只在自身实现内调一次本 helper，不再调其它会获取同一锁的函数。

    锁文件 ``~/.orca/.projects.lock``（``orca_home`` 不存在时先 ``mkdir -p``）。
    """
    home = orca_home()
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / _LOCK_FILE
    fd = os.open(
        str(lock_path),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    # 被另一进程持有；LK_LOCK 在 Windows 上通常阻塞，但保险起起 sleep 重试
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
        if sys.platform == "win32":
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_registry_unlocked() -> dict:
    """读注册表（不加锁，需调用方持锁）。

    JSON parse 失败 → 读 ``.bak``；``.bak`` 也坏 → raise RegistryCorruptError。
    返回的 dict 结构保证为 ``{"version": int, "projects": dict}``。
    """
    path = _registry_path()
    for candidate in (path, path.with_name(_BAK_FILE)):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        projects = data.get("projects")
        if not isinstance(projects, dict):
            continue
        version = data.get("version", _REGISTRY_VERSION)
        return {"version": int(version), "projects": projects}
    # 全部失败 → 视为新注册表（空）
    return {"version": _REGISTRY_VERSION, "projects": {}}


def _atomic_write_registry(data: dict) -> None:
    """原子写注册表 + 刷 ``.bak``。SPEC §13.3 P1。"""
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    # 刷 .bak（失败不阻断主写——.bak 是兜底，缺失只是恢复力退化，不 fail loud）
    try:
        bak = path.with_name(_BAK_FILE)
        bak.write_text(payload, encoding="utf-8")
    except OSError:
        pass


def list_registered() -> dict[str, dict]:
    """列全部注册项目：``{project_id: {path, name, first_seen, last_seen}}``。

    在 ``_with_lock`` 内读；注册表完全坏（JSON 非法且 .bak 也坏/缺）→ raise
    ``RegistryCorruptError``（fail loud，提示 ``orca project rebuild``）。

    本函数返回**浅拷贝**（避免 caller 误改注册表缓存）。
    """
    with _with_lock():
        # 显式触发 corrupt 检测：_read_registry_unlocked 在所有候选都坏时返回空 dict，
        # 此处若主文件存在但所有候选都 parse 失败 → 真坏 → fail loud。
        if _registry_path().exists() or _registry_path().with_name(_BAK_FILE).exists():
            # 判断是否真的 parse 失败：尝试再读一次直接抛
            try:
                _raise_if_registry_corrupt()
            except RegistryCorruptError:
                raise
        data = _read_registry_unlocked()
    projects = data.get("projects", {})
    return {pid: dict(meta) for pid, meta in projects.items()}


def _raise_if_registry_corrupt() -> None:
    """若主 + .bak 都存在但都 parse 失败 → raise RegistryCorruptError。"""
    path = _registry_path()
    bak = path.with_name(_BAK_FILE)
    main_exists = path.exists()
    bak_exists = bak.exists()
    if not main_exists and not bak_exists:
        return  # 空白状态
    main_ok = _is_valid_registry_file(path)
    bak_ok = _is_valid_registry_file(bak)
    if main_ok or bak_ok:
        return
    raise RegistryCorruptError(
        f"projects.json 损坏且 .bak 也损坏或缺失：\n  主：{path}\n  bak：{bak}\n"
        "请运行 `orca project rebuild` 重新注册（扫已知项目根重建注册表），"
        "或手动删除两文件后重新 `orca run/open`。"
    )


def _is_valid_registry_file(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, dict)
        and isinstance(data.get("projects"), dict)
    )


def register_project(project_root: Path | str) -> str:
    """注册一个项目（SPEC §13 D4 / M-15 / M-16）。

    步骤：
      1. resolve + 拒绝 OS 顶层目录（M-15）。
      2. 要求 path 含 ``workflows/`` 或 ``.orca/config.json``（M-16）。
      3. 拒绝 ``project_root == orca_home()``（P2 防锚定）。
      4. upsert 注册表（更新 path/name/last_seen；新项目记 first_seen）。

    返回 ``project_id``（即使已注册也返回同 id，幂等）。

    fail loud：顶层目录 → ``ValueError``；无 project marker → ``ValueError``；
    注册表 corrupt → ``RegistryCorruptError``。
    """
    resolved = _resolve_strict(project_root)
    if _is_toplevel(resolved):
        raise ValueError(
            f"拒绝注册 OS 顶层目录为项目根：{resolved}（M-15）。"
            "请在项目子目录下运行，或显式创建 ``workflows/`` / ``.orca/config.json``。"
        )
    if resolved == orca_home().resolve():
        raise ValueError(
            f"拒绝注册 ORCA_HOME 自身为项目根：{resolved}（P2 防锚定）。"
        )
    if not _has_project_marker(resolved):
        raise ValueError(
            f"项目根需含 ``workflows/`` 或 ``.orca/config.json`` 之一：{resolved}（M-16）。"
        )
    pid = project_id(resolved)
    now = time.time()
    with _with_lock():
        _raise_if_registry_corrupt()
        data = _read_registry_unlocked()
        projects = data.setdefault("projects", {})
        existing = projects.get(pid)
        if existing:
            existing["path"] = str(resolved)
            existing["name"] = resolved.name
            existing["last_seen"] = now
        else:
            projects[pid] = {
                "path": str(resolved),
                "name": resolved.name,
                "first_seen": now,
                "last_seen": now,
            }
        _atomic_write_registry(data)
    return pid


def is_registered_runs_dir(path: Path | str) -> bool:
    """``path`` 是否落在某注册项目的 ``<root>/runs/`` 子树下（attach allowlist）。

    SPEC §13.2 B-3 / §5.5：``resolve_tape_path`` 的 allowlist 分支用本方法。
    精确判：``resolve(path)`` 必须是 ``<registered_root>/runs`` 或其后代。
    """
    try:
        resolved = _resolve_strict(path)
    except ValueError:
        return False
    with _with_lock():
        _raise_if_registry_corrupt()
        data = _read_registry_unlocked()
    for meta in data.get("projects", {}).values():
        root_str = meta.get("path")
        if not isinstance(root_str, str):
            continue
        try:
            root = _resolve_strict(root_str)
        except ValueError:
            continue
        runs_dir = root / "runs"
        try:
            resolved.relative_to(runs_dir)
            return True
        except ValueError:
            continue
    return False
