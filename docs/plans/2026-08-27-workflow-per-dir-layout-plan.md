# 实施计划 - Workflows per-workflow 目录隔离改造

PLAN_STATUS: READY

> SPEC（唯一契约）：`C:\Users\mozzie\.claude\plans\crystalline-chasing-dewdrop.md`（用户 2026-08-26 拍板，8 项决策 + spec-reviewer 终审修订已并入）。
> 本计划把 SPEC 的 9 步转化为 coder-agent 可直接执行的 dispatch 批次（A..I，每批一个 commit）。计划中所有文件路径与行号均经实测核实（2026-08-27）；与 SPEC 原文行号有出入处已在「实测裁决」中显式标注，**均属 SPEC 自身授权的"以实测为准"条款，非契约变更**。

## 1. 目标与范围

**目标**：workflows/ 从平铺布局（根 `*.yaml` + 全局共享 `agents/` + `subagents/<wf>/` + 根 `knowledge_base/`）迁移为 per-workflow 自包含目录（`<wf>/workflow.yaml + agents/ + subagents/ + knowledge_base/ + scripts/`），加载层双形态兼容（新旧布局同时可解析），kd-nas 净删除，create-workflow skill 产出同步 per-wf，web workflows 页面展示每个 workflow 的全部资产。

**非目标（out of scope）**：
- 不改 agent prompts 正文（md 铁律，两处既定例外除外，见批 D）
- 不迁移 AgentHarness / Conductor 代码；不动 `docs/plans/`、`docs/specs/` 历史文档；不删用户数据目录（`kd-nas-artifacts.prior20260731-*` / `kd-nas-demo-artifacts/` / `examples/kd-nas-demo/`）
- 不 push（用户回来自行 review）
- 不重构 web 页面既有 placeholder 文案（`test_playwright_9b.py:180` 选择器耦合）

## 2. 契约影响面

| SPEC 契约项（逐字摘要） | 计划落点 |
|---|---|
| 决策 1：仓库源也改 per-wf，install 退化为纯目录拷贝 | 批 D（迁移）+ 批 E（install 重构） |
| 决策 2：共享资产复制副本完全自包含，不保留共享区 | 批 D 迁移脚本 SHARED 清单（硬编码 SPEC 所列） |
| 决策 3：先提交 v2 改动，全部工作留在 puzzle-supernet 不开新分支 | 批 A |
| 决策 4：create-workflow skill 同步 per-wf（含 17 benchmark expected） | 批 F |
| 决策 5：kd-nas 顺便删除 | 批 B |
| 决策 6：旧布局删干净后再全链测试（防假绿） | 批 H（安装态整删 + 非 repo cwd 探针） |
| 决策 7：无人值守 fail loud | 各批验收 + 全批共通纪律（§4.0） |
| 目标布局 14 个 wf 目录（SPEC §目标布局树） | 批 D 脚本 PLAN 表 |
| 共享副本清单（4 共享 agent ×2、_quant_scripts 2 文件 ×4、_struct/_puzzle/_po_scripts 独占、KB 独占） | 批 D 脚本硬编码 |
| kd 删除清单（yaml + 9 agents + _kd_scripts + subagents/kd-nas + 2 e2e 脚本 + 11 个整删测试 + 文档引用 + contract.py 条目） | 批 B |
| 步骤 2 加载层四点（catalog 双形态 / subagents 公式 / KB per-wf 来源 R4' / 单测四组） | 批 C |
| 步骤 3 迁移（parser 提取引用、pz_expand 显式补充、md 零改动铁律 + 两例外、psu parents[4]→[5]、测试路径闭清单、dry-run、验证=全量首跑） | 批 D |
| 步骤 4 install（三函数合一 per-wf sync、UD-1 backup 清理、benchmark runner 探测） | 批 E |
| 步骤 5 skill（SKILL.md + 4 reference + benchmark README + 17 expected + case 14 例外 + 静态断言双形态） | 批 F |
| 步骤 6 web（detail 加 subagents、tree 端点、前端 Subagents 区 + 资产树、先 Plan agent 设计、placeholder 不动） | 批 G |
| 步骤 7 review + 全链验证（安装态整删存档、三重探针、validate、残留 grep R2' 域、web 冒烟、REPORT） | 批 H |
| 步骤 8 收尾（release note、CHANGELOG、CURRENT、删迁移脚本、不 push） | 批 I |
| 成功标准 1-6（含 4b sha256 副本比对） | 批 D/H 验收 + §5 测试策略映射 |
| spec-reviewer 终审两清单：psu parents 五文件精确清单；`test_monitor_until_done.py` 实际路径 `tests/workflows/` | 批 D §4.4 固化（实测复核一致） |

## 3. 文件级改动清单

### 批 A（commit A：准备）
| 文件/对象 | 动作 | 改什么 | 为什么 |
|---|---|---|---|
| 工作区全部未提交改动 | commit | create-workflow v2（M 的 SKILL.md / install_cmds.py / agent-prompt-cleanliness-contract.md / 2 个测试 / e2e_phase13 artifacts + ?? 的 v2 spec / benchmark case 17 / reference/charts / scripts / test_skill_v1_checks.py 等） | SPEC 决策 3：先固化 v2 基线 |
| `.e2e_po/`、`.e2e_spe2e/` | 不提交 | E2E scratch，保持 untracked | 编排指令明确排除 |
| `docs/plans/2026-08-26-web-workflow-assets-design.md` | 不在批 A 范围 | 批 G 的 web Plan agent 产出 | 编排指令明确排除 |
| `D:\Projects\Orca\design-charts` skill 删除（git status 中 D 的文件） | 随批 A 提交 | 属 v2（design-charts 并入 create-workflow）遗留 | git status 已有 staged/unstaged 删除 |

### 批 B（commit B：kd-nas 净删除）
| 文件 | 动作 | 为什么 |
|---|---|---|
| `workflows/kd-nas.yaml` | git rm | SPEC 删除清单 |
| `workflows/agents/{model-flatten,kd-setup,teacher-gen,kd-train-script,train-script-verify,train-teacher,gen-student,distill,decide}/`（9 目录） | git rm | 同上 |
| `workflows/agents/_kd_scripts/` | git rm | 同上 |
| `workflows/subagents/kd-nas/project-fidelity-verifier-kd.md` | git rm | 同上 |
| `scripts/e2e_kd_nas_launch.sh`、`scripts/e2e_kd_nas_script_level.sh` | git rm | 同上 |
| `tests/workflows/` 下 11 个整删文件：`test_model_flatten.py` `test_teacher_gen.py` `test_kd_engine_trainer.py` `test_struct_kd_p7.py` `test_kd_redesign.py` `test_kd_train_script.py` `test_kd_reducer.py` `test_finalize_kd.py` `test_kd_prompt_no_source_narrative.py` `test_viz_kd_stage_metrics_tail.py` `test_receiver_variants.py` | git rm（删前逐个 grep fixture 归属） | SPEC 步骤 1 |
| `tests/exec/claude/test_accumulator.py`（:167 附近 `test_p5_tape_replay_kd_nas_flatten_extracts_final_json`） | modify：只删该用例 | SPEC：删前确认另有非 kd extractor 回归覆盖（已确认 `tests/exec/claude/` 存在 accumulator 系测试；coder 删前 grep `final_json\|extract` 复核，无覆盖则保留用例换非 kd fixture 并记录） |
| `tests/iface/in_session/test_resolve_artifacts_dir.py`（:172/:177 用例内 kd 引用） | modify：只删涉及 kd 的用例 | SPEC |
| `tests/e2e_redesign/contract.py`（:58 `"kd-nas": "kd-nas.yaml"`、:73 seed 注释、:155/:162 kd 分支） | modify：去掉 kd-nas 条目与分支 | SPEC |
| `orca/skills/create-workflow/reference/writing-style.md:74` | modify：`_kd_scripts/CONTRACTS.md` 范例 → `_po_scripts/PROFILER_CONTRACT.md`（实测存在）；**同行泛化示例 `workflows/agents/<wf>/CONTRACTS.md` 一并 per-wf 化**（如 `workflows/<wf>/agents/<wf>/CONTRACTS.md`——规范文档里的活示例教 skill 产出往哪放，非考古豁免类；不修则批 H 残留 grep 必命中且无人认领，plan-adversary Q15） | SPEC 文档引用清理 |
| `orca/exec/env.py:17,73-74,110`、`orca/exec/claude/executor.py:100-101`、`orca/exec/factory.py:88`、`orca/run/orchestrator.py:234`、`orca/exec/runner.py`、`orca/exec/script.py`、`orca/iface/cli/app.py`、`orca/iface/web/run_manager.py`、`orca/run/__init__.py` 内 kd-nas/_kd_scripts 注释/docstring 死例 | modify：换存活例（以 `grep -rn "kd" orca/ tests/ --include='*.py'` 实测为准，纯注释低风险） | SPEC 评审 E2 补全清单（10+2 处）+ `tests/exec/test_env.py:226-231`、`tests/exec/claude/test_executor_env_inject.py:117-125` docstring 死例 |

