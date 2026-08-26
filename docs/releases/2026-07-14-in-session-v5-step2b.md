# Release: in-session v5 §8 step 2b —— 入口切 skill + list inputs_schema + doctor skill_install + 删 start/cc_hooks/command + nudge hook

**日期**: 2026-07-14
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §8 step 2b（含 A5 修正：nudge 入 step 2b(7)）
**Branch**: `in-session-unified-backend`
**Commits**: `e2bd989`（items 1-6）+ `4b90508`（item 7 nudge）

## 做了什么

实施 SPEC v5 §8 **step 2b** 全 7 项：in-session 入口统一切到 orca skill，删旧 command/start/cc_hooks
入口，nudge hook 提醒主 session 推进（绝不自动推进）。**唯一真相源 + 单一接口 + 架构整洁**。

### items 1-6（commit `e2bd989`）

1. **建 orca skill**（`orca/skills/orca/SKILL.md`）：三步指导（`orca list` 选 wf → 据 `inputs_schema`
   抽 inputs → `orca <wf> --inputs` + 自调 `orca next --run-id --output` 循环到 done）。skill 绝不读
   YAML。CI 守门测试断言三步指导 + 禁业务逻辑关键词 + 禁 teams 命令（§4.5）。
2. **`orca list` 返 inputs_schema**：`{workflows:[{name, description, inputs_schema:[{name,type,description}]}]}`
   （B3：无 has_setup）。catalog 加 `inputs_schema`（list 形态，`_inputs_to_schema_list` 单一派生点）。
   **无 describe 命令**（v5 决策：冗余——list 一个命令给齐选 wf + 抽 inputs）。
3. **doctor 加 skill_install 硬检查**（A6）：扫四前端 user+project scope。hook 心跳改可选
   （entry/advance 永不 fail——transform 派发已禁用）。`ok` 仅由 `hard=True` 检查决定（加 `hard` 字段，
   取代硬编码 name tuple）。`_scan_skill_install` 加 home/cwd 注入 seam（修测试隔离 bug）。
4. **禁用 orca.ts transform marker dispatch**（B1）：early `return input`。**不整删**（v5 修正：
   idle hook 保为 nudge 载体）；transform 段 + 死代码 step 4 删，`_constants.py` MARKER_REGEX 同步删。
5. **删 command 模板**：`templates/opencode/command/orca/{doctor,run,status,stop}.md`（4 文件）。
6. **删 `start` 命令 + `cc_hooks.py`**（A 路径退场，死代码）；`daemon.py`/`templates __init__` 清引用。

**install 重构（§4.3 四前端）**：`teams install --target cc/opencode/cac/nga/all` 装所有随包 skill
（`orca` + `create-workflow`，按 `SKILL.md` 存在扫描，OCP）。平台常量 `HOST_DOTDIR`/`SKILL_TARGETS`/
`SKILL_HOSTS` 抽到 `skill_cmds` 单一真相源（`install_cmds` + `in_session/cli._scan_skill_install` 共享，
消除三处副本，review DRY/OCP 闭环）。opencode 仍装 plugin + `opencode.json` 声明（orca.ts 惰性，
step 4 整删）；command 模板已删，install 清理 legacy command 目录。

### item 7 nudge（commit `4b90508`，A5 修正：本步做）

**B 路径铁律（load-bearing）**：nudge = REMIND ONLY。hook 绝不调 `orca next`（那退化 A 路径自动推进）；
next 仍主 session 自调。判定只看 marker 存在（不用 tape 超时，会误报）。

- **opencode nudge（orca.ts）**：`session.idle` hook 从旧 A 推进（REST fetch + spawnCli next +
  promptAsync 下个 prompt）改为 nudge 提醒：`listActiveRuns()` 扫 `runs/orca-*.json` →
  `nudgeAllowed()` 全局 60s 节流 → `client.session.promptAsync` 注入提醒文本。删旧推进死代码；
  清当下孤儿（`SERVER_BASE_URL_FALLBACK`/`serverBaseUrl`/`readMarker`，idle 改 nudge 后无调用方）；
  model split 校验（缺/空/无斜杠 → 回退默认）。
