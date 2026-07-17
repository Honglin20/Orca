"""tests/events/test_adapters_family.py —— ``_family.py`` resolver（SPEC §P4）。

覆盖意图（contract 锁，纯函数 + tmp_path 文件系统隔离）：
  - resolver 4 优先级 oracle：env > config(family) > probe > default。
  - 探测歧义：cc + cac 同存 → 默认 .claude（保守），source="probe"。
  - opencode 探测：.opencode / .nga DB 存在性 → probe vs default。
  - opencode 默认 fallback：opencode.db > session.db（B2-VRFY Bug #1 回归）。
  - 同源 HOST_DOTDIR 一致性：``CC_FAMILY_DOTDIR`` / ``OPENCODE_FAMILY_DOTDIR`` 与
    ``orca.iface.cli.skill_cmds.HOST_DOTDIR`` 字段一致（不跨层 import 但必须同步）。
  - fail loud：family 非法 → ValueError；host_session 空（非 env）→ ValueError。
  - detect_cc_existing_roots / detect_opencode_existing_dbs：返存在家族集合。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca.events.adapters._family import (
    CC_FAMILY_DOTDIR,
    OPENCODE_FAMILY_DOTDIR,
    _encode_cwd,
    detect_cc_existing_roots,
    detect_opencode_existing_dbs,
    resolve_cc_sidechain_root,
    resolve_opencode_db,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉所有路径覆盖 env，防 dev 机环境污染探测结果。"""
    for k in ("ORCA_CC_SIDECHAIN_ROOT", "ORCA_OPENCODE_DB"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``Path.home()`` → tmp_path/home（隔离 ~/.claude / ~/.cac 探测）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fixed_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """固定 cwd（``_encode_cwd`` 输入稳定，便于算期望路径）。"""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return str(cwd)


# ── 同源 HOST_DOTDIR 一致性（SPEC §P4：不跨层 import 但字段同步）──────────────


def test_cc_family_dotdir_matches_skill_cmds_host_dotdir():
    """CC_FAMILY_DOTDIR 必须与 ``skill_cmds.HOST_DOTDIR`` 中 cc/cac 子集一致（同源约束）。"""
    from orca.iface.cli.skill_cmds import HOST_DOTDIR

    for fam, dotdir in CC_FAMILY_DOTDIR.items():
        assert HOST_DOTDIR[fam] == dotdir, (
            f"CC_FAMILY_DOTDIR[{fam!r}]={dotdir!r} 与 HOST_DOTDIR[{fam!r}]"
            f"={HOST_DOTDIR[fam]!r} 漂移（SPEC §P4 同源约束）"
        )


def test_opencode_family_dotdir_matches_skill_cmds_host_dotdir():
    """OPENCODE_FAMILY_DOTDIR 必须与 ``HOST_DOTDIR`` 中 opencode/nga 子集一致。"""
    from orca.iface.cli.skill_cmds import HOST_DOTDIR

    for fam, dotdir in OPENCODE_FAMILY_DOTDIR.items():
        assert HOST_DOTDIR[fam] == dotdir, (
            f"OPENCODE_FAMILY_DOTDIR[{fam!r}]={dotdir!r} 与 HOST_DOTDIR[{fam!r}]"
            f"={HOST_DOTDIR[fam]!r} 漂移（SPEC §P4 同源约束）"
        )


# ── _encode_cwd（从 cc_jsonl.py 移入；cc_jsonl re-export 零回归由该文件测试守）──


def test_encode_cwd_replaces_slashes():
    """``/`` → ``-``（CC projects 目录约定）。"""
    assert _encode_cwd("/mnt/d/Projects/Orca") == "-mnt-d-Projects-Orca"
    assert _encode_cwd("/") == "-"
    assert _encode_cwd("relative") == "relative"


# ── CC resolver：4 优先级 oracle ──────────────────────────────────────────────


def test_cc_priority_env_overrides_everything(fake_home, fixed_cwd, monkeypatch):
    """优先级 #1：env > config > probe > default。env 胜，family/probe 都被忽略。"""
    # 制造 config + probe 都存在的场景。
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", "/tmp/env-wins-sidechain")
    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd, family="cac")
    assert str(root) == "/tmp/env-wins-sidechain"
    assert src == "env"


def test_cc_priority_config_when_family_explicit(fake_home, fixed_cwd, monkeypatch):
    """优先级 #2：family 显式（"cac"）→ ~/.cac/projects/...；source="config"。

    即使探测路径都不存在（无 .claude/.cac 目录），family 显式仍胜（用户意图）。
    """
    # 清 env，无目录存在（保证不是 probe 触发）
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd, family="cac")
    encoded = _encode_cwd(fixed_cwd)
    assert root == fake_home / ".cac" / "projects" / encoded / "h-session" / "subagents"
    assert src == "config"


