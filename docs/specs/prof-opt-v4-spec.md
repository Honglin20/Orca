# Prof-Opt v4 实现 SPEC

> **依据**：[`prof-opt-v4-design-draft.md`](prof-opt-v4-design-draft.md) v3（spec-review 轮 1+2 闭环 + UD-1/2/3 拍板）。本文件 = 实现级契约；草稿是语义权威，冲突时以草稿为准并回改本文件。
> **重构范围**：v3.5（10 节点）→ v4（**8 节点**）。删 `po_implement`/`po_verify` 节点；`po_baseline` 基线完整训练（**非阻塞** + finalizer 守护 + live 图）；`po_propose` 三 subagent 内闭环；`po_probe` stop-at-k + GPU 串行守卫；`po_full_train` 锚简化 + 对称终检；`po_gate`/`po_report` 骨架不变（report 增终态收割）。
> **执行器/命名/`_po_scripts` 定位机制**：继承 v3.5 SPEC header（`$(dirname "$ORCA_AGENT_RESOURCES")/_po_scripts`，禁 `ORCA_WORKFLOWS_ROOT`；in-session 宿主子代理执行，`executor` 不参与）。

---

## 1. 交付物清单

```
workflows/
├── prof-opt.yaml                      # 8 节点 DAG（回边 po_gate→po_propose 不变）
├── agents/
│   ├── _po_scripts/                   # 跨节点共享确定性脚本（flatten 期 deploy，继承）
│   │   ├── deploy_scripts.sh          # 不变（glob 全量部署 + 孤儿回收——新增脚本零改动、退役脚本自动退役）
│   │   ├── orca_inject/{sitecustomize.py,header.env}   # 不变
│   │   ├── assert_shadow.py           # 不变
│   │   ├── PROFILER_CONTRACT.md       # 不变
│   │   ├── placeholder_profiler.py    # 保留（真 profiler 未到位的默认；v2 搁置物）
│   │   ├── mfu_benchmark.py           # 保留（v2 搁置物，无 v4 消费者）
│   │   ├── analyze.py                 # 不变
│   │   ├── predict_delta.py           # 不变
│   │   ├── gen_export_onnx.py         # 不变
│   │   ├── render_run.sh              # 增：header 组装层 PYTHONUNBUFFERED=1 token（D-V4-16）
│   │   ├── diff_check.py              # 不变
│   │   ├── advance_round.py           # 不变
│   │   ├── gate_decide.py             # 不变
│   │   ├── gate_node.sh               # 不变 + D-V4-15 引号修复（bash -n 必须过）
│   │   ├── emit_result.py             # 不变
│   │   ├── history_lib.py             # builder 机制不变；PROBE_FIELDS 增可选 eval_skipped_no_epoch_ckpt / monitor_failed / eval_acc / eval_failed（未知字段仍 fail loud；去重 probe 配置重试指纹键同步新字段）
│   │   ├── metric_curve.py            # compare 增可选 --at-epoch k（任一曲线缺第 k 点 fail loud；不传行为不变）；compare 输出**无条件**增 at_epoch（实际比较深度）+ baseline_path（锚来源）两字段
│   │   ├── experiment_ledger.py       # 不变
│   │   ├── dashboard_snapshot.py      # 不变
│   │   ├── stop_at_epoch.sh           # 【新增·D-V4-3】幂等单次检查：--log/--contract(=contracts.json 路径，与 metric_curve extract 同源读 metric 正则——单源，禁独立 --pattern；前提 = 正则数字组后行尾/非数字界锚定)/--stop-epoch/--pid-file(组长)；epoch≥k 首现 → /proc cmdline 归属校验 → kill -TERM -<pid>（进程组）→ 10s 宽限 → KILL → **重解析冻结日志，stopped_at_epoch = 终态时刻可解析最大完整 epoch（≥k，非恒 k）** → stop_status.json{killed, stopped_at_epoch, rc:null}；worker 自然结束 → {natural_done, ..., rc}；stop_status 已存在且 killed → 幂等返回
│   │   ├── check_bottleneck.py        # 【新增】bottleneck_analysis.json 校验：封闭 schema + referential（top_bottlenecks[i]={name:pattern_id, op_type, cycles:total_cycles} = analyze.py hot_patterns **保序子集**，非前缀）+ 排序/rank 一致 + base_report 存在可解析
│   │   ├── push_curves.py             # 【新增·D-V4-2b】sidecar 幂等 best-effort：读 baseline_metrics.jsonl + variants/*/metrics 曲线 → render_chart 单张 live 折线（hue=baseline/vid, x=epoch, y=metric）；**socket connect/send 各 ≤5s 硬超时**（超时 = stderr + 退出 0，防挂起拖死 worker）；每次成功推送追加审计行 {ts, baseline_epochs, curves:[...]} 到 $ORCA_ARTIFACTS_DIR/.chart_push.log；ORCA_CHART_SOCK 缺失 → 静默退出 0
│   │   └── verdict_decide.py          # 【新增·洁净回卷】两处内联 python3 -c 判定沉脚本（gate_decide 范式）：promote（probe 终判——promote line 重算自 epoch_compare.json 记录的 baseline_metric + contracts 方向；eval 双门仅当 proxy.json 与 baseline_k_acc.json 双在，缺一 → 曲线单判）/ final-budget（full_train within_budget——回读 final_acc.json 的 final_acc/baseline_full_acc/metric_direction）；--budget ≥0；坏输入 fail loud exit 2
│   │   （【退役删除】perturb_ckpt.py）
│   ├── po_flatten/       { agent.md, scripts/{reuse_check.sh, check_flatten.sh, extract_user_pkg.sh} }   # 不变；extract_user_pkg.sh 洁净回卷——实参改 <project_root> <model_path>（脚本内解析相对/绝对路径）+ fail-loud（去静默 || true）
│   ├── po_contract/      { agent.md, scripts/{check_contracts.sh} }   # 增量见 §4
│   ├── po_baseline/      { agent.md, scripts/{run_baseline_chain.sh, check_business_logic.sh} }   # 【重构】非阻塞链 + finalizer 守护 + 五段标题级检查
│   ├── po_propose/       { agent.md, scripts/{run_latency_recheck.sh, check_prerequisites.sh}, references/{structural-levers.md} }   # 【重构】；run_latency_recheck = 原 po_verify/scripts/run_verify.sh 迁移改名（331 行确定性逻辑，阈值实参 100/1/0.5 调用行显式；skip 键 = verdict.json 存在性——打回修复后复测前必须删该 vid verdict.json）；check_prerequisites = 入口部署完整性检查（洁净回卷抽自 agent.md Resource Anchors 内联块）
│   ├── po_probe/         { agent.md, references/{probe_protocol.md} }   # 渲染/锚/守卫语义更新
│   ├── po_full_train/    { agent.md, references/{full_train_protocol.md} }   # 锚简化 + 对称终检
│   └── po_report/        { agent.md, references/{report_format.md} }   # 四处改写 + 终态收割 + finalize chart
│   （【退役删除】po_implement/、po_verify/、po_gate/ 三目录）
└── subagents/prof-opt/
    ├── memory-verifier.md             # 不变
    ├── paradigm-verifier.md           # 白名单更新至 v4 语义（预算压缩条目删除）
    ├── business-logic-analyst.md      # 【新增】
    ├── bottleneck-analyst.md          # 【新增】
    ├── structure-proposer.md          # 【新增】
    └── variant-implementer.md         # 【新增】
tests/
├── test_po_scripts.py                 # 改：baseline 链新语义（非阻塞/finalizer 产物/指纹）/check_contracts 新断言/history_lib PROBE_FIELDS 字段全集更新；增：stop_at_epoch 全分支（组 kill/幂等/宽限/**实际深度重解析**/stop_status/pid 归属拒绝）、check_bottleneck（保序子集+referential+排序）、run_latency_recheck 迁移回归（含删 verdict.json 钉）、gate_node.sh bash -n 冒烟、push_curves（幂等/缺 sock 静默/半写行跳过/超时不挂起）、metric_curve --at-epoch（缺第 k 点 fail loud / 不传行为不变 / **曲线多点时强制取 k**）、eval@k 失败重派 1 次再降级分支（机械面：fail-loud 产物路径；重派判定控制流归 E2E）
├── test_po_diff_check.py              # 不变
└── test_po_inject.py                  # 不变
docs/specs/prof-opt-v2-design-draft.md  # 文件头加 superseded 标注
```

