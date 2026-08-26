"""test_workflows_routes.py —— ``/api/workflows`` 只读浏览路由契约测试。

plan idempotent-churning-lampson 闭环：无 manager 直挂 router（抄 ``test_attach_routes.py``）。
fixture ``monkeypatch`` ``catalog._workflow_dirs`` 指向 tmp_path（抄 ``test_catalog.py:68``），
**不** monkeypatch ``Path.cwd()``（blast radius）。

覆盖意图（plan §测试）：
  - 正常 list / detail / agents / tree / file
  - ``agents_referenced`` 含顶层 + foreach body agent
  - fail-soft ``missing`` 字段（坏 frontmatter agent）
  - 穿越守卫 8 范式（``../`` / 绝对 / 空 / symlink / URL 编码 / null byte / 二进制→422 / 超大→422）
  - golden fixture：tree JSON 深匹配 + file JSON 字段断言
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from orca.iface.web.routes.workflows import build_router as build_workflows_router

# starlette TestClient 的 httpx 弃用警告与本测试意图无关，过滤之。
warnings.filterwarnings("ignore", category=DeprecationWarning)


# ── 测试 fixture：典型 workflow + folder/single/broken 三类 agent ──────────────

_MYWF_YAML = """
name: mywf
description: test workflow
entry: a
inputs: {}
nodes:
  - name: a
    kind: agent
    agent: foo
    routes:
      - to: b
  - name: b
    kind: foreach
    source: "a.output.items"
    body:
      kind: agent
      agent: bar
    routes:
      - to: $end
