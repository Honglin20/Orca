# phase-13 release note —— script-side render_chart 接入（env 身份路由 + per-run Unix socket + 大数据防御）

> **SPEC**：[`phase-13-render-chart.md`](../specs/phase-13-render-chart.md)（对抗审闭环 v1，16 处修订）
> **计划**：[`2026-07-03-phase13-render-chart.md`](../plans/2026-07-03-phase13-render-chart.md)
> **分支**：`phase13-render-chart`（未 merge）
> **测试后端**：opencode + deepseek-v4-flash（CLAUDE.md 已记录，不再用 claude 作后端测试）
> **commits**：`1740a98`（S1-S4）+ `f260935`（S5 实施补丁：executor-agnostic）+ `b562a12`（S5 e2e）

---

## 1. 解决了什么

phase 13 回答：**「agent spawn 的 script 子进程里调 render_chart 时，怎么把图绑定到正确的 run？多个 run 并行时怎么不串？chart 数据可能很大，tape 怎么不被撑爆？」**

**核心机制**：
- **env 注入身份路由**：ClaudeExecutor / ScriptExecutor spawn 时注入 `ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK`，沿 subprocess 链自然继承到 script。script 调 `orca.chart.render_chart()` 从 env 读身份（不接收参数），agent 无法干扰。**multi-run 并行天然隔离**。
- **per-run Unix socket**：`runs/<run_id>.sock` 文件路径即 run 定位，零端口冲突、零跨 run 路由层。
- **大数据三道关**：① 客户端库自动降采样（max_points=2000，6 种 chart_type 各自策略，按 hue 分组）② 硬上限 2MB（含 envelope）fail loud ③ ingestor 端复核防绕过。
- **三壳零改动**：TUI（phase-12）/ Web（phase-9d）渲染侧已实现，本 SPEC 是生产者侧接入，零前端代码改动，1133 → 1224 passed 零回归。

---

## 2. 架构（生产者 → 真相源 → 渲染）

```
ClaudeExecutor / ScriptExecutor spawn (env overlay 注 4 个 ORCA_*)
        ↓ subprocess.Popen(env=overlay)
claude -p / opencode / script subprocess（继承 env）
        ↓ Bash 工具 spawn script / script 直接调
script: from orca.chart import render_chart
        ↓ render_chart() 内部：读 env → 校验 → 降采样 → 大小检查 → socket connect
per-run Unix socket (runs/<run_id>.sock)
        ↓
chart_ingestor task (RunManager / OrcaApp 各起一份)
        ↓ bus.emit("custom", {"kind":"chart", "chart":payload}, node, session_id)
Tape.append（唯一真相源）
        ↓ 单一读路径
TUI（phase-12 已实现）/ Web（phase-9d 已实现）零改动渲染
```

**核心不变量**：
- 身份维度（run_id / node / session_id）由 Orca 注入，**单调向下**，agent / script 无法反向伪造或污染其他 run。
- socket 是传输通道，不持久化任何状态；tape 仍是唯一真相源。
- 多 run 并行 = 多条独立 env 继承链 + 多个独立 socket 文件，零交叉。

---

## 3. 改动清单

### 3.1 新文件（生产代码）

| 文件 | 用途 | LOC |
|---|---|---|
| `orca/chart/__init__.py` | 公开 `render_chart` API | ~22 |
| `orca/chart/_limits.py` | 常量同源（`MAX_MESSAGE_BYTES=2MB` / `DEFAULT_MAX_POINTS=2000` / `ACK_TIMEOUT_SECONDS=10` / `SOCK_PATH_MAX=90`）| ~36 |
| `orca/chart/_validate.py` | ChartPayload fail loud 校验 | ~84 |
| `orca/chart/_downsample.py` | 6 种 chart_type 降采样（line/area/scatter 按 hue 分组、bar/pareto 按 x 聚合、table 前 N、radar 不变）| ~190 |
| `orca/chart/_render.py` | `render_chart` 主逻辑（env 读 → 校验 → 降采样 → 大小 → socket + ack）| ~190 |
| `orca/events/chart_ingestor.py` | per-run Unix socket listener（emit + ack + crash 恢复 + stale sock 处理）| ~210 |

### 3.2 修改文件

