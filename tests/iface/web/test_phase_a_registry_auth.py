"""tests/iface/web/test_phase_a_registry_auth.py —— Phase A 端口登记上移 + auth middleware stub。

覆盖：
  - web_registry：用户级登记 + legacy 迁移（SPEC §13 D6 / M-7 / B-6）
  - _auth.AuthMiddleware 全局兜底（SPEC §13.1 M-1 / AC19）
  - _identity.orca_home_fingerprint（D1 / U-2）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orca.iface.cli import web_registry
from orca.iface.web import _auth
from orca.iface.web._identity import orca_home_fingerprint


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "orca-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ORCA_HOME", str(home))
    yield home


# ── web_registry（D6 / M-7 / B-6） ────────────────────────────────────────────


def test_lookup_orca_home_port_returns_none_when_absent(_isolated_home):
    assert web_registry.lookup_orca_home_port() is None


def test_write_and_lookup_roundtrip(_isolated_home):
    web_registry.write_orca_home_registry(port=7428, runs_dir_fp="abc123")
    assert web_registry.lookup_orca_home_port() == 7428


def test_migrate_legacy_registry_moves_old_to_new(_isolated_home, tmp_path):
    """M-7：旧 per-project 登记静默迁移到 ~/.orca/。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    # 旧登记
    web_registry.write_registry(runs_dir, port=9999, runs_dir_fp="fp-old")
    # 触发迁移
    web_registry.migrate_legacy_registry(runs_dir)
    # 新登记就位
    assert web_registry.lookup_orca_home_port() == 9999
    # 旧文件被改名 .migrated
    assert not web_registry.registry_path(runs_dir).exists()
    assert (
        web_registry.registry_path(runs_dir).with_name(
            web_registry.REGISTRY_NAME + ".migrated"
        ).exists()
    )


def test_migrate_legacy_skips_when_new_exists(_isolated_home, tmp_path):
    """新登记已存在 → 旧不迁移（新权威）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    web_registry.write_orca_home_registry(port=7777, runs_dir_fp="new")
    web_registry.write_registry(runs_dir, port=8888, runs_dir_fp="old")
    web_registry.migrate_legacy_registry(runs_dir)
    # 新权威：7777
    assert web_registry.lookup_orca_home_port() == 7777
    # 旧文件未被改名
    assert web_registry.registry_path(runs_dir).exists()


def test_migrate_legacy_silent_on_missing(_isolated_home, tmp_path):
    """无旧登记 → 静默 no-op。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    web_registry.migrate_legacy_registry(runs_dir)
    assert web_registry.lookup_orca_home_port() is None


def test_orca_home_fingerprint_stable_across_calls(_isolated_home):
    assert orca_home_fingerprint() == orca_home_fingerprint()
    assert len(orca_home_fingerprint()) == 12


def test_orca_home_fingerprint_changes_with_orca_home(tmp_path, monkeypatch):
    """不同 ORCA_HOME → 不同指纹（D12 多用户隔离基础）。"""
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / "user1"))
    fp1 = orca_home_fingerprint()
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / "user2"))
    fp2 = orca_home_fingerprint()
    assert fp1 != fp2


# ── _auth.AuthMiddleware（M-1 / AC19） ────────────────────────────────────────


def test_create_app_installs_auth_middleware():
    """AC19：``app.user_middleware`` 含 AuthMiddleware（no-op stub）。"""
    from orca.iface.web.run_manager import RunManager
    from orca.iface.web.server import create_app

    app = create_app(RunManager())
    # AuthMiddleware 经 ``app.middleware("http")(...)`` 注册 → 出现在 user_middleware
    assert any(
        "auth" in str(m).lower() or "AuthM" in str(m)
        for m in app.user_middleware
    ), f"AuthMiddleware 未安装：{app.user_middleware}"


def test_auth_noop_passes_request_without_auth_header():
    """no-op：无 Authorization 头也放行（当前不校验）。"""
    app = FastAPI()
    _auth.install_auth_middleware(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_noop_ignores_authorization_header():
    """no-op：带任意 Authorization 头也放行（未来才校验）。"""
    app = FastAPI()
    _auth.install_auth_middleware(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/ping", headers={"Authorization": "Bearer any-token-here"})
    assert r.status_code == 200
