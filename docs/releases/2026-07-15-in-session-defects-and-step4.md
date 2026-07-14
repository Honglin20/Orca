# Release: in-session v5 —— E2E 缺陷修复（DEFECT-1/2）+ §8 step 4（orca.ts transform 整删）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §8 step 4
**Branch**: `in-session-unified-backend`
**Commits**: `2de50e3`（DEFECT-1）+ `e763e9e`（DEFECT-2）+ `52cc9f3`（step 4）

## 做了什么

E2E（test-agent）跑发现的 2 个 defect 各独立 commit 修复，随后继续 spec v5 §8 step 4 opencode
收尾（transform marker 派发入口 + 死代码整删，保留 idle nudge hook）。

### DEFECT-1（commit `2de50e3`）：cc_nudge.sh fail loud 改用 python3

E2E 发现的 fail-loud 违规：旧 `cc_nudge.sh` 用 `jq ... 2>/dev/null || true` 读 marker——
WSL conda orca 等环境缺 jq 时**静默失败**，nudge 永不触发且无报错，用户看不到任何信号。

修复：
- 脚本改用 python3（orca 本就依赖 python，跨环境可靠；与 orca 一致无新依赖）。
- marker 文件不可读 / 非合法 JSON → 写 stderr + sys.exit(2)，**fail loud**。
- nudge 语义不变：60s 节流 / 扫 `runs/orca-*.json` / emit `decision:block` JSON /
  零 `orca next` 调用 / 零反引号。
- throttle 读异常覆盖对称：`except (FileNotFoundError, ValueError)` 扩为
  `(OSError, ValueError)`——throttle 是 hook 本地 best-effort 态，损坏不该阻断主流程。

测试：5 个真子进程行为测试（block / pass / fail-loud 回归 / 60s 节流 / throttle 损坏容错），
缺 bash/python3 时 skip。静态守门加 `python3` 必备 + 执行体禁 jq。

附带（review MINOR）：新增 `.gitattributes` 锁 `*.sh`/`*.ts`/`*.py` 等 LF，消除 CRLF 跨平台隐患。

### DEFECT-2（commit `e763e9e`）：orca status 加 `--run-id` option

E2E 发现的 doc/code 不一致：SKILL.md + spec §2.1 都写 `orca status [--run-id <id>]`，
但 CLI 实现是位置参数 → 主 session 照文档跑 `orca status --run-id X` 报错。

修复：
- `status` 加 `--run-id` option（与 `next --run-id` 统一），保留位置参数兼容旧调用。
- 位置参数与 `--run-id` 同传且不同值 → `typer.BadParameter`（fail loud，铁律 12）；
  同值 → 容错（用户复制粘贴友好）。
- 模块 docstring + hint 文案统一 `--run-id` 形态，三处（spec/SKILL.md/CLI）一致。

测试：5 个新测试（mirror / json flag / 同值容错 / 异值 fail loud / 不存在 run 错误路径等价）。

**已知 follow-up（review MINOR#1）**：`stop` / `open` 存在同型 docs/CLI 错配（spec/SKILL.md
写 `--run-id`，CLI 用位置参数）。本次仅修 `status`（用户明确范围），stop/open follow-up
记 CURRENT.md。

### step 4（commit `52cc9f3`）：orca.ts transform 整删 + 死代码清零

opencode 收尾：transform marker 派发入口段是旧 A 路径第二入口，v5 入口统一切到 orca skill
后保留 transform = 让 marker 绕过 skill 起第二入口，违反单一接口。本步整删 transform + 全部
死代码 + `_constants.py`，仅保留 idle nudge hook（§4.4，opencode nudge 载体，绝不自动推进）。

改动：
- **orca.ts 整删**：`MARKER_REGEX`/`MARKER_LITERAL` 常量 + `experimental.chat.messages.transform`
  hook + 9 个 helper（`extractTaskOutput` / `spawnCli` / `spawnTopLevelCli` / `rewriteText` /
  `findLastUserTextPart` / `extractModel` / `buildCliArgs` / `CliReply` + entry 诊断计数器）。
  **保留**：`event (session.idle)` nudge hook + `Marker` interface + `listActiveRuns` /
  `nudgeAllowed` / `markNudged` / `writeHeartbeat` / `writeIdleHeartbeat`（rename from
  `writeAdvanceHeartbeat`）+ 诊断基础设施。