### 批 C（commit C：加载层双形态）
| 文件 | 动作 | 改什么 |
|---|---|---|
| `orca/compile/catalog.py`（:58 与 :110 两处 `sorted(d.glob("*.yaml"))`） | modify | 双形态收集：`sorted(d.glob("*.yaml")) + sorted(d.glob("*/workflow.yaml"))`，平铺优先。**DRY：两处抽 `_scan_yamls(d)` helper**（或入 layout 模块，见下行） |
| `orca/compile/layout.py` | new（可选，见批 C §4.2 决策 L1） | 双形态布局单一真相源：`scan_workflow_yamls(d)` + `resolve_subagents_dir(workflows_root, wf_name)`，供 catalog/validator/orchestrator 三处复用 |
| `orca/run/orchestrator.py:66-81`（`_compute_subagents_root`） | modify | SPEC 伪代码双形态：先 `workflow_dir/"subagents"` 直接含 `*.md` → 新形态；否则 `workflow_dir/"subagents"/wf_name` → 旧形态；再否则 `""`。**误命中坑**：旧形态下 `root/subagents` 目录存在但 md 在二级，不能只查 is_dir |
| `orca/compile/validator.py:1234`（`_check_subagents_md` 内 `subagents_root = workflows_root / "subagents" / wf.name` 定位语句；coder 用 `grep -n 'subagents_root = workflows_root' orca/compile/validator.py` 锚定防漂移） | modify | 同款双形态定位（SPEC 写 :122-131 为函数 docstring 段；错误分支文案约 :1244 一并同步双形态提示） |
| `orca/exec/render.py:81-89` | modify | 错误文案改为提示「subagents/ 应与 workflow.yaml 同目录（per-wf）或 workflows/subagents/<wf>/（旧平铺）」 |
| `orca/iface/cli/config.py:198-233`（`resolve_kb_dir`） | modify | 签名 `resolve_kb_dir(wf=None)`；per-wf 探测 `Path(wf.workflows_root)/"knowledge_base"`（含 `index.json`）插在 config 之后、`~/.orca/knowledge_base` 之前（序：env > config > per-wf > ~/.orca > cwd）。不得在 resolve 前写 os.environ |
| `orca/iface/cli/config.py:236-272`（`apply_kb_requirement`） | modify | `kb = resolve_kb_dir(wf)`；隐式命中注入 `os.environ["ORCA_KB_DIR"]` 时记录到模块级 `_INJECTED_KB_ENV: set[str]`；`resolve_kb_dir` 读 env 时**值 ∈ 该 set → 忽略该 env 项，从 config 起完整重走解析链**（口径钉死，SPEC 原文「按隐式来源参与排序」的两种实现取此释义——否则注入串可能以最高优先隐式项遮蔽下一 wf 的 per-wf KB；已知窄竞态：用户真显式值恰与注入串相同会被误判，接受并注释）。R4' 测试③ fixture 随之钉死：第一个 wf 无 per-wf KB（命中 ~/.orca 来源触发注入）、第二个 wf 有 per-wf KB，断言第二个解析到自己的 per-wf KB 而非注入残留 |
| `orca/iface/in_session/cli.py:1410` | modify | bootstrap 函数内（:1107 起），同函数 :1190 已有 `wf_obj` 在作用域 → 直接 `resolve_kb_dir(wf_obj)`（实测：零调用链改造） |
| `orca/iface/in_session/cli.py:1815` | modify | next 推进路径；作用域有 `tape` → 复用 `_load_wf_for_run(run_id, tape)`（:1659）取 wf 后透传；确认该大函数是否已加载 wf，避免重复加载；确无 wf 的路径退化为 `resolve_kb_dir()` 现行为并 log/注释明示（记录型退化非吞错） |
| `tests/compile/test_catalog.py` | modify | 加 per-wf 形态 + 双形态混存用例 |
| `tests/run/test_subagents_root.py` | modify | 新旧双形态用例 + 误命中回归（旧形态 `root/subagents/` 存在但无直接 md 时必须返回 `root/subagents/<wf>`） |
| `tests/iface/cli/test_config_kb.py` | modify | R4' 三条：① per-wf 命中优先于 ~/.orca/knowledge_base ② 显式 env 不被 per-wf 覆盖 ③ 同进程串行两个不同 wf 各自解析各自 KB |
| `tests/exec/test_render.py` | modify | subagents_root 空报错文案断言更新 |

注意（批 C 不动项）：`tests/compile/test_validator.py:21` `_REAL_WORKFLOWS = ... glob("*.yaml")` **暂不动**（源还是平铺，批 D 才改）；`orca/compile/agents.py` `LocalPoolResolver._search_bases` 第一项即 `context.workflow_dir / "agents"`——per-wf 布局天然兼容，**零改动**（实测确认，计划显式声明防 coder 去"修"）；web `routes/workflows.py` 的 `_resolve_context_for` 用 yaml parent，同样自动兼容。

