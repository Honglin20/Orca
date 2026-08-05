# Release Note: PostToolUse 事后告警守卫（防主 session 下场干活）

> **日期**：2026-08-05  | **分支**：`in-session-unified-backend`  | **SPEC**：[`posttooluse-rogue-guard.md`](../specs/posttooluse-rogue-guard.md)（spec-review 闭环 conditional-pass，18 真问题已修订）

---

## 0. 摘要

给四前端（cc / cac / opencode / nga）加 PostToolUse **事后告警守卫**：主 session 在活跃 run 期间
若自己用了「做节点活」的工具（Edit/Write/跑 train 等），在其执行**后**注入一条**纯文本提示**，
提醒它「这是子代理的活，改派 Task 或调 `orca next`」。**仅当本 session 有活跃 run 时触发**；
不阻止动作（动作已发生）；不调 `orca next`（B 路径铁律不变）。覆盖 §4.4 Stop/idle nudge 的 turn
中途盲区——主 session 在 turn 内连续调工具「下场」时 Stop/idle 没机会触发。

## 1. 实施清单（SPEC §12）

| # | 改动 | 文件 | 状态 |
|---|---|---|---|
| 1 | 新建 tool-classification.json 单一真相源（§5） | `orca/iface/in_session/templates/tool-classification.json` | ✅ |
| 2 | cc_nudge.sh 加 hook_event_name 分支（Stop 字节级不变 / PostToolUse 新增 §7.3 输出） | `orca/iface/in_session/templates/cc_nudge.sh` | ✅ |
| 3 | install_cmds._install_cc_nudge 合并 hooks.PostToolUse 条目（matcher + 去重）+ 拷 classification | `orca/iface/cli/install_cmds.py` | ✅ |
| 4 | orca.ts 加 tool.execute.after 钩子（mutex / sessionID fallback / 读 JSON / 独立 guard 节流键） | `orca/iface/in_session/templates/opencode/orca.ts` | ✅ |
| 5 | install_cmds._install_opencode 拷 tool-classification.json 与 orca.ts 同目录 | `orca/iface/cli/install_cmds.py` | ✅ |
| 6 | 单测覆盖 §11.1 / §11.2 / §11.4（4-前端对称 + 13 行为case + 守门） | `tests/iface/cli/test_install_cmds.py` | ✅ 24 新测 |
| 7 | §4.4 交叉引用本件 + templates `__init__.py` 文档更新 | 2 文件 | ✅ |

## 2. 关键设计决策

### 2.1 单脚本双事件（cc 家族 DRY）

`cc_nudge.sh` 按 stdin JSON 的 `hook_event_name` 分支：
- `Stop`：v5 §4.4 原 `decision:block` 行为**字节级不变**（60s 节流，键 `runs/.orca-nudge-cc-<sid>`）。
- `PostToolUse`：读 `tool_name`/`tool_input`/`session_id` → §5 分类 → §4 触发条件 → 命中则 stdout
  输出 `additionalContext`（**不** emit `decision`，**不** exit 2），30s 节流（键 `runs/.orca-guard-cc-<sid>`）。

stdin 读取策略：bash 用 `[ ! -t 0 ] && cat` 检测 pipe stdin（TTY/DEVNULL 不阻塞），通过 env var
`ORCA_HOOK_STDIN` 传给 python heredoc。这样既兼容原 Stop 测试（无 pipe stdin），又能读 CC 真实
PostToolUse pipe JSON。

### 2.2 session 解析 fallback（R5）

env 链优先（`ORCA_HOST_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` / CAC PID 回溯），取不到 → 从 stdin
JSON 的 `session_id` 字段取（CC hooks common input 声明）；两处都取不到 → 写
`runs/.orca-guard-unbound.json` 心跳 + 放行（fail-safe 降级）。

### 2.3 工具分类单一真相源

`tool-classification.json`（§5）：`writing_tools` + `bash_tools` + `readonly_bash_prefixes`
（word-boundary）+ `compound_separators`。cc_nudge.sh 与 orca.ts 启动时各 read 一次。CI grep 守门：
多字 readonly 前缀（如 `'git log'`）不应作为引号字面出现在 `*.sh` / `*.ts` 代码主体（容许 JSON 文件
内容 / 注释 / 字段名引用）。

