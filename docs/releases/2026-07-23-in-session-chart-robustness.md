# Release Note — In-Session Chart 鲁棒出图 + 失败告知 agent

**日期**：2026-07-23
**Commit**：`003acc3`
**SPEC**：[`docs/specs/2026-07-23-in-session-chart-robustness.md`](../specs/2026-07-23-in-session-chart-robustness.md)

---

## 一句话总结

根治 in-session 路径下 `agent-struct-exploration` 可视化静默失败：用「确定性自加载 env（从 ledger/champions anchor 向上找 `orca_env.sh`）+ 失败可见回流（stdout JSON 升级 + agent dumb copy + `_main` 兜底）」替代原本依赖 LLM「记得 source / 传 --env_file / 不拆调用」的易错链路，杜绝「workflow done 假成功、前端零图、agent/用户无感」。

## 背景与根因（SPEC §0）

- in-session 路径下节点子代理由**宿主 session**（opencode/CC）派发，不经 `ClaudeExecutor` → `ORCA_*` env 不自动注入子代理 bash
- bootstrap 已 detach 起 `chart_daemon` + 写 `runs/<run_id>/orca_env.sh`，但 **env 文件要子代理自己 source 才进 shell**
- `render_chart` 硬依赖 4 个 env（`ORCA_RUN_ID`/`NODE`/`SESSION_ID`/`CHART_SOCK`），缺任一即 `raise RuntimeError`（fail loud）
- opencode 的 bash 工具**不跨调用保 env**：子代理若把 `source orca_env.sh` 与 `python3 viz_struct.py` 拆两次调用，第二次 env 丢失 → raise
- 三处出图调用（curator Step4 / finalize step5 / step6）**均无 source 指令、无 env 兜底**，且失败被三层静默吞：`render_chart` raise → `viz_struct` except 吞成 stderr WARN → bash `|| true` → workflow 照常 done

## 改动清单（SPEC §4）

### 新增

- **`orca/chart/_env.py`**（light-touch，stdlib only）
  - `load_run_env_from_artifacts(anchor_path)`：幂等短路（env 已含 `ORCA_CHART_SOCK` → no-op）→ 否则从 anchor 向上找首个祖先目录含 `orca_env.sh` **且文件内容匹配 `^export ORCA_CHART_SOCK=` 行**（单一标志 + 内容校验，防用户项目根同名文件误匹配，**不依赖 `chart_daemon.log`**）→ 仅补 4 个身份键（不动 `ORCA_ARTIFACTS_DIR`/`ORCA_KB_DIR`）→ 找不到返 `{}`

### 改 viz_struct.py（SPEC §3.2/§3.4）

- 模块顶层 lazy import `load_run_env_from_artifacts`（与 `orca.chart` 同包，import 失败降级）
- `render_all` 开头调 `_resolve_env_status(Path(ledger_path))` 求状态：`ok`（env 已注）/ `env_loaded_from_file`（自加载成功）/ `env_missing`（自加载后仍缺）/ `import_failed`（`orca.chart` 不可用）
- stdout JSON 升级：
  - 顶层加 `viz_env_status` 字段
  - `charts[name]` 从 `{"pushed": bool}` → `{"pushed": bool, "reason": str}`
- `_classify_exc(e)` 把 `render_chart` 异常映射到稳定 reason（**调用点细化**，非统一 catch）：
  - `无法连接` → `socket_unreachable`（`_render.py:173-182` 转自 `FileNotFoundError`/`ConnectionRefusedError`）
  - `ack`/`拒收`/`缺 seq` → `ack_failed`（`_render.py:183-211`）
  - `缺 ORCA_* env`/`不在 Orca run 上下文中` → `env_missing`（half-injection 兜底）
  - 其余 → `generic:<Type>:<msg>`
- 数据不足（ledger < `_MIN_ROWS` / 无有效点）→ `data_insufficient`（设计内）
- 新增 `--mode compare`：仅推终态 Baseline vs Champion vs Final bar（替代 finalize step6 inline `python3 -c` 块，D1=b）。`final` 两值经 CLI 参数传入，**不替换 inline 占位**。两模式共享 stdout schema + 自加载 + `_main` 兜底 + import try/except
- `_main` 异常兜底（**B1 命门**）：catch 进程级异常后，先构造 fallback result（`viz_env_status="generic"` + charts 全 `{pushed:false, reason:"generic:<Type>:<msg>"}`），**先 `print(json.dumps(result)); sys.stdout.flush()` 再 `return 2`** —— stdout 永远有合法 JSON，agent dumb copy 不依赖 LLM 合成
- 删除废弃 `--out_dir` 死参