### 批 D（commit D：workflows/ 大迁移）
| 文件/对象 | 动作 | 改什么 |
|---|---|---|
| `scripts/_migrate_per_dir.py` | new（用后批 I 删） | 一次性迁移脚本，设计见 §4.4 |
| `workflows/` 全树 | git mv / cp+add | 15 yaml → 14 目录（kd-nas 已于批 B 删）；~69 agent 目录分流；subagents **6** 目录拍平（批 B 已删 kd-nas，原 7 减 1）；`knowledge_base/` 整树入 agent-struct-exploration；`scripts/kb_graph.py` 入 `workflows/agent-struct-exploration/scripts/` |
| `workflows/agent-struct-exploration/workflow.yaml`（原 :59 input default） | modify | `"workflows/agents/_struct_scripts/latency_onnxrt.py::measure"` → `"workflows/agent-struct-exploration/agents/_struct_scripts/latency_onnxrt.py::measure"`（yaml 非 prompt，既定例外①） |
| psu 5 脚本（实测精确清单，spec-reviewer 终审版，**本计划已 grep 复核一致**） | modify | `parents[4]` → `parents[5]` + 同步行内注释：`workflows/agents/psu_retrain/scripts/progress_watcher.py:61`（含 :58 注释）、`workflows/agents/psu_run_train/scripts/progress_watcher.py:61`（含 :58 注释）、`workflows/agents/psu_retrain/scripts/_common.py:47`、`workflows/agents/psu_run_search/scripts/_common.py:47`、`workflows/agents/psu_expand_supernet/scripts/search_space_table.py:38`。改完全量 `grep -rn "parents\[" workflows/` 复核无漏。**实测裁决**：SPEC 原文所列 `psu_search_pipeline/scripts/_common.py`、`psu_search_pipeline/scripts/search_space_table.py` 实测无 `parents[` 命中，正确对象是 `psu_run_search` 与 `psu_expand_supernet`——以实测为准（SPEC 自授权"以 grep 实测为准"） |
| `workflows/agents/elastic_optimizer/references/supernet_template.py:15` | modify | docstring 用法示例路径更新（.py 不属 md 铁律） |
| `workflows/agents/ptq-sweeper/scripts/run_ptq_sweep.py:38-39` | modify | 路径锚定注释更新 |
| `workflows/agent-struct-exploration/scripts/kb_graph.py`（原 `scripts/kb_graph.py:23-29` docstring 用法块、:357 `--kb-dir` default） | modify | 默认 KB 根改相对脚本自身锚定 `Path(__file__).resolve().parent.parent / "knowledge_base"`（不依赖 cwd）；docstring 内 `python scripts/kb_graph.py ...` 用法示例路径同步更新（.py 注释类允许清单） |
| `tests/compile/test_validator.py:21` | modify | glob 改 `*/workflow.yaml` |
| 8 个写死平铺路径的测试（实测锚点）：`tests/test_po_scripts.py:50-52`、`tests/workflows/test_push_describe.py:20`、`tests/workflows/test_monitor_until_done.py:17-19,:170-181`（**实测裁决：实际路径是 `tests/workflows/`，SPEC 原文 `tests/iface/in_session/` 不存在该文件**，spec-reviewer 终审版正确）、`tests/workflows/test_nas_supernet_enum_gate.py:20-24`、`tests/test_puzzle_evaluator_recall.py:37-38`、`tests/test_puzzle_measure_baseline.py:29,:830`、`tests/e2e_puzzle/evaluator_driver.py:32-33`、`tests/e2e_phase13/{_workflows.py,conftest.py:21}` | modify | 路径改 per-wf 形态（e2e_phase13 的 `WORKFLOWS_DIR = Path(__file__).parent/"workflows"` 是测试自身 fixture 目录则合法命中不改，逐个判读） |
| md 铁律校验 | 校验 | **`git diff --cached -M --stat`**（批 D 产物全在 index，worktree 口径产出空 diff 假绿）确认 `agents/**/agent.md`、`agents/**/SKILL.md`、`subagents/**/*.md` 只有位置变化（rename/copy）无内容 diff；共享副本逐文件 sha256 比对——**基准点 = 首份 git mv 目标**（原池路径此刻已不存在），即「首个 wf 目录内副本 vs 其余 wf 目录内副本」逐一比对（cp 产生的新增文件不进 diff，须单独比对） |
| `tests/e2e_redesign/contract.py` | **显式冻结（deferred）** | 迁移后 `AGENTS_DIR`/`WORKFLOWS` 字典/`_struct_scripts` rglob 路径断链，但 e2e_redesign 属真机 E2E 契约，修复需真机验证（超无人值守单测范围）→ **不修**，记入 LAYOUT_MIGRATION_REPORT.md 待用户决策区（批 B 删 kd 条目照 SPEC 执行，不使其更坏） |
| 残留引用 grep | 校验 | md 内不带 `$ORCA_WORKFLOWS_ROOT` 前缀的 `workflows/agents` 硬编码相对引用——命中（除既定例外 `subagents/puzzle/workflow-verifier.md:30-38` 区间的 checklist 扫描指引，SPEC 标 :34）不擅自改，记录 LAYOUT_MIGRATION_REPORT.md 待用户决策（plan-adversary 实测：删 kd 后非 kd 的 md 内 `workflows/agents` 硬编码零残留，预期无新增命中） |

### 批 E（commit E：install 重构）
| 文件 | 动作 | 改什么 |
|---|---|---|
| `orca/iface/cli/install_cmds.py:536-582`（`_install_bundled_workflows`）、`:585-610`（`_install_bundled_knowledge_base`）、`:613-648`（`_install_bundled_subagents`） | modify：三函数合并为一个 per-wf sync | 源 `CWD/workflows/` 下每个含 `workflow.yaml` 的子目录 → `shutil.copytree` 整树到 `~/.orca/workflows/<dir>/`（`dirs_exist_ok=True`，ignore `__pycache__`/`*.pyc`）。旧三函数删除 |
| 同文件：旧布局幂等清理（UD-1 = backup 方案） | new 逻辑 | 对 `~/.orca/workflows/agents/`、`~/.orca/workflows/subagents/`、`~/.orca/knowledge_base/`：删除前比对随包内容（随包旧池名集合 = 源 workflows 各 wf 目录 agents/ 子树之并集），**比对升级到内容级**（名字在随包集合内的条目做目录树逐文件 sha256 对比——防用户**自改**过的随包文件被名字级判定误删，plan-adversary Q3）；发现非随包文件或内容不一致 → 整目录移入 `~/.orca/_legacy_layout_backup_<date>/` + 打印 warn 清单，不直接删；**与随包 wf 同名的旧平铺 yaml**（如 `~/.orca/workflows/nas-supernet.yaml`，升级安装残骸——无论能否加载，防其按批 C「平铺优先 + first-wins」静默 shadow 同名 per-wf 新目录，plan-adversary Q2）与**未知的**平铺 yaml（如 po-probe.yaml 尸体）**一律入 backup**；三目录与随包完全一致（纯我们装的）→ 直接删。docstring 的旧 merge 承诺（:545-548/:594-597，实测锚点）同步改写 |
| `run_install`（:651-722）部署输出段 :684-714 | modify | CLI 输出按 per-wf 目录打印（`<wf>/` 一行含 agents/subagents/kb 摘要） |
| `tests/iface/cli/test_install_cmds.py` | modify | 部署形状断言全部重写（源 fixture 造成 per-wf 结构）+ 旧布局清理用例（backup 分支 / 直接删分支 / 未知平铺 yaml 入 backup） |
| `scripts/run_skill_benchmark.py:74-79` | modify | 产物探测列表加 `ws/"workflows"/*/agents`（实测锚点 :78-79 的 `for cand in [ws/"agents", ws/"workflows"/"agents"]`） |

### 批 F（commit F：create-workflow skill 同步）
| 文件 | 动作 | 改什么 |
|---|---|---|
| `orca/skills/create-workflow/SKILL.md`（:33-35 实测锚点 + :15/:38/:131/:153-154/:166-169/:251） | modify | 默认落盘 `./workflows/<name>/workflow.yaml`；agents 落 `<name>/agents/`、subagents 落 `<name>/subagents/`；design.md 落点同步；布局段落给 per-wf 目录树示意 |
| `reference/orca-workflow-contract.md`（:31/:78-79/:83/:160） | modify | 布局描述 per-wf 化 |
| `reference/solidify-validate.md`（:13/:91） | modify | 同上 |
| `reference/agent-prompt-cleanliness-contract.md`（:47/:150） | modify | 同上 |
| `reference/writing-style.md`（:37/:47/:51-52） | modify | 范例路径 per-wf 化（:74 kd 范例批 B 已换） |
| `benchmark/README.md:17-30` | modify | case 结构说明 per-wf |
| `benchmark/cases/` 17 个 case 的 `expected/` | modify | 有 workflow.yaml 的 case：expected 顶层 `workflow.yaml`+`agents/` → `expected/<wf-name>/workflow.yaml`+`expected/<wf-name>/agents/`（wf name 以 expected yaml 内 `name` 字段为准；无 agents 的 case 只挪 yaml）。**例外 case 14（agent-pool-only）**：保持平铺 `agents/` 不动，校验语义维持现状（只对盘上 agents 断言） |
| `tests/test_skill_benchmark.py`（:37/:57-58/:66 硬编码 `expected/workflow.yaml`） | modify | 静态断言改双形态兼容：有 workflow.yaml 的 case 找 `expected/<name>/workflow.yaml`，无的找 `expected/workflow.yaml`（case 14 维持旧路径） |
| `scripts/check_agent_md_static.py`（:26/:135/:153/:272）、`scripts/check_charts.py`（:260） | 验证为主 | 「yaml 同级 agents/」假设在 per-wf 下自然成立，跑一遍确认，不预期改动 |
| `examples/*.yaml`（4 个） | 不动 | 纯内联单文件示例无 agent 引用，保持 |

