# 2026-08-03 Web RunList 重设计 — 真机 E2E 验证 evidence log

**Commit under test**: `d782335`（feat(web): RunListPage 重设计——看板视图 + 列表管理 + 多选/排序/批量删/折叠持久/修主题）
**SPEC**: `docs/specs/web-runlist-redesign.md`（§8 AC-1..AC-18 + §10.6 AC-19..AC-23）
**最终结论**: **E2E 全绿**（修复 1 个 AC-4 真实 regression 后；详见下文）。

---

## 0. 环境确认（用户报「无 Python」的实测复核）

| 探针 | 结果 |
|---|---|
| `cmd.exe /c where python` | 仅 `WindowsApps` Store 别名（无效，符合用户描述） |
| `py`/`uv`/`conda`/`pipx`/`pip`（Git Bash + PowerShell `Get-Command`） | 全部 NOT FOUND |
| `D:\Projects\Orca\.venv\bin\python` | **存在但 symlink 指向 Linux miniconda** —— 在 Git Bash 不可执行 |
| `wsl.exe -l -v` | **Ubuntu (Running)** ✓ |
| `wsl.exe /mnt/d/Projects/Orca/.venv/bin/python --version` | **Python 3.12.13** ✓（venv 创建于 WSL，可在 WSL 内执行） |

**所以 Python 可用——但只能通过 WSL+venv。** 后续所有 Python 命令都从 WSL 执行。

依赖确认（venv 内）：`fastapi 0.139.0` / `pytest 9.1.1` / `httpx 0.28.1` / `starlette 1.3.1` / `uvicorn 0.51.0`。`playwright` 未装，下方说明安装过程。

---

## 1. 后端回归（AC-18：后端零改）

**命令**：
```
wsl.exe -e bash -c "cd /mnt/d/Projects/Orca && .venv/bin/python -m pytest \
  tests/iface/web/test_routes.py tests/iface/web/test_multi_run_phase_c.py -q --tb=short"
```

**真实输出**：`31 passed in 3.20s`

**git diff 验证**：commit `d782335` 不改 `run_manager.py` / `server.py` / `ws_handler.py` / `routes/`，工作树仅含本次重 build 的 `static/*`。**AC-18 ✓**。

二次验证（追加 `test_ws.py`）：`43 passed in 4.40s`。

---

## 2. Playwright 真机 E2E

### 2.1 环境搭建（关键：libnspr4 缺失 → 本地 .deb 解压绕过）

WSL 内无 passwordless sudo（`sudo -n true` → interactive authentication required），无法 `playwright install-deps`。Chrome 启动报 `libnspr4.so: cannot open shared object file`。

绕过方案（无 root）：
```
cd /tmp && apt-get download libnspr4 libnss3 libasound2t64 libatk1.0-0t64 \
  libatk-bridge2.0-0t64 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libxkbcommon0 \
  libatspi2.0-0t64 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxss1 libgtk-3-0t64
mkdir -p /tmp/chromium-libs && for d in /tmp/*.deb; do dpkg-deb -x "$d" /tmp/chromium-libs; done
export LD_LIBRARY_PATH=/tmp/chromium-libs/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH
```
**结果**：`chrome-headless-shell --version` → `Google Chrome for Testing 151.0.7922.34` ✓

### 2.2 第一轮：1 passed / 8 failed

**命令**：`pytest -m integration tests/iface/web/test_playwright_runlist.py -v --tb=short`
**结果**：`8 failed, 1 passed in 202.68s` —— 几乎所有用例在 `wait_for_selector("[data-testid=board]")` 或 `[data-testid=run-item]` 上 5s 超时。

### 2.3 诊断：探针捕获真实 DOM

写 `_probe_runlist_diag.py`（**取证后已删除**），从 WSL 启动 live_server + chromium，dump DOM / 截图 / console。

**第一次跑（默认 `~/.orca`）**：`/api/runs?scope=all` 30s 超时；manager 报 `legacy run kd-nas-* / quant-sensitivity-* / demo_linear-* tape 路径非绝对`。`_run_path_index size: 1060`。

→ **测试基础设施污染**：用户真实 `~/.orca/projects.json`（50KB，含历史项目）被 `discover_runs()` 全量扫描，导致 `/api/runs?scope=all` 在 1060 runs 上挂死。**非前端 bug，非本次 commit 回归。**

