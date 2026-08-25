# Prof-Opt 实现 SPEC

> **依据**：[`prof-opt-design-draft.md`](prof-opt-design-draft.md) v3.5（三轮对抗审查闭环 + v3.5 训练范式切换：一律从头训练、proxy 公平对比、pre-trained 仅参考）。本文件 = 实现级契约：文件清单、每文件职责、脚本 IO 契约、验收标准。草稿是语义权威，本 SPEC 是落地映射；冲突时以草稿为准并回改本文件。
> **执行器**：全节点 `kind: agent`。**in-session 下节点由宿主 session 派的子代理执行，`executor` 字段不参与节点执行**（`in_session/cli.py:126-128`）；claude 宿主（cc 家族）+ 全 agent 节点即成立。`model` 字段省略已核实可行（`exec/claude/executor.py:326-327` 仅显式才传；SPEC-review 轮销账）。
> **命名**：workflow `prof-opt`；节点 `po_*`；agent 目录 `workflows/agents/po_*/`；共享脚本 `workflows/agents/_po_scripts/`；磁盘 marker/chart label/artifacts 全链统一 `po_`/`prof-opt` 前缀。
> **`_po_scripts` 定位机制（SPEC-R1-B1，硬契约）**：in-session **无 `ORCA_WORKFLOWS_ROOT` env**——各 agent 一律以 `$(dirname "$ORCA_AGENT_RESOURCES")/_po_scripts` 定位（`ORCA_AGENT_RESOURCES` 两条执行路径均注入且为 folder-agent 绝对路径）；agent.md 写成守卫式（解析失败即 fail loud exit 2），**禁用 `ORCA_WORKFLOWS_ROOT`**。

---

## 1. 交付物清单