### 批 G（commit G：web 增强，先设计后实施）
| 文件 | 动作 | 改什么 |
|---|---|---|
| `docs/plans/2026-08-26-web-workflow-assets-design.md` | new（web Plan agent 产出） | 后端 API 形态 / 前端交互组件 / store 改动 / 测试清单；**必须遵守既定约束**（§4.7） |
| `orca/iface/web/routes/workflows.py`（detail :59-90；`_safe_resolve` :231-253；`_build_tree` :256-305） | modify | detail response 加 `subagents: [{name, description}]`（解析 `<wf-dir>/subagents/*.md` frontmatter，复用 agents 列表 fail-soft 模式；**兜底语义钉死（plan-adversary Q4）**：实测全部 subagent md frontmatter 只有 `subagent`/`version`/`sentinel` 三键、无 description——有 frontmatter 无 description 键或整块缺 frontmatter 时 `description = ""`（空串；不取 body 首行——正文语义劫持；name 恒为文件 stem 必出）；新增 `GET /api/workflows/{name}/tree`（root=`<wf-dir>`= yaml parent，复用 `_safe_resolve`+`_build_tree` 过滤）；文件读取复用现有 `/file` 端点逻辑或加 `/api/workflows/{name}/file?path=`（1MB 上限与二进制探测沿用） |
| `orca/iface/web/frontend/src/components/pages/WorkflowBrowsePage.tsx`（344 行，**实测路径：`src/components/pages/`，SPEC 原文 `src/components/workflow/` 不存在**） | modify | 左栏加 Subagents 区（同 Agents 列表样式）；文件树数据源扩展为 wf 级整树（复用 `frontend/src/components/conversation/FileTree.tsx` 现成组件，喂 `_build_tree` JSON）。**不动既有 placeholder 文案**（`tests/iface/web/test_playwright_9b.py:180` 选择器耦合 `input[placeholder='workflows/demo.yaml']`） |
| `orca/iface/web/frontend/src/stores/workflow-browse-store.ts`（264 行） | modify | 加载 subagents 列表 + wf 树 |
| `tests/iface/web/test_workflows_routes.py` | modify | 加 subagents / tree / 防 traversal 用例 |
| `orca/iface/web/frontend/test/workflows-page.test.tsx`、`frontend/test/workflow-browse-store.test.ts`（**实测路径**：测试目录是 `frontend/test/`） | modify | 前端测试相应更新 |

### 批 H（commit H：review + 全链验证，如有修复）
| 对象 | 动作 |
|---|---|
| 本轮全部 diff（A..G） | code-reviewer agent 审（依赖单向 / fail loud / DRY / 测试意图），发现即修（修复入 commit H） |
| WSL `~/.orca/workflows/` + `~/.orca/knowledge_base/` | 删前 `find` 列完整内容存档到报告 → 整删（仅本次验证；常态 install 走 backup） |
| `tars install` → 安装态断言 | `~/.orca/workflows/` 只有 14 个 per-wf 目录、无 agents/、无 subagents/、无平铺 yaml |
| 三重探针 | ① 非 repo cwd（/tmp）跑 `orca list` = 14 wf 来自安装态；② 逐个 `load_workflow(~/.orca/workflows/*/workflow.yaml)`（一探针三重覆盖 agent 解析 / subagents 校验 / yaml 可加载）；③ `tars validate` 14 wf 全过 |
| 残留 grep（R2' 域） | pattern：`workflows/agents`（顶层引用）、`workflows/subagents`（顶层引用）、`kd-nas`、`_kd_scripts`、根级 `knowledge_base/` 引用；域 = `orca/` + `tests/`（排除 `tests/e2e_*`）+ `workflows/`（排除 `*/knowledge_base/**`）+ `scripts/`；白名单：`workflows/puzzle/subagents/workflow-verifier.md` checklist 段 |
| web 冒烟 | `tars serve` 或 TestClient：curl `/api/workflows` 14 项、任一 wf detail 含 subagents、tree 返回含 workflow.yaml+agents+subagents+scripts |
| `LAYOUT_MIGRATION_REPORT.md`（仓库根） | new：每项 pass/fail + 证据摘录 + 待用户决策区（verifier checklist 断链预声明等） |

### 批 I（commit I：收尾）
| 文件 | 动作 |
|---|---|
| `docs/releases/2026-08-26-workflow-per-dir-layout.md` | new：布局前后对比、双形态兼容说明、kd-nas 删除清单、web 新端点、存量项目影响 |
| `docs/status/CHANGELOG.md` / `docs/status/CURRENT.md` | modify：索引 + 完成态 |
| `scripts/_migrate_per_dir.py` | git rm（历史留档） |

## 4. 实施步骤（有序，按 dispatch 批次）

### 4.0 全批共通纪律（每份 dispatch 单必附）

- 无人值守 fail loud：本计划未覆盖的问题 → 停后续步骤、已完成步骤保持已 commit、写 `LAYOUT_MIGRATION_REPORT.md` + 更新 `docs/status/CURRENT.md`、结束。
- commit message 前缀：批 A `chore(skill)`/如实描述、批 B `refactor(layout)`、批 C `feat(layout)`、批 D `refactor(layout)`、批 E `refactor(layout)`、批 F `docs(skill)`+`refactor`、批 G `feat(web)`、批 H `fix(layout)` 如有、批 I `docs(layout)`；全部含 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- WSL 命令形态：pytest/orca/tars 走 WSL `.venv`：`wsl bash -c "cd /mnt/d/Projects/Orca && .venv/bin/python -m pytest <paths> -q"`。**双层 shell 引用陷阱**：复杂命令（多引号/多行）先 Write 一个纯 LF 的临时 `.sh` 到仓库根（如 `_run_batch.sh`，跑完删），再 `wsl bash -c "cd /mnt/d/Projects/Orca && bash _run_batch.sh"`。pytest 按目录取舍时注意 `--noconftest`（memory 坑）。CLI 不可直调时用 `.venv/bin/python -m orca...` 或 `python -c "from orca.compile import catalog; ..."` 等价验证。
- 每批开工前读 `docs/status/CURRENT.md` 确认前批状态；完工后更新 CURRENT.md（不积累）。
- 不 push。

### 4.1 批 A（commit A）：准备

**做什么**：在 puzzle-supernet 上提交现有全部未提交改动 + 记录基线。
**提交甄别**（编排指令固化）：
- 提交：create-workflow v2 全部——git status 中 M 的 `CLAUDE.md`、`orca/iface/cli/install_cmds.py`、`orca/skills/create-workflow/SKILL.md`、`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`、`tests/iface/cli/test_install_cmds.py`、`tests/test_skill_benchmark.py`、`tests/e2e_phase13/_artifacts/*`；D 的 `orca/skills/design-charts/**`（并入 create-workflow 的删除）；?? 的 `docs/specs/create-workflow-skill-v2-{design-draft,spec}.md`、`orca/skills/create-workflow/benchmark/cases/17-chart-integration/`、`orca/skills/create-workflow/examples/charts/`、`orca/skills/create-workflow/reference/charts/`、`orca/skills/create-workflow/reference/solidify-validate.md`、`orca/skills/create-workflow/scripts/`、`tests/test_skill_v1_checks.py`。
- **一并提交（自指项，plan-adversary Q8）**：本计划文件 `docs/plans/2026-08-27-workflow-per-dir-layout-plan.md` 与 M 的 `docs/status/CURRENT.md`（docs/plans 与 docs/status 惯例入库）。
- 不提交：`.e2e_po/`、`.e2e_spe2e/`（E2E scratch，untracked 保持）。
- 不在批 A：`docs/plans/2026-08-26-web-workflow-assets-design.md`（批 G 产出）。
**基线**：WSL 内跑 `orca list`（或 `.venv/bin/python -c "from orca.compile import catalog; import json; print(json.dumps(catalog.list_workflows(), ensure_ascii=False, indent=1))"`）存 `/tmp/list_baseline.txt`；断言 15 个 wf 含 kd-nas。
**验收**：`git status` 干净（除 2 个 scratch 目录）；基线文件存在且 15 项。
**依赖**：无（首批）。**衔接**：批 B 起所有 diff 叠加在其上。
**回滚**：`git revert <A>`（理论上无需）。