### 改 agent-struct-exploration.yaml（SPEC §3.3/§3.4）

- `curator` + `finalize` `output_schema` 新增 `viz_status` 字段：
  - **必填**（`required: [env_status, charts]`）+ 外层 `additionalProperties: false` 严化
  - 内层 `charts` 用 `additionalProperties: {value schema}` 约束每个图名 value 为 `{pushed, reason}` required + 严化（D2）
  - `env_status` enum 含 `generic`（兜底合法）
  - **关键语义**：「必填」指 agent 必须写此字段；「失败值合法」指字段值可以是 `env_missing`/`generic` 等（sidecar 不阻断 + 缺字段 fail loud 双重保护）
- `finalize` step5 去 `|| true` 盲吞 → 捕获 stdout JSON dumb copy 写 `output.viz_status`（仅 exit 2 时 `|| true` 兜底）
- `finalize` step6 删 inline `python3 -c` 块 → 改 `viz_struct.py --mode compare` CLI 调用
- 新增 `finalize` step7：合并 step A/B 两份 stdout JSON 成 `output.viz_status`（`viz_env_status` → `env_status` rename + charts merge + env_status 取两边最坏值）
- `final_report.md` 顶部加 `## 可视化产出` 段（全成功列图 / 任一失败 ⚠️ + reason + 修复指引）

### 改 struct-curator/agent.md Step 4（SPEC §3.4）

- 去 `|| true` 盲吞 → dumb copy stdout JSON（`viz_env_status → env_status` rename，charts 原样透传）写 `output.viz_status`
- 输出 JSON 模板加 `viz_status` 字段示例

## 铁律守恒（SPEC §4）

| 铁律 | 守恒方式 |
|------|---------|
| `orca/chart/_render.py` 零改动 | env 仍只从 env 读（铁律 #2）；自加载发生在脚本层（调 `render_chart` 之前把 env 写进 `os.environ`） |
| `orca/iface/in_session/{chart_daemon,cli}.py` 零改动 | daemon/respawn/orca_env.sh 机制已就绪，本 SPEC 不引入新机制 |
| 依赖方向不破（schema→compile→exec→run→events→iface） | `_env.py` 仅 stdlib，`viz_struct import orca.chart._env` 合规（light-touch client） |
| sidecar 不阻断 | viz 失败值（`env_missing`/`generic` 等）合法产出，不阻断 `continue_loop`/`finalize`；**缺字段**才 fail |

## 失败路径矩阵（SPEC §5）

| 场景 | 自加载 | render_chart | `viz_status` | 阻断？ |
|---|---|---|---|---|
| 真 orca-run，env 已注 | no-op | 成功 | `env_status=ok`，全 pushed | 否 |
| in-session，env 缺，`orca_env.sh` 在 | 补 4 键成功 | 成功 | `env_loaded_from_file`，全 pushed | 否 |
| in-session，拆调用，`orca_env.sh` 在 | 脚本内补 | 成功 | `env_loaded_from_file` | 否 |
| `orca_env.sh` 找不到 | 返 {} | 不调 | `env_missing`，全 not pushed | 否（report ⚠️） |
| daemon 挂 | env 齐但连不上 | raise → `socket_unreachable` | 标注；next 推进时 respawn | 否 |
| 数据不足 | — | 脚本内跳过 | `data_insufficient` | 否（设计内） |
| `orca.chart` import 失败 | 降级不补 | 全跳过 | `import_failed` | 否 |
| 进程级异常 | — | — | `generic`（`_main` 兜底），`|| true` 不阻断 | 否 |

## 测试（SPEC §6）

