# Release: P4b agent.md 全量接入 ask-user 哨兵（缺 Tier B 必填 → 问用户而非 fail loud）

**日期**: 2026-07-22
**SPEC**: [`docs/specs/agent-ask-user-sentinel.md`](../specs/agent-ask-user-sentinel.md) §1（schema）+ §3（agent.md 契约模板）
**范围**: 只改 6 个含 Tier B 必填项的 agent.md——零 yaml / 零脚本 / 零 `orca/` / 零 SKILL.md 改动
  （P4 TARS skill 哨兵检测 + SendMessage 恢复 + MAX_ASK=3 兜底已落地激活）。
**Commit**: `<待填>`

---

## 结论

把 P5/P6/P7 落地的「缺 Tier B 必填 → fail loud（exit 非 0）」**全量升级**为「缺 Tier B 必填 → 以最终消息返回轻量 ask-user 哨兵 JSON」。TARS skill（P4 已激活）在调 `orca next` 之前 strict 识别哨兵魔键 → 问用户 → SendMessage / Task(task_id) 恢复**同一**子 agent（上下文不丢）→ 拿真实产出 → 才喂 `orca next`。**哨兵绝不进 `orca next`**，引擎零改动。

至此根因 B（IN-SESSION 无法问用户）的 agent.md 契约侧补齐——子 agent 缺必填项时有了「向上问」的可靠通道，不再被迫二选一（造假 vs fail loud 放弃）。

## 改了什么

### 6 个 agent.md，每个加同构「## 缺失必填输入时（严禁造假）—— ask-user 哨兵」段

| agent.md | Tier B 项（缺失走哨兵） | 保留的 SDK 合法 fallback / 确定性 fail loud |
|---|---|---|
| `ptq-sweeper` | 校准 loader、评估 loader | eval_fn 缺 → teacher-student mse（SDK 默认，非造假，**不在哨兵范围**） |
| `sensitivity-analyzer` | 校准 loader（所有 method）；eval_fn（**仅** `method ∈ {ptq_binary_sensitivity, mix_precision_search}`） | method ∈ {mse, layer_stats} 不需 eval_fn |
| `qat-trainer` | 训练 loader、评估 loader（所有 scheme）；校准 loader（**仅** `scheme ∈ {duquantpp, both}`） | eval_fn 缺 → teacher-student mse（**不在哨兵范围**）；`scheme=rtn` 不需 calib |
| `bit-curve-searcher` | 校准 loader、评估 loader | eval_fn 缺 → teacher-student mse（**不在哨兵范围**） |
| `kd-setup` | Step 6 抽用户 train.py 的 dataloader / loss / optimizer / scheduler 结构片段 | Step 1 build_fn / dummy_input / project_root 探测走「低置信」标注 fallback（不阻塞）；Step 3/4/5 脚本非零退出仍确定性 fail loud（不是 Tier B） |
| `nas-search-pipeline` | dataset 路径（`evaluator_cfg.data_dir`，grep 用户代码可得） | 占位字符串 / `torch.randn` 假 DataLoader 严禁（保留禁令） |

**每个段的同构结构**（SPEC §3 四要素）：
1. 引用 SPEC + TARS skill 机制（strict 识别 + SendMessage 恢复 + MAX_ASK=3）+「哨兵不进 `orca next`」（output_schema `additionalProperties:false` 会拒）
2. 列本节点 Tier B 项（每个 agent 按自身实际可读项裁剪，不抄全集）
3. 找到 → 写进 adapter.py / search_config.yaml 向后传
4. 读代码无果 → **不要**造假（保留原禁令：`torch.randn` / 复用 calib 当 eval / 复用 train 当 eval / 静默默认空 loader）+ 返回哨兵 JSON（2 必填键 `_orca_ask_user` + `_sentinel:"orca_ask_user_v1"`，可选 `options` / `context`）
5. 会被恢复（不是重跑），不要重做已完成的工作
6. 用户也答不出 → `{"_status":"fail_loud","reason":"..."}`

### 配套 inline 改动（DRY：单一真相源在 sentinel 段）

- 「## 输入」段相关 Tier B 字段（calib/train/eval loader / eval_fn / dataset）的 `**Tier B 契约**` 行：「找不到 → **fail loud**」一律改「找不到 → **返回 ask-user 哨兵**（见下文『缺失必填输入时』段）」。
- 「## 执行流程」段 adapter.py 生成步骤的「Tier B 获取三步」第 ② 步：「fail loud（adapter 直接 raise）」改「**不写 adapter / 不调脚本**，以最终消息返回 ask-user 哨兵（**不**让 adapter raise、**不** exit 非 0）」。
- 顺手修两处旧「**绝不**通勤 `torch.randn` 造假数据」错字 →「**绝不通过**」（ptq-sweeper / bit-curve-searcher；语义靠上下文猜中，但「通勤」=commute 不专业）。

## 关键设计决策（Rule 7 surface）

### 1. eval_fn 在 quant 三 workflow（ptq-sweep/qat/bit-curve）显式 OUT of sentinel scope

这三个 workflow 的 eval_fn 缺失时，脚本自动 fallback 到 `build_teacher_student_eval_fn`（teacher-student mse）+ stderr WARN「精度仅自洽性参考」。teacher-student mse 是 **SDK 合法默认**，有自洽性诊断价值，**非造假**——不触发哨兵。三文件的 sentinel 段底部显式脚注说明此排除。hard constraint 把 eval_fn 列为「Tier B」是泛指；具体到这三 workflow，eval_fn 缺失有合法 fallback，故排除。

