# In-Session 加固与性能 SPEC（v3）

> **状态**：Draft v4.1（2026-07-19）。**架构原则（用户定）**：orca 是管理者（状态/决策/compliance/route 全归它），主 session 只调度（派子代理/传 output）—— 不过度设计、不跨层耦合。v4.1 据此简化：O3（主 session 不反应 compliance，仅 status 可观测）、F1（复用 next 重发，删 prompt_file 零新字段）、D2（helper 简单 try/except）、D4（next 一处判断，删三态辅助）。P1–P6 全可开工。
> **v2→v3 变化**：
> - **F1 grace 重设计**（闭环 v2-B1/B2/B4）：删时间阈值（`last_next_at`/`RESUME_GRACE_S`），改用 **host_session 信号**（review 推荐 B）—— marker 加 `last_host_session`，跨 session = resume 豁免，同 session 连续无 output = 摸鱼照计。无阈值 trade-off。
> - **铁律 5.1 修正**（闭环 v2-B3）：error_kind 登记到 **raise 发生层** 的 ERR_*（`step.py` for advance_step / `cli.py` for bootstrap），非假设全在 step.py。F3 `inputs_validation_error` 登记到 cli.py 层。
> - D3 措辞修正（v2-M3 虚假闭环）：明示"覆盖守护死亡，不覆盖存活但持续失败（留 §8#4）"。
> - D4 API 形态 pick（v2-M2）：`read_marker` 仍返 `ActivationMarker | None`（4 调用点零改动）+ 新增 `read_marker_status(path)` 三态辅助。
> - D5 tail 精确化（v2-M8）：末尾 50 行；找不到终态 → `tape_unknown`（不 hint rm）。
> - O1a helper 位置（v2-M11）：`_replay_state_and_inputs` 落 `events/replay.py`（与 reducer 同文件）。
> - O3 stuck（v2-M4）：`stuck = no_output_count >= 1`。
> - P1 标语修正（v2-M5）：明示 O2/F3/O4 同改 bootstrap()，同一 coder 按序；F3 碰 step.py 与 P3 串行。
> - F3 不引入新依赖（v2-M10）：手写 TYPE_MAP，type 不在白名单 → pass-through。
> - AC 补全反例（v2 各 major）+ §8#6 v5/测试改动清单。
> **前置**：[`in-session-entry-and-simplification.md`](in-session-entry-and-simplification.md) v5（**决策 11 由本 SPEC 修订**，见 §4 F1）。

---

## 0. 目标

在**不改 workflow 决策逻辑、不改 emit 事件序列、不动 tape 唯一真相源**的前提下，修 in-session 路径的 fail-loud/孤儿状态缺陷、降低每次 `orca next` 的固定性能税、补 resume 编排缺口、把反复重演的同类 bug 用守门/helper 根治。

## 1. 约束铁律（spec-reviewer 逐条核）

1. **不影响功能**：既有可观测行为不变（返回 shape 只增不减字段）。
2. **不影响 workflow 逻辑**：`advance_step` 决策三分支、route 求值、emit 事件序列一字不改。**唯一例外**：F1 的 compliance 守卫（用户批准）。
3. **唯一真相源**：tape 仍是唯一读路径；marker 仍是 run 句柄。**3.1**：派生状态文件（doctor probe 等）必须明示生命周期，不得取代 tape。
4. **emit 序列稳定**：不新增/改 EventType（F2 因此 defer）。
5. **单一接口**：7 命令不增不减；新能力靠既有命令加 flag/字段。
6. **fail loud**：让静默吞/撒谎路径显式告警，不加宽 except。
7. **in-session 相关**：不碰 `tars` 后端、web reducer。
8. **5.1 error_kind 闭合**：新 error_kind 必须显式登记到 **共享 `orca/run/_errors.py`**（新设，单一真相源 for error_kind taxonomy）+ SKILL.md 错误信封段。`step.py` 现有 `ERR_*` 保留（YAGNI 不强制迁移）；F3 `inputs_validation_error` 登记到 `_errors.py`。

---

