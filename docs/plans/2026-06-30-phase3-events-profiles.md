# 开发计划 —— 阶段 3：events/ + profiles/ + capability 校验闭环

> **状态**：待执行
> **SPEC**：[`docs/specs/phase-3-events.md`](../specs/phase-3-events.md)（契约，逐字实现）
> **日期**：2026-06-30

---

## 0. 范围与步骤拆分

phase 3 = **3 个可独立验收的步骤**，依赖顺序 A → C；B 与 A 互相独立（可并行）。

```
schema（已就位，含 session_id）
        │
   ┌────┴────┐
   ▼         ▼
 步骤 A     步骤 B
 events/    profiles/（registry+capabilities+builtin）
   │         │
   │         ▼
   │       步骤 C
   │       profiles/validate.py + compile _check_profiles
   ▼
（A、C 各自验收，最终全量回归）
```

| 步骤 | 模块 | 产出文件 | 测试 | 依赖 |
|---|---|---|---|---|
| **A** | events/ | tape.py / bus.py / replay.py / __init__.py | test_tape / test_bus / test_replay | schema |
| **B** | profiles/（核心） | base.py / capabilities.py / registry.py / builtin/{claude,ccr}.py / __init__.py | test_registry / test_capabilities | schema |
| **C** | profiles/validate + compile | validate.py + 改 compile/validator.py | test_validate / test_validate_profiles | B（profiles）|

---

## 1. 步骤 A —— events/（唯一真相源）

### A.1 Tape (`orca/events/tape.py`)
- `class Tape(path, run_id, *, resume=False)`
- `append(event) -> seq`：**一把 `asyncio.Lock` 内完成「seq 分配 + write + flush」**（保证 seq 序 == 文件行序）；seq 写时单调自增；调用方不传 seq
- `_json_safe(obj)`：bytes→utf-8、Path→str、未知→str（借自 Conductor `event_log.py:43-55`）
- `replay(since_seq=0)`：逐行 parse，**容忍末尾残行**（跳过不完整 JSON，不抛）
- `resume=True`：append 模式打开 → **先扫描末尾，截断不完整行（记 warning）** → last_seq 从有效事件重算 → 从 last_seq+1 续写
- `last_seq()` / `close()`

**关键风险**：resume 残行截断（不截断会接坏行）；Lock 范围（必须覆盖 seq+write 整体）。

### A.2 EventBus (`orca/events/bus.py`)
- `class EventBus(tape)`：emit 第一动作 = `tape.append`（强制）
- `emit(type, data, node=None, session_id=None) -> Event`：构造 Event（**透传 session_id**）→ append → 异步 fan-out
- `subscribe() -> Subscription`：每个 Subscription 自带 `asyncio.Queue` + cursor
- fan-out：`put_nowait`；**队列满 → 丢最老 + warning**（不阻塞 emitter；订阅者靠 replay 补全）
- seq 单调交给 Tape 的 Lock（bus 不再加锁）

**关键风险**：慢订阅者阻塞 emitter（须异步）；session_id 必须透传到事件顶层。

### A.3 replay (`orca/events/replay.py`)
- `replay_state(tape, since_seq=0) -> RunState`：fold tape，纯函数
- `apply_event(state, event) -> RunState`：单一 reducer，每个 EventType 一个分支
- **幂等硬约束**：streaming text 用 `text@seq`（keyed by seq，last-writer-wins），**绝不字符串拼接**
- 同 node 多 session：node_status/context 取最后写入；session 细节不进 RunState

**关键风险**：幂等性（核心测试：应用两次 = 一次）。

### A.4 验收（SPEC §6.1–6.4, 6.8）
跑 `tests/events/` 全绿；**幂等性测试**必须覆盖。

---

## 2. 步骤 B —— profiles/ 核心（命令替换层）

### B.1 capabilities.py
- `ProviderCapabilities(BaseModel)`：frozen + `extra="forbid"`；7 个能力字段（SPEC §4.4）