```
workflows/
├── prof-opt.yaml                      # 10 节点 DAG（含回边 po_gate→po_propose）
├── agents/
│   ├── _po_scripts/                   # 跨节点共享确定性脚本（canonical 源，flatten 期 deploy 到 $ORCA_ARTIFACTS_DIR/scripts/；定位见 header）
│   │   ├── deploy_scripts.sh          # 部署本目录（幂等）：*.py/*.sh → $ORCA_ARTIFACTS_DIR/scripts/；orca_inject/ → $ORCA_ARTIFACTS_DIR/orca_inject/（artifacts 根，非 scripts/ 内——运行模板 PYTHONPATH 指向根）
│   │   ├── orca_inject/sitecustomize.py        # meta path finder（草稿 §2.3 逐字实现，含 stdlib guard）
│   │   ├── orca_inject/header.env     # 注入头静态片段（ORCA_SHADOW_*/PYTHONPATH 骨架，os.pathsep 渲染点）
│   │   ├── assert_shadow.py           # 运行时断言（遍历全部 ORCA_SHADOW_PKGS）
│   │   ├── PROFILER_CONTRACT.md       # 四件套字段级 schema + cost 量纲 + min_improvement_cycles 配套约定（对外契约——profile_script_path 替换者依此实现；SPEC-R1-M4）
│   │   ├── placeholder_profiler.py    # 依 PROFILER_CONTRACT.md 实现（rc≠0+stderr 列 unsupported op）；op 覆盖面必须 ⊇ 两 E2E 项目导出 op 集（E2E 前置检查）
│   │   ├── analyze.py                 # 瓶颈分析器 → bottleneck_report.json（pipeline 分解/关键路径/热点模式/cost table；未知键 fail loud）
│   │   ├── predict_delta.py           # op_delta×cost_table → predicted_delta_cycles + change_sig.params 归一化
│   │   ├── perturb_ckpt.py            # 确定性 ckpt 扰动副本工具（通用工具保留；v3.5 起双 ckpt 联动主来源 = 两个不同 seed 的随机初始化）
│   │   ├── gen_export_onnx.py         # export 脚本生成器（读 contracts 模型构造事实 → 产 artifacts 根 export_onnx.py；生成物 sha256 入 contracts.json——SPEC-R1-M11）
│   │   ├── render_run.sh              # run 模板渲染（--template --set k=v；注入头/断言/sys.executable 由模板+header.env 组装，LLM 只选参不手抄——SPEC-R1-M3）
│   │   ├── diff_check.py              # 两层声明校验（文件/图，参照恒当前 base，排除 __pycache__；v3.5 权重层随继承退役）
│   │   ├── advance_round.py           # 轮末原子推进（幂等键 = marker.round == rounds/ 最大编号，不等则重放——SPEC-R1-M6 回卷草稿；平局字段 proxy_acc）
│   │   ├── gate_decide.py             # 纯读现算 decision + 轮数硬帽（草稿 §7.5；best.proxy_acc）
│   │   ├── emit_result.py             # 单行 JSON emit（stdout 纯 JSON / 日志走 stderr 硬契约）
│   │   └── history_lib.py             # history.jsonl：**唯一写入口 = 分类型 builder**（append_implemented/append_latency/append_probe，裸 dict append 禁止——SPEC-R1-M10）+ 读法/去重规则（草稿 §5.3 v3.5：永久去重集 = {promoted, unsupported_op}；probe 行字段 proxy_acc）
│   ├── po_flatten/       { agent.md, scripts/{reuse_check.sh, check_flatten.sh, extract_user_pkg.sh} }
│   ├── po_contract/      { agent.md, scripts/{check_contracts.sh} }
│   ├── po_baseline/      { agent.md, scripts/{run_baseline_chain.sh} }
│   ├── po_propose/       { agent.md, references/{playbook.md} }
│   ├── po_implement/     { agent.md, references/{variant_implementation.md} }
│   ├── po_verify/        { agent.md, scripts/{run_verify.sh} }
│   ├── po_probe/         { agent.md, references/{probe_protocol.md} }
│   ├── po_gate/          { agent.md, scripts/{run_gate.sh} }
│   ├── po_full_train/    { agent.md, references/{full_train_protocol.md} }
│   └── po_report/        { agent.md, references/{report_format.md} }
└── subagents/prof-opt/
    ├── memory-verifier.md             # ns3 同款协议（manifest 事实复核，哨兵字段格式）
    └── paradigm-verifier.md           # Tier B porter 保真复核：清单 = loss/optimizer/scheduler/数据流/metric/eval 入口逐项比对 porter 产物 vs 用户源；不过 → 修一轮仍不过 → Tier C fail loud（SPEC-R1-M12）
tests/
├── test_po_backedge_routing.py        # P0-lite：回边 DAG 的 compile+route 单测（orchestrator 路径，无 LLM）
├── test_po_scripts.py                 # analyze/predict_delta/gate_decide/advance_round/history_lib 单测（advance 含 r1→r2 序列断言 base/shadow 二次替换；history_lib 含 builder 字段全集断言）
├── test_po_inject.py                  # sitecustomize meta finder 注入单测（script 形态 + stdlib guard + 裸模块闭包 + -S fail loud）
└── test_po_diff_check.py              # 两层声明校验单测（含 __pycache__ 排除/参照恒 base/退役 weights 层 fail loud）
```

**playbook.md 内容契约**（草稿 §9.1 v3.5）：三杠杆（激活替换/归一化结构/计算搬移[v1 仅零参数条目]），每条目 = 结构模板 + 硬件依据 + 精度风险 + SOTA 引用 + **融合单算子导出形态触发条款**（v3.4）；**weight_delta 维度整体删除**（从头训练不关心参数形状变化）；融合 LN 条目 = 直接删除 LayerNormalization 节点（无需 γβ 折叠）；深宽/低秩机制解锁、条目渐进收录（v1 不列条目不许提案）。产品说明书式，零开发残留。

---

## 2. prof-opt.yaml 契约

- **inputs**：草稿 §10 全表逐字落（含 default 与档位标签）+ **`full_train_epoch_cap` [advanced] default ""**（空 = 不截断；非空 = min(cap, train_epochs_full)——通用预算阀非项目特判，SPEC-R1-U2a，草稿 §10 已同步）+ **`min_improvement_pct` [advanced] default 1**（v3.4：时延 gate = max(min_improvement_cycles, min_improvement_pct% × base)——placeholder 估算器下 makespan = 全图总 cycles 且 MatMul 常占大头，1% 硬门槛会全灭结构性小赢，开放为可调；真 profiler 维持默认，草稿 §10 已同步）；**v3.5**：`pretrained_ckpt` [ask]→[advanced] default ""（仅参考不进 gate）、+`baseline_ref_acc` [advanced] default ""（final 锚；缺省自动满训基线一次并缓存）、**删 `catastrophic_floor`**（L1 零样本随范式退役）；不设 `iterations`。
- **nodes**：草稿 §1 十节点，全部 `kind: agent` / `executor: claude`；output_schema = 草稿 §11 逐字（字段名/enum/nullable 不得增删改）。
- **routes**：草稿 §11 路由清单逐字（顺序敏感 first-match-wins；每节点 catch-all → po_report；`po_gate.decision == 'loop' → po_propose` 为回边）。
- **outputs**：全读 `{{ po_report.output }}`（草稿 §11 末条）。
- workflow `description`：产品说明书式一句话（预训练模型 + 硬件 profiling 证据驱动的模型结构优化闭环），无内部术语。