**word-boundary 实现修正**：原 SPEC §5 措辞「取首个命令词」会令多词前缀（`git status`/`git log`）
永远不命中。实施时改为整 cmd 前缀比对（`cmd == prefix || cmd.startsWith(prefix + " ")`）——
既禁止 `ls` 命中 `lsof`/`lsblk`（E6 修订），又支持多词前缀。SPEC 措辞后续可校准（实施一致）。

### 2.4 orca.ts 共用内核（DRY）

`nudgeFile` → `throttleFile(scope, sessionID)` 参数化；`nudgeAllowed` / `markNudged` 接 scope
参数（idle `nudge` 60s / guard `guard` 30s 分键）。`tool.execute.after` 复用 `listActiveRuns` /
`hostSessionOfRun` / `injecting` mutex / `client.session.promptAsync`。

### 2.5 纯提示（pure hint，SPEC §2 用户决策）

PostToolUse 分支绝不 emit `decision:block` / `permissionDecision` / exit 2。结构化单测断言：

```python
assert "decision" not in out
assert "permissionDecision" not in out
```

`decision:block` 总出现次数 ≤ 基线 1（仅 Stop 分支合法含）。模板/plugin 内不得 spawn `orca` /
调 advance/router/tape 路径（B 路径铁律）。

## 3. Spike 结果（R1 + R5）+ fallback 状态

**SPEC §12 第 0 步 spike 委托给 test-agent 真机验证**（coder-agent 不跑真机 e2e）。SPEC 已声明
fallback，coder-agent 实施时 fallback 路径已编码并单测覆盖，真机 spike 留给 test-agent 闭环：

| Spike | 不确定项 | 已编码 fallback | 真机验证责任人 |
|---|---|---|---|
| **R1** | opencode `tool.execute.after` 的 `input.tool` / sessionID 取法 / bash 参数字段名 / mid-turn `promptAsync` 可调性 | sessionID 缺失 → 写 `runs/.orca-guard-unbound.json` 心跳 + return；`promptAsync` 失败 → console.error + 不计节流；args 字段名多候选（`input.args` / `input.command`） | test-agent（SPEC §11.3 E10） |
| **R5** | CC PostToolUse 子进程是否继承 `CLAUDE_CODE_SESSION_ID` | env 未注入 → 从 stdin JSON `session_id` 取；两处都缺 → 写心跳 + 放行 | test-agent（SPEC §11.2 真机 e2e） |

**单测已验证的 fallback 路径**：
- `test_cc_guard_session_id_fallback_when_env_missing`（R5 fallback 取 stdin.session_id）
- `test_cc_guard_unbound_heartbeat_when_no_session_anywhere`（R1/R5 fail-safe 心跳）
- `test_cc_guard_silent_when_classification_missing`（classification 缺失 fail-open + stderr warn）

若 test-agent 真机 spike 发现某家族彻底失败，按 SPEC §10 降级为「不覆盖」（心跳文件 + warn），
不阻塞另一家族。

### 3.1 真机 spike 闭环（test-agent，2026-08-05）

| Spike | 真机结论 | 证据 |
|---|---|---|
| **R5（CC env 链）** | ✅ **PostToolUse 子进程继承 `CLAUDE_CODE_SESSION_ID`**——与 Stop hook 同一 env 链。env 路径命中（`_host_session_from_env` 直接返回），stdin `session_id` fallback 不触发。 | 隔离 cwd `/tmp/orca-guard-test`，PostToolUse hook 子进程 env dump 实测含 `CLAUDE_CODE_SESSION_ID=631201d7-...`（与 Stop 同值，与 stdin `session_id` 同值）。 |
| **R1（opencode input 形状）** | ✅ **官方文档未给，真机实证**：`input.tool`（小写工具名，如 `"bash"`）/ `input.sessionID`（字符串）/ `input.callID` / `input.args = { command: "..." }`。sessionID **正常存在**，fallback `runs/.orca-guard-unbound.json` 不触发；args 字段名 = `args.command`（与 orca.ts 的 `args.command || args.args` 取法一致）。mid-turn `promptAsync` ✅ **可调**——deepseek 主 session 在 mkdir 后立即收到 §6 注入并响应「已执行。这是调试性操作，按提醒说明忽略即可」。 | 隔离 cwd `/tmp/orca-oc-test`（plugin id 改 `orca-r1-spike` 避开用户 global 或ca 同名冲突），dump 实测：`{"tool":"bash","sessionID":"ses_...","callID":"call_...","args":{"command":"echo r1orca"}}`；sqlite part 表查到 `【Orca 守卫·事后提醒】...用了 bash...r1test...` 真注入。 |

