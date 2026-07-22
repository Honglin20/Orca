# Release: P4 TARS skill 哨兵处理全量落地

**日期**: 2026-07-22
**SPEC**: [`docs/specs/agent-ask-user-sentinel.md`](../specs/agent-ask-user-sentinel.md) §2（TARS skill 行为）
**范围**: 只改 `orca/skills/tars/SKILL.md`——零 `orca/` 引擎改动、零 workflow 改动、零 agent.md 改动
  （agent.md 的「严禁造假」哨兵段落是 P5/P6/P7 的事）。
**Commit**: `774aa46`

---

## 结论

TARS skill（IN-SESSION 驱动 skill）全量接入 ask-user 哨兵闭环：派子 agent 跑节点 → 子 agent 缺
必填项时返回哨兵 JSON → TARS 在调 `orca next` **之前** strict 识别 → 捕获 task_id → 问用户 → 恢复
**同一**子 agent（上下文不丢）→ 拿真实产出 → 才喂 `orca next`。**哨兵绝不进 `orca next`**（5 处强调 +
机制说明），引擎 `output_schema` 校验只作用在真实产出上，compile validator 铁律 7 不触发，**引擎零改动**。

本段是 P3:0-b spike（`tests/spike_ask_user/tars_loop.py::drive_node`）的 skill 指令投影——把 Python
控制流翻成 LLM 可执行的 6 步小循环。spike 38 测试基线保持绿（改动未触碰 spike 代码）。

---

## 改了什么（`orca/skills/tars/SKILL.md`，+104/-5）

### 1. 驱动循环第 2 步加哨兵分支（L155-163）

原 step 2「子代理返回后，把产出原样作 --output」改为先判哨兵：
- 是哨兵 → 走【哨兵处理】小循环（问用户 → 恢复同一子 agent → 拿真实产出）；🔴 哨兵绝不进 `orca next`。
- 不是哨兵 → 真实产出原样作 `--output`。

### 2. 新增「### 哨兵处理」段（L174-264）

SPEC §2 伪代码的 skill 指令投影，6 步处理小循环：

