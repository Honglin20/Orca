# Release Note —— TARS 品牌 rebrand（skill 改名 tars + TARS 描述，CLI 仍 orca）

> 日期：2026-07-15 | 分支 `in-session-unified-backend` | 计划 [`docs/plans/2026-07-15-tars-skill-rebrand.md`](../plans/2026-07-15-tars-skill-rebrand.md)

## 背景 / 决策

用户决策（2026-07-15）：**skill 改名 `tars` + TARS 描述（CLI 仍 `orca`）**。

- **用户面 = TARS**：skill 名 `orca`→`tars`（slash `/tars`），description 改 TARS 语气——触发「用 TARS 帮我 X」「用 TARS 做 Y」「TARS，优化模型结构」→ 调 `orca list` 拿 description → **语义匹配用户意图** → 命中唯一启动 / 多个则问（≤2 问）→ 抽 inputs → 派子代理 → `orca next` 循环到 done。
- **CLI/命令仍 `orca`**（list/next/status/stop/open/doctor 不改）——TARS 是用户面身份，底层用 orca 引擎。`orca.ts` / `cc_nudge` 引用的 `orca` = CLI，不动。

## 改动点

### 1. skill 目录 + SKILL.md（`orca/skills/orca/` → `orca/skills/tars/`）
- `git mv orca/skills/orca orca/skills/tars`。
- frontmatter：`name: orca` → `name: tars`；`description` → 计划 §2 的 TARS 文案（逐字）。
- body：标题 `# orca` → `# TARS`；`<purpose>` 框 TARS 身份（「你是 TARS，运行在主 session 里。你的底层引擎是 Orca……与用户对话时你是 TARS；调命令时用 `orca`」）。
- **命令引用全保 `orca`**（`orca list` / `orca next` / `orca status` / `orca stop` / `orca open` / `orca doctor` / `orca <wf>` —— CLI 引擎名不改，仅 skill 身份是 TARS）。

### 2. `ENTRY_SKILL_NAME` 常量（单一真相源，防漂移）
- `orca/iface/cli/skill_cmds.py` 新增 `ENTRY_SKILL_NAME = "tars"`（紧邻 `SKILL_TARGETS`，注释说明：用户面 = TARS，目录 `orca/skills/tars/`，doctor/install/test 三处共用以防目录名与 check 漂移；skill body 里命令仍 `orca`）。
- `orca/iface/cli/install_cmds.py`：import 块加 `ENTRY_SKILL_NAME`（与既有 `SKILL_NAME` 并列 re-export，供 install 测试作稳定引用，DRY）。
- `orca/iface/in_session/cli.py` `_scan_skill_install`：import `ENTRY_SKILL_NAME`，把硬编码 `(root / "skills" / "orca" / "SKILL.md")` 改为 `(root / "skills" / ENTRY_SKILL_NAME / "SKILL.md")`。
- doctor `skill_install` check 的 detail 文案 + docstring 措辞同步：「orca skill 已装」→「TARS skill 已装」（用户面品牌一致）。

### 3. 测试同步
- `tests/iface/cli/test_install_cmds.py`：4 处 install 落地断言 `skills/orca`→`skills/tars`（opencode/cc/cac/nga 四前端）。
- `tests/iface/cli/test_skill_cmds.py`：helper `_orca_skill_file`→`_entry_skill_file`（用 `ENTRY_SKILL_NAME`）；3 处断言改用常量（非散落字面量）。
- `tests/iface/in_session/test_in_session_v8.py`：fixture helper `_install_fake_orca_skill`→`_install_fake_entry_skill`（目录名 + frontmatter name 都用 `ENTRY_SKILL_NAME`）；4 处 call site + docstring 同步。
- `tests/iface/in_session/test_v3_step1.py`（计划 ref map 漏列，coder 补）：§4.5 SKILL.md 守门 —— 常量 `ORCA_SKILL_MD`→`ENTRY_SKILL_MD`，路径 `skills/orca`→`skills/tars`；3 个 gate 测试函数 `test_orca_skill_md_*`→`test_entry_skill_md_*`（断言里 `orca list`/`orca next` CLI 字面保留正确）。

### 4. SPEC 措辞同步（契约一致）
- `docs/specs/in-session-entry-and-simplification.md` §4.1：「新增 `orca` skill」→「新增入口 skill（TARS 品牌：用户面 = TARS，底层用 orca CLI 引擎）」。
- §8 落点表上方描述：「建 orca skill」→「建入口 skill（TARS 品牌）」。

## 不改（计划 §1.5）
- CLI 命令 / `orca.ts` / `cc_nudge` / `cc_nudge.sh`（皆引用 `orca` = CLI 引擎）。
- `create-workflow` skill（独立 skill，不 rename）。
- `_bundled_skill_sources`（glob `iterdir()`，自动适应 rename，零改动）。
- 历史 `docs/plans/*` `docs/releases/*` 里的 `skills/orca`（历史记录，保留）。

## 偏离计划
- **test_v3_step1.py / test_skill_cmds.py 计划 ref map 漏列**：计划 §1.3 只列了 `test_install_cmds.py` + `test_in_session_v8.py`。但 `test_v3_step1.py:432` 的 §4.5 守门常量 `ORCA_SKILL_MD` 路径指向 `skills/orca`（rename 后必 fail），`test_skill_cmds.py` 有 4 处 `skills/orca` 断言——两处都是漏网的活引用，coder grep 全仓后补改（fail loud：漏改则单测红）。

## 验证
- **单测**：`pytest tests/iface/cli/test_install_cmds.py tests/iface/cli/test_skill_cmds.py tests/iface/in_session/` —— **176 passed，0 回归**（baseline 175 + code-reviewer 🟡#2 补的 frontmatter name gate 净增 1）。覆盖：install 四前端装 `skills/tars/`（非 orca）；doctor skill_install 找 tars（常量）；create-workflow 仍装；§4.5 gate 守门 rename 后的 SKILL.md + frontmatter name 锁。
- **grep 守门**：`orca/` 代码树 + `tests/` 下 `skills/orca` 活引用 = 0（残余全在 `docs/plans/*` `docs/releases/*` 历史记录，按计划保留）。
- **命令保 orca 核验**：SKILL.md body 所有 CLI 命令引用（list/next/status/stop/open/doctor/<wf>）逐字保 `orca`，未误改 `tars`；`tars`/`TARS` 只出现在 frontmatter name + description + 标题 + purpose 身份。
- **code-reviewer 两轮**：0 🔴 / 0 🟡 必修（code 轮 0 finding；test 轮提 2 🟡——① test_install_cmds 入口 skill 断言改用 `install_cmds.ENTRY_SKILL_NAME` 常量 DRY；② 补 frontmatter `name` gate 锁「目录名 == slash 触发名 == 常量」——**已全部修**）。
- **test-agent 真机**：由主 session 派（纯 CLI 禁 MCP）—— `teams install --target cc` → `.claude/skills/tars/SKILL.md` 真生成（name=tars、description TARS）；`orca doctor` skill_install pass；create-workflow skill 也在。

## Commit
`<本 commit，SHA 见 git log>`（单 commit：rename + SKILL.md + doctor 常量 + 测试 + SPEC + 计划 + release note + CHANGELOG + CURRENT）。
