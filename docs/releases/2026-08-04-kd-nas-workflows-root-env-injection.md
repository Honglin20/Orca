# Release: kd-nas headless 修复——`ORCA_WORKFLOWS_ROOT` env 注入 + agent.md 去 cwd-relative

**日期**: 2026-08-04
**Commit**: `a952ecc`
**类型**: 架构修复（P0）+ 契约强化（P1）

## 背景

test-agent 真跑 `tars run ~/.orca/workflows/kd-nas.yaml`（CWD=用户项目目录 `examples/mnist_kd/`）暴露：`kd-setup/agent.md:72` 的 `find workflows/agents/_kd_scripts -name kd_common.py` 是 **cwd-relative 查找**——agent CWD 是用户项目（非 Orca 仓库根），`workflows/` 在 CWD 下不存在 → `find` 报 `No such file or directory`。同病点扩散到 `kd-train-script/agent.md:45`、`teacher-gen/agent.md:156`、`agent-struct-exploration.yaml:131`（struct-curator inline）。

根因不是单个 agent.md 写错，而是**通用 exec 层缺一个「workflow 源根」env 注入**：executor 注入了 `ORCA_ARTIFACTS_DIR`（产物目录）/ `ORCA_AGENT_RESOURCES`（agent 资源）/ `ORCA_KB_DIR`（KB 根），但没注 workflow 自身的 yaml 所在目录——agent.md 只能 cwd-relative 猜。

## P0 架构修复（executor env 注入 + agent.md 路径派生）

### 设计决策（Rule 7：surface conflicts）

用户首选 `ORCA_KD_SCRIPTS_DIR` + `ORCA_STRUCT_SCRIPTS_DIR`（kd-nas 专属名）；实现前 surface 了一个 OCP 冲突：

- `build_env_overlay`（`orca/exec/env.py`）是**通用**层——所有现有 `ORCA_*` 名都是通用语义（ARTIFACTS/AGENT_RESOURCES/KB/chart 路由）。加 kd-nas 专属名 = 把 workflow-specific 概念硬编码进通用 exec 层；下一个 workflow 出现共享脚本目录（如 `_quant_scripts` 已存在）就要再改 3 处。
- 选了**单通用 env** `ORCA_WORKFLOWS_ROOT`（= workflow yaml 所在目录绝对路径）。OCP-clean：新 workflow 出现共享资源目录时零 exec 层改动，agent.md 自己派生子路径。
- 用户确认采纳此方案（推翻其首选）。
- `ORCA_KD_SCRIPTS_DIR`（`train_pipeline.py` 已读此 env，fallback `__file__` parent）继续在 agent.md bash inline export（`distill:216` / `train-teacher:109` 已是此模式），零引擎改动。

### plumbing 全链（kwarg 默认 None → 旧调用零回归）

`load_workflow(yaml_path).parent` → run 启动期解析 → 透传到 executor spawn：

- `orca/exec/env.py:build_env_overlay` 加 `workflows_root` kwarg → 注 `ORCA_WORKFLOWS_ROOT`
- `orca/exec/factory.py:make_executor` 加 `workflows_root` kwarg（agent + script 分支对称透传）
- `orca/exec/claude/executor.py:ClaudeExecutor` 构造 + `_build_spawn_config` 注入
- `orca/exec/script.py:ScriptExecutor` 构造 + `_build_spawn_env` 注入
- `orca/run/orchestrator.py:Orchestrator.__init__` / `from_tape` / `_bare_instance` 全 3 构造路径透传
- `orca/run/__init__.py:run_workflow`（库 API）+ `orca/run/__main__.py`（`python -m orca.run`）透传
- `orca/iface/cli/app.py:OrcaApp` + `orca/iface/cli/commands.py`（fresh run + headless daemon + resume）透传
- `orca/iface/web/run_manager.py:RunManager.start_run`（含 `InProcessRunHandle.workflows_root` 字段）透传

### agent.md 去 cwd-relative

| 文件 | 旧（cwd-relative） | 新（env 派生 + fail loud） |
|------|------|------|
| `kd-setup/agent.md:72-74` | `find workflows/agents/_kd_scripts ...` + `abspath('workflows/agents/_struct_scripts')` | `$ORCA_WORKFLOWS_ROOT/agents/_kd_scripts` + `[ -f .../kd_common.py ] \|\| exit 2` |
| `kd-train-script/agent.md:45` | `find workflows/agents/_kd_scripts ...` | 同款 |
| `teacher-gen/agent.md:156` | `abspath('workflows/agents/_kd_scripts')` | 同款 |
| `agent-struct-exploration.yaml:131` | `STRUCT_SCRIPTS_DIR="workflows/agents/_struct_scripts"` | `$ORCA_WORKFLOWS_ROOT/agents/_struct_scripts` + `[ -f .../measure_baseline.py ] \|\| exit 2` |

