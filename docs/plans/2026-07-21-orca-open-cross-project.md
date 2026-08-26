# 计划：`orca open` 跨项目端口占用——项目感知复用 + 自动起新 server

## 目标

当默认端口 7428 已被**别的项目**的 orca server 占用时，`orca open <id>` 不再静默挂到别
人项目的 tape（或困惑性 404/403），而是：识别出"那是别人的 server"→ 找到/起一个**本项目
自己的 server**→ 用**绝对 tape 路径**attach→ 正常打开自己的 run。

成功标准：
1. 7428 是本项目 server → 复用（行为不变）。
2. 7428 是别项目 server → 不复用；按 per-project 登记文件找到本项目已起的 server 则复用，
   否则在空闲端口起新 server 并登记；之后同项目重复 `orca open` 复用登记端口（不泄漏进程）。
3. 跨项目 attach 一律走绝对路径；server 端 `resolve_tape_path` 边界检查把跨项目 tape 干净
   403（fail loud），永不静默挂错。
4. 既有 open/attach 单测回归通过；新增跨项目 / registry / 绝对路径回归用例。
5. **`orca bootstrap`（带 `--inputs` 真启动路径）默认自动开 web**：写完 marker 后 detach
   起 `orca open <run_id>` 后台子进程；bootstrap 的 stdout JSON 契约零污染、零阻塞、失败 soft。
6. `--no-open-web` / `ORCA_BOOTSTRAP_OPEN_WEB=0` 可关；schema-only 路径（不带 `--inputs`）不触发。

## 依据

- 现状代码：
  - `orca/iface/cli/commands.py:1452 _open_run`（reuse 决策 + attach）
  - `orca/iface/cli/commands.py:1464,1501`（tape 相对路径解析 + 跨进程 POST）
  - `orca/iface/cli/commands.py:1116 _post_run_to_existing`（**已正确的跨 CWD 范式**：`str(Path(...).resolve())`，docstring 1122-1126 专门警告此坑）
  - `orca/iface/web/routes/attach.py:75 health`（只回 `{app,version,pid}`，无项目身份）
  - `orca/iface/web/run_manager.py:326 resolve_tape_path`（server 端按自己 CWD resolve + 边界检查）
- 既有测试：`tests/iface/cli/test_web_default_and_open.py`（open 全套 mock）、`tests/iface/web/test_attach.py`
- `.gitignore:40 runs/`、`.gitignore:55 .orca/`（登记文件落 `runs/` 天然不入库）

## 背景（根因，两处叠加）

**① tape 以相对路径跨进程发送（静默挂错）**
`_open_run` 把 `_resolve_tape_path(run_id)` 的 `Path("runs/<id>.jsonl")`（相对）经 `str(tape)`
直接 POST 给被复用的 server；server 端 `raw.resolve()` 按**server 自己的 CWD** 解析 → 指向别
项目。`run` 命令的 `_post_run_to_existing` 早已 `resolve()` 绝对化并写了警告 docstring，`open`
漏了同样硬化。

**② "7428 有 orca 就无脑复用"，不校验项目身份（UX 阻塞）**
`_probe_orca_server` 命中任何 orca 即复用，health 不报项目身份，client 无从识别"那是别人的
server"。即便修了①，server 边界检查会把跨项目 tape 403——fail loud 了，但用户仍打不开自己的 run。

无 pid/lock 文件绑定 port↔cwd；"是否在跑"纯靠一次 health 探测。

## 设计要点

- **项目身份 = `sha1(resolved runs_dir)[:12]` 指纹**（非明文路径）。health 默认 bind 0.0.0.0
  网络可达，返回明文项目绝对目录是信息泄漏；指纹不可逆、可比对。client 与 server 同算法。
- **per-project 登记文件 `<runs_dir>/.orca-web.json = {port, pid, runs_dir_fp}`**：spawn 后写、
  复用前读。陈旧（端口探测非 orca / 指纹不匹配）→ 忽略，下次 spawn 覆盖（自愈，无主动清理）。
- **绝对路径化**为跨进程 POST 的硬化骨干（与 `_post_run_to_existing` 一致）。
- 显式 `--port`：不查 registry（用户钉了端口）；被任何非本项目进程占用 → fail loud exit 2。

## 文件清单

### 1. `orca/iface/web/_identity.py` —— **新文件**（项目身份指纹，共享 DRY 源，stdlib-only）