## 2. 缺陷（D1–D5）

### D1 · `stop` emit 失败 → 孤儿 run
- **依据**：`cli.py:1359-1378`。emit 抛错时 finally 仍 `clear_marker`，但终态没落 tape。
- **方案**：best-effort 落终态（先 `workflow_cancelled`，失败退化 `workflow_failed(internal_error, reason="cancel_emit_failed: ...")`，**不扩 taxonomy**）；确认终态落 tape 才 `clear_marker`；落不了 → 保留 marker + reply `error_kind:internal_error` + 非 0 退出。死循环出口：D5 doctor `orphan_markers` hint 给 `rm runs/orca-<run_id>.json`。
- **架构影响**：零。**工作量 S**。**与 D2 同包（P4）**。

### D2 · `apply_step_result` 异常裸崩 → 留 running tape 无终态
- **依据**：`daemon.py:117-140` + `cli.py:998-1031`。根因 = `apply_step_result`（`emit_batch` + memory 写）抛非 `InSessionError` 时裸崩，无 workflow_failed 落 tape（**非**半写 tape —— 半写由 `_truncate_trailing_partial` 自愈）。
- **方案**：helper `_safe_apply_or_fail(bus, result, ...)` 落 `_step_io.py`（DRY，daemon + cli 两处复用）。**简单 try/except**：`try: await apply_step_result(...) except OSError: return await fail_in_session(bus, InSessionError(internal_error))`。
  - **partial-success 免特殊处理**：`apply_step_result` 内部 `emit_batch` 在前、memory 写在后且 best-effort（失败只 log 不重抛；memory 是派生缓存）→ emit_batch 成功即整体成功，helper 无需 `on_emit_success` 回调或状态机。
  - `fail_in_session` 自身失败（emit workflow_failed 又抛）→ 返纯错误信封 + log，不二次降级。
  - 外层契约不变：`_next_in_critical_section` 返 `(StepResult, bool)` 不变；cli/daemon 外层不改。
- **AC**：mock `apply_step_result` 抛 OSError → daemon/cli next 都 emit workflow_failed 落 tape + 返信封；**partial success**（emit_batch ok + memory fail）→ 不 emit workflow_failed，run 状态正确；`fail_in_session` 失败 → 纯信封；grep `emit_batch` 实现非循环 emit（原子批）。
- **架构影响**：小（新增 helper）。**工作量 M**。**与 D1 同包（P4）**。

### D3 · sidechain 守护死亡无可见信号（探针；不覆盖持续失败）
- **依据**：`sidechain_daemon.py:178-183` 永重试有意设计。
- **方案**：doctor 加可选 check（hard=False）调 `_sidechain_daemon_alive(run_id)` 探针 + 读日志/hint。**覆盖守护死亡；不覆盖守护存活但持续 iterate 失败**（留 §8#4，YAGNI 不做 socket 查询）。
- **AC**：守护存活 → pass；守护死 → degraded + hint。**明示不覆盖持续失败**（§8#4）。
- **架构影响**：小。**工作量 S**。**入 P1**。

### D4 · `read_marker` 损坏与缺失不分（next 一处判断，不动 marker 契约）
- **依据**：`marker.py:71-102` 有意容忍半写契约（返 None = passthrough）；`cli.py:1158-1161` 见 None 返 no-marker 无 prompt。
- **方案（简化，v4.1）**：`read_marker` 契约**不动**（仍返 `ActivationMarker | None`，4 调用点零改动，**不新增辅助函数/三态枚举**）。**仅 next 路径**在 `read_marker` 返 None 时用 `mpath.exists()` 区分：存在但解析失败 = corrupt → reply `error_kind:state_corrupt` + hint；不存在 = missing → 现状（no-marker + hint「需 bootstrap」）。损坏罕见，next 一处判断够，不值得三态 API。
- **AC**：损坏 → next 返 state_corrupt + hint；真 missing 零回归；`.tmp` 残文件不在 final path 不误判；`read_marker` 契约 + 4 调用点零改动。
- **架构影响**：零（next 一处加 exists 判断）。**工作量 S**。**与 D5 同包（P2）**。

