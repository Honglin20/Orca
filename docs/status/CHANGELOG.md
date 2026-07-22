# CHANGELOG —— 任务索引

> 每个任务完成后，在**顶部**加一条索引（1-2 句话 + commit SHA + release note 链接）。
> 最近的在上面。**不积累、不延后**——完成即记。

---

## [2026-07-22] doctor --probe-push 推送链路诊断（H1-H6 全 6 跳）

`orca doctor --probe-push`：一次跑完推送链路 6 跳（family_detect / cac_pid_walk / adapter_discovery / daemon_progress / bus_flow / ws_delivery），精确指出哪一跳断（不止「daemon 活着」）+ 输出 first_break + fix_hint 指针指向 runbook。新增唯一模块 `_push_probe.py`（叶子消费方，复用 _hostenv/sidechain_daemon/events.adapters 现有真相源，零新增接口）+ runbook `docs/troubleshooting/push-chain.md` + cli.py 加 3 typer Option（零副作用：无 --probe-push 时输出与基线一致）。H6 self-spawn 走 B2 决议 degradation（RunManager.start_run + monkey-patch Orchestrator.run + bus.emit 合成事件 + WS 3s 等收）。40 测试全绿（含 SPEC §5 三组守门 + fast e2e 冒烟 happy/负向 + H2 中间态自洽双向）。Commits: `275838b` (S1) → `af97ac1` (S2) → `a3f10a1` (S3) → `284b389` (S4)。详见 [release note](../releases/2026-07-22-push-chain-diagnostic.md)。
- **+ S5（`5b68629`）**：H6 passive `--ws-url` 模式生效——连用户真实在跑的 web，subscribe `--run-id`，8s 窗口被动等收真实事件（pass/fail/unknown 三态），回答「我这个 run 的事件到没到前端」。新增 `_hop_h6_ws_delivery_passive_async`；4 passive 测试，44 push_chain 全绿。

## [2026-07-22] bootstrap 启动即把 web 链接反馈给用户

补 `9677c1e`（bootstrap 默认自动开 web）的遗漏环：detached `orca open` 子进程的 URL echo 进了日志文件、用户终端看不到 → bootstrap 自身启动当下即算出 URL（单一真相源 `resolve_web_endpoint`，新增 helper `_resolve_web_url`，lazy import 避循环 + soft-fail），分两路显式吐给用户：①JSON `reply["web_url"]`（模型驱动路径拿得到）②stderr `Orca Web UI → <url>`（直接终端可见，不污染 stdout 契约）。**不进 `prompt`**：prompt 须与 `next` idempotent 重发逐字相等（`test_f1_resume_flow` 不变量）。已知 limitation：不探活端口归属（与 soft-fail 一致，端口探活是 `orca open` probe 的职责）。code-reviewer 一轮闭环（0 🔴，2 🟡 全修）；25 passed。详见 [release note](../releases/2026-07-22-bootstrap-surface-web-url.md)。

## [2026-07-22] KB 可移植 + struct-exploration 结构优先（direction 覆盖软闸）

解决「struct-exploration 只改超参不碰结构」两个根因。**Part 1（KB 可移植）**：`orca install` 部署 `knowledge_base/` → `~/.orca/knowledge_base/`；`config.resolve_kb_dir()` 确定性解析 KB 根（env>config>~/.orca>cwd，first-existing，显式来源权威不静默回退）；`ORCA_KB_DIR` 经 `build_env_overlay` 注入（env 作 transport，exec 不 import iface）；`apply_kb_requirement` 对 `requires:[knowledge_base]` 的 workflow 在 run 启动/in_session bootstrap 预检 KB，缺失 → ConfigurationError fail-loud（含 searched 路径+指引），不进 setup agent。**Part 2（结构优先软闸）**：新增确定性 `direction_coverage.py`（KB meta.json 枚举本族 direction 目录 D0-D21 + 读 ledger tried direction_id → untried/all_exhausted/near_target）；hypothesizer 每轮跑之 → 软闸优先选未试结构方向（all_exhausted/near_target 才允许 hyperparam）+ 补读 directions 切片 + 输出 direction_id；curator 记 direction_id 进 ledger；yaml 加 requires + setup 缓存 directions + KB 根改 $ORCA_KB_DIR。kd-nas defer。commit `6e0f167` + `0be8c6d`。详见 [release note](../releases/2026-07-22-kb-portability-struct-direction-gate.md)。

## [2026-07-22] Stage 3 统一 headless TARS-SKILL E2E（禁 CLI 驱动 workflow）

建 `tests/e2e_redesign/` 统一 headless TARS harness：经 TARS skill 路径（`orca <wf> --inputs` + `orca next --run-id`，复用 spike 基建，**禁** `orca run`/手搓 next）三层验证 8 workflow。①静态契约闸（64 parametrized：inputs/无残留引用/output_schema 链/device+seed/chart 标签/造假扫描 AST 感知/prohibition 正向存在）；②headless DAG walk（schema_faker 合成最小合规 JSON 喂 next，单节点 quant×4 到 done:true、多节点 bootstrap+首跳）；③哨兵路径 E2E（ptq-sweeper spawn→哨兵→resume→真实 output→done，断言 task_id 复用+哨兵不进 --output+MAX_ASK 兜底）。契约闸就地修了 **6 处 chart label 缺失**（P1/Stage4 遗漏：qat bar x_label + 5 处 table caption 漂移）+ **1 个 P9b 真 bug**（agent-struct setup prompt 自引用 `{{ setup.output.struct_scripts_dir }}` 渲染崩 → `{% raw %}` 转义——静态链 check 抓不到的**自引用**，动态 bootstrap 抓到）。code-reviewer impl+coverage 两轮闭环：0 🔴，5 🟡（DAGStallError 异常分离/schema_faker docstring/load_parsed DRY/conftest dir 过滤/测试名达意）+ 若干 🟢 全修；新增 4 个函数边界单测模块（schema_faker/contract_logic/conftest_cleanup/walk_dag_branches，39 测试）闭合确定性逻辑的 Rule 9 盲区。**120 passed, 2 skipped**（kd-nas 受用户既有活跃 run 阻塞 skip，按硬约束不 stop 用户 run）；回归 workflows+compile+spike 246 passed。全量真模型 run 不可行（无 GPU/数据集，预期），落在结构/契约/sentinel 可行边界。Commit: `89d30ee`。详见 [release note](../releases/2026-07-22-stage3-headless-tars-e2e.md)。

## [2026-07-22] P9a quant/NAS 按 input 三档原则精简 + 切 $ORCA_ARTIFACTS_DIR

quant-ptq-sweep 12→3 / sensitivity 11→3 / qat 15→3 / bit-curve 16→6（保留 accuracy_tolerance/avg_bit_budget/max_evals 三个 Tier A KPI/预算）；nas-agent-pipeline / nas-hp-search 6→5（下沉 output_dir）。Tier C 固化脚本默认（mode/bit_width(s)/recipes/scheme/cage/bake/method/ratio/low_bits/high_bits/candidate_format_space/bit_objective/granularity）；Tier B infer-once + P4b 哨兵（project_root 从 model_path 向上走对齐 NAS P6 + calib/train/eval loader/eval_fn ref）；output_dir → P8 `$ORCA_ARTIFACTS_DIR`（env 缺则 fallback 旧路径）——NAS entry infer-once + propagate（下游 nas-train-runner/nas-select 读 `{{ model_optimizer.output.output_dir }}`）。清理 P5 遗留 dead required 参数（4 脚本 --project_root/--calib_data_ref/--eval_data_ref/--train_data_ref/--eval_fn_ref，loader 逻辑在 adapter 不消费）。裁决策：qat lr/total_steps 走 smoke 兜底不走哨兵（QAT 短训恢复超参 ≠ 用户全量 epochs，哨兵 over-ask）但脚本 stderr WARN 非静默。SPEC §5 表 nas 目标 4→5（算术笔误订正）。code-reviewer 一轮闭环：0 🔴 + 2 🟡（qat WARN + 裁决策留痕）+ 4 🟢 全修或登记；新增 `test_p9a_input_contract.py`（6 wf input key 集合契约）。`tars validate` 0 error；Jinja StrictUndefined 16 节点 OK；250 passed。Commit: `64c5c11`。详见 [release note](../releases/2026-07-22-p9a-quant-nas-input-slim.md)。

## [2026-07-22] P9b struct/kd 按 input 三档原则精简 + 切 $ORCA_ARTIFACTS_DIR + setup 哨兵化 + create-workflow-skill 编码三档

struct 11→9 / kd 17→9 inputs（6 [ask] 主 + 3 [advanced] 固化）；Tier C 下沉（struct/kd_scripts_dir → setup.output.X infer-once+propagate；iterations 完全移除，引擎兜底 100 + `--max-iter` CLI 覆盖；teacher_layers=6/short_epochs=10/full_epochs=50/eval_dataset=""/proxy_dataset_spec="" 固化进 prompt）；setup 节点切 P8 `$ORCA_ARTIFACTS_DIR`（env 优先 + llm_artifacts 回退 + 尾斜杠补齐）；**P4b 遗留收口**——struct setup（yaml 内联）+ kd-setup Step 1 的 build_fn/dummy_input 缺失走 ask-user 哨兵（原「低置信猜测」/「报错」是潜在造假）；create-workflow-skill 编码三档（SKILL.md 新增专节 + reference §6 + 新 demo `tier-discipline.yaml`）。code-reviewer impl+coverage 两 review 闭环：0 🔴，4 🟡 + 7 🟢 全修或登记；新增 `test_no_jinja_ref_to_undeclared_input`（8 wf parametrize，填补 compile validator 只 warn 不 error 的契约守门空白）。`tars validate` 0 error；compile+workflows 196 passed。Commit: `8a1e5f0`。详见 [release note](../releases/2026-07-22-p9b-struct-kd-input-slim-and-skill.md)。登记后续：三档标签 lint / tier-discipline benchmark case / 生产 agent 哨兵 E2E（批 3）/ SPEC seed Tier A vs [advanced] 张力澄清 / SPEC device↔target_hardware 命名加注。

## [2026-07-22] Stage4 viz 执行：producer 侧 ~15 张图补 x_label/y_label/caption + 方向标注

应用 [viz 优化方案](../plans/2026-07-22-workflow-viz-optimization.md) 6 批 checklist：~15 张缺标签图（quant/NAS/kd/struct 推图脚本）全补人话轴标签 + caption + metric 方向（↓lower is better）；NAS 终态帕累托 scatter→pareto 恢复前沿连线。**dedup 键 label+title 冻结**：全范围零 label=/title= 值改动 → 无重复图风险；`higher_is_better` 设 required（错默认会反向 caption，违 Rule 12）。code-reviewer 零 🔴 + 2 🟡 全修；24 viz 测试无回归。Commits: `e05ad2d`/`81facb9`/`3b01a5f`/`4559f65`/`22b2392`/`250e4a7`/`23361af`。详见 [release note](../releases/2026-07-22-viz-labels-producer-side.md)。遗留：`nas-viz/scripts/` 死代码（零 yaml 引用）待 DRY 清理。

## [2026-07-22] P8 引擎注入 `$ORCA_ARTIFACTS_DIR` + `orca gc` 命令（Phase 4-A/4-C）

