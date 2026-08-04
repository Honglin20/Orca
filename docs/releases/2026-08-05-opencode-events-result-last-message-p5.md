# Release Note — opencode events result 取末条消息（P5）

**日期**：2026-08-05
**Commits**：`269e288`（主修复）→ `f9fe02c`（code-reviewer 一轮简化 + 边界测试）
**分支**：`in-session-unified-backend`

## 背景（真实失败定位）

KD-NAS 真跑 `examples/mnist_kd/`（MNIST 分类器 [1,1,28,28]→[1,10]）在 flatten 节点 fail，schema 校验报
`[1,1,28,28] is not of type 'object'`——但 tape seq 98 的末条 `agent_message` 是合法 output_schema JSON
object。根因（用真实代码 + 真实 tape `kd-nas-20260805-005130-ac3b11.jsonl` 复现确认）：

- `orca/exec/claude/accumulator.py` `events_result_text` 把**所有** `agent_message` 串接（含中间叙述）。
- `orca/exec/claude/result_extractor.py` `_first_balanced_block` 取**第一个** `{`/`[` 平衡块——
  flatten 中间叙述「input `[1,1,28,28]` float32」（seq 53）比末条 JSON 早出现 → 被当 result 抓出。

只在 opencode `events` 模式触发（claude `result_line` 模式推整段最终答案，不串接中间叙述，不受影响）。
kd-nas 各节点用 opencode executor → 中间叙述带 `[...]`/`{...}` 字面量就中招。

## SDD 语义对齐

契约本就声明「agent **最终消息** = JSON result」（`docs/specs/2026-08-04-in-session-failure-sentinel-and-injection.md`
§4.1「子代理最终消息，agent-emitted」+ `docs/specs/agent-ask-user-sentinel.md`「返回恰好这个 JSON 作为最终消息」）。
中间消息是叙述/工具输出，**不是 result**。旧实现「全串接」违反契约——是预存 engine bug。

## 实现（两 commit）

### Commit `269e288`：主修复

- `accumulator.events_result_text`：`"".join(self._text_parts)` → `self._text_parts[-1]`。
  仅取末条 `agent_message`。docstring 钉死「契约：末条 = JSON result」+ 解释 P5 bug 根因。
- `result_extractor.py` **不动**（blast radius 最小）：
  - claude `result_line` 模式经 `make_on_result_hook` 直写 `result_text`，路径完全不变。
  - events 模式末条若为纯 JSON（契约场景），`extract_json_text` step 1 `json.loads(text)` 直接成功。
- `opencode_translator._translate_text` docstring 更新「末条 = result，中间叙述不进 result」。
- 模块 docstring 同步更新（`events_result_text` / `consume_event` 注释）。

### Commit `f9fe02c`：code-reviewer 一轮闭环

- **YAGNI**：`_text_parts: list[str]` → `_last_text: str | None`。原 list 无实际消费方
  （`diagnose()` 只读 `result_text`），保留全列表违反 YAGNI。简化后 `events_result_text` 直接
  返回字段，无 `[-1]` 索引；`consume_event` 改 `self._last_text = text`（末条覆盖前者）。
- **边界测试**：补 `test_consume_event_missing_or_none_text_ignored`，钉死 `data` 缺 `text` 字段
  / `text=None` 不污染 result（守住 `if text:` 对 falsy 的过滤契约）。

## 测试

- **P5 决定性回归门（新增）**：`tests/exec/claude/test_accumulator.py::test_p5_tape_replay_kd_nas_flatten_extracts_final_json`
  - fixture：`tests/exec/claude/fixtures/kd_nas_flatten_p5_replay.jsonl`（101 events，抽自真实失败 tape）
  - replay 全量 events（含 `tool_call` / `usage` / `node_*` 干扰）→ 断言 `events_result_text` ==
    seq 98 合法 JSON object（`model_name=mnist_cnn`），且 **`"[1,1,28,28]" not in result_text`**
    （旧 bug 可观测信号）。再过 `extract_and_validate(result_text, flatten_schema)` 校验通过。