def test_cc_priority_config_family_cc(fake_home, fixed_cwd):
    """family="cc" 显式 → ~/.claude/projects/...；source="config"。"""
    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd, family="cc")
    encoded = _encode_cwd(fixed_cwd)
    assert root == fake_home / ".claude" / "projects" / encoded / "h-session" / "subagents"
    assert src == "config"


def test_cc_priority_probe_single_cac(fake_home, fixed_cwd):
    """优先级 #3：仅 .cac 存在（无 .claude）→ probe 胜，source="probe"。

    这是 SPEC §P4 验收 #1 场景：cac 环境下 CC adapter 自动读 .cac。
    """
    encoded = _encode_cwd(fixed_cwd)
    cac_root = fake_home / ".cac" / "projects" / encoded / "h-session" / "subagents"
    cac_root.mkdir(parents=True)

    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd)
    assert root == cac_root
    assert src == "probe"


def test_cc_priority_probe_single_cc(fake_home, fixed_cwd):
    """优先级 #3 变体：仅 .claude 存在 → probe 胜指向 cc。"""
    encoded = _encode_cwd(fixed_cwd)
    cc_root = fake_home / ".claude" / "projects" / encoded / "h-session" / "subagents"
    cc_root.mkdir(parents=True)

    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd)
    assert root == cc_root
    assert src == "probe"


def test_cc_priority_probe_ambiguity_defaults_to_claude(fake_home, fixed_cwd):
    """优先级 #3 歧义：cc + cac 同存无 config → 默认 .claude（保守，不破坏 cc 用户）。

    SPEC §P4 验收 #2：cc+cac 同装无 config → 默认 .claude + doctor 报告歧义 hint。
    """
    encoded = _encode_cwd(fixed_cwd)
    cc_root = fake_home / ".claude" / "projects" / encoded / "h-session" / "subagents"
    cac_root = fake_home / ".cac" / "projects" / encoded / "h-session" / "subagents"
    cc_root.mkdir(parents=True)
    cac_root.mkdir(parents=True)

    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd)
    assert root == cc_root, "歧义默认 .claude（cc 用户不受影响）"
    assert src == "probe"
    # doctor 可用 detect_cc_existing_roots 报告歧义（len=2）。
    assert detect_cc_existing_roots("h-session", cwd=fixed_cwd) == {"cc", "cac"}


def test_cc_priority_default_when_nothing_exists(fake_home, fixed_cwd):
    """优先级 #4：cc/cac 都不存在 → 默认 .claude；source="default"。"""
    encoded = _encode_cwd(fixed_cwd)
    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd)
    assert root == fake_home / ".claude" / "projects" / encoded / "h-session" / "subagents"
    assert src == "default"


def test_cc_priority_config_overrides_probe(fake_home, fixed_cwd):
    """优先级链验证：family=config 胜于 probe（即便 probe 结果不同）。

    设 family=cac，同时 .claude 存在但 .cac 不存在 → resolver 应返 .cac（config 胜）。
    """
    encoded = _encode_cwd(fixed_cwd)
    cc_root = fake_home / ".claude" / "projects" / encoded / "h-session" / "subagents"
    cc_root.mkdir(parents=True)  # probe 会选 cc，但 config 应胜

    root, src = resolve_cc_sidechain_root("h-session", cwd=fixed_cwd, family="cac")
    assert root == fake_home / ".cac" / "projects" / encoded / "h-session" / "subagents"
    assert src == "config"


# ── CC resolver：fail loud ────────────────────────────────────────────────────


def test_cc_resolve_raises_on_unknown_family(fake_home, fixed_cwd):
    """family 非法 → ValueError（fail loud；adapter/doctor 各自包装）。"""
    with pytest.raises(ValueError, match="unknown CC family"):
        resolve_cc_sidechain_root("h", cwd=fixed_cwd, family="vintage")


def test_cc_resolve_raises_on_empty_host_session_non_env(fake_home, fixed_cwd):
    """host_session 空 + 无 env + 无 family → ValueError（路径无意义）。"""
    with pytest.raises(ValueError, match="host_session"):
        resolve_cc_sidechain_root("", cwd=fixed_cwd)


def test_cc_resolve_env_path_works_without_host_session(fake_home, fixed_cwd, monkeypatch):
    """env 路径下 host_session 可空（整体覆盖，不依赖 session 定位目录）。"""
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", "/tmp/no-session-needed")
    root, src = resolve_cc_sidechain_root("", cwd=fixed_cwd)
    assert str(root) == "/tmp/no-session-needed"
    assert src == "env"


