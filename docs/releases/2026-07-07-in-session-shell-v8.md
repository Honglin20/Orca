# Release Note: in-session shell v7 → v8 增量

**日期**: 2026-07-07
**Commit**: `56083c1`（branch `phase13-in-session-v8`）
**SPEC**: [`docs/specs/in-session-shell-design-draft.md`](../specs/in-session-shell-design-draft.md) v8（§2.6 / §2.6.1 / §2.6.2 / §2.7 / §13 / §9.2）
**计划**: [`docs/plans/2026-07-07-in-session-shell-v8.md`](../plans/2026-07-07-in-session-shell-v8.md)

## 一句话定位

v7 经 e2e 发现 `command.execute.before` 在 opencode 1.14.22 runtime 不触发，v8 换入
`experimental.chat.messages.transform` + 加 `/orca doctor` 自检 + start 落 opencode 模板，
**v7 的 CLI 大脑与所有铁律不变**。

## 改动清单

| 文件 | 变更 |
|---|---|
| `orca/iface/in_session/cli.py` | 加 `doctor` 子命令；`status` 加 `--json` flag（MAJOR-1 闭环）；`stop` 加 `--owner <sid>` 入参（MAJOR-2 闭环）；`start` 把 orca.ts + orca.md 写入项目 `.opencode/`；`_install_opencode_templates` + `_atomic_write_with_backup` + `_default_rundir` helpers |
| `orca/iface/in_session/templates/_constants.py`（新） | `MARKER_REGEX` + `MARKER_LITERAL` 单一真相源（Python ↔ TS 同步） |
| `orca/iface/in_session/templates/__init__.py` | re-export `MARKER_REGEX` / `MARKER_LITERAL` |
| `orca/iface/in_session/templates/opencode/orca.ts` | **重写**：flat hooks 结构 + ctx.client + Bun.spawnSync + messages.transform 入口 + event 驱动 + spawnCli fail loud + 一次性消费 split-join 兜底 |
| `orca/iface/in_session/templates/opencode/command/orca.md`（新） | 唯一 slash 命令，body = `<!--orca:cmd $ARGUMENTS-->` |
| `orca/iface/in_session/templates/opencode/command/orca-{run,status,stop}.md`（删） | 由统一 `orca.md` 取代 |
| `tests/iface/in_session/test_in_session_v8.py`（新） | 52 测覆盖 regex / 改写 / 一次性消费 / doctor / 6 项架构 grep 守门 / start 模板写入 / v7 baseline 回归 |

## 关键设计决策（v8 在 v7 之上）

- **D-v8-1（入口机制换）**：opencode 入口从 `command.execute.before`（spike 实证 1.14.22 runtime 不触发）改为 `experimental.chat.messages.transform`（spike `/tmp/orca-xform` 实证可改写、模型未见原文）。单 `/orca` 命令 + marker 派发，加 command = CLI 子命令 + plugin 派发分支两处。
- **D-v8-2（doctor 自检）**：新增 `/orca doctor`（marker→transform→`orca in-session doctor` CLI→报告），迅速验 plugin 加载 / marker 派发 / CLI 通。自证：能回报告即入口链路活。
- **B1/B2/B3/B4 闭环**（第三轮 review 4 blocker）：
  - B1 改写语义：plugin 按 §2.6.2 提取 stdout JSON 顶层字段（非整 JSON）
  - B2 marker 规范：regex 行首/行尾锚定，args 禁 `>` 与换行
  - B3 doctor 盲区：3 项自检 + 对 idle 标注「需跑 /orca run 验证」，plugin 侧无 count++
  - B4 sessionID 传递：从 `out.messages[i].info.sessionID` 取作 `--owner` + `--session-id` argv

## 端到端契约（§2.6.2）

| 子命令 | CLI 子命令 | argv 形态 | stdout JSON | plugin 提取 |
|---|---|---|---|---|
| `/orca run <wf>` | `bootstrap <wf> --owner <sid> --session-id <sid> [--model <m>]` | 由 plugin 从消息 sessionID 注入 | `{run_id, tape, done, node, prompt}` | `.prompt` |
| `/orca status` | `status --json [<run_id>]`（plugin 查 marker 拿 run_id） | — | `{run_id, status, current_node, node_status, progress}` | `.status` + `.progress` + `.node_status`（友好串） |
| `/orca stop` | `stop --owner <sid>`（plugin 查 marker）或 `stop <run_id>` | — | `{run_id, ok, done}` | `.ok` + `.run_id`（友好串） |
| `/orca doctor` | `doctor` | 无 argv | `{ok, report, checks:[...]}` | `.report` |