## 2. prof-opt.yaml 契约

- **inputs**：**12 个全保留**，三处 description 更新（名/类型/default 零变更）：`probe_epochs`（变体短训深度 k）、`full_train_epoch_cap`（基线与 winner 共用上限，生效值落 full_train_budget）、`profile_script_path`（去 mfu_adapter 措辞，"外部脚本 onnx 进四件套出"直述）。
- **nodes**（8 个）：
  - `po_flatten`/`po_contract`：schema 不变（新事实进 contracts.json）。
  - `po_baseline` schema（属性级）——`required: [status, base_onnx, makespan_cycles, baseline_metrics, business_logic_path, profile_dir, bottleneck_report, error, generated_artifacts]`：
    - `status`: string enum `[executed, failed]`；executed = 早期链全过 + 训练与 finalizer 双存活确认 + business_logic.md 落盘（**不含训练完成**）
    - `base_onnx`: string（base/model.onnx；失败空串）；`makespan_cycles`: integer（失败 0）
    - `baseline_metrics`: string（曲线 JSONL 路径；emit 时可为部分曲线；失败空串）
    - `business_logic_path`: string（baseline/business_logic.md；失败空串）
    - `profile_dir`/`bottleneck_report`: string（失败空串）；`error`: string（成功空串）
    - `generated_artifacts`: array[string]（相对 $ORCA_ARTIFACTS_DIR）
    - 删 `baseline_proxy_acc`/`baseline_ref_acc`（baseline_full_acc/baseline_k_acc 均为盘面文件非节点 output）
    - 路由：`status == 'executed'` → po_propose；catch-all → po_report
  - `po_propose` schema（属性级）——`required: [status, proposals_count, exhausted, implemented, skipped, latency_pass_count, verdicts_path, proposals_path, error, generated_artifacts]`：
    - `status`: string enum `[executed, failed]`，description 钉 **"failed ⇔ error 非空"**
    - `proposals_count`: integer ≥0；`exhausted`: boolean；`latency_pass_count`: integer ≥0
    - `implemented`: array[string]（DONE 落盘 vid）；`skipped`: array of `{vid: string, reason: string, outcome?: string}`（沿用 v3.5 po_implement 语义）
    - `verdicts_path`/`proposals_path`: string（失败空串）；`error`: string；`generated_artifacts`: array[string]
    - 路由：`status == 'executed'` → po_probe；catch-all → po_report
  - `po_probe`：schema 不变。
  - `po_gate`：`kind: script` 不变。
  - `po_full_train`：`baseline_full_acc_source` enum = `["baseline", null]`；其余不变。
  - `po_report`：字段不变；`stage` enum **收缩**为 `[flatten, contract, baseline, propose, probe, gate, full-train, report]`。
