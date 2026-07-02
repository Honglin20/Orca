# orca executor —— 持久化后端二进制配置 + 健康检查

> 计划：[`~/.claude/plans/warm-herding-falcon.md`](../../../.claude/plans/warm-herding-falcon.md)（本特性的设计契约）
> 日期：2026-07-02

---

## 背景

换 agent 后端 binary（如 `claude -p` → `ccr code -p`）此前只能靠**每次命令前缀 env**
（`ORCA_CLAUDE_CLI="ccr code" uv run orca run ...`）或改 shell rc。用户诉求是 `pip install`
后**设一次、全局生效**的持久化机制，外加一条命令自检能否真跑通。

底层机制本已就绪：`CliProfile.resolve_cli_path()`（`orca/profiles/base.py:65`）运行时
`os.environ.get(cli_path_env, default)`。本特性**只**在 `orca` 启动期把 `~/.orca/config.json`
里的 per-profile binary override 注入到对应 env var，现有 exec / profile / registry 链路
**零改动**（OCP）。

## 终态（用法）

```bash
orca executor set claude "ccr code"   # 一次性写入 ~/.orca/config.json，永久生效
orca executor test claude             # 真起子进程自检：✓ 端到端 OK / ✗ 给原因
orca executor show                    # 看当前每个 profile 的 effective binary
orca executor list                    # 列可用 profile + override 标记
orca executor unset claude            # 清除 override，恢复 default
orca run examples/demo_linear.yaml    # 之后所有 run 自动 spawn ccr code（无需任何前缀）
```

**优先级**：shell env > config 文件 > profile default（`setdefault` 实现，显式 `export` 永远赢）。

**边界**：只覆盖**同协议 binary 替换**（ccr code / claude-ds-flash / 不同路径的 claude）。
异协议后端（codex / opencode / gemini）需新 profile + translator（代码活），`executor test`
会帮用户提前发现「协议不兼容」。

## 改动

### 新增

- **`orca/iface/cli/config.py`**（NEW）—— config 持久化 + env 注入层：
  - `config_path()` → `~/.orca/config.json`（与 `~/.orca/runs/` 同源约定，`bg_runner.py:46`）。
  - `load_config()`：缺失→`{}`；JSON 损坏 / 顶层非 object → warn + `{}`（不阻断启动，fail loud 但降级）。
  - `save_config(cfg)`：原子写（tmp + `os.replace`，对齐 `bg_runner.write_meta`）。
  - `apply_config_env(cfg)`：先 `load_builtin_profiles` + `load_project_profiles(cwd)`（支持
    project profile 覆盖），遍历 `cfg["binaries"]`，`get_profile(name)` 取 `cli_path_env`，
    `os.environ.setdefault(env, binary)`。未知 / disabled profile → warn + skip（对齐
    `disable_profile` 风格）。**setdefault 非 =**：保 env > config > default。
  - `bootstrap_config()` = `apply_config_env(load_config())`，供 `main()` + 各 executor 子命令调用（幂等）。
  - `list_overrides(cfg)`：纯展示用，提取合法 `{profile: binary}`。
  - **依赖方向**：仅 `orca.profiles.registry`（iface → profiles 合法）；**禁止 import `orca.exec`**
    （本模块在 exec 启动前被 `main()` 调用）。

- **`orca/iface/cli/executor_cmds.py`**（NEW）—— `orca executor` sub-Typer（`show/set/unset/list/test`）：
  - **sub-Typer 形态**（`app.add_typer(executor_app, name="executor")`）：代码库其余命令是扁平
    `@app.command()`，此处是有据的局部偏离——`executor show/set/...` 共享名词，sub-Typer 让
    `orca --help` 更干净（模块 docstring 注明）。
  - `classify(seen_types, saw_result, exit_code, timed_out, stderr) -> tuple[bool, str]`：
    **纯函数**判定 PASS/FAIL（可测性 seam，gotcha R3）。有序判定：超时 → 退出码非 0 →
    非 stream-json/协议不兼容 → 收到 result 端到端 OK → 有事件无 result（PASS+warn）。
  - `_record_type(line, seen_types)`：解析 stdout 行顶层 `type`（非 JSON 跳过）。
  - `_iter_available_profiles()`：show/list 共享遍历，对并发 disable 容错（DRY）。
  - `_binaries_dict(cfg)`：set/unset 共享，对非 dict 坏值 warn + 重置（DRY + fail loud 一致性）。
  - **`test` 命令**：复用 `SpawnConfig` + `CLIRunner`（无需 AgentNode）。SpawnConfig 由
    `profile.resolve_cli_path()` + `profile.flags` + trivial prompt + `build_env_overlay`
    （透传 `ANTHROPIC_API_KEY` 等）组装。两层超时（gotcha G4）：逐行 `SpawnConfig.timeout=30`
    （stall 检测）+ 外层 `asyncio.wait_for(60s)` wall-clock 硬上限（防永续流挂死）。
    spawn 失败（`FileNotFoundError`/`OSError`）→ 干净 FAIL exit 1（gotcha G5）。

### 修改

- **`orca/iface/cli/commands.py`**：
  - `main()`（:849）函数内 `from orca.iface.cli.config import bootstrap_config; bootstrap_config()`
    后再 `app()`（函数内 import，保模块导入零副作用，对齐 textual 延迟 import 纪律）。
  - :294 附近模块级 `app.add_typer(executor_app, name="executor", ...)`（Typer 要求构建期装配，
    注释说明故非延迟 import）。