> spec-review B3：`run_manager.py:33-52` 顶层重依赖（Orchestrator/EventBus/Tape/gates/
> chart_ingestor…），lazy import 进 client 的 `open` 路径会把整张依赖图拉进来（当前 open 路径
> 不加载 run_manager，是净回归）。故指纹函数**禁放 run_manager.py**，也**禁内联副本**（身份/
> 加密算法 DRY 是红线，单边改另一边没改 → 指纹静默不一致 → 所有复用失效）。下沉到无依赖模块。

```python
"""_identity.py —— web server 项目身份指纹（stdlib-only，无 fastapi/uvicorn/编排依赖）。

client（orca open 的 _runs_dir_fp）与 server（health 端点）同算法 → 同项目指纹一致。
放独立模块：attach.py（web 同层）与 commands.py（cli，lazy import）都从此 import，
不拉 run_manager 的重依赖图。依赖单向：本模块只依赖 stdlib。
"""
import hashlib
from pathlib import Path

def runs_dir_fingerprint(runs_dir: Path) -> str:
    """sha1(str(resolve(runs_dir)))[:12]。

    12 hex = 48bit；birthday paradox 对 ≤10^6 项目碰撞概率 < 10^-8（单机/团队远超够用）。
    用指纹非明文：health 默认 bind 0.0.0.0 网络可达，回明文项目目录是信息泄漏；sha1 不可逆。
    resolve 失败（权限/loop）→ 退化为未 resolve 字面（仍稳定可比，不抛）。
    """
    try:
        resolved = str(Path(runs_dir).resolve())
    except (OSError, RuntimeError):
        resolved = str(runs_dir)
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
```

### 2. `orca/iface/web/routes/attach.py` —— health 暴露指纹

```python
# 当前（attach.py:82-86）
return {"app": "orca", "version": orca.__version__, "pid": os.getpid()}

# 改动
from orca.iface.web._identity import runs_dir_fingerprint     # 无依赖共享模块（见文件1）
...
return {
    "app": "orca",
    "version": orca.__version__,
    "pid": os.getpid(),
    "runs_dir_fp": runs_dir_fingerprint(manager.runs_dir),
}
```

`_probe_orca_server` 不变（仍只认 `app=="orca"`）；指纹比对在新 helper。

### 3. `orca/iface/cli/web_registry.py` —— **新文件**（per-project 登记读写）

纯 stdlib（json/os/pathlib），不 import commands（避免环；探测/比对由 caller 在 commands.py 做）。

```python
"""per-project web server 端口登记：<runs_dir>/.orca-web.json = {port,pid,runs_dir_fp}。

orca open spawn 后写、复用前读。陈旧（端口探测非 orca / 指纹不匹配）→ 忽略，下次 spawn 覆盖
（自愈，无主动清理，YAGNI）。原子写 tmp+os.replace（与 marker.py 同模式）。
"""
REGISTRY_NAME = ".orca-web.json"

def registry_path(runs_dir): return Path(runs_dir) / REGISTRY_NAME
def read_registry(runs_dir) -> dict | None: ...   # 缺失/损坏/非 dict → None
def write_registry(runs_dir, *, port, pid, runs_dir_fp) -> None: ...  # mkdir + 原子 replace
```

### 4. `orca/iface/cli/commands.py` —— `_open_run` 重写决策 + helpers + spawn 返 pid

**(a) `_spawn_background_serve` 返回 `int | None`（pid），不再是 bool：**

```python
def _spawn_background_serve(host, port) -> int | None:
    proc = subprocess.Popen(["tars","serve","--host",host,"--port",str(port)], ...)
    return proc.pid            # FileNotFoundError → return None
```

**(b) 新 helpers：**

