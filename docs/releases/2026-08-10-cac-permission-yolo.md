# Release: CAC 权限审批 / yolo 生效（permission hook 补 CAC PID 回溯）

**日期**: 2026-08-10
**类型**: fix（in-session 权限审批：CAC 后端 PermissionRequest hook 拿不到 session 身份 → yolo/审批卡永不触发）
**分支**: in-session-unified-backend
**Commit**: `6128777`

## 背景 / 根因

CAC 是 Claude Code 的换皮后端（进程 `codeagentcli`、dotdir `.cac`），PermissionRequest hook 本已
随 cc+cac 家族一并安装（`install_cmds._install_cc_nudge`，cc / cac 对称落 `.claude`/`.cac/hooks/`）。
但 yolo / web 审批卡对 CAC 从不生效。

根因（设计讨论定位，无 CAC 真机，靠代码链路核实）：真 CC 给所有 bash 子进程注入
`CLAUDE_CODE_SESSION_ID`，permission hook 直接从 env 取 session_id；**CAC 不注入任何 session env**
（sessionId 存内存变量 `eZ.sessionId`，不写 `process.env`）。而 hook 的 `_resolve_session_id` 只查
`ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > stdin`——三路皆空 → 返 None → broker
`_resolve_active_run` early-return（`approval_broker.py:364` 空串守卫）→ `ask` → **`if self._yolo` 分支
在 resolver-hit 门之后（`approval_broker.py:316`），永不执行**。CAC 的 hook 装了，唯独缺 session 身份。

## 选定方案（用户拍板 C：先修 CAC yolo）

surgical 改 hook 一个文件：内嵌 CAC PID 回溯，让 hook 在 env 两键皆空时沿 PID 链回溯 `codeagentcli`
父进程、读 `~/.cac/sessions/<pid>.json` 拿 sessionId。不动 broker / `active_runs` / 前端 / 安装流程
（CAC 的 PermissionRequest 已装）。opencode 家族无 `PermissionRequest` 事件，本期不含（协议层不支持
交互审批，另行 `--auto` flag 固化）。

## 改动明细

### `orca/iface/in_session/templates/orca-permission-hook.py`

- 新增 `_cac_session_id_from_pid()`：**行为等价**（同路径 `~/.cac/sessions` / 同 exe 精确匹配
  `codeagentcli` / 同异常元组 / 同 `range(20)` 边界）于 `_hostenv.cac_session_id_from_pid` 与
  `cc_nudge.sh` 内嵌同款函数。这是该逻辑的**第三份副本**——结构性强制：三者皆 stdlib-only / 跑在无
  Orca venv 的 CC/CAC 子进程，不能 import `_hostenv`（SPEC §3.1 铁律）。纯 stdlib（`os`/`json`），无新
  import（刻意用 `os.path`/`open` 而非 `pathlib`，守 SPEC N11 stdlib 枚举与 `test_install_*_script_matches_bundle`
  逐字拷贝不变）。
- `_resolve_session_id` 顺序改为 `ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > CAC PID 回溯 > stdin`。
  PID 回溯优先于 stdin，对齐 `host_session_from_env`（进程身份 > payload）——保证 hook 取值与 tape
  `data.host_session`（bootstrap 同款写出，`cli.py` 经 `host_session_from_env`）一致 → broker 双键命中。
- **fail-safety（review SHOULD-FIX #1）**：PID 回溯 best-effort `try/except Exception → None` 包裹。该
  调用点位于 `main()` 无保护区（stdin-try 与 urlopen-try 之间），session 文件读取只接
  `(JSONDecodeError, KeyError)`，非 UTF-8 字节会抛 `UnicodeDecodeError` → 不包则穿出 main → exit 1 不
  emit 任何 decision（比 bug 修前的干净 `ask` 更差）。

### SPEC 契约同步

- `docs/specs/in-session-permission-hook.md` §3.1 step 2：session_id 解析加 CAC PID 回溯一级 + CC 短路说明。
- `docs/specs/2026-08-07-in-session-yolo-active-run-fallback.md` R2：关闭"CAC 待真机实证"缺口，记新优先级
  + 行为等价（非字节镜像，诚实措辞）+ inspection-verified 局限。

### 测试（`tests/iface/in_session/test_orca_permission_hook.py`）

新增 6 测 + 改 2 测：
- `_resolve_session_id` 优先级三路（env > CAC PID > stdin）+ env 短路不触达 PID 回溯（`assert_not_called`）。
- PID 回溯抛异常 → 回退 stdin / None（fail-safety，review #1 验证）。
- `~/.cac/sessions` 不存在 → None（跨平台确定性守卫，不触 `/proc`，HOME+USERPROFILE 隔离宿主状态）。
- **DRY 漂移闸门**（review #2）：读 hook + `_hostenv` 源文本，断言守恒常量（`codeagentcli` / `range(20)` /
  status 异常元组 / session 异常元组）同步——改基准忘改 hook → fail，防本 bug 原样复发（范式仿
  `test_host_session_binding.py` 的 cc_nudge 漂移闸门）。
- 既有 `test_resolve_session_id_prefers_env` / `test_main_session_id_taken_from_stdin` 改 mock PID 回溯
  为 None（确定性，不依赖宿主 `~/.cac` 状态）。

## 验证

- hook + install **30 passed**（含 `test_install_cc_permission_hook_script_matches_bundle` 证 install 拷贝
  与模板逐字一致）。
- 更广 in_session + approval_broker：**621 passed / 8 failed**；8 失败全在 doctor / push_probe / skill_md，
  grep 证零命中本改动符号（`_cac_session_id_from_pid` / `_resolve_session_id` / `orca-permission-hook`），
  属 pre-existing WIP（`chart_daemon.py` / `create-workflow SKILL.md` 等未提交改动）。
- code-reviewer 一轮闭环：**0 MUST-FIX / 3 SHOULD-FIX 全采纳**（fail-soft 包裹 / DRY 漂移闸门 /
  inspection-verified 措辞诚实化）。核心不变量（hook sessionId == tape `data.host_session` → broker 双键
  命中）经逐字段比对 + 进程树推理确认。

## 遗留（环境限制，如实）

- **无 CAC 真机**：`_cac_session_id_from_pid` 的 `/proc` 回溯体（~30 行）执行覆盖率为零——取值与 tape
  一致靠 inspection 逐字段比对 + 漂移闸门守恒常量，非真机 e2e。`sessionId` 为 ASCII，故基准 `read_text()`
  与 hook `encoding="utf-8"` 的编码差不产生取值分歧。
- **§9 #2 spike 仍开**（SPEC `in-session-permission-hook.md`）：交互式 CAC + Task 子代理下
  PermissionRequest 是否自然触发 + stdin 字段名实证。本修复把它的性质从"yolo 不生效根因排查"降级为
  "真机确认"——根因（hook 拿不到 session_id）已在代码层堵死。
- 另两块未做（用户选项）：opencode `--auto` flag 固化（堵 DEFECT-1 headless hang）/ opencode↔nga family
  自动探测。