- **`orca/profiles/builtin/ccr.py`**：`translator=_dummy_translator` → `claude_translator`
  （加 import，删 dummy 定义，更新 docstring）。ccr 是 claude 协议兼容路由器（其 docstring 自述），
  复用 `claude_translator` 语义正确；此前 dummy 导致 `executor: ccr` 事件全丢。
  `claude_translator` 对未知 `type` 返回 `[]`（`translators/claude.py:70`），协议分歧优雅降级。

### 测试

- **`tests/iface/cli/test_executor_cmds.py`**（NEW，35 单测，不真起进程）：config 全函数
  （missing/corrupt/非 object/原子写/未知 profile warn+skip/env>config 优先级/bootstrap 幂等）、
  `classify` 全分支（含 result 优先 exit_code 的反直觉分支）、5 命令 exit code + stdout +
  config 写入、`test` 命令 monkeypatch CLIRunner 模拟 FileNotFoundError/内部超时/wall-clock
  超时/正常路径、set/unset 非 dict binaries warn。
- **`tests/iface/cli/test_executor_e2e.py`**（NEW，9 e2e，**不 mock CLIRunner**）：用伪造可执行
  脚本当 backend，端到端驱动 `resolve_cli_path → SpawnConfig → shlex.split → CLIRunner.stream →
  create_subprocess_exec → readline → on_result → classify` 全链路。覆盖 good/bad_json/
  nonzero_exit/stuck(wall-clock)/binary_not_found + 配置 round-trip + 多 token binary
  （`"ccr code"` shlex.split 真起）+ **env>config 反证**（env 指坏脚本→FAIL 证 env 赢）。
- **`tests/iface/cli/test_executor_integration.py`**（NEW，2 测试，`@pytest.mark.integration`）：
  真起 `claude`，双条件 skip guard（`shutil.which("claude")` + `ANTHROPIC_API_KEY`），CI 默认跳过。
- **`tests/profiles/test_registry.py`**：加 `test_ccr_translator_reuses_claude_translator`
  锁住 ccr translator 修复。

## 设计要点

**为什么不造「后端切换系统」而走 config → 注入 env**：
1. **OCP**：`resolve_cli_path` 本就是设计好的运行时扩展点（SPEC §318「二进制替换零改动」）。
   复用它 = 加能力不改核心——exec/profile/registry 全程零改动（`git diff master` 核对）。
2. **单向依赖**：config（iface）只读 profiles 的 env 名；exec/profile 不知道 config 存在——
   无第二套真相源。binary 选择仍只在 `resolve_cli_path` 一处定。
3. **优先级自然**：`setdefault` 让 shell env 永远赢，config 是中间层 fallback，profile default 兜底。

**两层超时**（clean-code-builder 自审抓出的真 bug）：CLIRunner 内部逐行超时（`SpawnConfig.timeout=30`）
走「正常结束生成器 + 标记 `runner.timed_out=True`」路径（`runner.py:196-197`），**不抛异常**。
`test` 命令必须读 `runner.timed_out` 属性，否则卡死二进制（内部已 SIGTERM）会被误判为「非
stream-json」或「退出码 -1」，丢失「超时」诊断。外层 `asyncio.wait_for(60s)` 兜底防永续流
（逐行 timeout 抓不住持续吐行不 stall 的 backend）。两条路径独立、各有专属测试。

## 验证

- **新测试**：`test_executor_cmds.py`(35) + `test_executor_e2e.py`(9) + `test_registry.py`(+1) → 全绿。
- **全量回归**：`uv run pytest tests/ -m "not integration"` → **1031 passed / 1 skipped /
  40 deselected**（基线 1020 + 11 新非 integration 测试，**0 回归**）。
- **集成**：`test_executor_integration.py` 无 claude/key 时 skip（不阻断 CI）。
- **手动 E2E（待用户在装了 ccr/claude + key 的环境验）**：`orca executor set claude "ccr code"`
  → `test` ✓ → `run` 实际 spawn ccr code → `ORCA_CLAUDE_CLI=claude run` 临时回 claude（证 env>config）。

## review 闭环（code-reviewer 终审）

**0 🔴 / 1 🟡（已修）/ 2 🟢（不阻塞，跳过）**。
- 🟡（已修）：`set`/`unset` 对非 dict `binaries` 静默丢弃，与 `load_config`/`apply_config_env`
  的 warn 风格不一致 → 抽 `_binaries_dict` helper 统一 warn（顺带消 🟢#2 重复）+ 2 个测试。
- 🟢（跳过）：`_isolated_config` fixture 在 3 个测试文件重复（测试容忍度高，动 conftest 有风险）。
- 安全核查：`create_subprocess_exec` 非 shell=True，argv 经 `shlex.split` 拆 list 传入，config
  binary 字符串无 shell 注入风险。

## commit

_(SHA 提交后回填)_

## 后续

- 真起 claude 的端到端 manual 验证（需 key + claude CLI）。
- 若将来加异协议 backend（codex/opencode）：新增 `builtin/<name>.py` profile + `translators/<name>.py`，
  `executor set`/`test` 自动支持（零 config/executor_cmds 改动）。
