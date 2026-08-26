# Release Note — agent prompt dev-residue lint + 洁净契约

**日期**：2026-08-05
**Commit**：`2c87e72`
**分支**：`in-session-unified-backend`

## 背景

workflow 总残留开发期无关信息（plan 节号 `§9.1`、issue breadcrumb `（I10）`、Orca 引擎源码路径
`orca/exec/env.py:91`、内部 examples 路径作论据），人工审查总漏。结构性问题：审查靠 grep
（只能抓记得的类别），不靠受众翻转通读；缺一个 deterministic 兜底。

## 改动点

### 1. validator lint（deterministic 检测器）

`orca/compile/validator.py` 新增 `_check_prompt_dev_residue(wf, result)`，与其它 `_check_*`
同级调用。对每个有物化 prompt body 的 AgentNode（folder-agent / file-agent，即 `resources_root`
已物化；**跳过 inline 短 prompt**），用 regex 扫 body 命中 5 类无歧义开发期残留：

| 类别 | 例 |
|---|---|
| plan 编号 | `plan §9.1`、`plan §N1` |
| spec/plan 节号 | `§9.1`、`§2.3`（要求 N.M 形，避免误报枚举编号） |
| issue breadcrumb | `（I10）`、`(N1`、`（B2`（中英文括号 + I/N/B 前缀 + 数字） |
| Orca 源码路径 | `orca/exec/env.py:91`、`orca/compile/validator.py`（白名单 9 子目录） |
| 内部 examples 路径 | `examples/agents/plotter/agent.md` |

命中 → `result.add_warning(...)`（**warning 不阻断**）。message 含 location + matched + category +
契约文档路径。同类别在同一节点内只报首条命中（去重，避免刷屏）。foreach body agent 同样扫描。

**operational 零误报**：`$ORCA_AGENT_RESOURCES` / `$ORCA_ARTIFACTS_DIR` / `orca.chart.render_chart`
（API 调用，无 `.py` 后缀）/ `orca/skills/...`（白名单不含 skills，agent 可合法 Read skill 资源）/
`Git Bash` / `tape` / `output_schema` / NAS block 名（`swin_window` / `cswin` 等）—— 均不命中。

**warning-not-error 的取舍**：既有 workflow 可能有残留，error 会阻断 `tars validate` 使其不可用；
warning 让残留可见、作者按契约清理，执行靠契约 + 受众翻转通读。deterministic lint 是兜底，不替代人审。

### 2. 契约 MD（随 skill 安装）

`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（新建）：

- 受众分离原则：agent.md body 是 LLM 运行时指令（WHAT to do），不是给 reviewer 的设计论证
  （WHY 属 commit / release-note / plan）。
- 禁止残留表（lint pattern 表 + 解释）+ 允许的 operational 串表。
- 测试夹具防火墙：workflow prompt 用泛化抽象（`{{ setup.output.project_root }}` / `{{ inputs.* }}`），
  测试 fixture（如 MNIST）只作 inputs 喂入，绝不写进 prompt。建 workflow「先 dry → 再 fixture」。
- 多 agent 共建洁净契约：派 coder-agent 写 agent.md 时，prompt 里带本契约禁止项。
- 审查法——受众翻转通读（替代 pattern-grep）：假设「我是只懂业务、不懂 Orca 内部的 LLM agent」，
  逐句读 body，凡对执行任务无帮助的开发上下文都 flag。grep 找记得的；受众翻转抓忘了的；两者并用。
- deterministic 兜底 + 执行流程：跑 `tars validate` + 受众翻转通读，warning 清零 + 通读无疑才算完成。

### 3. CLAUDE.md（项目根）

「代码质量底线」节加一小段「workflow agent prompt 洁净」——只引用契约，不抄全文。

### 4. create-workflow SKILL.md

`产出过程.3`（强制自校验）加一步：跑 `tars validate` + 按契约做受众翻转通读，warning 清零。

### 5. install 部署

`tars install` 用 `shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("benchmark"))`
部署随包 skill —— 默认含 `reference/` 子目录，契约 MD 自动随 skill 部署，**无需调整 install 逻辑**。

## 偏差

无。按规格交付：lint warning-not-error、跳过 inline prompt、5 类 pattern、契约 + 短引用、install 不动。

## code-reviewer 闭环

一轮闭环：0 must-fix / 3 should-fix（已全修）+ 4 nit（部分采纳）。
- 修：`plan §9.1` 截断 → 扩字符类 `[0-9INBivx][0-9A-Za-z.]*` 吞完整编号。
- 修：在 `_DEV_RESIDUE_PATTERNS` 上方加不变量注释（pattern 内必须用 `(?:...)` 非捕获组，
  防 `lastgroup → tuple 索引` 类别映射错位）。
- 修：源码路径 regex 注释解释为何白名单不含 `skills`（operational 引用）。
- 修：契约文档表格补「要求 N.M 形」说明。
- 补测：foreach body 多类别聚合 + 全覆盖矩阵 invariant（防新增 pattern 漏测 + 防捕获组破坏映射）。

## 验证

- 单测：`tests/compile/test_validator_dev_residue.py` **23 passed**（5 类 pattern 正反两面 +
  operational 误报排除 5 case + inline 跳过 3 case + foreach body 4 case + 同类别去重 + 多类别聚合
  + 全覆盖矩阵 invariant）。
- 全 compile 测试套件：**191 passed**（零回归）。
- `tars validate workflows/nas-supernet.yaml` ✓ 过 + **0 dev-residue warning**（刚清过，确认洁净）。
- 其它现有 workflow 全部 0 warning：kd-nas / nas-hp-search / agent-struct-exploration /
  nas-agent-pipeline / prune-channel-sweep / quant-bit-curve / quant-ptq-sweep / quant-qat /
  quant-sensitivity（既有 prompt 已洁净，dev-residue lint 零误报）。