| 文件 | 改动 |
|---|---|
| `orca/exec/env.py` | `build_env_overlay` 加 4 个 keyword 参（run_id/node/session_id/chart_sock，缺则不注，backward compat）|
| `orca/exec/factory.py` | `make_executor` 透传 `runs_dir` 到 ClaudeExecutor + ScriptExecutor |
| `orca/exec/claude/executor.py` | `_build_spawn_config` 传 4 个 keyword + `_resolve_chart_sock_path` 帮助函数 |
| `orca/exec/script.py` | **S5 修补 gap**：`ScriptExecutor.__init__(runs_dir=None)` + `exec()` 内构造 chart env overlay（与 ClaudeExecutor 对称） |
| `orca/iface/web/run_manager.py` | `RunHandle` 加 `_chart_ingestor` 字段 + `start_run` 起 ingestor + `_teardown_handle` cancel + sock unlink + resume 边界（resume 模式不起）+ SOCK_PATH_MAX 检测 |
| `orca/iface/cli/app.py` | **S5 修补 gap**：`OrcaApp._run_pipeline` 起 per-run chart ingestor（与 RunManager.start_run 对称），`finally` cancel + unlink |
| `orca/run/orchestrator.py` | `_execute_agent` 推导 runs_dir 传入 |

### 3.3 新文件（测试）

| 文件 | 用例数 |
|---|---|
| `tests/chart/test_validate.py` | 14 |
| `tests/chart/test_downsample.py` | 11 |
| `tests/chart/test_render.py` | 19 |
| `tests/chart/test_sock_path_length.py` | 4 |
| `tests/events/test_chart_ingestor.py` | 10 |
| `tests/iface/web/test_run_manager_chart.py` | 4 |
| `tests/exec/claude/test_executor_env_inject.py` | 9 |
| `tests/exec/test_script_env_inject.py`（S5 补）| 10 |
| `tests/exec/test_env.py`（扩展）| +4 |
| `tests/e2e_phase13/test_e2e_1_basic_chart.py` | 1 真跑 |
| `tests/e2e_phase13/test_e2e_2_multi_run_parallel.py` | 1 真跑 |
| `tests/e2e_phase13/test_e2e_3_large_data_downsample.py` | 1 真跑 |
| `tests/e2e_phase13/test_e2e_4_oversize_rejected.py` | 1 真跑 |
| `tests/e2e_phase13/test_e2e_5_pressure.py` | 1 真跑（3 run × 10 chart 压测）|
| `tests/e2e_phase13/test_e2e_6_opencode_deepseek_tui.py` | 1 真跑（opencode + deepseek-v4-flash + TUI snapshot）|
| `tests/e2e_phase13/scripts/{chart_demo,chart_parallel,chart_large,chart_pressure}.py` | 4 demo script |

---

## 4. S5 e2e + 压测 + opencode+deepseek 真跑结果

### 4.1 E2E-1～5 全部真跑通过

| 用例 | 关键断言 |
|---|---|
| E2E-1 basic_chart | 1 chart 事件 / node=worker / chart_type=line / label=training / data 5 点 |
| E2E-2 multi_run_parallel | 3 run 并行 / 每 tape 各 1 chart / 3 label 唯一（嵌 ORCA_RUN_ID）|
| E2E-3 large_data_downsample | 100k 行 → ≤ 2000 + ≥ 1000 / payload 编码 < 2MB |
| E2E-4 oversize_rejected | 500k 行 + max_points=200000 → client raise + tape 无事件 |
| **E2E-5 pressure** | **3 run × 10 chart 无丢失 / 串扰**：每 tape 恰 10 条 / 3 run label 集合两两 isdisjoint / chart_type 5 种全覆盖 |

### 4.2 E2E-6 opencode + deepseek-v4-flash 真跑（SPEC §8.4 实施 blocker）

**真跑路径**：`OrcaApp.run_test()` 起 TUI → 真起 orchestrator → spawn opencode（model=`deepseek/deepseek-v4-flash`）→ opencode agent 调 bash 工具 spawn `python3 chart_demo.py` ×3 → env 链继承 ORCA_* → `orca.chart.render_chart` 真 socket 连 ingestor → tape。**全程不 mock**。

**4 个用户重点关注验证点逐条结论**：

