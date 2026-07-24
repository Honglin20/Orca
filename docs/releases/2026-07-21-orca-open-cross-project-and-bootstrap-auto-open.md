# Release: `orca open` 跨项目端口占用修复 + `bootstrap` 默认自动开 web

**日期**: 2026-07-21
**计划**: [`docs/plans/2026-07-21-orca-open-cross-project.md`](../plans/2026-07-21-orca-open-cross-project.md)
**SPEC**: `docs/specs/web-attach-and-default-spec.md` §5 + 新增 §5a

---

## 背景（两个问题）

### A — `orca open` 跨项目端口占用：静默挂错 tape

`orca open <run_id>`（`orca/iface/cli/commands.py:_open_run`）把**相对** tape 路径
`runs/<id>.jsonl` 跨进程 POST 给「7428 上探测到的任何 orca server」，server 端
`run_manager.resolve_tape_path` 按自己 CWD `resolve()` → 挂到**别的项目**的 tape（静默错），
或跨项目边界检查 403（困惑性失败）。`run` 命令的 `_post_run_to_existing` 早已用
`str(Path(...).resolve())` 绝对路径化并有 docstring 警告此坑，`open` 漏了同样的硬化。叠加
「7428 有 orca 就无脑复用、不校验项目身份」，B 项目的 `orca open` 复用 A 项目的 server。

### B — `bootstrap` 希望启动即自动开 web（便利）

in-session 跑 workflow 时，手动 `orca open <id>` 看 live 进度多一步。用户希望 `orca bootstrap`
起完 run 自动开 web。

---

## 方案

### A — 项目感知复用 + 绝对路径

1. **`orca/iface/web/_identity.py`（新）**：`runs_dir_fingerprint(runs_dir) = sha1(resolve(runs_dir))[:12]`，
   stdlib-only。client（`_runs_dir_fp`）与 server（health）同算法 → 同项目指纹一致。**指纹非明文**
   （health 默认 bind `0.0.0.0` 网络可达，明文目录是信息泄漏；sha1 不可逆）。
2. **`attach.py` health**：加 `runs_dir_fp` 字段（纯加法，向后兼容；旧 server 缺字段 → client 视为
   foreign → 安全降级 spawn）。
3. **`orca/iface/cli/web_registry.py`（新）**：per-project 登记文件 `<runs_dir>/.orca-web.json = {port,
   runs_dir_fp}`（无 pid）。spawn 后写、复用前读；**探测权威、registry 仅 hint**（读到 port 后仍
   probe+指纹校验）；陈旧自愈。
4. **`commands.py _open_run` 重写**：tape 绝对路径化 + 决策块——本项目 server（fp 匹配）复用 →
   registry 找本项目 server → 空闲端口起新 server 并登记。`_default_runs_dir` 从 `bg_runner.default_tape_path`
   派生（runs_dir 单源）。`_spawn_background_serve` 保持返 bool（未改签名）。
5. **SPEC 同步**：`web-attach-and-default-spec.md` L87/L88 + 新增 §5a（含指纹算法 / 隐私 threat note /
   缺字段降级）。

### B — `bootstrap` 默认自动开 web

- `in_session/cli.py bootstrap` 加 `--open-web/--no-open-web` + `ORCA_BOOTSTRAP_OPEN_WEB` env
  （flag > env > 默认**开**）。
- 新 `_spawn_open_web`：post-lock 块（marker 落 + sidechain 守护后）detach spawn
  `python -m orca.iface.in_session.cli open <run_id>`（与 chart/sidechain 守护同款 detach：
  `start_new_session=True` + `close_fds=True` + 日志重定向 + 全 body try/except OSError soft-fail）。
  **契约安全**：子进程 stdio 重定向到日志 → bootstrap 的 stdout JSON 契约**零污染**；soft-fail，
  绝不 fail bootstrap。schema-only 路径（不带 `--inputs`）不触发。`orca open` 本身零改动，复用 A 的
  `_open_run`。

---

## spec-review + code-reviewer 闭环

- **spec-reviewer（含 evaluator）两轮**：conditional-pass（6 blocker + 5 HIGH）。全接受并落实：
  B1（SPEC 同步）/ B2（runs_dir 单源派生 + 守门测试）/ B3（指纹下沉 `_identity.py` stdlib 模块，禁副本）/
  B4（`_register_my_port` 写失败 loud warn 不阻断）/ H1（registry 删 pid，`_spawn_background_serve` 保 bool，
  零签名 churn）/ H3（绝对路径安全样例）/ H4（stdout 契约 oracle 精确化）/ H5（隐私 threat note）。
  **降级 B5**（registry 不加 flock）：race 窗口窄 + probe-权威保证正确性 + 持锁 10s 串行化代价 > 偶发
  孤儿，文档化为 R10 已知限制。
