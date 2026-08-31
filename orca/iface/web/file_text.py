"""file_text.py —— web 路由共享的路径守卫 + 单文件文本读取（web SPEC §2.2 DRY）。

workflows 路由（``/api/workflows/.../file``）与 runs 路由（``/api/runs/<id>/artifacts/file``）
两端点同构的守卫 + 读取段只此一份：

  - ``safe_resolve``：三重守卫（``..`` / 绝对路径 / symlink 末端与中间段 / 不存在 → None），
    与 ``run_manager.resolve_asset_path`` 等强度（后者亦委托本函数）。
  - ``read_text_file``：守卫 + 1MB cap + 二进制检测 + utf-8 读取，返回同构 envelope。

**依赖单向**：本模块只依赖 stdlib + fastapi 标准件，是 ``iface.web`` 内部共享底层，
可被 ``run_manager`` / ``routes/*`` 安全 import（无反向依赖）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

# 单文件大小上限（plan §M2/M6：1MB；防主线程读取 + 前端 prism 高亮卡死）。
# web SPEC §2.1：artifacts 文档端点复用同一上限（超限 413）。
MAX_FILE_BYTES = 1_000_000


def safe_resolve(root: Path, rel: str) -> Path | None:
    """路径守卫（原 ``workflows._safe_resolve`` 语义保留 + run_manager 守卫并入）。

    三重守卫（web SPEC §2.1 口径：越界 / symlink / 不存在）：
      1. **越界**：``rel`` 为空、``..`` / 绝对路径 escape（resolve 后
         ``relative_to(root)`` 失败，含中间段 symlink 指出 root）→ None。
      2. **symlink**：末端 symlink 先查（``.resolve()`` 会跟随 symlink，必须在
         resolve 前 check）+ resolve 后 ``is_symlink()`` 兜底（防御纵深）。
      3. **不存在 / 非普通文件**→ None。

    整段包 try/except ``(ValueError, OSError)``：null byte / 盘符 / 其它 FS 错 → None
    （fail closed）。与抽前 ``run_manager.resolve_asset_path`` 的差异仅此异常输入类：
    旧版在越界 check 外的步骤上抛 → 500；共享版收敛为 404。合法与越界输入逐字一致。
    """
    try:
        rel = (rel or "").strip()
        if not rel:
            return None
        root = root.resolve()
        unresolved = root / rel
        if unresolved.is_symlink():
            return None
        candidate = unresolved.resolve()
        candidate.relative_to(root)  # ValueError → 越界
        if candidate.is_symlink():
            return None
        if not candidate.is_file():
            return None
        return candidate
    except (ValueError, OSError):
        return None


def read_text_file(
    root: Path,
    rel: str,
    *,
    too_large_status: int = 422,
    decode_error_status: int = 500,
) -> dict:
    """守卫 + 读取单文件文本（原 ``workflows._read_text_file`` 共享化）。

    agent file / workflow file / run artifacts 文档端点的共享读取函数（DRY）：同构的
    1MB / 二进制 / 404 守卫只此一份。守卫顺序：路径越界/symlink/非文件 → 404；
    超 1MB → ``too_large_status``（workflows 两端点 422 原行为；artifacts 端点按
    web SPEC §2.1 传 413）；二进制（前 2048 字节含 ``\\x00``）→ 422；非 utf-8 可解码
    → ``decode_error_status``（默认 500 与 workflows 抽前未捕获行为同状态码；
    artifacts 端点传 422，失败路径显式）。常规输入域 raise HTTPException 语义与抽前
    workflows 内联段一致。
    """
    candidate = safe_resolve(root, rel)
    if candidate is None:
        raise HTTPException(status_code=404, detail="file not found")

    size = candidate.stat().st_size
    if size > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=too_large_status,
            detail=f"file too large: {size} bytes (limit {MAX_FILE_BYTES})",
        )
    with candidate.open("rb") as f:
        if b"\x00" in f.read(2048):
            raise HTTPException(status_code=422, detail="binary file")
    try:
        text = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=decode_error_status,
            detail=f"file not utf-8 decodable: {rel}",
        ) from e
    ext = candidate.suffix.lstrip(".")
    return {
        "path": rel,
        "text": text,
        "ext": ext,
        "size": size,
        "truncated": False,
    }