| # | 验证点 | 断言证据 | 结论 |
|---|---|---|---|
| 1 | **agent_message 完整性** | tape 含 `agent_message` 事件（joined text="DONE"，**opencode events 模式不丢消息**）；TUI 流式 tab 含 `[msg]` 行（phase-12 §6.3 opencode translator 加的 `[msg]` 前缀）| ✓ 通过 |
| 2 | **TUI 各面板显示合理** | DagGraph 渲染含 "runner" 节点；`status_of_node("runner")=="done"`；NodeDetail `active=="runner"` `kind=="agent"`；编排 `terminal_state.status=="completed"` | ✓ 通过 |
| 3 | **render_chart 正确推送** | tape 含 3 条 `custom(chart)` 事件（chart_demo.py ×3 真 spawn）；chart 字段 `chart_type=line / label=training / title=loss / data 5 点`；TUI 图表 tab line chart 真渲染为 braille（`_PLOTEXT_OK=True` + `last_rendered` 含 braille 字符 0x2800-0x28FF）| ✓ 通过 |
| 4 | **图表排布合理** | `charts_for("runner")` 含 `training` label；`training_titles.count("loss")==1`（**同 label+title 替换不堆积**，SPEC §0.1 #6）| ✓ 通过 |

**artifacts 留档**：
- `tests/e2e_phase13/_artifacts/phase13_e2e6_tui.svg`（62KB，真截图）
- `tests/e2e_phase13/_artifacts/phase13_e2e6_tape.jsonl`（6KB 真tape）
- `tests/e2e_phase13/_artifacts/phase13_e2e6_workflow.yaml`

**tape 事件计数**（确证 render_chart 真推送）：
```
agent_tool_call         3   # opencode 调 bash 工具 3 次
agent_tool_result       3   # 3 次结果
custom                  3   # ← 3 条 chart 事件真落 tape
agent_message           1   # 最终答案 "DONE"
workflow_completed      1
```

### 4.3 既有测试套件零回归

```
pytest（全量）: 1224 passed, 30 skipped in 199.66s
- baseline 1208 passed
- 新增 10 单测（test_script_env_inject.py）+ 6 E2E（test_e2e_1~6）= 16
- 1208 + 16 = 1224 ✓
- 30 skipped：playwright 未装 / claude CLI 集成（pre-existing 环境限制）
- 0 failed / 0 回归
```

---

## 5. S5 实施发现的 2 个 gap（已修）

### 5.1 ScriptExecutor 漏 chart env 注入（违反 SPEC §11 #9 executor-agnostic）

**症状**：S1-S4 实施只把 chart env 注入到 ClaudeExecutor 路径（agent → claude/opencode → Bash → script），**ScriptExecutor 完全没接**。E2E-1～5 用 script 节点 spawn python 调 render_chart 时，env 缺 → client lib 第一步 raise。

**根因**：SPEC §1 图示只展示 agent 路径，但 §11 #9「executor-agnostic」是契约。S1-S4 漏覆盖 ScriptExecutor 是实施 bug。

**修复**（commit `f260935`）：
- `orca/exec/script.py`：`ScriptExecutor.__init__(*, runs_dir=None)` + `exec()` 内构造 chart env overlay + `create_subprocess_shell(env=...)`
- `orca/exec/factory.py`：`make_executor` script 分支透传 `runs_dir`
- `tests/exec/test_script_env_inject.py`：10 个对称单测

### 5.2 OrcaApp CLI TUI 路径漏起 chart ingestor

**症状**：phase-13 S2 把 chart ingestor 接入 `RunManager.start_run`（web shell 路径），但 **CLI TUI shell (`OrcaApp._run_pipeline`) 漏接**。E2E-6 第一次跑时 4 验证点 1/2 通过，但 3 失败（`got 0 chart events`）—— `agent_tool_result` 含 `FileNotFoundError` (sock 不存在)，证明 ingestor 未起。

**根因**：与 5.1 同类实施 gap（CLI shell 与 web shell 两套 run 启动路径，phase-13 S2 只覆盖一边）。

**修复**（commit `f260935`）：
- `orca/iface/cli/app.py::_run_pipeline` 起 per-run chart ingestor（与 `gate_handler.start()` / `interrupt_handler.start()` 同 phase）
- `finally` cancel + unlink sock
- sock path 过长 → log warning + 不起（不阻塞 run，与 RunManager 一致语义）