### B.2 base.py
- `CliProfile`（frozen dataclass）：name/capabilities/cli_path_env/default_cli_path/flags/prompt_channel/mcp_flag_template/env_overlay_prefixes/stream_format/translator/result_extractor/prompt_paradigm
- `resolve_cli_path()`：env > default，运行时读
- 类型别名 `Translator`、`ResultExtractor`

### B.3 registry.py
- `load_builtin_profiles()`：扫 `builtin/*.py` 导入 `PROFILE`
- `load_project_profiles(cwd)`：扫 `<cwd>/.orca/profiles/*.py` 覆盖 builtin（`HARNESS_DISABLE_PROJECT_PROFILES=1` 禁用）
- `get_profile(name)`：不存在/disabled → ValueError（附 disable 原因）
- `register / disable_profile / available_profiles`
- 损坏文件 → `disable_profile` + fail loud（不静默丢）

### B.4 builtin/{claude,ccr}.py
- claude：flags `-p --output-format stream-json --include-partial-messages --verbose --permission-mode auto --bare`；capabilities 全开
- ccr：`default_cli_path="ccr code"`；capabilities 按实情（mcp_tools=False 等）
- translator/result_extractor 用 **dummy 占位**（真实现 phase 4），须类型匹配含 session_id 的 Event

### B.5 验收（SPEC §6.5–6.6）
`get_profile` / env 覆盖 / project 覆盖 / disable / capabilities frozen。

---

## 3. 步骤 C —— capability 校验闭环

### C.1 profiles/validate.py
- `@dataclass ProfileIssue(node, severity, message)`
- `validate_workflow_profiles(wf) -> list[ProfileIssue]`，四条规则：

| # | 条件 | severity |
|---|---|---|
| 1 | `get_profile(executor)` 失败 | error |
| 2 | `output_schema is not None` 且 `structured_output=="none"` | error |
| 3 | foreach body 是 AgentNode 且 `concurrent_safe==False` | error |
| 4 | `streaming_events==False` | warning |

- **只依赖 `orca.schema` + `orca.profiles.registry`**，不依赖 compile

### C.2 compile/validator.py 集成
- 追加 `_check_profiles(wf, result)`（第 ⑨ 项）：`from orca.profiles import validate_workflow_profiles`，issue → result.add_error/add_warning
- `validate_workflow` 在 8 项后追加调用，仍走 `raise_if_errors`（聚合一次报全）

### C.3 验收（SPEC §6.7–6.8）
四条规则各覆盖；compile 与 phase 2 的 8 项**共存不回归**。

**依赖方向核对**：compile → profiles → schema，无环。

---

## 4. 验收总则（SPEC §6.0，5 条铁律，每步自检）

1. **唯一真相源**：事件只写 Tape 一处（grep 无第二份存储）
2. **幂等性**：reducer 应用 N 次 = 1 次（有测试）
3. **一条读路径**：streaming = replay = 同一 apply_event
4. **fail loud**：未知 executor / 不兼容 capability / 残行 / 损坏 profile 全显式报错
5. **依赖单向无环**：events→schema、profiles→schema、compile→profiles、exec→profiles

---

## 5. Definition of Done

- [ ] A/B/C 三步产出齐全（events 4 文件 + profiles 7 文件 + compile 改动）
- [ ] SPEC §6 全部 checkbox 通过
- [ ] 全量 `pytest` 绿（含 phase 1+2 不回归，预期 ≥ 103 + phase 3 新增）
- [ ] 自我 review 通过（reducer 幂等 / Tape 唯一真相 + resume 清残行 / Lock 覆盖 seq+write / 异步不阻塞 / session_id 透传 / profiles 依赖方向 / validate 规则只基于真实字段）
- [ ] release note + CHANGELOG + CURRENT.md 更新
- [ ] commit

---

## 6. 不做（边界）

WebSocket/HTTP（phase 7）· `--bg`/attach（未来）· snapshot/session 分区（文档化目标）· sidecar（反模式②）· 真 translator（phase 4）· 生成 run_id（phase 5）
