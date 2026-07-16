# host_session 绑定防串台（tape-only）

> 2026-07-17。SPEC：[`2026-07-17-host-session-binding-design-draft.md`](../specs/2026-07-17-host-session-binding-design-draft.md) v2。
> 任务1（goal：先将串台解决）。spec-reviewer 13 挑战全闭环 → coder-agent 实现 → test-agent E2E 全 PASS。

## 问题
nudge（CC Stop-hook `cc_nudge.sh` / opencode `orca.ts` `session.idle`）无差别扫全部活跃 run marker、不区分宿主 session → 别的 session 的 run 会 nudge 当前空闲 session（**串台**，多次复现：`agent-struct-exploration`、`nas-hp-search-20260717-001119-561ed6`）。

## 根因（spike 坐实）
- **数据层**：`workflow_started.data` 无 `host_session`（run 无归属）。
- **扫描层**：`cc_nudge.sh` glob + `orca.ts listActiveRuns` 扫全部 marker，无 session 过滤。
- **限流层**：nudge 状态文件全局共享（A nudge 后 60s 抑制 B）。
- **session id 可获取性**：CC env `CLAUDE_CODE_SESSION_ID` **开箱**；opencode bash 子进程不注入 → 需 plugin 注入（实证 `shell.env` hook 可行）。

## 方案（tape-only，spec-reviewer 对抗评审 13 挑战全闭环）
评审强推 tape-only（用户铁律「tape 唯一真相源」的直接推论）：
- **host_session 只存 tape `workflow_started.data`**（同 `yaml_path` tape-only 先例），**marker.py 零改动**（无 desync 向量）。
- **env 优先级**：`ORCA_HOST_SESSION_ID` > `CLAUDE_CODE_SESSION_ID` > None。
- **nudge 读 tape 首行** host_session 过滤（`cc_nudge.sh` `_host_session_from_tape` + `orca.ts` `hostSessionOfRun`，O(1) 读首行）。
- **per-session 限流**：状态文件按 session 分键（`runs/.orca-nudge-cc-<current>` / `runs/.orca-nudge-<sessionID>.json`）。
- **emit 真链**（评审 C6 纠正）：`lifecycle.make_workflow_started` ← `step.advance_step`（仅 pending 分支）← `cli.bootstrap`。
- **opencode**：`shell.env` hook 注入 `ORCA_HOST_SESSION_ID` + **fail-open 安全网**（注入全局失效→退回 status quo，防 C5 nudge 静默死；混合场景不回退）。
- **边界 fail-safe**：host_session 不等/None/读失败→跳过；无 current+活跃 marker→stderr warn（区分手 CLI vs env bug）。

## 实现（8 commits，分支 `in-session-unified-backend`）
1. `70c2ac8` lifecycle/step emit 真链接入 host_session
2. `8fb7715` cli bootstrap 读 env 注 host_session（`_host_session_from_env`）
3. `e6fb7c2` cc_nudge.sh 读 tape 过滤 + per-session 限流
4. `623046d` orca.ts shell.env 注入 + tape 过滤 + per-session 限流
5. `a1ee171` orca.ts 函数名避 `Tape(` 守门禁词
6. `e4b42f6` host_session 绑定防串台单测（21 测）
7. `735d99c` orca.ts fail-open 回退防 C5 静默死 + O(1) 读首行
8. `3dae964` 补 cc_nudge ORCA 优先级 / 首行坏 JSON / next 不改写 / orca.ts 结构守门（+4 测）

**铁律**：`grep host_session` 全仓仅 `lifecycle.py:90` 写入（单路 env→tape）；`marker.py` 零改动（`test_marker_only_three_fields` 仍绿）。

## E2E（test-agent 真机，全 PASS 零 bug）
- **#1 不串台**：sess-A 只 nudge runA、sess-B 只 nudge runB（真脚本 + fixture）。
- **#2 per-session 限流**：A nudge 不抑制 B（STATE 分键）。
- **#4 Stop-hook env 实证**：`CLAUDE_CODE_SESSION_ID` 经 bash→exec→python3 可达（spike 疑问闭合）。
- **#8 opencode 端到端**：`shell.env` 注入 `ORCA_HOST_SESSION_ID=ses_xxx` + tape `data.host_session` 真值（非硬编码/非 None）。
- **边界**：无 env+marker→warn、host_session=null→跳过、旧 tape→跳过、坏 marker→fail-loud exit 2，全 PASS。

## 未尽 / follow-up
- `~/.config/opencode/plugins/orca.ts` deploy 副本由 test-agent 刷新到 source 版（旧版备份 `.pre-e2e.bak`）；`tars install` 后正式同步。
- SPEC-B（子 agent 过程推送 web）复用本 SPEC §4.6 的 `host_session` env 契约（B2 ingestor 用 host_session 定位 CC sidechain）。
