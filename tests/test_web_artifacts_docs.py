"""test_web_artifacts_docs.py —— run artifacts 只读文档端点（web SPEC §6.1）。

覆盖意图（非仅行为）：
  1. **白名单不变量**：artifacts 端点只能读该 run artifacts 根内的普通文本文件——
     越界（``..`` / 绝对路径）/ symlink（末端与中间段）/ 不存在 / 二进制全部拒绝，
     意图是「面板不可能被诱导读出 run 产物之外的任意 fs 内容」。
  2. **attach 语义**：未知 run_id 先懒挂载、仍未知 → 404（与 /meta /assets 同款）。
  3. **只读红线**：响应是纯文件正文（text/plain; charset=utf-8），不含 fs 绝对路径。
  4. **体量上限**：> 1MB → 413 fail loud（复用 workflows 路由的 MAX_FILE_BYTES）。
  5. **守卫等价对拍（W1-T1，先于抽取的回归网）**：共享守卫（file_text.safe_resolve，
     经 run_manager.resolve_asset_path 走全链）与抽前旧内联逻辑在同一矩阵上逐例一致
     ——既有 ``/assets`` 端点行为零变化的机械证据。

环境：与其它 web 测试同款（make_manager 短路径 runs_dir + httpx ASGITransport，
无 pytest-asyncio，统一 ``run_async``）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from orca.iface.web.server import create_app

from tests.iface.web.conftest import demo_linear_yaml, make_manager, run_async


@pytest.fixture(autouse=True)
def _isolate_project_detect(monkeypatch, tmp_path):
    """env 隔离（M5）：让 start_run 的 project detect 失败，run 落默认 runs_dir。

    不隔离时 ``detect_project_root()`` 命中本仓库（cwd 有 workflows/）→ 每 run 把
    tape / artifacts 写进真实仓库 ``runs/`` + 写全局 ``~/.orca/projects.json``。
    detect 失败是 RunManager 支持的回退路径（warn + 默认 runs_dir），测试借此不污染
    真实仓库。既有 web 用例的同类污染是历史遗留，回迁归后续（不在 W-P1 范围）。
    """
    def _raise():
        raise RuntimeError("isolated for tests")

    monkeypatch.setattr(
        "orca.iface.web.run_manager.detect_project_root", _raise
    )
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / "orca-home"))


def _client_factory(manager):
    """build app + ASGITransport async context manager factory（同 test_routes.py）。"""
    app = create_app(manager)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _start_run(manager, tmp_path):
    """起一个纯 script demo run 并等终态（注册进 manager._runs 的最短路径）。"""
    yaml_path = demo_linear_yaml(tmp_path)
    rid = await manager.start_run(str(yaml_path), {}, None, None)
    await manager.wait_done(rid, timeout=10.0)
    return rid


def _artifacts_root(manager, rid: str) -> Path:
    """该 run 的 artifacts 权威根——与生产同源（tape 所在 runs 目录派生）。

    引擎按 ``artifacts_dir_for_run(<tape 所在 runs 目录>, run_id)`` 注入
    ``$ORCA_ARTIFACTS_DIR``；端点也从 tape 位置派生（``resolve_artifacts_root``），
    故测试造盘面 / 断言一律从 tape 位置派生，不硬拼 manager.runs_dir。
    """
    tp = manager.get_handle(rid).tape.path
    return tp.parent / rid / "artifacts"


def _make_artifacts(manager, rid: str) -> Path:
    """落盘该 run 的 artifacts 根 + 样例文档（agent 本应写此处；测试直接造）。"""
    root = _artifacts_root(manager, rid)
    (root / "baseline").mkdir(parents=True, exist_ok=True)
    (root / "baseline" / "business_logic.md").write_text(
        "# baseline\n\n- 五段结构\n- 训练设备: npu\n", encoding="utf-8"
    )
    (root / "base" / "profile").mkdir(parents=True, exist_ok=True)
    (root / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        "# bottleneck\n", encoding="utf-8"
    )
    (root / "base" / "origin_anchor.json").write_text(
        '{"baseline_makespan_cycles": 1000}', encoding="utf-8"
    )
    return root


# ── web SPEC §6.1-2：正常读取 + 只读红线 ────────────────────────────────────


def test_artifacts_file_serves_md_and_json(tmp_path):
    """md / json 正文 → 200 + 正确内容 + text/plain charset（点开拉正文 happy path）。

    意图：面板经清单 path 拉到的正文与盘上逐字一致（json 走同端点原样返回）。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        _make_artifacts(manager, rid)
        async with _client_factory(manager) as client:
            for rel, expect in [
                (
                    "baseline/business_logic.md",
                    "# baseline\n\n- 五段结构\n- 训练设备: npu\n",
                ),
                (
                    "base/origin_anchor.json",
                    '{"baseline_makespan_cycles": 1000}',
                ),
                (
                    "base/profile/mfu_bottleneck_report.md",
                    "# bottleneck\n",
                ),
            ]:
                resp = await client.get(
                    f"/api/runs/{rid}/artifacts/file", params={"path": rel}
                )
                assert resp.status_code == 200, f"{rel}: {resp.status_code}"
                assert resp.text == expect
                assert resp.headers["content-type"].startswith("text/plain")
                assert "charset=utf-8" in resp.headers["content-type"]
                # 只读红线：响应不含 fs 绝对路径（web §2.2）
                assert str(_artifacts_root(manager, rid)) not in resp.text
        await manager.shutdown()

    run_async(go())


