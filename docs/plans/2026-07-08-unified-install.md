# 实施计划：统一安装入口 `orca install`（全局 + 合并 skill/in-session）

> **日期**：2026-07-08
> **分支**：`phase13-in-session-v8`
> **SPEC**：`docs/specs/in-session-shell-design-draft.md` §2.4（本计划同步回填「统一安装」收口，SPEC line 445 已预告 defer）

## Context（为什么做）

Orca 当前安装链路**碎片化 + scope 不一致**，用户实测阻塞：

| 步骤 | 命令 | scope | 装什么 |
|---|---|---|---|
| 1 | `pip install orca` | — | `orca` 本体 |
| 2 | `orca skill install` | **全局** | create-workflow skill |
| 3 | `orca in-session start <wf>` | **项目级**（cwd `.opencode/`） | opencode plugin/command 模板 + CC marker |

三个命令、两个命令组（`skill` vs `in-session`）、两种 scope 默认。用户在 opencode session 里看不到 `/orca`，根因有**两层**：

1. **碎片化**：要装 in-session 得记另一个命令、还得在正确 cwd 跑。
2. **既有缺口（更严重）**：`_install_opencode_templates()`（`cli.py:687-710`）**只写 `orca.ts` + `orca.md`，完全不碰 `opencode.json`**。但 SPEC §2.4 明确 opencode 加载 plugin 靠 `opencode.json` 声明 `"plugin": ["./.opencode/plugin/orca.ts"]`（spike 验证）。且目录名 `plugin/`（单数）与官方 `plugins/`（复数）不一致。现有测试（`test_in_session_v8.py:396-460`）只断言文件存在 + 源码 grep，**无真·opencode 加载 e2e** → 这个缺口没被任何测试拦住。

**结论**：光跑 `start` 也不够，plugin 可能根本没被加载。统一安装必须**同时修这个加载缺口**，不能只做 UX 合并。

**目标**：一条 `orca install` 收口所有宿主集成，全局默认，幂等，含 opencode plugin **加载声明**。`pip install` 那步不可消除（任何方案都要先有 `orca` 本体），其后的「装集成」从两命令/两 scope → 一命令/一默认。

**不做**（本计划 scope 外）：
- `orca mcp`（phase-10 MCP 壳）的安装合并 —— SPEC line 445 预言的 `--host mcp` 收口留独立小设计，本计划只收 skill + in-session。但接口预留 `--target` 扩展位，未来加 mcp 不破。
- in-session 运行时逻辑（bootstrap/next/marker/plugin TS）**零改动**——本计划只动「安装/落地」层。

## 关键决策（接口）

### 命令面

```
orca install [--target claude|opencode|all] [--scope user|project]
  默认：--target all --scope user（全局）
  幂等：文件内容相同跳过；不同 backup .bak；JSON 配置读-改-写保已有键
```

per target 职责：

| target | --scope user（默认） | --scope project |
|---|---|---|
| **opencode** | `~/.config/opencode/`：`skills/create-workflow/` + `plugins/orca.ts` + `command/orca.md` + `opencode.json` 合并 plugin 声明 | 项目 `.opencode/` 同结构 |
| **claude** | `~/.claude/skills/create-workflow/` + `~/.claude/settings.json` 合并 Stop/PostToolUse hook 片段 | `.claude/` 同结构 |

`OPENCODE_CONFIG_DIR` 仍被尊重（复用 `skill_cmds.install_targets` 的解析）。

### 三个钉死的子决策（含 why）

1. **目录名用官方 `plugins/`（复数）**，非当前 `plugin/`（单数）。why：官方自动发现目录；避免依赖 opencode.json 声明即可加载。迁移：若检测到旧 `start` 写的 `.opencode/plugin/`（单数）残留 → 清理 + warn。

2. **opencode.json 的 `"plugin"` 声明：必须写（spike 已裁决）**。opencode 1.14.22 **无 `plugins/` 目录自动发现**——光丢 `.ts` 不加载；必须在 `opencode.json` 声明 `"plugin": [<path>]` 才加载（spike：加声明前 0 marker + 无 `loading plugin` 日志；加声明后 log `service=plugin path=file://.../.opencode/plugins/spike.ts loading plugin` + eval/factory/event 3 marker 全现）。故 install **必须合并 `opencode.json`**：
   - 项目 scope：相对路径 `"plugin": ["./.opencode/plugins/orca.ts"]`
   - 用户 scope（默认全局）：**绝对路径** `"plugin": ["/Users/<u>/.config/opencode/plugins/orca.ts"]`（全局 config 非项目相对；spike 用 `OPENCODE_CONFIG_DIR` 隔离 + 空 cwd 验证全局加载成立、与 cwd 无关）
   - 旁证：`opencode serve` **不**加载项目级 plugin（只 `run`/TUI 加载）——doctor/e2e 须用 `run` 路径，不能用 serve 验。

3. **CC settings.json 改为自动读-改-写合并**（带 `.bak`），替代当前 `start` 的「打印片段让用户手贴」。why：对齐 opencode「丢文件即生效」体验；手贴是 CC 路独有的反人类步。合并策略：读现有 `settings.json`（不存在则 `{}`）→ 合并 `hooks.Stop` / `hooks.PostToolUse` 数组（去重 by command）→ 原子写 + backup。

### 弃用 / 复用