- **routes/outputs**：回边不变；outputs 全读 `{{ po_report.output }}`。**总则**：nodes 侧 description 与注释块随 v4 语义同步重写，禁保留 ref-input / auto-trained / 懒补训 / epoch-only proxy 等已删机制措辞。
- `description` 更新（产品说明书式；含准入条款一句话："训练须按给定轮数精确执行"）。

## 3. 节点 agent.md 契约（通用骨架继承 v3.5 SPEC §3）

**7 个 agent.md**（po_gate 是 script 节点）全部含：frontmatter / Resource Anchors / Path Iron Rules / Subagent Call Protocol（仅声明实际 dispatch：flatten→memory-verifier；contract→paradigm-verifier；baseline→business-logic-analyst；propose→bottleneck-analyst+structure-proposer+variant-implementer；probe/full_train/report 不派）/ Lazy Loading / Workflow 步骤 / Validation / Output。节点本地 `scripts/`（check_business_logic.sh / run_latency_recheck.sh / check_prerequisites.sh）经 `$ORCA_AGENT_RESOURCES` 直跑。prompt 洁净 = 强制验收（§6）。

## 4. 各节点专属契约（草稿锚 + 实现增量）

| 节点 | 草稿锚 | 实现增量 |
|---|---|---|
| po_flatten | v3.5 §3.1 | 零改动 |
| po_contract | §4/D-V4-19 | 指标格式实测快跑统一 **≥2 epochs**（一跑双用：epoch 行格式 + ckpt 行为）；**早停 best-effort 早拒**（argparse/config 扫描 + 实跑观察，检出 → viable=false 归因披露）；metric pattern 钉"数字组后须紧跟行尾/非数字界"；快跑 best-effort 断言实跑 == 渲染；contracts 增 `train.ckpt_output_rule`（三态描述）+ `train.ckpt_per_epoch`（可寻址布尔）+ `full_train_budget{epochs, seed, data:{dataset_knob:null, data_value:null}}`（值级指纹 UD-2）；`probe_cap_mechanism="stop-at-k"`；**reuse 路径 contracts 缺 v4 新字段 → fail loud + fresh_start 提示**；**contracts.json 顶层 `reason` 含准入条款句**（"训练须按给定轮数精确执行，自带 early-stopping 项目不在范围"——条款句原文以 po_contract/agent.md 为唯一源，check_contracts.sh 以常量子串校验其在场）；check_contracts.sh 断言变更：接受 stop-at-k / epoch-only 全 null 保留 / 新字段校验 / probe-full 若分模板须渲染自同一文件且断言数据管线一致 |
| po_baseline | §2.1/D-V4-2 | `run_baseline_chain.sh` **重构为非阻塞**（v3.5 step1 reference 交叉核对退役——warning-only 信息步骤，v4 无 input 入口且精度锚自产）：①pristine 快照 + 导出 + profile + analyze（**保留 pristine 快照**——结构对账锚）②完整训练 launch：同 train 契约模板、`--out baseline/train.rendered.sh` 显式、`--set epochs=<full_train_budget.epochs>`、**wrapper 组长不 exec**（`setsid bash -c 'echo $$ > pid; bash train.rendered.sh; echo $? > rc'`——pid/rc 各有写者）、PYTHONUNBUFFERED（render 层）③**finalizer 守护 launch**（setsid + `baseline/finalizer.pid` + `baseline/finalizer.log`，**日志行首一律 ISO8601 UTC** `date -u +%FT%TZ`）④存活确认（训练 pid 活 + finalizer.pid 活 + train.log 出现，一律 /proc cmdline 归属校验）→ emit。**finalizer 守护契约**：轮询训练 pid/rc（无 rc 死亡重派 ≤3 + per-attempt train.log 命名 + 按 train 契约 resume 规则 wipe partial out-dir）；每 poll 周期 = 增量 extract（**从当前 attempt train.log 全量重 derive、内容有变化才原子替换** baseline_metrics.jsonl）+ `push_curves.py`（best-effort）+ alive 心跳行（ISO8601 UTC + 曲线点数）；rc=0 收尾链（每步写 stage 行）：终检（`--expected-epochs` = full 生效值；实跑 ≠ 渲染 → train_final{failed, stage: final_check}，文案指向准入条款）→ 末 ckpt eval → `baseline_full_acc.json`（值+ckpt+full_train_budget 指纹，verify_anchor_budget 范式防陈旧）→ ckpt 可寻址 → 第 k ckpt eval → `baseline_k_acc.json`（值+k+指纹）→ `baseline/train_final.json{status: done\|failed, rc, stage}`；**任何内部步骤失败 → 尽力写 train_final{failed} 再退**；`baseline_status.md` 跨 turn 真相源；business-logic-analyst 训练启动后 dispatch（并行）；`check_business_logic.sh`（存在/非空/哨兵/五段标题）= Validation 必查 |
| po_propose | §3/D-V4-7 | Step 0 reuse：proposals.json 存在且可解析 → 跳过 Step 3 **从 Step 4 续做**（DONE marker 逐提案幂等）→ Step 5 照跑；Step 2 stamp 键 = base 版本标识（best.vid / base onnx sha）+ 机械报告内容指纹（非轮号）；Step 3 机械闸过滤后 count==0 → exhausted 强制 true；exhausted=true ⇒ exhausted_rationale 结构化非空（≥1 已尝试方向条目）进 Validation；**Step 4 每提案完成/终态跳过后由节点侧机械补写 history IMPL 行**（append_implemented builder；terminal-skip 两步 append + reconciliation）——IMPL 行的 {round, change_sig} 是 advance_round 轮过滤与永久去重的唯一数据源，该职责随 po_implement 退役内聚到 po_propose（计划轮 2 BLOCKER 闭环）；配额 4→3 显式；Step 5 `run_latency_recheck.sh`（阈值实参 100/1/0.5 显式）+ 打回修复后复测前**删该 vid verdict.json**；**真 profiler 条件守卫**：`profile_script_path` 非空 → Step 5 前置等基线 worker 退出（placeholder 默认空不等）；三 subagent 失败矩阵（校验败/超配额/产物缺失 → 重派 1 次 → error 披露） |
| po_probe | §2.1/2.3/D-V4-2 | **GPU 串行守卫（探测目标 = finalizer.pid，生存期 ⊇ 训练）四象限**：活 → bounded-wait（单调用 ≤480s + status message 续驱；停滞判据 = 训练活期 train.log mtime 与曲线点数均停滞 ≥30min → stalled error；finalizer 期 finalizer.log 停滞 ≥30min → error）/ 死 + train_final=done → 放行 / 死 + failed → error 路由 po_report / 死 + 缺失 → error fail loud；每变体：渲染同模板 `--out variants/<vid>/train/train.rendered.sh` + `--set epochs=<full 生效值>` + shadow_dir → 变体影子 → detach → bounded-poll（**poll 间隔 ≤30s**，probe_protocol 钉死）反复调 `stop_at_epoch.sh --stop-epoch k --contract contracts.json`（State derivation 增"stop_status 未出且组活 → 继续调"分支）→ 曲线 extract（`--expected-epochs` = stopped_at_epoch）→ `metric_curve compare --at-epoch k` vs baseline_metrics.jsonl；ckpt 可寻址 → 第 k ckpt eval vs baseline_k_acc **双过才 promote**（**eval 加载失败 → 重派 1 次，仍败 → `eval_failed: true` + eval_acc=null + 按曲线单判路径 + 披露**）；不可寻址 → 曲线单判 + `eval_skipped_no_epoch_ckpt: true`；natural_done 且轮数 > k → `monitor_failed: true`；probe 行 `proxy_acc` 恒填曲线@k 值、eval 值置 `eval_acc`；等待循环内 push_curves sidecar；advance_round/禁二次 detach/probe_status.md 继承 |
| po_gate | §4 | 零改动 |
| po_full_train | §4/D-V4-11 | 删 baseline/full_train/ 路径与第二 pid 键；锚 = baseline_full_acc.json + 指纹逐字段校验 + 防御性 train_final=done 检查；winner 同模板 `--out final/train.rendered.sh` + full_train_budget 同指纹；**winner 训练终了对称终检实跑 == full**（不符 → status=failed 归因）；`baseline_full_acc_source` 恒 "baseline"（failed null）；常驻机制继承 |
| po_report | §4 | **终态收割**：emit 前读 finalizer.pid——死直过 / 活 bounded 等 ≤60s / 到点无终态 → 双组 kill（训练组[读 baseline pid 文件] + finalizer 组）+ 扫 `variants/*/` 在飞 pid 一并 kill + 披露 "aborted at terminal"；report_format.md 四处改写：终态表 row3 读 baseline_full_acc.json **三态判定**（train_final 缺失 → null+披露 / failed → 归因 / done → 读盘）、ref_acc 删 baseline_ref.json 优先级直读 baseline_full_acc、Fairness Note 轮数读 full_train_budget.epochs、内归因 implement/verify 并入 propose（DONE/verdict/history 三态）；`baseline.proxy_acc` = 曲线@k（不足 → null+披露）；finalize chart：push_curves.py title `(final)`（终稿推送兜底）+ 每轮 makespan 趋势（best-effort）；写回断言：≥1 promoted 时继承 v3.5 全套；no-promotion 终态 → 零写回 + 披露（非失败）；其余继承 |

