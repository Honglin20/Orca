# 产物去考古化 + 写作规范固化

**日期**：2026-08-02
**Commits**：`7465231` + `e546255` + `4fcda82`（45 + 19 + 12 文件）

## 背景

审视 `nas-agent-pipeline`（普适性好）vs `kd-nas` 系列（满篇 `BLK-3/10`、`CONTRACTS §6`、"This is the KD-NAS analogue of the NAS supernet-train-script"）后发现：create-workflow skill 管了「结构/契约/拓扑/输入三档」，唯独没管「产物写给谁看、什么语气写」——合规 ≠ 好产物（kd 那种考古体 agent 完全符合旧 skill 规则）。

量化证据：`kd-train-script/SKILL.md`（233 行）含 20 处开发考古引用；同规模 `nas-search-pipeline/SKILL.md`（157 行）仅 2 处。

## 规范固化（writing-style.md §0-§9）

新建 `orca/skills/create-workflow/reference/writing-style.md`，与 `orca-workflow-contract.md` 并列（契约管「结构对不对」，writing-style 管「语气对不对」）：

- **§0 心智**：产物是产品说明书（写给用功能的人/agent），不是设计日志（写给做功能的你）。
- **§8 考古 vs 导航**：A 类考古禁（`[A-Z]+-[0-9]+` 编号 BLK/BUG/HI/U/MED/LO 含单字母前缀、`analogue of`/`迁移自`/`前身是`、`v嵌入`、`spec-review`、外部 plan·SPEC §）；B 类真实文件导航允许（CONTRACTS.md §N、docs/specs §N、workflow §N、checklist item N）。例外：设计日志、CONTRACTS.md 决策溯源、release note。
- **§9 自检**：标准正则 + 宽口径 grep（`P[0-9]|Increment [A-Z]|code-reviewer|...` 抓无连字符编号）+ `tars validate`（只校验结构，不检考古——必须靠 grep 兜底）。

同步 `create-workflow/SKILL.md`（加「产物写作规范」节 + success_criteria 验收）+ memory（跨会话兜底）。三件套禁词/正则/例外完全一致。

## 清理范围（全仓库产物层）

| 系列 | 范围 | 处置 |
|---|---|---|
| kd | kd-nas.yaml + kd-* agent.md/SKILL + _kd_scripts/*.py + templates | 删 BLK/BUG/HI/U/MED 编号 + analogue of + v 嵌入，保留 CONTRACTS §N 等导航 |
| struct | agent-struct-exploration.yaml + struct-* agents + _struct_scripts/*.py | 按新规范重做，去 plan §X/SPEC §/编号 ~140 处，功能字节级等价（git diff 核查） |
| quant + nas | 4 yaml + agent + scripts | 删 plan §P5/spec-review N7/P8/code-reviewer 等 |
| model optimizer | elastic_optimizer + pytorch-model-optimizer | P8 接口 |
| 新增 | prune-channel-sweep.yaml + agent + script | 用新规范生成的剪枝 workflow，端到端验证规范生效 |

保留：B 类导航（docs/specs §N、CONTRACTS §N）、外部 SDK 引用、chart 标签（C3a）、Rule N、`_deprecated/`（回归测试防回潮依赖，仅清注释不删文件）。

## 验证

- **prune 端到端**：用新规范从零生成 prune-channel-sweep.yaml，tars validate 0 error。
- **三轮 code-reviewer 独立 review**：含 git diff 核查 struct 功能等价（最高风险点过关）。
- **WSL tars validate 全量**：kd-nas / 4 个 quant yaml / struct workflow 全 0 error。

## 终审返工（commit 4fcda82）

终审「review 所有实现」抓到 4 项严重，全修复：

1. **prune torch API 误用**（`run_prune_sweep.py:245`）：`CustomFromMask` 类当函数用 → `custom_from_mask` 函数调用，真跑 API_OK（之前 tars validate 通过但运行时崩）。
2. **struct `{%raw%}` 误删**（`agent-struct-exploration.yaml:108`）：setup prompt 自引用 `setup.output` 的 Jinja 转义 `{%raw%}` 被当考古删 → StrictUndefined 渲染崩。恢复 + 全仓库扫描确认仅此 1 处误删。
3. **盲区 8 群组**：model optimizer 整条线 + `_deprecated/` + `_kd_scripts` 次要文件（`_device.py`/`teacher_setup.py`/`profile_onnx.py`）+ create-workflow 的 contract/examples。`_deprecated` 保留文件（回归测试依赖），仅清注释。
4. **规范三件套不自洽**：§9 宽口径 grep 只在 writing-style，SKILL/memory 丢。三处对齐（补宽口径 8 模式 + leaves off/前作/前身是 + spec_review + plan·SPEC 正则 + sentinel §N 白名单 + CONTRACTS 例外）。

## 教训

- `tars validate` 不检运行时（prune API bug 漏网）+ 不检考古（规范 §9 自陈）。引用校验补第一个盲区（见后续 commit `5b139ac`「tars validate 加引用合规校验门」）。
- 「正面样板」（nas）深挖 scripts/comments 也有考古——清理要深入 scripts，不只看表层 agent.md。
- 全程子 agent 委托（清理/review/重做/创建/修复/分析），主 agent 协调 + 精确 commit；多轮 code-reviewer 独立 review 是质量底线。