**两家族 merge blocker 解除**——SPEC §10 R1/R5 spike 项闭环，fallback 路径保留（编码 + 单测）但不依赖。

## 4. 测试

**新增 27 测**（`tests/iface/cli/test_install_cmds.py`）覆盖：
- **§11.1 安装对称（4 测）**：cc/cac PostToolUse 注册 + matcher；opencode/nga 拷 classification；
  幂等不重复；保留用户已有 PostToolUse 条目。
- **§11.1 E11 触发工具集相等（1 测）**：cc matcher 工具集（去 NotebookEdit/PowerShell）≡ opencode
  classification 的 writing+bash 工具集。
- **§11.2 PostToolUse 行为（14 测）**：Write/Edit 直命中告警 + 只读 Bash 静默 + orca next 静默 +
  python train.py 告警 + 复合命令（8 分隔符含 `>` / `>>` 重定向）告警 + word-boundary（`lsof`）
  告警 + 无活跃 run 静默 + 他 session 静默 + 30s 节流 + guard/nudge 分键互不影响 + classification
  缺失 fail-open + session_id fallback + unbound 心跳 + **malformed marker fail-open（review §四-1）**。
- **§11.2 Stop 字节级回归（1 测）**：Stop mock stdin → stdout 字段集 = {decision, reason}，含
  abc + orca next。
- **§11.4 守门（4 测）**：decision:block 计数 = 基线 1；行首裸 orca / 反引号 / `$(orca` 禁；
  tool-classification.json 单一真相源（多字前缀引号形态断言 + 反向 reference 断言）；
  orca.ts 含 `tool.execute.after` + 不 spawn 或ca/advance。
- **§11.4 install_cmds 零业务逻辑**（1 测，扩展原测）：新增 PostToolUse 仍是纯配置合并。
- **host_session_binding structural test**（1 测修）：`nudgeFile` rename → `throttleFile(scope, sessionID)`
  守门同步 + `injectingIdle`/`injectingGuard` 独立 mutex 结构断言（review §四-2）。

**回归**：`test_install_cmds.py` 全套（含原 30+ 测）+ `test_in_session_v8.py` + `test_host_session_binding.py`
= **138 passed**（其中 cc_nudge.sh 行为测真子进程跑 bash + python3 heredoc）。

## 5. 与已退场 A 路径 PostToolUse 的区别（必须讲清）

| 维度 | 已删 A 路径 PostToolUse | 本件（B 路径守卫） |
|---|---|---|
| 目的 | 捕 Task output → cache → Stop 读 cache 驱动 next（**自动推进**） | 检测「下场干活」→ 注入提示（**不推进**） |
| 触发工具 | Task/Agent（捕子代理产出） | Edit/Write/Bash（下场干活） |
| output | 捕获并跨 hook 传递 | 不捕、不传 |
| 路径 | A（hook 驱动编排） | B（主 session 自调 next） |

本件**不是** A 路径复活——SPEC §3 对比表已闭环。

## 6. 已知限制 / 后续

- **Issue #34692**（CC PostToolUse 在 subagent 委派时被跳过）：良性，文档化于 SPEC §9。我们关心的
  触发工具（Edit/Write/Bash）不是 subagent 委派，不被跳过；主 session 正确委派 Task 时跳过 = 我们
  想要的（委派不该告警）。
- **真机加载**（R4）：CAC/NGA 真机是否读 `.cac`/`.nga` 的 settings.json/plugin 仍属跨平台用户侧
  验证（本件不解决，沿用现状假设）。