单一真相源：`artifacts_dir_for_run()` 落 `orca/chart/_paths.py`（绕开 exec/↛run/ 反向 import 契约），`build_env_overlay` + executor/script mirror + bootstrap `mkdir -p` + `orca_env.sh` 注入 `ORCA_ARTIFACTS_DIR=<abs>/runs/<run_id>/artifacts/`；workflow `source orca_env.sh` 后 `os.environ` 读（替换自建 `llm_artifacts/...`，P9 迁移）。`orca gc --max-age 14d [--keep N] [--dry-run]`：4 类候选（stale-run/orphan-dir/orphan-marker/orphan-lock）+ 安全（active run 不删 / 路径逃逸拒 / advisory lock / MANIFEST run 跳过保 P9 worktree 闭环）。67 新测试 + 664 回归 passed；spike NameError 已修。Commit: `b1eaf43`。详见 [release note](../releases/2026-07-22-p8-engine-artifacts-dir-and-gc.md) + [P9 接口约定](../releases/2026-07-22-p8-engine-artifacts-dir-and-gc.md#p9-接口约定)。

## [2026-07-22] P4b agent.md 接入 ask-user 哨兵（接通 ask-user 最后一段）

P4（TARS skill 哨兵检测）已激活，本任务把 6 个含 Tier B 必填项的 agent.md（ptq-sweeper/sensitivity-analyzer/qat-trainer/bit-curve-searcher/nas-search-pipeline/kd-setup）的「缺数据 fail loud」升级成「读代码无果→返回轻量哨兵 `{"_orca_ask_user","_sentinel":"orca_ask_user_v1"}`」。与 SPEC 轻量版（2 必填键，options/context 可选）+ spike `is_sentinel` strict 识别逐键对齐；保留 fail_loud fallback；DRY（每 agent.md 加「缺失必填输入时（严禁造假）」段，inline 回指）。`tars validate` 0 error；127 compile + 61 workflow + 39 spike 测试过。Commit: `530b580`。详见 [release note](../releases/2026-07-22-p4b-agent-md-ask-user-sentinel.md)。范围外：struct setup 哨兵化（yaml 内联 prompt，留 P9/后续）。

## [2026-07-22] P7 struct/kd workflow 精简（11→6 / 13→6）+ latency-first + 图表根因 + device

struct / kd 两 workflow 大刀阔斧精简：struct `family_detect+baseline_measure`→`setup`、`analyst+viz_round`→`curator`、删 `structure_gate`（tag 改 curator 内联 ast_diff deterministic 推导）、内联 `viz_finalize`→`finalize`；kd 同款再合 `teacher_setup+profile_gate+kd_train_script_gen`→`setup`、**`kd_trainer+measure_student`→`candidate_eval` 改 latency-first**（先默认权重导 ONNX 测 latency → 不达标 FAIL_latency 不训练 → 通过才短训测 proxy_mse；measure_student 加 latency-only 模式 db_gap=-1 sentinel）。图表根因修：viz_struct Pareto 过滤 accuracy=None（修 y=0 伪点）+ 删 Round Ledger / Exploration Tree（零信息）+ Candidate Ledger 短字段拆分；viz_kd round 模式 db_gap/met_acc 移出默认列 + finalize compare caption 标 champion deferred / teacher_accuracy_known=false 警告。device/latency_provider/ONNX：`_device.py`（resolve_device + ort_providers，inline 自 NAS）struct/kd 各一份；6 脚本加 `--device/--seed`；`export_onnx --no-external-data` 默认断言；`latency_provider` 升 [advanced] input；`seed` 加两 yaml；解开 export/measure_student/teacher_setup 原 `device="cpu"` 硬编码。P2 遗留收口：7 agent.md 拼接切 setup 专用字段 + CONTRACTS.md 6-节点 I/O 表同步 + kd-hypothesizer `rationale_summary`→`rationale` + kd-curator 加 phase==2 门 + champion_db_gap 短训恒 -1（不造假）+ kd-setup 不硬编码 teacher_accuracy_known。code-reviewer 一轮闭环：R1-R4 必修（造假/静默丢点/契约漂移/round 计算）+ M1-M7 中等（LLM 兜底/deterministic path/字段派生）+ L1-L7 轻微全修；新增 24 smoke test（latency-first 顺序 / Pareto None 过滤 / 6-节点结构 / output_dir 拼接守门 / teacher_accuracy_known 传播）。`tars validate` 0 error；319 测试无回归。**Surface-conflict（Rule 7）**：plan headline 写"11→7/13→7"，bullet 算下是 6——以 bullet 为准（更具体），plan 已订正。Commit: `66f74ea`。详见 [release note](../releases/2026-07-22-p7-struct-kd-restructure.md) + [计划](../plans/2026-07-21-workflow-redesign.md) §Phase 3。

## [2026-07-22] P6 NAS 系 workflow 重设计（补 KPI inputs + sink project_root + heavy 7→5 对齐 slim）

两 NAS workflow（`nas-agent-pipeline` heavy / `nas-hp-search` slim）补 4 个 [ask] KPI input（target_hardware / latency_constraint / max_rounds / seed）；`project_root` 下沉给 setup 节点 infer-once（从 model_path 向上走）+ output_schema 向后传（抄 agent-struct family_detect 范式）；heavy 7→5 节点对齐 slim 确定性护栏（删 viz_describe / LLM evaluator / viz_finalize，viz 内联进 setup、选架构复用 slim `nas-select`）；`train_runner` 加 output_schema `search_records minimum:1` 防假执行；`latency_estimator.py` 构造函数 device 无默认（forcing function）；dataset 缺失 fail loud（不造假，暂不哨兵）。code-reviewer 一轮提 1 🔴（output_schema vs SKILL Step4 早退契约断裂）+ 3 🟡（slim 同形 / heavy doc 漂移 / best-effort vs strict JSON 边界）全闭环：两 yaml `model_type` 加 `enum: [..., unsupported]` + 条件路由短路 $end + 两 setup agent.md 加早退 JSON 分支 + docs/workflows/{nas-agent-pipeline,nas-hp-search,README}.md 同步。`tars validate` 0 error；6 个 NAS agent.md Jinja2 StrictUndefined 渲染全 OK。Commit: `42e4a06`。详见 [release note](../releases/2026-07-22-nas-workflow-redesign.md) + [计划](../plans/2026-07-21-workflow-redesign.md) §Phase 2。

## [2026-07-22] P5：quant 四 workflow 正确性修复（删造假 + device + bit-curve bake 改动生效）

修 ptq-sweep / sensitivity / qat / bit-curve 四 workflow 的契约级硬伤：①**删造假**——agent.md 模板原指示「torch.randn 兜底 / 复用 calib 当 eval / 复用 train 当 eval」全删，改 Tier B 契约（读用户代码找 loader dotted-path → 找不到 fail loud，stderr 明确 + exit 2）；脚本 grep 0 个 `torch.randn`。②**device**——新增 `_quant_scripts/_device.py` 共享模块（`resolve_device` / `is_npu_available` / `set_seed` / `move_batch_to_device` / `wrap_forward_with_device` / `add_device_seed_args` / `resolve_device_and_seed`，inline 自 nas-agent 不引跨包依赖）；4 yaml 加 `target_hardware`(Tier A [ask]) + `seed`(默认 0) input；4 脚本加 `--device`/`--seed`，`fp_model.to(device)` + `wrap_forward_with_device`（batch 搬 device 自动做）；NPU 经 `torch.npu.is_available()` 有路径。③**bit-curve bake 改动生效**——`_bake_selected` reload 落盘 state_dict + 重 eval（strict=True 键失配 fail loud），返 `(path, reeval_metric)`；`_check_bake_metric_consistency` 超 tol（相对 1e-4）exit 3；持久化顺序保证 exit(3) 时 `best_mixed_model.pt` 与 `bit_curve_summary.json` 一致；bake 失败不阻断曲线产出（N7）。④`output_dir` 默认加 `/<wf-name>/` 子目录防撞；⑤qat 示例数字修正（recovery=after−before，mse 口径负=改善）；⑥sensitivity 补 `--env_file` 对齐 PTQ env 兜底；⑦qat recovery bar / bit-curve pareto 用 P1 轴标签（`x_label`/`y_label`/`caption`），pareto 标题用 `metric_kind` 替代写死的 "Accuracy"。eval_fn_ref 空 → WARN「用 teacher-student mse，精度仅自洽性参考」（SDK 合法默认，非造假）；eval_loader 缺 → fail loud（复用 calib/train 是禁掉的造假口径，code-reviewer Rule 7 surface）。code-reviewer 两轮闭环（impl + coverage 并行）：5 🔴 + 6 🟡 + 8 🟢 全处理（既有 7 类 helper 复制 + 死 required 参数登记给 P9 input slim 同期）。37 新测试 + 110 既有测试无回归；`tars validate` 0 error。详见 [release note](../releases/2026-07-22-quant-workflow-correctness-fix.md)。

## [2026-07-22] P4 TARS skill 哨兵处理全量（子 agent 缺必填项时问用户而非造假）

TARS skill（`orca/skills/tars/SKILL.md`）驱动循环第 2 步加哨兵分支 + 新增「### 哨兵处理」段：派子 agent → 子 agent 缺必填项返回哨兵 JSON → TARS 在调 `orca next` **之前** strict 识别（括号配平抽最外层 JSON + `_sentinel:"orca_ask_user_v1"` 魔键，非 substring）→ 捕获 task_id（CC `agentId`/opencode `ses_xxx`）→ 问用户（CC `AskUserQuestion`/opencode 聊天问）→ 恢复**同一**子 agent（CC `SendMessage`/opencode `Task(task_id=)`）→ MAX_ASK=3 兜底 fail loud → 真实产出才喂 `orca next`（**哨兵绝不进 `orca next`**，引擎零改动）。是 P3 spike `drive_node` 的 skill 指令投影（6 步控制流逐字翻译）。只改 SKILL.md，零引擎/workflow/agent.md 改动。spike 38 测试基线保持绿。code-reviewer 两轮闭环（design + spike-equivalence 并行，无 🔴，2 🟡 + 6 🟢 全修）。CC 主路径先 ship，opencode 标 experimental。Commit: `774aa46`。详见 [release note](../releases/2026-07-22-tars-skill-ask-user-sentinel.md).

## [2026-07-21] P3:0-b ask-user 哨兵闭环 spike（de-risk TARS 全量改造前的 ask-user 路径）

建独立最小 harness（`tests/spike_ask_user/`，2 节点 workflow + driver + 38 测试 + 2 真 claude integration）证明：子 agent 缺 Tier B 必填项 → 严格 JSON 哨兵（`_sentinel:"orca_ask_user_v1"`）→ driver strict 识别（非 substring）→ 捕获 task_id → SendMessage/Task/`claude --resume` **恢复同一子 agent**（task_id 复用断言）→ 拿真实 output → 喂 `orca next`（哨兵不进引擎，零引擎改动）；重入 3 次 fail loud（MAX_ASK）；造假检测（`torch.randn` 等）兜底。产出可复用 `SubagentBackend` ABC + `MockSubagentBackend`（scenario 全局时序）+ `ClaudeCliBackend`（`claude -p --session-id` + `--resume`，等价 CC SendMessage 的 headless 形态）+ `tars_loop.drive_node/drive_workflow`（SPEC §2 Python 投影）。code-reviewer 两轮闭环（impl+coverage 合并：1 🔴 哨兵泄漏断言空操作修复 + 5 SHOULD-FIX DRY/diagnose ABC 抽取/dead code 清理 + 8 新测试覆盖 OrcaBusyError/orca_cli 5 raises/nested JSON/node B sentinel 等）。**Spike pass**，可开 P4（TARS skill 全量改造）。详见 [release note](../releases/2026-07-21-spike-ask-user-sentinel.md).

## [2026-07-21] chart 加 x_label/y_label/caption 轴标签与图下说明能力（P1 workflow 重设计 Phase 0-a）

解「图表看不懂」根因 C：`render_chart` 签名加 `x_label/y_label/caption` 三参数（默认空串，仅在非空时塞 payload，与 `pareto_direction` 同款契约），单一真相源 = ChartPayload（backend `_render.py`/`_validate.py` + frontend `types.ts` 两端同源）。前端 `chartTheme.ts` 加 4 个 label helper（DRY，5 widget 共用）+ 新 `ChartCaption.tsx` 共享小组件，8 widget 全部接入（Line/Bar/Area/Scatter/Pareto 加 XAxis/YAxis label；Heatmap 加 caption + 矩阵下轴标题；Radar/Table 加 caption）。TUI `chart_canvas.py` plotext `xlabel`/`ylabel` + 空数据/非空数据两路径都保留 caption；heatmap 降级把 axis 拼进 hint 保语义。viz_struct `_push_champion_trace` 落地作证（候选序号 / 时延 / ★=达标）。**向后兼容**：旧 tape 无新字段 → 默认空串 → 三端回退旧行为；color（b820ef1）+ heatmap chart_type（ec3d598）零回归。code-reviewer 两轮闭环（一审删 Python 类 shadow 重复测试 + 修空数据 caption 丢 + 修 plotext reload cleanup 生效顺序；二审补 TUI 空数据 / heatmap 降级 / frontend 双轴缺省三个覆盖 🔴）。174 chart 相关测试 + 51 frontend chart 测试全绿；新增 27 测试。Commit: `a7de596`. 详见 [release note](../releases/2026-07-21-chart-axis-labels.md).

## [2026-07-21] workflow 产物路径拼接漏斜杠 BUG 修复（Phase 4-B / P2）

struct family_detect / kd teacher_setup 的 output_schema 新增显式带尾斜杠字段（snapshots_dir / worktree_root / viz_dir / ledger_path / champions_path + kd-only ckpts_dir / profile_report_path），setup prompt 强制 `OUTPUT_DIR=$(python3 -c "...os.path.abspath + '/'")` 一次计算；下游 struct-engineer / kd-engineer / kd-teacher-setup agent.md + yaml inline prompts（structure_gate / viz_* / profile_gate / kd_trainer）改读字段而非 `{{ output_dir }}<suffix>` 字符串拼根——从源头杜绝 `<run>snapshots/`、`<run>.worktrees/` 兄弟孤儿目录。code-reviewer 抓到 kb_cache/ 局部回归（m1）已修。范围外 7 个下游 agent.md（struct-evaluator / curator / analyst + kd-curator / analyst / hypothesizer / train-script）的同款拼接按计划留给 Phase 3 P7；CONTRACTS.md 节点 I/O 表 stale 同期处理。Commit: `e41974f`。详见 [release note](../releases/2026-07-21-workflow-path-concat-fix.md) + [计划](../plans/2026-07-21-workflow-redesign.md) §4-B。

## [2026-07-21] `orca open` 跨项目端口占用修复 + `bootstrap` 默认自动开 web

**A（`7d9b7eb`）**：7428 被别项目 orca 占用时不再静默挂错 tape。根因：`_open_run` 把相对 tape 路径跨进程 POST + 「7428 有 orca 就无脑复用」。修：`_identity.py`（新，`runs_dir_fingerprint=sha1(resolve)[:12]`，stdlib-only）+ health 加 `runs_dir_fp`（指纹非明文防 0.0.0.0 泄漏）+ `web_registry.py`（新，per-project 端口登记，探测权威 registry 仅 hint）+ `_open_run` 重写（绝对路径化 + 项目感知复用：本项目→registry→起新 server）+ SPEC §5a 同步。**B（`9677c1e`）**：`bootstrap` 默认开 web——post-lock 块 detach spawn `orca open`（与 chart/sidechain 守护同款 detach + soft-fail），stdout JSON 契约零污染；`--no-open-web`/`ORCA_BOOTSTRAP_OPEN_WEB=0` 关；schema-only 不触发。spec-reviewer 两轮 conditional-pass（6 blocker+5 HIGH 全闭环，B5 flock 降级为已知限制）+ code-reviewer 需修后合（3 🟡 测试缺口全补）。987 passed；2 既有失败（bg_integration/install nudge）git stash 证伪为基线既有。详见 [release note](../releases/2026-07-21-orca-open-cross-project-and-bootstrap-auto-open.md) + [计划](../plans/2026-07-21-orca-open-cross-project.md).

## [2026-07-21] Workflow 可视化全量优化（sensitivity bar/table + KD 0 图 bug + 横向优化）

7 个改动点（每点独立 agent 实现 + 逐 diff 验收）：前端 ChartPayload 加 `color` 字段（per-row 着色，hue 优先；BarChartWidget + ScatterChartWidget，`b820ef1`+`e1272e8`）；sensitivity bar 去 hue 改 color、table 改全层（`235ba98`）；**KD 0 图 bug 修复**——viz_round 复用 viz_struct 但 schema 不匹配致每行被剔→0 图，新建 viz_kd.py（4 图）+ 改 yaml 两节点（`f516223`）；struct 新增逐候选表（`0910c87`）；bit-curve 假 pareto 改真 pareto + 全候选 scatter（`70bb4ff`）；ptq-sweep 删无意义 hue + table 补失败行（`d154d1d`）；qat 补训练 loss 曲线（`f361171`）。KD 用真实账本 mock 捕获证实 5 图正确（修前 0 图）。详见 [release note](../releases/2026-07-21-workflow-viz-overhaul.md).

## [2026-07-20] sidechain family 由 env 身份决定（修 dotdir 误判回归）

`129fff8`（cac 优先）的回归修复：真 CC + `~/.cac` 存在（`orca install` 装 hook/skill 副作用）→ dotdir 探测误判 cac → daemon tail 空 `.cac` → 子 agent 消息进不了 web。根因：family 决策用 dotdir 存在性而非 env/进程身份。新增 `orca/iface/in_session/_hostenv.py`（stdlib-only）收敛 env 探测（提取 cli.py/sidechain_cmds.py 的 `_cac_session_id_from_pid`/`_host_session_from_env`/`_detect_backend_from_env` 副本 + 新增 `detect_family_from_env`：`CLAUDE_CODE_SESSION_ID`→cc / `CODEAGENT`+PID 回溯→cac）。三个 caller（`_spawn_sidechain_daemon` / doctor `_check_sidechain_backend` / `sidechain_cmds._print_effective`）统一 `detect_family_from_env() or config`，优先级 **env > config > dotdir 探测（兜底）**；events 层 `_family.py` 保留 probe 兜底不改。**builtins.next**：函数搬到 `_hostenv`（无 `def next` 遮蔽）后用普通 `next`（`__globals__` 绑定定义模块，CC/CAC 均安全）。code-reviewer 发现 3 处测试回归（test_sidechain_cmds / host_session_binding / sidechain_daemon 的 monkeypatch 路径 + config 断言）+ 2 stale docstring，全修。验证：doctor（真 CC+.cac）family=cc/resolved=.claude/available=True（修前 cac/fail）；daemon spawn family=cc（修前 None）；tape 34 个 agent_ 事件（修前 0）→ web 子 agent 可见。149 passed。Commit: `2f9be37`. 详见 [release note](../releases/2026-07-20-sidechain-family-env-identity.md).

## [2026-07-20] CAC session id PID 回溯替代 env 注入

撤回 `config.py` 的 `_normalize_cac_session_env()`（将 `CODEAGENG3_SESSION_ID` 注入 `CLAUDE_CODE_SESSION_ID`，仅在 Python 内存有效，子进程继承不到）。改用 **PID 链回溯**：`_cac_session_id_from_pid()` 沿 PID 链找 `codeagentcli` 父进程 → 读 `~/.cac/sessions/<pid>.json` 取 `sessionId`。`_host_session_from_env()` 加第三优先级（PID 回溯）、`_detect_backend_from_env()` 加 CAC 检测（`CODEAGENT=1` + session 可用 → `"cc"`）。同步更新 `cc_nudge.sh` / `sidechain_cmds.py` 两处副本。删除 `tests/iface/cli/test_config.py`（旧 `_normalize_cac_session_env` 测试），新增 CAC PID 回溯单元测试 ×4。

## [2026-07-20] sidechain cac 优先 + `orca sidechain family` 命令 + import 性能修复

CC sidechain resolver 探测改 **cac 优先**（`orca/events/adapters/_family.py::resolve_cc_sidechain_root`：`.cac` 存在即走 cac，含两存；原两存歧义默认 .claude）+ 新 `orca sidechain family` sub-Typer（set/show/unset，`--scope project|user`，照搬 `executor_cmds.set`）。配套：doctor fam_eff/hint 同步；`config.sidechain_family` helper（cli + sidechain_cmds 共享，DRY）；`load_merged_config` 合并 sidechain（修 project 级 `sidechain.family` 不生效既有 bug）。**import 性能回归修复**：`orca/iface/cli/__init__.py` eager import Textual TUI 壳 → 新命令 import config 拖慢 cli import（3.7s→5.9s）→ daemon pidfile 迟写 → 5 个 daemon e2e fail；改 PEP 562 `__getattr__` lazy + config profiles lazy 后 config import `4.4s→0.08s`，daemon e2e 全恢复。code-reviewer 核心检查全 pass（依赖单向/lazy/merge 边界/star import），修 2 minor（死 import / unset 空 dict 残留）。177 passed（含 5 daemon e2e）+ 86 非 daemon。Commit: `129fff8`. 详见 [release note](../releases/2026-07-20-sidechain-cac-priority.md).

## [2026-07-20] workflows 文档学术化重构（7 篇 + README）

按统一学术模板重写 `docs/workflows/` ×7（4 量化 + 3 NAS）+ README 索引：每篇**实现概览前置**（架构流程图 + 输入输出 + 激活）→ 定义 → 背景（含相关工作与引用）→ 方法（含公式推导）→ 实验 → 局限 → **附录库接口手册**（`ts_quant` / `nas_agent` 用户自调用法）。核心方法形式化并照库源码还原：PTQ 零空间 Q2N（Hessian 谱分解 + 能量骤降划零空间 + 子空间混合 + 行级闭式标量缩放 + 再量化回退）、W1 四种敏感度分析（mse / layer_stats 分布压力打分 / binary / mix）、W3 m0_pareto 三段（sensitivity probe → layer_policy 剪枝 → 主搜索）+ Pareto 支配关系、W4 CAGE 后校正（$W\!\leftarrow\!W-\eta\lambda_t(W-Q(W))$，不动点 $W^*\!=\!Q(W^*)$）、NAS 弹性超网 + NSGA-II 整数编码三算子 + 高权衡 Pareto 选择、struct-exploration 四不变量 + champion ratchet 单调下探。去除原版口语比喻，改学术风。Commits: `e94c45a`(PTQ) `66a9257`(W1) `3401212`(W3) `001ca77`(W4) `bd0da01`(hp-search) `78ca485`(agent-pipeline) `0c56995`(struct-exploration) `02e2225`(README).

## [2026-07-20] quant-bit-curve（W3）+ quant-qat（W4）—— 量化路线图收尾 + insession/7 workflow 文档

量化 pipeline W3/W4，类比 W1/W2（单 agent + folder-agent + `run_*.py` 确定性脚本 + adapter + render_chart + stdout JSON），全 mxint 基。**W3 `quant-bit-curve`**：与 W2 互补——精度约束下对比位宽/格式（INT8/W4A8/INT4/MX4/MX8），`search_mix_precision(strategy=m0_pareto, mode=explore)` 找 Pareto 前沿 + 格式分布可视化 + bake 最佳混合精度模型（`final.layer_configs`→`qconfig_dict`→`quantize_model`）。**W4 `quant-qat`**：对比 rtn/duquantpp 两训练态 fake-quant 方案，`prepare_trainable_fakequant_model` + `prepare_trainable_qat`(CAGE) + teacher-student label-free QAT，per-step 收敛 + 前/后恢复可视化 + bake 最佳 q_model。探针实证 W3（report/frontier/final 字段 + 格式→QConfig 表，因 candidate_format_space 只吃 QConfig 不吃字符串）+ W4（trainable API + duquantpp 两约束：显式 target_patterns + block_size 对齐）。验证：tars validate 0 error；ViT-Tiny 脚本级 smoke 双过（W3 cand_0002[INT8×26+INT4×24] bit 5.35 bake 21MB / W4 rtn+duquantpp 双方案 bake best）；in-session `orca <wf>` 返 schema。**文档**：`docs/in-session-usage.md`（安装+使用）+ `docs/workflows/` ×7（3 NAS + 4 量化，每篇激活→原理→结果+截图占位）+ README 索引。量化路线图完结。Commits: `e6646cf` + `da609ac`. 详见 [release note](../releases/2026-07-20-quant-w3-w4.md) + [计划](../plans/2026-07-20-quant-w3-w4.md)。

## [2026-07-19] quant-ptq-sweep workflow（W2 粗粒度 PTQ 扫描）

量化 pipeline 第二级。单 agent 节点 + folder-agent + `run_ptq_sweep.py`（833 行确定性脚本），双 mode：lightweight=4 累积路径 ablation（S/Q/A/R 派，~11 unique 候选，line 累积曲线）；full=位宽×预变换×求解×后处理全枚举（按 SDK §9.4 拒绝表过滤 rtn+q2n → 45 候选，heatmap 矩阵）。默认 `build_teacher_student_eval_fn` mse 评估 + bake 最佳 state_dict。修正 W1 `w4a16` 预设语义错位（`a_elem_format=fp16` 在 method=int 下不生效 → 改 `a_quant_enabled=False`）。code-reviewer 一轮（impl+coverage 合并）：6 🟡 全修（bake 顺序 / ts_quant 顶层 import / bake 白名单 / forward_fn 校验 / recipes DRY / w4a16 语义）+ 5 🟢 修；0 测试按任务范围（3 文件）+ plan §验证 deferred 阶段 5。Commit: `d356979`. 详见 [release note](../releases/2026-07-19-quant-ptq-sweep-w2.md).

## [2026-07-19] chart 加第 8 种 chart_type `heatmap`（行×列矩阵 cell 着色）

跨栈加 heatmap（量化实验对比矩阵：行=recipe，列=bitwidth，cell=accuracy）。**后端**（`_limits`/`_validate`/`_downsample`/`_render`）：加 `"heatmap"` 到共享 allowlist（两端同源）+ heatmap 必填 `x`/`y`/`value` fail loud + table 同款 top-N 降采样 + `render_chart` 加 `value` 参数。**前端**：`HeatmapChartWidget`（CSS Grid + 浅钢蓝→PALETTE[0] 钢蓝线性色阶，无新依赖）+ `ChartWidget` switch + `types.ts` 加 `value?: string`。**CLI**：修 CRITICAL DRY 违规（原 `chart_canvas.py` 复制 allowlist 漏更 heatmap → 改 import `_limits`）+ heatmap 终端 DataTable 降级。code-reviewer 两轮（C1/M1/M2/m1-m6 全闭环）：null/空串 cell 不 coerce 0 / 单值矩阵不除零 / 大数组 reduce 防 spread 栈溢出 / 色阶方向钉死 / 三端同源 contract test。78 后端 + 39 前端测试全过。Commit: `ec3d598`. 详见 [release note](../releases/2026-07-19-chart-heatmap-type.md)。

## [2026-07-19] 量化能力集成启动：W1 敏感层分析 + nas/create-workflow 配套修复

把 PatchTST_Optimal（ts_quant）量化能力集成成 Orca workflow 的第一块。**W1 `quant-sensitivity`**（`ca6bb60`）：单 agent + `run_sensitivity.py`（`analyze_low_precision_sensitive_layers` + `render_chart`），method 四选一、low_bits 默认 w4a4-mx 可配、按模型原始顺序可视化；ViT-Tiny 端到端实测通过（50 Linear 层 / 5 敏感层 / bar+table 推 web tape / done:completed）。实测修复 5 处：executor opencode→claude（当前环境 cc available）/ optional input 须 `[default]`/`[advanced]` 标签才能省略 / `module_types` 支持（CNN 需加 Conv）/ `ranked_layers` 真实字段名 `name` / `tars run --background` 的 `-i` 透传 bug → 改用 in-session `orca <wf> --inputs '{json}'`。配套：nas 4 agent 补 `.venv` activate fallback（`ce2158c`）；create-workflow 加 H8（description 须与 `orca list` 现有 wf 可区分，tars 选 wf 语义依据，`5e1f8f9`）；create-workflow validate 命令 orca→tars（in-session shell 无 validate 子命令，`7ee6276`）。ts_quant 已 editable 装入 conda orca env。路线图 W2（PTQ）/ W3（位宽曲线）/ W4（QAT）见 CURRENT。

## [2026-07-19] in-session 加固与性能（SPEC v4.1 整体交付：P3 + P1 + P5）

SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 驱动的 in-session 路径加固与性能优化。**架构铁律（用户）**：orca 管所有状态/决策/compliance，主 session 只调度（派子代理/传 output），不过度设计、不跨层耦合。经 3 轮 spec-reviewer + 用户原则简化（弃 host_session 豁免 / on_emit_success 回调 / 三态枚举 / prompt_file / compliance_warning 让主 session 反应）。

**已交付 3 包**（各自 code-reviewer 两轮 0 🔴 + 测试全绿，详见下三条 + 各 release note）：
- **P3 O1a**（性能）：`advance_step` 两次 tape 遍历合一次，`next` 性能税减半（`256a843`）
- **P1**（8 项小合集）：S7 tape helper / S9 daemon liveness / S2 SKILL flag CI 守门 / D3 sidechain 探针 / O2 bootstrap 锁缩小 / O3 status 透 compliance / O4 busy retry_after_ms / F3 inputs 校验 + `_errors.py`（9 commits）
- **P5 F1**（resume，最高价值）：session 断了续跑半完成 run —— status resumable + SKILL 续跑段 + 占位 spec；零 marker 改动、复用 `advance_step` idempotent-replay（`705009a`）

**暂停**（用户决策，不阻塞 workflow）：P2 D4+D5（marker 损坏/孤儿，低）/ P4 D1+D2（失败兜底，中）/ P6 S1（contract-test，低）。累计 ~900+ 测试 0 回归。

## [2026-07-19] in-session 加固与性能 P5（F1 TARS resume v4.1 简化版）

SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 §4 F1 落地。**架构铁律（用户）**：resume 是 run 级别的事（用 run_id 管，**与 host_session 无关**），复用 `advance_step` 现成 idempotent-replay（branch 4：`orca next --run-id X` 无 output 重发 prompt）+ SKILL 教续跑流程，**零 marker 字段改动、零 host_session、零 prompt_file**。改动：`cli.py status` 无参加 `resumable: True`（marker 在即 resumable，纯派生标志）+ 文本输出 + 尾行续跑提示；`SKILL.md` 加「续跑」段（status → next 无 output 重发 → 子代理 → next output 推进）；新建 `docs/specs/agent-interrupt-design-draft.md` 占位（in-session resume = F1 落地；engine-level interrupt = TBD）；修 `CURRENT.md` 断链。SPEC §7 F1 AC + §1 铁律 AC + v2→v3 changelog 闭环 stale（原写 host_session v3 语义，与 v4.1 矛盾）。守门双修：SKILL.md 用 `\bresume\b` word-boundary 守 tars 后端命令（允 `resumable` JSON 字段，禁孤立 `resume`）+ 去 `replay_state` 内部名。code-reviewer impl+coverage 两轮 0 🔴（🟡 全修：F1 测加 tape-not-added 否定断言 + 拆 `no_output_count` 为 `==1`/`==0` 精确断言 + 去掉 `--tape` 走默认路径解析真验生产形态）；196 in_session 测试全过。Commit: `705009a`。详见 [release note](../releases/2026-07-19-in-session-p5-f1-resume.md)。

## [2026-07-19] in-session 加固与性能 P1（8 项小合集：S2/S7/S9 + O2/O3/O4 + D3/F3）

SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 §6 P1 行 8 项一次做（cli.py 串行组，单一 coder 按序）：**S7** 抽 `tape.read_last_complete_lines` helper DRY 三处 binary-mode tape 读（chart/sidechain 守护增量扫）+ 8 单元测；**S9** 抽 `_daemon_liveness.{socket,pidfile}_daemon_alive` helper DRY chart/sidechain liveness 探针（pidfile+cmdline run_id 校验防 pid 复用）+ 10 单元测；**S2** SKILL.md code fence flag ↔ CLI `--help` CI 守门（regex 不引 markdown lib）+ 5 测含负面守门；**D3** doctor 加 `sidechain_daemon` 存活探针（hard=False，覆盖死亡不覆盖持续 iterate 失败 §8#4）；**O3** `status --run-id` 加 `no_output_count`（raw 透出，主 session 不反应 compliance）；**O4** busy 信封加 `retry_after_ms:500` × 3 处（`_echo_busy_reply` helper DRY；主 session 不重派子代理/不重发 prompt）；**F3** bootstrap `--inputs` 校验（手写 TYPE_MAP 不引 jsonschema；新 `orca/run/_errors.py` 登记 `INPUTS_VALIDATION_ERROR` 铁律 5.1；bool/int 双向反陷阱）；**O2** bootstrap 锁临界区缩到 dupe check + gen run_id + advance+emit + write_marker，spawn daemons 移锁外（dupe-check 不变量仍成立）。code-reviewer impl+coverage 两轮 0 🔴 blocker（3 🟡 impl + 5 🔴 test 全修，commit `d3893b9`）；862 测试全过。架构铁律（orca 管所有状态/决策/compliance，主 session 仅调度）逐条核通过。Commits: `9100481`(S7)/`047629f`(S9)/`4bb81c5`(S2)/`1ed2c90`(D3)/`bc620e3`(O3)/`a3e28bd`(O4)/`e5d3c5b`(F3)/`b4e4b67`(O2)/`d3893b9`(review 闭环)。详见 [release note](../releases/2026-07-19-in-session-p1-hardening.md)。

## [2026-07-19] O1a —— `advance_step` 内合并两次 tape 全遍历为一次（in-session 性能 SPEC v3.1 §3 O1a，包 P3）

`advance_step` 此前两次全 tape 遍历（`replay_state(tape)` + `Orchestrator._inputs_from_tape(tape)`），合并为单次 `_replay_state_and_inputs(tape) -> (RunState, dict)`（落 `events/replay.py`，与 reducer 同文件，单次遍历既 fold state 又抽首条 ws.data.inputs）。`advance_step` 单次调用 tape 迭代 2→1；`_inputs_from_tape` 改薄封装保留对外 API（`from_tape`/`_bare_instance` 调用方零回归）；`replay_state` 对外 API 不变。pure refactor（state+inputs 逐字相等，决策三分支/emit 序列零改）。SPEC §1 铁律 + §7 O1a AC 逐条达成；code-reviewer impl+coverage 两轮 0 🔴/0 🟡（5 🟢 全修：砍 `since_seq` 防 footgun / snapshot 改固定值去自证 / 加 first-ws-bad 测试 / wrapper parity 参数化 / AST grep 守门 AC3）；654 测试全过（events+run+iface/in_session，+13 新测试）。Commit: `256a843`。详见 [release note](../releases/2026-07-19-o1a-tape-traversal-fold.md)。

## [2026-07-19] Web 界面视觉优化（P0–P4：token 收口 / lucide / 左栏增强 / TopBar+WS+暗色 / 三栏统一）

纯前端（后端零改、testid 与功能接口保留）。5 阶段：**P0** token 收口（179→26 hits 全白名单 NODE_STATUS_HEX/PALETTE/LEVEL_TEXT_COLOR/DiffView，status-style.ts DRY 出口）/ **P1** lucide 统一图标库（全量替换 emoji，保留 ▎ 流式光标，test oracle 迁移）/ **P2** AgentsRail 增量增强（元信息单行 + running ▎ + 色条加粗）/ **P3** TopBar（runId 复制 + status badge）+ WS 连接指示（ws-connection-store，SPEC §1.1 transport-only exception）+ 暗色三态开关（SPEC §7 双触发 `.dark/.light`）+ amendment 文档 / **P4** 三栏 surface 统一（orca-bg-app 治割裂，去双线 border）。spec-reviewer conditional-pass（3 决策 D1/D2/D3 全收敛）。318 test PASS（1 pre-existing flaky DAG lazy）。Commits: `644cc4f`(P0)/`a8c6a3e`(P1)/`a577367`(P2)/`13d0e1f`(P3)/`617d991`(P4)。详见 [release note](../releases/2026-07-19-web-visual-refinement.md)。

## [2026-07-18] 节点记忆（Node Memory）—— AgentNode 跨 run 记忆（写确定性 / 读注入 agent 判断）

in-session workflow 此前无跨 run 记忆（新 run_id 看不到旧 run 产出）。**否决确定性指纹缓存**（agent 非纯函数，收益薄改动大），改为把必然性与智能判断解耦：**写记忆 = 引擎确定性**（节点完成必然覆盖写 `<cwd>/.orca/memory/<wf.name>/<node.name>.md`，存上一轮 output 原文，不靠子 agent 自觉）+ **读+跳过 = agent**（prompt 注入「上一轮记忆+复用协议」，agent 自判复用/重跑，走正常推进路径，**引擎零 skip 分支**）。`AgentNode.memory: bool`（opt-in，仅 AgentNode）；新 `orca/run/memory.py`（write/read/inject helper，零 events/tape 依赖）；`_step_io.apply_step_result` emit_batch 后置写（cli/daemon 单一真相源）；`step._deliver` 注入；CLI `--no-memory`；best-effort 写失败不阻断（MD 是派生缓存，tape 才是真相）。覆盖式写 = 天然单份 + 过期清除。spec-reviewer conditional-pass（5 P0 + 3 决策全收敛，修正 2 处事实错误）；code-reviewer 2 🔴+6 🟡 全修；22 新测试 + 515 回归全 PASS；test-agent 真机 5 场景全过（首跑写/二跑注入/--no-memory 字节级不动/跨 cwd 隔离/写失败不阻断）。不碰 EventType/reducer/tape/advance_step 决策/Status 语义/render_prompt。Commit: `29c70b3`。详见 [release note](../releases/2026-07-18-node-memory.md)。

## [2026-07-18] Web 前端呈现层完善（P1-P5：log 降噪 / 子 agent 维度 / 左栏重做 / cac-nga / 美化）

B2 把子 agent 过程事件推 tape 后前端暴露 6 痛点（log 暴涨 / 对话异常长 / 执行完才显示 / 左栏割裂 / ITERATION 难观测 / cac-nga 不适用），根因同源于「子 agent 维度缺失 + 无事件分级」。5 阶段：**P1** LogStream 分级 classifier（e3b8ad 4779→19 行，过程事件归 ConversationView）/ **P2** 会话按 (node,session_id) 分段 + store in-order 增量 fold + nodesIndex（buildEntries 4226→~208）/ **P3** 左栏统一底色+根治 GAP(w-56→w-full)+NODE_STATUS_HEX 色条+Setup/Loop/Finalize 分组+R{iter}+子 agent 折叠 / **P4** cac/nga 家族路径解析（_family.py 零 iface import，env>config>probe>default）+ doctor sidechain_backend check / **P5** 图表可读(axis-tick slate-700)+统一 cursor 消 hover 黄+去 cost+9 token 明暗双套（reviewer 抓 R1 CSS hsl→rgb bug）。参考 microsoft/conductor 双 classifier 分流。spec-reviewer conditional-pass 全闭环（7 P0+5 P1+4 P2）；test-agent 真机（e3b8ad + react-dom/server）全 PASS 无 P0。事故：P2 stash 险丢并行 P4（已恢复零损失+memory 固化 git 禁令）。Commits: `0a4683d`(P1)/`b77422f`(P4)/`3a0f66e`(P2)/`7cc232e`(P3)/`f0cf695`(P5)/`2d416eb`(构建产物)。详见 [release note](../releases/2026-07-18-web-presentation-refinement.md)。

## [2026-07-17] B2 test-agent 真机 E2E 收尾：3 P0 bug 修复 + 5 回归测试

test-agent 真机 E2E（4435 真 CC `agent-*.jsonl` + 573 真 opencode `event` 表行 → 真 daemon subprocess → 真 tape → 真 `tars serve` HTTP → 真 react-dom 渲染）暴露原代码（`ed5cbeb`）3 个**单测盲区 P0**（79 单测全 PASS 但真机死）：① opencode DB 路径错（代码找 `session.db`，真机 v1.18 写 `opencode.db` → discover 静默返空 → ingest 0 事件）② opencode `source_id=opc:{seq}` 跨 child 撞车（event PK=`(aggregate_id,seq)`、seq per-session 非 global，多 child 44% 撞 → dedup 静默丢）③ text-mode `seek(字节)/read(字符)` 混算在多字节 UTF-8 tape 崩（offset 漂移到 continuation byte → `UnicodeDecodeError` 非 OSError 未兜住；波及共享 `chart_daemon`，B2 引入中文 agent_* 必崩）。修复：`opencode.db` 优先 + `source_id=opc:{child}:{seq}` + 三处 binary-mode（byte seek + `rfind(b"\n")` + decode）。补 5 回归测试。fix 后 64（B2+回归）+ 7（chart）+ 20（daemon）全 PASS；grep 守门 0 hit；test-agent V1-V10 全链路真机 PASS（实时 ≤1.0s / 幂等 / 无串台）。Commit: `99efcde`。

## [2026-07-17] B2 子 agent 过程推送 web（双 adapter：CC jsonl + opencode sqlite）

in-session 路径 detach 起 sidechain 守护，主动 tail CC sidechain jsonl / 查询 opencode sqlite event 表 → 经统一 IR `RawAgentEvent`（payload 1:1 = EventType.data，R1）→ `SidechainIngestor`（1:1 透传 R2 + source_id 查重 R3 + U1 读 tape 派生 node §6）→ `bus.emit` → `_FlockSafeTape`（复用 chart_daemon 七组件，零 DRY）→ follow_task → WS → 前端（**零改**，复用 B1 entries.ts agent_* 渲染）。SPEC-B **v4**（spec-reviewer conditional-pass，5 BLOCKER 全闭 R1-R7 + 4 决策 U1-U4）；接口同一性 grep 守门 0 hit；防御性 deviation 登记（CC source_id 扩 block_idx；opencode source_id 用 seq 而非 part.id，因单 part 双状态必撞）；code-reviewer 0 🔴 + 5 🟡 全修；79 新测试 + 352 events/in_session 回归全 PASS；e2e subprocess 测试覆盖实时 ≤2s / SIGKILL→respawn 幂等 / 终态自退。Commit: `ed5cbeb`。详见 [release note](../releases/2026-07-17-subagent-output-b2.md)。

## [2026-07-17] orca list 瘦身 + inputs_schema 移到启动命令

砍 `orca list` 的 `inputs_schema`（选 wf 阶段 84% 字节噪音；`agent-struct-exploration` 单 wf 21 input 字段占该 wf 输出 90%）→ 只返 `{name, description}`；schema 改由启动命令 `orca <wf>` 不带 `--inputs` 按需带出（带则真启动），**零新命令**（命令数 7 / 保留字 / CI 禁 describe 全不变）。改动：`cli.py` `list_workflows` 砍字段 + `bootstrap` 加 `inputs is None` 纯只读分流（不建 run/tape/marker）+ `catalog._inputs_to_schema_list` 公开化为 `inputs_schema_list`；SKILL 三步重组（list 选 → `<wf>` 看 schema → `<wf> --inputs` 启动）；SPEC §2.1/§2.3/§4.2/决策5/§8/§11 同步；测试 list 断言重写（按名定位，**顺手解 `~/.orca/workflows` 隔离缺陷**）+ 新增 schema 返回测试 + ~15 处 bootstrap 补 `--inputs "{}"`（3 个 `_bootstrap` helper 一处覆盖）。list 字节 4010→636（降 84%）；268 + 185 测试全过；`tars validate` 3 wf 过；code-reviewer 0 🔴（🟡 SPEC stale + 🟢 优化全修）。Commit: `ec3d598`。详见 [release note](../releases/2026-07-17-orca-list-slim-schema-via-start-cmd.md)。

## [2026-07-17] B1 前端渲染 node_completed output（子 agent 输出推送 web）

解 in-session web **不显示节点 output** 痛点：output 已在 tape `node_completed.data.output`，但前端 `entries.ts` 把 `node_completed` 归 `node-divider`（`NodeDivider` 不读 output）。**B1 纯前端零后端**：`entries.ts` 移 `node_completed` 出 `NODE_DIVIDER_TYPES` + 新增 `node-output` kind；`NodeOutputBlock.tsx`（新增）按 `typeof output` 分支（string→Markdown / object→`safeJson` JSON / null→dim）；`ConversationView` case + `estimateRowHeight:160`；删虚构 elapsed。spec-reviewer conditional-pass（修 dict BLOCKER + elapsed MAJOR）；4 commits；test-agent 真机 PASS（生产 build + 真 `tars serve` + attach 真 tape + `react-dom/server` 渲染 13 节点含 9 dict **零 `[object Object]`** + 17 单测）。Commits: `75116a0`…`8ebe45d`。详见 [release note](../releases/2026-07-17-subagent-output-b1.md)。**B2（过程推送）暂缓待用户决策**（spec-reviewer fail，5 设计洞 + U1-U4 + SoT 灰色）。

## [2026-07-17] host_session 绑定防串台（tape-only）

修 nudge「串台」：run-id ↔ 宿主 session 绑定，nudge 只提醒本 session 的活跃 run。host_session 只存 tape `workflow_started.data`（同 yaml_path tape-only 先例，**marker.py 零改动**，无 desync）；env 优先级 `ORCA_HOST_SESSION_ID` > `CLAUDE_CODE_SESSION_ID` > None；nudge 读 tape 首行过滤 + per-session 限流；emit 真链 lifecycle←step←cli；opencode `shell.env` hook 注入 + fail-open 安全网（防 nudge 静默死）。spec-reviewer 对抗评审 13 挑战全闭环（tape-only 是用户铁律直接推论）；25 单测 + test-agent 真机 E2E 全 PASS（多 session 不串台 + Stop-hook env 实证 + opencode 端到端）。Commits: `70c2ac8`…`3dae964`（8 commits）。详见 [release note](../releases/2026-07-17-host-session-binding.md)。

## [2026-07-16] nas-hp-search runner/select 反伪造 + output_schema 强制

修「假执行」bug（tape 铁证：runner(3s)/select(19s)/train_script_gen(1s) 没跑脚本、只复述上游散文；search.jsonl 640 条是诊断时手动跑的）。根因 = prompt 诱骗（顶部上游散文的「已完成」语域诱骗 deepseek 顺着复述）+ 无强制（fake 还静默标 completed）。① `nas-train-runner/agent.md` 重写（执行置顶、删上游散文灌入改用 `{{ inputs.output_dir }}`、反伪造、末尾 python 从真 search.jsonl 计数输出自校验 JSON）；② `nas-select/agent.md` 同样去诱骗+反伪造；③ `nas-hp-search.yaml` runner 加 `output_schema`（`search_records≥1`，in-session `step.py:_parse_output` 确定性强制：散文/0 记录 → `output_schema_mismatch` → `node_failed`，不真跑过不了）。共享 agent 契约变更：须显式传 output_dir。验证脚手架（FAST/MOCK）剔除不进生产。E2E 两次通过（opencode+flash+脚手架绕 deepseek 慢）：runner JSON 过 output_schema、select 真选 top-3 + final_report。Commit: `<SHA 见 git log>`。详见 [release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。

## [2026-07-16] tars install skill 改名清理 + CLAUDE.md「TARS 是 SKILL」注记

CC 装出的 skill 名是陈旧 `orca`（tars 改名前装的残留），与命名约定（skill=`tars`）不符；且 install 不清旧 skill 目录名。`install_cmds.py:_install_skill` 加改名迁移清理（install 自动清陈旧 `skills/orca/`、`skills/teams/`，同 `command/orca` 清理 pattern，fail-soft）+ 修陈旧 docstring；`CLAUDE.md` 加「TARS 是 SKILL 不是 CLI」注记（skill 编排、驱动 `orca` CLI、不存在 `tars <wf>`）。重装 CC → 正名 `tars`，`orca doctor` `skill_install: PASS(cc,opencode)`；改名清理造陈旧目录实测命中。Commit: `<SHA 见 git log>`。详见 [release note](../releases/2026-07-16-nas-hp-search-enforce-and-tars-skill-cleanup.md)。

## [2026-07-16] nas-hp-search：轻量 NAS 超参搜索流水线（slim 5 节点）

新增 `workflows/nas-hp-search.yaml`（线性 `model_optimizer→train_script_gen→search_pipeline_gen→runner→select`）——重 7 节点 pipeline 的轻量版：① 新 slim folder-agent `elastic_optimizer`（只读 model + Elastic 速查 + 最小 supernet 模板，不展平/不读 optimize_rules，上下文从数十文件降到 3 文件）；② 新脚本化 `nas-select`（subprocess `nas-select-architecture` + 模板填空 `final_report.md` + 推 C5/C6，零 LLM，替代 22min evaluator）；③ 复用 `supernet-train-script` checklist 加 `[MAJOR] 28`（train_supernet.py 内联 `_push_chart()`，accumulate+全序列推、label/title 对齐 tail_metrics C3a/C3b 保 refresh-idempotent，无独立 viz 节点）；④ 复用 `nas-search-pipeline`/`nas-train-runner` 不改。节点名 `model_optimizer`（agent 指向 `elastic_optimizer`）对齐复用 agent body 的 `{{ model_optimizer.output }}` 硬契约（prompt+agent 互斥）。附带 `.gitignore` 修：`references/`→`/references/`（锚定根目录，避免误伤 folder-agent 的 skill 资源）。`tars validate` 0 error、5 agent 全 resolve；template 自测 diff=0；select_and_report 端到端 EXIT=0 SELECTED=3；code-reviewer impl+coverage 两轮 🔴 全修。Commit: `a5dd2cc`。详见 [release note](../releases/2026-07-16-nas-hp-search-slim.md)。

## [2026-07-16] in-session chart 守护 respawn —— `next` 路径补被杀后拉起

补 [in-session chart 接入](../releases/2026-07-16-in-session-chart.md) 的缺口：chart 守护**只在 bootstrap spawn 一次**，run 中途被杀（如 `pkill opencode` 误伤 detached 守护）后 `orca next` 不 respawn → 后续节点 `render_chart` 连不上 socket、chart 全丢（实测一次 run 0 chart）。本补丁：① `_chart_daemon_alive` 确定性 socket connect 探测（不靠进程名 grep）；② `_ensure_chart_daemon` 在 `next` 的 tape flock 临界区内 probe + 复用 `_spawn_chart_daemon` respawn；③ `_wait_for_sock` 从 `exists()` 加强为 connect 探（修 respawn 路径上 stale socket 假阳性）；④ 调用点守卫与 env 写对齐（`result.node is not None`，终态/no-marker 不 respawn）；⑤ spawn 失败降级 warn 不崩 next。+7 测试（含 SIGKILL→respawn→chart 落 tape 的 intent 级 e2e + 两个负向守卫测试）；158 in-session 测试 0 新回归（1 既有 list 测试隔离缺陷）；code-reviewer impl+coverage 两轮 0 🔴（🟡 全修：守卫/docstring/spawn 降级/负向测试）。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-16-in-session-chart-respawn.md)。

## [2026-07-16] in-session 路径接入 `orca.chart.render_chart`（per-run chart 守护 + run 级 env 文件 + 指针 source 行）

补 in-session skill 驱动路径的 chart 缺口：web/tars-run 路径下 ClaudeExecutor spawn 时一次性注入 `ORCA_*` env + 起 per-run ingestor（同进程）；in-session 路径下节点子代理由宿主 session（opencode/CC）派发不经 executor → env 无 `ORCA_*` 也无人起 ingestor → `render_chart` raise。本任务三件套补缺：① bootstrap detach 起 `_FlockSafeTape` 守护（跨进程 flock + 增量 disk max-seq 刷新，复用 `chart_ingestor` 协议零改动）；② `runs/<run_id>/orca_env.sh` per-node env 文件（5 var：4 chart + `ORCA_AGENT_RESOURCES`，folder-agent 资源定位缺口同补）；③ 节点 prompt 指针加 `source <env>` 行。守护 `_watch_terminal` 监听终态事件自退 + 6h TTL 兜底；partial-line race 防护（`last_size` 仅推进到最后 `\n`）。24 新测试（19 守护单测 + 5 集成：chart 落 tape / 并行不串台 / folder-agent + `$ORCA_AGENT_RESOURCES`）；710 in-session+chart+events+exec 测试 0 新回归；code-reviewer 两轮 0 🔴（R1 1 🔴 partial-line race + 5 🟡 全修；R2 0 🔴 0 🟡）。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-16-in-session-chart.md)。