def test_cc_resolve_family_with_empty_host_session_raises(fake_home, fixed_cwd):
    """family 显式但 host_session 空 → ValueError（仍需 session 定位目录）。"""
    with pytest.raises(ValueError, match="host_session"):
        resolve_cc_sidechain_root("", cwd=fixed_cwd, family="cac")


# ── detect_cc_existing_roots ─────────────────────────────────────────────────


def test_detect_cc_existing_roots_empty_for_no_host_session(fake_home, fixed_cwd):
    """host_session 空 → 返空集（无法定位目录）。"""
    assert detect_cc_existing_roots("", cwd=fixed_cwd) == set()


def test_detect_cc_existing_roots_empty_when_no_dirs(fake_home, fixed_cwd):
    """无 .claude / .cac 目录 → 返空集。"""
    assert detect_cc_existing_roots("h", cwd=fixed_cwd) == set()


def test_detect_cc_existing_roots_finds_both(fake_home, fixed_cwd):
    """cc + cac 都存在 → 返 {'cc', 'cac'}。"""
    encoded = _encode_cwd(fixed_cwd)
    for fam in CC_FAMILY_DOTDIR:
        dotdir = CC_FAMILY_DOTDIR[fam]
        (fake_home / dotdir / "projects" / encoded / "h" / "subagents").mkdir(parents=True)
    assert detect_cc_existing_roots("h", cwd=fixed_cwd) == {"cc", "cac"}


# ── opencode resolver：4 优先级 oracle ───────────────────────────────────────


def test_opencode_priority_env(fake_home, monkeypatch):
    """优先级 #1：env 胜。"""
    monkeypatch.setenv("ORCA_OPENCODE_DB", "/tmp/env.db")
    db, src = resolve_opencode_db()
    assert str(db) == "/tmp/env.db"
    assert src == "env"


def test_opencode_priority_config_family_explicit(fake_home):
    """优先级 #2：family 显式 → ~/.local/share/<family>/opencode.db；source="config"。"""
    db, src = resolve_opencode_db(family="nga")
    assert db == fake_home / ".local" / "share" / "nga" / "opencode.db"
    assert src == "config"


def test_opencode_priority_config_family_opencode(fake_home):
    """family=opencode 显式 → ~/.local/share/opencode/opencode.db；source="config"。"""
    db, src = resolve_opencode_db(family="opencode")
    assert db == fake_home / ".local" / "share" / "opencode" / "opencode.db"
    assert src == "config"


def test_opencode_priority_probe_single_opencode(fake_home):
    """优先级 #3：仅 .local/share/opencode/opencode.db 存在 → probe 胜。"""
    oc_db = fake_home / ".local" / "share" / "opencode" / "opencode.db"
    oc_db.parent.mkdir(parents=True)
    oc_db.write_bytes(b"")

    db, src = resolve_opencode_db()
    assert db == oc_db
    assert src == "probe"


def test_opencode_priority_probe_single_nga(fake_home):
    """优先级 #3：仅 .local/share/nga/opencode.db 存在 → probe 胜指向 nga。"""
    nga_db = fake_home / ".local" / "share" / "nga" / "opencode.db"
    nga_db.parent.mkdir(parents=True)
    nga_db.write_bytes(b"")

    db, src = resolve_opencode_db()
    assert db == nga_db
    assert src == "probe"


def test_opencode_priority_probe_ambiguity_defaults_to_opencode(fake_home):
    """优先级 #3 歧义：opencode + nga 同存 → 默认 opencode；source="probe"。"""
    oc_db = fake_home / ".local" / "share" / "opencode" / "opencode.db"
    nga_db = fake_home / ".local" / "share" / "nga" / "opencode.db"
    for p in (oc_db, nga_db):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"")

    db, src = resolve_opencode_db()
    assert db == oc_db, "歧义默认 opencode（保守）"
    assert src == "probe"
    assert detect_opencode_existing_dbs() == {"opencode", "nga"}


def test_opencode_priority_default_falls_back_to_session_db(fake_home):
    """优先级 #4：无 family + probe 未命中（无 opencode.db）→ default 走 session.db fallback。

    B2-VRFY Bug #1 回归：原 opencode adapter 已有 ``opencode.db > session.db`` 回退；resolver
    在 default 分支延续此语义。probe 阶段已耗尽 opencode.db 命中可能（detect 用 is_file 判），
    故 default 不重复 opencode.db 检查，直接 session.db。
    """
    oc_dir = fake_home / ".local" / "share" / "opencode"
    oc_dir.mkdir(parents=True)
    # 不创建 opencode.db（probe 失败）→ default 兜底 session.db（路径可能不存在，caller is_file 判）。
    db, src = resolve_opencode_db()
    assert db == oc_dir / "session.db"
    assert src == "default"