**第二次跑（`ORCA_HOME=/tmp/orca-probe-home` 隔离）**：
- `/api/runs?scope=all` HTTP 200 len=699，返回 3 runs ✓
- 页面 34 个 `data-testid`：`board` / `board-column-queued/running/blocked/completed/failed`（**AC-20 五列齐全**）/ 3× `board-card`+`run-item`+`run-checkbox`+`delete-btn`（completed 列内 3 张卡）/ `view-toggle-board/list` / `theme-btn` / `search-input` / `status-chip-*` / `sort-trigger`
- `<html class="light">`（主题）✓
- console errors: 0，page errors: 0
- 截图与 DOM 存档：`D:\Projects\Orca\_e2e_artifacts\{screenshot.png, page.html}`

### 2.4 第三轮：`ORCA_HOME` 隔离跑全量 Playwright

**命令**：
```
wsl.exe -e bash -c "export ORCA_HOME=/tmp/orca-e2e-home && \
  export LD_LIBRARY_PATH=... && \
  .venv/bin/python -m pytest -m integration tests/iface/web/test_playwright_runlist.py -v --tb=short"
```
**结果**：`1 failed, 8 passed in 23.21s` —— 唯一失败：`test_list_view_collapse_persistence_across_reload`（AC-4）。

---

## 3. AC-4 真实 bug —— 诊断 + minimal 修复

### 3.1 复现

3 次重跑同一用例：3/3 FAILED，**确定性**。

诊断探针（`_probe_collapse_diag.py`，**取证后已删除**）的逐拍采样：
```
[probe] PRE-reload localStorage = '["demo"]'      ← 折叠正确持久化
[probe] PRE-reload run-row count = 0              ← 折叠生效
[probe] POST-reload localStorage = '[]'           ← ★ storage 被擦写为空
[probe] t=0.00..4.75s run-row count = 2           ← 永远不折叠
```

### 3.2 根因（`orca/iface/web/frontend/src/hooks/use-collapsed-projects.ts` 旧实现）

```ts
const [collapsed, setCollapsedState] = useState<Set<string>>(() => {
  const raw = readStored();
  for (const n of raw) if (known.has(n)) cleaned.add(n);  // ← 挂载时 known=空集
  return cleaned;                                          //   → cleaned 永远空
});
useEffect(() => { writeStored(collapsed); }, [collapsed]); // ← 反过来把空集覆盖回 storage
```

挂载时 `/api/runs` 未回 → 父级 `knownProjects` 为空集 → `useState` 初值把持久态（`["demo"]`）过滤成 `Set()` → write-back effect 把空集写回 `localStorage` → **持久态被永久清空**。

**为什么 vitest 436 没抓到**：现有 AC-4 vitest 只测「折叠 → 写」单方向（`run-list-page.test.tsx:337`），未测「mount 前预存 → 加载后保持折叠」反方向。

### 3.3 Minimal 修复（`use-collapsed-projects.ts`）

改为 **hydration 模式**：初值取空，等 `known` 首次非空时一次性 hydrate（读 + 惰性清理 + setState），并只在 hydrate 之后允许 write-back。详见 `orca/iface/web/frontend/src/hooks/use-collapsed-projects.ts:52-83`（带 inline 注释说明旧 bug 与新模式契约）。无新 API、无新依赖、不破现有调用方。

### 3.4 回归测试（`run-list-page.test.tsx`）

新增 vitest：`AC-4 持久态加载：mount 前 localStorage 预存 [demo] → 加载后保持折叠（不擦写 storage）`。

### 3.5 修复后复测

| 验证 | 结果 |
|---|---|
| `npx tsc --noEmit` | 0 错 ✓ |
| `npx vitest run` | **437 passed**（+1：新回归用例）✓ |
| `npx vite build` | `built in 5.57s` ✓ |
| `pytest -m integration tests/iface/web/test_playwright_runlist.py -v`（隔离 `ORCA_HOME`） | **9 passed in 21.06s** ✓（AC-4 用例转绿）|

---

## 4. AC 逐条对真机结果