## [2026-07-16] 后端命令 teams → tars 改名（品牌收口：skill=tars / 后端=tars / in-session=orca）

后端/运维命令 `teams`（install/run/serve/ps/validate/mcp/executor/list/logs/wait/resume）→ `tars`，与上一步 TARS skill rebrand 对齐——三套命名收口。改 `pyproject [project.scripts]` 入口 + `DEFAULT_BACKEND_CMD` 默认 + `validator` 保留字（`teams`→`tars`，防 wf 名撞命令）+ `commands.py` help/docstring + `teams_app` deprecated 别名保留（向后兼容）+ 用户面消息（orca epilog/doctor/skill 弃用警告）+ shipped 产物（cc_nudge.sh / SKILL.md / templates）+ `examples/mxint_analysis.yaml` 注释 + 测试 + SPEC live 段。`orca` in-session 命令不动；`ORCA_BACKEND_CMD` env 名不变（只改默认值）。重装后 `tars` 上 PATH、`teams` 退场（验：`which tars`✓ / `tars --help` 显示 tars / `orca --help` 指 tars）。768 单测 0 回归（+2 净增：pyproject 入口锁 + teams_app 别名锁）；code-reviewer 两轮 0 🔴（R1 🟡 examples 注释漏改 / R2 🟡 测试名实不符 + 🟢 别名锁，全修）。真机 `tars install/--help/list` 待 test-agent 验。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-16-teams-to-tars-rename.md)。

