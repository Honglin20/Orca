"""test_workflow_scripts_exec.py —— workflows 脚本 exec 位守卫（索引级，跨平台）。

不变量：``workflows/`` 下（排除 ``deprecated/``）被 git 跟踪的 .sh/.py——
- 首行带 shebang ⇔ 索引 mode 100755（Linux clone 后可直接 ``./x.sh`` 执行）；
- 无 shebang ⇔ 100644（库模块 / 数据文件，不应被直接执行）。

**为什么查索引、不查工作树**：Windows 侧 NTFS + ``core.filemode=false``，
工作树 chmod 既不落盘也不进 git——索引 mode 是跨平台唯一真相源，直接决定
Linux ``git clone`` 落盘权限。守卫据此在 Windows 本地 / CI 都可跑。

背景：2026-09-08 前全仓 workflow 脚本索引全 100644，远程 Linux 服务器上
直接执行调用点（agent prompt 指示的 ``$ART/scripts/x.sh``）集体 Permission
denied；治本 commit ``c8f4807`` 已翻转存量，本守卫防复发。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = "workflows/"
EXCLUDED_PREFIX = "workflows/deprecated/"
EXEC_MODE = "100755"
REG_MODE = "100644"


def _tracked_script_modes() -> list[tuple[str, str]]:
    """git 索引中 workflows/（排除 deprecated/）的 (mode, path) 列表，仅 .sh/.py。"""
    out = subprocess.run(
        ["git", "ls-files", "-s", SCAN_DIR],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode = meta.split()[0]
        if path.startswith(EXCLUDED_PREFIX):
            continue
        if path.endswith((".sh", ".py")):
            entries.append((mode, path))
    return entries


def _has_shebang(path: str) -> bool:
    """工作树文件首两字节是否 ``#!``（内容与索引一致时即索引内容首行）。"""
    f = REPO_ROOT / path
    if not f.is_file():
        # 工作树缺失（未 checkout 等）不判 shebang——mode 断言仍执行（守卫不哑过）
        return False
    with f.open("rb") as fh:
        return fh.read(2) == b"#!"


def test_shebang_scripts_are_executable_in_index():
    """shebang ⇔ 100755 不变量：双向断言，违例给出可执行的修正命令。"""
    need_exec: list[str] = []
    need_reg: list[str] = []
    for mode, path in _tracked_script_modes():
        if _has_shebang(path) and mode != EXEC_MODE:
            need_exec.append(path)
        elif not _has_shebang(path) and mode == EXEC_MODE:
            need_reg.append(path)
    assert not need_exec, (
        "带 shebang 但索引不可执行（Linux clone 后直接执行会 Permission denied）——"
        f"修正：git update-index --chmod=+x <path>\n{need_exec}"
    )
    assert not need_reg, (
        "无 shebang 却带可执行位（不该被直接执行）——"
        f"修正：git update-index --chmod=-x <path>\n{need_reg}"
    )


def test_guard_scope_is_not_vacuous():
    """守卫自身活性：扫描集非空且确有可执行脚本（防路径/排除配置漂移致哑绿）。"""
    entries = _tracked_script_modes()
    assert entries, "git ls-files 未返回任何 workflows 脚本——守卫范围配置失效"
    assert any(m == EXEC_MODE for m, _ in entries), (
        "扫描集内没有任何 100755 脚本——排除/扫描配置疑似漂移"
    )