```python
def _default_runs_dir() -> Path:
    # spec-review B2：从 bg_runner.default_tape_path 派生（单一真相源——与 tape 落盘约定同源），
    # 不再字面 "runs"（避免与 RunManager 默认 runs_dir 两处字面漂移）。
    from orca.iface.cli.bg_runner import default_tape_path
    return default_tape_path("__probe__").parent

def _runs_dir_fp(runs_dir: Path) -> str:
    # lazy import 无依赖模块（spec-review B3：不拉 run_manager 重依赖图）
    from orca.iface.web._identity import runs_dir_fingerprint
    return runs_dir_fingerprint(runs_dir)

def _health_is_my_project(health: dict | None, my_fp: str) -> bool:
    return bool(health) and health.get("runs_dir_fp") == my_fp   # 缺指纹（旧版/非 orca）→ False

def _lookup_my_registered_port(my_runs_dir, my_fp, probe_host) -> int | None:
    reg = read_registry(my_runs_dir)                          # 探测权威，pid 仅存档不 gate
    port = reg.get("port") if reg else None
    if not isinstance(port, int): return None
    return port if _health_is_my_project(_probe_orca_server(probe_host, port), my_fp) else None

def _register_my_port(my_runs_dir, *, port, pid, fp) -> None:
    # spec-review B4：写失败不能 silent warn——server 已起(detached pid)却没登记 → 下次 open
    # 找不到 → 重复 spawn → 泄漏。loud 可操作：本次 open 仍继续 attach（不阻断），但显式 stderr
    # 告知 + pid + kill 提示（可见=符合 fail loud 精神，又不破坏本次 open）。
    try:
        write_registry(my_runs_dir, port=port, pid=pid, runs_dir_fp=fp)
    except OSError as e:
        typer.echo(
            f"⚠ web registry 写失败（{e}）；server 已起 pid={pid} 但下次 `orca open` 会重复"
            f" spawn。可手动 kill {pid} 或忽略（下次成功写覆盖）。",
            err=True,
        )
```

**(c) `_open_run` 决策块重写（绝对路径 + 项目感知复用）：**

```python
# 1) tape 绝对路径化（跨进程 POST 不能用相对路径——server CWD 可能不同）。
tape = tape_path if tape_path is not None else _resolve_tape_path(run_id)
if not tape.is_file():
    typer.echo(f"Tape 不存在：{tape}（用 --tape <path> 显式指定）", err=True)
    return EXIT_ARG_OR_VALIDATE
tape_abs = str(tape.resolve())

# 2) 选端口：本项目 server 复用 → registry → 起新 server。
my_fp = _runs_dir_fp(_default_runs_dir())
health = _probe_orca_server(probe_host, target_port)
if _health_is_my_project(health, my_fp):
    actual_port = target_port                              # 2a 本项目 server 在 target → 复用
else:
    actual_port = (None if port is not None                # 2b 显式 --port 不查 registry
                   else _lookup_my_registered_port(my_runs_dir, my_fp, probe_host))
    if actual_port is None:                                # 2c 起新 server
        if port is not None and not _is_port_free(bind_host, target_port):
            typer.echo(f"--port {target_port} 被占用（非本项目 orca 或其它进程）", err=True)
            return EXIT_ARG_OR_VALIDATE
        actual_port = target_port if _is_port_free(bind_host, target_port) else _find_free_port(bind_host=bind_host)
        pid = _spawn_background_serve(bind_host, actual_port)
        if pid is None:
            typer.echo("无法起后台 ``tars serve``：可执行不在 $PATH", err=True)
            return EXIT_RUN_FAILED
        if not _wait_for_health(probe_host, actual_port, timeout=10.0):
            typer.echo(f"后台 tars serve 未在 {actual_port} 上 ready（超时 10s）", err=True)
            return EXIT_RUN_FAILED
        _register_my_port(my_runs_dir, port=actual_port, pid=pid, fp=my_fp)

# 3) POST /api/runs/attach（绝对路径）。
attach_error_code = _attach_and_get_error(probe_host, actual_port, tape_abs, run_id)
# 4) 浏览器（不变）。
```

### 5. 测试

**`tests/iface/cli/test_web_default_and_open.py`（更新 + 新增）：**