## [2026-07-15] TARS 品牌 rebrand —— skill 改名 orca→tars + TARS 描述（CLI 仍 orca）

用户面 = TARS：skill 名 `orca`→`tars`（`/tars`、description TARS 语气——触发「用 TARS 帮我 X / 用 TARS 做 Y」→ `orca list` 语义匹配 description → 命中唯一启动 / 多个则问（≤2 问）→ 抽 inputs → 派子代理 → `orca next` 循环到 done）。CLI/命令仍 `orca`（TARS 用 orca 引擎；orca.ts/cc_nudge 不动）。抽 `ENTRY_SKILL_NAME = "tars"` 常量单一真相源（`skill_cmds.py`，doctor `_scan_skill_install` + install re-export + 三处测试全经它，防目录名与 check 漂移）；SKILL.md body 命令引用全保 `orca`（仅 frontmatter name + 标题 + `<purpose>` 身份是 TARS）。SPEC §4.1/§8 措辞同步。176 单测 0 回归（+1 frontmatter name gate）；code-reviewer 两轮 0 🔴（test 轮 2 🟡 已修：install 断言改用常量 DRY + 补 frontmatter name 锁）。test-agent 真机待主 session 派。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-tars-skill-rebrand.md)。

## [2026-07-15] in-session v5 §8 step 6 —— teams install nga/cac 全套（CAC≡cc / NGA≡opencode）【spec v5 §8 全 step 收尾】

用户澄清 CAC ≡ Claude Code（`.claude`→`.cac`）、NGA ≡ opencode（`.opencode`→`.nga`），install 阶段两家族全套统一装（不只 skill）：cac 走 cc 家族（skill + nudge Stop-hook：`.cac/hooks/orca-nudge.sh` + `.cac/settings.json`）、nga 走 opencode 家族（skill + plugin `orca.ts` idle nudge + `opencode.json` 声明指 `.nga`）。`run_install` 按家族路由（opencode+nga / cc+cac，显式 `elif` + 末尾 fail-loud `AssertionError`），cc/opencode 零回归（byte-identical）；泛化 `_opencode_plugin_decl` project-scope 路径用 `hr.root.name`（去硬编码 `.opencode`，opencode 旧值不变）。SPEC §4.3/§4.4/§11/§9#1 同步升级为「家族全套」。164 单测 0 回归（+4 净增：cac/nga 全套 + nga project-scope 泛化闸门 + cac/nga 幂等）；code-reviewer 两轮 0 🔴（Rule 7 surface 一处镜像测试冗余）。真机加载（CAC/NGA 是否真读 `.cac`/`.nga` + nudge/plugin 生效）留 §9#1 跨平台用户侧。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-step6-nga-cac-install.md)。

## [2026-07-15] in-session 批量闭环 FU-2 + 3a + FU-3 —— status 活跃+结构化 / doctor 删 entry_hook dead / skill 补 error_kind

三个独立低复杂度 follow-up 合并单 commit。FU-3：`orca status` 无参对齐 SPEC §2.1/§2.3——只列活跃 run（marker `runs/orca-*.json`）+ 结构化 `{run_id,node,status,last_next_at,elapsed}`（时间字段取 tape `Event.timestamp` 末事件，**非** RunState 零时间字段 / **非** marker mtime；`elapsed` 用 `time.time()` 同基非 monotonic，spec-reviewer 时间基纠正）。FU-2：doctor 删 entry_hook check（step 4 整删 transform 后 PROBE_ENTRY 心跳永不再写，dead）+ 连带死代码（`PROBE_ENTRY_NAME` 常量 / `_read_probe` 死变量 / 报告路径行）；5→4 checks，advance_hook 保留（idle hook 仍写）。3a：SKILL.md 失败处理补 `error_kind` 一句（5b 信封加字段后），同步已装副本。132 单测 0 回归；code-reviewer 两轮 0 🔴（时间基钉死 / marker skip 路径 / 非 empty 人类可读分支全补测试）。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-batch-fu2-3a-fu3.md)。

## [2026-07-15] in-session v5 §8 step 3b —— catalog 物理迁 orca/compile/catalog.py（依赖铁律归位）

catalog（workflow 发现/加载/描述）从 `iface/mcp/catalog.py` 物理迁到 `orca/compile/catalog.py`（`git mv`，内容字节不变）：它是 compile 层关注却坐在 iface = 依赖方向越位，迁入 compile 与 parser/validator 同层方向正。7 处 lazy import → 顶层 **module import** `from orca.compile import catalog` + `catalog.<fn>()`（偏离原计划裸函数 import 的正当修正：commands.py/in_session 各有同名 typer 命令 `list_workflows`，裸 import 触发 RecursionError；且 `mock.patch("...catalog.list_workflows")` 守门单一真相源契约需 module 属性动态查找才 bite——code-reviewer 两轮实证）。9 处 mock target 同步（test_catalog 2 + 跨文件 7）；守门 grep `iface/mcp/catalog` = 0；1123 passed 0 回归（7 failed 全 pre-existing env-blocked，stash 对比复现）。test-agent 真机三路 list 一致待跑。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-step3b-catalog-relocate.md)。

## [2026-07-15] in-session v5 §8 step 5b —— daemon batch emit + in-session 错误信封统一（×2）

daemon `next()` 逐条 emit → `emit_batch`（注释「反例 A 消除」原为假，SIGTERM 落批内留半截 tape → resume state_corrupt，铁律 12）；in-session 失败信封统一（daemon + cli，MCP 出 scope：8 tool 全用 phase-11 ErrorKind 轴）：抽 `_step_io` helper（`apply_step_result` 吸收 `_emits_to_event_datas` + `fail_in_session`），daemon `_fail` 的 isinstance 塌缩消除，改读 `exc.error_kind`。信封加 `error_kind` 字段（tape `data.kind` 不变，两者同值——B4/B7 字段名陷阱）。新建 `test_daemon.py`（InSessionDaemon 零覆盖补齐 5 测试：成功路径 / batch emit spy / 畸形 output→kind+error_kind / 反向无 `in_session_error` / 终态+非终态幂等）；拆分误并入 malformed 的 render_error 测试。SPEC §7.5 ×3→×2 + MCP 排除；§2.3 信封加 error_kind。348 单测 0 回归；code-reviewer 两轮 0 BLOCKER（Round 1 M1 经 git show 核验非回归 disputed + m1/m2 fixed；Round 2 m1/m2 fixed + m3 登记）。跨阶段 debt：tape `workflow_failed.data.kind` 是 ErrorKind/error_kind 两值集共享字段，登记 CURRENT。test-agent 真机 E2E 待跑。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-step5b-daemon-error-envelope.md)。

## [2026-07-15] in-session FU-1 —— orca stop/open 加 --run-id option（命令族统一，套 DEFECT-2 e763e9e）

`stop`/`open` 都只有位置参数、缺 `--run-id`，但 SKILL.md + SPEC §2.1 教 `--run-id` → 主 session 照跑报 `No such option: --run-id`（test-agent 真机复现）。修：抽 `_merge_run_id` helper（status/stop/open 三处合流 DRY，防漂移）；stop 位置参数必填→可选 + None 守卫（保 missing fail loud exit 2）；open 加 option（None=活跃 run 默认）；status 既有内联合流替换为调 helper。125 单测 passed 0 回归（+15 FU-1）；code-reviewer 两轮 0 BLOCKER / 0 MAJOR。test-agent 真机 E2E 待跑。顺带回填 step 5a 文档 SHA 占位符为 `bce29f8`。Commit: `<本 commit，SHA 见 git log>`。详见 [release note](../releases/2026-07-15-in-session-fu1-stop-open-runid.md)。

## [2026-07-15] in-session v5 §8 step 5a —— 删 setup phase 全栈 + MCP migration note（A2 gate 保留）

删 setup phase 全栈（路径 B 死代码）：schema `Workflow.setup` / compile `_check_setup_phase_constraints` + jinja valid_root 去 setup / exec `RunContext.setup` + render setup ns / run orchestrator setup_ns / iface(mcp/web/cli) 全层；MCP breaking：删 `tool_get_agent_prompt` + `tool_start_workflow` 去 `setup_outputs`（migration note 兜底旧客户端）。m13 fail loud 靠 pydantic `extra="forbid"`（零新代码）。**A2 铁律**：execute phase gate 校验（`_check_execute_phase_no_gate_tools` / `_INTERRUPT_TOOL_NAMES` / `_check_no_interrupt_tools`）保留不删，唯一覆盖测试从 `test_setup_phase.py` 搬迁到 `tests/compile/test_validator.py`（防丢）。契约 doc 同步（setup 删后旧陈述变假）。1526 单测 passed 0 回归（8 failed 全 pre-existing env-blocked，stash 对比复现）；test-agent 真机 E2E 全绿（--help/list 契约 / 3 节点 bootstrap→next→completed / setup YAML fail loud exit 1 / A2 gate fail loud / doctor ok / MCP 8 工具）；code-reviewer 两轮 0 BLOCKER / 0 MAJOR。Commit: `bce29f8`。详见 [release note](../releases/2026-07-15-in-session-step5a-setup-removal.md)。

## [2026-07-15] in-session E2E defects 修复 + v5 §8 step 4（orca.ts transform 整删）

E2E 跑发现 2 defect 各独立 commit 修复：① cc_nudge.sh 缺 jq 时静默失败（fail-loud 违规）改用 python3 + marker 损坏时 stderr/exit 2。② `orca status` 加 `--run-id` option（与 SKILL.md/spec 一致；位置参数保留兼容；异值冲突 fail loud）。随后做 v5 §8 step 4 opencode 收尾：删 orca.ts transform marker 派发入口 + 9 个死代码 helper + `_constants.py`，**保留 idle nudge hook**（§4.4，opencode nudge 载体）；review 捕获 BLOCKER（test_web_default_and_open 跨文件漏扫 8 测试）+ MAJOR（advanceCount/lastAdvanceRunId 死代码）全闭环。spec 决策 #12 + 验收标准措辞修正对齐。185 affected passed 0 回归。Commits: `2de50e3`（DEFECT-1）+ `e763e9e`（DEFECT-2）+ `52cc9f3`（step 4）。详见 [release note](../releases/2026-07-15-in-session-defects-and-step4.md)。

## [2026-07-14] in-session v5 §8 step 2b —— 入口切 skill + list inputs_schema + doctor skill_install + 删 start/cc_hooks/command + nudge hook

实施 SPEC v5 §8 step 2b 全 7 项：in-session 入口统一切到 orca skill（三步指导：list→抽 inputs→<wf>+自调 next），删旧 command/start/cc_hooks 入口，nudge hook 提醒主 session 推进（**绝不自动推进**，B 路径铁律）。① 建 orca skill（CI 守门：三步指导 + 禁业务关键词 + 禁 teams 命令）。② `orca list` 返 `{workflows:[{name,description,inputs_schema}]}`（无 has_setup，无 describe）。③ doctor 加 skill_install 硬检查（A6）+ hook 心跳可选 + `hard` 字段定 ok。④ 禁用 orca.ts transform dispatch（early return，文件不整删——idle hook 保为 nudge 载体）。⑤ 删 4 个 command 模板。⑥ 删 start + cc_hooks（A 路径退场）。⑦ nudge（A5 修正入本步）：opencode idle hook 改提醒模式（listActiveRuns→节流→promptAsync 注入，不 spawn next）；CC 新 Stop hook（cc_nudge.sh，零反引号 decision:block）+ `teams install --target cc` 合并 settings.json。install 重构四前端（cc/opencode/cac/nga/all）装所有随包 skill，平台常量抽 skill_cmds 单一源（DRY/OCP）。208 affected passed 0 回归；code-reviewer 两轮（2 BLOCKER + 关键 MAJOR）全闭环。Commits: `e2bd989`（1-6）+ `4b90508`（7 nudge）。详见 [release note](../releases/2026-07-14-in-session-v5-step2b.md)。

## [2026-07-14] in-session v3 §8 step 1 —— orca 接口打包 + 14 命令归宿 + teams 变量化 + marker 精简

实施 SPEC v3 §8 step 1：① `orca` 顶层 = in-session 7 命令（`list/<wf>/next/status/stop/open/doctor`），删 `in-session` 子命令层；`bootstrap` → `orca <wf>` 语法糖（单一实现，hidden bootstrap + rewrite，非双入口）。② 14 后端命令归 `teams` entry point（`run/serve/ps/...`），`list`/`open` 共享单一实现。③ `ORCA_BACKEND_CMD` env 变量化（默认 teams）。④ marker 精简到 `{run_id, model, no_output_count}`（删 desync 向量 tape_path/yaml/session_id/owner），`marker_path(rundir, run_id)` O(1) 直定位（删扫描），yaml 从 tape.workflow_started.data.yaml_path 派生（唯一真相源）。⑤ 重复 bootstrap 同 wf → fail loud（m12，well-known `.orca-bootstrap.lock` serialize 防 TOCTOU，review B1 闭环）。⑥ 保留字黑名单（§2.2 MS1，compile fail loud）。⑦ B1 同 commit 改全活调用点（cli.py 驱动协议 / orca.ts spawn+argv / cc_hooks / command 模板）。⑧ `_inputs_from_tape` 首调噪声修复。in-session 134 passed（+37 新增），CLI 后端 + compile + orchestrator 281 passed 0 回归；code-reviewer 1 BLOCKER（并发 TOCTOU）+ 4 MAJOR + 5 MINOR 全闭环。详见 [release note](../releases/2026-07-14-in-session-v3-step1.md)。Commit: `d14cde5`。

