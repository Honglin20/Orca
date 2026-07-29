# SPEC：in-session run 可见性根治 —— 解耦 `workflows/` marker，注册 tape 实际所在目录

> **v2（2026-07-29）**：经 round-1 spec-reviewer 对抗审视（Conditional Pass）+ 用户拍板后修订。
> 决策已锁定：**D-rebuild=A / D1=是 / D2=否 / D3=接受**。待 round-2 spec-reviewer 稳态确认后实现。
> **实施 base 分支：`in-session-unified-backend`（HEAD `3a73675`）**——非 `master`；`master` 在 `98d7c67`（PR#4），
> 不含注册表机制。SPEC 所有 file:line 已在该分支实测命中。
> 相关前作：`docs/plans/2026-07-24-in-session-bootstrap-register-project.md`（surgical 补丁，本 SPEC 是其根治续作，
> §4.1 B 显式推翻前作"注册 detect_project_root"的选择）。

---

## 1. 现象（用户实测）

1. 在**任意文件夹**（含无 `workflows/` 的目录）下用 in-session（TARS / `orca` CLI）启动一个 workflow：
   - 前端 web 列表**看不到**该 run；
   - 子 agent 事件**推不到**前端（前端观测不到该 run）。
2. 该目录无 `workflows/` 时 bootstrap 报 warning：`注册项目失败…项目根需含 workflows/ 或 .orca/config.json（M-16）`。
3. 手动 `tars project rebuild` 后立即恢复可见。
4. **诉求**：不应因"用了别处已存在的 workflow、本地无 `workflows/`"被卡；任意目录 in-session 起 wf → web 必见 + 子 agent 必推；**不再依赖 `workflows/` / `.orca/config.json` 检查**。

## 2. 根因（三层叠加 + push-chain 同根）

web discovery（`run_manager.py:1326` `discover_runs`）+ 详情 attach（`run_manager.py:1656` `resolve_run_path`）**只认注册表** `~/.orca/projects.json`，逐项目扫 `<注册根>/runs/*.jsonl`。注册表写入与 tape 落点是**三套独立计算**，仅当重合才可见：

| # | 独立来源 | 代码 | 发散点 |
|---|---|---|---|
| ① | tape 落点 = `cwd/runs/<id>.jsonl`（相对 CWD） | `orca/iface/cli/bg_runner.py:148` `default_tape_path`（`Path("runs")/...`） | 跟启动 CWD 走 |
| ② | 注册根 = `detect_project_root()`（向上找 `workflows/`/`.git`，可能 ≠ cwd） | `in_session/cli.py:560` `_register_current_project`；`run_manager.py:1258` `_resolve_project_path_for_run` | 可能跳到 cwd 祖先 |
| ③ | discovery 扫描 = `<注册根>/runs`（字面 `"runs"`） | `run_manager.py:1355`；`resolve_run_path:1677` | 只扫已注册项目 |

- **①≠②③**：bootstrap 把 tape 写 `cwd/runs`，却注册 `detect_project_root()`（祖先）→ discovery 扫 `<祖先>/runs` 看不到 `cwd/runs` 的 tape。即便注册成功也 miss。
- **M-16 门槛太死**（`_project.py:366`）：注册根无 `workflows/`/`.orca/config.json` → `ValueError` → `_register_current_project` fail-open 只 warn（`cli.py:585`）→ 永不进注册表 → 全 miss。
- **一次性 fail-open 无自愈**：注册只在 bootstrap 跑一次，失败无重试。
- **隐藏耦合（round-1 challenge A）**：① 的 `Path("runs")` 与 ③ 的 `root/"runs"` 是**两处独立硬编码字面**，无共享常量——`commands.py:1552` 曾有一次把 tape 侧改为从 `default_tape_path` 派生，discovery 侧未跟进。本 SPEC 抽 `RUNS_DIRNAME` 常量统一（§4.1 E）。