- **`_constants.py` 整删**：MARKER_REGEX/LITERAL 仅被已退场的 transform 段引用，无消费者。
- **死代码清零（review MAJOR M1）**：`advanceCount` / `lastAdvanceRunId` 是 step 2b 改 nudge
  后旧 A 路径自动推进的遗留——永不赋值、doctor 不读，连同 heartbeat payload 字段 + test
  fixture 一并清；`writeAdvanceHeartbeat` → `writeIdleHeartbeat` rename（达意）。
- **spec 决策 #12 + 验收标准措辞修正（review MINOR m1/m2）**：与 §8 step 4 + §4.4 对齐
  （保 idle nudge hook；grep `MARKER_REGEX` in code = 0，注释/docstring 解释性提及 OK）。
- **stale 注释清理（review MINOR m4）**：`cli.py:756` `rewriteText` → 主 session 经 skill 消费。

**测试清理**：
- `test_in_session_v8.py`：删 17 个 transform/marker 守门测试 + `_extract_ts_function_body`
  helper（无消费者）。新增 `test_orca_ts_has_no_transform_hook_step4` 守门防复活（裸键查，
  双 / 单引号形态都覆盖）。
- `test_web_default_and_open.py`：删 `TestOrcaOpenSlashContract`（8 测试，plugin-side
  `/orca open` marker 派发守门——step 4 扫描时漏的跨文件，review BLOCKER B1）。
- 修 `test_orca_ts_idle_hook_is_nudge_no_advance`：去掉已失效的 transform early-return 断言。

## 与计划的偏差

- **DEFECT-1 选择 python 而非「jq 缺失时报错」**：python 与 orca 一致（无新依赖），跨环境
  可靠；「jq 缺失时报错」仍依赖 jq 存在才能跑通判断逻辑，治标不治本。
- **DEFECT-2 保留位置参数兼容**：用户说「保留位置参数兼容或仅 `--run-id`（你定）」——选了
  双形态（位置 + option），向后兼容既有调用 / 测试 / SKILL.md 流程示例。
- **step 4 spec 决策 #12 措辞修正**：原决策「删整个 orca.ts plugin」与 §8 step 4 +
  验收标准「保留 idle nudge hook」矛盾——§4.4（step 2b 加入 nudge）后决策 #12 未同步。
  实现遵循 step 4 + 验收（保 idle nudge hook），spec 决策 #12 修正对齐。

## 验证

- **DEFECT-1**：5 个真子进程行为测试（含 fail-loud 回归）+ 静态守门收紧（python3 必备 +
  执行体禁 jq）；28 install_cmds tests 0 回归。
- **DEFECT-2**：5 个新 status 测试（mirror / json / 同值 / 异值 / 不存在 run），140
  in_session tests 0 回归。
- **step 4**：185 affected tests passed（in_session + install + web_default_and_open），
  0 回归。grep `MARKER_REGEX` in code = 0（仅注释/docstring 解释性提及）。
- **code-reviewer 三轮**：
  - DEFECT-1：0 BLOCKER / MAJOR；3 MINOR + 5 NIT 全闭环（CRLF .gitattributes / 异常覆盖
    对称 / throttle 副作用断言 / sorted 注释 / 静态断言收紧）。
  - DEFECT-2：0 BLOCKER / MAJOR；2 MINOR + 2 NIT 全闭环（错误路径测试 / stop+open 同型
    follow-up 留待）。
  - step 4：1 BLOCKER（test_web_default_and_open 跨文件漏扫）+ 1 MAJOR（advanceCount
    死代码）+ 5 MINOR 全闭环。

## 已知 follow-up（step 5a/5b/6，非本步）

- **step 5a**：删 setup 全栈（§6.1 清单：schema/compile/MCP/RunContext/Orchestrator）+ MCP
  migration note（§6.2）。**A2 铁律**：execute phase gate 校验保留。
- **step 5b**：daemon batch emit + 错误信封统一（独立 commit，C3）。
- **step 6**：teams install nga/cac nudge 机制真机验证（留用户侧）。
- **DEFECT-2 follow-up（review MINOR#1）**：stop / open 同型 docs/CLI 错配（spec/SKILL.md
  写 `--run-id`，CLI 用位置参数），记 CURRENT.md 下个 sprint 收。
- **合并推迟**：决策核心合并（`advance_step`↔`Orchestrator`），见 unified-backend draft，
  等触发条件。