## [2026-07-09] in-session 三件打磨（outputs 求值 + inputs 从 tape 恢复 + prompt 收紧）

model-driven advance 补丁（`4b3a4d6`）之上的 surgical polish：① `_final_outputs` fail-loud stub → `render_template` 求 `wf.outputs`（与 `Orchestrator._evaluate_outputs` 同源，渲染错 fail loud `ERR_RENDER_ERROR`，in-session 专用不动正常路径）；② `advance_step` 改 `Orchestrator._inputs_from_tape` 恢复 inputs（模型不必每步重传 `--inputs`，修非 entry 节点 `{{ inputs.* }}` 渲染隐患）；③ `run.md`/`_drive_protocol` 加「不许自己 Read 节点 .md」+ 修 stale 自动推进 + `bootstrap --format prompt` 补驱动协议（CURRENT 遗留 #2）。COMMAND→MCP 不换（解决不了实际失败模式 + 重复 phase-10）。in_session 96 passed（+3）；tests/run+iface 1007 passed 0 回归；code-reviewer PASS。Commit: `f86df86`。详见 [release note](../releases/2026-07-09-in-session-outputs-inputs-prompt-polish.md) + [计划](../plans/2026-07-09-in-session-outputs-inputs-prompt-polish.md)。

## [2026-07-08] Web attach + web 默认 + in-session open —— COMPLETE（e2e PASS，让 web 监控任意单 run）

Web v2 只认 in-process run 的 gap 补齐：**X** web 按 tape 路径 attach（read-only `tape_reader` + tail-follow + `RunView` 双 handle 单 registry）+ seq-windowed `/meta`/`/events` huge 模式 perf（103MB fixture `/meta` 5.2ms / `tail=500` 41.5ms）+ 安全 `relative_to` 三重守卫；**Y** `orca run` 默认起 web（浏览器自动开 + WS client-count 驱动 auto-exit）+ `orca open` / `/orca open` 打开任意 run（含 `--background` / in-session，observe-only）。SDD 全流程：SPEC rev2 spec-review PASS → Step1 `69e5c7b` → Step2 `fe81e42` → 3 e2e defect 修 `58947fd` → test-coverage-e2e 真跑 PASS（live P99=250ms / 安全 5+allowlist / §7 失败路径全过 / 铁律 grep）。pytest 674 + npm 262 绿。详见 [release note](../releases/2026-07-08-web-attach.md) + [SPEC](../specs/web-attach-and-default-spec.md)。Follow-up：`/orca open` fork-and-return、detached serve PID 管理。

## [2026-07-08] Web attach 3 e2e 缺陷修复（AC9 / AC11 / AC5 负向）

修 `test-coverage-e2e` 发现的 3 个真实缺陷：AC9 非 wf-started 首完整行被误判 running（upfront reject + 显式 probe_validated 参数替换 offset 推断 bypass + follow 立即拒 partial→complete 非 wf-started）；AC11 AskGate 忽略 writable=false（抽共享 gate-writable helper）；AC5 负向 活跃 WS 不挡 auto-exit（WebServer.active_ws_count + _wait_ws_autoexit count==0 AND window）。+5 后端 +3 前端测试；87 passed + 262 npm 绿。Commit: `58947fd`。routes 层 HTTP 403 端到端回归守门补 `test_attach_routes.py`（+2 TestClient 用例，code-review 🟡#2 闭环）。Commit: `3f7aa00`。详见 SPEC `docs/specs/web-attach-and-default-spec.md` §6.7/§8 AC9/AC11/AC5。

---

## [2026-07-08] Web attach Step2（Y）—— `orca run` web 默认 + `orca open` + `/orca open` slash

按 SPEC `web-attach-and-default-spec.md` rev2 §4/§5/§8 AC5-7/11 实现 Web attach Step2：`orca run <wf>` 默认走 web（probe 7428 → 复用 `POST /api/run` / 否则起新 in-process serve + RunManager.start_run in-process + `webbrowser.open` + WS 驱动 auto-exit（`last_ws_activity_at` env `ORCA_WEB_AUTOEXIT_SECONDS`）+ Ctrl-C 路径闭环）；`orca open <id>` CLI（probe / spawn detached serve / attach / browser）；`/orca open <id>` slash 走新 `spawnTopLevelCli`（plugin 哑传输 grep 守门 + 三元路由 signature-contract）。`--tui` opt-in 保留旧 Textual TUI；`--background` 不变。**code-reviewer 4 BLOCKER + 6 MAJOR + 3 MINOR 全闭环**（asyncio CancelledError / 双 shutdown / spawn FileNotFoundError / yaml_path resolve / --stay warning / routing signature）。674 passed / 30 skipped（+15 新增）+ npm 259 绿；铁律 grep 全过。Web attach feature COMPLETE（Step1 + Step2）。Commit: `fe81e42`。详见 [release note](../releases/2026-07-08-web-attach-step2.md) + [SPEC §4/§5](../specs/web-attach-and-default-spec.md)。

---

## [2026-07-08] Web attach Step1（X + perf）—— attach by tape path + huge-mode + perf

按 SPEC `web-attach-and-default-spec.md` rev2 §2/§3/§6/§8 实现：后端 `POST /api/runs/attach` + `RunView` ABC 双 handle（InProcess/Attached）+ read-only tail-follow（`EventBus.relay` fan-out only）+ 安全三重守卫（lstat + relative_to + open+fd-re-stat 防 TOCTOU）+ `GET /meta` huge 模式服务端 fold 派生 overview + `GET /events?since/limit/tail` 窗口化 + `GET /api/health`；前端 huge-mode（serverOverview slice + tail + 增量 prepend + load full）+ attached run gate observe-only。perf fast-path：`_scan_meta_overview` 单遍扫 + bulk-type substring skip + regex seq 提取（60k fixture ~150ms vs naive ~8700ms）+ `tail_events` 反向扫 O(tail)。**code-reviewer 2 BLOCKER + 6 MAJOR + 5 MINOR 全闭环**。1863 passed / 2 skipped（perf 默认 skip）。Commit: `69e5c7b`。详见 [release note](../releases/2026-07-08-web-attach-step1.md) + [SPEC §2/§3/§6/§8](../specs/web-attach-and-default-spec.md)。

---

## [2026-07-08] orca install —— 统一安装入口（全局默认 + 合并 skill/in-session）

收口碎片化安装（`pip` → `skill install` → `in-session start` 三步、两种 scope）为单条 `orca install [--target claude|opencode|all] [--scope user|project]`（全局默认）。**Step 0 spike 钉死承重事实**：opencode 1.14.22 无 `plugins/` 目录自动发现，plugin 加载**必须** `opencode.json` `"plugin":[<path>]` 声明（项目相对 / 用户绝对）——修掉既有「光丢文件不声明」缺口（`start` 之前只写两文件不碰 opencode.json，无加载 e2e 守门）。`skill install` 降为弃用别名（warn+委托）；`in-session start` 收窄为 CC-only run bootstrap（opencode 路运行时 `bootstrap` 自举）。code-reviewer 0 BLOCKER + 4🟡/3🟢 全闭环；`tests/iface` 689 passed + 新增零业务逻辑守门。详见 [release note](../releases/2026-07-08-unified-install.md) + [plan](../plans/2026-07-08-unified-install.md)。

## [2026-07-08] in-session compact prompt —— 文件交付 + 缺字段干净 fail loud（e2e PASS）

in-session shell 的节点 prompt 交付从"整段渲染文本注入主 session"改为**compact**：Orca 把渲染后 prompt 落盘到 `<rundir>/<run_id>/prompts/<node>.md`，主 session 只收一句 host-facing **指针**（"用 task 派子代理，完整指令已写入 `<path>`，先 Read 再执行"），子代理从文件读完整指令——主 session 上下文不再随节点数膨胀。两种 agent 形态（`agent:<name>` md 引用 / inline `prompt:`）渲染无差别（compile 已扁平化进 `node.prompt`）；plugin 零改动（仍读 `.prompt`）。**顺手修既有脏崩溃 bug**：`output_schema` 缺字段 / 畸形 schema / 下游 render 引用缺失字段，原本 `ExecError`/`SchemaError` 逃逸 → 无 `workflow_failed`、不清 marker、tape 悬挂、下次卡死；现 `_parse_output` 加 jsonschema 校验、`_render_or_fail` 包错 → 走既有干净 taxonomy（`output_schema_mismatch` / `render_error`）。不接 LLM `validator`——主 session 自己当判官。计划 [2026-07-08-in-session-compact-prompt](../plans/2026-07-08-in-session-compact-prompt.md)；SPEC `in-session-shell-design-draft.md` §2.1/§2.5 回填。code-reviewer 1 🔴（SchemaError 漏网）+ 🟡 全闭环。**顺手消既有债**：`InSessionError` 加 `error_kind` 显式字段 + `ERR_*` 常量，`_classify_in_session_error` 改直读字段（取代脆弱的消息子串匹配，类型安全）。92 in-session + 851 跨模块测试绿；e2e `/tmp/orca-compact-exp/repro.sh` PASS。

## [2026-07-08] Web Shell v2 —— 推倒重写 COMPLETE（单 tape + AH 风格，e2e PASS）

旧 Web 很差 → 按 SDD（SPEC→spec-review→clean-code→test-e2e）推倒重写前端：单 tape 唯一真相 + 单 Zustand store + codegen + AH 风格渲染（markdown/流式 RAF/工具折叠/DiffView/Charts/LogStream liveness/Gate/DAG）。后端 B1/B2（opencode translator lossless：reasoning/step_start/reasoning_tokens/unknown_event + `--thinking` 开关，EventType 37→39）。test-coverage-e2e 真跑（opencode+deepseek `--thinking` + Playwright + 全 39 类型 fixture）3 Must 全 PASS，铁律 AC 全过，npm 249 + py web 64 测试绿。Commits：c3a738f + 84a2645 + 5a26957 + 01af451 + 7d76934 + 60539b8。详见 [release note](../releases/2026-07-08-web-shell-v2.md) + [SPEC](../specs/web-shell-v2-spec.md)。Follow-up：demo_task 真 run 挂起（后端 opencode 冷启动，非前端）、DiffView LCS、Conv chunk 再拆、LogStream auto-scroll 真跑触发。

## [2026-07-08] Web Shell v2 Chunk D（completion + polish + bundle split）

完成前端**所有剩余项**（D1-D7）+ 86% bundle 减重（initial 2,035 KB → 290 KB / gzip 93.65 KB）。
D3 image URL rewrite（backend `/api/runs/<id>/assets/<path>` + 前端 `rewriteImageSrc` +
path traversal / symlink 守卫）/ D4 resume-fallback watchdog + `resume_ok` 协议 ack 帧（idle
场景不误触发全量重拉）/ D5 view 层 lazy 切分（ConversationView / ChartsView / WorkflowGraph
独立 chunk）/ D7 StatusLine 折叠修正（Chunk B YAGNI 偏离）+ e2e Gate / lazy DAG / markdown
渲染 / image rewrite / ws-fallback / dropBuffer 时序断言。1 BLOCKER + 3 MAJOR + 5 MINOR
全闭环。249 npm tests + 64 backend tests 双绿。**前端实现 COMPLETE，ready for e2e。**
详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-d-completion-polish.md)。

## [2026-07-08] Web Shell v2 Chunk C（ChartsView + LogStream + TopBar + AgentsRail + useElapsedTick）

按 SPEC §5.1/§5.2/§5.4/§5.5/§5.6/§5.7 + §0 D5/D9 实现 6 个面板完整渲染 + 单一共享
elapsed tick。ChartsView（IntersectionObserver 懒挂 + 响应式 grid + scatter→bubble 扩
展 + selectCharts 唯一去重真相出口）/ LogStream（react-window v2 `scrollToRow` auto-scroll，
predictable-over-magic 状态机）/ TopBar（D5 elapsed live→snap，failed/cancelled 也 snap，
读 tape 末条 workflow_* 事件 ts）/ AgentsRail（per-agent elapsed + D9 stall + 单一 timer
断言）/ useElapsedTick（singleton useSyncExternalStore，N consumer = 1 setInterval）。
53 新测（170→223）全绿，build 绿。闭环 review 1 BLOCKER + 4 MAJOR + 6 MINOR 全闭环。
Commit: `01af451`。详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-c-charts-log-tb-rail-tick.md)。

## [2026-07-08] Web Shell v2 Chunk B（ConversationView 全渲染）—— markdown + 折叠 + ▎ IFF + 工具展开 + 虚拟化

按 SPEC §5.3 实现中栏「会话」页签完整渲染。markdown stack（react-markdown + gfm +
math + katex + prism）+ per-EventType 全表（prompt/thinking/message/tool/dialog/
chart/custom/error/divider/status/unknown）+ 折叠规则（默认折叠/成组/永不折叠）+
▎ IFF（selectStreamingCursor：finished tape 必 false）+ smart arg（bash/read/write/
render_chart）+ DiffView/FileContentView（轻量自建）+ react-window v2 虚拟化（>500 条，
函数式 rowHeight 按 kind 估高）。闭环 review 1 BLOCKER + 4 MAJOR + 4 MINOR/NIT。
93 新前端测试（含 EventType 穷尽表驱动 + B1 回归 + 折叠 DOM oracle），170 passed。
Commit: `5a26957`。详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-b-conversation.md)。

## [2026-07-08] Web Shell v2 Chunk A（foundation）—— codegen + 单 store fold + selectors + RAF 流式 + WS resume + 删除过期

按 SPEC §0 D1/D2/D6/D7/D8 + §3.1/§3.3/§4/§8/§10 实现前端基础层。新 `scripts/gen_events_ts.py`
（D1 codegen）+ pytest drift guard 根治 21↔39 漂移；删 Replay/multi-run/NodeDetail/formatLogLine
全部（§8 无兼容层）；单 Zustand store = fold(tape)，seq 升序 + refold（D7 序无关）；纯 selector
（selectAgents/Conversation/Charts/Log）；`useStreamingText` RAF 批处理 + 多 session sync-flush；
WS reconnect resume by seq（D6）+ server-side `_handle_resume`（重放 tape.replay(since_seq=N)）。
3-column 占位布局（AgentsRail/[会话|图表]/LogStream）。77 前端测试 + 55 后端测试全绿。
详见 [release note](releases/2026-07-08-web-shell-v2-chunk-a-foundation.md)。

---

## 模板

```
## [日期] 阶段名 —— 一句话描述
- commit: <SHA>
- 详情：[release note](releases/<date>-<name>.md)
```

---

## [2026-07-08] in-session shell v8.1 —— 修 5 bug + 签名契约测试（防 builder 回退）

按 SPEC v8 + e2e `/tmp/orca-e2e-v8/` 实证，修 shipped plugin 5 个真 bug（builder 上一轮从已验证
spike 回退导致）：A transform hook 签名（单参 → 两参 `(input, out)`）/ B event hook payload 包装
（裸 event → `input?.event ?? input`）/ F SDK message-fetch 非 list 改 REST fetch / G bootstrap+next
返 prompt 未 prepend Task-tool 指令（cli.py 端补，DRY 单一常量）/ E plugin 不透传 --model（从
info.model 动态抽，非 CLI 默认）。加 6 签名契约测试（断言 shipped 模板 transform/event/fetch/model
四处的代码形态 == spike 实证形态 + bootstrap prompt startswith Task 指令）—— 防再回退，根因教训
「TS 纯单测验不出运行时签名 bug」写进测试注释。baseline 83 → after 89 全绿，0 回归。守门 grep
（8 禁词）clean。Commit: `8bea9dd`。详见 [release note](../releases/2026-07-08-in-session-shell-v8.1-bugfixes.md)。

---

## [2026-07-07] web-shell-v2 B1/B2 —— opencode translator lossless + reasoning exposure

按 SPEC §3.2 + §11 step1 实现 web-v2 后端硬前置：opencode translator lossless（reasoning→agent_thinking / step_start→agent_step_started / step_finish 加 reasoning_tokens / 未知→unknown_event）+ EventType 加 2 项 + 全消费者 grep 审计（reducer no-op、LogStream/EventVISIBILITY/AgentHistory/summary 加 arm）+ B2 supports_reasoning opt-in + reasoning_flags_env env 注入（ORCA_OPENCODE_REASONING_FLAGS，默认 off）+ fixture 扩到 9 行。1758 passed / 0 新回归。Commit: `c3a738f`。详见 [release note](../releases/2026-07-07-web-b1-b2-translator-lossless.md)。

---

## [2026-07-07] in-session shell v8 —— 入口换 messages.transform + doctor 自检 + start 落 opencode 模板

按 SPEC v8（§2.6/§2.6.1/§2.6.2/§2.7）实现 v7→v8 增量。v7 CLI 大脑零改；本轮重写 plugin
模板（flat hooks + ctx.client + Bun.spawnSync + experimental.chat.messages.transform 入口，
spike 实证 v7 的 command.execute.before 在 opencode 1.14.22 不触发）、加 `orca in-session doctor`
3 项自检、统一 `/orca <sub>` 命令、start 落 .opencode/ 模板；CLI status 加 --json flag / stop
加 --owner（MAJOR-1/2 闭环），plugin spawnCli fail loud（MAJOR-3 闭环）。52 新测全绿
（31→83），全 unit 1775/1776（唯一 fail 预存 B-8）。
- commit: `56083c1`
- 详情：[release note](../releases/2026-07-07-in-session-shell-v8.md) + [SPEC](../specs/in-session-shell-design-draft.md) v8

## [2026-07-07] in-session shell v7 —— 薄 CLI 唯一大脑 + plugin/hook 哑传输
按 SPEC v7 + ADR v3 实现：CLI `bootstrap/next/stop/status/start` 唯一大脑（per-call flock
+ `Tape.append_batch` 单次 write 原子化 B1 + `--output` 空串 normalize B2 + 失败 taxonomy F6
+ 合规计数 F11 + marker RMW 在 flock 临界区内 N2）；plugin / CC hook = 哑传输（零业务逻辑，
grep 守门）；daemon 降级无头 CI。43 新测全绿，子集 1591 passed / 0 回归。
- commit: `6cd430c`
- 详情：[release note](../releases/2026-07-07-in-session-shell-v7.md)

## [2026-07-07] executor CLI 扩展 —— 命令唯一真相源 + spawn 参数全可改 —— `orca executor show` 打印完整生效 argv + 每字段来源（env/项目/用户/default）；`set --binary/--flags/--prompt-channel/--scope` 三维可改 + 项目/用户两层 config；接通 phase-14 遗留的 `resolve_flags` 死通道，新增 `resolve_prompt_channel`
- commit: `f4b10da`
- 详情：[release note](../releases/2026-07-07-executor-cli-extend.md)

## [2026-07-07] create-workflow skill + orca skill install + headless benchmark —— 通用 workflow 生成/转换 skill（吃描述或既有素材 → 归一化 DAG → Orca YAML+agent md，强制 orca validate 闭环），显式装 CC+opencode 两边；16 case 公平 headless benchmark + harness，评测闭环从 8/16 → 16/16，抽象 H1-H7 通用规则
- commit: `09fd7a8`
- 详情：[release note](../releases/2026-07-07-create-workflow-skill.md)

<!-- 新条目加在这里（本行下方）-->