"""


@pytest.fixture
def wf_dir(tmp_path: Path, monkeypatch) -> Path:
    """组装 tmp_path/workflows 目录（隔离测试）。

    ``monkeypatch.setattr`` ``catalog._workflow_dirs`` 仅返本 tmp 目录——**不** monkeypatch
    ``Path.cwd()``（blast radius：影响其它并行测试）。``ResolveContext.cwd`` 仍走真实
    ``Path.cwd()``，但 cwd/agents 通常不存在，无干扰。
    """
    d = tmp_path / "workflows"
    d.mkdir()
    (d / "mywf.yaml").write_text(_MYWF_YAML, encoding="utf-8")

    agents = d / "agents"
    # foo：folder agent（含 scripts/ + references/ 子目录，给 tree golden 用）
    foo = agents / "foo"
    foo.mkdir(parents=True)
    (foo / "agent.md").write_text(
        "---\ndescription: foo agent\n---\n# Foo\n\nhello\n",
        encoding="utf-8",
    )
    (foo / "scripts").mkdir()
    (foo / "scripts" / "helper.py").write_text(
        "def hello():\n    return 'world'\n",
        encoding="utf-8",
    )
    (foo / "references").mkdir()
    (foo / "references" / "doc.md").write_text("# Doc\n\ntext\n", encoding="utf-8")
    # 隐藏文件 + __pycache__ + .pyc（验证 m4 过滤）
    (foo / ".hidden").write_text("hidden", encoding="utf-8")
    (foo / "__pycache__").mkdir()
    (foo / "__pycache__" / "helper.cpython.pyc").write_text("pyc", encoding="utf-8")
    (foo / "scripts" / "_mod.pyc").write_text("pyc", encoding="utf-8")

    # bar：单文件 agent
    (agents / "bar.md").write_text(
        "---\ndescription: bar single\n---\nbar body\n",
        encoding="utf-8",
    )

    # broken：folder agent 但 frontmatter YAML 损坏（给 fail-soft missing 用）
    broken = agents / "broken"
    broken.mkdir()
    (broken / "agent.md").write_text(
        "---\nthis is not: valid: yaml: at all\n  bad indent\n---\nbody\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "orca.compile.catalog._workflow_dirs",
        lambda: [d],
    )
    return d


@pytest.fixture
def client():
    """无 manager FastAPI + TestClient（抄 test_attach_routes.py）。"""
    app = FastAPI()
    app.include_router(build_workflows_router())
    with TestClient(app) as c:
        yield c


# ── Endpoint 1: GET /api/workflows ────────────────────────────────────────────


def test_list_workflows(wf_dir, client):
    """list_workflows 返回 catalog（每项含 name/description 等）。"""
    resp = client.get("/api/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "mywf"
    assert data[0]["description"] == "test workflow"
    assert data[0]["entry"] == "a"


def test_list_workflows_empty(tmp_path, monkeypatch):
    """空 catalog → []（不 raise）。"""
    d = tmp_path / "workflows"
    d.mkdir()
    monkeypatch.setattr("orca.compile.catalog._workflow_dirs", lambda: [d])
    app = FastAPI()
    app.include_router(build_workflows_router())
    with TestClient(app) as c:
        assert c.get("/api/workflows").json() == []


# ── Endpoint 2: GET /api/workflows/{name} ─────────────────────────────────────


def test_get_workflow_detail_includes_foreach_body_agent(wf_dir, client):
    """agents_referenced 含顶层 agent + foreach body agent（plan M3 闭环）。"""
    resp = client.get("/api/workflows/mywf")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "mywf"
    assert body["description"] == "test workflow"
    assert body["entry"] == "a"
    # 关键：foreach body 的 bar 也进 referenced（plan §M3 用 _iter_agent_nodes）
    assert body["agents_referenced"] == ["foo", "bar"]
    # inputs_schema 来自 describe_workflow
    assert "inputs_schema" in body


def test_get_workflow_not_found(wf_dir, client):
    """未知 workflow → 404 + detail。"""
    resp = client.get("/api/workflows/nonexistent")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workflow not found"


# ── Endpoint 3: GET /api/workflows/{name}/agents ──────────────────────────────


def test_list_workflow_agents_includes_fail_soft_missing(wf_dir, client):
    """agents 含 foo/bar（resolve 成功）+ broken（fail-soft missing: true）。"""
    resp = client.get("/api/workflows/mywf/agents")
    assert resp.status_code == 200
    items = {a["name"]: a for a in resp.json()}
    # foo（folder）+ bar（single）+ broken（folder 但坏 frontmatter）
    assert set(items.keys()) >= {"foo", "bar", "broken"}

    foo = items["foo"]
    assert foo["is_folder"] is True
    assert foo["description"] == "foo agent"
    assert foo["missing"] is False

    bar = items["bar"]
    assert bar["is_folder"] is False
    assert bar["description"] == "bar single"
    assert bar["missing"] is False

    # fail-soft：broken resolve 失败 → missing: true（不中断整个列表）
    broken = items["broken"]
    assert broken["missing"] is True
    assert broken["description"] == ""


def test_list_workflow_agents_unknown_workflow_404(wf_dir, client):
    """未知 workflow → 404（不在 agents endpoint 做 fail-soft）。"""
    resp = client.get("/api/workflows/no-such/agents")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workflow not found"


# ── Endpoint 4: GET /api/workflows/{name}/agents/{agent}/tree ──────────────────


def test_get_agent_tree_golden(wf_dir, client):
    """tree JSON 深匹配（plan §golden-fixture）：过滤 hidden/pycache/pyc，目录先于文件。"""
    resp = client.get("/api/workflows/mywf/agents/foo/tree")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "foo"
    assert body["root"].endswith("foo")  # resources_root 绝对路径
    # golden：仅 agent.md / references/ / scripts/ 三项（hidden/pycache/pyc 被过滤）
    # 排序：目录先于文件 → references, scripts 在前；agent.md 在后
    top_paths = [n["path"] for n in body["nodes"]]
    assert top_paths == ["references", "scripts", "agent.md"]
    # 目录项 children 非 null，文件项 children=null
    by_path = {n["path"]: n for n in body["nodes"]}
    assert by_path["references"]["is_dir"] is True
    assert by_path["references"]["children"] == [
        {
            "path": "references/doc.md",
            "name": "doc.md",
            "is_dir": False,
            "size": by_path["references"]["children"][0]["size"],
            "children": None,
        }
    ]
    assert by_path["scripts"]["is_dir"] is True
    # scripts/_mod.pyc 被过滤，仅 helper.py
    assert [c["path"] for c in by_path["scripts"]["children"]] == ["scripts/helper.py"]
    assert by_path["agent.md"]["is_dir"] is False
    assert by_path["agent.md"]["children"] is None
    # agent.md size > 0（实际内容）
    assert by_path["agent.md"]["size"] > 0


def test_get_agent_tree_unknown_workflow_404(wf_dir, client):
    resp = client.get("/api/workflows/no-such/agents/foo/tree")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workflow not found"


def test_get_agent_tree_unknown_agent_404(wf_dir, client):
    resp = client.get("/api/workflows/mywf/agents/ghost/tree")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "agent not found"


# ── Endpoint 5: GET /api/workflows/{name}/agents/{agent}/file?path=... ────────


def test_get_agent_file_text(wf_dir, client):
    """正常读文本文件 → 200 envelope（plan §M2）。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=scripts/helper.py")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "scripts/helper.py"
    assert body["text"] == "def hello():\n    return 'world'\n"
    assert body["ext"] == "py"
    assert body["size"] > 0
    assert body["truncated"] is False