### 4.2 批 B（commit B）：kd-nas 净删除

**做什么**：按 §3 批 B 清单 git rm + 修引用 + 删 kd 用例。
**要点**：
1. 11 个整删测试文件删前逐个 grep 确认其 fixture 只服务 kd 资产（如 fixture 引用非 kd 资产 → 保留该文件改判，记录报告）。
2. `test_accumulator.py` 删 `test_p5_tape_replay_kd_nas_flatten_extracts_final_json`（:167 附近）前，grep `tests/exec/claude/` 确认另有非 kd 的 extractor/result 抽取回归覆盖；无 → 保留该用例换非 kd fixture 并记录 LAYOUT_MIGRATION_REPORT.md。
3. kd 注释清理以 `grep -rn "kd" orca/ tests/ --include='*.py'` 实测为准（SPEC 清单是起点非闭集），纯注释/docstring 死例换存活例。
4. 文档引用：`writing-style.md:74` 范例替换。
**验收命令**（WSL venv）：
```
.venv/bin/python -m pytest tests/workflows/ tests/exec/claude/test_accumulator.py tests/iface/in_session/test_resolve_artifacts_dir.py -q
grep -rn "kd-nas\|_kd_scripts" orca/ tests/ --include='*.py' | grep -v e2e   # 预期仅剩 e2e 或已换存活例的注释
```
**依赖**：批 A。**衔接**：批 D 的「agents/ 下不应剩任何 kd 系之外的东西」依赖本批删光。
**回滚**：`git revert <B>`（单 commit 单元）。

### 4.3 批 C（commit C）：加载层双形态改造

**做什么**：§3 批 C 四组改动 + 单测。迁移前先改代码，保证仓库全程可跑（此时源仍平铺，旧形态路径全走 legacy 分支，行为零变化——这是回归保障）。
**实现决策 L1（计划层，DRY 铁律驱动）**：catalog 两处 glob + orchestrator subagents 公式 + validator subagents 定位 = 三处同款双形态逻辑。**新建 `orca/compile/layout.py`** 承载 `scan_workflow_yamls(d)`（平铺优先 + per-wf）与 `resolve_subagents_dir(workflows_root, wf_name) -> Path | ""`（SPEC 伪代码），catalog / validator import 之；orchestrator（run 层）import compile 合法（依赖单向 compile ← run）。若 coder 发现更贴合现状的内聚点（如并入 `orca/compile/agents.py`），允许落点微调，但**三处不得各自复制实现**。
**KB 改造注意**：
- `resolve_kb_dir` 的 per-wf 探测条件：`wf is not None and wf.workflows_root is not None and (Path(wf.workflows_root)/"knowledge_base"/"index.json").is_file()`（KB 判据含 index.json，防误把任意同名目录当 KB）。
- `_INJECTED_KB_ENV` 竞态声明按 SPEC 原文注释。
- web `run_manager.py:347` 与 `orca/run/__main__.py:84`、`commands.py` 4 处（:272/:731/:883/:1076，实测）的 `apply_kb_requirement(wf)` 已持有 wf，**零调用方改动**（函数内部透传即可；实测核实）。
- L1 补充验证要求（plan-adversary Q10）：批 H code-reviewer 必须核实 catalog/validator/orchestrator 三处**确实 import** 共享实现而非各留复制；「落点微调」口子限死在 `orca/compile/` 包内（layout.py 或既有模块），不得扩散到其他包。
**验收命令**：
```
.venv/bin/python -m pytest tests/compile/ tests/run/test_subagents_root.py tests/iface/cli/test_config_kb.py tests/exec/test_render.py -q
.venv/bin/python -m pytest tests/ -q --ignore=tests/e2e_puzzle --ignore=tests/e2e_redesign --ignore=tests/e2e_phase13   # 回归冒烟（可选，批 D 才全量首跑）
```
（e2e 排除目录以仓库实际 `tests/e2e_*` 为准；「全量非 e2e」的等价 pytest 调用由 coder 用 `--ignore` 逐个排除或 `python -m pytest tests/ -q --deselect` 按实测 e2e 目录列表组织。）
**依赖**：批 B（kd 测试已清，全量跑不被 kd 用例干扰）。**衔接**：批 D 迁移后新形态路径立即被本批代码接住——本批是迁移的安全网。
**回滚**：`git revert <C>`。

### 4.4 批 D（commit D）：workflows/ 大迁移（最大风险批）

**做什么**：写 `scripts/_migrate_per_dir.py` 并执行，一次成型 per-wf 布局。
**迁移脚本设计（dispatch 要求具体化）**：