**子 agent 推送为何同根**：push-chain（sidechain daemon → bus → tape → WS pump，`docs/troubleshooting/push-chain.md` H1-H6）**不依赖** `register_project`/`workflows/`/`detect_project_root`（已 grep 验证 events/gates/_push_probe/sidechain_daemon 零命中）。daemon 把子 agent 事件写 tape。但**末跳 H6（前端看到该 run）需 web 能 attach 该 tape**：`ensure_attached → resolve_run_path` 查注册表，未注册 → 0 命中 → 404 → 推送对前端失效。**故"前端可见"与"子 agent 推送"同根，一并修好。**

**`rebuild` 为何能修**：`rebuild_registry`（`_project.py:454`）重收候选根（旧注册 + extras + detect）逐个 re-register = 手动 reconcile。但 round-1 抓到**它会把本修复擦掉**（§4.1 D / issue #2）。

## 3. 目标 / 非目标

**目标**
- G1：任意非顶层目录 in-session 起 wf → web 列表必见 + 详情可 attach + 子 agent 事件可见，**无需 `workflows/`/`.orca/config.json`**。
- G2：`tars project rebuild` **不再是**可见性必经步骤；且 rebuild **不擦除** marker-free 注册（D-rebuild=A）。
- G3：保留安全边界——外部/任意路径注册仍受 M-15（拒顶层）+ M-16（marker）双闸；web 只能读"orca 自己写过 tape 的目录"。
- G4：单一真相源——tape 落点 = discovery 扫描根（经 `RUNS_DIRNAME` 共享常量），不再两处独立字面。

**非目标**
- N1：不改 push-chain 本身（它没问题）。
- N2：不改 tape 格式 / reducer / 前端 store（Bug2 另案）。
- N3：不把 tape 迁全局目录（保留 runs 在用户 cwd/项目下）。
- N4：不做注册表 stale 条目自动清理（配套前端提示 + 手动入口，§5，非 blocker）。
- N5（D2=否）：`tars serve`/`orca open` 启动时不注册 serve-cwd（非当前诉求；serve cwd 不一定有 run）。

## 4. 设计：marker-free 自注册"tape 所在目录" + rebuild 不擦除

**核心原则**：discovery 扫 `<注册根>/runs`。**注册"tape 实际所在 runs 目录的父级"**，则 `<root>/runs` 天然 = tape 位置——零 discovery 改动、零新机制、复用既有注册表。自注册是可信路径（正在真跑 wf / 写 tape），故**放宽 M-16**；外部注册仍走严格 M-16。

### 4.1 契约改动（A-E）

**A. `orca/runtime/_project.py::register_project`（加 `require_marker`）**
```python
def register_project(project_root: Path | str, *, require_marker: bool = True) -> str:
    resolved = _resolve_strict(project_root)
    if _is_toplevel(resolved):              # M-15：始终保留
        raise ValueError(...)
    if resolved == orca_home().resolve():   # P2：始终保留
        raise ValueError(...)
    if require_marker and not _has_project_marker(resolved):  # M-16：仅外部注册强制
        raise ValueError("项目根需含 workflows/ 或 .orca/config.json（M-16）…")
    ...  # upsert 不变
```
默认 `require_marker=True`：`rebuild_registry` 的**新候选**、未来 `tars project add` 等**零改动**。`False`：仅可信自注册（bootstrap / start_run / rebuild 旧 entry）。

**B. `orca/iface/in_session/cli.py::_register_current_project`（注册 tape 目录父级，marker-free）**
```python
def _register_current_project() -> None:
    try:
        from orca.runtime import register_project
        # 注册 tape 物理位置（runs 目录的父级），与 discovery 的 <root>/runs 天然对齐。
        # 不再用 detect_project_root()（可能跳 cwd 祖先，与 tape 落点 cwd/runs 脱节）。
        # 显式声明：注册的是 tape 物理位置（cwd），与逻辑项目根（ORCA_PROJECT_ROOT）解耦。
        rundir_parent = _default_rundir().resolve().parent
        register_project(rundir_parent, require_marker=False)
    except Exception:
        logger.warning("bootstrap: 注册项目失败…（可 `tars project rebuild` 补登记）", exc_info=True)
```
- `_default_rundir()`=`Path("runs")`（相对）→ `.resolve().parent`=`cwd`。tape 在 `cwd/runs/<id>` → 注册 `cwd` → discovery 扫 `cwd/runs` → 命中。①②③ 对齐。
- `_default_rundir().resolve()` 失败（cwd 异常）→ fail-open warn（既有语义）。
- **不再读 `ORCA_PROJECT_ROOT`/`detect_project_root`**（注册侧）；该 env 仅影响别处（如 `default_tape_path` 未来演进），与注册解耦。