def test_get_agent_file_markdown(wf_dir, client):
    """.md 文件 ext 字段无点。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=agent.md")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ext"] == "md"
    assert "Foo" in body["text"]


def test_get_agent_file_not_found(wf_dir, client):
    """文件不存在 → 404 file not found。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=missing.txt")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "file not found"


# ── 穿越守卫 8 范式（plan §测试 + run_manager.py:277-300 算法）───────────────────


def test_traversal_parent_escape_404(wf_dir, client):
    """``..`` 越界 → 404 file not found。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=../bar.md")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "file not found"


def test_traversal_absolute_path_404(wf_dir, client):
    """绝对路径 → 404（resolve 后不在 root 下）。"""
    # 跨平台：/etc/passwd 在 posix 是绝对；Windows 下 root/"/etc/passwd" 拼不成绝对
    # 但 ``is_file()`` 必 False（不存在），仍 404。无需 platform.dispatch。
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=/etc/passwd")
    assert resp.status_code == 404


def test_traversal_empty_path_404(wf_dir, client):
    """空 path → 404。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=")
    assert resp.status_code == 404


def test_traversal_symlink_404(wf_dir, client):
    """symlink（即便指向 root 内）→ 404（防御纵深，抄 run_manager.test）。"""
    foo_dir = wf_dir / "agents" / "foo"
    target = foo_dir / "agent.md"
    link = foo_dir / "link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=link.md")
    assert resp.status_code == 404


def test_traversal_url_encoded_dotdot_404(wf_dir, client):
    """URL 编码 ``%2e%2e%2f`` → FastAPI 解码后等同 ``../`` → 404。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=%2e%2e%2fbar.md")
    assert resp.status_code == 404


def test_traversal_null_byte_404(wf_dir, client):
    """null byte ``foo%00bar`` → Python pathlib ValueError → 捕获 → 404。"""
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=foo%00bar.md")
    assert resp.status_code == 404


def test_traversal_binary_file_422(wf_dir, client):
    """二进制文件（前 2048 字节含 \\x00）→ 422 binary file。"""
    foo_dir = wf_dir / "agents" / "foo" / "scripts"
    bin_file = foo_dir / "data.bin"
    bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=scripts/data.bin")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "binary file"


def test_traversal_oversized_file_422(wf_dir, client):
    """超 1MB 文件 → 422 file too large。"""
    foo_dir = wf_dir / "agents" / "foo" / "scripts"
    big = foo_dir / "big.txt"
    # 2MB 文本（>1_000_000 上限）
    big.write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
    resp = client.get("/api/workflows/mywf/agents/foo/file?path=scripts/big.txt")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith("file too large:")
    assert "1000000" in detail