| AC | 测试位置 | 覆盖 |
|----|---------|------|
| AC1a | `tests/chart/test_env.py` | ledger anchor + 不覆盖已存在 env + 不补非身份键 + render_chart 端到端 mock 验证 |
| AC1b | `tests/chart/test_env.py` | champions anchor（compare 模式用） |
| AC2 | `tests/chart/test_env.py` | 幂等 no-op |
| AC3 | `tests/chart/test_env.py` | 无文件 / 有文件无 SOCK 标志行 / 祖先同名文件不误匹配 / 注释行不匹配 / 空 SOCK 值不注入 |
| AC4 | `tests/workflows/test_viz_struct_robustness.py` | 5/5 ack 消息 + socket unreachable 双形态（EOF + refused）+ half-injection + generic + data_insufficient 双分支 |
| AC5a | `tests/workflows/test_viz_struct_robustness.py` | stdout 字段（happy / env_missing / env_loaded_from_file / import_failed / `_main` 兜底 generic）+ `--mode compare` compare_bar + happy path exit 0 |
| AC9 | `tests/iface/in_session/test_in_session_chart.py` | ORCA_NODE 漂移 invariant（4 节点状态机：bootstrap→starter→middle→finisher→终态不重写 + per-run 常量 RUN_ID/SOCK 不变） |

### 范围外（SPEC §6 / §7）

- **AC5b/AC8**（agent 转写 + e2e fake daemon）→ 纳入 `tests/e2e_redesign/` stage3 harness；stage3 暂不支持 fake chart_daemon / mock socket，**登记为后续**，不阻塞本 SPEC
- **bit-curve / viz_kd 迁移到 `load_run_env_from_artifacts`**（DRY 统一）→ SPEC §7 后续，本 SPEC 仅解 `agent-struct-exploration`

## 验证结果

- `tars validate workflows/agent-struct-exploration.yaml`：0 error
- Jinja StrictUndefined：全节点 OK（setup / finalize 含新增 step5/6/7 + dumb copy inline python）
- 回归测试：
  - `tests/chart/` + `tests/workflows/` + `tests/events/test_chart_ingestor.py` + `tests/iface/in_session/test_chart_daemon.py` + `tests/iface/in_session/test_in_session_chart.py`：**245 passed**
  - `tests/compile/` + `tests/e2e_redesign/`：**249 passed, 2 skipped**（kd-nas 受用户既有活跃 run 阻塞，与本改动无关）

## code-reviewer 两轮闭环

- **impl review**：0 🔴；4 🟡（R1 untracked 文件 / R2 env_status 合并不对称 / R3 空 champions latency=0 误导 / R4 half-injection 误归类）+ 6 🟢 全修或登记
- **coverage review**：0 🔴；5 🟡（AC4 ack-class 5/5 覆盖 / AC1a render_chart mock 验证 / data_insufficient 无有效点分支 / monkeypatch isolation / `_main` compare happy path）+ 6 🟢 全修或登记

## 偏离 SPEC 的决策（Rule 7 surface）

1. **half-injection 归类（R4）**：SPEC §3.1 KISS 决策「`_resolve_env_status` 只看 SOCK 单键」保留不变；但 `_classify_exc` 新增 half-injection 分支（`缺 ORCA_* env` → `env_missing`）兜底归类精度。理由：SPEC §3.1 KISS 是在「env 检查」层做简化（避免 4 键校验），但「reason 分类」是给 agent 看的归因，精度更高更好。两者不矛盾。
2. **空 champions → data_insufficient（R3）**：SPEC §3.4 未明示空 champions 行为。原 inline `python3 -c` 代码用 `champ_lat = last_champ.get('latency_ms', 0)` 退化为 0（latency 字段）。code-reviewer 指出「0 ms 是不可能的优秀值，比缺失更误导」。改为返 `data_insufficient`（与三图空数据同款语义），不推误导图。这是对原行为的修正，未改变 SPEC 意图（sidecar 不阻断）。
3. **`env_status` 合并取两边最坏值（R2）**：SPEC §3.3/§3.4 未明示 step A/B 两份 stdout JSON 的 `env_status` 合并方向。原代码（我的初稿）只取 step A 的值。code-reviewer 指出若 step A/B 状态分歧会语义反转。改为「任一非 ok 即报非 ok」（worst-of-two），更保守、更准确。

## 涉及文件

新增：
- `orca/chart/_env.py`
- `tests/chart/test_env.py`
- `tests/workflows/test_viz_struct_robustness.py`
- `docs/specs/2026-07-23-in-session-chart-robustness.md`
- `docs/releases/2026-07-23-in-session-chart-robustness.md`（本文件）

修改：
- `workflows/agents/_struct_scripts/viz_struct.py`
- `workflows/agent-struct-exploration.yaml`
- `workflows/agents/struct-curator/agent.md`
- `tests/iface/in_session/test_in_session_chart.py`（+AC9 测试）
- `tests/workflows/test_struct_kd_p7.py`（同步 mock setup）