## 5. subagent 契约（point-to-file + sentinel，骨架同 memory-verifier）

| subagent | 输入 | 输出（写盘 + 首行哨兵） | 节点侧校验 |
|---|---|---|---|
| business-logic-analyst | project_manifest.md + shadow 模型源码 + contracts.model_facts | `baseline/business_logic.md` 五段（任务语义/输入输出/架构动机/逐模块职责与物理意义/训练目标与指标方向） | `check_business_logic.sh`（存在+非空+哨兵+五段标题） |
| bottleneck-analyst | base/profile/ 四件套 + 全部原始产物 + bottleneck_report.json | `base/bottleneck_analysis.json`（零重复机械字段；保序子集映射） | `check_bottleneck.py`；失败重派 1 次仍败 → error |
| structure-proposer | business_logic.md + bottleneck_analysis.json + history.jsonl + references/structural-levers.md | `rounds/<NNN>/proposals.json`（≤3；rationale/op_delta/edited_files/change_spec/sota_reference；exhausted_rationale 结构化） | 机械准入三闸 + count ≤3 + 去重对账 + rationale 校验 |
| variant-implementer | proposals.json + base shadow + 导出模板 | 逐提案 declaration.json + DONE（或 skipped）；compact 摘要 | diff_check 文件层 + DONE 存在性 + 配额轨迹落盘 |