## 3. 节点 agent.md 契约（通用骨架，全部 10 个）

每个 agent.md 必含（ns3_flatten 骨架同构）：
1. frontmatter `description`（产品语义）+ `tools`（bash/read/write/edit/glob/grep/task）
2. Resource Anchors：`$ORCA_AGENT_RESOURCES` / `$ORCA_ARTIFACTS_DIR` / `{{ inputs.* }}`（cwd-independent，先 cd 再跑）
3. Path Handling Iron Rules（pathlib 强制）
4. Subagent Call Protocol（point-to-file，仅声明本节点实际调用的 subagent：flatten→memory-verifier；contract→paradigm-verifier；其余节点不派）
5. Lazy Loading（不预读 reference）
6. Workflow 步骤（节点专属，见 §4）
7. Validation（本节点 check 脚本 gate + fix-loop ≤3 + fail loud 出口）
8. Output（output_schema 单行 JSON，emit_result.py 产出）

**agent prompt 洁净**（强制验收）：无 plan/issue 编号、无 Orca 源码路径、无内部 examples 路径、无测试项目名硬编码（mnist_kd/target 等禁止出现）；按 [`agent-prompt-cleanliness-contract`](../../orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md) 受众翻转通读。

## 4. 各节点专属契约（语义引用草稿，只列实现增量）