## 守门测试（6 项 grep + 1 项 regex 同步）

- `test_plugin_uses_ctx_client_not_npm_import` — 不 import `@opencode/core/client`，用 `ctx.client`
- `test_plugin_uses_spawn_sync_pipe_not_string` — `Bun.spawnSync` + `stdout:"pipe"`，非 `spawn`+`stdout:"string"`
- `test_plugin_exports_flat_hooks_not_nested` — flat hooks 结构（非 nested）
- `test_plugin_uses_messages_transform_entry` — 入口钩子是 `experimental.chat.messages.transform`
- `test_plugin_does_not_use_command_execute_before` — 不依赖 `command.execute.before`
- `test_plugin_has_no_orca_business_logic` — 无 `advance_step/router.resolve/replay_state/tape.append/EventBus/Tape(/drive_loop/advance(`
- `test_plugin_embeds_canonical_marker_regex` — Python ↔ TS regex 字面同步
- `test_plugin_embeds_canonical_marker_literal` — Python ↔ TS MARKER_LITERAL 同步（NIT-2 闭环）

## 偏离 SPEC 的地方

无。逐字按 §2.6/§2.6.1/§2.6.2/§2.7 实现。

## 测试结果

| 套件 | 通过 | 失败 | 备注 |
|---|---|---|---|
| in_session 全部（v7 baseline + v8 新增） | 83/83 | 0 | 31 v7 baseline 0 回归 + 52 v8 新增 |
| 全 unit 套件（不含 e2e） | 1775/1776 | 1 | 唯一 fail = `daemon.py:105:sys.exit` 预存 B-8 follow-up，与本次无关 |

## 未实证项（留 `test-coverage-e2e` 真链路验）

SPEC §9.2 明示，本增量无可执行 e2e，需真 opencode runtime 验证：
- transform await 外部进程时序（M3）：opencode transform async 超时阈值未知，长 wf 加载待实证
- sessionID 路径（M3）：从 `out.messages[i].info.sessionID` 取（spike 未打印，先按推测结构 + 多路径兜底）
- multi-session 绑定（M3）：用户已开 ≥2 session 后在某 session 触发 `/orca run`
- bootstrap 端到端：`/orca run <wf>` → marker → transform → bootstrap → entry prompt → idle 驱动多节点
- 子 session 过滤 e2e（D-v7-5）：跑 task-subagent workflow，plugin 日志见子 session idle 全 skip
- `/orca doctor` 真链路：在真 opencode 里敲 → 回报告（自证 transform 链路活）

## 守门 grep 实证

```
forbidden grep (plugin TS code-only):
  advance_step: absent
  router.resolve: absent
  replay_state: absent
  tape.append: absent
  EventBus: absent
  Tape(: absent
  drive_loop: absent
  advance(: absent

required (plugin TS code-only):
  ctx.client: present
  Bun.spawnSync: present
  "experimental.chat.messages.transform": present
  MARKER_REGEX: present
  __orca_error: present
  exitCode: present

regex sync: True (Python MARKER_REGEX == TS regex literal)
```

## Code Review 闭环

dispatched `code-reviewer` 审 v8 全量代码 + 测试。3 项 MAJOR + 1 MINOR + 4 NIT，**全部已闭环**：

- 🔴 MAJOR-1（CLI status 非 JSON 输出）→ 闭环：`status --json` flag（默认人类可读保留 v7）
- 🔴 MAJOR-2（`/orca stop` 缺 run_id argv）→ 闭环：`stop --owner <sid>` + plugin 查 marker 派发
- 🔴 MAJOR-3（plugin spawnCli 吞 stderr）→ 闭环：检查 exitCode + `__orca_error` 信封 + stderr 首 400 字符回显
- 🟡 MINOR-1（`_atomic_write_with_backup` 静默吞 OSError）→ 闭环：加 `logger.warning`
- 🟢 NIT-1（doctor `cli_reachable` 改名 `cli_imports_ok`）→ 闭环
- 🟢 NIT-2（MARKER_LITERAL Python↔TS 同步测试）→ 闭环
- 🟢 NIT-3（`test_marker_regex_rejects` special-case 拆分）→ 闭环：分 rejects / tolerates 两个 parametrize
- 🟢 NIT-4（`_extract_ts_function_body` 用 brace counting）→ 闭环：替换脆弱 regex