## 6. 洁净度验收（实现完成后，**每节点单独一个 reviewer agent**——用户要求）

- **7 个 agent.md + 6 个 subagent md + 4 个 references**（structural-levers/probe_protocol/full_train_protocol/report_format）逐一单独分 agent 审查（受众翻转通读）。
- **审查记录落盘仓库内 `verify/cleanliness/<file>.md`**（跨 run 可复核）；**合格判据** = 逐段受众翻转结论 + 行号引用 findings 或显式零 finding（缺任一 = 该文件不通过）。
- `tars validate workflows/prof-opt.yaml` warning 清零。
- 残留 grep（范围 = `workflows/agents/ + workflows/subagents/prof-opt/ + workflows/prof-opt.yaml`，命中 = 0）：v3.5 词表（`mnist_kd`、`playground`、`prof_opt_demo`、ns3/psu/kd-nas/nas-supernet 词边界、`prof-opt-design-draft|prof-opt-v2-design-draft|prof-opt-v4-design-draft`、`docs/specs`、`D:\?Projects`、`/mnt/d`、`spec-review`、`SPEC-R1`）+ 增补退役物（`run_verify`、`baseline_proxy_acc`、`baseline_ref`、`mfu_adapter`、`perturb_ckpt`、`playbook`、`ref-input`、`auto-trained`）。

