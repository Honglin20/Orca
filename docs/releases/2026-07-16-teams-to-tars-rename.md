# Release Note —— 后端命令 teams → tars 改名

> 日期：2026-07-16 | 分支 `in-session-unified-backend` | 计划 [`docs/plans/2026-07-16-teams-to-tars-rename.md`](../plans/2026-07-16-teams-to-tars-rename.md)

## 背景 / 决策

用户决策（2026-07-16）：**后端 / 运维 CLI 命令 `teams` → `tars`**。

- 上一步（2026-07-15 TARS rebrand）已把用户面 skill 改名 `tars`；本步把后端命令也对齐——品牌收口：**skill = `tars` / 后端命令 = `tars` / in-session = `orca`**。
- `tars` 是后端/headless 入口：`tars install/run/serve/ps/validate/mcp/executor/list/logs/wait/resume/open`（operator 用）。
- **`orca` in-session 命令不动**（list/next/status/stop/open/doctor + `<wf>`，LLM-facing）。
- 机制保留：`ORCA_BACKEND_CMD` env 仍可覆盖显示名（只改默认值 `teams`→`tars`）。

## 改动点

### 1. 入口名（`pyproject.toml`）
- `[project.scripts]`：`teams = "orca.iface.cli.commands:main"` → `tars = "orca.iface.cli.commands:main"`。
- 注释块同步（teams→tars），并标注旧入口名 `teams` 已退役。
- **重装**（`pip install -e .`）让 `tars` 上 PATH、`teams` 退场（验：`which tars` ✓ / `which teams` 已 not found / `tars --help` 显示 `Usage: tars`）。

### 2. 默认命令名 + help/docstring（`orca/iface/cli/commands.py`）
- `DEFAULT_BACKEND_CMD = "teams"` → `"tars"`。
- `main()` docstring、命令名变量化注释段、`backend_cmd_name()` docstring 全部 teams→tars（`tars --help` 自我引用一致）。
- **加 `tars_app = app`**（新语义别名），**保留 `teams_app = app` 作 deprecated 别名**（向后兼容：外部代码 / notebook 仍 `from orca.iface.cli.commands import teams_app` 可用，注释标注 deprecated + 指引用 `tars_app`）。

### 3. validator 保留字（`orca/compile/validator.py`）
- `RESERVED_WF_NAMES` 集合里 `"teams"` → `"tars"`（wf name 禁取 `tars`，防 `orca <wf>` 语法糖撞后端命令名）。
- 注释 + `_check_workflow_name_reserved` docstring 同步；错误消息动态 `sorted(RESERVED_WF_NAMES)` 自动反映。

### 4. 用户面消息 + shipped artifacts
- `orca/iface/in_session/cli.py`：`orca --help` epilog「见 `teams --help`」→「见 `tars --help`」；doctor `skill_install` fail 提示「跑 `teams install`」→「跑 `tars install`」。
- `orca/iface/cli/install_cmds.py` + `skill_cmds.py`：docstring + `skill install` 弃用警告 stderr 全 teams→tars。
- shipped 模板 / skill 产物：`templates/__init__.py`、`cc_nudge.sh`（`'teams install --target cc'`→`'tars install'`）、`skills/__init__.py`、`skills/create-workflow/SKILL.md`（L102 `teams validate`→`tars validate`）。
- **`examples/mxint_analysis.yaml:7`**（code-reviewer R1 🟡 补）：用户面注释 `# 跑：teams run ...`→`tars run`（计划 grep 范围未覆盖 `examples/`，reviewer 扫到——用户照抄示例得失效命令）。

### 5. 测试同步
- `tests/iface/in_session/test_v3_step1.py`：保留字参数化 `"teams"`→`"tars"`；§2.4 `test_orca_help_lists_seven_commands_no_teams`→`no_tars`（loop var `teams_cmd`→`tars_cmd`）；§3.1 `test_orca_list_and_teams_list_*`→`tars_list`（`import app as tars_app`）；§3.2 `DEFAULT_BACKEND_CMD == "tars"` + `backend_cmd_name() == "tars"`；§4.5 SKILL.md 守门禁词 `teams run/serve/install`→`tars run/serve/install`（**禁词用 `tars <子命令>` 整串，非裸 `tars`**——`tars` 是入口 skill slash 名 `/tars`，SKILL.md 合法自我引用，避免误伤）。
- `tests/iface/cli/test_install_cmds.py` / `tests/test_skills_bundle.py`：docstring teams→tars。
- `tests/iface/cli/test_skill_cmds.py`（code-reviewer R2 🟡/🟢 补）：`test_orca_and_tars_both_aliases_work` 名实不符（名承诺验 binary alias、实际只验 skill 子命令注册）→ 改名 `test_skill_subcommand_registered_on_app`（名副其实）+ 新增 `test_backend_entry_point_is_tars_not_teams`（deterministic 读 `pyproject.toml` 锁 `tars` 入口存在 + `teams` 已退役，binary 真上 PATH 仍由 test-agent 真机验）+ `test_teams_app_deprecated_alias_still_importable`（锁 `teams_app is tars_app is app`，防将来误删 deprecated 别名）。