```
scripts/_migrate_per_dir.py 结构：
  REPO = Path(__file__).resolve().parents[1]
  WFS = REPO / "workflows"

  # 1) 硬编码 PLAN 表（SPEC 目标布局逐字固化）
  WF_DIRS = {  # wf-name -> None（yaml 文件名即目录名，全部满足）
    "agent-struct-exploration", "nas-supernet", "nas-supernet-v2", "nas-supernet-v3",
    "puzzle", "puzzle-supernet", "prof-opt", "nas-agent-pipeline", "nas-hp-search",
    "quant-ptq-sweep", "quant-qat", "quant-sensitivity", "quant-bit-curve", "prune-channel-sweep",
  }
  SHARED_COPIES = {  # 共享 agent：首份 git mv 到 key[0]，其余 cp -r + git add
    ("nas-agent-pipeline", "nas-hp-search"): [
      "supernet-train-script", "nas-search-pipeline", "nas-train-runner", "nas-select"],
  }
  SELECTIVE_POOL = (  # 只取子集的池（SPEC L52「git mv 第一份，cp+git add 其余」逐字执行）：
      "_quant_scripts", ["_common.py", "_device.py"],
      ["quant-ptq-sweep", "quant-qat", "quant-sensitivity", "quant-bit-curve"],
  )  # 首个 quant wf：git mv 两个文件；其余 3 个：copy2 + git add；
    # 原池剩 __pycache__（untracked）→ 步骤 6 __pycache__ 清理 + 空壳 rmdir 兜住，
    # 自检 b「workflows/ 根下零 agents/」才能过（plan-adversary Q1：全 copy 会留原池炸自检）
  EXCLUSIVE_POOLS = {  # 独占池：git mv 整目录
    "_struct_scripts": "agent-struct-exploration",
    "_puzzle_scripts": "puzzle",
    "_po_scripts": "prof-opt",
  }
  EXTRA_AGENT_MOVES = {"pz_expand": "puzzle"}   # 显式补充（SPEC：唯一 checklist 载体）
  PSU_PARENTS_FIX = [  # 五文件精确清单（实测/spec-reviewer 终审版）
    "psu_retrain/scripts/progress_watcher.py", "psu_run_train/scripts/progress_watcher.py",
    "psu_retrain/scripts/_common.py", "psu_run_search/scripts/_common.py",
    "psu_expand_supernet/scripts/search_space_table.py",
  ]

  # 2) agent 引用提取：orca.compile.parser.load_workflow(<yaml>) 逐 wf 解析，
  #    _iter_agent_nodes(wf)（orca/compile/parser.py 现成）收集 node.agent 引用
  #    交叉核对：yaml 文本 grep 'agent:' 行粗集 ⊇ parser 集（diff 非空 → 打印差异并 sys.exit(1)）
  # 3) dry-run（默认）：打印全部计划动作（MV/CPO/MKDIR/RM 每行一条）到 stdout，不执行
  # 4) --execute 才真跑：
  #    - git mv workflows/<wf>.yaml workflows/<wf>/workflow.yaml
  #    - 每 agent：首次 git mv workflows/agents/<a> workflows/<wf>/agents/<a>；
  #      共享第二份起 shutil.copytree + subprocess git add
  #    - _quant_scripts：首 wf（quant-ptq-sweep）git mv 两文件入其 agents/_quant_scripts/；
  #      其余 3 wf mkdir + copy2 + git add（见 SELECTIVE_POOL 注）
  #    - 独占池 git mv；pz_expand git mv
  #    - subagents：git mv workflows/subagents/<wf>/* workflows/<wf>/subagents/（拍平）
  #    - git mv knowledge_base workflows/agent-struct-exploration/knowledge_base
  #    - git mv scripts/kb_graph.py workflows/agent-struct-exploration/scripts/kb_graph.py
  # 5) 内容修正（yaml default :59、psu 5 文件 parents[4]->[5]、elastic_optimizer
  #    supernet_template.py:15 docstring、ptq-sweeper run_ptq_sweep.py:38-39 注释、
  #    kb_graph.py 默认 KB 根锚定）——正则/行级替换，替换计数与预期不符 → exit(1)
  # 6) 骨架清理（顺序钉死，plan-adversary Q16a：先 pycache 后空壳，自底向上）：
  #    a. find workflows -name __pycache__ -exec rm -rf（确认 git 不跟踪 .pyc）
  #    b. 自底向上 rmdir 空壳：先 workflows/agents/_quant_scripts（若仅剩空壳），
  #       后 workflows/agents、workflows/subagents；非空（有 tracked 残留）→ 打印清单 exit(1)
  # 7) 自检（exit code 说话）：
  #    a. git status --porcelain：无未预期 Untracked（脚本自身产物全部已 add）
  #    b. workflows/ 根下零 yaml、零 agents/、零 subagents/
  #    c. 14 个 wf 目录各含 workflow.yaml
  #    d. 共享副本 sha256 逐一比对，基准点 = 首份 git mv 目标（成功标准 4b；
  #       原池路径此刻已 mv 走，比对「首个 wf 目录内副本 vs 其余 wf 副本」）
  #    e. git diff --cached -M --stat 中 *.md 仅 rename/copy，无内容 diff 行
  # 8) 幂等：检测目标已存在 → 该动作 skip 并 warn（断点重跑安全）；
  #    任何 git mv/cp 失败 → 立即中止（fail loud），已做动作留给 git 状态可见
```

**流程**：coder 先 `--dry-run` 把清单贴进实施报告核对 → `--execute` → 自检过 → 手工补测试路径修正（§3 批 D 测试清单，脚本外改）→ 全量首跑。
**agent prompts 零改动铁律**：脚本不碰任何 `agent.md`/`SKILL.md`/`subagents/**/*.md` 内容；例外仅 §3 所列（yaml default、workflow-verifier.md 断链**不修**、白名单记录）。
**验收命令**（commit D 完成条件 = 全量首跑点）：
```
# 14 wf 与基线 diff（除 kd-nas 消失外 name/description/entry/inputs_count 一致）
.venv/bin/python -c "from orca.compile import catalog; ..."（对比 /tmp/list_baseline.txt）
# 全量非 e2e 单测（首跑点，不留到批 H）
.venv/bin/python -m pytest tests/ -q --ignore=<各 e2e 目录>
# md 零内容改动 + 副本一致（脚本自检 d/e + 人工复核 git log --stat）
grep -rn "parents\[" workflows/    # 预期全部 parents[5]
```
**依赖**：批 C（新形态解析已就位）。**衔接**：批 E 的 install 源假设 per-wf；批 F 的 skill 布局叙事基于本批结果。
**回滚**：迁移脚本失败中止时（git mv 已 staged，`git checkout` 撤不回 index）`git reset --hard HEAD && git clean -fd workflows/ knowledge_base/ scripts/`（未 commit 前恢复到批 C 提交态）；已 commit 后 `git revert <D>`（目录 rename 由 git 自动跟踪）。**脚本幂等 + git 干净起点 = 双保险**。

### 4.5 批 E（commit E）：install 重构

**做什么**：§3 批 E。三函数合并为 per-wf sync + 旧布局 backup 清理 + 测试重写。
**要点**：
1. 新 sync 函数保持「无 CWD/workflows 或无 `*/workflow.yaml` → no-op」语义（非仓库根跑 install 不报错）。
2. backup 判定的「随包旧池名集合」= 源 workflows 各 wf 目录下 `agents/` 子树的一级子目录名并集（hardcode 禁止，运行时计算，防漂移）；名字命中后再做**目录树逐文件 sha256 内容比对**，任一文件不一致 → 该目录整树入 backup（Q3）。**比对规则三句钉死（plan-adversary Q13）**：(a) 比对与 copytree 同款 ignore `__pycache__`/`*.pyc`（防真机安装态 pycache 使「完全一致→直接删」分支永不触发）；(b) 共享 agent 在源中有两份副本（nas-agent-pipeline / nas-hp-search 各一）→ installed 副本与**任一**随包源副本一致即算一致（any-match，防单点比对假阳性）；(c) subagents/KB 映射公式：`~/.orca/workflows/subagents/<wf>` ↔ `workflows/<wf>/subagents/`、`~/.orca/knowledge_base` ↔ `workflows/agent-struct-exploration/knowledge_base`。清理分支（顺序）：① 目录/文件非随包名 → backup ② 名字随包但内容不一致 → backup ③ 与随包 wf 同名或未知的平铺 yaml → backup ④ 完全一致 → 直接删。test_install_cmds 覆盖全部四分支。
3. `run_install` 输出段改 per-wf 打印；docstring 旧布局描述全部改写（:537-548/:586-597/:614-635 三段）。
4. test_install_cmds 的旧断言（部署 yaml/agents/subagents/KB 四段）全部按 per-wf 目录形状重写；fixture 源从平铺改为 per-wf 结构；另加一个「升级安装」用例：预置旧平铺 `~/.orca/workflows/<wf>.yaml` + agents/ 混合态 → install 后断言平铺 yaml 与旧 agents/ 均入 backup、per-wf 目录无 shadow（Q2 回归锁）。
**验收命令**：
```
.venv/bin/python -m pytest tests/iface/cli/test_install_cmds.py -q
```
**依赖**：批 D（源已 per-wf）。**衔接**：批 H 的 `tars install` 全链验证依赖本批。
**回滚**：`git revert <E>`。

### 4.6 批 F（commit F）：create-workflow skill 同步

**做什么**：§3 批 F（SKILL.md + 4 reference + README + 17 expected + 测试双形态）。
**要点**：
1. expected 重排用脚本/手工 git mv 皆可（`expected/<name>/` 一致命名，name 取 expected yaml 内 `name` 字段，不是 case slug）。
2. case 14 例外钉死：不管 pool-only 产出的 per-wf 落点歧义，校验语义维持现状。
3. `check_agent_md_static.py` / `check_charts.py` 跑一遍确认零改动假设成立（不成立 → 修并记录）。
**验收命令**：
```
.venv/bin/python -m pytest tests/test_skill_benchmark.py tests/test_skill_v1_checks.py -q
.venv/bin/python orca/skills/create-workflow/scripts/check_agent_md_static.py（或其实际调用形态，跑通零报错）
```
（skill 静态检查脚本的实际入口以 `orca/skills/create-workflow/scripts/` 内 README/usage 为准。）
**依赖**：批 D（布局事实）；与批 E 无顺序依赖但排在后（编排可 E+F 同批 dispatch）。**衔接**：批 H 全量跑含这些测试。
**回滚**：`git revert <F>`。

