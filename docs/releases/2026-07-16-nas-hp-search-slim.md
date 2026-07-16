# 2026-07-16 — nas-hp-search：轻量 NAS 超参搜索流水线（slim）

## 背景

现有 `workflows/nas-agent-pipeline.yaml`（7 节点）跑通但重：model_optimizer 85min（展平/
优化规则/分类/生成 supernet/精修）、evaluator 22min（LLM）。本交付提供一个**轻量版**
`nas-hp-search`：model_optimizer 改 slim（只判 Elastic 参数，不展平）、evaluator 脚本化、
viz 流入生成脚本（不要独立 viz 节点）。验证跑通后合回原 workflow。

## 做了什么

### 新 workflow：`workflows/nas-hp-search.yaml`（5 节点，线性）

`model_optimizer → train_script_gen → search_pipeline_gen → runner → select`，
`outputs.result = select.output`。inputs 同原版（model_path/project_root/output_dir）。
executor=opencode，model=deepseek/deepseek-v4-pro。

**节点名裁决（Rule 7）**：node-1 命名 `model_optimizer`（不是 `elastic_optimizer`），
agent 指向新 slim folder-agent `elastic_optimizer`。原因：复用 agent
`supernet-train-script/agent.md` body 硬编码 `{{ model_optimizer.output }}`，而 Orca
`prompt`+`agent` 在节点上互斥（`parser.py:108`，不能 inline 覆盖），fork 共享 agent 违反
DRY。命名对齐硬契约是唯一架构合规解，且与既有重流水线同形（node `model_optimizer` →
agent `pytorch-model-optimizer`，已解耦命名）。node 2-5（train_script_gen /
search_pipeline_gen / runner / select）满足其余复用 agent 的硬编码引用。

### 1. `workflows/agents/elastic_optimizer/`（**新 slim folder-agent**）

- `agent.md`：slim 指令——读 model（不展平）+ 速查 + 模板 → 判 Elastic 参数 → 仿模板生成
  `supernet.py`（`python supernet.py` 自测前向）+ `supernet_summary.md` → 末尾推 C1/C2。
  **绝不**读 optimize_rules/supernet_specs/inspect_examples（slim 边界）。
- `references/elastic_cheatsheet.md`：Elastic 原语 API 速查（ElasticConv2d / ElasticLinear /
  ElasticBatchNorm2d / ChoiceLayer 构造 + sample_config + get_active_subnet + ArchConfig /
  SearchSpace 字段）。逐条比对 `nas-agent` 源码，零偏差。
- `references/supernet_template.py`：基于 `demo_run/supernet.py` 提炼的最小 CNN supernet
  模板（3 stages: 16/32/64, depth (1,2), kernel (3,5), ChoiceLayer{conv,res} + ElasticLinear
  head）。**可独立运行**，`python ...supernet_template.py` 5 trial 前向/子网一致性 diff=0.00e+00。
- `scripts/push_describe.py`：从 nas-viz 复制（C1/C2）；修 `_err` 兜底 re-raise bug。

### 2. `supernet-train-script` checklist 加 `[MAJOR] 28`（**复用 skill，改 checklist**）

`01_training.md` 新增 item 28：生成的 `train_supernet.py` 须内联 `_push_chart()`——
accumulate 进程内全序列 + 每次 push 全序列（同 label+title 是替换语义，推单点会擦历史），
label/title 精确对齐 `tail_metrics.py` C3a/C3b（`nas/training` + `Training Loss` /
`Validation Metric`）保 refresh-idempotent；`render_chart is None` 时 no-op（Orca-agnostic），
推图异常 try/except 不 crash 训练。附 reference sketch 给 agent 仿写。

### 3. `search_pipeline_gen` / `runner`（**复用**，不改）

分别复用 `nas-search-pipeline`（body 引用 `{{ train_script_gen.output }}` ✓）与
`nas-train-runner`（`{{ search_pipeline_gen.output }}` ✓）。训练图由 train_supernet.py
内联 live 推，搜索图由 tail_metrics `--mode search` 推。

### 4. `workflows/agents/nas-select/`（**新脚本化 folder-agent**，替代 LLM evaluator）

