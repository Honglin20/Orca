# Plan: TARS 品牌 —— skill 改名 tars + TARS 描述（CLI 仍 orca）

> 用户决策（2026-07-15）：skill 改名 tars + 描述（CLI 仍 orca）。用户面 = TARS（`/tars`、描述 TARS 语气：触发「用 TARS 做 X」→ 意图匹配 workflow → 多个则问）；内部 CLI/命令仍 orca（TARS 用 orca 引擎）。
> 状态：草稿 | 分支 `in-session-unified-backend` | spec v5 §8 已全闭环，本件为新需求

---

## 0. 目标

- skill 名 `orca` → `tars`（目录 + frontmatter name + slash 命令 `/tars`）。
- description 改 TARS 语气：触发「用 TARS 帮我 X / 用 TARS 做 Y / TARS，优化模型结构」→ 调 `orca list` 拿 description → **语义匹配用户意图** → 命中唯一启动 / 多个则问（≤2 问）→ 抽 inputs → 派子代理 → `orca next` 循环到 done。
- **CLI/命令保持 `orca`**（list/next/status/stop/open/doctor 不改）——TARS 是用户面身份，用 orca 引擎。orca.ts / cc_nudge 引用的 `orca` = CLI，不动。

---

## 1. 改动（ref map 已扫）

### 1.1 skill 目录 + SKILL.md
- `git mv orca/skills/orca orca/skills/tars`。
- `skills/tars/SKILL.md` frontmatter：`name: orca` → `name: tars`；`description` → §2 TARS 文案。
- body：`<purpose>`/标题框 TARS 身份（「你是 TARS，用 orca 的 7 个命令驱动 workflow」），**命令引用保 `orca`**（CLI 没改，别误改成 tars）。

### 1.2 doctor skill_install check（cli.py:881）—— 关键
- 现 `(root / "skills" / "orca" / "SKILL.md").is_file()` 硬编码 `orca` → rename 后 doctor 找不到装的 skill。
- **抽常量**（推荐，单一真相源）：`skill_cmds.py` 加 `ENTRY_SKILL_NAME = "tars"`（紧邻 `SKILL_TARGETS`），doctor 改 `(root / "skills" / ENTRY_SKILL_NAME / "SKILL.md")`。防 skill 目录名与 doctor check 漂移。
- install（`_bundled_skill_sources` glob 全 skill）无需改（自动含 tars）。

### 1.3 测试
- `tests/iface/cli/test_install_cmds.py` L112/191/437/461：`(… / "skills" / "orca" / …)` → `"tars"`（4 处断言）。
- `tests/iface/in_session/test_in_session_v8.py` doctor 测试：fixture 创建 `skills/orca` + docstring L87/88/148 → `skills/tars`（确认是 fixture 路径 + 断言，非纯注释）。

### 1.4 SPEC 措辞（可选，契约一致）
- SPEC §4.x「orca skill」→「TARS skill（用 orca CLI 引擎）」。§4.3 落点表 skill 列措辞同步。

### 1.5 不改
- CLI 命令 / orca.ts / cc_nudge / cc_nudge.sh 内容（皆引用 orca = CLI 引擎）。
- `create-workflow` skill（独立 skill，不 rename）。
- `_bundled_skill_sources`（glob，自动适应）。
- 历史 `docs/plans/*` `docs/releases/*` 里的 `skills/orca` = 历史记录，不改。

---

## 2. 新 description（TARS 语气）

```
TARS —— 在主 session 里把用户的一句话意图（「用 TARS 帮我 X」「用 TARS 做 Y」「TARS，优化模型结构」）自动匹配到已注册的 workflow 并驱动完成。当用户描述想做的事（而非直接给 workflow 名）时使用：调 `orca list` 拿全部 workflow 的 `description` → 据用户意图语义匹配 → 命中唯一则启动；多个可能则简短问用户选哪个（≤2 问，不把列表丢回去）→ 据 `inputs_schema` 抽 inputs → `orca <wf> --inputs` 启动 → 派 Task 子代理逐节点执行 → `orca next --run-id --output` 循环到 `done:true`。整个流程在主 session 内闭环，不依赖系统自动推进；绝不自己 Read workflow YAML（全经 `orca list`）。底层用 orca CLI 引擎。
```

---

## 3. 注册 workflow（配套，用户「先想知道」的——已当面解释，此处备查）

- `orca list` 扫 `./workflows/` + `~/.orca/workflows/`（SPEC §2.1）。
- 注册 = 把 workflow YAML（`name`/`description`/`inputs`/`nodes`/`routes`）+ agent prompt 放进去。
- **匹配靠 `description` 字段**（skill 据 description 语义匹配用户意图）——description 写清楚 = 自动匹中；多个 NAS workflow → skill 问用哪个（skill step 1 已支持）。
- 生成方式：`create-workflow` skill（一句话需求 → 合规 YAML + agent md + `orca validate`）。

---

## 4. 测试 / E2E（纯 CLI，禁 MCP）

- **单测**：install 四 host 装 `skills/tars/SKILL.md`（非 orca）；doctor skill_install 找 tars（常量）；create-workflow 仍装；test_v8 doctor fixtures 用 tars。
- **E2E（test-agent）**：`teams install --target cc` → `.claude/skills/tars/SKILL.md` 真生成（name=tars、description TARS）；`orca doctor` skill_install pass；create-workflow skill 也在。

---

## 5. 风险 / scope

- **R1（name 漂移）**：skill 目录名 / doctor check / 测试三处须一致 → 抽 `ENTRY_SKILL_NAME` 常量单一真相源。
- **R2（命令误改）**：body 里 `orca list/next/...` 是 CLI，**别**误改 tars（只 skill 身份是 TARS）。code-reviewer 把关。
- **scope**：rename + description + doctor 常量 + 测试 + SPEC 措辞。不改 CLI/orca.ts/cc_nudge/create-workflow。

---

## 流程闭环
本计划 → **coder-agent**（rename + description + doctor 常量 + 测试 + SPEC 措辞 + code-reviewer 重点查漏改的 `orca` skill 引用 + 命令别误改 + commit + 状态文档）→ **test-agent** 真机（纯 CLI：install 装 skills/tars + doctor pass + skill 内容 TARS）。