## [2026-07-07] in-session shell（hook 驱动，宿主主 session 执行 workflow）—— 第四种执行驱动模式：宿主（opencode/CC）主 session 用自带 subagent 跑每个节点，Orca daemon 独占 tape + `observe`/`next` 单一接口 + `session.idle`/Stop hook 自动推进（立项、CCW 一致）。纯增量（drive_loop/from_tape/三壳零改），daemon 经 `advance_step` 原子决策、flock 独占 + 半写恢复 + 仅本地 FS（铁律 1 扩展走 ADR）。opencode serve 模式端到端验证：3 节点 `completed`、tape 事件序列与 `orca run` 逐 seq 对齐、并发两 run 隔离。v1：opencode serve + CC、仅 agent 节点（parallel/foreach/gate fail loud 走 TUI/Web）。
- commit: <待填>
- 详情：[release note](../releases/2026-07-07-in-session-shell.md)

## [2026-07-07] phase-16 —— AgentHistory 单流重构（CC 风格 inline + 工具配对折叠）
AgentHistory 从「两区」（RichLog 摘要 + 独立 detail 面板）重构为**单条 RichLog inline 流**：tool_call+tool_result 按 `tool_call_id` 配对成一条 entry（就地升级保 seq/位置，避 `_selected_seq` dangling）；message bold+主题色 / thinking dim italic / tool `✓/…/✗` icon 视觉分级；Enter 全量 reflow（detail 内联）。删 `#agent-history-detail*` DOM（铁律 #7 无兼容路径）。reducer fold 顺序无关（`_pending_results` 缓冲）。28 单测 + 3 真 tape boot smoke + 1 phase-12 e2e 断言回填；mxint report_painter 79 events fold 30.9ms（< 300ms SPEC §7 标准）。详见 [release note](releases/2026-07-07-phase-16-agent-history-single-stream.md)。

## [2026-07-07] TUI bugfix 批次 A —— layout + AgentHistory 三体感 bug
- layout：NodeDetail `display:none`（修右侧栏全黑：原 `height:0+offset` 不移出布局流，把 `#right-pane` 挤到 width=1）+ AgentsList `height:1fr`（修左栏 auto-size 截断）。
- A.1 Enter 无选中时默认作用于最后一条（修「Enter 没反应」）；A.2 移除死键 `c`（App + NodeDetail 两处，图表统一走 `C`）；A.3 `#agent-history-detail` 包 VerticalScroll（修长 report 截断）。
- code-reviewer 回改：删 NodeDetail 残留 `c` 绑定（接口统一性）+ docstring + 空 entries 显式测试。
- 详情：[release note](releases/2026-07-07-tui-bugfix-batch-a.md)。

## [2026-07-07] setup_outputs 注入 runtime context（phase-10 🔴 技术债回填）
MCP `start_workflow(setup_outputs=...)` 真注入：校验后穿透 RunManager.start_run → _run_with_sem → Orchestrator.__init__ 包成 `{agent: {"output": raw}}` 存 RunContext.setup → render 暴露 `{{ setup.<agent>.output.<field> }}`；_make_ctx 透传 setup。resume + setup phase → fail loud（边界声明）。code-reviewer 🔴 修复：`with_locals` 改用 `dataclasses.replace`（原手工列字段漏传 setup，foreach body 引用 `{{ setup.* }}` 静默拿空 dict）+ 补 foreach+setup 回归测试。E2E setup workflow 强化（deploy 真消费 setup 变量）。1688 passed / 0 回归。详见 [release note](releases/2026-07-07-setup-outputs-injection.md)。

## [2026-07-07] CLI `list` 与 MCP `list_workflows` 统一（catalog 同源）
CLI `list` 子命令委托 MCP 同源的 `catalog.list_workflows()`（按 `wf.name` 扫 `./workflows` + `~/.orca/workflows`，first-wins），删旧 `--dir` 扫 `./examples` 按文件名逻辑（接口统一铁律：全量替换）。CLI 与 MCP 现在看到完全一致的 workflow 列表。详见 [release note](releases/2026-07-07-cli-list-mcp-unify.md)。

## [2026-07-07] phase-10 MCP v4（9 工具 + setup/execute 分相 + Result 信封）
server.py 重写：6 旧工具（含 resolve_gate）→ 9 v4 工具（Discovery 4 + Lifecycle 3 + History 2）；setup/execute 分相（workflow.setup 字段 + compile validator execute phase 拦截 ask_user/gate + setup phase 结构约束）；三重杠杆防跳过 setup；Result 信封（kind 是 ErrorKind 值，无 layer）；新增 catalog / setup_phase / agent_catalog / tape_index 模块。Commit: df563f4。详见 [release note](releases/2026-07-07-phase-10-mcp-v4.md)。

## [2026-07-07] TUI v2 review remediation + 批 1 backend（Status.blocked + projections.py）
- 修 commit 5562e5e 回归（j/k hoist 后 down/up 无绑定，Enter 展开非末条 entry 失效）：
  App 级 BINDINGS 加 `down`/`up`（`priority=True` 覆盖 RichLog scroll）+ 3 pilot 测试。
- 批 1（ADR §4.3/§4.3.1）：Status Literal 加 `blocked`；`orca/run/projections.py` 单一
  派生算法源（node_status / node_usage / node_session_ids / node_iter），apply_event
  扩展 blocked fold（gate/interrupt 同源），TUI 删独立 fold 副本（`_node_session_ids` /
  `_per_node_last_usage_seq`）全部改调 projections（DRY）；`agents_list.py` 类型收紧
  Status + 删 `== "failed"` 字面量比较（P4）；AST 守门（`test_status_literal.py`）。
- 1596 passed / 0 回归（baseline 1558 + 38 新增）。
- commit: 见 `git log`（commit message 末尾含 Claude+Happy co-author）。
- 详情：[release note](releases/2026-07-07-tui-v2-review-batch1-projections.md)。

## [2026-07-07] phase-11-process-lifecycle —— 子进程生命周期管理（ProcessRegistry DI + 进程组 cancel + 退出码 5 档）
新增 `orca/exec/registry.py`（ProcessRegistry DI + 三段式 cancel SIGTERM→SIGKILL→cleanup + 平台分支 POSIX killpg/Windows CTRL_BREAK）+ `orca/iface/exit_codes.py`（ExitCode 5 档 0/1/2/3/130 + `exit_for_terminal_status` 纯函数派生）；runner.py / script.py 接入 `start_new_session=True` 进程组隔离（推翻 phase-3 §2.5 旧决策）+ registry.acquire/release；orchestrator.py 加 `shutdown()` 方法（不动 phase-11-error except 链）；run/__main__.py SIGTERM handler 只设 `threading.Event`（signal-safe，SPEC §1.3）+ 退出码经权威派生。code-reviewer 2 🔴 + 5 🟡 闭环（script.py 铁律 1+2 违规修复 / DI 闭环留 phase-12 follow-up / `_handle_timeout` 加 2s 超时防御 / singleton 测试复位 / asyncio.run+signal 交互注释 / script.py try/finally 覆盖 CancelledError）；test-coverage-e2e 真跑 5 项验证全过（退出码 0/1/2 / pgid==pid 证 start_new_session / shutdown 3 次幂等 / grep 守门 clean）。**1558 passed 0 回归**（baseline 1525 + 33 新增）。Commit：`cdc3469`。详见 [release note](releases/2026-07-07-phase-11-process-lifecycle.md)。

## [2026-07-07] TUI Redesign v2 —— 取消 DAG + agent 输出可见 + 切换 agent 看历史（三块布局重写）
TUI 三块布局重写：左 30% AgentsList + 右上 70% AgentHistory + 右下 30% LogStream。真删 v1.1.1 widget（DagGraph / dag_layout / _dag_render / activity_stream）+ display:none 双写兼容路径。用户核心需求闭环（last message 默认展开 + j/k 切换 + Log Stream 5 level icon）。
SPEC：[tui-redesign-v2-design-draft.md](../specs/tui-redesign-v2-design-draft.md) · release：[2026-07-07-tui-redesign-v2.md](../releases/2026-07-07-tui-redesign-v2.md) · commits：59021c9 + 5f9988c + e252653 + ab3b254 + 0e9e877 + 77f5685 + 85ecb61

## [2026-07-07] phase-11-error-handling —— 统一错误处理（ErrorKind 11 分类 + Result 信封 + classifier 双入口）
ExecError 字段集改 `{kind,message,phase,node,raw}`（kind 必填唯一分类轴）；新增 4 个 exec/ 层模块（`error_kinds.py` / `result.py` / `classifier.py` / `retry.py`）；`WorkflowAborted/MaxIter/RouteError` 改 ExecError 子类（固定 kind,phase），`WorkflowTerminated` 保留独立；error_type→kind 全量迁移（emit 写 kind + 读兼容期保留 error_type）；retry_started.data 扩展 layer/kind/reason/next_retry_at；编排 exception 子类化 + orchestrator except 顺序（WorkflowTerminated 先于 ExecError）。code-reviewer 3 个 🔴 + 8 个 🟡 闭环（wait.py 走标准 ExecError 路径 / `_classify_error` 用 ErrorKind.X.value / classifier profile 钩子加 warning log / DRY `_with_retryable` helper / 补 transport retry 测试）；test-coverage-e2e 真跑 demo_max_iter + opencode bad model 发现 2 处 emit defect（**Defect A**：orchestrator retry path 漏写 `next_retry_at` / **Defect B**：`layer` 与 `kind` 经两份派生表不一致）→ 已修 + 加 regression test。**1525 passed 0 回归**（baseline 1386 + 139 新增）。Commit：`451dd39`。详见 [release note](releases/2026-07-07-phase-11-error-handling.md)。

## [2026-07-04] TUI 重设计 v1.1.1 —— 真用户验证 4 GAP 收口（A/B/C/E）
修 test-coverage-e2e 真跑发现的 4 个 spec 违规：(1) **GAP-A** `app.py` agent_usage 同步投 `DagGraph.update_node_projection(tokens=...)`（DAG 行 3 由 `-- tok` 变实际数字，spec §4.4 acceptance）；(2) **GAP-B** Activity Stream 维护 `tool_call_id → (tool, args, call_ts)` cache，`agent_tool_result` 反查派生 tool/args（canonical Event result data 仅含 `{tool_call_id, result}`），summary 由 `?  {}` 变 `glob **/*.py` 等（spec §5.4「与 call 同 entry」语义）；(3) **GAP-C** elapsed 从 `call.timestamp + result.timestamp` 派生（顶层 Event 字段，spec §3），spec §5.4 订正为 `<N> lines · <elapsed>s`（exit_code 可选，canonical 不支持）；(4) **GAP-E** `DagGraph.build_from_workflow` 允许 self-loop（loop workflow `counter → counter` 重入语义），多节点环仍 fail loud。新增 8 测试 + 真 TUI 重放脚本（`_tui_gap_verify.py`），**1392 passed 0 回归**（baseline 1380 + 12 新断言），mxint tape 重放 5/5 节点 tokens 全非 None + 60/60 tool_result summary 含 tool name + meta 含 elapsed，demo_loop tape 重放 counter iter=3 与 node_started 次数一致。
Commit：`225933e`。详见 [release note](releases/2026-07-04-tui-redesign-v1-gaps-abce.md)。

## [2026-07-04] TUI 重设计 v1（spec v1.1 全 P0 闭环：3 行盒子 DAG + Activity Stream 双行 entry + EVENT_VISIBILITY 噪音治理 + 取消 NodeDetail + `f` 键 filter）
TUI 整体重设计对齐 spec v1.1（spec-review-adversarial conditional-pass → 5 P0 + 3 用户决策闭环）。新增 `_event_filter.EVENT_VISIBILITY`（7 tag 全 32 EventType 覆盖 + 完整性测试守门）+ `_dag_render` 独立渲染 helper（3 行盒子 + fan-in `(N inputs · M/N arrived)` 副标 + `after=None` 单独 section + ≥5 并行 fallback）+ `activity_stream` 双行 entry + 折叠详情（32 EventType per-type 字段级映射，复用 phase-15 `render_tool`/`render_message`/`render_thinking`）+ Header footer per-node usage（横向滚动 + running 优先）+ `f` 键 filter 模式（O1=c 取消 NodeDetail 但保留实例兼容）。reducer 派生 fold：iter 号 `node_session_ids`（重放产相同值，retry/skip/interrupt 不算新 iter）；fan_in arrived（dst 节点 node_completed 累加）。**单向依赖守住**（新模块零 orca.exec/run/events.bus 反向 import）。**1380 passed 0 回归**（baseline 1333 + 47 新测试），mxint 真跑 tape 重放 SVG 截屏（186 events → 152 进 Activity Stream，filter 掉 17 prompt_rendered + 17 agent_usage）。
Commit：`7bd43ef`。详见 [release note](releases/2026-07-04-tui-redesign-v1.md)。

## [2026-07-04] mxint_analysis 真实 bitx 量化分析迁移（替 stub + 5 agent prompts 真版）
将 `examples/mxint_analysis.yaml` + 5 个 agent prompts + `tests/e2e_mxint/` 从**简化 stub**（伪 SimpleNet + fake JSON，2 分钟跑完）迁移到**真实 bitx 量化分析**：target 换成 `ConfigurableMLP`（8970 params，sklearn digits 8x8，~90% eval_acc）+ 真调 bitx `Session` + 5 observers + `StudyReport.save` + `run_diagnostic_pipeline` 三阶段；2 个 driver script（`run_analysis.py` / `run_diagnostic.py`，后者含 bitx 1.1.1.dev395 `DistOverlayData.to_chart_data` bug 的进程内 monkey-patch）。**foreground 真跑 185s**（>2 分钟 stub baseline），5 张 chart（accuracy/bottleneck/sensitivity/qsnr_depth/recovery）真推 tape，76 行 REPORT.md 含真 QSNR 数据（51.37 dB avg，weight-dominated，recovery 31.7%）。**1333 passed 0 回归**。已知 follow-up：`_run_workflow_headless` 不起 chart ingestor，但 env 仍透传死 sock 路径（background 模式 chart 不通，prompt 让 agent 优雅 fallback）。
Commit：`838695f`。详见 [release note](releases/2026-07-04-mxint-real-bitx.md)。

## [2026-07-04] phase-15 render layer v1 —— e2e gaps 闭环（GAP#1 opencode read 文件 envelope + GAP#2 file_write subtitle）
修真跑发现的 2 个用户可见视觉异常：(1) opencode `read` **文件** result 同样是 XML envelope（与目录同形），原 `_normalize_file_read` 只检测 directory，file 走兜底 → envelope tag 泄漏 + opencode 自带 `N:` 前缀与 Rich Syntax 双重行号 + `(End of file)` marker 漏出；抽统一 `_parse_opencode_xml_envelope` helper（DRY），剥三层修饰（envelope 起手换行 + `N:` 前缀 + EOF marker）+ 仅 `<path>` 起手式才尝试 XML 解析（避免 claude Read 普通 HTML/XML 文件误判）+ fail visible（解析失败/未知 type/缺字段 → warning + 降级原文，§13）。(2) `_make_subtitle` 加 `file_write` 分支 → `new, NB`（spec §8.1）。spec §6.3 同步订正（原"opencode read 文件：同 claude"与实测不符）。**1333 passed** 0 回归（baseline 1327 + 6 新增）；真跑 tape seq=5 验证 72 行 TOML 干净渲染。Commit：`900fcfd`。详见 [release note](releases/2026-07-04-render-layer-v1-e2e-gaps.md)。

## [2026-07-04] phase-15 render layer v1（TUI 端）
实现 render-layer-design-draft §11.1 v1：在 canonical Event 之上加 iface 层纯函数渲染抽象（`normalize_tool` → RenderItem → `render_tool` → Rich renderable）。新增 `orca/schema/render_item.py` + `orca/iface/cli/widgets/tool_render/`（normalize/kinds/registry/reduce，单向依赖 only schema+rich+stdlib）+ `tests/e2e_phase15/_artifacts/render_tool_cases.json` 11 case fixtures + `tests/iface/cli/test_tool_render.py` 32 test（snapshot + fail loud + reducer + claude-code 对齐 acceptance §14.1）。迁移：log_stream 工具事件摘要共享 `describe_tool_event`（DRY，行为不变）；node_detail 流式 tab 工具事件升级为 Rich tool card（opencode read 目录现渲染为 17 条目树，不再 XML 一坨）+ thinking dim+italic 纯文本 + `t` 键切可见性（§12.8）。**1327 passed 0 回归**（baseline 1276）。Web 端 / shiki 流式 / 复制按钮 / codex 显式不做（v1 外）。
Commit：`ae0126b` + `edd738f`。详见 [release note](releases/2026-07-04-render-layer-v1.md)。

## [2026-07-03] examples 整理（固化 opencode 后端 + description + render_chart example + 全跑通 e2e）
13 agent example 固化 `executor: opencode` + `model: "deepseek/deepseek-v4-flash"`（with_ask_user 保留 claude——ask_user 需 mcp_tools=True）；补全 21 example description（TUI 信息明确）；`examples/README.md` 分类（纯 script / agent workflow / claude-only 例外）；新建 render_chart example（**文件夹化 agent** plotter + scripts/chart_demo.py 资源，演示 phase-14 `ORCA_AGENT_RESOURCES` + phase-13 chart 链路）；parallel_research 迁移 phase-14 `agent: <name>` 显式引用（消除旧约定 warn）。**验证**：8 script + 13 agent + render_chart 全跑通（opencode+deepseek-v4-flash **真跑不 mock**）；with_ask_user 例外（claude-only）。tests: test_examples_script + test_examples_opencode。
Commit：`c5c13b1`。详见 `examples/README.md`。

## [2026-07-03] phase 14 Agent 一等化（agent 池 + 文件夹化 + 统一解析层）+ Route 输出变换（批 1）
agent 从内嵌 prompt 升级为可命名/可复用/可携带资源的一等公民：新增 `orca/compile/agents.py` 统一解析层（`AgentResolver` Protocol + `LocalPoolResolver`，**删 `_load_prompts` + `_load_agent_md` 双加载债**）→ `AgentNode.agent` 显式引用 + 文件夹化（`<name>/agent.md` + 资源子目录）+ frontmatter 元数据 + `Route.output` 终点输出变换 + MCP `list_agents`/`get_agent`。**spec-review-adversarial 对抗审闭环**（2 P0 + 5 P1：warn 通道/skip end_route 统一/tools None 消歧/is_folder/frontmatter 精确算法/空串防御）。实现期修 SPEC 隐含缺陷（互斥预检须物化前）。**opencode+deepseek-v4-flash 真跑 e2e**：E2E-1 agent 引用（GREETER_OK）+ E2E-2 文件夹化 resources（`$ORCA_AGENT_RESOURCES` → SECRET_FLAG_42）。顺带修 executor capability guard（opencode + tools 不注 `--allowed-tools`）。**1276 passed 0 回归**。批 2（包分发 + workspace-instruction）留 phase-15。
Commit：`74d65b3`。详见 [release note](releases/2026-07-03-phase14-agent-first-class.md)。

## [2026-07-03] phase 13 script-side render_chart 接入（env 身份路由 + per-run Unix socket + 大数据三道关 + opencode+deepseek e2e）
让 claude/opencode/script 节点 spawn 的 script 子进程调 `orca.chart.render_chart` 推图：env 注入 4 个 ORCA_*（ClaudeExecutor + ScriptExecutor 都接，**executor-agnostic S5 闭环**）→ subprocess 链自然继承 → per-run Unix socket 传输 → tape 落 custom(chart) → 三壳零改动渲染。**对抗审闭环 16 处修订**（4 blocker + 9 major + 3 minor，含 ack timeout / sock 路径长度 / resume 边界 / opencode env 继承 / envelope 含义 / hue 分组降采样 / table 取前 N 等）。**大数据三道关**：自动降采样（max_points=2000，6 chart_type 各自策略）+ 2MB 硬上限 + ingestor 复核。**E2E-5 压测**：3 run × 10 chart 无丢失/串扰；**E2E-6 opencode+deepseek-v4-flash 真跑**：4 验证点（agent_message 完整性 / TUI 各面板合理 / render_chart 推送 / 图表排布）逐条通过；TUI snapshot 留档。**1224 passed 0 回归**（baseline 1208→1224，新增 16 测试）。S5 顺带修 2 实施 gap：ScriptExecutor 漏 chart env（违反 SPEC §11 #9）+ OrcaApp CLI shell 漏起 ingestor。
Commit：`1740a98`（S1-S4）+ `f260935`（S5 实施 gap 补丁）+ `b562a12`（S5 e2e）。详见 [release note](releases/2026-07-03-phase13-render-chart.md)。