| 测试 | 改动 | 覆盖意图 |
|---|---|---|
| `test_open_reuses_existing_server` | **更新**：mock probe 补 `runs_dir_fp`（= 本项目指纹） | 同项目 server 仍复用、不 spawn |
| `test_open_spawns_serve_when_no_existing` | **更新**：`_spawn_background_serve` mock 返 int pid（非 True） | 无 server 时 spawn 流程不破 |
| `test_spawn_background_serve_returns_false_when_orca_missing` | **更新**：断言返 `None`（非 False） | spawn 失败语义 |
| `test_open_attach_failure_exits_one` / `test_open_with_explicit_tape_flag` | **更新**：mock probe 补匹配指纹 | 既有路径回归 |
| `test_open_foreign_project_server_spawns_new` | **新增** | **核心**：probe 指纹不匹配 → spawn 新 server + 登记注册 + attach 走绝对路径 |
| `test_open_reuses_registry_port_when_default_foreign` | **新增** | 7428 foreign + 登记文件指向本项目端口 → 复用该端口、不 spawn |
| `test_open_sends_absolute_tape_path` | **新增** | **回归①**：捕获 attach 的 tape 参数，断言 `isabs` 且 == `str(tape.resolve())` |
| `test_open_explicit_port_foreign_orca_exits_two` | **新增** | 显式 --port 被别项目 orca 占 → exit 2 |

**`tests/iface/web/test_attach.py`（更新/新增）：**
- 断言 `/api/health` 返回 `runs_dir_fp`（12 hex）且 == `runs_dir_fingerprint(manager.runs_dir)`；
  断言响应**不含**明文 runs_dir 路径（防泄漏）。

**`tests/iface/cli/test_web_registry.py`（新文件）：**
- `read_registry`：缺失/损坏 JSON/非 dict → None；`write→read` roundtrip；`registry_path` 落 `<runs_dir>/.orca-web.json`。

---

## 工作流 B：`bootstrap` 自动开 web（默认开）

### 设计

- `bootstrap` 的 stdout 是**机器契约**（JSON / `--format prompt` pointer，TARS skill 逐字解析）。
  自动开 web 必须 **detached 子进程**做，**绝不**写 stdout / 阻塞 / 拖垮 bootstrap。
- 复用工作流 A 修好的 `_open_run`：detach spawn `orca open <run_id>`，它在后台 probe→ensure
  server→attach tail-follow→开浏览器→退出（与 chart/sidechain 守护同款 detach pattern，
  `in_session/cli.py:179`）。`orca open` 本身**零改动**。
- 默认**开**；`--no-open-web` 单次关，`ORCA_BOOTSTRAP_OPEN_WEB=0` 粘性关（flag > env > default=on）。
- 仅真启动路径（带 `--inputs`、marker 已落）触发；schema-only 早退路径（不带 `--inputs`）不触发。

### 文件

**`orca/iface/in_session/cli.py`：**

(a) `bootstrap` 签名加 flag：
```python
open_web: bool = typer.Option(
    None, "--open-web/--no-open-web",
    help="启动后自动开 web（默认开；ORCA_BOOTSTRAP_OPEN_WEB=0 可粘性关）",
),
```

(b) 解析有效设置（flag > env > 默认 on）：
```python
def _bootstrap_open_web_enabled(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    env = os.environ.get("ORCA_BOOTSTRAP_OPEN_WEB")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    return True  # 默认开
```

(c) post-lock 块（marker 已落 + chart/sidechain 守护 spawn 之后，建 reply 之前，~1056 行）：
```python
if _bootstrap_open_web_enabled(open_web):
    _spawn_open_web(run_id)   # detach，soft-fail warn
```

(d) 新 helper `_spawn_open_web(run_id)`：镜像 `_spawn_chart_daemon` 的 detach 模式
（`start_new_session=True` + 日志重定向到 rundir，**非 DEVNULL**——保留排查信息，与 chart 守护
一致见 `in_session/cli.py:184`）；`FileNotFoundError`（`orca` 不在 PATH）→ warn 不 fail。
```python
def _spawn_open_web(run_id: str) -> None:
    """bootstrap 后台开 web（detached，soft-fail）。复用 orca open 的跨项目 _open_run。"""
    log_path = _default_rundir() / f".orca-open-{run_id}.log"
    try:
        subprocess.Popen(
            ["orca", "open", run_id],
            stdout=open(log_path, "ab"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except FileNotFoundError:
        logger.warning("run %s: 自动开 web 失败——`orca` 不在 $PATH", run_id)
    except OSError as e:
        logger.warning("run %s: 自动开 web spawn 失败（%s）", run_id, e)
```

### 测试（`tests/iface/in_session/test_in_session_cli.py` 或新 `test_bootstrap_open_web.py`）

