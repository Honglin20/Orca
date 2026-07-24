# 2026-07-22 P6 — NAS 系 workflow 重设计（补 KPI inputs + sink project_root + heavy 7→5 对齐 slim）

> 计划：[`docs/plans/2026-07-21-workflow-redesign.md`](../plans/2026-07-21-workflow-redesign.md) §Phase 2
> 任务派发：coder-agent P6（批3，与 P5 quant / P7 struct-kd / P8 产物目录并行）
> 配套 SPEC：[`docs/specs/workflow-input-design-principle.md`](../specs/workflow-input-design-principle.md)（input Tier A/B/C 分类 + infer-once + propagate）

## 背景

审计发现 NAS 系两 workflow 三处系统性缺陷：

1. **KPI 输入缺失**：`nas-agent-pipeline` / `nas-hp-search` 只 `required: model_path/project_root/output_dir`，关键 KPI（target_hardware / latency_constraint / max_rounds / seed）缺失——NAS 无目标时延/硬件 = 瞎搜，无预算闸门 = 烧算力无上限，无 seed = 不可复现。
2. **project_root 是 input 但属 Tier B（代码事实）**：违反 input 设计原则 §1——project_root 能从 model_path 向上走 grep 出来，应下沉给 setup 节点 infer-once + 向后传，而非让用户手填（手填会与代码漂移）。
3. **heavy 7 节点过度拆分**：`viz_describe`（独立 viz 节点）+ `evaluator`（内联 LLM 选架构）+ `viz_finalize`（终态图）三节点职责零散、与 slim 的 5 节点确定性护栏不一致；LLM evaluator 还引入非确定性选择。

## 改动

### A. 输入契约对齐（两 yaml 同形）

| 操作 | 字段 | Tier | 透传 |
|---|---|---|---|
| 新增 [ask] | `target_hardware` (cuda\|npu\|cpu) | A | 写 supernet_summary.md 备查（device 路径不破坏，沿用既有 `--device auto`） |
| 新增 [ask] | `latency_constraint` (ms) | A | 经 `nas-search-pipeline` agent 透传到 search_config.yaml `latency_constraint` 字段（NSGA-II 据此剪枝过时延候选） |
| 新增 [ask] | `max_rounds` (代数) | A | 同 agent 透传到 search_config.yaml `num_generations`（搜索预算闸门） |
| 新增 [ask] | `seed` (默认 0) | A | 写 supernet_summary.md；全 workflow 必填（input 原则 §6 checklist） |
| 下沉 [infer] | `project_root` | B | 从 inputs 删除，改为 setup 节点 infer-once + output_schema 暴露 + 写 supernet_summary.md |
| 保留 [default] | `output_dir` | C | 暂留 input（Phase 4-A 将 sink 到 `$ORCA_ARTIFACTS_DIR`） |

### B. project_root infer-once + propagate 链

抄黄金模板 `workflows/agent-struct-exploration.yaml::family_detect`（SPEC §4）：setup 节点一次推断、写进 `output_schema`、下游用 Jinja 取——**严禁**「每个 agent 各自重新自找」。

链路（UNBROKEN，code-reviewer 已验证）：
1. `pytorch-model-optimizer` / `elastic_optimizer` agent.md 新增「推断 project_root」步骤：从 `model_path` 所在目录起向上逐级找第一个含 `train.py` / `pyproject.toml` / `.git` 的祖先目录；走到 `/` 仍找不到 → 取 model_path 的 dirname + 输出标注 `(low-confidence: ...)` 后缀（不阻塞但显式标注）。**不许**用 `pwd` / `git rev-parse` / 最近编辑文件推断；**不许**留空或编造。
2. setup 节点 emit 严格 JSON（含 `project_root` 字段）→ `output_schema` 校验。
3. setup 节点同时写 `Source Project: <推断绝对路径>` 到 `supernet_summary.md`。
4. 下游 `supernet-train-script` / `nas-search-pipeline` agent 仍从 `supernet_summary.md` 的 `Source Project:` 行读 project_root（既有 pattern，零改动）——单一真相源保完整。

### C. heavy 7→5 节点对齐 slim（同构 + 同确定性护栏）