## [2026-07-03] phase 12 CLI TUI 重设计（拓扑图 + NodeDetail + 终端图表 + opencode e2e）
重设计三面板：左 DagTree→DagGraph 拓扑图（分层+连边，max 33%）、右上 ActiveNode→NodeDetail（流式/输出/图表 tab，6 kind 永不空白）、新增终端图表渲染（plotext braille）+ ChartBrowser 全屏。6 新文件零后端 import、壳无真相、确定性 fold、`_selected_node`/`_auto_follow` 不写 tape（全有单测守护）。LayeredDagLayout spike 全过（未 fallback）。**S10 e2e：opencode 后端（glm-4.6v）真跑驱动 TUI 端到端通过**（SPEC §6 逐项 + 断言证据；图表渲染走解耦注入真路径——braille + 多图分组规整；`render_chart` 生产者未实现，待 phase-10）。e2e 顺带修真 bug：`ClaudeExecutor` 无条件注 `--allowed-tools`/`--mcp-config` → opencode spawn 失败，gate 到 `capabilities.mcp_tools` 修复。**1133 passed 0 回归**（基线 1082→1133，净增 51 测试）。
Commit: `38fd78c`（S0-S9）+ `cd6c1ee`（opencode spawn fix）+ `81d2f93`（S10 e2e）。详见 [release note](releases/2026-07-03-phase12-tui-redesign.md)。

## [2026-07-03] 后端统一抽象 + opencode 后端接入
把"后端怎么信号 done+result+usage+错误"下沉成 profile 字段 `TerminalContract`（`result_line` /
`events` 两模式）+ 共享 `RunAccumulator`，executor 保留一处小分支，runner 不动。加 opencode =
加 translator + profile 两文件（events 模式，prompt_channel=argv）。E2E 发现并修 runner 的
argv-channel stdin 不关闭导致 opencode 永久挂死的真实 bug。真实 orca CLI 双后端 E2E 跑通
（opencode glm-4.6v + claude/deepseek，均 completed）。688 passed 0 回归。
Commit: `f3129d1`。详见 [release note](releases/2026-07-03-opencode-backend.md)。

## [2026-07-02] orca executor —— 持久化后端二进制配置 + 健康检查
新增 `orca executor set/show/unset/list/test` 命令组：`~/.orca/config.json` 持久化 per-profile
binary override，`orca` 启动期 `os.environ.setdefault` 注入，复用既有 `resolve_cli_path()` 运行时
读 env——**exec/profile/registry 零核心改动**（OCP）。`pip install` 后 `orca executor set claude
"ccr code"` 一次设、全局生效；`executor test` 真起子进程自检协议兼容性（两层超时 + spawn 失败
fail loud）。顺带把 ccr profile 的 dummy translator 接上 `claude_translator`（ccr 协议兼容）。
config.py + executor_cmds.py（含纯函数 classify）+ 35 单测 + 9 e2e（假脚本走完整 spawn 链路，
不 mock CLIRunner）+ 2 integration。终审 0 🔴 1 🟡（已修）/ 2 🟢（跳过）。1031 passed 0 回归。
Commit: `ce559b6`。详见 [release note](releases/2026-07-02-executor-config.md)。

## [2026-07-02] agent 可观测性 + TUI 闪退 + 子进程泄漏修复（4 bug）
排查 demo_mixed 529 闪退时定位的 4 个 Orca 自身 bug：① OnResult 加 `api_error_status` 第 5 参
（全仓 11 处同步），executor `_result_diag()` 让 529 等 API 错误详情落到 `node_failed`（原只带空 stderr）；
② translator ApiRetry 对齐真实字段 `attempt`/`retry_delay_ms`/`error_status`（原读 `retry_count`/`wait_seconds`
永远 null，显示「第 ? 次」）；③ TUI 终态后停留 + notify 提示「按 q 退出」（原 `self.exit()` 闪退）；
④ `CLIRunner.stream()` finally terminate proc（原中途 q 强退留孤儿 claude）。7 新测试，985 passed 0 回归。
Commit: `f422d98`。详见 [release note](releases/2026-07-02-agent-observability-tui-fixes.md)。

## [2026-07-02] terminate step —— 新增 node kind `terminate`（业务级显式工作流终止节点）
新增第 6 个 node kind：触达即终止，`status=success` → `workflow_completed`（用 terminate.outputs），
`status=failed` → `workflow_failed{error_type=WorkflowTerminated, message=reason}`。补 `TerminateExecutor`
（仿 set_node 模板）+ factory 分派 + orchestrator 终态分发（新 `WorkflowTerminated` 异常 + `_finalize_terminated`
helper）+ compile 层 4 项 fail loud 校验（routes 空 / 非entry / 非parallel branch / 非foreach body）。
零 EventType/reducer 改动（复用既有 `node_completed`）；19 新测试，1013 passed 0 回归。
Commit: `41a5936`。详见 [release note](releases/2026-07-02-terminate-step.md)。

## [2026-07-02] phase 11 收官 —— CLI feature 补全全部完成（11 feature，652→959 测试，0 回归）
对抗评审（fail→conditional-pass，22 真问题闭环）→ 4 wave clean-code-builder + 4 wave test-coverage-e2e →
code-reviewer 横切审计（0 🔴 0 🟡）。交付 CI / Interrupt+Guidance / Resume / Retry / ask_user MCP /
Wait / Validator / Dialog / Skip / daemon 共 11 feature；e2e 审计狩猎并修复 2 个单 Tape 不变量
critical bug（interrupt_resolved 丢事件 / Ctrl+G 打不断 wait）；9 处 SPEC 偏离全部 Rule 7 裁定双落。
Budget（D3）/ attach（D2）descoped。commit: `120085f`→`d295922`（见各条）。
- 详情：[release note](releases/2026-07-02-phase11-complete.md)

## [2026-07-02] phase 11 P3.2 —— daemon `--background` 模式 + ps/logs/wait（attach descoped）
长跑 workflow 不占终端：`orca run --background` fork detached child（headless Orchestrator，
非 TUI——detached 无 TTY Textual 会崩，SPEC §11.9 裁定），父进程立即返回 run_id + pid；
配合 `ps`（dead pid 标 crashed，fail loud）/ `logs <id> [-f]` / `wait <id>` 三件套。
`daemonize` 5-callback seam 可测（CI 不留孤儿）；run_id 经 env 父子一致（metadata/tape/orchestrator
三处对齐，resume 可接）。code-reviewer 1 🔴（BaseException 漏 SIGTERM）+ 6 🟡 + 2 🟢 全修。
904→956（+52），0 回归。Commit: 见 git log。
- 详情：[release note](releases/2026-07-02-phase11-daemon.md)

## [2026-07-02] phase 11 P4 —— Skip to Agent（显式 skip 目标 + NodeSelectModal + §9.2 route 容错）
wave-1 SKIP 只能沿 route 跳，无兜底 route 时 NoRouteMatch 崩溃（SPEC §10.2 item12）。本 wave 补齐：
`request_interrupt` 加 `skip_target` 参数 → `_drive_loop` 直接跳该 node（不经 route 求值）；
`NodeSelectModal`（iface/cli/screens/）让用户选目标（pattern A：InterruptModal → app 推选择器）；
router §9.2 容错（skipped node 的 None output 让 when 求值失败走兜底，非崩溃）；`_validate_skip_target`
fail loud（ValueError，非 NoRouteMatch）；`interrupt_resolved.data.skip_target` 写 tape 可观测。
code-reviewer 1 🔴（验证顺序致脏 tape）+ 3 🟡 全修。888→904 零回归。Commit: 见 git log。
- 详情：[release note](releases/2026-07-02-phase11-skip-to-agent.md)

## [2026-07-02] phase 11 fix —— Ctrl+G 立即唤醒 sleeping wait node（wave-3 e2e 审计 bugfix）
wave-3 e2e 审计发现 SPEC §9.7.6 + §10.2 item9 承诺的「Ctrl+G 打断 wait node」实际不工作：
`notify_all_waits` 原本只在 node 边界 `_handle_interrupt` 触发，wait sleep 期间 drive_loop 阻塞
在 `_dispatch` 到不了边界 → 对 sleeping wait 是死代码。修复：`Orchestrator.request_interrupt`
登记 pending 的同时即时调 `bus.notify_all_waits()`（保留 record_resolved/resolve 里的同一调用
作 defense-in-depth）。xfail 复现测试翻转 pass + 8 新 wave-3 e2e 测试采纳，879→888 零回归。
- commit: 89b23ab
- 详情：[release note](releases/2026-07-02-phase11-wait-interrupt-fix.md)

## [2026-07-02] phase 11 P2.2 —— Dialog（agent 跑完后多轮追问，重 spawn claude 拼历史）
用户按 `d` 键就已完成 agent 的 output 多轮追问：`DialogHandler` 3-method split（start/send/end），
每轮重 spawn claude 把「output + 完整历史 + 本轮问题」拼进 prompt（`-p` 路线无 in-process
session，靠 prompt 拼历史）。Rule 7 裁定 3-method split（SPEC §6.2 单一 run_dialog 无法在轮间
交还 UI 控制）；`ctx.dialog_history` 是 web shell replay 预留位（真相在 tape）；抽
`orca/exec/env.py` 化解三处 `_build_env_overlay` 重复（Rule 6 DRY）。+27 测试断言 INTENT
（含历史累积核心契约 + send 失败 fail loud + 按钮复位），852→879 零回归。
- commit: caa3943
- 详情：[release note](releases/2026-07-02-phase11-dialog.md)

## [2026-07-02] phase 11 P2.1 —— Semantic Output Validator（LLM 二次语义校验 agent output）
agent 产出后 spawn 第二个 claude -p 做 LLM 语义校验（非 shape/type），失败时 issues 作 guidance
反馈重 spawn，直到通过或预算用尽（fail-safe：validator 自身崩 → 当作 passed）。`validate_output`
纯函数不持 bus（Rule 7 化解铁律 2），三类 validator_* 事件由 orchestrator loop 统一 emit；validator
与 retry 独立预算（SPEC §11.6 deviation）。822 → 852 passed（+30，0 回归）。Commit: e4eb07c。
详见 [release note](releases/2026-07-02-phase11-validator.md)。

## [2026-07-02] phase 11 P3.1 —— Wait Node（asyncio.sleep 节点，Ctrl+G 可打断）
SPEC §9.7：新 `kind: wait` 节点（`asyncio.sleep(duration)`，`interruptible=True` 时可被 Ctrl+G 打断）。新增 `orca/exec/wait.py`（`WaitExecutor` + `parse_duration` + `WaitHandleRegistry` Protocol）+ `WaitNode` schema（加入 `AnnotatedNode` 判别联合，5 kind）+ `wait_started`/`wait_completed` 事件 + `EventBus.register_wait_handle`/`unregister_wait_handle`/`notify_all_waits`（SPEC §9.7.6 公开契约，`threading.Lock` 保护集合）+ `make_executor` 加 `bus` 参（仅 wait 分支透传）+ `InterruptHandler.resolve`/`record_resolved` 双路径调 `notify_all_waits`（Ctrl+G 立即打断正在 sleep 的 wait）+ `_PHASE_TO_ERROR_TYPE` 登记 `config`/`ConfigError` + LogStream 描述。**关键设计**：`WaitHandleRegistry` Protocol 化解「WaitExecutor 需 bus 访问」与「铁律 2 禁 exec 持 bus」的张力（ISP/DIP，能力裁剪到最小，executor 无法写 tape/emit，契约测试全绿）。SPEC §11.5 记 3 处偏离。**全量 822 passed / 1 skipped**（基线 784 + 38 新测试，0 回归）。Commit: `3921c89`。详见 [release note](../releases/2026-07-02-phase11-wait-node.md)。

## [2026-07-02] phase 11 P1.2 —— ask_user MCP 工具挂载（被编排 claude 主动问用户）
SPEC §5：Orca 进程内嵌 socket SSE MCP server（`AgentToolsMcpServer`，`mcp.server.fastmcp`），注册 `ask_user` 工具；被编排的 claude -p 经 `--mcp-config` 连上，调 ask_user 触发 `HumanGate(source=agent_ask)` → 等壳 resolve → 返回 answer。**SSE spike 双轮全 PASS**（in-memory ClientSession round-trip + real claude `-p --mcp-config` 连通性 + 工具调用）。确定性 tool-params 路由（D4：`orca_run_id`/`orca_node`，**不**依赖 MCP session 反查）+ spike 实证 claude -p 默认不给 MCP 工具授权（自动 append `--allowed-tools mcp__orca-agent-tools__ask_user`，SPEC §11.3）。register 债补完（B2）+ gates `RunContext`→`SessionLoc` 改名（B2）+ `unregister_run` 按 run 批清（SPEC §6）+ orchestrator `run()`/`run_from_state()` lazy start/stop server（start 失败 → workflow_failed fail loud）+ `_append_ask_user_instruction` 把路由参值拼进 prompt。**两轮 code-reviewer 全反馈闭环**（🔴 tape 配对断言 + unregister 接线 + start fail loud + 4 个测试 gap）。SPEC §11.2-§11.4 记 3 处偏离。**全量 773 passed / 1 skipped**（基线 753 + 20 新测试，0 回归）。Commit: `dcc3e63`。详见 [release note](../releases/2026-07-02-phase11-ask-user-mcp.md)。

## [2026-07-02] phase 11 P0.3 —— Retry Policy（节点级自动重试 transient claude 失败）
SPEC §9.5：agent node 声明 `RetryPolicy`（max_attempts/backoff/retry_on/jitter）→ transient 失败（spawn_error/timeout/api_error/http_429）自动重试，带 exponential/linear/constant backoff + ±20% jitter 防雪崩。新增 `orca/run/retry.py::execute_with_retry`（核心 loop：was_interrupted 短路 + retry_on 白名单过滤 + retry_started/succeeded/exhausted 事件可观测）+ `_compute_delay`（DRY 单点 delay 计算）+ `_classify_for_retry`（**error_type 对齐层**：桥接 ClaudeExecutor 的 `CliExitNonZero`/`ExecTimeout`/`ClaudeStreamError` 到 retry_on 的 `spawn_error`/`timeout`/`api_error`/`http_429` 语义短名，SPEC §9.5.2 对齐表）+ `RetryPolicy` schema（`Field(ge=1)` 下界校验）+ `ExecError.from_failed_data` classmethod（DRY：retry loop 与 execute_and_emit 共享）+ orchestrator `_dispatch` 集成（agent+retry 走 retry loop，否则既有路径）+ reducer retry_* no-op + LogStream 描述。validator（wave 3）将复用本 loop。**全量 753 passed / 1 skipped**（基线 726 + 27 新测试，0 回归）。Commit: `95cdae4`。详见 [release note](../releases/2026-07-02-phase11-retry-policy.md)。

## [2026-07-02] phase 11 —— `interrupt_resolved` 同步写 Tape 修复（wave-1 e2e 审计）
wave-1 e2e 审计发现 critical bug：CLI 单壳中断路径 abort/skip（continue 偶发）分支的 `interrupt_resolved` 被 async broadcaster 与 `run()` 的 `bus.close()` 竞态丢失（Tape 缺配对事件，违反单 Tape 唯一真相源）。Option A 修复：`record_resolved` 改同步 `await bus.emit` 写 Tape，async broadcaster 仅留给同步 `resolve()` 入口。6 个 xfail(strict=True) 全转 PASS + 新增 emit-on-closed-bus fail-loud 契约测试。全量 726 passed / 1 skipped / 0 xfailed，0 回归。Commit: `a3ae691`。详见 [release note](../releases/2026-07-02-phase11-interrupt-resolved-fix.md)。

## [2026-07-02] phase 11 P2.2 —— Checkpoint Resume（`orca resume` 崩溃续跑）
SPEC §7：Orca 的 Tape 天生是 checkpoint（append-only JSONL，无需 Conductor 的独立状态序列化系统）。新增 `orca run/resume.py`（typed exceptions + 纯辅助：中段损坏检测/outputs aggregate 重建/parallel mid-crash 检测）+ `Orchestrator.from_tape` classmethod + `run_from_state`（emit `workflow_resumed{from_tape,resumed_node,replayed_events}` 后续跑）+ `_drive_loop` 抽出 `_drive_from(start_node, initial_outputs)` 让 `run()`/`run_from_state()` 共享（DRY）+ `workflow_resumed` 事件类型 + reducer no-op 分类（interrupt_*/prompt_rendered/workflow_resumed）+ CLI `resume` 子命令（参数解析 + 6 种失败模式 → exit code，headless 不启动 TUI）+ LogStream 描述。**code-reviewer 全部反馈闭环**：`_bare_instance` 字段漂移安全网（`_DRIVE_REQUIRED_FIELDS` + `_assert_drive_fields_complete`）/ `_find_first_corrupt_line` position-aware（末尾残行不算 corrupt，from_tape 不依赖调用方先截断）/ fallback 分支测试 / 消除冗余 tape 读（单遍扫描返 valid_count）/ `_inputs_from_tape` 空 inputs warning / Event-schema 损坏测试。parallel 组中间崩溃不支持（SPEC §7 risk，exit 1）。**全量 712 passed / 1 skipped**（基线 697 + 15 新测试，0 回归）。Commit: `0d53eed`。详见 [release note](../releases/2026-07-02-phase11-checkpoint-resume.md)。

## [2026-07-02] phase 11 P1.1 Step B —— mid-run Guidance 注入 + SIGINT + review §2.1 critical 修复
SPEC §4 Step B：RunContext 加 `user_guidance`/`interrupt_history` + `with_guidance`/`guidance_prompt_section`（逐字对齐 Conductor `[User Guidance]` 段）+ render_prompt 拼 guidance section + orchestrator `_make_ctx` 注入累积 guidance（SPEC §10.3 C3：走既有 _make_ctx）+ CLIRunner.send_sigint/was_interrupted + ClaudeExecutor SIGINT 优先判定（emit node_failed{was_interrupted}，不 raise，SPEC §9.5.2 retry 短路前置）+ spawn 前 emit prompt_rendered（preview ≤200 字符，guidance 注入可观测，SPEC §10.2 item3 B5）。**code-reviewer 发现 critical 时序死锁（§2.1）**：Step A 的 action_interrupt「登记 pending + 立即 resolve」连调，但 handler.request 要等 node 边界才注册 future → resolve 落空 + workflow 卡死。修复：CLI 单壳路径 `request_interrupt(ireq, answer=)` + 新 `InterruptHandler.record_resolved`（emit requested + 入队 resolved，不经 await-future）；多壳 await-future 路径保留给 P3。SPEC §11.1 记此偏离。**全量 697 passed / 1 skipped**（Step A 后 674 + 23 新测试，0 回归）。Commit: `01af451`。详见 [release note](../releases/2026-07-02-phase11-guidance-injection.md)。