| 测试 | 覆盖意图 |
|---|---|
| `test_bootstrap_open_web_spawns_orca_open` | 默认（无 flag）→ `_spawn_open_web`/Popen 被调，参数含 `[orca, open, <run_id>]` + detached |
| `test_bootstrap_no_open_web_disables` | `--no-open-web` → 不 spawn |
| `test_bootstrap_open_web_env_disables` | `ORCA_BOOTSTRAP_OPEN_WEB=0` → 不 spawn |
| `test_bootstrap_open_web_stdout_contract_clean` | spawn 后 bootstrap stdout 仍是合法 JSON（含 run_id），**无** web 文本污染 |
| `test_bootstrap_schema_only_no_open_web` | 不带 `--inputs` → 不 spawn（schema-only 早退路径） |
| `test_bootstrap_open_web_missing_orca_soft_warn` | `orca` 不在 PATH → warn，bootstrap 仍 exit 0 + 正常 JSON |

## 验收

1. 上述测试全绿：`pytest tests/iface/cli/test_web_default_and_open.py tests/iface/cli/test_web_registry.py tests/iface/web/test_attach.py -q`
2. `python -m py_compile` 改动文件通过。
3. 铁律守门（`TestIronLaws`）回归：单 `_runs` registry 不破、TUI 仍在、attacher 只读。
4. 手动 E2E（可选，真机）：A 项目 `tars serve` 占 7428 → B 项目 `orca open <id>` 应在另一端口起自己的 server 并打开 B 的 run。
5. **bootstrap 自动开 web**：默认开 → post-lock detach spawn `orca open <run_id>`；`--no-open-web` /
   `ORCA_BOOTSTRAP_OPEN_WEB=0` 关；schema-only 不触发；stdout JSON 契约纯净（无 web 文本）。

## 偏离 SPEC 处 / SPEC 同步（spec-review B1，blocker）

**必须同 PR 修订 `docs/specs/web-attach-and-default-spec.md`**（SPEC 是契约不是建议，不许漂移）：

- **L87** health 契约：`{app:"orca", version, pid}` → `{app:"orca", version, pid, runs_dir_fp}`，
  并补一句："`runs_dir_fp = sha1(resolve(runs_dir))[:12]`；client 据此判定 server 是否同项目。
  缺该字段（旧 server）→ client 视为 foreign → 安全降级到 spawn（可能留孤儿 server，见 F2）。"
- **L88** `orca open` 流程：补「项目感知复用」语义——"探测 7428 是 orca 且 `runs_dir_fp` 匹配
  本项目 → 复用；否则按 per-project 登记文件 `<runs_dir>/.orca-web.json` 找本项目 server，
  无则空闲端口起新 server 并登记。tape 路径跨进程以**绝对路径** POST。"
- 验收新增一条："SPEC diff 含上述 L87/L88 更新。"

向后兼容：health 加字段是纯加法（旧 client 忽略新字段）；旧 server 缺字段 → 新 client 视为
foreign → spawn 新 server（F2 升级窗口期可能留孤儿，用户感知后手动 `pkill -f 'tars serve'`）。

## 风险/疑问

- **R1（依赖重量）**：`run_manager.py` 顶层若拉 fastapi/uvicorn，`_runs_dir_fp` 的 lazy import
  会让 `open` 路径变重。实现期核对；过重则函数下沉无依赖模块或 commands 内联副本（≤2 处，DRY 允许）。
- **R2（登记文件落 runs/）**：`<runs_dir>/.orca-web.json` 与 `<rundir>/orca-<run_id>.json` marker
  同处，已被 `runs/` gitignore 覆盖；assets 路由按 `run_id/assets/` 作用域服务，不暴露根级 dotfile。
- **R3（pid 仅存档）**：复用判定以 health 探测为权威，pid 不 gate（避免 `tars` wrapper fork 导致
  误判失活 → 漏复用）。pid 仅供诊断/未来 stop 功能。
- **R4（范围外）**：`orca run` 的 reuse 分支（`_post_run_to_existing`）同样"7428 有 orca 即复用"，
  对**发起新 run** 存在同类跨项目隐患（会把 run 落到别项目 runs_dir）。本次不动（用户问的是 open），
  作为 follow-up 记 CHANGELOG。
- **R5（bootstrap 契约，工作流 B）**：自动开 web **必须**只以 detached 子进程形式做——绝不写
  bootstrap stdout / 阻塞 / 拖垮 bootstrap。测试 `test_bootstrap_open_web_stdout_contract_clean`
  守门 stdout 仍为合法 JSON。`_spawn_open_web` 失败一律 soft warn（`orca` 不在 PATH / OSError）。