| 节点 | 草稿锚 | 实现增量 |
|---|---|---|
| po_flatten | §3.1 | 复用 ns3_flatten 骨架；增：`deploy_scripts.sh` 落 scripts/+orca_inject/、shadow_pkgs 机械枚举、stdlib 撞名预检、BASELINE.lock 写入（`{py_files_sha256, ckpt_sha256, model_path, pretrained_ckpt}`，py 限 shadow 闭包；**v3.5：pretrained_ckpt 可选仅参考——提供才记录 path+sha，缺省两字段空串**）、.run_lock（每次 Validation 步首 touch 心跳 + mtime 阈值 30min 或 pid 存在性判活——SPEC-R1-m5）；就绪检查 3 = `pretrained_loadable`（提供时 torch.load 即过，信息性无 strict 比对；未提供恒真）；**memory-verifier 报告落盘入 generated_artifacts**（`verify/memory_verifier_report.md`，首行哨兵；文件缺失/哨兵不符 = 按未复核 fail loud——v3.4 P4） |
| po_contract | §3.2 | 三契约实测（v3.5：eval 双 ckpt 联动的两 ckpt = **两个不同 seed 的随机初始化**——`contract_work/make_dual_ckpt.py` 直接实例化存 state_dict[seed 0/1]，容器形态随 eval 入口；契约期不再预评精度）；train 契约参数需求 = ①epochs ③out-dir ④截断 ⑥**数据子集/限量旋钮发现（有则必用，无则退纯 epochs）**；**实测义务含**：用户自有 sitecustomize 合并披露、`-S`/`-E` 旗标实测（发现即 fail loud——SPEC-R1-m6）；Tier A/B/C 判定（B = porter 生成 **train_proxy_entry.py**/eval_entry.py + paradigm-verifier 复核[§1 协议]，不过修一轮仍不过 → Tier C；C = viable=false）；**paradigm-verifier 报告落盘入 generated_artifacts**（每入口一文件 `verify/paradigm_verifier_report_<train|eval>.md`，首行哨兵；缺失/不符 = 按未复核 fail loud——v3.4 P4）；contracts.json schema = 草稿 §3.2 产出字段 + **`proxy_budget{epochs,dataset_knob,data_value,max_steps,seed}`**（选定理由落 `contract_work/proxy_budget_selection.json`——基线与变体共用单一来源）+ train/eval 入口 sha256 + `eval.ckpt_container` + **export_onnx.py 生成物 sha256**（`gen_export_onnx.py` 产，落 artifacts 根——SPEC-R1-M11）；run 四模板经 `render_run.sh` 组装（模板 + header.env + assert_shadow + sys.executable；LLM 只选参不手抄注入头）；训练模板**无 ckpt token**（从头训练）、probe 模板按旋钮发现含 `<<data_value>>` |
| po_baseline | §3.3 | `run_baseline_chain.sh` 七步链（①-⑦）逐步幂等 + baseline_status.md 跨 turn 状态；**② 含 pristine shadow 快照 → `baseline/original_shadow/`**（round-0 结构，供 final 期自动满训基线）；⑤ = `baseline/baseline_ref.json`（input 值或空标记）；⑥ = 基线 proxy **从头训练**（渲染读 contracts.proxy_budget 逐字段同值——公平不变量）→ `baseline/baseline_proxy_acc.json`；长步骤（⑥；真 profiler 下③）detach+有界轮询（单次 bash ≤10min）；rc 聚合；**用户项目根唯一来源 = readiness/readiness.json，全链禁读 `ORCA_PROJECT_ROOT` env**（引擎已占用该名 = Orca 仓库根，同名冲突曾锚错项目——v3.4 P2） |
| po_propose | §9 | 先跑 analyze.py 刷新 base/bottleneck_report.json；history_lib 去重（v3.5 去重集：永久 = {promoted, unsupported_op}；probe_insufficient 重试指纹 = epochs/步数/数据子集值，两端均逐字读 contracts.proxy_budget——null = 已钉死的预算值非「未设」）；proposals.json 顶层 {round, exhausted, proposals[], filtered_count}；**exhausted 机械判据**：playbook 可生成 admissible 提案数 == 0（去重过滤后计 filtered_count，非 LLM 口头判断——SPEC-R1-m4）；准入：predicted<0 严格负 / edited_files⊆shadow / op_delta⊕change_spec 自检（proposal 无 weight_delta 字段——v3.5 删） |
| po_implement | 草稿 §5.1 | 逐提案：复制 base shadow → 编辑 → export（模板）→ diff_check 文件层预检（**失败 → outcome=structural_mismatch、不写 DONE、history 单行**；两层校验的 structural_mismatch 判定权仍归 po_verify——SPEC-R1-m1）→ declaration.json → DONE；**无 ckpt 生成步骤**（v3.5 训练从头开始，weights/ 不再产出）；variant_broken/预检失败 skipped 不阻断 |
| po_verify | §7.1 | `run_verify.sh` 对全部 DONE 变体批量：diff_check **两层（文件/图）** → profile → 时延 gate → verdicts.jsonl + history 行（经 history_lib.append_latency builder；outcome：latency_pass 过程态/终态淘汰值） |
| po_probe | §7.2-7.3 | 幸存者串行：**proxy 从头训练**（渲染读 contracts.proxy_budget 与基线逐字段同值 + detach 轮询 + 禁二次 detach）→ eval → promote（gate 公式锚 **baseline_proxy_acc**）→ probe_results.jsonl + history 行（builder，字段 proxy_acc）→ advance_round.py（幂等键 = 轮号，见 §1）；**v3.5：L1 零样本整段删除** |
| po_gate | §7.5 | `run_gate.sh` → gate_decide.py 纯读现算 → emit decision；硬帽：轮数≥max_rounds 永不 loop；best 平局/排序字段 = proxy_acc |
| po_full_train | §7.4 | psu 常驻模式（状态文件 + 存活检测 + 禁二次 detach）；winner **从头**完整训练（epochs = min(full_train_epoch_cap 或 ∞, train_epochs_full)，数据 = 完整无子集值，无 ckpt）；**final 锚解析**（v3.5）：baseline_ref.json 有值→ref-input；空→以完整预算从头训基线一次（结构源 = baseline/original_shadow/，幂等判据 = baseline/baseline_full_acc.json 存在，跨轮跨 run 缓存复用）→auto-trained；final eval + within_budget 判定（相对锚） |
| po_report | §11/§12 | 磁盘读态判终态（零跨节点引用）；成功且 write_back：终局 shadow diff → `<stem>_prof_optimized<suffix>` 写回（冲突不覆盖/lock 复验/删除不写回列示）；静态图（history 聚合）；charts_summary；报告含**公平对比说明段**（基线与变体同预算从头训练）+ baseline{makespan,proxy_acc,ref_acc} + pretrained_ref_acc（仅对照，可 null） |