# ── web SPEC §6.1-1：越界 / symlink / 不存在 / 未知 run → 404 ────────────────


def test_artifacts_file_traversal_404(tmp_path):
    """``..`` / 绝对路径 / 编码越界 → 404（path escape 守卫，fail loud）。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        _make_artifacts(manager, rid)
        async with _client_factory(manager) as client:
            # 试图逃逸到 tape 文件本身（URL 编码 ..，同 test_routes.py 资产越界用例）
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": f"../../{rid}.jsonl"},
            )
            assert resp.status_code == 404
            # 绝对路径 escape
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file", params={"path": "/etc/passwd"}
            )
            assert resp.status_code == 404
            # 多级 .. 拼接
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "baseline/../../../etc/passwd"},
            )
            assert resp.status_code == 404
            # null byte（path 含 \x00）→ 共享守卫 fail closed → 404（抽前旧逻辑会上抛 500）
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "baseline/ba\x00d.md"},
            )
            assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_binary_and_non_utf8_422(tmp_path):
    """二进制（前 2048 字节含 NUL）/ 非 utf-8 文本 → 422（失败路径显式，fail loud）。

    意图：artifacts 根内混入不可文本化文件时端点给出可判别的 4xx，而不是 500 崩面。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        root = _make_artifacts(manager, rid)
        (root / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\x00\x00BINARY")
        # GBK 编码文本：无 NUL（过二进制探测）但非 utf-8 可解码
        (root / "gbk.md").write_bytes("中文".encode("gbk"))
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file", params={"path": "plot.png"}
            )
            assert resp.status_code == 422
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file", params={"path": "gbk.md"}
            )
            assert resp.status_code == 422
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_missing_404(tmp_path):
    """run 存在、artifacts 根存在、文件不存在 → 404。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        _make_artifacts(manager, rid)
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "baseline/never.md"},
            )
            assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_missing_root_404(tmp_path):
    """run 存在但派生 artifacts 目录不存在 → 404（S-8「派生目录不存在 → 404」语义）。"""
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        # 前置断言锚到权威根（tape 派生），不锚 manager.runs_dir
        assert not _artifacts_root(manager, rid).exists()
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
            assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_unknown_run_404(tmp_path):
    """未知 run_id → 先 ensure_attached（懒挂载）→ 仍未知 → 404（attach 语义）。"""
    manager = make_manager(tmp_path)

    async def go():
        async with _client_factory(manager) as client:
            resp = await client.get(
                "/api/runs/no-such-run/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_rejects_symlink(tmp_path):
    """末端 symlink / 指出根外的中间段 symlink → 404（防御纵深，防 symlink 逃逸）。

    意图：agent 误/恶意在 artifacts 内放 symlink 指向敏感文件时，端点不可能吐出。
    注意中间段语义与 ``resolve_asset_path`` 等强度（legacy 同款）：中间段 symlink
    指向**根内**目录 → resolve 后仍在根内 → 放行；指向**根外** → ``relative_to``
    越界拦截。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        root = _make_artifacts(manager, rid)
        outside = root.parent / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        # 末端 symlink：指向根内合法文件（也拒——防御纵深）
        end_link = root / "baseline" / "link.md"
        try:
            end_link.symlink_to(root / "baseline" / "business_logic.md")
        except OSError:
            pytest.skip("filesystem does not support symlinks")
        # 中间段 symlink（目录段）：指向根内目录 vs 指向根外目录各一
        mid_link_dir = root / "link_dir"
        mid_link_dir.symlink_to(root / "baseline")
        escape_dir = root / "escape_dir"
        escape_dir.symlink_to(root.parent, target_is_directory=True)
        async with _client_factory(manager) as client:
            for rel in [
                "baseline/link.md",              # 末端 symlink → 拒
                "escape_dir/outside-secret.txt",  # 中间段指出根外 → 拒
            ]:
                resp = await client.get(
                    f"/api/runs/{rid}/artifacts/file", params={"path": rel}
                )
                assert resp.status_code == 404, f"{rel}: {resp.status_code}"
            # 中间段指向根内 → resolve 后仍在根内 → 放行（与 legacy 等强度）
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "link_dir/business_logic.md"},
            )
            assert resp.status_code == 200
        await manager.shutdown()

    run_async(go())