| AC | 真机证据 | 结论 |
|---|---|---|
| AC-1 删除按钮大常显 | DOM 内每个 board-card / run-row 均带 `delete-btn` testid（page.html） | ✓ |
| AC-2 多选+Shift 三态 | Playwright `test_list_view_bulk_select_and_delete` PASSED（勾 checkbox → bulk-bar → 真机 DELETE /api/runs/<id>） | ✓ |
| AC-3 排序 | Playwright `test_list_view_sort_menu` PASSED；DOM 含 `sort-trigger` | ✓ |
| **AC-4 折叠持久** | **修复前 3/3 fail，修复后 PASSED + 新增 vitest 回归用例** | ✓（修后） |
| AC-5 项目头 | Playwright collapse test 用 `group-header`；DOM 含 `group-demo`（项目分组） | ✓ |
| AC-6 主题真切换 | Playwright `test_theme_button_toggles_html_class` PASSED（`<html>` class + localStorage） | ✓ |
| AC-7 搜索穿透 | Playwright `test_list_view_search_force_expands_collapsed_group` PASSED | ✓ |
| AC-8 blocked 穿透 | DOM 含 `status-chip-blocked` + `board-column-blocked`（filter+列同时存在） | ✓ |
| AC-9 三态 | DOM 五个 status-chip-* 齐全 +骨架/空态由 vitest 覆盖 | ✓ |
| AC-13 对话框 a11y | DeleteConfirmDialog focus trap 由 vitest 覆盖（436→437 项） | ✓ |
| AC-14 WS 重连 | use-ws-runlist WS reconnect 由 vitest 覆盖 | ✓ |
| AC-18 后端零改 | 31 passed；commit 不动后端文件 | ✓ |
| AC-19 看板默认 | DOM 默认渲染 `[data-testid=board]`，无 `run-row` | ✓ |
| AC-20 五列 | `[data-testid=board-column-{queued,running,blocked,completed,failed}]` 全在 DOM | ✓ |
| AC-21 BoardCard 进度 | DOM 3 个 `[data-testid=board-card]`，每个含 `run-item`（含 workflow / project / cost / event count） | ✓ |
| AC-22 限长 | DOM visible text 已截断（`wf-2` / `demo` / `$0.00` / `3 事件`） | ✓ |
| AC-23 共享 selection | Playwright `test_run_item_selector_works_in_both_views` PASSED（`run-item` testid 在两视图都能命中） | ✓ |

---

## 5. 测试基础设施发现（非本次 commit 范围，记录给后续）

**`tests/iface/web/conftest.py::live_server` / `make_manager` 不隔离 `ORCA_HOME`。** 当用户真实 `~/.orca/projects.json` 注册了多个项目（本机 50KB / 1060 runs）时，`RunManager.discover_runs()` 全量扫描真实注册表 → `/api/runs?scope=all` 挂死 → Playwright 全套 5s 超时失败。

**影响**：开发机上跑 `pytest -m integration tests/iface/web/` 必然全挂。**这是测试 fixture 的隔离缺陷，不是被测代码 bug。** 建议在 conftest 加 `monkeypatch.setenv("ORCA_HOME", tmp_path)` 或 fixture 级 env 隔离。本验证用 `export ORCA_HOME=/tmp/orca-e2e-home` 临时绕过。

---

## 6. 给用户的精确复现命令（任意装好 Python+uv+playwright 的环境）

```bash
cd D:/Projects/Orca
cd orca/iface/web/frontend && npx vite build && cd ../../..   # 已 build，无须重做
.venv/bin/python -m pytest tests/iface/web/test_routes.py \
  tests/iface/web/test_multi_run_phase_c.py tests/iface/web/test_ws.py -q
ORCA_HOME=/tmp/orca-e2e-home .venv/bin/python -m pytest \
  -m integration tests/iface/web/test_playwright_runlist.py -v
```

---

## 7. 工作树新增 / 修改（未提交）

- `M orca/iface/web/frontend/src/hooks/use-collapsed-projects.ts` —— AC-4 bug 修复（hydration 模式）
- `M orca/iface/web/frontend/test/run-list-page.test.tsx` —— 新增 AC-4 反向回归用例
- `M orca/iface/web/static/index.html` + `?? static/assets/*` —— `npx vite build` 产物（含修复）
- `D:\Projects\Orca\_e2e_artifacts\{screenshot.png, page.html}` —— 取证存档（非项目文件，可删）

未 commit，等用户决定（CLAUDE.md：commit only when user asks）。