### D5 · doctor 不检测孤儿 marker + 三处注释撒谎
- **依据**：`cli.py:641/1261/1265` 注释撒谎；doctor（1573-1704）无检测。
- **方案**：doctor 加 `orphan_markers` check（hard=False）：glob `runs/orca-*.json`，对每个 marker **tail 读末尾 50 行**（不全 replay）判 `tape_terminated / tape_missing / tape_unknown / marker_corrupt`。
  - `tape_terminated` / `tape_missing` → hint `rm runs/orca-<run_id>.json`；
  - `tape_unknown`（tail 找不到终态事件）→ **不 hint rm**（可能正常在跑，标记 unknown 供人判断）；
  - `marker_corrupt` → 同 D4 next 路径 `mpath.exists()` 判定，hint rm。
  - glob 命中但 `read_marker` 返 None → 跳过不崩。
  - 删三处错注释。
- **AC**：孤儿（终态/缺）→ 列出 + rm hint；tail 模式；unknown 不 rm；三注释删除。
- **架构影响**：零。**工作量 S–M**。**与 D4 同包（P2）**。

---

## 3. 优化（O1–O5）

### O1a · `advance_step` 内部合并两次 tape 遍历
- **依据**：`step.py:323 replay_state` + `step.py:328 _inputs_from_tape` = 两次全遍历；`replay.py:58-66` reducer 不存 inputs（只存 workflow_name）。
- **方案（选 C）**：新增 `_replay_state_and_inputs(tape) -> (RunState, dict)` 落 **`events/replay.py`**（与 reducer 同文件），单次遍历既跑 reducer 逻辑又抽 `workflow_started.data.inputs`。`advance_step` 调它；`_inputs_from_tape` 改为调它取 inputs（或 advance_step 内联）。`replay_state` 对外 API 保留。结果与改前逐字相等。
- **AC**：`advance_step` 单次调用 tape 遍历 2→1（计数断言）；state+inputs snapshot 逐字相等；grep 确认 `_inputs_from_tape` 仅 `advance_step` 调用（无其他调用方被波及）。
- **架构影响**：中（改 advance_step 读路径内部）。**工作量 M**。**单独包 P3**。

> O1b（wf 缓存）/ O1c（Tape resume）：defer（§8）。

### O2 · bootstrap 锁临界区缩小
- **依据**：`cli.py:807-950` 全局锁跨 daemon spawn + 5s socket wait。
- **方案**：锁只包 dupe check + gen run_id + advance+emit + write_marker；`_write_orca_env` + `_spawn_*_daemon` + `_wait_for_sock` 移锁外。
- **AC**：连续 3 bootstrap 耗时下降；dupe-check 不变量仍成立。
- **架构影响**：零。**工作量 S**。**入 P1**（与 F3/O4 同改 bootstrap()，同一 coder 按序）。

### O3 · `no_output_count` 可观测（仅 status 透出，主 session 不反应）
- **依据**：marker 有计数但 status 不透出，用户看不到 compliance 红线进度。
- **原则（v4.1）**：compliance 是 **orca 自我保护**（防无限空转，到 limit 自己 fail）；**主 session 调度固定**（拿 prompt→派子代理→next output，不因计数改变行为）→ 不让主 session/SKILL 参与 compliance 管理。
- **方案**：仅 `orca status --run-id X` 详情加 `no_output_count`（raw 透出供用户观测）。**删** next reply compliance_warning / stuck 语义 / SKILL 教反应（compliance 偏窄见 §8#5，stuck 不准 + 主 session 不该反应）。
- **AC**：status --run-id 含 no_output_count；next reply 不加 compliance 字段（零回归）。
- **架构影响**：零。**工作量 S**。**入 P1**。