# ── web SPEC §6.1-2：超 1MB → 413 ──────────────────────────────────────────


def test_artifacts_file_too_large_413(tmp_path):
    """超 ``MAX_FILE_BYTES``（1MB，与 workflows 路由共享）→ 413 fail loud。"""
    from orca.iface.web.file_text import MAX_FILE_BYTES

    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        root = _make_artifacts(manager, rid)
        (root / "big.md").write_text(
            "x" * (MAX_FILE_BYTES + 1), encoding="utf-8"
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file", params={"path": "big.md"}
            )
        assert resp.status_code == 413
        # 边界：上限判定是 >，== MAX 与 MAX-1 均放行（钉死 > 与 >= 分界）
        (root / "exact.md").write_text(
            "y" * (MAX_FILE_BYTES - 1), encoding="utf-8"
        )
        (root / "at-limit.md").write_text(
            "z" * MAX_FILE_BYTES, encoding="utf-8"
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file", params={"path": "exact.md"}
            )
            assert resp.status_code == 200
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file", params={"path": "at-limit.md"}
            )
            assert resp.status_code == 200
        await manager.shutdown()

    run_async(go())


# ── web SPEC §6.3-2（W3-T2）：端点只读——写方法在路由层即被拒 ─────────────────


def test_artifacts_file_rejects_write_methods(tmp_path):
    """W3-T2 只读验证：POST/PUT/PATCH/DELETE 全部 405——端点不存在任何写路径，
    写请求连 handler 都不进（GET-only 路由），盘上文件零改动。

    意图：面板 + 端点全程无写请求（web §5「只读」红线的服务侧锚点）。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        root = _make_artifacts(manager, rid)
        before = (root / "baseline" / "business_logic.md").read_text("utf-8")
        async with _client_factory(manager) as client:
            for method in ("post", "put", "patch", "delete"):
                kwargs = {} if method == "delete" else {
                    "content": "malicious overwrite"}
                resp = await getattr(client, method)(
                    f"/api/runs/{rid}/artifacts/file",
                    params={"path": "baseline/business_logic.md"},
                    **kwargs,
                )
                assert resp.status_code == 405, f"{method}: {resp.status_code}"
        assert (root / "baseline" / "business_logic.md").read_text(
            "utf-8") == before  # 盘上零改动
        await manager.shutdown()

    run_async(go())


# ── web SPEC §6.1-3：共享守卫与 resolve_asset_path 等价对拍（W1-T1）──────────


def _legacy_asset_guard(assets_root: Path, rel_path: str) -> Path | None:
    """抽前 ``RunManager.resolve_asset_path`` 内联守卫的逐字复刻（对拍基准）。

    W1-T1 要求「先写等价对拍单测再抽」：本函数是 refactor 前的守卫逻辑快照，
    与共享守卫（经 resolve_asset_path 走全链）在同一矩阵上逐例比对。
    """
    assets_root = assets_root.resolve()
    decoded = rel_path.strip()
    if not decoded:
        return None
    unresolved = assets_root / decoded
    if unresolved.is_symlink():
        return None
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(assets_root)
    except ValueError:
        return None
    if candidate.is_symlink():
        return None
    if not candidate.is_file():
        return None
    return candidate


def test_shared_guard_equivalent_to_legacy_resolve_asset_path(tmp_path):
    """共享守卫 vs 抽前旧逻辑：同矩阵逐例一致（既有 /assets 端点零变化的机械证据）。

    意图：守卫抽取是纯重构——任何输入下新旧判定不漂移，才有资格说
    「/assets 行为零变化」。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        assets_dir = manager.runs_dir / rid / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "real.png").write_bytes(b"\x89PNG\r\n\x1a\nREAL")
        (assets_dir / "sub").mkdir()
        (assets_dir / "sub" / "deep.png").write_bytes(b"PNG2")
        outside = manager.runs_dir / rid / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = None
        mid_dir = None
        try:
            link = assets_dir / "link.png"
            link.symlink_to(assets_dir / "real.png")
            mid_dir = assets_dir / "link_dir"
            mid_dir.symlink_to(assets_dir / "sub", target_is_directory=True)
        except OSError:
            pytest.skip("filesystem does not support symlinks")
        assets_root = manager.runs_dir / rid / "assets"
        matrix = [
            "real.png",                      # 合法普通文件
            "sub/deep.png",                  # 嵌套合法
            "never.png",                     # 不存在
            "sub",                           # 目录非文件
            "",                              # 空
            "  ",                            # 空白
            f"../{rid}.jsonl",               # .. 越界（tape）
            "/etc/passwd",                   # 绝对路径
            "sub/../../assets/real.png",     # .. 归一后回根内（旧逻辑 resolve 后合法）
            "link.png",                      # 末端 symlink
            "link_dir/deep.png",             # 中间段 symlink
        ]
        for rel in matrix:
            legacy = _legacy_asset_guard(assets_root, rel)
            current = manager.resolve_asset_path(rid, rel)
            assert current == legacy, f"guard drift on {rel!r}: {current} != {legacy}"
        # 异常输入类（null byte）：旧逻辑上抛 ValueError（→500），共享版 fail closed
        # 收敛 None（→404）——唯一有意差异，显式断言锁定方向（不漂回 500）。
        with pytest.raises(ValueError):
            _legacy_asset_guard(assets_root, "ba\x00d.png")
        assert manager.resolve_asset_path(rid, "ba\x00d.png") is None
        # 未知 run：新旧都 None（守卫前置分支）
        assert manager.resolve_asset_path("nope", "real.png") is None
        await manager.shutdown()

    run_async(go())