- **CC nudge**：新资产 `cc_nudge.sh`（扫 marker → `decision:block` 提醒 + 60s 节流，**全篇零反引号**
  ——双引号 REASON 内反引号 = bash 命令替换会误执行 next，退化 A）。`teams install --target cc`
  拷脚本到 `<root>/hooks/orca-nudge.sh` + 合并 `<root>/settings.json` 的 `hooks.Stop`（去重 + 保已有
  键 + 非法形态 warn+重置）。

## 与计划的偏差

- **`orca list` 渲染分叉**（review 🟡）：`orca list` 出 JSON（给 skill/LLM），`teams list` 出文本
  （给运营）。共享**同一 catalog**（`catalog.list_workflows()`），渲染层按消费者不同——非两套 list
  实现。step-1 的「单一 run_list 实现」在此演化为「单一 catalog 数据源 + 两种渲染」。
- **install 保留 opencode plugin**（orca.ts 惰性）：step 2b 不整删 orca.ts（idle hook 保为 nudge 载体），
  故 install 仍拷 plugin + `opencode.json` 声明保不悬空；step 4 删 transform 段 + 死代码 + 声明。
- **CC nudge `decision:block`**：CC Stop hook 的提醒机制是 block（force continuation）。bounded by
  marker 清除（workflow 完成 → marker 清 → Stop 放行）+ 合规计数器（真卡住 → workflow_failed）。
- **cac/nga nudge**：仅装 skill（nudge 机制取决于前端真机，step 6 验证）。本期 cc/opencode 两种
  原生机制（promptAsync / Stop block）已实施。
- **跨 session 误注入（已知限制）**：v3 marker 文件名去 sessionID，nudge 扫所有活跃 run 注入当前
  idle session；多 session 共存时非主 session idle 会跨渗收到提醒。单 workspace 单 session 约定下
  无影响，多 session 由后续 spec 收。

## 验证

- **affected 单测**：208 passed（in_session + catalog + install + skill_cmds + mcp tools），0 回归。
  仅 env-gated e2e 测试因 WSL conda 缺 `uv`/`opencode` 二进制失败（非本次改动）。
- **code-reviewer 两轮**：items 1-6 轮 2 BLOCKER（SKILL.md 守门测试 + doctor 测试隔离 bug）+ 关键
  MAJOR（DRY/OCP 平台常量、catalog 契约测试、doctor hard 字段、dispatch_count 漂移、多平台覆盖）
  全闭环；item 7 轮 0 BLOCKER + 关键 MAJOR（守门断言精度、model split、节流注释、当下孤儿清理）
  全闭环。
- **验收（§11 相关）**：`orca --help` 7 命令无 teams/start/describe；`orca list` 返 inputs_schema 无
  has_setup；`orca doctor` 报 skill_install（装 skill 后 pass）；command 模板/start/cc_hooks 不存在；
  orca.ts transform early-return；SKILL.md 三步 + 无业务关键词；`teams install --target cc/opencode`
  装 skill 到对应目录。
- **真机验证留用户侧（test-agent 的活）**：opencode promptAsync 注入 / CC Stop block / skill 实跑 wf /
  cac+nga nudge 机制。

## 已知 follow-up（step 3-6，非本步）

- **step 3** skill 完善（catalog via `orca list` + inputs 代填已就位）+ catalog 物理迁 `orca/compile/`。
- **step 4** 整删 orca.ts transform 段 + 死代码（extractTaskOutput/spawnCli/buildCliArgs/rewriteText/
  MARKER_REGEX 等）+ `_constants.py` MARKER_REGEX/LITERAL（保 idle nudge hook）。
- **step 5** 删 setup 全栈（schema/compile/MCP/RunContext/Orchestrator，§6.1 清单）+ MCP migration +
  daemon batch emit + 错误信封统一。
- **step 6** teams install nga/cac nudge 机制真机验证。
- **合并推迟**：决策核心合并（`advance_step`↔`Orchestrator`），见 unified-backend draft，等触发条件。