- **L1 契约锐化**：本件是症状层防御（L2），不解决 Q1 根因（L1 契约不够「唯一显然」）。L1 另议。

## 7. Commit

- `coder-agent` 实现 + 24 测：见 git log（本 commit）。
- code-reviewer 第二轮（fresh 拾遗）：1 must-fix + 2 should-fix 已修：
  - **must-fix §四-1**：PostToolUse 路径泄漏 exit 2。原 `_scan_my_active_run_ids` 在 marker 损坏时
    `sys.exit(2)`，会经 guard 路径退化成 exit 2，违反 SPEC §7.3 纯提示铁律。改为加 `strict: bool`
    参数：Stop 用 `strict=True` 保留 fail loud（marker 真相源契约）；guard 用 `strict=False` 改
    fail-open（stderr warn + 跳过该 run）。新增回归测 `test_cc_guard_failopen_on_malformed_marker`
    锁住「guard + 损坏 marker → exit 0 + 仍从其他合法 marker 告警 + stderr warn」。
  - **should-fix §四-2**：idle / guard 共用 `injecting` mutex 让 idle 的 await 期间所有 PostToolUse
    静默漏告警——恰好挡掉 guard 设计要覆盖的 turn 中途盲区。拆为 `injectingIdle` / `injectingGuard`
    两独立 Set，同路径重入仍互斥、异路径并发不再互相吞噬。`test_orca_ts_has_host_session_binding_hooks`
    加结构断言（`injectingIdle` / `injectingGuard` 各存在，旧 `injecting` 不再出现）。
  - **should-fix §三**：``>`` / ``>>`` 重定向不在复合分隔符集，会让 `cat foo > /etc/passwd` 这种
    rogue 写入漏过。加入 `compound_separators`（参数化测同步扩到 8 case）。
  - nice-to-have：补 `Edit` 直命中测（与 Write 同语义但单独锁，避免集合相等测掩盖个体 bug）；
    `_bash_command_is_writing` 空命令返回 True 加注释说明与 fail-open 不对称的理由。
- 后续：test-agent 四前端真机 e2e（SPEC §11.2 / §11.3）。

## 8. 验收

- [x] §11.1 安装（4 测）+ 工具集相等（1 测）
- [x] §11.2 PostToolUse 行为（12 测）+ Stop 字节级回归（1 测）
- [x] §11.4 守门（4 测 + install_cmds 零业务逻辑）
- [x] **§11.2 真机 e2e（test-agent，2026-08-05）**：隔离 cwd 真 `claude -p` 驱动——
  PostToolUse hook 真触发（`runs/.orca-guard-cc-<sid>` 时间戳落盘 + transcript 出现 `【Orca 守卫·事后提醒】...用了 Bash...r5test...`）；read-only 单 Bash（`cat`）静默（无 guard 文件）；无活跃 run 静默；**Stop nudge 回归 intact**（`runs/.orca-nudge-cc-<sid>` 时间戳落盘 + transcript 出现 `Stop hook feedback: 你还有活跃的 Orca run：r5test...`）。
- [x] **§11.3 opencode 家族真机 e2e + R1 spike 闭环（test-agent，2026-08-05）**：隔离 cwd + 隔离
  `OPENCODE_CONFIG_DIR` 真 `opencode run` 驱动——`tool.execute.after` 真触发（`runs/.orca-guard-<sid>.json` `last_nudged_at` 落盘 = promptAsync 成功后才写）；sqlite part 表实证 `【Orca 守卫·事后提醒】...用了 bash...r1test...` 真注入；read-only bash 静默；无活跃 run 静默；**idle nudge 回归 intact**（part 表实证 `【Orca nudge】你还有活跃的 Orca run：r1test...`）。
- [x] **R5 spike 闭环**：CC PostToolUse env 链继承 `CLAUDE_CODE_SESSION_ID`（实证），fallback 不触发。
- [x] **R1 spike 闭环**：opencode `input.tool` / `sessionID` / `args.command` / mid-turn `promptAsync` 全部实证可用（deepseek 主 session 收到 §6 注入并响应）；fallback 不触发。