### 4.7 批 G（commit G）：web 增强（先设计后实施）

**做什么**：先派 web Plan agent（只读设计）产出 `docs/plans/2026-08-26-web-workflow-assets-design.md`，按设计实现。
**既定约束（设计不得偏离，逐字来自 SPEC + 用户指令；设计偏离 → fail loud 停机）**：
- 目标：workflows 页面可见每个 wf 全部资产——workflow.yaml、agents、sub-agents（用户核心诉求）、knowledge_base、脚本资产（`scripts/`、`agents/<agent>/scripts/`、`agents/_xxx_scripts`）。
- 后端：detail 加 `subagents: [{name, description}]`（frontmatter 解析 + fail-soft；description 兜底语义见 §3 批 G——空串，name 恒出）；新增 `GET /api/workflows/{name}/tree`（root = yaml parent；复用 `_safe_resolve` + `_build_tree`；过滤 hidden/`__pycache__`/`.pyc`）；文件读取复用 `/file` 逻辑（1MB 上限 + 二进制探测）。
- 设计文档文件名**按 SPEC 钉死**为 `docs/plans/2026-08-26-web-workflow-assets-design.md`（SPEC 指定名，不按实际执行日期改名，防计划/SPEC 引用断链）。
- 前端：`WorkflowBrowsePage.tsx` 左栏 Subagents 区；文件树复用 `FileTree.tsx`；store 加载 subagents + wf 树。
- 禁改既有 placeholder 文案（`test_playwright_9b.py:180` 耦合）。
- 路径实测修正：前端在 `orca/iface/web/frontend/src/` 与 `frontend/test/`（SPEC 原文路径少了 `frontend/` 段）。
**验收命令**：
```
.venv/bin/python -m pytest tests/iface/web/test_workflows_routes.py -q
wsl bash -c "cd /mnt/d/Projects/Orca/orca/iface/web/frontend && npx vitest run test/workflows-page.test.tsx test/workflow-browse-store.test.ts"
# placeholder 耦合不碎（若环境可跑）：tests/iface/web/test_playwright_9b.py 按 WSL Playwright 备忘执行；不可跑则静态 grep 断言 placeholder 字串未被 diff 触及
```
**依赖**：批 D（per-wf 布局下 tree 才有 wf 级整树）。**衔接**：批 H web 冒烟 curl 断言。
**回滚**：`git revert <G>`（设计文档独立无害）。

### 4.8 批 H（commit H）：自我 review + 全链验证

**做什么**：§3 批 H。code-reviewer 审 A..G 全部 diff（重点：依赖单向 schema/run/exec/events/iface、fail loud、DRY、测试意图）→ 修复 → 安装态整删验证 → 三重探针 → 残留 grep → web 冒烟 → REPORT。
**顺序敏感点**：
1. 删 `~/.orca/workflows/` 前：① 先 `find` 完整清单存档进 REPORT（计划内动作，删前存档）；② **在途进程断言（plan-adversary Q6/Q14）**：`pgrep -af "tars serve"` 与 `pgrep -af "bin/orca"`（**精确匹配集**——裸 `pgrep -af orca` 会误伤 cmdline 含 "orca" 字样的自证命令如 `python -c "from orca.compile ..."`，致全链验证假阳性跳过）；命中 → 先甄别 PID 归属（排除本验证进程自身树、把 cmdline 归档进 REPORT），确认非自身依赖共享安装态的真在途进程才 fail loud 停机，不盲删也不误停（~/.orca 是全机唯一安装点，memory 有多驱动者同场竞速先例）。
2. 探针①必须在非 repo cwd（如 `/tmp`）——防 catalog first-wins 命中源态的假绿。
3. 残留 grep（R2' 域与 pattern 逐字同 SPEC）命中清单逐条人工判定。**判定语义（Q7 钉死）**：命中 ≠ 残留，标准是「指向仓库平铺布局的活引用」；预置白名单（除 SPEC 的 `workflow-verifier.md` 外）：`orca/exec/render.py` 与 `orca/compile/validator.py` 错误提示文案中的旧布局说明（双形态兼容代码合法提及）、`orca/iface/cli/install_cmds.py` backup 清理逻辑中的旧目录名（清理目标必须点名）——批 C/E 改动自身引入的合法「旧布局提示文本」。白名单外命中 → 判定并记录（能机械归档的归档，拿不准的进待用户决策区，不擅自改）。
4. REPORT「待用户决策区」必含：workflow-verifier checklist 断链预声明（UD-2 后果）、`tests/e2e_redesign/contract.py` 冻结记录（批 D 表格）。
**验收**：成功标准 1-6 全项 pass 且写入 REPORT（REPORT 是本批交付物，不是附属品）。
**依赖**：批 E/G 全部就位。**衔接**：批 I 收尾。
**回滚**：修复类 `git revert <H>`（单 commit 含全部 fix）。

### 4.9 批 I（commit I）：收尾

**做什么**：release note + CHANGELOG 索引 + CURRENT 完成态 + 删 `scripts/_migrate_per_dir.py`。不 push。
**验收**：`docs/status/CURRENT.md` ≤50 行完成态；CHANGELOG 有索引行；git log 9 个 commit 可读。
**回滚**：`git revert <I>`（文档级）。

## 5. 测试策略

| 验收标准（SPEC 成功标准） | 验证手段 | 批次 |
|---|---|---|
| 1. workflows/ 只剩 14 per-wf 目录 + R2' grep 零残留 | 迁移脚本自检 b/c + 批 H 残留 grep（域与白名单逐字） | D、H |
| 2. install 同构 + orca list 14 一致 + 探针 + validate | 批 H：安装态整删 → tars install → 非 repo cwd list + 逐 yaml load_workflow + tars validate | E（单测）、H（真机） |
| 3. 全量单测绿（WSL venv 非 e2e） | 批 D 全量首跑（完成条件）+ 批 H 复跑 | D、H |
| 4. web detail 含 subagents + tree 含 scripts/agent 脚本（curl 硬验收） | `tests/iface/web/test_workflows_routes.py` 新用例 + 批 H TestClient/serve curl | G、H |
| 4b. md 零内容改动 + 副本 sha256 一致 | 批 D 脚本自检 d/e + `git diff --stat` 人工复核 | D |
| 5. skill 文档 + 17 expected per-wf | `tests/test_skill_benchmark.py`（双形态断言）+ `test_skill_v1_checks.py` | F |
| 6. REPORT 完整证据 | 批 H 交付物检查清单 | H |
| 批 C 回归（SPEC 步骤 2 单测四组） | test_catalog / test_subagents_root / test_config_kb / test_render 新旧双形态用例 | C |
| 批 B kd 清理 | tests/workflows/ 剩余 + accumulator/resolve_artifacts 删例后绿 + grep 零 kd | B |

项目测试惯例：pytest 布局 `tests/` 镜像源码包；WSL `.venv` 执行；前端 vitest（`orca/iface/web/frontend/test/`）；E2E 目录批内一律排除（批 H 之外的批不跑 e2e）。

## 6. 风险与回退

