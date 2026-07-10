# orca install —— 统一安装入口（全局默认 + 合并 skill/in-session）

**日期**: 2026-07-08
**分支**: `phase13-in-session-v8`
**计划**: [`docs/plans/2026-07-08-unified-install.md`](../plans/2026-07-08-unified-install.md)
**SPEC**: [`docs/specs/in-session-shell-design-draft.md`](../specs/in-session-shell-design-draft.md) §2.4 / §11 回填

## 背景

Orca 此前安装链路**碎片化 + scope 不一致**：`pip install` → `orca skill install`（全局 skill）→ `orca in-session start <wf>`（**项目级** opencode 模板 + CC marker）。三个命令、两个命令组、两种 scope 默认。用户实测在 opencode session 看不到 `/orca`，调查还挖出**比 UX 更深的既有缺口**：

- **既有缺口**：`_install_opencode_templates()` 只写 `orca.ts` + `orca.md` 两个文件，**完全不碰 `opencode.json`**；且目录名 `plugin/`（单数）与官方 `plugins/`（复数）不一致；现有测试只断言文件存在 + 源码 grep，**无真·opencode 加载 e2e**——缺口没被任何测试拦住。

## Step 0 spike 结论（承重，全部验证）

`/tmp/orca-install-spike/` + `/tmp/orca-spike-global/` 真跑 opencode 1.14.22：

| 命题 | 结论 | 证据 |
|---|---|---|
| 光丢 `.ts` 到 `plugins/` 自动加载？ | ❌ **不加载** | 加声明前 0 marker、无 `loading plugin` 日志 |
| `opencode.json` 声明 `"plugin":[<path>]` 加载？ | ✅ **必须且充分** | 加声明后 log `service=plugin path=file://... loading plugin` + eval/factory/event 3 marker 全现 |
| `opencode serve` 加载项目 plugin？ | ❌ 不加载（只 `run`/TUI） | serve log 不含项目 config；run log 含 |
| 全局（`~/.config/opencode/` + 绝对路径）？ | ✅ 成立，与 cwd 无关 | `OPENCODE_CONFIG_DIR` 隔离 + 空 cwd 仍加载 |
| `ctx.serverUrl` 注入？ | ✅ `http://localhost:4096/` | 印证 orca.ts 假设 |

**→ `orca install` 必须合并 `opencode.json` 加 `"plugin"` 声明（项目相对路径 / 用户绝对路径）+ 同时写 `command/orca.md`（`/orca` 命令来源）。光丢文件不够。**

## 改动

### 新增 `orca install`（`orca/iface/cli/install_cmds.py`）
统一入口：`orca install [--target claude|opencode|all] [--scope user|project]`，全局默认。

- **opencode**：写 `plugins/orca.ts` + `command/orca.md` + `skills/create-workflow/` + 合并 `opencode.json` 的 `"plugin"` 声明（项目 scope 相对 `./.opencode/plugins/orca.ts`；用户 scope 绝对 `<config_dir>/plugins/orca.ts`）。
- **claude**：写 `skills/create-workflow/`（CC in-session hooks 是 **per-run**——`settings.json` 片段内嵌 tape_path/run_id，无法全局装；仍由 `in-session start` 生成）。
- 复用 `skill_cmds.SKILL_NAME` + 新抽 `opencode_global_root(home)` 单一真相源（含 `expanduser` 兜底）。
- 原语：`_atomic_write_with_backup`（tmp+replace，从 in_session.cli 搬来）+ `_merge_json_file`（读-改-写保已有键，损坏 fail-soft 读 / 原子写）。

### 弃用 `orca skill install`（`skill_cmds.py`）
降为弃用别名：warn + 委托 `run_install(target, "user")`。**行为升级**：opencode target 现额外装 plugin/command/声明（不再是 skill-only）。删 `_bundled_skill_dir` / `shutil` / `bootstrap_config` 死代码。

### 收窄 `orca in-session start`（`in_session/cli.py`）
删 opencode 模板落地分支 + `_install_opencode_templates` + `_atomic_write_with_backup`（搬到 install_cmds）。保留 CC marker + settings 片段生成。**CC-only run bootstrap**——opencode 用户不再需要 `start`（`/orca run` 的 `bootstrap` 运行时自举）。

### 注册 + 文档
- `commands.py`：注册 `install` sub-Typer（callback 形态，避免 `orca install install` 双层嵌套）。
- `README.md`：in-session + skill 章节的安装/使用 → `orca install`（全局默认）。
- SPEC §2.4：`plugin/`→`plugins/`、加载机制事实（声明必需 / 无自动发现 / serve 不加载项目 plugin）、引用 `orca install`。§11：统一安装标记**已落地**，`start` 收窄、`skill install` 弃用。

## 测试

- **新增** `tests/iface/cli/test_install_cmds.py`（17 case）：`resolve_roots` 矩阵 + `OPENCODE_CONFIG_DIR` 覆盖 + opencode 全套落地 + opencode.json 合并（保 `$schema`/其他 plugin/自定义键、去重幂等、损坏恢复、非数组 warn）+ 项目相对 vs 用户绝对 + claude 只 skill + 不拷 benchmark + fail loud + 模板防漂移 + legacy singular warn + **零业务逻辑守门**（grep install_cmds 源禁 `orca.run/events/schema`/advance/router/replay/tape）。
- `test_skill_cmds.py`：`test_install_fail_loud` → `test_skill_install_deprecated_warns_and_delegates`（fail-loud 路径移交 install_cmds 测）。
- `test_in_session_v8.py`：4 个 `start` 写 `.opencode/` 旧测试 → 新契约（`start` 不再写 `.opencode/` + 指向 `orca install`/`/orca run`）。
- `tests/iface` 全层 **689 passed / 1 skipped**；collect-only 全量 1875 测试无 collection 错。

## Review 闭环

`code-reviewer` 自检：**0 BLOCKER**。4 🟡 + 3 🟢 全处理：
- 🟡#1 `OPENCODE_CONFIG_DIR` 解析两处重复 + `expanduser` 不一致 → 抽 `opencode_global_root` 共享，消除漂移。
- 🟡#2 `plugin` 非数组静默重置丢数据 → warn + 显式告知原值。
- 🟡#3 docstring 称「CI grep 守门」未兑现 → 补 `test_install_cmds_has_no_orca_business_logic` 让承诺成真。
- 🟡#4 legacy singular `plugin/` 迁移 warn 无测试 → 补。
- 🟢#1 绝对路径断言对称性隐患 → 补 `startswith("/")` 意图断言。
- 🟢#3 `_merge_json_file` 非原子 window → docstring 显式声明。

## 成功标准达成

1. `pip install orca` 后**单条** `orca install` → opencode plugin/command/声明 + skill 全局就位。
2. 全局默认（`~/.config/opencode/`），无需 per-project。
3. `orca skill install` 仍可跑（warn + 委托，向后兼容）。
4. 既有「光丢文件不声明」缺口消除，spike 结论回填 SPEC 并有测试守住。
5. `tests/iface` 全绿 + 零业务逻辑守门。

## 未做（scope 外，spec §11 已记）

- `orca mcp`（phase-10）安装合并（`--host mcp`）——接口预留 `--target` 扩展位，未来加 mcp 不破。
- 真·opencode `/orca doctor` 端到端加载验证（需 opencode provider auth + 交互 TUI）——留 test-coverage-e2e 真跑。
