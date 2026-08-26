# host_session 绑定防串台 —— 设计草稿（v2，tape-only）

> **状态**：Draft v2（2026-07-17），经 spec-reviewer 对抗评审（13 挑战全闭环）+ 用户铁律裁决，**采 tape-only 方案**。待 coder-agent 实现。
> **关联**：[`in-session-entry-and-simplification.md`](in-session-entry-and-simplification.md) v5 §4.4（nudge）/ §7.2（marker 精简）。
> **v1→v2 关键变化**：host_session **只存 tape**（`workflow_started.data.host_session`），**marker 不加字段**（同 `yaml_path` tape-only 先例）。消解评审 C3/C4/C7/C11。+ per-session 限流（C1）+ opencode 不合并策略（C5）+ emit 真链（C6）+ 验收钉值/实证（C9/C10）。
> **范围**：CC 为主落地（env 开箱零配置）；opencode 侧写契约，**plugin 注入 v1 不可行则 orca.ts 过滤改动不合并**（保留现状，用户已允许可暂缓）。

---

## 0. 目标

nudge（CC Stop-hook / opencode `session.idle`）只对**当前 session 自己启动的**活跃 run 提醒，不再无差别广播到任意空闲 session。

---

## 1. 问题（已复现 + spike 坐实）

### 1.1 现象
本 CC session idle 时被 nudge 提醒推进**别的 session** 的 run（`agent-struct-exploration`、`nas-hp-search-20260717-001119-561ed6`，均为跨 wf 串台）。

### 1.2 根因（数据 + 扫描 + 限流 三层）
- **数据层**：`workflow_started.data` 无 `host_session`（`lifecycle.py:74-80`），run 无归属信息。
- **扫描层**：`cc_nudge.sh:53-73` glob + `orca.ts:75-89` `listActiveRuns` 扫全部活跃 marker，**不按 session 过滤**。
- **限流层**（评审 C1）：`cc_nudge.sh:35` `STATE="runs/.orca-nudge-cc"` + `orca.ts:71` `NUDGE_FILE` 是**全局单文件**，跨 session 共享 → A nudge 后 60s 内 B 即使有自己的 run 也不被提醒。

### 1.3 session id 可获取性（2026-07-17 spike 坐实）
| 宿主 | bash 子进程能否拿宿主 session id | 来源 |
|---|---|---|
| **CC** | ✅ **开箱** | env `CLAUDE_CODE_SESSION_ID`（CC 注入所有 bash 子进程；spike 实测 `f449e4cd-...`） |
| **opencode** | ❌ **不注入** | bash 子进程只 `OPENCODE=1`/`OPENCODE_PID`；session id `ses_xxx` 仅日志/sqlite。**需 plugin 注入** |

---

## 2. 方案（tape-only 核心）

### 2.1 归属注入（CLI bootstrap 读 env）
`orca <wf> --inputs`（bootstrap）读宿主 session id，优先级：
```
ORCA_HOST_SESSION_ID  >  CLAUDE_CODE_SESSION_ID  >  None
```
- **CC**：零配置（fallback 命中 `CLAUDE_CODE_SESSION_ID`）。
- **opencode**：需 `orca.ts` plugin 注入 `ORCA_HOST_SESSION_ID = <session.id>`；**注入路径 v1 不可行 → orca.ts 过滤改动不合并**（§2.5）。

### 2.2 单一持久真相源 = tape（marker 不加字段）
- **唯一真相源**：tape `workflow_started.data.host_session: str | null`（bootstrap emit 时写入，append-only，永不变更）。
- **marker 零改动**：`ActivationMarker` 仍是 `{run_id, model, no_output_count}`（同 `yaml_path` 先例——tape-only，marker 不复存）。nudge 需要归属时**读 tape 首行派生**（同 `_read_workflow_yaml_path` `cli.py:862-883` 模式）。
- **无 desync 向量**：host_session 只一处（tape），不存在 tape/marker 漂移。`test_marker_only_three_fields` 不破。

### 2.3 nudge 过滤（读 tape 首行取归属）
- **CC**（`cc_nudge.sh`）：`current = os.environ.get("ORCA_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")`；glob marker 拿 run_id → 对每个 run 读 `runs/<run_id>.jsonl` 首条 `workflow_started.data.host_session` → 仅收 `== current` 的。
- **opencode**（`orca.ts`）：`current = event.properties.sessionID`；`listActiveRuns` 扫 marker 拿 run_id → 读 tape 首行 host_session → 过滤。
- **不改 stdin**（cc_nudge.sh heredoc 占 stdin，env 已够）。

### 2.4 per-session 限流（评审 C1）
- **CC**：`STATE = f"runs/.orca-nudge-cc-{current}"`（按 session 分键）。
- **opencode**：`NUDGE_FILE = f"runs/.orca-nudge-{sessionID}.json"`（按 session 分键）。
- 效果：A nudge 不再抑制 B 的 nudge（各自独立 60s 窗口）。

