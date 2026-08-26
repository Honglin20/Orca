# Release Note：测试解析 CLI 输出前去 ANSI（修 2 个 pre-existing 假报）

> 2026-07-24 · test-only · commit `df5380a`

## 现象

`tests/iface/in_session/` 长期 2 个 pre-existing 失败（与任何近期生产改动无关）：
- `test_skill_md_flags_subset_of_cli_help`
- `test_cli_gc_max_age_zero_rejected`

## 根因

typer/Rich 即使在 `CliRunner`（非 tty）下，仍给 `--help` 与 `BadParameter` 错误输出上 ANSI 色，并把 flag token **拆碎**：`--run-id` 被渲染成跨 span 的 `-` + `run` + `-id`（每个片段裹独立 ANSI 码）。

- `test_skill_md_flags_guard._help_flags` 用 `re.findall(r"--[a-zA-Z]...")` 抽 flag，但 `--` 后紧跟 `\x1b`（ANSI）→ regex 全 miss → `orca <cmd> --help` 抽到**空集** → SKILL.md 所有真实 flag（`--run-id`/`--inputs`/`--output` 等）都被判「CLI 未声明」→ guard 假报。
- `test_cli_gc_max_age_zero_rejected`：`--max-age 0` 触发 `Invalid value: --max-age 必须为正：'0'`，typer 把参数名 `--max-age` 高亮拆碎 → `"max-age" in r.output` 失配。

**均为测试断言太脆，生产逻辑没问题**：gc 正确 exit 2 + 中文「必须为正」；next/open/stop/status 的 flag 都真实声明。

诊断证据：`orca next --help` 原始输出 `'--run-id' in out` → False；`_help_flags("next")` → `[]`；去 ANSI 后 → `['--run-id','--output','--inputs','--tape','--no-memory','--log-level','--help']`。

## 修复（test-only）

- `tests/iface/in_session/test_skill_md_flags_guard.py`：加 `_strip_ansi`，`_help_flags` 去色后再 regex。
- `tests/iface/in_session/test_gc.py`：加 `import re` + `_strip_ansi`，`test_cli_gc_requires_max_age_or_keep` 与 `test_cli_gc_max_age_zero_rejected` 两个 CLI 错误断言去色后再匹配。

## 验证

`test_gc.py + test_skill_md_flags_guard.py`：**33 passed**（原 31 passed + 2 failed）。

## 不做

- 不改 typer/Rich 全局配色（真实 tty 用户看彩色输出是预期；仅测试解析需去色）。
- 不抽共享 conftest helper——目前仅这两个文件命中，局部 `_strip_ansi` 即可；后续更多测试命中再 DRY 抽出。