| heavy（旧 7 节点） | heavy（新 5 节点） | 说明 |
|---|---|---|
| model_optimizer | **model_optimizer**（保留） | 加 output_schema（暴露 project_root + KPI + model_type enum） |
| viz_describe | ❌ 删 | push_describe.py 内联进 pytorch-model-optimizer/agent.md（抄 slim elastic_optimizer 模式） |
| train_script_gen | **train_script_gen**（保留） | — |
| search_pipeline_gen | **search_pipeline_gen**（保留） | — |
| train_runner | **train_runner**（保留） | 加 output_schema（抄 slim runner `search_records minimum:1`）防假执行 |
| evaluator (LLM) | ❌ 删 | 内联 LLM → 替换为脚本化 `select` 节点（零 LLM） |
| viz_finalize | ❌ 删 | push_pareto_final/push_funnel 已在 nas-select/scripts/select_and_report.py:215-216 内联 |
| — | **select**（新） | 复用 slim `nas-select` agent；route → $end |

最终拓扑与 slim 完全同构（`model_optimizer → train_script_gen → search_pipeline_gen → train_runner → select → $end`），唯一差异：heavy 用 `pytorch-model-optimizer`（重量级：展平+optimize_rules+block 维度），slim 用 `elastic_optimizer`（轻量：只读不展平）。

### D. output_schema 与 SKILL Step 4 早退路径对齐（code-reviewer 🔴-1 闭环）

`pytorch-model-optimizer/SKILL.md` Step 4.6 允许「macro-architecture 不可分类 → 停下、保留 flat/optimize artifacts、不继续 Step 5+」。新增 output_schema 时若忽略此分支，agent 早退时 emit 不出合法 JSON → output_schema_mismatch 泥潭。修法三层：

1. **schema 层**：两 yaml `model_optimizer.output_schema.properties.model_type` 加 `enum: [cnn, hierarchical_transformer, isotropic_transformer, unsupported]`——把 `unsupported` 哨兵变成一等公民。
2. **路由层**：两 yaml `model_optimizer.routes` 改条件路由：
   ```yaml
   routes:
     - when: "model_optimizer.output.model_type != 'unsupported'"
       to: train_script_gen
     - to: $end   # 兜底：unsupported → 短路 $end，不烧训练/搜索算力
   ```
3. **prompt 层**：两 setup agent.md 的「输出」段加显式「早退路径」JSON 分支——model_type=`unsupported`、artifacts 只列实际生成的、绝不伪造 supernet.py、失败原因写 stderr + 分类报告（不进 JSON）。

slim 的 elastic_optimizer 没有 SKILL Step 4 分类门，但 supernet 自测反复失败时同理（agent.md 「修到过或把失败原因写进输出」之前 shape 不明）——同批加 `unsupported` 早退 JSON 分支。

### E. latency_estimator 构造函数 forcing function

`workflows/agents/nas-search-pipeline/references/supernet_workflow_examples/latency_estimator.py:18-23` 构造函数默认 `device="cpu"` → 改强制传参（`device: str | torch.device`，无默认）。理由：默认 "cpu" 是隐患——NAS 的 latency_measurement 在错设备上测得无意义数；应强制由调用方（搜索 worker / 框架）显式注入。`__main__` smoke-test CLI 的 `--device default="cpu"` 保留（测试便利默认，非生产路径）。

### F. dataset 缺失 fail loud（暂不哨兵）

`nas-search-pipeline/agent.md` 加 Step 2 强制段：search_config.yaml 的 `evaluator_cfg.data_dir` 与生成的 evaluator.py 数据路径必须**从用户项目代码读到**（grep `data_dir` / `DataLoader` / `ArgumentParser(--data` / `root=`）；读不到 → fail loud：
- **绝不**留 `<dataset_root>` / `/path/to/dataset` / `./data` 占位字符串假装配置完成。
- **绝不**用 `torch.randn` 造假 DataLoader（搜索 evaluator 会拿它当真，跑出无意义的 acc 排序）。
- **绝不**静默默认任何猜测路径。
- 正确动作：`data_dir` 写 `null` + stderr 显式错误 + 非零退出。

（哨兵机制 Phase 0-b 全量落地后改为「读不到→哨兵问用户」；当前先 fail loud。）

### G. 顺手更新 docs

- `docs/workflows/nas-agent-pipeline.md`：整篇对齐新 5 节点拓扑（header / §1.2 ASCII DAG / §1.3 inputs-outputs / §3.2 / §4.3 / §5.2 / §6 / §A.3）。
- `docs/workflows/nas-hp-search.md`：§1.3 inputs 表 + §1.4 example 命令对齐 6 输入。
- `docs/workflows/README.md:135`：删「LLM 评估选择」描述。

## 文件改动清单（绝对路径）