- `agent.md`：跑脚本 + 回 stdout（tools 仅 `[bash, read]`）。
- `scripts/select_and_report.py`：`subprocess` 调 `nas-select-architecture`（`sys.executable
  -m`，`cwd=<output_dir>` 保 `import supernet`）→ 读 fresh `selection_summary.json` + 
  `search.jsonl` 模板填空 `final_report.md`（best acc/latency、pareto 数、选中 arch）→
  subprocess 推 C5/C6。**零 LLM**。fail loud：select CLI 失败 → rc 传播 + 报告 ⚠ 段 +
  不读 stale summary（SELECTED=0）。
- `scripts/push_pareto_final.py` + `push_funnel.py`：从 nas-viz 复制（C5/C6）。

### 附带修复：`.gitignore`

`references/` → `/references/`（锚定根目录）。原意忽略外部只读 `<repo-root>/references/nas/`，
但 bare `references/` 误伤 `workflows/agents/<agent>/references/`（folder-agent 核心 skill
资源——速查/模板/checklist），导致 slim agent 交付物无法提交。锚定后两者各得其所。

## 偏离计划

- node-1 命名 `model_optimizer`（非用户字面的 `elastic_optimizer`）——见上「节点名裁决」。
- 未一并修 nas-viz 的 `_err` bug（同款 pre-existing，属重流水线文件，超 slim scope；已登记 follow-up）。
- 未统一 viz 脚本的 Orca 无关范式（sys.exit(2) import-or-die vs item 28 的 render_chart=None 守卫）——🟡 follow-up，production 路径（env 齐备）不受影响。

## 验证

- `tars validate workflows/nas-hp-search.yaml` → **0 error**；`load_workflow` 5 agent 全
  resolve 为 folder-agent，resources_root 正确。
- compile 期校验**会**扫 materialized agent.md body 模板（`_resolve_agents` 物化在
  `validate_workflow` 前），故 0 error 已浅校验 body 的 `{{ node.output }}` 根引用全合法
  （字段级/上游完成度归 Phase D 端到端）。
- `python .../supernet_template.py` → 5 trial 一致性 diff=0.00e+00，`SearchSpace.validate()`=True。
- `select_and_report.py` 端到端（demo_run fixture 复制到 /tmp）：EXIT=0、SELECTED=3、
  `objectives`（非 stale `objs`）正确渲染到 final_report.md；C5/C6 sidecar 在无 ORCA env
  下 stderr loud 但不阻断（EXIT 仍 0）。
- D-2 验证：bogus config 触发 select 失败 + stale summary 在场 → SELECTED=0（非 stale 1）、
  EXIT=1、报告 ⚠ FAILED 段。
- code-reviewer impl + coverage 两轮：🔴 全修（`objs`→`objectives` 键、selected 段复用全局
  kind、cwd 守卫、stale summary mask、`_err` re-raise）；🟡 按 scope 裁决（DRY parity 注释、
  item 28 reference sketch 已加；范式统一 + nas-search adapter 证据留 Phase D）。

## 上下文对比（slim vs 重 model_optimizer，定性）

| 维度 | 重 pytorch-model-optimizer | slim elastic_optimizer |
|------|---------------------------|------------------------|
| 读 SKILL + workflow | 7 步 workflow + 多 checklist | 无（agent.md body 自含） |
| optimize_rules/ | cv/transformer/telecom ≈15 文件 | **不读** |
| supernet_specs/ | 3 model type × (spec + search_space.py) | **不读** |
| inspect_supernet_examples/ | 3 范例 | **不读** |
| 模型展平 | 生成 `_flat.py` | 不展平（只读） |
| 实际读 | 模型 + 上述全部 | 模型 + 速查(1) + 模板(1) |

→ slim 上下文从「数十文件」降到「3 文件」，是 model_optimizer 85min → 显著加速的主因。

## Commit

`a5dd2cc`（branch `in-session-unified-backend`）。

## follow-up

- 端到端真机验证（Phase D）：跑通 nas-hp-search 全链路（含 train_supernet.py 内联推图），
  证明 slim agent 生成的 supernet.py 能被 nas-search 消费。
- nas-viz `_err` 同款 bug（重流水线文件）择机修。
- viz 脚本 Orca 无关范式统一（sys.exit(2) → render_chart=None 守卫）。
- 复用 agent（supernet-train-script/nas-search-pipeline/nas-train-runner/nas-viz/
  pytorch-model-optimizer）的其余源文件仍为 untracked WIP，用户择机整包提交。
