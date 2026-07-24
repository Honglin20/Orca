# Plan: in-session v5 §8 step 6 —— teams install nga/cac 全套安装（CAC≡cc / NGA≡opencode）

> SPEC：`docs/specs/in-session-entry-and-simplification.md` §8 step 6 / §4.3 / §4.4 / §11
> 状态：草稿（待 spec-reviewer）| 分支 `in-session-unified-backend` | 最后一步
> **用户澄清（2026-07-15）**：CAC ≡ Claude Code（`.claude`→`.cac`，其余同）；NGA ≡ opencode（`.opencode`→`.nga`，其余同）。install 阶段把两家族**全套统一装上**（不只 skill）。

---

## 0. 目标与成功标准

`teams install --target cac` 装上 **cc 家族全套**（skill + nudge Stop-hook）；`--target nga` 装上 **opencode 家族全套**（skill + plugin orca.ts idle nudge + json 声明）。两家族结构与各自基座（cc/opencode）相同，仅目录前缀换（`.claude`→`.cac`、`.opencode`→`.nga`）。

**成功标准**：
1. `teams install --target cac` → `.cac/skills/orca/SKILL.md` + `.cac/hooks/orca-nudge.sh` + `.cac/settings.json`（Stop hook 声明）。
2. `teams install --target nga` → `.nga/skills/orca/SKILL.md` + `.nga/plugins/orca.ts` + opencode.json plugin 声明（路径指 `.nga`）。
3. cc/opencode 原行为不变（回归）。
4. 幂等（重跑覆盖、内容同跳过、json 去重保已有键）。
5. 单测 + 真机 E2E（纯 CLI，禁 MCP）：cac/nga 全套生成 + 结构与 cc/opencode 对称。

---

## 1. 现状（侦察）+ 缺口

### 已有（step 2b era）
- `resolve_roots`（install_cmds.py:75-118）已正确：cac→`<home>/.cac`/project `.cac`、nga→`<home>/.nga`/project `.nga`。
- `_install_skill(hr.root)` root-relative ✓；`_install_cc_nudge(hr)` 用 `hr.root`（hooks/settings 全 root-relative）✓；`_install_opencode(hr)` skill/plugin/json 全 `hr.root` ✓。
- `run_install`（L404-419）：**只** opencode 走 `_install_opencode`、cc 走 skill+`_install_cc_nudge`；**cac/nga 只装 skill**（L411 else 分支，注释「nudge 取决于真机，本期只装 skill」）。

### 缺口（step 6 要补）
1. **路由**：cac 应走 cc 家族（skill + nudge）、nga 应走 opencode 家族（skill + plugin + json）。当前 run_install 没这么分。
2. **硬编码 `.opencode`**：`_opencode_plugin_decl`（L226-231）project scope 写死 `"./.opencode/plugins/orca.ts"` → NGA project scope 应 `./.nga/plugins/orca.ts`。须泛化为按 `hr.root.name` 派生。
3. **命名/docstring**：`_install_cc_nudge` / `_install_opencode` 现服务整个家族（cc+cac / opencode+nga）——docstring 注明家族适用（或重命名为 family-generic，coder 定）。

### 不缺（核实过）
- `_install_cc_nudge` 全 root-relative（hr.root/hooks、hr.root/settings.json）→ cac（hr.root=.cac）直接可用，无需改路径逻辑。
- `_install_opencode` 主体 root-relative → nga（hr.root=.nga）可用，仅需修 #2 的 plugin_decl 硬编码。

---

## 2. 改动范围

### 2.1 `run_install`（install_cmds.py:404-419）按家族路由
```python
for hr in roots:
    if hr.host in ("opencode", "nga"):      # opencode 家族
        written = _install_opencode(hr)      # 泛化后 nga 亦可
        ...
    else:  # cc / cac：cc 家族
        dirs = _install_skill(hr.root)
        ...
        if hr.host in ("cc", "cac"):         # cc 家族都装 nudge
            for comp, p in _install_cc_nudge(hr).items(): ...
```
（删旧「cac/nga 只装 skill」注释。）

### 2.2 泛化 `_opencode_plugin_decl`（L226-231）
- project scope 相对路径：`./{hr.root.name}/plugins/orca.ts`（hr.root.name = `.opencode` 或 `.nga`），不再写死 `.opencode`。
- user scope 绝对路径：`str(plugin_dst.resolve())`（已 root-relative，无需改）。

### 2.3 docstring / 命名
- `_install_opencode` / `_install_cc_nudge` docstring 注「服务 opencode 家族（opencode+nga）/ cc 家族（cc+cac）」。重命名可选（KISS：保名+docstring 注即可，避免大改）。

### 2.4 假设（NGA 配置文件名）
- NGA ≡ opencode「其他默认相同」→ 配置文件名仍 `opencode.json`（非 nga.json），位于 hr.root（user）/ cwd（project）。**若真机 NGA 实读 `nga.json` 或别处，留真机修正（§9#1）**——本步按「文件名同、目录换」实现，surface 此假设。