### 2.5 边界兼容（fail-safe）
| 情形 | 处理 |
|---|---|
| `host_session == current` | nudge |
| `host_session != current` | 跳过（别的 session 的 run） |
| `host_session is None`（手 CLI 起无 env / opencode 未注入） | 跳过（无法证明归属） |
| tape 首行读失败/缺 host_session | 跳过（fail-safe，同 `_find_active_run_for_wf`） |
| 取不到 `current`（env 都无） | 放行（不 block）；**且若有活跃 marker → warn**（区分「手 CLI」与「env 注入 bug」，评审 C10） |

---

## 3. 单一真相源论证（用户铁律，tape-only 后无懈可击）

1. **host_session 仅存 tape**（`workflow_started.data`），**marker 不复存** → 无 desync 向量。与 v3 §7.2 删字段原则（「tape 里有就别在 marker 复存」）完全对齐。
2. **同 `yaml_path` tape-only 先例**：`yaml_path`（`lifecycle.py:81-82`）只存 tape，marker 读 tape 派生（`cli.py:862-883`）。host_session 照此办理——**不是新模式，是既有模式的复用**。
3. v1 反例 `model`（marker-only，不在 tape）是另一类（marker 独占运行态），不适用 host_session（host_session 是归属，需持久 + nudge 读）。
4. **「破坏单一真相源」定义**（评审 C13）：= 两路独立采集、可发散的值。host_session 单路（env → tape），不构成破坏。无需停止报告。

---

## 4. 契约（逐字，coder-agent 依据）

### 4.1 emit 真链（评审 C6 纠正：不在 cli.py emit）
`workflow_started` 在 `step.py:319` 经 `make_workflow_started`（`lifecycle.py:55-83`）emit。加 host_session 需穿 3 点：
- `orca/run/lifecycle.py` `make_workflow_started(...)` 加参 `host_session: str | None = None`，写入 `data["host_session"]`（与 `yaml_path` 同 data 区，`:74-82`）。
- `orca/run/step.py` `advance_step(...)` 加参 `host_session`，仅在 `state.status=="pending"`（首节点，`step.py:315`）分支透传给 `make_workflow_started`；next 路径（非 pending）不传（不重发 workflow_started）。
- `orca/iface/in_session/cli.py` bootstrap（`:662` 调 advance_step 处）传 `host_session=_host_session_from_env()`；**marker 写入行（`:684`）不变**。

### 4.2 env helper（cli.py 新增）
```python
def _host_session_from_env() -> str | None:
    return os.environ.get("ORCA_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
```

### 4.3 marker.py —— 零改动
`ActivationMarker` 不加字段（tape-only）。`read_marker`/`write_marker`/`clear_marker` 不动。

### 4.4 cc_nudge.sh（读 tape 首行 + per-session 限流）
```python
def _host_session_from_tape(run_id: str) -> str | None:
    """读 runs/<run_id>.jsonl 首条 workflow_started.data.host_session（同 yaml_path 派生模式）。"""
    try:
        with open(f"runs/{run_id}.jsonl", encoding="utf-8") as f:
            for line in f:                       # 首条即 workflow_started
                o = json.loads(line)
                if o.get("type") == "workflow_started":
                    return o.get("data", {}).get("host_session")
                break                            # 只看首条
    except (OSError, json.JSONDecodeError):
        return None                              # fail-safe
    return None

def _scan_my_active_run_ids(current: str) -> list[str]:
    ids = []
    for path in sorted(glob.glob("runs/orca-*.json")):
        data = json.load(open(path))             # fail loud 不变
        rid = str(data.get("run_id"))
        if rid and _host_session_from_tape(rid) == current:
            ids.append(rid)
    return ids

def main():
    current = os.environ.get("ORCA_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not current:
        # 评审 C10：区分「手 CLI（预期）」与「Stop-hook env 坏（bug）」
        if glob.glob("runs/orca-*.json"):
            sys.stderr.write("orca-nudge: 无 host session env 但有活跃 marker（手动 CLI 或 env 注入异常）\n")
        return 0
    state = f"runs/.orca-nudge-cc-{current}"     # per-session 限流（C1）
    if now - _read_throttle(state) < THROTTLE_SEC: return 0
    ids = _scan_my_active_run_ids(current)
    if not ids: return 0
    ...  # block 文案不变
```

