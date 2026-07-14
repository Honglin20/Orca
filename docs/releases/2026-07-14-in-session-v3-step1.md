# Release: in-session v3 §8 step 1 —— orca 接口打包 + 14 命令归宿 + teams 变量化 + marker 精简

**日期**: 2026-07-14
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v3 §8 step 1
**Branch**: `in-session-unified-backend`

## 做了什么

实施 SPEC v3 §8 **step 1**（orca 接口打包 + 14 命令归宿 + teams 变量化 + marker 精简），不改 step 2+（cc_hooks A 删 / start 删 / orca skill 建立 / command 模板删 / transform 删 / setup 删 / NGA-CAC 适配）。

### orca 单一接口（§2.1 七命令定型）

- `orca` 顶层 = in_session/cli.py 的 `app`（删 `in-session` 子命令层）。7 命令：`list` / `<wf>` / `next` / `status` / `stop` / `open` / `doctor`。
- **`orca <wf>` 语法糖**（§2.1）：custom click Group `_OrcaTopLevelGroup.resolve_command` 把未注册的首 token 重写为 `bootstrap <wf>`。**单一实现**——bootstrap 是 hidden 命令，`<wf>` 是 rewrite sugar（非双入口）。
- bootstrap 位置参数接 wf **名**（catalog 精确反查）或 yaml 路径（`_resolve_wf_path`）。
- 新增 `orca list`（委托 `commands.run_list`，单一 catalog 逻辑）+ `orca open [<run_id>]`（默认当前活跃 run，复用 `teams open` 的 `_open_run`）。
- `start` 标 **deprecated**（warn），不删（§8 MS2：step 2b spike 通过后才删）。
- 删 in-session `serve`（归 teams，MS6）。

### 14 命令归宿（§3.1）

- 后端/headless 命令（`run/serve/ps/logs/wait/resume/install/validate/mcp/executor/skill`）归 **teams** entry point（`commands.py:app` = `teams_app`）。
- `list` catalog 共享（orca + teams 都暴露，单一 `run_list` 实现）。
- `open` 共享（单一 `_open_run`；orca 默认活跃 run，teams 按 run_id）。

### teams 命令名变量化（§3.2）

- env `ORCA_BACKEND_CMD`（默认 `teams`）控制 backend 显示名（`backend_cmd_name()`）。pyproject 加 `teams = "orca.iface.cli.commands:main"` entry。

### marker 精简（§7.2 m11）

- `ActivationMarker` 只 `{run_id, model, no_output_count}`。删 `tape_path/yaml/session_id/owner`（desync 向量）。
- 文件名固定 `orca-<run_id>.json`，`next`/`stop` 用 `marker_path(rundir, run_id)` **O(1) 直定位**（删 `find_marker_by_run_id` 扫描）。
- yaml 运行时从 tape 唯一真相源派生：`make_workflow_started` 加可选 `yaml_path` 字段（bootstrap 期记入 canonical realpath），`next` 的 `_load_wf_for_run` 读 tape.workflow_started.data.yaml_path 反查（fallback catalog 名查）。

### 重复 bootstrap fail loud（§7.3 m12）

- 同 wf（按 `wf.name` 经 tape workflow_started 匹配）已有活跃 marker → fail loud 提示续跑 / 先停（取代旧 N1 复用）。
- **TOCTOU 闭环**（review B1）：bootstrap serialize 锁用 well-known `.orca-bootstrap.lock`（NOT per-run_id）——per-run_id 锁无法防同 wf 并发（两进程各 gen 不同 run_id → 各锁不同文件）。全局锁 serialize 所有 bootstrap，第二者锁内看到第一者 marker → fail loud。

### 保留字黑名单（§2.2 MS1）

- `RESERVED_WF_NAMES`（orca 7 + teams 后端 + `teams`）compile 期校验；wf 取保留名 → ConfigurationError（保 `orca <wf>` 语法糖无歧义）。

### B1 同 commit 改全活调用点（§8）

- `cli.py:_drive_protocol` 字面量 `orca in-session next` → `orca next`（+ §5.2 引号转义 `'\''` 规约）。
- `orca.ts` spawn `["orca", "in-session", ...]` → `["orca", ...]`；`buildCliArgs` 删 `--owner`/`--session-id`（marker 精简后 bootstrap 不再接受）；Marker interface `tape_path` → optional；transform 的 readMarker 派发废弃（sessionID≠run_id，A 路径 step 2b 删）。
- `cc_hooks.py` spawn `orca in-session next` → `orca next`。
- command 模板（run/doctor/status/stop.md）改新命令名。

### _inputs_from_tape 首调噪声修复（§7.5）

- tape 无 workflow_started（bootstrap 首调正常态）→ **静默**返 {}（不 WARNING）。
- 仅 tape 有 workflow_started 但 data.inputs 缺/坏（真异常）才 WARNING。

## 与计划的偏差

- **dupe-check 按 wf.name 而非 yaml realpath**（review m4）：marker 只 3 字段不存 yaml，wf.name 经 compile 保唯一，是 realpath 的合理代理。两不同 yaml 同名 wf 视为同一 wf（符合「同 wf 不重复 bootstrap」语义）。注释显式记录此偏差。
- **catalog 物理位置未迁 orca/compile/catalog.py**：保留 iface/mcp/catalog.py（单一实现，两 app 都调它），迁址为 follow-up（CURRENT.md 已记）。

## 验证

- **in-session 单测**：134 passed（含 37 新增 step-1 验收 + review 补缺测试），0 回归。
- **CLI 后端 + compile + orchestrator 单测**：281 passed，0 回归。
- `orca --help` 含 7 命令、不含 teams 命令名（守门测试 + 真二进制 smoke）。
- `teams --help` 含后端命令族。
- marker dataclass 恰 3 字段（精确集合断言）。
- coordinator 铁律自检：无两套 list / 两套 bootstrap 入口 / 两套 marker 定位；依赖单向；fail loud。

## 已知 follow-up（step 2+）

- orca.ts dead code（readMarker / extractTaskOutput / REST fetch / promptAsync）——step 4 删。
- command 模板 + transform 钩子——step 2b 删（skill 建立 + spike 通过后）。
- `start` 命令——step 2b 删。
- setup 全栈删（含 MCP breaking）——step 5。
- catalog 迁 orca/compile/catalog.py——择期。
- daemon.py 逐条 emit → batch emit（B-8）——择期。

## Commit

- `orca in-session v3 step 1` —— 见 `git log`（本 release note 对应的 commit）。