### O4 · busy 信封加 `retry_after_ms`（不重发 prompt）
- **依据**：`cli.py:841/986/1361` 三处 busy 无 prompt。
- **方案**：busy reply 加 `retry_after_ms:500`；drive 协议 + SKILL 补"reason=busy 等 retry_after_ms 重试同一 next，不重派子代理"。不重发 prompt（避免 advance_step 不持锁调用契约冲突）。
- **AC**：busy reply 含 retry_after_ms；不重发 prompt。
- **架构影响**：零。**工作量 S**。**入 P1**（与 O2/F3 同 bootstrap，同 coder 按序；O4 还改 next:986/stop:1361）。

### O5 · defer（锁竞争窗口小，§8）

---

## 4. 新功能（F1 破约束 / F2 defer / F3）

### F1 · TARS resume（run_id + SKILL，零 host_session）✅ 不破约束（v4，对齐用户洞察）
- **痛点**：session 断后新 session 不知有半完成 run；SKILL 无 resume 段。
- **关键认知（v4 修正，弃 host_session 方案）**：resume 是 **run 级别**的事，用 **run_id** 管（status/next 现成），**与 host_session 无关**。后台（tape + marker）**已知执行到哪个节点**（`replay_state` → `current_node`）。v2/v3 的 host_session 豁免 compliance 是**过度设计** —— 解的是伪问题「如何豁免 compliance 对 resume 的冤枉」，而真问题是「SKILL 不教 resume 流程」。把流程教对（status → prompt → 子代理 → next **with output**），resume 主路径**根本不触发 compliance**（compliance 只在"无 output next"时 +1）。
- **方案**：
  - `orca status`（无参）列活跃 run 加 `resumable: true`（marker 在即 resumable）+ 已有 current_node。
  - resume **复用 `advance_step:390-402` 现成的 idempotent-replay**：`orca next --run-id X`（无 output）重发 current_node 的 prompt。**零新字段**（不透 prompt_file —— 避免 status 加字段 + 子代理读文件的复杂度；prompt 本就由 orca 经 next 重发，主 session 拿到即派子代理）。
  - **SKILL.md 加 resume 段**：新 session 启动 → `orca status`（无参）→ 看到 resumable run X、current_node=Y → `orca next --run-id X`（无 output，idempotent 重发 Y 的 prompt）→ 派子代理跑 Y → 产出 → `orca next --run-id X --output '<产出>'`（带 output 正常推进）。
  - **不动 compliance / marker / host_session**。
  - 补 spec 断链：新建 `docs/specs/agent-interrupt-design-draft.md` 占位（in-session resume = F1 落地；engine interrupt 仍 TBD），修 CURRENT 引用。
  - SKILL 源改 `orca/skills/tars/SKILL.md`；**已 install 旧副本需用户重跑 `tars install`**（install 经 `iface/cli/install_cmds.py` 分发）。
- **AC**：status 无参列 `resumable`；SKILL 含 resume 段（status → next 无 output 重发 → 子代理 → next output）；占位 spec 建立；**marker 仍 3 字段（不动）、零新字段**。
- **compliance 语义（独立 issue，不绑架 F1，留 §8）**：`no_output_count` 把所有"无 output next"（含合法 idempotent 重发 / 丢 prompt 重发 / resume fallback）一律算 +1，语义偏窄。F1 主路径绕过；仅 fallback 模式 +1（单次 resume 不 fail，需连续 3 次无 output 才 fail = 反复断连且每次断在派子代理前，极罕见）。是否重设计 compliance 留独立 issue。
- **影响**：✅ **不破 v5 决策 11**（marker 3 字段不动）；✅ 不改 workflow 决策/emit 序列；✅ 单一接口；✅ tape 仍唯一真相源；✅ **零 host_session、零 spike**。
- **架构影响**：小（status 加字段 + SKILL）。**工作量 S–M**。**碰 cli.py status（入 cli.py 串行组）+ SKILL**。

### F2 · `next --retry --feedback` ❌ defer（用户决策）
- 撞约束 4/5 + workflow 终态逻辑。移 §8，需独立 ADR。本期 workaround：output_schema 放宽 + prompt 强约束格式。