1. **批 D 迁移脚本漏迁/错迁 agent**（最大风险）：漏迁 → install 后 agent not found（批 D 自检 b/c + 批 H 探针②逐 yaml load 双保险兜住）；错迁（agent 进错 wf）→ parser 引用提取 + grep 交叉核对 + dry-run 人工核对清单三层防。回退：commit 前工作区恢复（checkout + clean），commit 后 `git revert <D>`。fail-loud 点：脚本自检任一 exit(1)。
2. **批 C KB env 泄漏误判 / subagents 误命中**：R4' 三条单测 + 误命中回归用例直接锁死；`_INJECTED_KB_ENV` 窄竞态已按 SPEC 接受并注释。回退 `git revert <C>`。fail-loud 点：显式坏路径仍返 ""（不静默回退）契约用例。
3. **批 E 旧布局 backup 误删用户内容**：backup 方案只在「与随包完全一致」时直接删，任何非随包文件整目录入 backup；单测覆盖三分支。回退 `git revert <E>`；~/.orca 侧从 `_legacy_layout_backup_<date>/` 恢复。fail-loud 点：backup 移动失败 → install 报错不继续。
4. **批 F/G 对 17 expected / 前端测试的大面积重写引入假绿**：双形态断言以 expected yaml `name` 字段为锚（非 slug）；case 14 例外钉死防过度迁移；web 硬验收用 curl 不依赖前端实现。回退各自单 commit revert。
5. **跨批回归**（批 F/G 改动碰坏批 D 布局）：每批验收含本批测试集 + 批 H 全量复跑兜底；批间 git revert 单元独立（每批一个 commit，无交织）。
6. **WSL/win32 环境坑**（memory 固化）：pytest 走 WSL `.venv`；双层 shell 引号写临时 .sh（纯 LF）；`python3` 直调被 WindowsApps alias 拦截；并行子代理 ≤2-3 防 429；Playwright 无 sudo 备忘（跑不到就静态断言 + 记录，fail loud 不硬编）。

## 7. 规模标注

**large**。判断依据：9 个 commit 段；仓库目录大迁移（~90 个 git mv/cp 对象：15 yaml + ~69 agent 目录 + 7 subagents 目录 + KB 整树 860K）；多模块代码改造（compile/run/exec/iface.cli/iface.web 四层）；skill 资产 17 case 重排；web 前后端 + 设计前置；每批独立验收 + 全量首跑点 + 真机安装态验证。计划深度与之匹配（全量计划，非 mini）。

---

## 附：实测裁决记录（SPEC 原文 vs 实测，均已 grep/read 复核）

| # | SPEC 原文 | 实测 | 裁决 |
|---|---|---|---|
| 1 | psu 五文件含 `psu_search_pipeline/scripts/_common.py`、`psu_search_pipeline/scripts/search_space_table.py` | `grep -rn "parents\[" workflows/agents/` 仅 5 处：`psu_retrain/scripts/progress_watcher.py:61`、`psu_run_train/scripts/progress_watcher.py:61`、`psu_retrain/scripts/_common.py:47`、`psu_run_search/scripts/_common.py:47`、`psu_expand_supernet/scripts/search_space_table.py:38` | 采 spec-reviewer 终审清单（= 实测）；SPEC 自授权"以 grep 实测为准" |
| 2 | `tests/iface/in_session/test_monitor_until_done.py:17-19` | 文件实际在 `tests/workflows/test_monitor_until_done.py`（:17-19 锚点吻合） | 采实测路径 |
| 3 | 前端 `src/components/workflow/WorkflowBrowsePage.tsx`、`src/stores/workflow-browse-store.ts`、`test/workflows-page.test.tsx` | 实际 `orca/iface/web/frontend/src/components/pages/WorkflowBrowsePage.tsx`、`frontend/src/stores/`、`frontend/test/` | 采实测路径（SPEC 少 `frontend/` 段） |
| 4 | validator subagents 定位 `:122-131` | `validate_workflow` :121-156，定位语句在 `_check_subagents_md` :1234（:1244 是其错误分支） | 锚点修正为 :1234（grep 锚防漂移） |
| 5 | install merge 承诺 `:549-551/:597-599` | 实测 docstring 在 :545-548/:594-597 | 锚点微漂，定位无歧义 |
| 6 | KB 裸调用「从调用链透传 wf」 | bootstrap :1410 同函数已有 `wf_obj`（:1190）；next :1815 有 `_load_wf_for_run`（:1659）现成 | 透传比 SPEC 预期更简单，零链路改造 |
| 7 | SPEC 步骤 2 未提 agents 解析器 | `LocalPoolResolver._search_bases` 首项 = `workflow_dir/agents` → per-wf 天然兼容零改动；web `_resolve_context_for` 同理 | 计划显式声明「不动」，防 coder 画蛇添足 |

## 附 2：plan-adversary 第 1 轮闭环记录（12 质疑全闭环）

| Q | 裁决 | 落点 |
|---|---|---|
| Q1 `_quant_scripts` 全 copy 留原池炸自检 | 真问题，已修订 | §4.4 SELECTIVE_POOL：首 wf git mv + 其余 cp，空壳 rmdir 兜底 |
| Q2 随包旧平铺 yaml 升级残留 shadow | 真问题，已修订 | §3 批 E / §4.5 要点 2 分支③ + 升级安装回归用例 |
| Q3 backup 名字级比对误删用户自改 | 真问题，已修订 | §3 批 E / §4.5 要点 2 内容级 sha256 比对 |
| Q4 subagents description 无数据来源 | 真问题，已修订 | §3 批 G / §4.7：兜底钉死空串，name 恒出 |
| Q5 e2e_redesign/contract.py 断链三不管 | 真问题，已修订（deferred 路线） | §3 批 D 冻结行 + §4.8 要点 4 REPORT 待决策区 |
| Q6 整删共享安装态无在途进程检查 | 真问题，已修订 | §4.8 顺序敏感点 1②：pgrep 前置断言 |
| Q7 残留 grep 白名单未消化自身引入的合法文本 | 真问题，已修订 | §4.8 顺序敏感点 3：判定语义 + 预置白名单 |
| Q8 批 A 枚举与「status 干净」自指矛盾 | 真问题，已修订 | §4.1 一并提交计划文件 + CURRENT.md |
| Q9 md 校验 diff 口径错 + sha256 基准未定 | 真问题，已修订 | §3 批 D `git diff --cached -M` + §4.4 自检 d 基准点 |
| Q10 layout.py 是否私货 | 设计权衡，接受 + 补验证 | §4.2 注意段：批 H 核实三处 import 无复制；微调限 compile 包内 |
| Q11 `_INJECTED_KB_ENV` 槽位语义含糊 | 设计权衡，收严 | §3 批 C：忽略 env 项完整重走 + 测试③ fixture 钉死 |
| Q12 记录性锚点漂移 5 项 | 驳回为漂移记录 | kb_graph :357+用法行（已修入表格）；commands.py 3 处（已修）；subagents 6 目录（已修）；validator :1234（已修）；web 设计文档名钉死（已修 §4.7） |

无疑问项 12 条（成功标准 1/2/3/4/4b/5/6 落点、决策 4/5/8、双形态无误扫、parser 提取完备性 65 agent 反查、批次顺序闭合）见对抗轮原始报告，未触发修订。

## 附 3：plan-adversary 第 2 轮闭环记录（4 新真问题全闭环）

| Q | 裁决 | 落点 |
|---|---|---|
| Q13 批 E 内容级比对三孔（pycache 不对称 / 共享双源基准 / subagents+KB 映射缺失） | 真问题，已修订 | §4.5 要点 2 比对规则三句钉死（ignore 对称 + any-match + 两映射公式） |
| Q14 批 H pgrep 误报跳过全链验证 / 漏检 `python -m tars` | 真问题，已修订 | §4.8 顺序敏感点 1②：精确匹配集（`tars serve` / `bin/orca`）+ PID 甄别归档再停机 |
| Q15 writing-style.md:74 泛化示例半修裂缝 | 真问题，已修订 | §3 批 B 该行：同示例对一并 per-wf 化 |
| Q16 三处记录性错（step 6 顺序 / 附 1 #4 行号 / commands.py 处数） | 已修订 | §4.4 step 6 顺序钉死自底向上；附 1 #4 → :1234；commands.py → 4 处（:272/:731/:883/:1076） |

第 2 轮对第 1 轮 12 项裁决逐项核验：全部落实；第 1 轮无疑问项 12 条抽查全部维持、无翻案。第 2 轮新问题均为修订增量缺陷，烈度低-中，闭环后无遗留 open 项。
