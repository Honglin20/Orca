# 2026-07-19 —— in-session 加固与性能 P1（8 项小合集）

**SPEC**: [`docs/specs/2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1
**范围**: P1 串行组（cli.py + events/tape.py + sidechain/chart_daemon + 新 _daemon_liveness.py + 新 _errors.py + SKILL.md + tests）。**不碰** events/replay.py / run/step.py / run/orchestrator.py（P3 territory）。

## 8 项（按 SPEC §6 P1 行）

### S7 — tape multi-byte helper 抽公共（commit `9100481`）
- 新 `events/tape.py:read_last_complete_lines(path, start_offset, end_offset) -> tuple[list[str] | None, int]`：SPEC §5 S7 抽出公共 helper，把三处重复的 binary-mode seek+read+rfind(\n)+decode 模式 DRY 掉（`chart_daemon._FlockSafeTape._read_max_seq_from_disk` / `chart_daemon._watch_terminal` / `sidechain_ingestor._derive_current_node`）。
- 零行为变化：B2 multibyte 回归测试全过；chart/sidechain 守护的 partial-line race 防护 + binary-mode 多字节安全语义不变。
- 8 直接单元测试（`tests/events/test_tape_read_last_complete_lines.py`）覆盖 OSError / end≤start / 纯 partial / 推进 offset / partial 尾不推进 / 多字节 byte 对齐 / decode errors=replace / 空文件。

### S9 — daemon liveness helper 抽公共（commit `047629f`）
- 新 `iface/in_session/_daemon_liveness.py`：`socket_daemon_alive(sock_path, timeout)` (connect-probe) + `pidfile_daemon_alive(pidfile, module_name, run_id)` (pidfile + /proc/<pid>/cmdline)。
- `cli._chart_daemon_alive` + `sidechain_daemon._sidechain_daemon_alive` 改薄 wrapper 复用新 helper。
- 10 直接单元测试（`tests/iface/in_session/test_daemon_liveness.py`）：socket（无文件/stale/真监听者/timeout）+ pidfile（缺/坏/死/模块名不匹配/**run_id 不匹配**/run_id=None skip）。重点覆盖 pid 复用防御第三层。

### S2 — SKILL.md flag ↔ CLI --help CI 守门（commit `4bb81c5`）
- 新 `tests/iface/in_session/test_skill_md_flags_guard.py`：解析 `orca/skills/tars/SKILL.md` code fence，扫 `orca <cmd> ...` 行的 flag，断言 ⊆ `orca --help` + 各子命令 `--help` 输出。
- 仅扫 SKILL.md（SPEC md 含讨论性假命令不扫）；regex 抽 fence（不引 markdown lib 依赖）。
- 5 测：exists / subset / 7-commands / spec-md-not-scanned（注入假 SKILL+SPEC 真验证排除）/ fails-on-unknown-flag（负面守门）。

### D3 — doctor sidechain 守护存活探针（commit `1ed2c90`）
- `doctor` 加新可选 check `sidechain_daemon`（hard=False）：对每个活跃 run 调 `_sidechain_daemon_alive` 探针。
- 状态：守护存活 → pass；死 → fail（degraded，hard=False 不计 ok）+ hint（next 自动 respawn）；无 host_session env / 无活跃 marker → unknown。
- **明示不覆盖持续 iterate 失败**（§8#4：YAGNI 不做 socket 查询，靠 daemon log + 用户排查）。
- 与 `sidechain_backend` 互补：前者查静态基础设施，本 check 查运行时存活。doctor checks 数从 5 → 6。
- 4 测覆盖 unknown/dead/alive/pass 四态。

### O3 — status --run-id 加 no_output_count 字段（commit `bc620e3`）
- `orca status --run-id <id>` 详情加 `no_output_count`（从 marker 读，raw 透出供观测 compliance 红线）。
- **compliance 是 orca 自我保护**（防无限空转，到 _COMPLIANCE_LIMIT=3 自己 fail）；**主 session 调度固定**（不因计数改行为）→ 不让主 session/SKILL 参与 compliance 管理。
- 删 SPEC v4.1 没要的 next reply compliance_warning / stuck 语义。无 marker（run 终态/损坏）→ None。
- 3 测：bootstrap 后 0 / next 无 output 后 1 / 终态后 None；加 next reply 零回归测（不含 compliance 字段）。

### O4 — busy 信封加 retry_after_ms:500（commit `a3e28bd`）
- bootstrap/next/stop 三处 busy 信封加 `retry_after_ms:500`。新 `_BUSY_RETRY_AFTER_MS=500` 常量 + `_echo_busy_reply()` helper DRY 三处拼装。
- 主 session 据 retry_after_ms 等 500ms 后**重试同一 next 命令本身**（**不重派子代理 / 不重发 prompt** —— 避免 advance_step 不持锁调用契约冲突）。
- `_drive_protocol` 补 busy 重试规则；SKILL.md 第 3 步加 reason=busy 分支。
- 4 测：bootstrap/next/stop 三处 + `_drive_protocol` 含 busy/retry_after_ms 文本；busy reply 无 prompt/node 字段（不重发 prompt AC）。

### F3 — bootstrap --inputs 校验 + inputs_validation_error（commit `e5d3c5b`）
- 新 `orca/run/_errors.py`（SPEC §1 铁律 5.1 单一真相源 for 新 error_kind）：登记 `INPUTS_VALIDATION_ERROR`。step.py 现有 ERR_* 不迁移（YAGNI）。
- 新 `_TYPE_MAP` 手写 isinstance 校验（**不引入 jsonschema 依赖**），含 bool/int 隔离（Python `isinstance(True, int)` 的反陷阱）；未声明 type / 自定义 type → pass-through（旧 wf loose-typed 零回归）。
- `[default]`/`[advanced]` 标签字段省略不触发 required（与 SKILL 抽 inputs 标签契约一致）。
- 校验在 `bootstrap_lock` 之前（不触 state，fail fast 在 gen run_id 前）。
- 12 测：错类型/缺必填/默认/无 inputs/bool-int 隔离/自定义 pass-through/信封契约/`_errors.py` 登记 + TYPE_MAP 全 alias parametrized（~45 case）。

### O2 — bootstrap 锁临界区缩小（commit `b4e4b67`）
- `.orca-bootstrap.lock` 临界区从「dupe check → spawn daemons + socket wait + build reply」缩到「dupe check + gen run_id + advance+emit + write_marker」。
- 锁外：`_write_orca_env` + `_spawn_chart_daemon` + `_wait_for_sock` + `_spawn_sidechain_daemon` + build reply（run_id 派生路径，不参与 dupe 判定）。
- **dupe-check 不变量仍成立**：锁仍包 dupe check + write_marker；释放在 write_marker 之后、spawn 之前。第二个 bootstrap 等锁释放后进 dupe check → 看到 first 的 marker → fail loud。
- 收益：bootstrap 持锁时间从「spawn + 5s socket wait」降到「emit + write_marker」（<100ms）。
- 锁外加 `assert run_id/tape_path/result`，防未来「锁内不 raise 的 return」漏赋变量导致锁外 NameError。
- 2 测：`test_bootstrap_lock_released_before_spawn_daemons`（spawn 时锁可用）+ `test_bootstrap_dupe_check_invariant_preserved`（并发 bootstrap second 仍 fail loud）。

## 验收对照（SPEC §7）

| 条目 | AC | 实现位置 | 测试 |
|---|---|---|---|
| S2 | SKILL.md flag ⊆ --help；SPEC md 不扫 | `test_skill_md_flags_guard.py` | 5 测（含负面守门） |
| S7 | 三处替换为 helper，B2 回归全过 | `tape.py:read_last_complete_lines` | 8 直接 + multibyte 回归 |
| S9 | 两守护复用 helper，respawn 零回归 | `_daemon_liveness.py` | 10 直接 + daemon 回归 |
| D3 | 守护存活 → pass；死 → degraded + hint；**不覆盖持续失败** | `cli._check_sidechain_daemon_liveness` | 4 测 |
| O2 | 连续 3 bootstrap 耗时下降；dupe-check 不变量成立 | `cli.bootstrap` 锁外化 spawn | 2 测（锁外 + 并发 dupe） |
| O3 | status --run-id 含 no_output_count；next reply 不加 compliance 字段 | `cli.status` + marker read | 3 测 + next 零回归 |
| O4 | busy reply 含 retry_after_ms；不重发 prompt | `_echo_busy_reply` + `_drive_protocol` | 4 测（含 drive_protocol 文本） |
| F3 | 错类型/缺必填 → inputs_validation_error + 定位；旧 wf 零回归；无新依赖 | `_validate_inputs` + `_errors.py` | 12 测 + TYPE_MAP 全 alias |

**铁律 AC**：无新裸 sys.exit / 宽 except pass / 2>/dev/null||true；advance_step emit snapshot 不变；**无未登记 error_kind**（F3 inputs_validation_error 登记 `_errors.py`）；7 命令不变；marker 字段=3；tape 仍唯一真相源。

## 偏差

无。8 项严格按 SPEC v4.1 各条目段实现，无 deviation / scope creep。

## 验证结果

- **862 测试全过**（tests/iface/in_session/ + tests/events/ + tests/run/ + tests/compile/），含 P1 新增 ~40 测。
- **code-reviewer 两轮（impl + test-coverage）**：0 🔴 blocker；3 🟡 impl + 5 🔴 test + 关键 🟡 全部 address（commit `d3893b9`）。剩余 🟢 项保留（YAGNI）。
- **架构铁律（user）逐条核通过**：orca 管所有状态/决策/compliance；主 session 仅调度（不参与 compliance 决策、不重发 prompt、不重派子代理）；不跨层耦合；不过度设计。

## Commits

| SHA | 项 | 类型 |
|---|---|---|
| `9100481` | S7 | refactor（tape helper） |
| `047629f` | S9 | refactor（daemon liveness helper） |
| `4bb81c5` | S2 | test（SKILL.md guard） |
| `1ed2c90` | D3 | feat（doctor sidechain 探针） |
| `bc620e3` | O3 | feat（status no_output_count） |
| `a3e28bd` | O4 | feat（busy retry_after_ms） |
| `e5d3c5b` | F3 | feat（inputs validation） |
| `b4e4b67` | O2 | perf（bootstrap 锁缩小） |
| `d3893b9` | code-reviewer 闭环 | fix（impl 3 🟡 + test 5 🔴 + 关键 🟡） |

## 文件改动

**生产代码**（绝对路径）:
- `/mnt/d/Projects/Orca/orca/events/tape.py`（S7 helper）
- `/mnt/d/Projects/Orca/orca/events/sidechain_ingestor.py`（S7 调用点）
- `/mnt/d/Projects/Orca/orca/iface/in_session/chart_daemon.py`（S7 调用点）
- `/mnt/d/Projects/Orca/orca/iface/in_session/_daemon_liveness.py`（S9 新文件）
- `/mnt/d/Projects/Orca/orca/iface/in_session/sidechain_daemon.py`（S9 wrapper + 重命名常量）
- `/mnt/d/Projects/Orca/orca/iface/in_session/cli.py`（S9/D3/O3/O4/F3/O2 主改动）
- `/mnt/d/Projects/Orca/orca/run/_errors.py`（F3 新文件）
- `/mnt/d/Projects/Orca/orca/skills/tars/SKILL.md`（F3/O4 文档化）

**测试代码**:
- `/mnt/d/Projects/Orca/tests/events/test_tape_read_last_complete_lines.py`（S7 单元，新建）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_daemon_liveness.py`（S9 单元，新建）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_skill_md_flags_guard.py`（S2 guard，新建）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_in_session_cli.py`（F3/O2/O4/O3 + intent 测）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_in_session_v8.py`（D3 + O3 测）

## 遗留

- **未做（SPEC §8 defer 项 / YAGNI）**：F2 retry / O1b cache / O1c tape resume / O5 lock contention。
- **后续包（SPEC §6）**：P2（D4 + D5 marker 三态 + doctor orphan）/ P4（D1 + D2 失败路径统一）/ P5（F1 resume）/ P6（S1 contract-test 黄金集）—— 都碰 cli.py，按 SPEC 串行 P1→P2→P4→P5。
- **SKILL.md install 副本**：源已改（O4 busy + F3 inputs_validation_error），用户若装了旧 TARS skill 副本需重跑 `tars install` 同步（与 F1 同款约束）。