## [2026-07-01] phase 11 P1.1 Step A —— 优雅中断 UI（InterruptHandler + InterruptModal + Orchestrator wiring）
SPEC §3 Step A：抽出 `orca/gates/_broadcaster_mixin.py`（HumanGateHandler/InterruptHandler 共享 start/stop/_broadcaster，DRY）+ 新增 `InterruptHandler`（request/resolve/first-wins/跨线程 broadcaster emit `interrupt_resolved`）+ `InterruptRequest` 原语 + 3 个新事件类型（interrupt_requested/interrupt_resolved/prompt_rendered）+ `WorkflowAborted` 异常 + Orchestrator `request_interrupt`/`_handle_interrupt`/node 边界 pending 检查（可选注入，None 向后兼容）+ Textual `InterruptModal`（CONTINUE/SKIP/ABORT + guidance textarea + Esc=abort）+ OrcaApp Ctrl+G 绑定 + LogStream format_event。**全量 674 passed / 1 skipped**（基线 652 + 22 新测试，0 回归）。本 commit 同时合入先前未提交的 mxint 端到端实测 bugfix 基线（orchestrator default-fill / app.py on_mount kickoff / log_stream agent_usage / commands.py，见下条），因 Step A 的 `_drive_loop` 改造建立在 mxint default-fill 循环之上、同 hunk 不可分。Commit: `9db57f4`。详见 [release note](../releases/2026-07-01-phase11-interrupt-ui.md)。

## [2026-07-01] phase 11 P0.1 CI —— GitHub Actions 双 workflow（gate + opt-in integration）
新建 `.github/workflows/test.yml`（gate：push/PR(master) → matrix Python 3.10/3.11/3.12 → `uv run pytest -m "not integration"`）+ `.github/workflows/integration.yml`（opt-in：PR comment 含 `/integration` → guard 校验 PR-only + write 权限 + 非 fork PR + API key 非空 → 真 claude E2E）。基线 `uv run pytest tests/ -m "not integration"` = **652 passed / 1 skipped / 37 deselected** 绿。code-reviewer 0 critical，2 major + 2 minor + 2 nit 全闭环（trigger 改 contains / fork 拒绝 / API key fail-loud / timeout-minutes / 注释订正）。Commit: `120085f`。详见 [release note](../releases/2026-07-01-phase11-ci.md)。

## [2026-07-01] 端到端实测 `orca run` 修 3 个真实 bug —— CLI 跑不起来 / inputs.default 缺失 / agent_usage 显示简陋
迁移 AgentHarness 的 mxint-analysis（5 agent 链：analyzer→configurator→runner→diagnostic_saver→report_painter，保骨架换内容无 torch/bitx 依赖）做端到端实测，**首次 `orca run` 撞 3 个真实问题**，全部是 phase 7/5 的功能 gap 且单测零覆盖：(1) **架构 bug**：`commands._run_workflow` 在 `tui.run()` 前调 `kickoff()`，`@work` decorator 需 loop running，撞 `RuntimeError: no running event loop` —— 真实 `orca run` 完全跑不起来；测试 mock 回避故未发现。修：commands 不调 kickoff，挪到 `OrcaApp.on_mount` 末尾（与既有 `_consume_events` 同 pattern）。(2) **功能缺失**：yaml 声明的 `inputs.x.default` 从未被消费（除 `iterations` 特例），render 时 UndefinedError；schema/执行层契约断裂。修：`Orchestrator.__init__` 添加 default 填充循环 + required 缺失 fail loud。(3) **UX 改进**：LogStream `agent_usage` 仅显示字面值，未展示 token 数。修：`format_event` 加 agent_usage case 显示 `usage: in=.. out=.. cache=.. cost=$..`。**实跑验收**：209s 全绿 exit 0，5 个 agent 全部按要求完成结构化输出（schema 100% 匹配），落盘 adapter.py / results.json / diagnostic/*.json / REPORT.md(126 行) 齐全。**tape 完整性 8 项校验全过**：seq 连续无空洞 / 5 个 node 生命周期完整 / tool_call-result 30/30 完美配对 / agent_usage 在 node_completed 前 / workflow 闭环 / tape replay 还原 RunState 全部 5 个 output。**全量回归 683 passed / 0 failed**。反思：phase 7 CLI 壳虽写了 24 个测试但**真实 `orca run` 路径无端到端覆盖**，建议未来每 phase 完成至少跑一次真实 `orca run examples/<demo>.yaml` 作 acceptance 硬条件。Commit: `9db57f4`。详见 [release note](../releases/2026-07-01-e2e-mxint-bugfix.md)。

## [2026-07-01] 阶段 10 iface/mcp 壳（外部 MCP 服务）—— 单进程多壳共存（MCP stdio + Web HTTP 共享 RunManager，gc 启动 assert 保护）+ HandleId 四件套工具（start_workflow / get_task_status / resolve_gate / cancel_task，每 tool 秒级返回规避 CC 60s 超时）+ tape-only query path（pending_gates_from_tape 纯函数派生 + RunManager.run_summary 合并，禁读 handler._pending/_gates_meta，反 AgentHarness 多真相源）+ source="mcp" 复用 handler.resolve（零新 resolve 路径，first-wins + broadcaster 与 Web 同款）+ workflow_cancelled 事件类型（cancel 写 tape 才是唯一真相）+ stdio 每消息 flush（FlushingStdoutWriter 兜底，规避 opencode #21516）+ stdin EOF 双行为（无 --with-web 随 CC 生灭 / 有 --with-web 转 daemon）+ orca mcp 命令（--with-web / --web-port / --max-concurrent / --idle-timeout / --runs-dir）；5 个 E2E 闭环（demo_linear 真 stdio round-trip / 合成 gate + source="mcp" 端到端 / MCP+Web first-wins + 广播写 tape / opencode flush 并发不丢 / 真 claude integration）+ 53 passed 2 skipped（tests/iface/mcp/）+ 652 passed 默认套件零回归 0 warnings；七铁律 grep 全过；6 个透明偏离（emit-before-cancel 顺序 / mcp<1.28 cryptography<49 构建地狱 / 慢 script 替 demo_linear 防 tape close race / 真 RunManager 替 mock 证 HandleId / daemon 60s tick 改 mock 单测 / 加 --runs-dir 测试隔离）；路径 A（CC agent + skill）明确不做留后续。Commit: `4860def`→`ca5ca4b`→`20472b1`→`c26307c`→`2cf5c66`。详见 [release note](../releases/2026-07-01-phase10-mcp.md)。

## [2026-07-01] phase 9 浏览器 E2E 修复 —— SPA fallback(深链 404) + live_server fixture + 测试 bug(run_id/WS/playwright API/async)
phase 9 前端浏览器实测可用但 playwright E2E 套件有测试代码 bug + 一个真实后端 bug：`server.py` 加 SPA fallback（catch-all GET → index.html，修深链 `/runs/<id>` 刷新返回 404 的生产 bug，注册在 API/WS 之后且仅 GET 不吞 `/api/*` `/ws` `/gate`）；4 个测试文件的 `live_server` fixture 端口轮询替代坏掉的 sleep；WS live 推送测试改慢 workflow（sleep 5）+ 三重断言（事件数/run_id 标签/真编排 type）确定性证明 pump 真推送；`test_new_run_form` 修错误的 `run-*` URL 模式为 `demo-*-*`（贴合 `gen_run_id` 真实格式）；`test_cyclic_layout_no_overlap` 修不存在的 `allBoundingBoxes()` → `evaluate_all` getBoundingClientRect；`test_playwright_9d.py` 6 个 async 测试改 sync `def` + `asyncio.run` + chart 测试导航到 RunDetailPage output tab（ChartRenderer 仅在 output tab 挂载，首页注入无组件消费）。验收：playwright E2E **20 passed**（3+6+5+6）、默认套件 599 passed 0 warnings、vitest 84 passed。Commit: `4f891e8`。详见 [release note](../releases/2026-07-01-phase9-browser-e2e-fix.md)。

## [2026-07-01] Tape 写句柄惰性打开 —— 消除 ~30 条 ResourceWarning（root-cause fix）
`orca/events/tape.py::Tape` 写句柄由 `__init__` eager-open 改为首次 `append()` 在 `async with self._lock` 内惰性打开（race-free）+ `close()` 对只读 Tape 幂等 + `__del__` leak 安全网；只读构造（replay/inspect）不再泄漏未关闭的 append handle。顺带修 `tests/gates/test_hook_bridge.py` 9 处 mock server 漏补 `server_close()`（不同根因、同属 ResourceWarning 卫生类、trivial）。验收：`-W "error::ResourceWarning"` 全绿（30→0）、RuntimeWarning 全绿、599 passed 零回归、vitest 84 passed。Commit: `f85bc48`。详见 [release note](../releases/2026-07-01-tape-lazy-open.md)。

## [2026-07-01] 阶段 9d iface/web gate 弹窗 + render_chart —— gate 富交互弹窗（两 source：tool_permission 4 按钮 / agent_ask radio|textarea）全读 store.gate（零本地 gate state）+ 走 backend POST /gate/respond（前端纯 forward 不决策）+ 不乐观更新（答后等 human_decision_resolved 才关，保唯一真相源）+ 三通道竞速广播（别壳先答 → store.gate=null + lastResolved → ResolvedToast「已被 [source] 答」）+ render_chart 迁移 AgentHarness 学术配色 chartTheme（PALETTE 8 色逐字）+ 扁平 record-array spec + 5 种 recharts widget（line/bar/scatter/pareto/table）+ chart 是事件（custom kind=chart 从 store.events filter 无独立通道）+ 同 label+title 替换（实时更新）+ replay 同步（chart ≤ replayPosition）+ hue pivot 共享 helper（DRY）+ ?debug=1 opt-in 调试入口（playwright 集成用，prod 默认不暴露）+ happy-dom 尺寸打桩（recharts ResponsiveContainer 渲染所需）；vitest 84 passed（gate 10 + chart 16 + 既有 58 零回归）+ build 成功 + 595 Python 全绿零回归 0 RuntimeWarning；review 全修复 3 建议（hue pivot 去重 / pareto 前沿线测试 / AskGate selected 重置）+ 1 可选；6 playwright integration。**phase 9 全部子阶段 9a/9b/9c/9d 完成，分支 phase9-web 可合并 master**。Commit: `6d0c5e1`。详见 [release note](../releases/2026-06-30-phase9d-web-gate-chart.md)。

## [2026-07-01] 阶段 9c iface/web DAG 可视化 + tape replay —— ReactFlow 12 + @dagrejs/dagre：拓扑进 workflow_started.data（tape 单一真相源，live+历史 replay 都从事件拿）+ findBackEdges DFS 三色识别回环边（反向喂 dagre，渲染保持原方向）+ 5 种 node widget（Agent/Script/Set/Foreach/End 共享 NodeShell，NODE_STATUS_HEX 5 色）+ WorkflowGraph 三 effect 增量（拓扑全量 build / 节点状态只改变化节点 data 未变保持引用 / route_taken 标记走过边）+ replay setReplayTarget 前进 apply / 后退 checkpoint restore（每 20 事件存 snapshot，enterReplay 建 -1 空态 checkpoint 消除全量重置分支）+ 单路径 fold（replay applyOne 复用 foldEvent 同一 handler 表，反双路径）+ live==replay byte-identical 断言（含 cost/gate/foreach 富流）+ react-window v2 虚拟日志（1000 事件 < 50 DOM row，session 分组）+ NodeDetail + ReplayBar（play/pause/速度 1×-20×）；后端 surgical：lifecycle.make_workflow_started 加 topology 摘要（非破坏）；vitest 58 passed（store 13 + graph 15 + replay 12 + hooks 9 + log-detail 9）+ build 成功 + 595 Python 全绿零回归 0 RuntimeWarning；review 全修复 3 Must-fix（progress 透传 / live==replay 富流断言 / checkpoint-1 消除全量重置）+ 5 Minor + Nit；5 playwright integration。**分支 phase9-web**。Commit: `adc856c`。详见 [release note](../releases/2026-06-30-phase9c-web-dag-replay.md)。

## [2026-07-01] 阶段 9b iface/web 前端骨架 —— React 19 + Vite 6 + TypeScript SPA：react-router v6 BrowserRouter（`/`·`/runs/new`·`/runs/:runId`，navigate push，后退 = 浏览器原生）+ Zustand 单 store（全 src 唯一 create()，immer middleware 锁不可变）+ eventHandlers 表覆盖全部 21 个 EventType（live/replay 共用 processEvent，seq 去重 + last-writer-wins 保证 fold 幂等）+ 懒加载（useRunsList 只轮询 /api/runs 元数据，useRunEvents mount 才拉 /events，unloadRun 清不累积）+ useWebSocket（按需 subscribe + run_id 过滤 + 指数退避重连，重连才全量重拉避免双拉竞态）+ 三页面骨架（RunDetailPage tab 占位 dag/log/output/yaml 给 9c/9d）；TS 类型逐字对齐后端 Event/RunMeta/RunStatus；vitest 22 passed（store 13 + hooks 9，含单 store 正则断言 + fold 幂等显式测试）+ build 到 static/ + 6 playwright integration（后退语义/懒加载网络/URL 直达）；review 全修复（immer / 单一加载路径 / WorkflowStatus 导出 / fail loud / cleanup callbacks / build 产物），n4 双轮询 deferred 9c；594 Python 全绿零回归 0 RuntimeWarning。**分支 phase9-web**。Commit: `0347a66`。详见 [release note](../releases/2026-06-30-phase9b-web-frontend-core.md)。

## [2026-07-01] 阶段 9a iface/web 后端 —— FastAPI（单进程同引擎 uvicorn）+ RunManager 真并发（asyncio.Semaphore 默认 3，每个 run 独立 bus+tape+gate_handler 隔离）+ 懒加载 REST（`/api/runs` 只元数据无 events，事件走 `/api/runs/<id>/events` tape.replay）+ WebSocket 单通道按需订阅（subscribe(run_id) 只推该 run，切 run cancel pump，反向 gate_response）+ 多 run gate 分发（session_id→registry→run_id→handle.gate_handler，复用 phase-6 共享 helper DRY）；五条铁律 grep 全过；review 全修复（shutdown 超时兜底 / EventBus.close 幂等 / has_pending 公开 / N+1 优化 / gate 路由 8 测试补齐）；37 web 单测全绿（0 RuntimeWarning 0 ResourceWarning），594 全量全绿（零回归）。**分支 phase9-web**。Commit: `b34c87d`。详见 [release note](../releases/2026-06-30-phase9a-web-backend.md)。

## [2026-07-01] 阶段 7 iface/cli CLI 壳 —— Textual TUI（DAG 进度 + 流式日志 + gate ModalScreen）+ typer 命令绑定（run/validate/list，parse_inputs 类型推断，退出码 0/1/2）+ OrcaApp @work 编排 worker + _GateHttpBridge（uvicorn 独立线程跑 hook 桥 /gate，socket 预 bind deterministic 就绪）+ GateModal 双 source 渲染（tool_permission/agent_ask）+ 广播输家哨兵；壳无业务真相（事件流驱动渲染）+ 依赖单向铁律（grep 验证）；fold 进 hook_script.py sys.path 阴影 surgical 修复（phase 6 hook 桥 9 测试由此转绿）；79 单测净增，557 全绿（零回归）。**里程碑：Orca 已是可用 CLI 工具**。Commit: `69a905e`。详见 [release note](../releases/2026-06-30-phase7-cli.md)。

## [2026-07-01] 阶段 6 gates/ HMIL 层 —— HumanGate 统一原语（tool_permission + agent_ask 共模型）+ HumanGateHandler（request/resolve + _broadcaster 广播协程）+ PreToolUse hook HTTP 桥（stdlib only，安全优先 exit 2 语义）+ /gate & /gate/respond FastAPI 端点 + SessionContextRegistry（claude session_id → run_id/node 映射）+ ask_user；session_id 透传 event 顶层；36 单元 + 4 integration 测试，478 全绿（+36 净增，零回归）
- commit: `2edcefc`
- 详情：[release note](../releases/2026-06-30-phase6-gates.md)

## [2026-07-01] 阶段 5-R follow-up —— 集合 bug 修复（补 `tests/__init__.py` 让 `tests.run` 可绝对导入，三个 run 测试文件原本 collection 失败）+ code-review 修复（foreach `max_concurrent<1` 编译期 fail loud / `resolve_max_iter` 非法值 fail loud 不静默降级 / 补 parallel+foreach continue_on_error 部分失败聚合透传下游的端到端测试）；442 测试全绿（+7 净增），零回归
- commit: `7bf0f97`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)（§4.1 / §4.2）

## [2026-07-01] 阶段 5-R run/ 编排层 —— Orchestrator 单指针主循环（entry→…→$end）+ Router first-match-wins 纯函数 + ExecutorAdapter（executor AsyncIterator → bus.emit 拆四参桥接）+ parallel 组（asyncio.gather + 幂等 + failure_mode 三态）+ foreach（Semaphore + locals 注入 + 聚合）+ lifecycle（run_id / 生命周期事件 / max_iter）；扩展 RunContext 加 locals/task、ExecError 加 node 字段、validator 允许 inputs/parallel 组名作 Jinja2 root；9 demo 端到端（6 零 token + 3 agent）+ 439 测试全绿（353 基线 + 86 净增，零回归），5 条铁律全过
- commit: `6fa171b`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)

## [2026-06-30] 阶段 5-M schema 单轨化迁移 —— 废除 `Node.after` 双轨制，统一为 routes 单指针 + `ParallelGroup` 显式并行（diamond）；validator 9 项重排（删 ③⑤ after 校验，加 ⑩ parallel 组结构 / ⑪ 兜底 route 位置 / ⑬ entry 非组）；3 examples + 9 fixtures + 3 测试文件全改 + 文档全覆盖；353 测试全绿（323 基线 + 30 净增，零回归），零 after 字段残留
- commit: `f0d7e99`
- 详情：[release note](../releases/2026-06-30-phase5-migration.md)

## [2026-06-30] 阶段 4 exec/ 执行内核 —— Executor 接口（AsyncIterator[Event]）+ ClaudeExecutor（claude -p 子进程 + 真 translator）+ ScriptExecutor / SetExecutor + CLIRunner（asyncio subprocess + stdin pump + 超时 SIGTERM→SIGKILL）+ Jinja2 渲染；3 条架构决策覆盖（translator 归 profiles / seq 占位 / result_extractor 拆半），322 测试全绿（196 基线 + 126 新增，零回归）
- commit: `c891f75`（feat(exec): phase 4 执行内核 — ClaudeExecutor + ScriptExecutor + SetExecutor + CLIRunner + translator 真实现）
- 详情：[release note](../releases/2026-06-30-phase4-exec.md)

## [2026-06-30] 阶段 3 events/ + profiles/ + capability 校验闭环 —— Tape 唯一真相源（append-only JSONL + Lock 覆盖 seq+write+flush + resume 清残行）+ EventBus（异步 fan-out + session_id 透传）+ 幂等 reducer + CliProfile/ProviderCapabilities 命令替换层 + compile `_check_profiles`（⑨），195 测试全绿（103 基线 + 92 新增，零回归）
- commit: `1b86019`（feat(events): phase 3 事件层 + profiles 命令替换层 + capability 校验闭环）
- 详情：[release note](../releases/2026-06-30-phase3-events-profiles.md)

## [2026-06-30] 阶段 2 compile/ 解析校验层 —— YAML→Workflow + 两层校验（结构 pydantic + 语义 8 项 + warnings），103 测试全绿
- commit: `5b5ba06`（feat(compile): phase 2 解析与校验层）
- 详情：[release note](../releases/2026-06-30-phase2-compile.md)

## [2026-06-29] 阶段 1 schema/ 数据层 —— 纯数据结构地基（workflow/event/state），50 测试全绿
- commit: `d69c47c`（实现）+ `6d7dfea`（二次 review 修复：SPEC 25→21 + 测试加固）
- 详情：[release note](../releases/2026-06-29-phase1-schema.md)

