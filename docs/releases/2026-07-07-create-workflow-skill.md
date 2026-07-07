# create-workflow skill + `orca skill install` + headless benchmark

> 日期：2026-07-07
> 关联：CLAUDE.md「SDD 开发流程」、参考仓 CCW（`reference/Claude-Code-Workflow`）

## 背景

Orca workflow 是声明式 YAML 契约（节点 / routes / agent 三态 / gates / retry / validator），手写门槛高。参考 CCW 的 `workflow-plan` skill 做法，落地一个**通用**的 `create-workflow` skill：吃用户描述或既有素材，归一化成 Orca workflow（YAML + agent md），强制自校验闭环。配套 `orca skill install` 显式装进 Claude Code + opencode 两边 skill 目录。

## 交付

### 1. create-workflow skill（随包分发）

`orca/skills/create-workflow/`（hatchling 默认把包内非 .py 文件打进 wheel）：
- **SKILL.md** —— 通用 pivot 规则（非死步骤）：任意输入 → 归一化 DAG → Orca 产物。两种入口（描述意图 / 已有文件夹）汇到同一中间模型。含 agent 三态自动决策 + H1-H7 硬规则 + 名称一致性 / 不派 explore 子任务等铁律。
- **reference/orca-workflow-contract.md** —— 完整 YAML 契约（知识源，schema 变只改这）。
- **examples/** —— 3 个最小 crib YAML（linear / parallel / foreach），均过 `orca validate`。
- **benchmark/** —— 16 case 评测集（见下）。

### 2. `orca skill install` 命令

`orca/iface/cli/skill_cmds.py`（sub-Typer，仿 `executor_cmds`），挂载于 `commands.py`：
- 把打包的 skill 拷到 `~/.claude/skills/create-workflow/` **和** `~/.config/opencode/skills/create-workflow/`（honoring `$OPENCODE_CONFIG_DIR`）。
- 🔴 **排除 `benchmark/`**（评测答案不进用户 skill 目录，防泄露）。
- 幂等（`dirs_exist_ok=True`，覆盖前 `⚠` 提示）、`--target claude|opencode|all`、fail loud（不可写 → exit 1 + stderr）。

### 3. headless benchmark + 公平评测 harness

- **16 case**（`benchmark/cases/`）：NL 从零（5）/ 转换异构 workflow（2）/ 散 agent md 组装（2）/ skill→agent（2，含脚本资产迁移）/ 混合（1）/ 设计文档（1）/ 只造 agent 池（1）/ script→节点或 agent（2）。每 case = `input.txt`（干净输入）+ `assets/`（转换素材）+ `case.md`（不变量）+ `expected/`（钉死产物，全过 validate）。
- **`scripts/run_skill_benchmark.py`** —— 公平 headless harness：workspace 在 repo 外（`/tmp`，避免 opencode 按 git root 定项目根污染源码）、copy skill 时**排除 benchmark/**（防答案泄露，子 agent 评审裁出并修）、`orca skill install` fail-loud、480s timeout、产出后定位 yaml + 跑 validate + 出 report。

### 4. 评测闭环（opencode 后端真跑）

按用户要求走完整 loop：headless 跑 skill → 子 agent 评审 harness 客观性（**裁出 P0 答案泄露并修**）→ 子 agent 检查产出 vs 预期 → 汇总问题 → 抽象**通用规则**（非 case-hack）→ 改 skill → 重跑。

**从 8/16 → 16/16**（每 case 均验证通过）。抽象出的通用规则（写入 SKILL.md H1-H7 + 铁律）：

| 规则 | 解决的失效模式 |
|---|---|
| H1 文件夹 agent 契约（scripts/ 子目录 + `$ORCA_AGENT_RESOURCES` 重写 + frontmatter） | skill 含脚本转换时路径/布局错 |
| H2 fan-in 默认 `set` + 引用 `<组>.output.outputs.<分支>` | merge 节点形态 + 数据契约 |
| H3 `validator`/`retry` 正交（别互替）+ outputs 不加 `.json` | 结构化校验/重试字段误用 |
| H4 workflow 必有 `outputs` | 链尾无出口 |
| H5 散 agent md → 引用不重写 | 组装场景素材被丢 |
| H6 节点最小化 / entry 即分支 + model 用 `provider/name` | 冗余 starter + model 前缀 |
| H7 script 节点不迁移脚本 vs 文件夹 agent | 两类节点资源语义混淆 |
| 铁律：skill 起草/prompt 片段 → 内联；角色 md → 引用 | 过度物化 agent md |
| 名称一致性：`agent: <name>` 逐字等于文件路径 | resolver 找不到 |
| headless 不阻塞（直接写最终路径、不 AskUserQuestion） | 全程卡死 |
| 素材就近读、别派 explore 子任务 | 无谓探索超时 |

## 测试

- `tests/iface/cli/test_skill_cmds.py` —— install 命令（两边装 / `--target` / 幂等 / fail loud / **排除 benchmark/**）。
- `tests/test_skills_bundle.py` —— 随包 skill 守门（importlib.resources 定位 + crib YAML 过 validate）。
- `tests/test_skill_benchmark.py` —— benchmark 守门（全 15 workflow case 过 validate + folder-agent 资产迁移不变量）。
- 34 skill 测试全过；1680+ 既有测试 0 回归。

## 诚实交代（非确定性）

skill 由 LLM 驱动，单次 full run 通常 14-16/16，偶发个别 case flake（每次 flake 的 case 不同，每条失效模式都有对应通用规则）。规则已覆盖观察到的所有失效模式，但无法保证每次 full run 都 100% 16/16——生成式 skill 的固有方差。

## 文件

- `orca/skills/__init__.py` + `orca/skills/create-workflow/{SKILL.md, reference/, examples/, benchmark/}`
- `orca/iface/cli/skill_cmds.py` + `orca/iface/cli/commands.py`（挂载）
- `scripts/run_skill_benchmark.py`
- `tests/{test_skills_bundle.py, test_skill_benchmark.py, iface/cli/test_skill_cmds.py}`