| 旧命令 | 处置 | why |
|---|---|---|
| `orca skill install` | **deprecate 别名**：打印 `⚠ deprecated, use 'orca install'` + 委托 `orca install`（行为升级：现在也装 in-session） | 收口；别名保向后兼容 |
| `orca in-session start <wf>` | **repurpose 为 CC-only run bootstrap**：只写 CC 激活 marker（`owner=run_id`）+ 提示「hooks 已由 `orca install` 装好」。**删掉**它的 opencode 模板落地（移到 `install`）。opencode 用户不再需要 `start`（`/orca run` 的 `bootstrap` 子命令在运行时写 sessionID marker） | 拆分 install（静态、全局）vs bootstrap（per-run）；opencode 路的 `start` marker 本就 vestigial |

## 实施步骤

### Step 0 —— 承重 spike：opencode plugin 加载机制 ✅ 已完成（2026-07-08）

**裁决**：opencode **无目录自动发现**，`opencode.json` 声明是唯一加载入口；`serve` 不加载项目 plugin（须 `run`）；全局用绝对路径声明 + `~/.config/opencode/opencode.json`，cwd 无关。详见上方「关键决策 2」。原 spike 步骤记录备查：

`/tmp/orca-install-spike/`，opencode 1.14.22 实测：

1. 建极简 plugin（`console.error("[spike] loaded")` on load）放 `.opencode/plugins/spike.ts`，**不写 opencode.json**。`cd` + `opencode run "hi"` → 看 `[spike] loaded` 是否出现。
   - 出现 → **自动发现成立**，`plugins/` 复数即可，install 不写 opencode.json。
   - 不出现 → 进 2。
2. 加 `opencode.json`：`{"plugin": ["./.opencode/plugins/spike.ts"]}`，重跑 → 出现则**声明必需**，install 须合并 opencode.json。
3. 全局验证：用 `OPENCODE_CONFIG_DIR=/tmp/orca-spike-global` 放 `plugins/spike.ts` → 任意 cwd 跑 opencode → 验全局加载。
4. 结论写回本计划 + SPEC §2.4 回填（`plugin/`→`plugins/` + 加载机制事实）。

> spike 失败兜底：声明路径（SPEC 原 spike 已证）必成立，故最坏情况 = install 多一步合并 opencode.json，**无方案级风险**。

### Step 1 —— `orca install` 主体（`orca/iface/cli/install_cmds.py` 新模块）

- 新 sub-Typer `install`（同 `skill_cmds` / `executor_cmds` 既有 nested sub-Typer 模式）。
- 复用：`skill_cmds.install_targets()`（skill 目标解析）、`_atomic_write_with_backup()`（从 `in_session.cli` 提到共享 util，或就地复用）、CC hook 片段渲染 `render_cc_settings_fragment()`。
- opencode 落地：写 `skills/` + `plugins/orca.ts` + `command/orca.md`；（依 step-0）合并 `opencode.json`。
- claude 落地：写 `skills/` + 合并 `settings.json` hooks。
- fail loud：任一目标不可写 → exit 1 + stderr 报路径（同 `skill_cmds` G4）。

### Step 2 —— 旧命令处置

- `skill_cmds.install` → deprecated wrapper（warn + 委托）。
- `in_session.cli.start` → 删 opencode 模板落地分支；保留 CC marker + 改提示文案指向 `orca install`。

### Step 3 —— README + SPEC 回填

- README「in-session shell」章节：`start` → `orca install`（全局默认）；删「每项目跑一次 start」表述。
- SPEC §2.4：`plugin/`→`plugins/`、加载机制事实（step-0 结论）、统一安装收口（line 445 defer 项落地）。

### Step 4 —— 测试

- `install_cmds` 单测：目标解析（user/project × claude/opencode/all）、幂等（内容同跳过 / 不同 backup）、opencode.json/settings.json 读-改-写保键、不可写 fail loud。
- 弃用测试：`skill install` warn + 委托；`start` 不再写 opencode 模板。
- **新增真·加载断言**（补既有缺口）：spike 结论若 = 自动发现 → 测试断言 install 产物落在 `plugins/`（复数）；若 = 声明必需 → 断言 `opencode.json` 含声明。（文件级断言；真·opencode 进程加载 e2e 留 test-coverage-e2e agent。）
- 铁律 grep：install_cmds 零 Orca 业务逻辑（只拷文件 + 合并 JSON），守门同 in-session 模板。

### Step 5 —— 自我 review + status

- `code-reviewer` 自检（依赖铁律 / DRY / fail loud / 测试覆盖意图）。
- release note → CHANGELOG 索引 → CURRENT.md 更新（强制流程）。

## 文件改动（预估）

| 文件 | 改动 |
|---|---|
| `orca/iface/cli/install_cmds.py` | **新增** install sub-Typer |
| `orca/iface/cli/commands.py` | 注册 `install` Typer |
| `orca/iface/cli/skill_cmds.py` | `install` → deprecated wrapper |
| `orca/iface/in_session/cli.py` | `start` 删 opencode 模板分支；`_atomic_write_with_backup` 提共享 util |
| `orca/iface/in_session/templates/__init__.py` | 暴露 opencode 模板源路径给 install（不再 start 独占） |
| `tests/iface/cli/test_install_cmds.py` | **新增** |
| `tests/iface/in_session/test_in_session_v8.py` | 改：start 不再写 opencode 模板的断言 |
| `README.md` / `docs/specs/in-session-shell-design-draft.md` | 文档回填 |

## 成功标准

1. `pip install orca` 后**单条** `orca install` → 任意 opencode 项目 `/orca doctor` 回报告（plugin 真加载）。
2. 全局默认：`~/.config/opencode/` 产物生效，无需 per-project 操作。
3. `orca skill install` 仍可跑（warn + 委托，向后兼容）。
4. step-0 spike 结论回填 SPEC，既有「光丢文件不声明」缺口消除且有测试守住。
5. 单测 + 铁律 grep 全绿；test-coverage-e2e 真·opencode 加载验证 PASS。
