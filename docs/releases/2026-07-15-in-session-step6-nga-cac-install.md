# Release: in-session v5 §8 step 6 —— teams install nga/cac 全套（CAC≡cc / NGA≡opencode）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) §4.3（落点 + 家族）/ §4.4（nudge 家族）/ §11（验收）/ §9#1（真机风险）
**Plan**: [`docs/plans/2026-07-15-in-session-step6-nga-cac-install.md`](../plans/2026-07-15-in-session-step6-nga-cac-install.md)（spec-reviewer CONDITIONAL-PASS，3 处精度修订闭环）
**Branch**: `in-session-unified-backend`
**Commit**: `<本 commit，single-commit；SHA 见 git log / CHANGELOG>`
**前置**: step 2b `e2bd989`/`4b90508`（install 四平台落点 + nudge）+ step 4 `52cc9f3`（orca.ts transform 整删，保留 idle nudge）

> **spec v5 §8 全 step 收尾**（step 1 / 2b / 3b / 4 / 5a / 5b / defects / FU-1 / 批量 FU-2+3a+FU-3 / **6**）。

## 用户澄清（supersede 旧 SPEC）

2026-07-15 用户澄清：**CAC ≡ Claude Code**（`.claude`→`.cac`，其余同）、**NGA ≡ opencode**（`.opencode`→`.nga`，其余同）。install 阶段两家族**全套统一装**（不只 skill）。此澄清 supersede SPEC §11 旧「生成对应目录 SKILL.md」+ §4.3 nga「同 opencode」+ §4.4 nudge 单家族措辞。

## 做了什么

install 阶段把 cac/nga 从「只装 skill」升级为各自家族全套，结构与基座对称（仅 dotdir 前缀换）。

### 家族路由（`install_cmds.py:run_install`）

按家族路由（cc/opencode 行为 byte-identical，纯增量）：
- **opencode 家族**（`opencode` + `nga`）→ `_install_opencode`：skill + plugin `orca.ts`（idle nudge 载体）+ `opencode.json` 声明。
- **cc 家族**（`cc` + `cac`）→ `_install_skill` + `_install_cc_nudge`：skill + nudge Stop-hook（`hooks/orca-nudge.sh` + `settings.json` 声明）。
- 路由用显式 `elif hr.host in ("cc", "cac")` + 末尾 `else: raise AssertionError`（fail loud 铁律 12：不可达分支，防未来加第五 host 静默误归家族）。

旧「cac/nga 的 nudge 取决于真机，本期只装 skill」注释整删；cc/opencode 既有路径不变（旧 `if host == "opencode"` + `if host == "cc"` 两分支 → 新元组 membership，cc 仍 skill+nudge、opencode 仍 `_install_opencode`，参数同一 `hr`）。

### 泛化 `_opencode_plugin_decl`（`install_cmds.py`）

旧 project-scope 写死 `"./.opencode/plugins/orca.ts"` → 泛化为 `f"./{hr.root.name}/plugins/orca.ts"`。`hr.root.name` 由 `resolve_roots` 按宿主派生（opencode project → `cwd/.opencode` name=`.opencode`；nga project → `cwd/.nga` name=`.nga`）。故同一段代码服务整个 opencode 家族，opencode 旧值 byte-identical，nga 正确派生 `.nga`。user scope 走绝对路径分支（本就 root-relative，不读 `hr.root.name`）——这是 spec-reviewer #1/#2 关切的核心：**user scope 不改泛化也能过，必须 project scope 测才抓得住泛化 bug**。

### docstring 同步

`_install_opencode` / `_install_cc_nudge` / `_opencode_plugin_decl` / `install` callback / 模块顶部 五处 docstring 统一改「家族」语义并注 step 6 由来（保名 KISS：避免大改重命名，仅 docstring 注 family applicability）。

### SPEC 同步（§4.3 / §4.4 / §11 / §9#1）

- §4.3：落点表加「家族」列；install 行展开为两家族全套（cc 家族 skill+nudge / opencode 家族 skill+plugin+json）+ 用户澄清。
- §4.4：nudge 段改「opencode 家族（opencode+nga）」+「cc 家族（cc+cac）」。
- §11：验收从「生成 SKILL.md」展开为 cac=cc 家族全套 / nga=opencode 家族全套 双行。
- §9#1：风险面收窄措辞——从「skill 加载真机」→「全套集成（skill + nudge/plugin + json）真机加载与生效」（step 6 后风险面扩大）。

