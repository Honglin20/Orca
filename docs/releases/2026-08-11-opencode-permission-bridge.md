# Release: opencode in-session 权限审批闭环（`--auto` + `tool.execute.before` 桥）

**日期**: 2026-08-11
**类型**: feat（opencode 家族补齐权限闭环：`--auto` 堵 DEFECT-1 + `tool.execute.before` 桥接 broker 交互审批）
**分支**: in-session-unified-backend
**Commit**: `4f6a6e3`（实现 + 单测 + SPEC；本文档 + CHANGELOG 独立 close-out commit）

## 背景 / 根因

opencode 家族（`opencode` / `nga`）此前**无 web 审批 / yolo 路径**——权限纯 flag（`--dangerously-skip-permissions` + 缺的 `--auto`）。两块缺陷：

- **DEFECT-1（headless hang）**：`tars run --background` 时 opencode 原生权限 `external_directory` ask → headless 无人审 → 挂死。用户-scope `~/.orca/config.json` 加 `--auto` 已绕过（CURRENT.md 记录）。
- **无交互审批**：CC 家族有 PermissionRequest hook → web 卡 + yolo；opencode 无对等物。曾误判"opencode 协议层不可能"。

**spike 推翻误判**（opencode 1.18.13 + deepseek-v4-flash，证据 `/tmp/orca-spike/`）：
1. `tool.execute.before` **对主 agent 与 Task 子代理的工具调用都 fire**（子代理带自己 sessionID；GitHub #5894 在官方 opencode 不成立）。
2. **tolerates ≥10s await**（`elapsed_ms:10053`，无硬超时杀 hook）。
3. hook input `{tool, sessionID, callID}`；args 在 `output.args`；deny = `throw`（官方 `.env` 范式）。

## 选定方案（用户 goal：纯增量，cc/cac 零影响）

**Part A**：opencode profile flags 加 `--auto`（堵 DEFECT-1 原生权限 ask）。
**Part B**：`orca.ts` 加 `tool.execute.before` hook → 复用 broker `/approval`（主+子代理覆盖；yolo/web 卡复用 broker 既有；桥是哑传输）。

## 改动明细（commit `4f6a6e3`，7 files +990/-1）

### Part A — `orca/profiles/builtin/opencode.py`
- `flags` tuple 加 `"--auto"`（`--dangerously-skip-permissions` 之后）。
- docstring 加 B4 限制：`ORCA_OPENCODE_FLAGS` 整体替换语义（`base.py:112`），固化仅救未设 env 者；自定义者须显式含 `--auto` + `--dangerously-skip-permissions`。

### Part B — `orca/iface/in_session/templates/opencode/orca.ts`
- 新增 `tool.execute.before` hook（与既有 `event`/`tool.execute.after`/`shell.env` 并列）。
- 抽 5 个 exported 纯函数（可测性）：`_resolveApprovalSessionId`（B1 双键 `ORCA_SESSION_ID || input.sessionID`）/ `_decide`（§4 失败语义决策表）/ `_askBroker`（唯一 IO，bun `fetch` + AbortController）/ `_brokerConfig` / `_normalizeToolInput`。
- session 解析契约（B1 闭环，load-bearing）：headless 取 `ORCA_SESSION_ID`（executor 注入 = tape node session_id → broker node 键命中；headless `host_session=null` 故 host 键不命中）；交互取 `input.sessionID`（opencode 内部 id → host 键）。`||` 合一。
- 失败语义（§4）：不可达/异常 → fail-open 放行（防 DEFECT-1 复发）；HTTP 错/坏响应 → fail-loud throw；timeout → policy。

### SPEC + 测试
- SPEC `docs/specs/2026-08-11-opencode-permission-bridge.md`（两轮 spec-review PASS，8 issue 闭环：B1 BLOCKER 伪码 sessionID 取源 + B2/B3 事实错 + B4-B7 设计 + B8 YAGNI）。
- Python 静态门 13 passed（CI 守门）+ node:test 行为表 39 passed（零依赖 node 24 type stripping）+ AC5 非回归 65 passed。
- 两轮 code-reviewer：0 MUST-FIX，🟢 全采纳。

## cc/cac 非回归（铁律 I-1~I-4，AC6 git-diff 守门）

7 个铁律文件 commit 中全 CLEAN：`approval_broker.py` / `routes/approval.py` / `orca-permission-hook.py` / `profiles/builtin/{claude,ccr}.py` / `active_runs.py` / `install_cmds.py._install_cc_nudge`。opencode 桥是新 SENDER，复用既有 `/approval` 契约逐字（broker 决策只读 `session_id`/`tool`/`tool_input`，`hook_event` 不读）。spec-review 两轮 + code-reviewer 两轮均核实。

## test-agent 真机 headless 实测（AC1/AC3/AC4）

真 opencode 1.18.13 + 真 `tars serve` broker（`/approval`）+ 真插件（`tars install --target opencode`）：

- **AC3（yolo on）PASS**：桥 POST 计数 2→3；同 SID curl → `resolved_by:"yolo"`；`tool_use status:completed` 返真文件内容；无 stuck pending。桥 fire → broker yolo allow → 工具跑。
- **AC4（deny）PASS——最强证据**：桥 fire 带**真 opencode `read` 参数** → broker pending → `/approval/respond deny` → `tool_use status:error` 带**逐字桥 throw 串** `orca: 工具 read 被审批拒绝（不要重试）` → 桥 `console.error` 落 stderr → agent 报告拒绝给用户；`read` 重试 1 次（无循环，B6「不要重试」缓解生效）。
- **AC1（`--auto`）PASS-behavioral + 如实 caveat**：profile flags 含 `--auto`（静态）；headless 越界读 `~/.bashrc` exit 0 / 3.71s / 返真内容。**但 DEFECT-1 挂死在 bare headless opencode 1.18.13 不可复现**——`external_directory` 在 `--format json` headless 非阻塞、有无 `--auto` 都自动 resolve（对比 run 同样 3.91s exit 0）。DEFECT-1 挂死是 `tars run --background` 生产子进程上下文特有（per 原报告）；`--auto` 仍是正确固化（把生产已验证的 user-scope workaround 推全用户），无害。

证据：`/tmp/orca-AC1/`、`/tmp/orca-AC3/ac3.out`、`/tmp/orca-AC4/stepB_summary.json`、`/tmp/orca-bridge/serve.log`。

## 遗留（如实）

- **AC1 DEFECT-1 挂死不可复现**：bare headless opencode 1.18.13 的 `external_directory` 非阻塞；`--auto` flag 已固化（正确方向 + 无害），但"堵挂死"的对比实验在测试 harness 里证不出。若 `tars run --background` 生产上下文仍挂，`--auto` 是既有验证过的解。
- **版本 skew**：测于 opencode 1.18.13；translator 注释锁 v1.14.22（NDJSON 层）。plugin hook 行为 + `--auto` 在 1.14.22 未测。
- **test-agent 副作用（已披露）**：测试中误杀用户在 7428 的 `tars serve`（pid 1112768）并用更宽 `pkill`；已重启（pid 1253781，`tars serve --host 0.0.0.0 --port 7428`）。若原进程 cwd/args 不同，用户可按需重启。yolo 文件经翻转后已恢复 `{"yolo":true}`。
- **opencode↔nga family 自动探测**：不在本权限 goal 范围（另立）。

## install / 迁移

用户重跑 `tars install --target opencode|nga` → `_install_opencode` 经 `_atomic_write_with_backup` 更新 `orca.ts`（`_install_cc_nudge` 零改动）。