## 7. E2E 验收（claude 后端 + tars skill，两项目；驱动形态继承 v3.5 SPEC §7）

- **inputs 钉**：`full_train_epoch_cap=2, probe_epochs=1`（k<full 使停止判据非退化；秒级训练下 kill 可能落在自然结束之后——严格 kill 语义归单测，E2E 侧以 stop_status 终态计数披露行使性）、`max_rounds=2`、`latency_reduction_min` 显式传 0.3~0.5（强制 loop 至少一轮，防 round 1 即 full-train 空转验收——v3.5 同策略）。placeholder 场景阈值调整落点 = `run_latency_recheck.sh` 调用行实参（**非 input**）。
- **machinery 验收线（条件要求）**：8 节点预期终态清单（含 no-promotion 分支：gate finish-failed → report 正常收口）；`baseline_full_acc.json` + `train_final.json` 恒在（适用判定 = 终态收割时 train_final 存在且 status=done；否则由 row3 三态 + 披露覆盖——合计全路径）；`baseline_k_acc.json` 仅 `ckpt_per_epoch=true` 时在，否则 history 含 `eval_skipped_no_epoch_ckpt`；`best.json` 仅 ≥1 promoted 时在。**两 E2E 项目均非可寻址 ckpt 时 eval@k 路径仅单测覆盖**（显式声明，非缺口）。
- **内容验收线**：
  - `target`：①三类 rendered 文件 `--set` 除 **{out_dir, vid, shadow_dir}** 外逐字段一致 ②stop_status.json 存在 + stopped_at_epoch ≥ k + monitor_failed 时披露在场 + 报告披露 stop_status 终态计数（killed/natural_done 分布——行使性披露，正确性归单测）③epoch_compare.json **`at_epoch` == k** 且 **`baseline_path` = baseline/baseline_metrics.jsonl** ④变体曲线来自变体自身 train.log + 有 ckpt 时哈希互异 ⑤business_logic.md 五段非空 ⑥bottleneck_analysis 过 referential ⑦promote 真实执行 + 非退化 + 写回断言（≥1 promoted 时；否则零写回+披露）⑧**非阻塞证据**：finalizer.log 存在 timestamp > tape(po_baseline.node_completed) 的任意带时间戳行（心跳 ∪ stage，**行首 ISO8601 UTC 同格式解析比较**）；若 mtime(train_final) ≤ tape(node_completed) 则降级为存在 timestamp > tape(node_started) 的行 + 披露 N/A（非失败；E2E 报告披露实际走了主断言还是降级分支）⑨**live 图**：ORCA_CHART_SOCK 在场（判定 = env 非空 + `test -S $ORCA_CHART_SOCK`）时 `.chart_push.log` 非空且末行 baseline_epochs == 曲线终稿点数（report finalize 终稿推送兜底 + report 幂等重入 finalize 复验；断言失败归因二分：环境失败修环境重跑 vs workflow 缺陷，末行陈旧归环境侧）。
  - `mnist_kd`：Tier B(train)/A(eval)（Tier 事实锚 = v3.5 SPEC `docs/specs/prof-opt-spec.md` §7）+ 子 agent 提案路径；合法 exhausted 可接受（exhausted_rationale 非空可复核）；有提案同 target ①-④。
- **通用规则铁律**：修复只落 workflow 通用逻辑，禁项目特判。

## 8. 流程与回卷规则

SPEC → spec-review（对抗闭环）→ 确认闸 → 计划 → 实现（coder）→ **逐节点洁净审查（每节点单独 agent + 记录落盘 verify/cleanliness/）** → 单测全绿 → E2E（test-agent）→ 通用修复循环 → 终态报告 + 收口。
实现期发现草稿级语义问题：回卷草稿（变更记附 A），不静默偏移；**spec 每次实质变更必须重新过确认闸**。任务完成收口 = release note → CHANGELOG 索引 → CURRENT.md 更新（项目规约三件套）。