- `workflows/nas-agent-pipeline.yaml`（heavy：inputs 改 + 5 节点 + 2 处 output_schema + 条件路由 + model_type enum）
- `workflows/nas-hp-search.yaml`（slim：inputs 改 + model_optimizer 加 output_schema + 条件路由 + model_type enum）
- `workflows/agents/pytorch-model-optimizer/agent.md`（drop inputs.project_root / 推断 project_root / KPI inputs / push_describe 内联 / 早退 JSON / 严格 JSON output）
- `workflows/agents/pytorch-model-optimizer/scripts/push_describe.py`（**新建**，从 elastic_optimizer/scripts/push_describe.py 复制——DRY 推迟到 Phase 4）
- `workflows/agents/elastic_optimizer/agent.md`（同 pytorch-model-optimizer 改动，slim 版）
- `workflows/agents/nas-search-pipeline/agent.md`（KPI 透传 search_config.yaml / dataset fail loud）
- `workflows/agents/nas-search-pipeline/references/supernet_workflow_examples/latency_estimator.py`（构造函数 device 无默认）
- `docs/workflows/nas-agent-pipeline.md`（5 节点拓扑对齐）
- `docs/workflows/nas-hp-search.md`（inputs 表 + example 对齐）
- `docs/workflows/README.md`（删 LLM 评估描述）

## 测试策略说明

P6 改动对象是 workflow YAML + agent prompt + 参考模板（声明式 spec，由引擎执行），非传统可单测代码：

- **结构校验**：`tars validate workflows/nas-{agent-pipeline,hp-search}.yaml` 双 0 error（含 `when:` route 兜底位置 + output_schema 结构 + Jinja2 StrictUndefined 模板字段覆盖）。
- **Jinja 渲染**：6 个 NAS agent.md 用 sample inputs 跑 `jinja2.Environment(undefined=StrictUndefined).render(**ctx)` 全 OK，含新早退 JSON 模板（无悬空 `{{ inputs.* }}`）。
- **py_compile**：`latency_estimator.py` + `push_describe.py` 双通过。
- **E2E headless run**：计划 §6 统一 harness（TARS-skill 驱动 opencode+deepseek-v4-flash）在 P4 之后才建，P6 不在该批次；典型 NAS run 是分钟-小时级真训练+搜索，超出单测范围。改为由 code-reviewer 做 spec/contract 审计 + release note 显式标记「真机 in-session E2E 待统一批次」（CLAUDE.md 状态文档规则 + 既有量化 workflow 同款惯例）。

## code-reviewer 闭环

两轮：一审发现 1 🔴（output_schema vs SKILL Step 4 早退路径契约断裂）+ 3 🟡（slim 同形隐患 / heavy doc 漂移 / best-effort vs strict JSON 边界）+ 5 🟢（Phase 4 deferred）。全部 🔴 + 🟡 已修（详见 §D + §G + agent.md 分隔注释）；🟢 全部 Phase 4 接收（latency_estimator supernet-to-CPU quirk 是 pre-existing 且 brief 明示 don't-break / push 脚本 docstring 漂移等 DRY 收口时一并改 / output_dir 待 4-A / 数值 input 类型待 input schema 放开 number 类型）。

## 偏差

无。逐字对齐 brief 必改项 1-5 + 硬约束（slim 黄金模板不动 device / 不碰 quant-struct-kd-P4 文件 / `tars validate` 0 error / git 只 add NAS 文件）。

## Commit SHA

- `42e4a06` —— feat(workflows): P6 NAS 系重设计（本 release note 所述全部改动）

## 验证结果

- `tars validate workflows/nas-agent-pipeline.yaml`：✓ 校验通过（0 error）
- `tars validate workflows/nas-hp-search.yaml`：✓ 校验通过（0 error）
- Jinja2 StrictUndefined 渲染 6 个 NAS agent.md（pytorch-model-optimizer / elastic_optimizer / supernet-train-script / nas-search-pipeline / nas-train-runner / nas-select）：sample inputs 下全 OK，含新早退 JSON 模板（无悬空 `{{ inputs.* }}`）。
- `python3 -c "py_compile.compile(...)"`：`latency_estimator.py` + `push_describe.py` 双通过。
- Orca router.py 源码核对：`when:` route 支持 `<node>.output.<field>` Jinja 表达式求值，兜底 route 必须最后——两 yaml `model_optimizer.routes` 排序合规（`when` route 在前，`- to: $end` 兜底在后）。
- E2E headless run：deferred 至批 3 后统一 TARS-skill harness（plan §6）；P6 改动对象是声明式 workflow spec + agent prompt + 参考模板，无传统单测范围。