### 4.5 orca.ts（读 tape 首行 + per-session 限流 + 不合并策略）
- `listActiveRuns(hostSession)`：扫 marker 拿 run_id → 读 `runs/<run_id>.jsonl` 首行 host_session → 仅收 `== hostSession`。
- `NUDGE_FILE` 按 `sessionID` 分键（per-session 限流）。
- **Marker interface（`orca.ts:111-121`）不加 host_session**（tape-only，不读 marker 归属；评审 C7 消解）。
- **不合并条件**（评审 C5）：若 opencode plugin 注入 `ORCA_HOST_SESSION_ID` v1 不可行 → `listActiveRuns` 过滤改动**不合并**，保留现状（未过滤，所有 run 都 nudge，串台留作 opencode open issue）。coder-agent 实现时先确认 plugin 注入可行性。

### 4.6 公共 env 契约（SPEC-B 复用）
| env | 含义 | CC | opencode |
|---|---|---|---|
| `ORCA_HOST_SESSION_ID` | 宿主 session id（标准名） | 可选（fallback CC env） | plugin 注入（v1 不可行则 orca.ts 不合并） |
| `CLAUDE_CODE_SESSION_ID` | CC 自带 | CC 注入（fallback 源） | — |

---

## 5. 验收标准（钉值 + 实证，评审 C9/C10）

1. **CC 多 session 不串台**：起 2 个 CC session，**各启动不同 workflow**（同 wf 会被 m12 dupe-check 拒，评审 C2）；session A idle → nudge 只提醒 A 的 run，不提 B 的 run（test-agent E2E 真机证）。
2. **per-session 限流**（C1）：A nudge 后 60s 内 B idle → B 仍被提醒自己的 run（不被 A 的限流抑制）。
3. **tape 真相源钉值**（C9）：`tape.workflow_started.data.host_session == 启动时 $CLAUDE_CODE_SESSION_ID`（非硬编码/非 None）。
4. **Stop-hook env 实证**（C10）：真实 CC Stop-hook 触发时（非手动 shell），`cc_nudge.sh` 观察到非 None 的 `CLAUDE_CODE_SESSION_ID`；current=None 且有活跃 marker → stderr warn。
5. **边界**：手 CLI 起 run（无 env）→ 不被任何 session nudge（fail-safe）。
6. **作用域**（C12）：host_session 仅作用 nudge；`status`/`open`/`next`/`stop` 行为不变（显式用户意图，非鉴权边界）。
7. **回归**：现有 in-session 测试 0 新回归；`test_marker_only_three_fields` 仍绿（marker 未改）；`tars validate` 0 error。
8. **opencode**（非阻塞）：plugin 注入可行 → orca.ts 过滤 + per-session 限流生效；不可行 → 不合并，登记 open issue。

---

## 6. 风险 / 待定

| # | 项 | 处理 |
|---|---|---|
| 1 | CC Stop-hook 子进程是否真有 `CLAUDE_CODE_SESSION_ID` | spike 已证 bash 子进程有；Stop-hook 同 env 链；验收 #4 真机复测（评审 C10 无法独立证实） |
| 2 | opencode plugin 能否注入 env 到 bash 子进程 | coder-agent 实现时确认；不可行 → orca.ts 不合并（C5） |
| 3 | nudge 读 tape 首行的成本 | O(1) 读首行，可忽略；marker 仍 glob（活跃判断） |
| 4 | tape 首行非 workflow_started（异常 tape） | `_host_session_from_tape` 只看首条 + fail-safe 返 None |

---

## 7. 决策清单（v2 冻结）

1. **host_session = tape-only**（`workflow_started.data`），marker 不加字段（同 yaml_path 先例）。
2. **env 优先级**：`ORCA_HOST_SESSION_ID` > `CLAUDE_CODE_SESSION_ID` > None。
3. **CC 零配置**；**opencode 需 plugin 注入，v1 不可行则 orca.ts 不合并**。
4. **nudge 读 tape 首行**取归属（marker 仅活跃判断 + run_id 句柄）。
5. **per-session 限流**（cc_nudge.sh / orca.ts 状态文件按 session 分键）。
6. **emit 真链**：lifecycle.make_workflow_started ← step.advance_step ← cli.bootstrap（不在 cli.py emit）。
7. **fail-safe**：host_session 不等/None/读失败 → 跳过；无 current → 放行 + warn。
8. **作用域**：host_session 仅 nudge；status/open/next/stop 不变。
9. **不破坏单一真相源**（§3）；「破坏」= 两路独立采集可发散（host_session 单路）。

---

## 附：评审闭环映射（spec-reviewer 13 挑战）
C1 全局限流→§2.4 per-session；C2 同 wf→§5.1 不同 wf；C3 测试 3 字段→tape-only 消解；C4 next RMW 覆盖→tape-only 消解（marker 不存）；C5 opencode 静默死→§2.5/§4.5 不合并策略；C6 emit 位置→§4.1 真链；C7 TS Marker→tape-only 消解；C8 存储模式→§2 tape-only（用户铁律）；C9 钉值→§5.3；C10 Stop-hook env→§5.4；C11 非原子→tape-only 消解；C12 status/open→§5.6；C13 停止 criteria→§3.4 定义。