# ── 2026-09-07 SPEC §1（A1）：project-scoped artifacts 根（orca_env.sh 记录）────


def _write_orca_env(manager, rid: str, lines: list[str]) -> None:
    """写 run 目录 ``orca_env.sh``（bootstrap 同位：tape 所在 runs 目录 / <run_id>/）。

    值的引号形态与 bootstrap 写盘一致（shlex 单引号），覆盖引擎可能已写的文件，
    保证用例盘面确定性。
    """
    run_dir = manager.get_handle(rid).tape.path.parent / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "orca_env.sh").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_artifacts_file_recorded_project_scoped_root(tmp_path):
    """happy：``orca_env.sh`` 记录的 project-scoped 目录存在 → 端点按该根解析。

    意图（SPEC §1 验收 1）：prof-opt 等 project-scoped 产物的真实落点记录在
    bootstrap env 文件里；同机场景端点必须读得到（此前的 404 根因）。
    红线（SPEC §1 验收 5 / R-17）：recorded 根与 per-run 派生根**两个绝对路径**
    都不得出现在响应正文——双根同时造盘，防只断言派生根的假绿。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        scoped = tmp_path / "proj-root" / "artifacts" / "prof-opt"
        (scoped / "variants" / "v1").mkdir(parents=True)
        (scoped / "variants" / "v1" / "assessment.md").write_text(
            "# v1 评估\n", encoding="utf-8"
        )
        # 双根同时在场：recorded（scoped）+ per-run（_make_artifacts 造），
        # recorded 优先且两者都不泄露。
        _make_artifacts(manager, rid)
        _write_orca_env(
            manager, rid, [f"export ORCA_ARTIFACTS_DIR='{scoped}'"]
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "variants/v1/assessment.md"},
            )
            assert resp.status_code == 200
            assert resp.text == "# v1 评估\n"
            # per-run 根存在但 recorded 根优先：per-run 下无此文件也 200 证优先级
            assert not (_artifacts_root(manager, rid) / "variants").exists()
            # recorded 根一旦解析成功即独占：per-run 独有文件不跨根兜底 → 404
            resp_per_run = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
            assert resp_per_run.status_code == 404
            # 红线：双根绝对路径均不泄露
            assert str(scoped) not in resp.text
            assert str(_artifacts_root(manager, rid)) not in resp.text
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_recorded_missing_path_falls_back(tmp_path):
    """sad：记录的路径不存在（异机）→ 回退 per-run 派生根；两处皆无 → 404。

    意图（SPEC §1 验收 2）：``orca_env.sh`` 里的绝对路径指向 workflow 机器的本地
    盘，serve 机上不存在 → 端点回退既有 per-run 派生（老 run 语义不变），仍无则
    404 fail loud——不因 env 记录失效而 500。
    """
    manager = make_manager(tmp_path)

    async def go():
        rid = await _start_run(manager, tmp_path)
        _make_artifacts(manager, rid)
        _write_orca_env(
            manager, rid,
            ["export ORCA_ARTIFACTS_DIR='/nonexistent/remote/artifacts'"],
        )
        async with _client_factory(manager) as client:
            # per-run 根有文件 → 回退后 200
            resp = await client.get(
                f"/api/runs/{rid}/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
            assert resp.status_code == 200

        # 第二个 run：记录路径不存在且 per-run 根也不存在 → 404
        rid2 = await _start_run(manager, tmp_path)
        _write_orca_env(
            manager, rid2,
            ["export ORCA_ARTIFACTS_DIR='/nonexistent/remote/artifacts'"],
        )
        assert not _artifacts_root(manager, rid2).exists()
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid2}/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
            assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_artifacts_file_recorded_parse_edges(tmp_path):
    """edge：引号含空格路径 / 多行重复 export（末行生效）/ 相对路径 / 坏引号行。

    意图（SPEC §1 验收 4）：解析逐字对齐 shell source 语义——末条 export 胜、
    坏引号行跳过**不崩**（R-5 防 500）、其后有效行照常生效；相对路径视为无记录
    回退 per-run。
    """
    manager = make_manager(tmp_path)

    async def go():
        # (a) 值带 shlex 引号且含空格：路径本体有效 → 按该根解析
        rid_a = await _start_run(manager, tmp_path)
        scoped = tmp_path / "proj a" / "artifacts prof-opt"
        (scoped / "base").mkdir(parents=True)
        (scoped / "base" / "origin_anchor.json").write_text(
            '{"ok": 1}', encoding="utf-8"
        )
        _write_orca_env(
            manager, rid_a, [f"export ORCA_ARTIFACTS_DIR='{scoped}'"]
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid_a}/artifacts/file",
                params={"path": "base/origin_anchor.json"},
            )
            assert resp.status_code == 200
            assert resp.text == '{"ok": 1}'

        # (b) 多行重复 export：末行（空值）胜 → 无记录 → 回退 per-run
        rid_b = await _start_run(manager, tmp_path)
        _make_artifacts(manager, rid_b)
        _write_orca_env(
            manager, rid_b,
            [
                "export ORCA_ARTIFACTS_DIR='/nonexistent/first'",
                "export ORCA_ARTIFACTS_DIR=",
            ],
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid_b}/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
            assert resp.status_code == 200  # 空值无记录 → per-run 回退

        # (c) 相对路径 → 视为无记录 → 回退 per-run（无文件 → 404，不 500）
        rid_c = await _start_run(manager, tmp_path)
        _write_orca_env(
            manager, rid_c, ["export ORCA_ARTIFACTS_DIR=relative/artifacts"]
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid_c}/artifacts/file",
                params={"path": "baseline/business_logic.md"},
            )
            assert resp.status_code == 404

        # (d) 坏引号行 + 其后有效行：坏行跳过、有效行生效（R-5：不崩不 500）
        rid_d = await _start_run(manager, tmp_path)
        _write_orca_env(
            manager, rid_d,
            [
                "export ORCA_ARTIFACTS_DIR='/nonexistent/broken",  # 坏引号
                f"export ORCA_ARTIFACTS_DIR='{scoped}'",           # 其后有效行
            ],
        )
        async with _client_factory(manager) as client:
            resp = await client.get(
                f"/api/runs/{rid_d}/artifacts/file",
                params={"path": "base/origin_anchor.json"},
            )
            assert resp.status_code == 200
            assert resp.text == '{"ok": 1}'
        await manager.shutdown()

    run_async(go())