### 2.5 SPEC 同步（spec-reviewer #4，用户澄清 supersede 旧 SPEC）
- 用户 2026-07-15 澄清（cac=cc 家族全套 / nga=opencode 家族全套）supersede SPEC §11（旧仅「生成 SKILL.md」）+ §4.3（nga「同 opencode」措辞）+ §4.4。同步更新这三处为「cac=cc 家族全套（skill+nudge）/ nga=opencode 家族全套（skill+plugin+json）」，保 spec-plan 一致。

---

## 3. 架构审视

- **单事实源**：install 单一实现，四 host 共用 `_install_skill`/`_install_opencode`/`_install_cc_nudge`（家族复用），不新增 cac/nga 专用安装逻辑——仅路由 + 1 处路径泛化。无多套安装。
- **单接口**：`teams install --target <host>` 统一入口，四 host 行为对称（家族内同）。
- **依赖铁律**：install_cmds 在 iface/cli，依赖 schema/compile（catalog 等）单向，不改依赖。
- **fail loud**：未知 target BadParameter（既有）；install OSError 报错（既有）。
- **改后清理**：删「cac/nga 只装 skill」注释；泛化后无 `.opencode` 硬编码残留。

---

## 4. 测试 / E2E（纯 CLI，禁 MCP）

### 单测（`tests/iface/cli/test_install_cmds.py`）—— spec-reviewer #1/#2 关键
- **改写 `test_install_cac_and_nga_targets`（L426-434）**：现状循环断言 cac+nga `not (root/"plugins").exists()` → step 6 后 nga 有 plugin **必 fail**。拆两支：
  - cac 支：断言 `.cac/hooks/orca-nudge.sh` + `.cac/settings.json` 含 Stop hook（**现状 cac 零覆盖，本步补**）+ 无 plugins/command。
  - nga 支：删 no-plugins，加 `.nga/plugins/orca.ts` + json 声明断言。
- **新增 nga `--scope project` 单测（验 L230 泛化的唯一确定性闸门）**：project scope 下 cwd 根 `opencode.json` 的 plugin 声明须含 `./.nga/plugins/orca.ts`（user scope 走 L231 绝对路径本就 root-relative，**不修 L230 也能过** → 必须 project scope 测才抓得住泛化 bug）。
- **cac nudge 幂等**：重跑 settings.json Stop 去重断言（key `orca-nudge` host 无关）。
- **回归**：cc/opencode 全套既有端到端测试（test_install_opencode_*、test_install_cc_*）保绿——路由改造纯增量。

### E2E（test-agent 真机，纯 teams/orca CLI）
- `teams install --target cac --scope project`（tmp cwd）→ 真生成 .cac 三件套；`orca doctor` skill_install 反映。
- `teams install --target nga --scope project` → 真生成 .nga 三件套 + json 声明路径 .nga。
- cc/opencode 回归（`teams install --target cc`/`opencode` 仍全套装）。
- 幂等重跑。
- 测后清 tmp .cac/.nga project 目录。

---

## 5. 风险 / scope

- **R1（NGA 配置文件名/位置）**：假设 NGA 读 `opencode.json`（同 opencode）。真机若读 `nga.json`/别处 → 留 §9#1 真机修正，本步按「目录换、文件名同」实现并 surface。
- **R2（CAC settings.json hook 格式）**：CAC≡cc → settings.json Stop hook 格式同 cc。真机若 CAC hook 格式不同 → §9#1 修正。
- **R3（真机加载）**：install 装上 ≠ CAC/NGA 真加载 skill/plugin/hook——后者是 §9#1/#4 跨平台真机验证，留用户侧。本步只保证**装得对、结构对称**。
- **R4（nga user-scope 路径，spec-reviewer #3）**：resolve_roots nga user-scope = `~/.nga`，但 opencode user-scope = XDG `~/.config/opencode`；若 NGA≡opencode 对称，NGA user-scope 更可能 `~/.config/nga`。**未验证，defer §9#1 真机修正**。step 6 不引入此问题（resolve_roots 来自 step 2b），但首次让 nga 真装东西，在此 surface（**本步不动 resolve_roots**，仅登记假设）。
- **scope**：路由 + plugin_decl 泛化 + docstring + 测试 + SPEC 同步。不改 cc/opencode 既有逻辑（仅路由让 cac/nga 复用）、不改 skill 内容、**不改 resolve_roots**（nga user-scope 路径假设见 R4，留真机）。

---

## 流程闭环
本计划 → **spec-reviewer**（核：家族路由合理 / plugin_decl 泛化点 / NGA 配置文件名假设 / 测试覆盖对称 / 不破 cc-opencode 回归）→ **coder-agent**（路由 + 泛化 + docstring + 测试 + commit + 状态文档）→ **test-agent** 真机（纯 CLI：cac/nga 全套 + 回归 + 幂等）。**spec v5 §8 全 step 收尾**。