## 5. P0 前置验证（SPEC-R1-U1a：in-session 回边探针恢复为 coder 开工第一步）

1. **P0-探针（先于任何 agent 实现）**：2 节点最小回边 workflow（全 agent 节点，prompt 极简）在 in-session + claude 宿主下实测：回边重入 / 同节点二次执行的 tape 与 reducer / `orca next` 续跑 / chart daemon 推送 / 跨节点引用回边节点 output 的取版语义（本设计 gate 读盘规避之，但实证记录）。任一不符 → 触发草稿 §8.2 降级（循环折叠进 po_cycle，盘面契约不变），**在实现铺开前落定**。
2. `test_po_backedge_routing.py`：orchestrator 路径（无 LLM）——compile 容环 + route 循环推进 + 达条件退出 + 回边目标二次执行（`resolve_max_iter` 该路径生效）。
3. `test_po_scripts.py`：analyze（fixture → 热点模式/cost table）；predict_delta（算术 + params 归一化幂等）；gate_decide（四 decision 全分支 + 硬帽 + stall 清零）；advance_round（**r1→r2 序列断言 base/shadow 被二次替换** + 幂等键=轮号 + marker）；history_lib（多版本行读法/v3.5 去重全分支[永久集 = {promoted, unsupported_op}]/联合重试预算/builder 字段全集）。
4. `test_po_inject.py`：script 形态 shadow 优先（含子模块）/ stdlib guard / 裸模块兄弟闭包全遮蔽 / `-S` fail loud。
5. `test_po_diff_check.py`：op_type multiset diff / `__pycache__` 排除 / 参照恒 base（base≠baseline 场景）/ 退役 weights 层请求 fail loud。

## 6. 洁净度验收（实现完成后 reviewer 执行）

- 逐 agent.md 按洁净契约审查（每个 agent 单独过一遍，不留批量抽查）；playbook/references/subagents 同标准。**草稿节号（§N.M）禁进 agent.md/references**（validator lint 即 warning；SPEC 本文的「草稿锚」列仅 coder 导航用）。
- `tars validate workflows/prof-opt.yaml` warning 清零（全量校验含 `_check_prompt_dev_residue`）。
- 残留 grep（SPEC-R1-M1 修正版词表——路径形/词边界，避免 `target_modules`/`target_makespan` 误伤）：
  范围 = `workflows/agents/ + workflows/subagents/prof-opt/ + workflows/prof-opt.yaml`，以下命中数必须为 0：
  `mnist_kd`、`playground`、`prof_opt_demo`、`(^|[^A-Za-z0-9])(ns3|psu|kd-nas|nas-supernet)([^A-Za-z0-9]|$)`、`prof-opt-design-draft`、`docs/specs`、`D:\\?Projects`、`/mnt/d`、`spec-review`、`SPEC-R1`。

## 7. E2E 验收（claude 后端 + tars skill，两项目）

**驱动形态**：**WSL 内执行**（orca in-session 引擎 POSIX-only——fcntl 前提；且 target 项目 `Config` 硬编码 `/mnt/d/...` 数据路径）——`claude -p`（headless，WSL 原生）起会话 → 意图触发 tars skill（`tars install --target cc` 装于 `.claude/skills/`）→ skill 编排 agent 调 `orca` CLI（`orca prof-opt --inputs ...` + `orca next --run-id --output` 逐节点）。**inputs 的 project_root 等路径一律 `/mnt/d/...` 形态**。**宿主 cwd 必须在 Orca 仓库**（catalog `./workflows` first-wins）或改 workflow 后重跑 `tars install` 同步安装副本——防静默跑旧版。
节点执行：in-session 下由宿主 session 派子代理执行（`CLAUDE_CODE_SESSION_ID` 链路；`executor` 字段不参与）。

**输入取值（两项目统一策略）**：`target_makespan = baseline_makespan × 0.5~0.7`（强制 loop 至少一轮，防 round 1 即 full-train 的空转验收）；`max_rounds=2`、`max_proposals_per_round=3`（控训练预算）；`full_train_epoch_cap=1~2`（target 原 100 epochs 不截断则验收不可判定——SPEC-R1-U2a）；placeholder 估算器场景 `min_improvement_pct` 可调低（如 0.2）以容结构性小赢（makespan = 全图总 cycles 且 MatMul 常占大头，1% 硬门槛会全灭小赢——v3.4）；v3.5 起两项目均无需任何预备 ckpt（全链从头训练）。