- **R6（默认开的副作用）**：默认开后每个 in-session run 触发一次 detached `orca open`。靠工作流 A
  的复用机制（同项目 server 复用 + registry）+ bootstrap dup-check（同 wf 不重复 bootstrap）把
  实际 server/tab 数控到「每 distinct run 一个」。循环/测试场景用 `ORCA_BOOTSTRAP_OPEN_WEB=0` 粘性关。
- **R7（日志重定向而非 DEVNULL）**：`_spawn_open_web` 子进程 stdout/stderr → `runs/.orca-open-<run_id>.log`
  （非 DEVNULL），与 chart 守护一致，保留 attach/spawn 失败的排查信息；该日志已被 `runs/` gitignore 覆盖。

---

## spec-review 第二轮闭环（evaluator 合并后；**覆盖前文 §3/§4 对应细节**）

第二轮 conditional-pass（6 blocker + 5 HIGH）。逐条裁决如下，**实施以本节为准**。

### 接受（改计划/代码）

- **B1 已落**（SPEC 同步，见上「偏离 SPEC 处」）。
- **B2 已落**（`_default_runs_dir` 从 `bg_runner.default_tape_path` 派生）。**补 guard 测试**
  `test_default_runs_dir_single_source`：断言 `_default_runs_dir()` 与 `RunManager()` 默认 runs_dir
  basename 一致（跨层不能共享常量——web 禁 import cli，故用测试守门防漂移）。
- **B3 已落**（指纹下沉 `orca/iface/web/_identity.py`，stdlib-only，R1 hedge 删除）。
- **B4 已落**（`_register_my_port` 写失败 loud 可操作 warn）。**补测试**
  `test_register_my_port_failure_loud_warn`：mock `write_registry` 抛 OSError → 断言 stderr 含
  「registry 写失败」+ 本次 open 仍 attach 成功（不阻断）。
- **H1 接受 → 级联简化（覆盖 §3/§4a/§4b/§4c 的 pid 相关）**：
  - registry 字段 = `{port, runs_dir_fp}`（**删 pid**——Popen.pid 可能是 `tars` wrapper pid，是潜在
    错误数据；且 pid 不 gate 任何决策，YAGNI）。
  - **`_spawn_background_serve` 保持返回 `bool`**（不改签名 → 零测试 churn，`test_spawn_background_serve_returns_false_when_orca_missing` 仍有效）。
  - `_register_my_port(my_runs_dir, *, port, fp)`（无 pid）；loud warn 改报 port：
    `f"⚠ web registry 写失败（{e}）；server 已起 port={port} 但下次 orca open 会重复 spawn，可 lsof -ti tcp:{port} | xargs kill 或忽略"`。
  - §4(c) spawn 块：`if not _spawn_background_serve(bind_host, actual_port): return EXIT_RUN_FAILED`。
- **H3 接受**：`tests/iface/web/test_attach.py` 安全 6-tuple **用绝对路径形态补跑**——`<tmp>/runs/../../etc`
  绝对化后 `/etc` 仍被 `relative_to` 拒；正常 `<tmp>/runs/x.jsonl` 绝对路径仍通过。
- **H4 接受**：`test_bootstrap_open_web_stdout_contract_clean` oracle 精确化——(a) `json.loads(stdout)`
  成功；(b) schema `{run_id:str, tape:str, done:bool}`；(c) regex 负向断言 stdout 不含 `http://` /
  `webbrowser` / `Orca Web UI`。
- **H5 接受（文档化）**：`_identity.py` docstring + SPEC L87 补 threat note——"指纹不可逆推路径，但同
  项目多次 bootstrap 指纹稳定；bind 0.0.0.0 时内网观察者可跨 session 关联同项目（威胁面：被动观察）。
  缓解（follow-up）：fp 仅在内部 header/loopback 下返回，或默认 bind 127.0.0.1。"
- **B6 接受（文档化）**：风险加 R8——"本 PR 不支持 custom runs_dir：`tars serve` 不接受 `--runs-dir`，
  `orca open` 也不透传；故 `mcp --with-web --runs-dir /custom` 起的 server 与 client 指纹永远不匹配
  → 永远 spawn 新 server。Follow-up：`tars serve --runs-dir` + `orca open` 透传 + 同源算指纹。"