### F3 · bootstrap 期 inputs validate（向后兼容，无新依赖）
- **依据**：bootstrap（cli.py:779/792）不校验 inputs。
- **方案（闭环 v2-B3/M10）**：bootstrap 带 `--inputs` 时，用 `inputs_schema_list(wf)` 校验：
  - **手写 TYPE_MAP（isinstance），不引入 jsonschema 依赖**；type 不在白名单 → pass-through（不校验）；
  - 仅对**显式声明 type** 的字段校验类型 + 必填；未声明 type → pass-through（旧 wf loose-typed 零回归）；
  - `[default]`/`[advanced]` 省略不触发 required；
  - 不符 → fail loud `error_kind=inputs_validation_error`（**新 error_kind，按铁律 5.1 登记到共享 `orca/run/_errors.py` + SKILL.md**）+ 字段定位。
- **AC**：错类型/缺必填（显式 type）→ inputs_validation_error + 定位；旧 wf（无 type 声明）零回归；default/advanced 省略不触发 required；无新依赖（grep 无 jsonschema import）。
- **架构影响**：零。**工作量 S–M**。**入 P1**（与 O2/O4 同 bootstrap，同 coder 按序；F3 碰 step.py ERR_* 与 P3 O1a 串行）。

---

## 5. 系统性债（S1/S2/S7/S9）

### S1 · adapter contract-test 黄金集
- **方案**：fixture 加 `source_version`（pydantic schema 固定字段集，CI 逐字段比对）；版本 drift fail loud。放 `tests/iface/in_session/contract/`。
- **AC**：新 adapter 对真 fixture 跑通（事件 1:1 进 tape）；fixture schema drift fail loud。
- **工作量 M**。**单独包 P6**。

### S2 · CI 守门：SKILL.md flag ↔ CLI `--help`
- **方案**：markdown AST 解析 code fence，仅扫 `orca <cmd> ...` 行 flag；范围 = `orca/skills/tars/SKILL.md`（SPEC md 不扫）。断言 ⊆ `--help` 全输出。
- **工作量 S**。**入 P1**。

### S7 · tape multi-byte read helper 抽公共
- **方案**：抽 `events/tape.py` helper，三处替换（`_FlockSafeTape._read_max_seq_from_disk` / `_watch_terminal` / `sidechain_ingestor._derive_current_node`）。B2 回归守住。
- **工作量 S**。**入 P1**。

### S9 · detached daemon liveness helper 抽公共
- **方案**：抽 `iface/in_session/_daemon_liveness.py`（connect-probe / pidfile+cmdline），两守护复用。
- **工作量 S**。**入 P1**。

---

## 6. 工作量分组 + 任务分配

> 原则：**小合集一个 coder 一次做**；**关键/大项单独 coder**。所有包**串行**（都碰 cli.py）。

| 包 | 条目 | 工作量 | 分配 | 依赖/注意 |
|---|---|---|---|---|
| **P1（小合集）** | S2 + S7 + S9 + O2 + O3 + O4 + D3 + F3 | 8×S | 一个 coder 一次做 | O2/F3/O4@841 **同改 bootstrap()/next/stop 函数体，同 coder 按序**；F3 在 cli.py 层登记 ERR_*（**不碰 step.py**） |
| **P2** | D4 + D5（read_marker 三态 + doctor orphan，合并 commit） | S–M | 单独 coder | 改 marker.py + doctor |
| **P3** | O1a（advance_step 内部 fold） | M | 单独 coder（关键性能） | 改 events/replay.py + step.py + orchestrator.py；**须在 P1 F3 之前**（F3 用 step.py ERR_*） |
| **P4** | D1 + D2（失败路径统一） | M | 单独 coder（关键健壮性） | P2 先合并（read_marker 契约） |
| **P5** | F1（resume：status 加 resumable + SKILL resume 段 + 占位 spec，**零新字段、不破约束**） | S | 单独 coder | cli.py 串行组（P4 后） |
| **P6** | S1（contract-test 黄金集） | M | 单独 coder | 独立 |
| **defer** | F2 / O1b / O1c / O5 | — | 不本期 | §8 |