**C. `orca/iface/web/run_manager.py::_resolve_project_path_for_run`（D1=是，切 marker-free）**
```python
register_project(root, require_marker=False)   # 原：register_project(root)
```
- start_run 已把 tape 写到 `resolved_project/runs`（`run_manager.py:336`）并注册 `resolved_project`——内部对齐，**唯一缺陷是 M-16 门槛**。切 `require_marker=False` 后无 marker 项目也能注册 → 对齐 → 可见。
- **tape 落点行为（必须在 SPEC 注明，round-1 issue #4）**：`project_path=None` 时 `resolved_project=detect_project_root()`。若 detect 跳到 cwd 祖先（如 cwd=`<proj>/sub`、workflows/ 在 `<proj>`），tape 落 `<proj>/runs`（**非** `<proj>/sub/runs`），注册 `<proj>`，discovery 扫 `<proj>/runs` → 命中。与 bootstrap（注册 cwd）**入口语义不同但各自自洽**（见 §4.2）。
- 显式 `project_path` 入参（web POST 必填）失败仍 raise（fail loud，契约不变）。

**D. `orca/runtime/_project.py::rebuild_registry`（D-rebuild=A，不擦除旧 marker-free entry）**
```python
# step2 收候选时记来源：old_projects（旧注册表）vs extra_paths/detect（新候选）
old_paths: set[str] = { ... 来自 old_projects.values() 的 path ... }
...
# step5 逐个 register：旧 entry 视为"先前可信注册"→ require_marker=False；新候选走默认 True
for path_str in deduped:
    require_marker = path_str in old_paths   # 旧 entry 信任，新候选严格 M-16
    try:
        register_project(path_str, require_marker=require_marker)
        registered += 1
    except (ValueError, OSError, RuntimeError):
        skipped += 1
```
- **语义**：rebuild = reconcile（对齐注册表 vs 现实），**不是 re-gate**。"曾在注册表"= 先前可信注册的证据。新候选（extra_paths / detect）仍走严格 M-16（安全）。
- 修复 round-1 issue #2：marker-free 旧 entry 不再被默认 M-16 擦除；G2 成立。

**E. 抽 `RUNS_DIRNAME` 常量（round-1 challenge A）**
- `orca/runtime/_project.py` 顶部加 `RUNS_DIRNAME = "runs"`（中立层，iface 合法向下 import）。
- `_project.py`（`is_registered_runs_dir` 的 `root/"runs"`）、`run_manager.py`（`discover_runs` `root/"runs"`、`resolve_run_path` `Path(root_str)/"runs"/...`）、`bg_runner.py`（`default_tape_path` `Path("runs")/...`）三处字面统一改引 `RUNS_DIRNAME`。
- 消除 ①③ 隐藏耦合；未来改 runs 目录名只动一处。

### 4.2 覆盖矩阵（按入口拆，round-1 issue #3）

| 入口 | 启动场景 | tape 落点 | 注册根（改后） | discovery 扫描 | 可见？ |
|---|---|---|---|---|---|
| **bootstrap**（in-session/TARS） | cwd=项目根（有 workflows/） | `cwd/runs` | `cwd` | `cwd/runs` | ✅ |
| **bootstrap** | cwd=**无 workflows/ 的目录** | `cwd/runs` | `cwd`（marker-free） | `cwd/runs` | ✅（新） |
| **bootstrap** | cwd=`<proj>/sub` | `<proj>/sub/runs` | `<proj>/sub` | `<proj>/sub/runs` | ✅ |
| **start_run**（`orca run`/web POST） | detect=cwd（有 workflows/ 或无 marker） | `<cwd>/runs` | `<cwd>`（marker-free） | `<cwd>/runs` | ✅ |
| **start_run** | detect 跳祖先（cwd=`<proj>/sub`，workflows/ 在 `<proj>`） | `<proj>/runs` | `<proj>`（marker-free） | `<proj>/runs` | ✅ |
| **外部** | `tars project add /可疑路径` | n/a | M-15+M-16 拒 | n/a | 安全（不变） |

