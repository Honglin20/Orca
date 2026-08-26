# KB 可移植基础设施 + struct-exploration 结构优先（direction 覆盖软闸）

> commit `6e0f167`（Part 1 KB infra）+ `0be8c6d`（Part 2 struct direction gate）。计划 `~/.claude/plans/sprightly-questing-donut.md`。

## 背景
`agent-struct-exploration` 设计上靠 KB 的 `latency_moves` / `directions` 驱动结构搜索，但实测两个结构性缺陷导致「只改超参、不碰结构」：

1. **KB 不可移植 + 缺失静默**：`knowledge_base/` 是仓库根裸相对路径（`agent-struct-exploration.yaml`），100% 靠 LLM agent 按 CWD 解析（`orca/` python 零代码解析）；`orca install` 只部署 `workflows/` + `agents/`，**不部署 KB**。换项目跑 → KB 找不到 → setup agent **静默继续**（无 fail-loud）→ kb_cache 空 → hypothesizer 读不到 latency_moves → 弱 LLM 只能保守改数值。用户看到的 wireless principle 经 grep 全 KB 确认是 agent **自己编的**（KB 里没有），印证 agent 凭参数化知识而非 KB 干活。
2. **探索策略零方向感**：`ledger_reducer.py` 的 `route_mode` 只看「换没换 champion」，`reject_hyperparam_only` 固化 false（超参自由通过、能当 champion），终止只有 `champion_met | max_rounds`，无「结构方向耗尽」概念。

用户拍板：① kd-nas defer；② **软闸**（prompt 加强，不动 reducer 硬拒绝）；③ **KB direction 目录**作 tried/untried 枚举基准。

## Part 1：KB 可移植基础设施（两 workflow 共享）
- **install 部署**（`install_cmds.py`）：`_install_bundled_knowledge_base` 克隆 `_install_bundled_workflows`，`copytree(dirs_exist_ok=True)` merge 语义（同 agents 池）部署 `knowledge_base/` → `~/.orca/knowledge_base/`，`run_install` 调用。
- **路径解析**（`config.py`）：`resolve_kb_dir()` 确定性解析 KB 根，优先级 `env ORCA_KB_DIR > config knowledge_base_dir(project>user) > ~/.orca/knowledge_base > cwd/knowledge_base`，first-existing。**显式来源（env/config）权威**——设了但目录不存在 → 返回空（不静默回退，让 fail-loud 暴露错路径）；隐式来源 best-effort。`knowledge_base_dir` 字段复刻 `sidechain` 路径解析维度（project 覆盖 user，不进 `CONFIG_FIELDS`）。
- **ORCA_KB_DIR 注入**（`env.py`）：`build_env_overlay` 加 `kb_dir` keyword（同 `artifacts_dir`）；`executor.py` + `script.py` spawn 点读 `os.environ['ORCA_KB_DIR']` 显式 overlay——**env 作 transport，exec 不 import iface（依赖单向）**；`in_session/_write_orca_env` 写 `export ORCA_KB_DIR`。
- **缺失 fail-loud**（`config.py::apply_kb_requirement`）：workflow `requires:[knowledge_base]` 且 KB 解析不到 → `ConfigurationError`（含 searched 路径 + 修复指引：`orca install` / config `knowledge_base_dir` / `export ORCA_KB_DIR`）。在 `orca run` 四条路径（web/headless/TUI/background）+ in_session bootstrap 预检，**run 启动即停，不进 setup agent**。`schema/workflow.py::Workflow` 加 `requires: list[str] = []`（默认空，旧 workflow 零回归）。选 fail-loud 而非 ask-user 哨兵：KB 缺失是环境/安装缺口，不是「agent 缺用户知道的项目事实」。

## Part 2：struct-exploration KB 驱动的结构优先（软闸）
核心：**确定性算 untried，LLM 软引导选 untried**（贴合 `[[deterministic-over-model-mediated]]`）。
- **`direction_coverage.py`（新增，`_struct_scripts/`，纯函数确定性）**：读 KB `meta.json` 枚举本族 direction 目录（wireless D0-D21）+ 读 ledger 收集 tried `direction_id` → 输出 `{catalog, untried, all_exhausted, near_target, coverage_ratio}`。tiers 族用 `directions/`，单层族（cnn/transformer 无 meta.json）catalog 空。`near_target` = champion 已在目标带（latency ≤ target×1.15 且 met_accuracy）。
- **`ledger_reducer.py`**：candidate 可选透传 `direction_id` 进 ledger（不在 `_LEDGER_REQUIRED`，旧 ledger 向后兼容）。
- **struct-hypothesizer**：Step 0 每轮跑 `direction_coverage.py` 拿覆盖信号 → 软闸（catalog 非空且 untried 非空 → **必须**选 untried 方向；`all_exhausted`/`near_target` 才允许 hyperparam）；补读 directions 切片（原漏读 index.json 声明的 `{family}.directions/{selected}`）；output 加必填 `direction_id`（`Dx` / `hyperparam` / `off_catalog:指纹`）。
- **struct-curator**：candidate 记 `direction_id`（透传 hypothesizer）→ ledger；KB 写回路径改 `$ORCA_KB_DIR`。
- **yaml**：顶层 `requires:[knowledge_base]`；setup Phase A Step 6 缓存命中族 `directions/`+`meta.json` + KB 根改 `$ORCA_KB_DIR`；hypothesizer `output_schema` 加 `direction_id`。

## 测试（committed）
- `tests/workflows/test_direction_coverage.py`（13）：catalog 枚举（wireless 22 / cnn 空 / 未知族空）/ compute_coverage（空 ledger 全 untried / tried 扣减含 off_catalog / 旧 ledger 向后兼容 / all_exhausted / 单层族）/ near_target（band 内/外/无 champion）/ KB 缺 fail-loud exit 1。
- `tests/iface/cli/test_config_kb.py`（8）：resolve_kb_dir 优先级（env>隐式 / 显式不存在→空 / cwd 回退）+ apply_kb_requirement（no-op / 写 env / 缺 KB ConfigurationError 含指引 / 未知 token no-op）。
- `tests/exec/test_env.py`（+3）：build_env_overlay kb_dir（注入 / 缺省不注 / 与 artifacts_dir 共存）。
- `tests/compile/test_validator.py`（+2）：requires 白名单（known 放行 / typo fail loud）。
- `tars validate agent-struct-exploration` 0 error。
- 回归：compile + env + direction_coverage + struct_kd_p7 + config_kb 195 passed；exec 41 + struct/kd 32 全过（修 executor `os` import 后零回归）。预存环境失败（`uv` 未装 / cc-nudge 脚本反引号断言）非本次回归。

## 不在本次范围（defer）
- kd-nas 接入 KB（用户 defer；其 hypothesizer/engineer 当前完全不读 KB、无 structure_gate）。
- 硬闸（reducer `reject_hyperparam_only` 条件化 true）——若软闸实测仍滑回超参，作为小 follow-up（参数槽位 `ledger_reducer.py` 已存在）。
- 单层族（cnn/transformer）的 direction 目录：当前 catalog 空（无 meta.json），软闸对此二族退化为靠 latency_moves；如需 direction 级覆盖，后续可补 `moves.json` 清单。