- **code-reviewer**：需修后合（无 🔴，3 🟡 test-coverage gap）—— 补 `test_open_register_failure_loud_warn`
  （B4 loud-warn 守门）/ `test_open_stale_registry_falls_through_to_spawn`（自愈契约）/ `test_spawn_open_web_uses_module_cmd_and_detached`
  （Popen cmd + detached 守门）；加 L-1 docstring 说明 `sys.executable -m` 选择。

---

## 测试策略

- **A 测试**：`test_web_registry.py`（新，read/write roundtrip + 损坏自愈 + 原子写）；`test_attach.py`
  加 health-fp 真 ASGI 测试（FastAPI TestClient，非 mock）+ 绝对路径安全样例（H3）+ 无明文泄漏；
  `test_web_default_and_open.py` 更新 3 个既有 mock（补 `runs_dir_fp`）+ 新增 `TestOpenProjectAwareReuse`
  （foreign spawn / registry 复用 / 绝对路径回归 / 显式端口 foreign exit 2 / loud-warn / stale-registry 自愈）
  + `test_fingerprint_single_point_consistency` / `test_default_runs_dir_single_source`（DRY/单源守门）。
- **B 测试**：`test_bootstrap_open_web.py`（新）—— `_bootstrap_open_web_enabled` 全分支 + 集成（默认调 /
  `--no-open-web` / env=0 / schema-only 不调 / `--format prompt` 也触发）+ stdout 契约 H4 + soft-fail +
  Popen cmd 守门。
- **IronLaws**：加 `test_web_does_not_import_cli`（F10，守依赖单向）。
- **回归**：`tests/iface/cli/ + tests/iface/in_session/ + tests/iface/web/` 全跑 **987 passed / 31 skipped**。
  另 2 个**既有失败**（`test_bg_run_ps_logs_wait_e2e` 需真后端、`test_install_cc_nudge_script_never_calls_next`
  脚本内嵌 docstring 反引号）已用 `git stash` 证伪为基线既有、非本次回归（本次未碰 run/install 路径）。

---

## 文件清单

**新增**
- `orca/iface/web/_identity.py` —— 项目身份指纹（stdlib-only）
- `orca/iface/cli/web_registry.py` —— per-project web server 端口登记
- `tests/iface/cli/test_web_registry.py`
- `tests/iface/in_session/test_bootstrap_open_web.py`
- `docs/plans/2026-07-21-orca-open-cross-project.md`

**修改**
- `orca/iface/web/routes/attach.py` —— health 加 `runs_dir_fp`
- `orca/iface/cli/commands.py` —— `_open_run` 项目感知重写 + 5 个 helper
- `orca/iface/in_session/cli.py` —— bootstrap `--open-web` + `_spawn_open_web` + `_bootstrap_open_web_enabled`
- `docs/specs/web-attach-and-default-spec.md` —— L87/L88 + §5a
- `tests/iface/cli/test_web_default_and_open.py` —— 既有 mock 更新 + 项目感知测试类 + IronLaws 守门
- `tests/iface/web/test_attach.py` —— health-fp 真 ASGI + 绝对路径安全样例

---

## 已知限制 / Follow-up

- **R4（范围外）**：`orca run` 的 reuse 分支（`_post_run_to_existing`）同样「7428 有 orca 即复用」，
  对**发起新 run** 有同类跨项目隐患（会把 run 落到别项目 runs_dir）。本次未动，记 follow-up。
- **R8（custom runs_dir）**：`tars serve` 不接受 `--runs-dir`，`orca open` 也不透传；`mcp --with-web --runs-dir`
  起的 server 与 client 指纹永远不匹配 → 永远 spawn 新 server。Follow-up：`tars serve --runs-dir` + 透传。
- **R10（并发）**：并发 `orca open`（auto-open + 手动同窗口）极小概率产生 1 个闲置孤儿 server，probe-权威
  保证正确性，由下次成功 registry 自愈；不做 flock（持锁串行化代价更大）。
- **H5（隐私 follow-up）**：指纹虽不可逆，但 bind `0.0.0.0` 时内网观察者可跨 session 关联同项目。缓解
  follow-up：fp 仅 loopback/header 下返回，或默认 bind 127.0.0.1。
