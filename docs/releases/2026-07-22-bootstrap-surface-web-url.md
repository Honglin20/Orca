# Release: `bootstrap` 启动即把 web 链接反馈给用户

**日期**: 2026-07-22
**前置**: [`2026-07-21-orca-open-cross-project-and-bootstrap-auto-open.md`](2026-07-21-orca-open-cross-project-and-bootstrap-auto-open.md)（commit `9677c1e`，bootstrap 默认自动开 web）

---

## 背景

`9677c1e` 让 `orca bootstrap` 默认自动开 web——但实现是 detach 一个 `orca open` 子进程
（`_spawn_open_web`），其 stdio 重定向到 `runs/.orca-open-<run_id>.log`。结果：真正的
「Orca Web UI → ...」echo **进了日志文件**，bootstrap 终端只看到 JSON 契约，**链接从不进用户能
看到的通道**。远程 / SSH / headless 下 `webbrowser.open` 又不弹窗 → 用户经常「只顾着启动 workflow，
没能把链接反馈给用户」，得手动 `orca open <id>` 才拿得到链接。

## 方案

bootstrap 自身进程在启动当下即算出 web URL（单一真相源 `resolve_web_endpoint`），分两路显式吐给用户：

1. **JSON `reply["web_url"]`** —— 模型驱动路径拿得到（`http://{display_host}:{port}/runs/{run_id}`）
2. **stderr echo `Orca Web UI → <url>`** —— 直接终端用户可见，不污染 stdout JSON 契约

新增 helper `_resolve_web_url(run_id)`：函数内 lazy import `resolve_web_endpoint`（避开
`iface.in_session ↔ iface.cli` 循环 import，与同文件 `_default_tape_path` / in-session `open`
委托同款 pattern），任一异常 → soft-fail 返 None，绝不阻断 bootstrap（与 `_spawn_open_web`
fail-open 语义一致）。

### 为什么不把 URL 放进 `prompt` 字段

`prompt` 是 `(node, run_id)` 的纯函数，bootstrap 首发与 `orca next` 的 idempotent 重发必须
**逐字相等**（`test_f1_resume_flow_status_resumable_then_next_no_output_resends_prompt` 钉的
不变量）。web 链接是 bootstrap 时刻的旁路通知，不属于节点 prompt——放进去会破坏该不变量。故只走
stderr + JSON 两路。

### 已知 limitation

URL 基于 config（host/port）解析，**不探活端口归属**。若默认端口 7428 被非-orca 进程占用，
detached `orca open` 会 probe 后另选端口，而此处吐给用户的 URL 仍指向 7428。概率低；与 soft-fail
语义一致，不在此处引入端口探活（bootstrap 须保持轻量，端口探活是 `orca open` 自身 probe 的职责）。

## 改动

- `orca/iface/in_session/cli.py`：新增 `_resolve_web_url`；bootstrap 主体开 web 时算 URL → JSON
  字段 + stderr echo。
- `tests/iface/in_session/test_bootstrap_open_web.py`：改写 `test_bootstrap_stdout_contract_clean`
  （旧 H4「stdout 不含 http://」→ 新「显式带 web_url + stderr 行」）+ 新增反向契约
  `test_bootstrap_no_open_web_omits_web_url` + fail-soft 契约 `test_bootstrap_resolve_web_url_failure_is_soft`。

## 验证

`python -m pytest tests/iface/in_session/test_bootstrap_open_web.py -q` → **25 passed**。
code-reviewer 一轮闭环：0 🔴，2 🟡（fail-soft 单测 + 端口归属 docstring）全修。