## 测试

`tests/iface/cli/test_install_cmds.py`（+4 净增）：

- `test_install_cac_family_full_set`（**重写**，旧 `test_install_cac_and_nga_targets` 拆分）：cac 全套——`.cac/hooks/orca-nudge.sh` + `.cac/settings.json` Stop hook 声明（断言 command 含 `.cac` 路径，意图揭示家族落点对称）+ 无 plugins/command。
- `test_install_nga_family_full_set`（**重写**）：nga 全套——`.nga/plugins/orca.ts` + `opencode.json` 恰好一条 orca 声明 + 绝对路径指向 `.nga`。
- `test_install_nga_project_scope_uses_dotnga_relative`（**新增，泛化闸门**）：project scope cwd 根 `opencode.json` 声明含 `./.nga/plugins/orca.ts` **且**不含 `.opencode`（双向闸门）。docstring 显式说明 user scope 抓不住此 bug。
- `test_install_cac_nudge_idempotent_no_duplicate`（**新增**）：cac nudge 重跑 settings.json Stop 去重（与 cc 同款）。
- `test_install_nga_idempotent_no_duplicate`（**新增**）：nga plugin 声明重跑去重（nga decl 经 `hr.root.name` 派生多一层间接，对称补测）。
- `test_install_cc_only_skill` → 重命名 `test_install_cc_family_full_set`（旧名是 step 2b(7) 加 nudge 前的 stale 痕，与新 sibling 命名对齐）。

回归：cc/opencode 既有端到端测试（`test_install_opencode_*` / `test_install_cc_*` / `test_install_project_scope_relative_declaration` 等）全绿、零行为变更。

### code-reviewer 两轮闭环

- **Round 1（代码）**：0 🔴。🟡 补 opencode project-scope byte-identical 镜像测试 → **Rule 7 surface**：既有 `test_install_project_scope_relative_declaration` 已直接断言 opencode project-scope 产 `./.opencode/plugins/orca.ts`（旧值），加镜像冗余（DRY），不补；nga 测试守泛化、此测试守 byte-identical，合起来完整。🟢 全采纳：`run_install` `else`→`elif`+fail-loud（采纳）/ `test_install_cc_only_skill` 重命名（采纳）/ SPEC §9#1 + §4.3 措辞收窄（采纳）/ CRLF→LF 归一（采纳，匹配 commit `0c87b02` 行尾规范）。
- **Round 2（测试覆盖）**：0 🔴。🟡 补 `test_install_nga_idempotent_no_duplicate`（采纳——nga decl 多一层 `hr.root.name` 间接）/ docstring 行号「L230」校准为函数名引用（采纳，防代码漂移）。

## 验证

- 单测：`pytest tests/iface/cli/test_install_cmds.py tests/iface/in_session/` → **164 passed 0 回归**（baseline 160 + 净增 4）。
- code-reviewer 两轮（代码 + 测试覆盖）。
- 行尾：两 .py 文件归一 LF（`.gitattributes` 强制 `eol=lf`）。
- test-agent 真机 E2E（纯 CLI，禁 MCP）由主 session 派，待跑。

## 假设 / 留给真机（§9#1）

- **NGA 配置文件名**：假设 NGA 读 `opencode.json`（≡opencode「其余同」）。真机若读 `nga.json`/别处 → 留 §9#1 修正。
- **CAC settings.json hook 格式**：CAC≡cc → Stop hook 格式同 cc。真机若 CAC hook 格式不同 → §9#1 修正。
- **真机加载**：install 装上 ≠ CAC/NGA 真加载 skill/plugin/hook。这是 §9#1/#4 跨平台真机验证，留用户侧。本步只保证**装得对、结构对称**。
- **nga user-scope 路径**：`resolve_roots` nga user-scope = `~/.nga`（来自 step 2b，本步不动）；若 NGA≡opencode 对称，user-scope 更可能 `~/.config/nga`（XDG）。defer §9#1 真机修正。

## scope

- 不动：`resolve_roots`（nga user-scope 路径假设见上）/ skill 内容 / cc/opencode 既有安装逻辑（仅路由让 cac/nga 复用）/ MCP / advance/router/replay/tape。
- 单 commit：代码 + 测试 + SPEC 同步 + 计划 + release note + CHANGELOG + CURRENT。
