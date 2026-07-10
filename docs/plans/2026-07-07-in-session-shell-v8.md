# Plan: in-session shell v7 → v8 增量（plugin 模板重写 + doctor + transform 入口）

**SPEC**: `docs/specs/in-session-shell-design-draft.md` v8（§2.6 / §2.6.1 / §2.6.2 / §2.7 / §13 / §9.2）

## 范围（增量）
v7 CLI 大脑（bootstrap/next/status/stop/start + marker + Tape.append_batch + 合规/taxonomy）零改；只动：
1. 重写 opencode plugin 模板（`orca/iface/in_session/templates/opencode/orca.ts`）
2. 改 slash 命令模板：3 个 .md → 1 个 `orca.md`（body = `<!--orca:cmd $ARGUMENTS-->`）
3. 加 `orca in-session doctor` CLI 子命令
4. 更新 `start`：同时写 opencode 模板文件 + CC 片段 + 打印 `/orca doctor` 提示
5. 测试（regex/extract/一次性消费/doctor/grep 守门 + v7 baseline 0 回归）

## 实现要点
- **Plugin 结构**：`export const OrcaPlugin = async (ctx) => ({ ...flat hooks })`；client = `ctx.client`；`Bun.spawnSync({stdout:"pipe",stderr:"pipe"})`
- **入口钩子**：`experimental.chat.messages.transform`：扫最后一条 user msg 最后一个 text part；regex 匹配 marker；按 sub 派发 spawn CLI；按 §2.6.2 改写语义替换该 text part；无 marker 透传；替换文本不含 `<!--orca:cmd` 字面
- **驱动钩子**：`event`（session.idle）：子 session 过滤（marker.session_id）+ in-flight mutex + task ToolPart.output 提取 + spawn next + promptAsync
- **零业务逻辑守门**：无 advance/router/replay/tape/`<task_result>` 解析/task_id 剥离（注：task output 提取在 plugin 侧允许，按 SPEC §2.5 D-v7-4）。**但 SPEC §13 v8 描述 plugin 仍需 "从 ToolPart.state.output 提取（解 <task_result>）"** — 此非 Orca 业务逻辑而是宿主 payload 扁平化，允许。**grep 守门测试需保留 `<task_result>` 检查，但 v7 已把它列入禁止词**。Re-check: 现状测试 `forbidden` 含 `advance_step/router.resolve/replay_state/tape.append/EventBus/Tape(/drive_loop/advance(` — 不含 `<task_result>`。task_result 解析允许。
- **doctor JSON**：`{ok, report, checks:[{name, pass, detail}]}`，3 项（plugin 加载/transform 触发 = doctor 在跑；marker 派发 = 同；CLI 可达 = import + version）。report 描述 marker 用反引号，不写完整 marker
- **start 校验**：`.md` 的 `$ARGUMENTS` 不含 `>` / 换行（提示文本）

## 测试矩阵
| 测试 | 内容 |
|---|---|
| `test_marker_regex` | regex 命中 run/status/stop/doctor/无 args/含空格 wf 路径/含 `>` 拒绝/无 marker 透传 |
| `test_plugin_rewrite_*` | run→.prompt / doctor→.report / status→友好 / stop→ok+run_id；mock CLI stdout JSON |
| `test_plugin_one_shot_consume` | 替换文本无 `<!--orca:cmd` 字面 |
| `test_plugin_session_id_argv` | sessionID 从 info.sessionID 取作 argv |
| `test_doctor_*` | 3 checks、JSON 结构、report 无完整 marker、ok=and(pass) |
| `test_plugin_greps` | 无 `@opencode/core/client` / `command.execute.before` / `stdout:"string"` / `advance/router/replay` |
| baseline | v7 31 测全绿（CLI 未动） |

## 边界（不动）
- `cli.py` 现有命令（bootstrap/next/status/stop/start/serve）主体不动，仅加 `doctor`
- `marker.py`、`step.py`、`daemon.py`、`Tape.append_batch` 不动