- **末条覆盖意图（更新）**：`test_consume_event_agent_message_last_wins`——构造「中间叙述带
  `[1,1,28,28]` 字面量 + 末条 JSON」钉死末条契约（旧 `..._appends` 测试编码了 buggy 全串接语义）。
- **e2e 契约测试（更名）**：`tests/exec/test_e2e_opencode.py::test_opencode_node_completed_output_is_last_text_event`
  （旧 `..._is_concatenated_text`）。FakeRunner 走完整 executor 路径，补齐 tape-replay 只过
  `consume_event` 不过 executor.py:198 wiring 的缺口。
- **回归全绿**：
  - `tests/exec/` 440 passed + 1 skipped（含 claude `result_line` 模式既有测试，零回归）。
  - `tests/profiles/` 89 passed。
  - `tests/exec/test_e2e_opencode.py` + `tests/exec/claude/` 88 passed。

## 跨 opencode workflow 回归

修改的是所有 opencode-executor workflow 共享的 events 模式 result 语义。struct-exploration / kd-nas
等 workflow 的 agent prompt 都按契约「末条消息即 JSON result」设计，故契约收紧（从「全串接」到「末条即 result」）
不破坏合规 agent；任何依赖中间叙述进 result 的实现都是契约违背，行为变化是契约执行而非回归。

## 真实 e2e 验证

`tars run workflows/kd-nas.yaml` 对 `examples/mnist_kd/`（max_rounds=2 / full_epochs=2 / device=cpu），
run_id `kd-nas-20260805-011253-6c2ebe`。**flatten 节点 seq 92 `node_completed` PASS**——P5 修复在
原失败点直接验证（旧实现必死在 schema 校验 `[1,1,28,28] is not of type 'object'`）。后续节点：
flatten → setup → gen_teacher → gen_train_script → train_script_verify → train_teacher（真 10 epoch CPU
训练 + eval）→ gen_student → distill → decide（轮 1）→ gen_student → distill → decide（轮 2）→ finalize。

**12/13 节点 PASS**（含 P5 原失败点 flatten，以及 2 轮完整 KD 蒸馏循环）。gen_teacher 末条
`agent_message`（seq 243）是合法 JSON（`{"teacher_model_path": ...}`），证明「末条即 result」契约
在生产 agent 上自然成立。

### finalize 节点失败（非 P5 回归，workflow-agent 层 bug）

finalize agent 末条消息的 ```json fence 内 JSON **结构性畸形**——括号深度计数显示 `final_depth=1`
（缺一个根级 `}`），`json.loads` 报 `Expecting ',' delimiter: line 6 column 1253`。**与 P5 无关**：
验证方法是把 finalize 节点的所有 `agent_message` 按旧「全串接」语义拼起来再跑同样的 fence 提取 +
json.loads，**同样失败**（同一个 char 1523 错）。这是 workflow 层 agent 产出的真实 malformed JSON，
按用户约束「不碰 kd-nas workflow 文件（P5 是 engine 层）」不在本次范围。Engine 的 fail loud 行为正确：
错误信息清晰指出「result 文本无法提取为合法 JSON」+ 前 200 字符预览。

### 产物（真实落盘）

- ledger.jsonl: 2 行（2 轮 KD）
- champions.jsonl: `{"round": 0, "id": "baseline", "latency_us": 42.262021, "accuracy": 0.9,
  "delta_vs_baseline_us": 0.0, "snapshot": ""}`（baseline champion，无 student 超越）
- reports/final_report.md: 已写（finalize agent Step 1 完成）

## 偏差

无（实现方案落在用户给的首选方案：events_result_text 返回末条，result_extractor 不动）。
finalize 失败属新发现的 workflow-agent bug（P6 候选），不在 P5 engine 层范围。