**两个 gap 共同点**：e2e 真跑才发现的设计盲点。**回归证据**：1224 passed 全量零回归。

---

## 6. 关键决策（对抗审闭环 16 处）

phase-13 SPEC 经 `spec-review-adversarial` 1 轮对抗审，闭环 4 blocker + 9 major + 3 minor：

| 类别 | 闭环点 |
|---|---|
| Blocker（必改）| E3 ack timeout（10s）/ E4 sock 路径长度（90B）/ E10 resume 边界（不支持 +chart 共存）/ E11 opencode env 继承（兜底 fail loud）|
| Major（必改）| E1 env 注入条件 / E2 socket 协议细则 / E5 envelope 含义 / E6 dedup vs iteration（删夸大宣称）/ E7 table 降采样（取前 N）/ E8 hue 分组 / E9 ingestor crash 恢复 / E12 验收客观化 / E16 MCP 废弃论据 |
| Minor（必改）| E13 workflow 级 chart 边界声明 / E14 RunHandle 字段显式 / E15 1MB→2MB typo |

详见 SPEC §0.1–§11 + 关键决策备忘。

---

## 7. 已知 gap / 后续

| 项 | 状态 | 处理 |
|---|---|---|
| resume 模式 + chart 共存 | YAGNI 不支持（SPEC §3.1）| 真痛点时另开 SPEC |
| chart 历史（不替换）语义 | 默认实时替换（SPEC §6.4）| script 用不同 title workaround；未来扩 `history: bool` |
| workflow 级 chart（node=None）| 本 SPEC 不支持（env 必有 ORCA_NODE）| TUI `__workflow__` 桶是前端预留扩展点，本 SPEC 不产出对应事件 |
| 非 Python 脚本客户端 | 协议开放（newline-JSON over Unix socket）| 第三方可实现，本 SPEC 不提供 |
| MCP 工具版 render_chart | §0.4 明确废弃（方向错误）| 未来 agent 自画图场景如需，可与 ask_user 并存，不冲突 |
| sidecar 大文件 / gzip | §5.4 / §5.5 否决（YAGNI）| 真需要时另开 SPEC |

---

## 8. 关键决策备忘

1. **render_chart 不是 MCP 工具**（§0.4）—— script 内 Python 调用，env 继承路由。MCP 版废弃。
2. **身份路由 = env 继承**（§0.1 #2 / §2）—— 单向信息流，agent 无法干扰。multi-run 并行天然隔离。
3. **per-run Unix socket**（§0.1 #3 / §3）—— socket 路径即 run 定位，零端口冲突。
4. **chart 是事件**（§0.1 #1 / §6）—— 沿用 phase-9d / phase-12 契约，零 schema 改动。
5. **dedup key = label + title**（§0.1 #6 / §6.2）—— 跨 session_id 也替换（默认实时更新）；**chart 不保留 iteration 历史**。
6. **大数据三道关**（§5）：自动降采样 + 硬上限（2 MB 含 envelope）+ tape 拒收超限。两端常量同源。
7. **不做 sidecar / gzip**（§5.4 / §5.5）—— YAGNI；inline + 降采样 + 上限已足够鲁棒。
8. **session_id 不保留 iteration 历史**（§6.3）—— dedup 跨 session 替换；如需保留用不同 title。
9. **executor-agnostic**（§2.1）—— env 注入对 claude / opencode / ScriptExecutor / 任何 executor 同样工作（S5 已闭环）。
10. **fail loud 9 处**（§7）—— env 缺、payload 校验失败、socket 不可达、ingestor 拒收、大小超限、ack timeout、sock 路径过长，全部 raise。
11. **三壳零改动**（§8.6）—— phase-13 是生产者侧接入，TUI/Web 渲染侧不动。
12. **client lib 不接收身份参数**（§4.3）—— 杜绝 agent 诱导 script 传错 run_id 的攻击面。
13. **不支持 resume+chart 共存**（§3.1）—— YAGNI 决策。
14. **socket 路径长度限制**（§7.7）—— > 90 字节 fail loud；`ORCA_RUNS_DIR` workaround。
15. **测试后端固定 opencode + deepseek-v4-flash**（§8.4）—— 不再用 claude 作为后端测试（CLAUDE.md 已记录）。