def test_opencode_resolve_raises_on_unknown_family(fake_home):
    """family 非法 → ValueError。"""
    with pytest.raises(ValueError, match="unknown opencode family"):
        resolve_opencode_db(family="vintage")


# ── detect_opencode_existing_dbs ─────────────────────────────────────────────


def test_detect_opencode_existing_dbs_empty(fake_home):
    """无 DB → 空集。"""
    assert detect_opencode_existing_dbs() == set()


def test_detect_opencode_existing_dbs_finds_both(fake_home):
    """opencode + nga DB 都存在 → 返 {'opencode', 'nga'}。"""
    for fam in OPENCODE_FAMILY_DOTDIR:
        db = fake_home / ".local" / "share" / fam / "opencode.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
    assert detect_opencode_existing_dbs() == {"opencode", "nga"}


# ── adapter ctor family 透传（SPEC §P4：cc_jsonl / opencode_sqlite ctor）──────────
#
# 守 ctor 的 ``family`` 参数 → resolver 透传链。``root`` / ``db_path`` 显式参数优先级最高
# （测试覆盖既有），family 仅在无显式 root/db_path 时生效（本组覆盖）。


def test_cc_adapter_ctor_family_cac_resolves_dot_cac(fake_home, fixed_cwd, monkeypatch):
    """CCJsonlAdapter(host_session, family="cac") → root 指向 .cac（不靠 root= 显式）。"""
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    from orca.events.adapters.cc_jsonl import CCJsonlAdapter
    a = CCJsonlAdapter("h-sid", family="cac", cwd=fixed_cwd)
    encoded = _encode_cwd(fixed_cwd)
    assert a.root == fake_home / ".cac" / "projects" / encoded / "h-sid" / "subagents"


def test_cc_adapter_ctor_invalid_family_raises_cc_error(fake_home, fixed_cwd, monkeypatch):
    """family 非法 → resolver ValueError → ctor 包装成 CCAdapterError（fail loud 包装链）。"""
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    from orca.events.adapters.cc_jsonl import CCAdapterError, CCJsonlAdapter
    with pytest.raises(CCAdapterError, match="unknown CC family"):
        CCJsonlAdapter("h-sid", family="vintage", cwd=fixed_cwd)


def test_cc_adapter_ctor_root_overrides_family(fake_home, fixed_cwd, monkeypatch):
    """显式 root 参数优先级 > family（向后兼容既有 ``root=`` 测试用例）。"""
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    from orca.events.adapters.cc_jsonl import CCJsonlAdapter
    explicit = fake_home / "explicit-root"
    a = CCJsonlAdapter("h-sid", cwd=fixed_cwd, root=explicit, family="cac")
    assert a.root == explicit


def test_opencode_adapter_ctor_family_nga_resolves_nga_dir(fake_home, monkeypatch):
    """OpencodeSqliteAdapter(host_session, family="nga") → db_path 指向 .local/share/nga。"""
    monkeypatch.delenv("ORCA_OPENCODE_DB", raising=False)
    from orca.events.adapters.opencode_sqlite import OpencodeSqliteAdapter
    a = OpencodeSqliteAdapter("h-sid", family="nga")
    assert a.db_path == fake_home / ".local" / "share" / "nga" / "opencode.db"


def test_opencode_adapter_ctor_invalid_family_raises_adapter_error(fake_home, monkeypatch):
    """family 非法 → resolver ValueError → ctor 包装成 OpencodeAdapterError。"""
    monkeypatch.delenv("ORCA_OPENCODE_DB", raising=False)
    from orca.events.adapters.opencode_sqlite import (
        OpencodeAdapterError, OpencodeSqliteAdapter,
    )
    with pytest.raises(OpencodeAdapterError, match="unknown opencode family"):
        OpencodeSqliteAdapter("h-sid", family="vintage")


def test_opencode_adapter_ctor_db_path_overrides_family(fake_home, monkeypatch):
    """显式 db_path 参数优先级 > family（向后兼容既有 ``db_path=`` 测试用例）。"""
    monkeypatch.delenv("ORCA_OPENCODE_DB", raising=False)
    from orca.events.adapters.opencode_sqlite import OpencodeSqliteAdapter
    explicit = fake_home / "explicit.db"
    a = OpencodeSqliteAdapter("h-sid", db_path=explicit, family="nga")
    assert a.db_path == explicit