1. **strict 识别哨兵**（非 substring match）：括号配平抽最外层 JSON（支持 ```json 围栏 / 前后文本）
   + `json.loads` + `dict["_sentinel"] == "orca_ask_user_v1"` 魔键校验。任何一步失败 → 当真实产出。
   只做魔键识别；body schema 严格性（unknown key / 类型）由子 agent agent.md 约束，driver 不校验。
2. **捕获 task_id**（Task 返回时立刻记下）：CC `agentId`（含 PostToolUse hook）/ opencode `ses_xxx`。
   拿不到 → 当崩溃 fail loud，不重派。
3. **问用户**：CC 原生 `AskUserQuestion`（结构化 options）/ opencode 主聊天问读下一轮（自由文本）。
4. **恢复同一子 agent**：CC `SendMessage(task_id, ...)` / opencode `Task(task_id="ses_xxx", ...)`。
   含「用户答不知道」字面模板（spike `_build_resume_message(None)` 等价）。
5. **MAX_ASK=3 兜底**：`while 是哨兵 and attempts<3: 问+恢复; if 仍哨兵: fail loud + orca stop`。
   不无限循环。off-by-one 与 spike `drive_node` 一致（3 次恢复后仍哨兵 → 放弃）。
6. **真实产出 → 才 `orca next`**：退出小循环后必为非哨兵；🔴 若含造假痕迹（`torch.randn`/`fake_data`
   等）不喂 `orca next`，当子 agent 失败 fail loud。

外加「失败路径」表 5 行（MAX_ASK 耗尽 / 子 agent 崩溃 / false positive / 上下文膨胀 / 跨 session 续跑）。

### 3. 常见错误 + success_criteria 同步（L359-360, L367-368）

- 常见错误加：「🔴 哨兵 JSON 绝不喂 `orca next`」。
- success_criteria 把「每个节点」条拆细 + 新增完整哨兵闭环条（task_id 捕获 → 问用户 → 恢复同一子 agent
  → MAX_ASK fail loud → 哨兵绝不进 orca next）。

---

## spike 等价性（skill 指令 ↔ spike driver）

| spike `drive_node` 分支 | skill 投影 | 等价 |
|---|---|---|
| `backend.spawn` (L130) | drive-loop step 1「用 Task 工具派子代理」 | ✅ |
| `is_sentinel` + `_extract_json_object`（括号配平 + 魔键） | 处理小循环 step 1 | ✅ 逐字 |
| `SubagentResult.task_id`（spawn 时捕获） | 处理小循环 step 2 | ✅ |
| `answer_provider(question)` | 处理小循环 step 3 | ✅ |
| `_build_resume_message(answer)` / `(None)` | 处理小循环 step 4（含 None 模板） | ✅ |
| `backend.resume(task_id, msg)` | 处理小循环 step 4 | ✅ |
| `while is_sentinel: if attempts>=MAX_ASK: raise` | 处理小循环 step 5 | ✅ off-by-one 一致 |
| `assert not is_sentinel` (post-loop) | step 6「退出小循环后必为真实产出」 | ✅ 语义 |
| `looks_fabricated` → `FabricationDetected` | step 6 🔴 prompt 层判断（降级，见下） | ✅ 故意降级 |

**故意的降级**（spike README「P4 关键输入」#5 显式背书）：
- `looks_fabricated` 确定性扫描在生产路径降级为 step 6 的 prompt 层判断（agent 输出非确定，
  无确定性 regex）；agent.md 的「严禁造假」段落是 prompt 层主约束（P5/P6/P7 落地）。
- `parse_sentinel` 的 unknown-key / 类型严格校验由子 agent agent.md 约束，driver 侧只做魔键识别。

---

## 跨后端差异处理

| 维度 | CC（生产主路径，先 ship） | opencode（experimental） |
|---|---|---|
| task_id 捕获 | Task 返回 `agentId`（+ PostToolUse hook `tool_response.agentId`） | 解析 `<task id="ses_xxx">` |
| 问用户 | 原生 `AskUserQuestion`（结构化 options） | 主聊天问 + 读下一轮用户回复（自由文本，prompt 强制结构化） |
| 恢复同一子 agent | `SendMessage(task_id, msg)` | `Task(task_id="ses_xxx", subagent_type=<同>, prompt=msg)` |

opencode 的 `Task(task_id=ses_xxx)` 恢复机制在 1.18.3 已验证原生支持（SPEC §2 注），但本任务未做
opencode in-session E2E（CC 路径为主）。opencode 实测留作 follow-up。

---

## 验证

- **spike 基线保持绿**：`pytest tests/spike_ask_user/ -m "not integration"` → 38 passed, 2 deselected
  （改动未触碰 spike 代码，`git diff --stat tests/spike_ask_user/` 空）。
- **code-reviewer 两轮闭环**（design/contract + spike-equivalence 并行）：
  - 无 🔴 阻断。
  - 2 🟡 全修：(1) 自包含违规——块引用 provenance 原含仓库相对路径（安装到
    `~/.config/opencode/skills/tars/` 后断链），改为维护者溯源措辞；(2) fabrication 兜底缺失——
    step 6 加 prompt 层造假判断（合并两位 reviewer 的分歧：R1 当 🟡 缺失，R2 指出是 spike README P4 #5
    显式降级——采 prompt 层提醒路径，Rule 7）。
  - 6 🟢 全修：schema 严格性说明 / task_id 捕获失败 fail loud / None 分支字面模板 /
    「回第 1 步」→「回本小循环第 1 步」/「Tier B 项」→「必填项」(skill 自包含术语) /
    失败表加「跨 session 续跑」行（哨兵不落 tape）。
- **硬约束遵守**：只改 `orca/skills/tars/SKILL.md`（`git diff --name-only` 证实）；零引擎改动；
  零 workflow / agent.md 改动。

---

## 偏差与后续

- **opencode 实测**：`Task(task_id=ses_xxx)` 恢复已在 1.18.3 验证，但本任务未跑 opencode in-session E2E
  （CC 路径为主，先 ship）。留 follow-up。
- **agent.md 哨兵段落（P5/P6/P7）**：本任务只改 skill；含 Tier B 必填项的 agent.md 加「严禁造假 +
  哨兵 schema」段落是后续包（spike `data-finder/agent.md` 是可直接复用的模板）。
- **headless TARS-SKILL E2E（SPEC §5）**：本任务交付 skill 指令文本；SPEC §5 的「构造缺 calib loader
  最小 workflow + headless 驱动 TARS」判据需等 agent.md 落地后才能端到端验证（spike 已证 driver 逻辑，
  skill 投影经 reviewer 确认等价）。