注：两入口注册的"根"可能不同（bootstrap=cwd，start_run=detect 祖先），但**各自 tape 落点与注册根自洽**，discovery 均命中。

## 5. 安全分析（round-1 issue #9 收紧）

- **放宽 M-16 是否打开任意 tape 读取？** 否。marker-free 仅发生在**可信代码路径**（bootstrap、start_run、rebuild 旧 entry）——本就在写 tape/曾注册。"orca 正往这写 tape"是比"有 workflows/ 文件夹"**更强**的有效上下文证据。
- **安全模型限定：单用户本地**。"正在写 tape = 更强证据"在**单用户本地**部署成立；**共享/远程 web 部署**下不自动成立（启动者可能越权子 agent 写到非预期目录）。本 SPEC 不改变既有"web 默认 no-op auth middleware"假设（单用户）；多用户隔离是独立议题，不在本 SPEC。
- **web 能读哪些 tape**：`<注册根>/runs/*.jsonl` ∪ `ORCA_WEB_TAPE_ALLOWLIST` ∪ serve 默认 runs_dir。改后 = "orca 曾写 tape 的目录" ∪ "显式 marker 注册项目"。不含任意未写 tape 的目录。攻击面不扩大。
- **M-15/P2 始终守**（round-1 确认三道闸独立 `if/raise`，`require_marker=False` 只包第三个）。
- **写副作用（round-1 新增）**：start_run（`run_manager.py:337`）/bootstrap（`Tape` 写）会在 cwd 下 `mkdir runs/`。若 cwd 是受保护/非预期路径，属非预期写。mitigation：M-15 拒顶层；用户从合理项目目录启动是常态。低风险，接受。
- **注册表膨胀 + stale（N4）**：marker-free 后每个曾跑 wf 的 cwd 留一条；`is_registered_runs_dir` 授 web 读每个注册根整个 `runs/` 子树。配套：既有 `list_stale_projects` 前端折叠区 + 手动清理入口（推荐，非 blocker）。

## 6. 备选方案（已否决，留痕）

- **每次 bootstrap 自动 rebuild**：否决。`rebuild_registry` 先清空再重插，有"瞬态可见性"空窗（`_project.py` docstring），并发读者读空注册表；每 run 全量重算浪费 + 竞态。
- **全局 runs 目录（tape 统一写 `~/.orca/runs`）**：否决。discovery 最简但改 tape 落点（runs 离开用户项目/cwd，artifacts/prompts 跟随迁移），破坏"runs/ 在我仓库"心智，改动面大。注：`bg_runner.ORCA_RUNS_DIR`（`bg_runner.py:46`）名像 env 实为**硬编码常量**，chart 代码"改 ORCA_RUNS_DIR env"注释误导；若未来做全局目录需先把它做成真 env，另立 SPEC。
- **独立 runs 目录 manifest（append-only）**：等价本方案但引入新文件/结构。本方案复用既有注册表 + discovery，零新机制（Simplicity First）。

## 7. 验收标准（round-1 修订：AC2/AC3/AC4/AC5/AC6 收紧）