**依赖顺序（按文件冲突）**：**P3**（`events/replay.py` + `step.py` + `orchestrator.py`，**不碰 cli.py**）可与 P1 **并行**；**P1/P2/P4/P5 都碰 cli.py → 串行 P1→P2→P4→P5**；**P6**（tests）独立可任意时点。F3 在 cli.py 层登记 ERR_*（不碰 step.py，Q5 修正 v3 误述）。每包独立 commit + 自带 code-reviewer + test-agent。

---

## 7. 验收标准（v3，含反例）

- **D1**：mock stop emit 抛错 → marker 未清 + run 仍 status 可见 + reply `internal_error`；退化也失败 → marker 保留 + D5 列出 + rm hint；正常 stop 零回归。
- **D2**：mock apply_step_result 抛 OSError → daemon/cli next 都 emit workflow_failed 落 tape + 返信封；partial success（emit_batch ok + memory fail）→ 不 emit workflow_failed；fail_in_session 失败 → 纯信封；grep emit_batch 非循环。
- **D3**：守护存活 → pass；守护死 → degraded + hint；**不覆盖持续失败（§8#4）**。
- **D4**：损坏 → next 返 state_corrupt + hint；真 missing 零回归；.tmp 残文件不误判；4 调用点零改动。
- **D5**：孤儿（终态/缺）→ 列出 + rm hint；unknown 不 rm；glob None 跳过；三注释删除。
- **O1a**：tape 遍历 2→1；snapshot 逐字相等；_inputs_from_tape 仅 advance_step 调用。
- **O2**：连续 3 bootstrap 耗时下降；dupe-check 不变量成立。
- **O3**：status --run-id 含 no_output_count；next reply 不加 compliance 字段（零回归）。
- **O4**：busy reply 含 retry_after_ms；不重发 prompt。
- **F1**：marker 4 字段（last_host_session）；同 session 无 output next → count==1；resume（新 host_session）第一次 → count==0 + last_host_session 更新；resume 后同 session 连续 → 照计；status resumable；SKILL resume 段；占位 spec；v5 决策 11 修订。
- **F3**：错类型/缺必填（显式 type）→ inputs_validation_error + 定位；旧 wf 零回归；无新依赖。
- **S1**：adapter 对真 fixture 跑通；fixture schema drift fail loud。
- **S2**：SKILL.md flag ⊆ --help；SPEC md 不扫。
- **S7**：三处替换为 helper，B2 回归全过。
- **S9**：两守护复用 helper，respawn 零回归。
- **铁律 AC**：无新裸 sys.exit/宽 except pass/2>/dev/null||true；advance_step emit snapshot 不变（O1a 结果等价）；**无未登记 error_kind**（F3 inputs_validation_error 登记 cli.py 层）；7 命令不变；marker 字段=4（F1 后）；tape 仍唯一真相源。

---

## 8. 风险 / 待定

| # | 项 | 状态 |
|---|---|---|
| 1 | **F2 retry** | defer，需 ADR（约束 4 加 node_retry 事件 vs 决策 11 再扩 marker） |
| 2 | **O1b/O1c** | defer（进程级缓存对 next 无效 / tape.py 风险） |
| 3 | **O5** | defer（锁竞争窗口小） |
| 4 | **D3 持续失败** | 不覆盖（守护存活但持续 iterate 失败）；YAGNI 不做 socket 查询，靠 daemon log + 用户排查 |
| 5 | **compliance 语义偏窄**（独立 issue，不绑架 F1） | `no_output_count` 把所有"无 output next"（含合法 idempotent 重发 / resume fallback）一律算 +1；F1 主路径（status + output next）绕过；是否重设计 compliance（区分"主动重发"vs"子代理空回"）留独立 issue |
| 6 | **F1 改动清单**（实现时）：`cli.py status`（加 `resumable` 字段）+ `orca/skills/tars/SKILL.md`（resume 段）+ 新建 `docs/specs/agent-interrupt-design-draft.md` 占位 + 修 CURRENT 引用；**marker / v5 决策 11 不动、零 prompt_file**；旧 install 副本重跑 `tars install` |
