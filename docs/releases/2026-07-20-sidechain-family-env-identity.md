# sidechain family 由 env 身份决定（修 dotdir 存在性误判）

> 日期：2026-07-20 ｜ 关联：`129fff8`（cac 优先）的回归修复 ｜ 计划：`docs/plans/vast-weaving-willow.md`（session 内）

## 问题

用真 Claude Code 跑 workflow，**web 只看到主 session 最终输出，看不到子 agent 的 message/tool/thinking**。

## 根因（活样本 `quant-sensitivity-20260720-182623-a53445` 坐实）

`resolve_cc_sidechain_root`（`orca/events/adapters/_family.py`）的 probe 分支用 **dotdir 存在性**判 family——`~/.cac` 存在即走 cac。但 `~/.cac` 是 **`orca install` 主动建的**（`skill_cmds.py:58-91` `HOST_DOTDIR`，装 hook+skill 给 cac 前端，**不是 cac 工具建的，用户也从未真跑过 cac**——无 `sessions/`、无 `codeagentcli`）。于是真 CC（`CLAUDE_CODE_SESSION_ID` 在、数据在 `~/.claude`）的 run 被 daemon 误导向 tail 空的 `~/.cac/.../subagents/` → ingest 0 条 → tape 无 `agent_` 事件 → web 空。

**核心设计错误**：当前后端是 cc 还是 cac，由 env/进程身份决定，**不是 dotdir 存在性**。dotdir 存在只代表"装过该前端"。`129fff8` 加的 cac 优先 + 未提交的机器级 `detect_cc_existing_roots()` 放大了触发面（从"跑过 cac"降到"装过 cac"）。

## 修复

family 决策收敛到 **env/进程身份**，优先级 **env 身份 > config（`sidechain.family`）> dotdir 探测（仅兜底）**。

### 新增 `orca/iface/in_session/_hostenv.py`（宿主身份探测单一来源，stdlib-only）

提取 cli.py / sidechain_cmds.py 既有的 `_cac_session_id_from_pid` / `_host_session_from_env` / `_detect_backend_from_env` **字节级副本**（消除 DRY 违规）+ 新增 `detect_family_from_env`：

- `CLAUDE_CODE_SESSION_ID` 在 → `"cc"`（真 CC）
- `CODEAGENT=1` + PID 回溯命中 `codeagentcli` → `"cac"`（CAC 换皮）
- 其余 → `None`（caller 回退 config/probe）

**为什么独立模块**：env/进程探测（读 `/proc`）独立于 config I/O（`config.py`）；`sidechain_cmds.py` 严禁 import `cli`（add_typer 循环），放 in_session 同层两方共享无环。

### 三个 caller 统一 `detect_family_from_env() or <config>`

- `cli._spawn_sidechain_daemon`（主修复——实际 ingest 路径）：daemon argv `--family` 从 `None`（走 probe）变为 `cc`/`cac`（走 resolver source=config，跳过 probe）。链路已验证全程透明透传（cli → daemon → adapter → resolver）。
- `cli._check_sidechain_backend`（doctor）：`has_cc_env` 改 `detect_backend_from_env()=="cc"`（**认 cac**——修前 cac 下直接返 unknown）；family/fam_eff 走 env 优先。
- `sidechain_cmds._print_effective`（`orca sidechain family` 查看）：同上 + 删本地副本。

### events 层 `_family.py` 不改主逻辑

probe + cac 优先保留作兜底（仅 daemon 未传 `--family` 时触达：手动起 daemon / 测试直调 / 非 in-session）。生产路径（daemon）现在必传 family，走不到 probe。

### `builtins.next` 处理

原 cli.py 的 `_cac_session_id_from_pid` 用 `builtins.next`（cli.py 顶层 `def next(...)` typer 命令遮蔽 builtin）。提取到 `_hostenv.py` 后，`_hostenv` 无遮蔽，普通 `next` 解析到 builtin——**Python 函数 `__globals__` 绑定定义模块，不随 import 位置变**（实测：`_hostenv` 命名空间无 `next` 定义 + 调用不报错），CC/CAC 环境均安全。cli.py 删 `import builtins`（已无使用）。

## 排除

- **opencode 家族**（`resolve_opencode_db`）：无对称 bug——探测 DB 文件 `is_file()`，install 不建 DB；env 无法区分 opencode/nga（共用 plugin），YAGNI。
- **`cc_nudge.sh`**（bash）：独立实现，走 host_session 不走 family。
- **删 `~/.cac`**：install 正常产物，删不得且会重建。

## 验证

- **单测**：`test_hostenv.py`（11，`detect_family_from_env` 各分支）+ `test_in_session_v8.py`（4 个 doctor 测试改写：bug 回归锚 `env_family_cc_when_cac_dotdir_installed` / cac env / env 胜 config / env family no root）+ `test_adapters_family.py` 全过 = **89 passed**。
- **doctor**（真 CC + `~/.cac` 存在）：`family=cc（source=env）`、`resolved_root=.../.claude/...`、`root_source=config`、`available=True`、status=pass（修前 `family=cac`/`available=False`）。
- **daemon spawn**：`family=cc`（修前 `family=None`）。
- **tape**：**34 个 `agent_` 事件**（修前 0）→ web 子 agent 区可见。

## 文件

- 新增 `orca/iface/in_session/_hostenv.py`
- `orca/iface/in_session/cli.py`（删 3 副本 + import as 别名 + daemon spawn/doctor env family）
- `orca/iface/in_session/sidechain_cmds.py`（删 2 副本 + `_print_effective` env family + docstring 同步）
- 新增 `tests/iface/in_session/test_hostenv.py`
- `tests/iface/in_session/test_in_session_v8.py`（4 doctor 测试改写）
- `tests/iface/in_session/test_sidechain_cmds.py`（`test_family_show_with_env_resolves_root` 改 env 胜 config）
- `tests/iface/in_session/test_host_session_binding.py` + `test_sidechain_daemon.py`（monkeypatch 路径 `cli._cac_session_id_from_pid` → `_hostenv.cac_session_id_from_pid`）