### 6. SPEC 同步（live contract）
- `docs/specs/in-session-entry-and-simplification.md`：live 段全 teams→tars——§1（架构定性）/§2.2/§2.4（保留字 + 守门契约）/§3 + §3.1 + §3.2（命令族 + 变量化默认）/§4.3 + §4.4（install 命令 + hook 生成）/§10.7（决策）/§11（验收标准）。
- **§6.1 / §7.1 / §8 落地顺序进度块保留 `teams`**：这些是带 commit SHA 的历史步骤记录（`orca run→teams run` 迁移 / step 1 `teams 变量化` / step 2b `teams install` 落点代码），等价 changelog——改了反而失真。live 契约（用户该跑什么命令）已全在 §3/§4/§11 说 `tars`。

## 不改（计划 §1.6）
- `orca` in-session CLI（`iface/in_session/cli.py` 的 app + pyproject 入口 `orca`）——只改 docstring 里的后端命令名引用，行为零改动。
- `ORCA_BACKEND_CMD` env 名（机制名不变，只改默认值）。
- `orca.ts` / `cc_nudge` 引用的 `orca`（= in-session CLI，非后端）。
- 入口 skill `tars`（上一步已是 tars）。
- 历史 `docs/plans/*` `docs/releases/*` 里的 `teams`（历史记录，保留）。

## 验证
- **重装核验**：`pip install -e .` 后 `which tars` = `.../bin/tars`（上 PATH）/ `which teams` = not found（退场）/ `tars --help` 首行 `Usage: tars [OPTIONS] COMMAND...`（列 run/validate/list/ps/logs/wait/resume/serve/mcp/open/executor/install/skill）/ `orca --help` epilog 指 `tars --help`。
- **单测**：`pytest tests/iface/cli/ tests/compile/ tests/iface/in_session/` —— **768 passed，0 回归**（8 skipped = claude CLI 集成测试 + 真实 tape 缺失；净 +2：code-reviewer R2 补的 pyproject 入口锁 + teams_app 别名锁）。2 pre-existing failure（`test_orca_list_returns_inputs_schema_json`：`~/.orca/workflows/nas-agent-pipeline.yaml` 全局 wf 使 catalog 返 2≠1 = 测试未隔离 user-level 扫描根；`test_bg_run_ps_logs_wait_e2e`：`orca run --background` 选项不存在 = 测试 rot）经 **stash 对比父提交复现**确认与本次 rename 无关（登记为 follow-up，非本任务引入）。
- **grep 守门**：`grep -rn '"teams"\|teams ' orca/ tests/ --include="*.py"` = **0 活引用**（残余仅 `commands.py` 的 `teams_app` deprecated 别名 + 其注释，符合计划 §1.2）。
- **code-reviewer 两轮**：**0 🔴**。R1（code）🟡×1 已修（`examples/mxint_analysis.yaml:7` 用户面注释漏改）；R1 🟢 三项裁决——validator 不额外留 `"teams"`（design 注释自洽 + `teams` binary 已全退场，YAGNI）/ `in-session-unified-backend-draft.md` 推迟草稿 teams 残留不动（YAGNI，启用时再改）/ `test_orca_list_*` 测试隔离缺陷非本任务引入（登记 follow-up）。R2（test）🟡×1 已修（测试名实不符 → 改名 + 补 pyproject 入口锁）、🟢×1 已修（teams_app 别名补 identity 锁）。

## Commit
`<本 commit，SHA 见 git log>`（单 commit：pyproject + commands + validator + shipped artifacts + tests + SPEC + 计划 + release note + CHANGELOG + CURRENT）。