### 2. sensitivity-analyzer 的 eval_fn 反而 IN scope（仅两个 method）

sensitivity-analyzer 的 `ptq_binary_sensitivity` / `mix_precision_search` 两 method 真依赖业务 eval_fn 做有意义的敏感度搜索——**无 SDK 合法 fallback**。故这两个 method 下 eval_fn 缺失走哨兵；其余 method（mse / layer_stats）不需 eval_fn。hard constraint 把 sensitivity-analyzer 排除在「eval_fn OUT of scope」名单外（只列了 ptq-sweep/qat/bit-curve），故此处理由充分。

### 3. kd-setup 的禁令做语义改写（非直译 torch.randn/复用）

kd-setup 的 Tier B 不是 quant loader dotted-path，而是从用户 train.py 抽取的结构片段（dataloader 构造 / loss callable / optimizer / scheduler）。原直译禁令（`torch.randn` / 复用 train 当 eval）语义不适用，故改写为「编造默认 DataLoader / 写空 loss / 假 optimizer / 静默套模板跳过缺失项」——语义等价（都是「不许造假」），字面不符通用 grep。后续 reviewer 勿用 `grep torch.randn` 判 kd-setup 漏写禁令。

### 4. struct setup 不在本次范围（yaml 嵌入，不碰 yaml 硬约束）

`agent-struct-exploration.yaml` 的 setup 节点 prompt 是**内联在 yaml**里（没单独 agent.md 文件）。hard constraint「不碰 yaml」→ struct setup 的 Tier B（build_fn / dummy_input / project_root）哨兵化留待后续（且这三项已有「低置信」标注 fallback，不阻塞）。本次只覆盖有独立 agent.md 的 setup（kd-setup）。

### 5. 工作树卫生（commit 边界）

工作树里同时有 P8 WIP（`orca/exec/` + `orca/iface/in_session/cli.py` 注入 `ORCA_ARTIFACTS_DIR`，与 P4b 无关）。本次 commit **只 `git add` 6 个 agent.md**，P8 WIP 留工作树另起 commit。

## 验收

- **`tars validate` 0 error**：8 个 workflow（quant×4 + struct + kd + nas×2）全部 `✓ 校验通过`。
- **哨兵 schema 严格一致**：6 文件示例 JSON 全含两必填键 `_orca_ask_user` + `_sentinel:"orca_ask_user_v1"`，魔键 literal 无 typo；spike `tests/spike_ask_user/sentinel.py::is_sentinel` 的 strict 识别（括号配平 + JSON parse + 魔键校验）与 agent.md schema 对齐。
- **spec §3 四要素齐**：6 文件每个 sentinel 段都有 不造假 / 返回哨兵 / 会被恢复 / fail_loud fallback。
- **章节定位一致**：6 文件 sentinel 段全部紧贴 `## 输出` 之前（locatability）。
- **DRY**：inline 输入列表 + adapter 生成步骤全部回指 sentinel 段（「见下文『缺失必填输入时』段」），不重复 schema。
- **fail loud 边界零越界**：确定性脚本错误（teacher_train / profile_onnx / AST / ONNX 导出 / latency_provider 加载失败）仍 fail loud；只有「读用户代码无果」走哨兵。
- **Tier A / Tier C 未越界**：6 文件 sentinel 段只列 Tier B；`model_path` / `train_command` / `target_hardware` / `latency_constraint` / `max_rounds` / `seed` 等 [ask] / [advanced] input 一律不动。
- **既有测试无回归**：`tests/compile/` 127 passed；`tests/workflows/` 61 passed；`tests/spike_ask_user/` 39 passed（机制层，含 strict 哨兵识别 + SendMessage 恢复 + MAX_ASK=3 兜底，未触）。1 failed = `test_real_orca_two_node_closed_loop` 是 **P8 WIP 副作用**（`orca_cli.py:87 NameError: _resolve_default_artifacts_dir`，P8 Phase 4-A artifacts_dir 注入未完工），与 P4b 无关。
- **code-reviewer 闭环**：1 轮 review，0 🔴 + 2 🟡 + 4 🟢 全处理（🟡 #1 工作树卫生 → commit 边界；🟡 #2 生产 agent E2E 覆盖 → 登记为批 3 统一 headless TARS-SKILL E2E harness 范围，非 P4b 阻塞；🟢 全修：错字 / scheme=rtn inline 含糊 / nas-search blockquote schema 拒因补齐 / kd-setup 禁令语义改写登记）。

## 范围外（登记给后续）

- **生产 agent 哨兵路径零直接 E2E 覆盖**：SPEC §5 验收目前只在 spike data-finder 上做了（40 测试含 2 真 claude integration），6 生产 agent.md 的「读代码无果 → 返哨兵」分支无测试驱动。登记给 CURRENT 待办里的「批 3 后统一 headless TARS-SKILL E2E harness」——届时按 SPEC §5 各挑一个 agent 做最小 headless E2E（如 calib loader 缺失 → 哨兵 → SendMessage 恢复 → 真实 output）。
- **struct setup 哨兵化**：yaml 内联 prompt，需 yaml 改动，留待解除「不碰 yaml」约束后做。
- **opencode in-session E2E**：opencode 路径标 experimental（task_id 续跑已验证，跨 session 续跑 v1 不支持），完整 E2E 待批 3。