- **F3 接受**：`_spawn_open_web` Popen 加 `close_fds=True`（与 `_spawn_chart_daemon:208` 对齐）。
- **F8 接受（文档化）**：风险加 R9——"`_find_free_port` → `_spawn_background_serve` 间 TOCTOU（端口被
  抢）由 `_wait_for_health` 10s 超时 fail loud 兜底（既有行为）。"
- **F9 接受**：§4(c) 决策块显式 `my_runs_dir = _default_runs_dir()`（伪代码变量补全）。
- **F10 接受**：`TestIronLaws` 加守门——grep `orca/iface/web/**` 内 `import orca.iface.cli` 应为 0
  （守本次新设的 lazy import 方向：web 禁反依赖 cli）。

### H2 部分接受（裁剪）

- **接受**：`tests/iface/web/test_attach.py` 加 **真 health 端点**（非 mock）测试——起一个真
  `tars serve`（或 `create_app(manager)` + httpx ASGI），断言 `GET /api/health` 返回的 `runs_dir_fp`
  == `runs_dir_fingerprint(manager.runs_dir)`，且不同 runs_dir → 不同 fp。覆盖 health→fingerprint 真链路。
- **不强制**全双真 server E2E：决策逻辑（fp 匹配→复用/不匹配→spawn）由 mock 测试清晰覆盖意图
  （断言 spawn 被调/不调），AC4 手动 E2E 作为最终跨项目确认保留。理由：双真 server pytest 重（启动/
  端口/teardown），边际价值低于「真 health 端点测试 + mock 决策测试」组合；符合 Simplicity First。

### 降级/驳回（附理由）

- **B5（registry flock 并发 race）→ 降级为文档化已知限制**，不加 flock。理由：
  (1) race 窗口窄——需 bootstrap auto-open 的 detached `orca open` 与用户手动 `orca open` 在同一
  ~10s spawn 窗口内并发、且 7428 foreign、且无 registry；非「必然」。
  (2) **正确性不受损**——probe 为权威，孤儿 server 不被 registry 引用也不污染复用判定，仅闲置一个
  uvicorn 进程（可 kill）。
  (3) 正确的 flock 修复需跨 spawn+`_wait_for_health`（~10s）持锁 → **串行化同项目所有 open** → UX
  退化（比偶发孤儿更差）；lock-free 协议又违 Simplicity First。
  → 风险加 R10：「并发 open（auto+manual 同窗口）极小概率产生 1 个闲置孤儿 server，probe-权威设计
  保证正确性，由下次成功 registry 自愈；不做 flock（持锁串行化代价 > 偶发孤儿）」。
- **F5 驳回（H1 后 moot）**：`_spawn_background_serve` 保持 bool → `test_spawn_background_serve_returns_false_when_orca_missing` 名字/断言不变。
- **F6 驳回**：保留文件名 `web_registry.py`（cli 侧 web-server 端口登记，语义清晰，无需改名）。
- **F7 部分接受**：保留 py_compile（快），AC 额外加 import smoke
  `python -c "import orca.iface.cli.commands, orca.iface.in_session.cli, orca.iface.web._identity"`（catch import 环/重依赖）。

### 测试增量清单（合并两轮，实施时全做）

`test_web_default_and_open.py`：mock probe 补 fp（4 处既有用例）+ `test_open_foreign_project_server_spawns_new`
+ `test_open_reuses_registry_port_when_default_foreign` + `test_open_sends_absolute_tape_path`（断言
`== str(Path(tape).resolve())` 字面等）+ `test_open_explicit_port_foreign_orca_exits_two`（断言 attach/spawn
未调）+ `test_old_server_missing_fp_treated_as_foreign` + `test_default_runs_dir_single_source` +
`test_register_my_port_failure_loud_warn` + `test_fingerprint_single_point_consistency`（`commands._runs_dir_fp`
== `_identity.runs_dir_fingerprint`）。
`test_web_registry.py`（新）：read/write roundtrip + 缺失/损坏→None + `registry_path` 位置。
`test_attach.py`：真 health 端点 fp 断言 + 绝对路径安全 6-tuple 补跑 + 无明文路径泄漏。
`test_bootstrap_open_web.py`（新）：见 §B 表 + H4 精确 oracle + `test_bootstrap_open_web_format_prompt_also_triggers`。
