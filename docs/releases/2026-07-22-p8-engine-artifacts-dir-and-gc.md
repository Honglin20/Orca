# P8:引擎注入 `$ORCA_ARTIFACTS_DIR` + `orca gc` 命令

> Phase 4-A / 4-C。commit `b1eaf43`。计划 `docs/plans/2026-07-21-workflow-redesign.md` §Phase 4。

## 背景
两套 run_id 不合流:引擎 `runs/<run_id>/` vs workflow 自建 `llm_artifacts/<model>/runs/<timestamp>/` → 产物散落、查「run X 产出在哪」要 grep、老 run 不清。P2 已修孤儿目录;本任务让引擎统一注入权威产物目录,并加 gc。

## Phase 4-A:`$ORCA_ARTIFACTS_DIR` 注入(单一真相源)
- `artifacts_dir_for_run(runs_dir, run_id)` 落 **`orca/chart/_paths.py`**(非 `orca/run/`——exec/ 禁反向 import run/,contract test `test_dependency_no_run_no_compile` 守门;chart/_paths 已是 run 派生路径中枢,含 `chart_sock_path`,各层安全 import)。
- `orca/exec/env.py::build_env_overlay()` 加 `artifacts_dir` keyword(第 6 个 ORCA_*,缺省空串=不注,4 旧调用方零回归)。
- `exec/script.py` + `exec/claude/executor.py` 加 `_resolve_artifacts_dir`(SPEC §11 #9 函数级 mirror,与 `_resolve_chart_sock_path` 同款);spawn 时注 env。
- `iface/in_session/cli.py::_write_orca_env()` 加 `artifacts_dir: Path`;bootstrap `mkdir -p` + 注 env;next 透传同一 per-run 常量。

**P9 接口约定**:env `ORCA_ARTIFACTS_DIR` = `<abs>/runs/<run_id>/artifacts/`;workflow `source orca_env.sh` 后 `os.environ["ORCA_ARTIFACTS_DIR"]` 读;替换自建 `llm_artifacts/...`;worktree 写 `$ORCA_ARTIFACTS_DIR/.worktrees/MANIFEST.json` 供 gc 回收。

## Phase 4-C:`orca gc`
- `orca gc --max-age 14d [--keep N] [--dry-run] [--runs-dir DIR]`,复用 `orca.exec.wait.parse_duration`。
- 4 类候选:stale-run / orphan-dir / orphan-marker / orphan-lock。
- 安全(fail loud):active run 永不删;`resolve()` 守门防 `..`/symlink 逃逸;`--max-age`/`--keep` 都不给 → BadParameter;`.orca-gc.lock` advisory lock serialize 多 gc;持 MANIFEST 的 stale-run 跳过删(保 P9 worktree 闭环)。

## 测试
67 新测试全过:`tests/chart/test_artifacts_path.py`(9)+ `tests/exec/test_env.py`(+4)+ `tests/iface/in_session/test_gc.py`(28,含 safety + correctness)。回归 `tests/exec/` 全套 + in_session + bootstrap 664 passed / 1 failed(integration `test_smoke_real_claude_spawn`,CI skip,与 P8 无关)/ 1 skipped。spike `test_real_orca_two_node_closed_loop` NameError 已修(pass)。

## code-reviewer 闭环
🔴 worktree MANIFEST 语义(持 MANIFEST 的 run 跳过删)+ 🟡 orphan-dir/marker 永久孤儿(`_is_tape_flock_held` helper)+ 🟡 gc↔bootstrap race(advisory lock)+ 🟢 顶层 import / dead branch 全修。
