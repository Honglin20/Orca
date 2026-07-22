# Stage 3：统一 headless TARS-SKILL E2E（禁 CLI 驱动 workflow）

> 计划：[2026-07-22-stage3-headless-tars-e2e](../plans/2026-07-22-stage3-headless-tars-e2e.md)。来源：redesign plan §6。

## 做了什么

建了 `tests/e2e_redesign/` 统一 headless TARS E2E harness，**经 TARS skill 路径**（`orca <wf> --inputs`
+ `orca next --run-id`，复用 `tests/spike_ask_user/` 基建）驱动 8 workflow，**禁用** `orca run` / 手搓
next 循环。三层验证（受限于本环境无真模型/数据集/GPU）：

1. **静态契约闸**（`contract.py`，8 workflow × 7 check = 64 parametrized）：inputs 解析 / 无
   `{{ inputs.X }}` 残留引用 / output_schema 链不破（`{{ X.output.Y }}` 逐字校验）/ device+seed input
   在 / chart 推图标签（axis-bearing 必传 x_label+y_label+caption，table 必传 caption）/ 造假扫描
   （AST 感知：fake_data/dummy_calib 零容忍；torch.randn 仅在非 smoke/dummy/proxy 上下文才 finding）
   / 严禁造假 prohibition 正向存在。
2. **headless DAG walk**（`tars_harness.walk_dag` + `schema_faker`）：用 schema_faker 合成最小合规 JSON
   喂 `orca next`，单节点 quant×4 走到 `done:true`；多节点（nas×2 / struct / kd）bootstrap + 首跳。
3. **哨兵路径 E2E**（`tars_harness.sentinel_e2e_run`）：ptq-sweeper spawn→哨兵→resume→真实 output→
   `done:true`；断言 task_id 复用、哨兵不进 `--output`、MAX_ASK 兜底 fail loud、真实 output 无造假。

契约闸就地捕获并修了 **6 个 P9 label 遗漏** + **1 个 P9b 真 bug**（见下）。

## 发现的契约违例 + 修复

### A. 真 bug（就地修 + commit）

1. **agent-struct-exploration setup 节点 prompt 自引用渲染崩**（P9b 回归）：setup 节点 prompt 的
   说明文字里写了 `{{ setup.output.struct_scripts_dir }}`（向读者展示下游怎么取该字段），但 Jinja2
   会渲染它——setup 自己的 output 在 bootstrap 时还不存在 → `'setup' is undefined` render_error。
   修：`{% raw %}...{% endraw %}` 转义（保留文档语义，不渲染）。**静态契约闸 ``check_output_schema_chain``
   没抓到**（不是断链——字段在 schema 里；是**自引用**），是动态 bootstrap E2E 抓到的——证明动态层
   必要。
2. **6 处 chart label 缺失**（P1/Stage4 viz 遗漏；契约闸 ``check_chart_labels`` 抓的）：
   - `qat-trainer/run_qat.py` recovery bar 缺 `x_label`（axis-bearing chart 缺轴标签——真违例）。
   - `ptq-sweeper/run_ptq_sweep.py` `_push_table` 缺 caption（与 qat/bit-curve 同款 helper 不一致——DRY 漂移）。
   - `pytorch-model-optimizer/push_describe.py:248` 主表缺 caption（elastic_optimizer 版有、pytorch 版
     漏——copy-paste 漂移）。
   - 三处 error/诊断 table 缺 caption（elastic_optimizer / pytorch-model-optimizer `_err` helper +
     nas-train-runner `tail_metrics` schema-error fallback）。

### B. 设计边界（写进报告，不修）

- **dead-code `nas-viz/scripts/`**：4 处缺标签（含 scatter/bar），但 nas-viz 不在任何 8 workflow 的
  active path（P6 heavy 7→5 已删 viz_describe/viz_finalize）。CURRENT.md 已登记「nas-viz/scripts/ 死代码
  待 DRY」。契约闸 scope 排除之（仅检 active-path 脚本）。
- **kd-nas bootstrap/walk E2E 跳过**：orca 允许每 wf 仅一个活跃 run；用户既有 `kd-nas-20260720-230334`
  （2 天前，running）阻塞新 bootstrap。按任务硬约束**不 stop 用户 run**，kd-nas 的动态 E2E skip
  （elapsed>120s 判定用户既有）。静态契约 + tars validate 仍全过（不依赖活跃 run）。

## 全量真模型 run 是否可行

**不可行**（预期）。本环境无真模型/数据集/GPU——全量真 run（烧卡跑 PTQ/NAS/KD）是用户手动测的事。
本 Stage 落在可行边界：结构/契约（静态，64 测试全过）+ DAG walk（mock 合成产出，单节点到 done:true、
多节点首跳）+ 哨兵路径（mock 子 agent 闭环）。这三层把「不经 TARS / 造假兜底 / 哨兵泄漏 / 缺标签 /
schema 链破 / input 残留引用」全部卡死在 CI 层。

## 验证结果

- `tests/e2e_redesign/`：**81 passed, 2 skipped**（kd-nas 受用户活跃 run 阻塞 skip）。
  - 静态契约 64 全过；单节点 quant×4 walk 到 done:true；多节点 3 个（nas×2 + struct）bootstrap+首跳；
    8-workflow bootstrap 冒烟（7 过 + kd-nas skip）；哨兵 3 测试（闭环 + 造假 sanity + MAX_ASK 兜底）。
- 回归：`tests/workflows/` + `tests/compile/` 208 passed（label/agent-struct fix 无回归）；
  `tests/spike_ask_user/` 38 passed（复用基建无回归）；`tars validate` agent-struct + kd-nas 0 error。
- code-reviewer 两轮闭环（impl + coverage）：0 🔴，5 🟡 + 若干 🟢 全修或登记。

## 关键设计裁定（Rule 7）

- **造假扫描分层**（不全量 grep torch.randn）：spike 的 `looks_fabricated` 针对 agent **output**，
  直接套**源码**产生大量误报（smoke generator / KD proxy dataset / ONNX dummy input / docstring /
  prohibition 段落都合法含 torch.randn）。改为：fake_data/dummy_calib 零容忍 + torch.randn 仅 AST 非
  legit-context 才 finding + prohibition **正向存在** check。零误报优先，false-negative 风险 = 造假者
  把函数命名为 `materialize_*`（自证 smoke/proxy 语义，by-design）。
- **chart 标签分级**：axis-bearing（line/bar/scatter/...）必传 x_label+y_label+caption；table 仅 caption
  （无轴）。table 的 x_label/y_label 强求是无意义的形式主义。
- **walk_dag 多节点不 raise**：路由条件依赖真数据时 next 报错属预期；catch 成 `result.error` + 记
  `node_sequence`（证链前段不破），不 raise——但 **DAGStallError**（引擎不变式违反：done=False 且无
  next node）独立类型，fail loud 冒出不吞。

## Commit

- Stage 3 harness + 契约闸 + label bug fix + agent-struct 自引用 fix：<SHA>

## 遗留 / Follow-up

- 全量真模型 headless E2E（opencode + deepseek-v4-flash + TARS skill 端到端，见 plan §6 原始设想）——
  需真模型/数据集，用户手动测。
- kd-nas 动态 E2E（用户 stop 旧 run 后可补跑）。
- nas-viz/scripts/ 死代码 DRY 清理（已登记 CURRENT）。
- 多节点 workflow 的 walk「中段」覆盖（需 route-aware schema faker 或真模型，当前只证入口链）。