- **AC1**：`register_project(path, require_marker=False)` 对无 marker 的非顶层目录**成功注册**（返 project_id，进 `projects.json`）；`require_marker=True`（默认）对同目录仍 raise `ValueError`。
- **AC2**：`register_project(path, require_marker=False)` 对 OS 顶层仍 raise——**必测**：`/`、`/etc`、`/home`、`/tmp`、`/usr`、`C:\`（round-1 补 `/home`/`/tmp`：非 `parent==self`，只靠 `_TOPLEVEL_DIRS` 黑名单，防实现清空黑名单漏网）。
- **AC3**：in-session bootstrap 在**无 `workflows/` 的 tmp 目录**起 run → `list_registered()` 含该 cwd → `discover_runs()` 返回该 run（source=attached）→ `resolve_run_path(run_id)` 命中。**用 `CliRunner` cwd 钉 `cwd_tmp` fixture，不依赖任何 env**（round-1：前作靠 `ORCA_PROJECT_ROOT` 钉 detect 的测试须重写）。
- **AC4a**（round-1 拆，确定性）：上述 run `ensure_attached(run_id)` → `resolve_run_path` 不抛 + `get_run_events(run_id)` 返回非空（合成 tape 落盘后读，不涉 WS 时序）。
- **AC4b**（round-1 拆，确定性）：in-process `bus.publish(agent_event)` → starlette `TestClient` websocket `receive_text()` 收到该事件（绕过 tape 文件 flush 时序，内存队列同步可断言）。**废弃**"落 tape 后 WS subscribe 收到"形态（依赖 flush + pump 轮询，不可确定性验证）。
- **AC5a**：既有 marker 项目经 `register_project`（默认）/`rebuild_registry` 注册**不变**（零回归）。
- **AC5b**（D-rebuild=A 联动）：marker-free 注册的旧 entry 经 `rebuild_registry` 后**仍存在**（不被默认 M-16 擦除）。
- **AC5c**：start_run 在 detect≠cwd 时 tape 落 `detect_root/runs`、注册 `detect_root`、discovery 命中（§4.2 第 5 行契约）。
- **AC6**：`orca doctor --probe-push`（H6 **self-spawn**，合成事件 3s 确定性 pass）对 in-session 无-marker run 通过。passive 模式仅真实排障用，**不作 AC**（round-1：passive 8s 等真实事件，无活跃子 agent 时 `unknown` → CI 假阴性）。

## 8. 测试计划（Rule 9：测意图）

- `tests/runtime/test_project.py`：AC1/AC2（`require_marker` 双向 + M-15/P2 不被绕过，含 `/home`/`/tmp`）。
- `tests/runtime/test_project.py`（rebuild）：AC5b（marker-free 旧 entry 经 rebuild 仍存在）+ AC5a（marker 项目零回归）。
- `tests/iface/in_session/test_in_session_cli.py`：AC3（`ORCA_HOME=tmp`、`cwd_tmp` 无 workflows/、CliRunner cwd 钉死、不设 env，断言注册 + discovery 可见）。
  - **改写既有 `test_register_current_project_fail_open`（`:141`）**：原"无 marker 目录 fail-open"用例改写为**顶层目录场景**（仍由 M-15 拒，fail-open 不抛仍成立；`:157 assert list_registered()=={}` 不破）。
- `tests/iface/web/test_run_manager.py`：AC4a（attach + events 非空）、AC4b（bus→WS TestClient）、AC5c（start_run detect≠cwd 落点）。
- 回归：既有 runtime/in_session/web 注册相关测试全绿。

## 9. 决策（已锁定）

- **D1=是**：start_run/web POST 切 `require_marker=False`（§4.1 C）。detect≠cwd 时 tape 落点行为已在 §4.2/AC5c 注明。
- **D2=否**：`tars serve`/`orca open` 不注册 serve-cwd（N5）。
- **D3=接受**：bootstrap 注册 cwd 时 `name=cwd.basename`（仅展示用，不影响功能）。
- **D-rebuild=A**：rebuild 对旧 entry `require_marker=False`，新候选严格 M-16（§4.1 D）。

## 10. 落地步骤（round-2 review 通过后）

1. `_project.py`：`RUNS_DIRNAME` 常量 + `register_project(require_marker)` + `rebuild_registry` 旧 entry 透传 + `is_registered_runs_dir` 用常量。守门测试 AC1/AC2/AC5a/AC5b。
2. `in_session/cli.py`：`_register_current_project` 改注册 `rundir.parent`（marker-free）。AC3 + 改写 fail_open 用例。
3. `run_manager.py`：`_resolve_project_path_for_run` 切 `require_marker=False` + `discover_runs`/`resolve_run_path` 用 `RUNS_DIRNAME`。AC4a/AC4b/AC5c。
4. `bg_runner.py`：`default_tape_path` 用 `RUNS_DIRNAME`。
5. code-reviewer 自检（依赖铁律、M-15/P2 未绕过、DRY、fail loud、RUNS_DIRNAME 单源）。
6. test-agent 端到端（headless tars-skill，多文件夹含无 workflows/）。
7. release note + CHANGELOG + CURRENT 更新；合入 `in-session-unified-backend`；推送。