**Tier 判定事实（SPEC-R1-M9，验收预期与 D13 三档一致；v3.5 参数需求 = ①epochs ③out-dir ④截断 ⑥数据子集旋钮）**：
- `target`：train 缺 ①epochs ③out-dir（`Config.TRAIN_NUM_EPOCHS=100`、`Config.MODEL_SAVE_PATH` 硬编码，常量驱动可参数化）、eval 缺 ⑤ckpt 参数（`_baseline_eval.py` 硬编码）→ **train/eval 双 Tier B**（porter 适配入口被真实检验）。`pre_trained.pth` 现成但 v3.5 起仅参考（可提供以验证参考链路，不进 gate）。
- `mnist_kd`：train 缺 ③out-dir ④截断（`--epochs` 有 argparse；`--ckpt` 是保存路径——v3.5 无 init-ckpt 需求，不再相关）→ **Tier B**；eval 有 `--ckpt` → **Tier A**。**v3.5 起无需任何预备 ckpt**（契约期双 ckpt = 随机初始化，基线/probe/final 全部从头训练）。

**验收线（machinery，两项目都必须）**：全部节点到达预期终态、tape 无死锁、report 磁盘读态正确、history/verdicts/proposals/盘面齐全、无引擎级异常。
**验收线（内容）**：
- `target`（transformer，主战场）：≥1 轮完整 propose→implement→verify→probe→gate；回边重入 ≥1 次或 stall 退出；**草稿 §13.5 断言**：①≥1 提案 latency gate 过 ②promote 判定真实执行 ③非退化（若 winner：makespan < baseline）④**公平不变量**（v3.5 替零样本断言：基线与全部变体的 proxy 训练渲染命令中预算参数[数据子集/epochs/步数/seed]逐字段一致——读 contracts.json 与各 .rendered.sh 机械比对）+ **变体真实训练**（变体 proxy 训练输出 ckpt 的 eval 与基线 eval 来自不同模型实例——shadow 断言链 + ckpt 哈希互异）⑤eval 双 ckpt 联动（contract 期已验，E2E 抽查 verdict 与盘面一致）⑥写回断言（write_back.files 落盘与终局 shadow diff 一致 + 同名冲突不覆盖）；逐 agent 产出符合节点契约（profiling 三件套真实非空/瓶颈报告含热点模式/提案带算术）。
- `mnist_kd`（LeNet CNN）：重点验 **Tier B porter（train）+ Tier A（eval）**与 playbook 通用性——LeNet+ReLU 在 v1 三杠杆下**合法 exhausted 是可接受终态**（结构化 finish/failed-with-exhausted，非崩溃；exhausted 判据按 m4 机械规则可复核）；若产生提案则同 target 断言 ①②④。
- **通用规则铁律**（用户拍板）：E2E 发现的任何问题，修复只能落在 workflow 通用逻辑（agents/_po_scripts/yaml）；**禁止**针对 mnist_kd/target 写任何特判/硬编码/项目名分支。修完 → 洁净度复审 → 重跑 E2E，循环至两项目均达验收线。

## 8. 流程与回卷规则

SPEC → spec-review（对抗闭环）→ **P0 探针先行** → 分批实现（coder）→ 洁净度审查（reviewer 逐 agent）→ 单测全绿 → E2E（test-agent）→ 通用规则修复循环（修复→洁净复审→重测）→ 终态报告 + 状态文档收口。
**SPEC-R1 回卷项**（已同步草稿）：① advance_round 幂等键 existence-only → 轮号比对（草稿 §7.3）；② export_onnx.py 从静态共享清单移除，改 contract 期生成物 + sha 防漂移（草稿 §5.1/§3.2）；③ 草稿 §10 增 `full_train_epoch_cap`；④ 草稿 §13.3 playground demo 项目**不再单独建**——P0 探针 + 两 E2E 真项目覆盖其职责；Windows 原生 os.pathsep 场景随 WSL-only 决策显式放弃覆盖（记录于草稿附 A）。
实现期发现草稿级语义问题：回卷草稿（变更记录进附 A），不静默偏移。