agent.md **fail loud 三档**（覆盖 deepseek-v4-flash 可能撞到的空值路径）：
1. env 缺：`[ -n "$ORCA_WORKFLOWS_ROOT" ] || exit 2`
2. dir 缺：`[ -d "$STRUCT_SCRIPTS_DIR" ] || exit 2`
3. file 缺：`[ -f "$KD_SCRIPTS_DIR/kd_common.py" ] || exit 2`

## P1 fail-loud 契约强化

`kd-setup/agent.md` 的「严禁」块加：**step 非零退出时禁止返回含空字符串字段的 JSON 占位对象**（如 `{"kd_artifacts_dir":"", ...}`），必须上抛原始 stderr/stdout。覆盖 deepseek-v4-flash 倾向填满 schema 的行为（test-agent 实测命中此模式）。

## 单测

- `tests/exec/test_env.py`：3 个新测试（注入 / 缺省不注 / 与 artifacts_dir+kb_dir 共存）
- `tests/exec/claude/test_executor_env_inject.py`：3 个新测试（`_build_spawn_config` 注入 / 缺省 / `ClaudeExecutor(workflows_root=...)` 构造存储）

## 验证

### 守门测试
- `tests/workflows/test_kd_prompt_no_source_narrative.py`：31 passed（agent.md 任务纯净态无回归——新增 bash 注释是描述「当前怎么做」的合法 why 注释，不触 deny-list）
- `tests/compile/test_validator.py`：全绿
- `tests/exec/test_env.py` + `tests/exec/claude/test_executor_env_inject.py`：30 passed

### 全 suite 回归对比
- 本分支：899 passed / 25 failed / 13 errors
- master（无改动）：893 passed / 25 failed / 13 errors
- **25 failed + 13 errors 全是 pre-existing**（test isolation 问题，单跑全绿）；本 commit 比 master 多 6 个 passing（新单测）

### 核心 fix 直接验证（绕过 flatten P3 延迟）
模拟 `kd-setup/agent.md` step1 在 **生产场景**（CWD=`/tmp`，`ORCA_WORKFLOWS_ROOT=~/.orca/workflows`）跑：
```
OK: KD_SCRIPTS_DIR=/home/mozzie/.orca/workflows/agents/_kd_scripts
OK: STRUCT_SCRIPTS_DIR=/home/mozzie/.orca/workflows/agents/_struct_scripts
OK: kd_common.py exists
OK: train_pipeline.py exists
ORCA_KD_SCRIPTS_DIR seen by python: /home/mozzie/.orca/workflows/agents/_kd_scripts
```
旧 `find workflows/agents/_kd_scripts ...` 在同 CWD 必失败（`workflows/` 不存在）。

executor env_overlay 端：
```python
cfg.env_overlay["ORCA_WORKFLOWS_ROOT"] == "/home/mozzie/.orca/workflows"  ✓
```

### headless e2e（部分）
从 `examples/mnist_kd/`（CWD=用户项目目录，yaml=`~/.orca/workflows/kd-nas.yaml`，即原 bug 复现场景）`tars run --background`：
- workflow_started / flatten node_started 正常
- flatten 节点跑 25min+ 仍未完（P3 deepseek 重读文件延迟，**显式不在本次修复范围**）
- 卡在 flatten（在 setup 之前），**未触达 setup 节点**——无法用此 e2e 证明 setup bash 实跑通

e2e 卡点 = P3（deepseek latency），非本 fix 范围。setup 节点的 bash 正确性由上面的「核心 fix 直接验证」独立证明。

## 不在本次范围（follow-up）

- **P2** `~/.orca/runs/<id>/log` 空文件（executor 日志 bug）—— executor 层问题
- **P3** flatten 9m46s+ deepseek 重读文件延迟 —— 优化层问题，本 e2e 实测命中（25min+ 仍跑）

两项是 executor/优化层问题，非 kd-nas 重构范围；记为 follow-up。

## code-reviewer 闭环

一轮 review，0 must-fix / 1 nice-to-have（已修）：
- 🟡 `run_workflow()` 库 API（`orca/run/__init__.py`）+ `python -m orca.run`（`orca/run/__main__.py`）缺 `workflows_root` 透传——本 commit 声明「plumbing 全链」的未完成项。**已修**（`__init__.py` 加 kwarg + `__main__.py` 传 `Path(args.yaml).resolve().parent`），amend 进同一 commit。

🟢 建议（未做，可选优化）：
- resolve 时机统一（Orchestrator 入口 resolve 一次，移除 executor 端兜底）
- 端到端 plumbing contract 测试（monkeypatch Popen 断言子进程 env）
- ScriptExecutor 对称测试（与 ClaudeExecutor 对称）
