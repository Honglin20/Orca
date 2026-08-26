# Prof-Opt v4 重构设计草稿 (SDD)

> 跨阶段设计议题，prof-opt v4 各 phase SPEC 撰写前必读。
> **定位**：对已交付的 `workflows/prof-opt.yaml`（v3.5，10 节点 = 9 agent + 1 script）做重构。问题域不变（profiling 证据驱动的模型结构优化闭环），变化集中在四点：① **基线改为完整训练、非阻塞**（后台跑原始参数全程、逐轮精度记录作全链对比锚、训练进度 live 可视化）；② **po_propose 重构为节点内子 agent 闭环**（瓶颈分析→业务逻辑+SOTA 提案→直接实现→复测时延打回改），删 `po_implement`/`po_verify` 两节点；③ 新增**业务逻辑分析 subagent**；④ **stop-at-k 短训渲染**（变体按全量轮数规划学习率、到 k 轮外部停止）。
> **与 v2 草稿关系（supersede）**：本稿取代 [`prof-opt-v2-design-draft.md`](prof-opt-v2-design-draft.md)（v1.2）。其 D-N10（基线完整训练非阻塞 + epoch 对齐）经用户 2026-08-25 再次拍板**采纳**（并新增可视化要求）；review-agent / 硬件知识库 / mfu_adapter 四层链 / full_train live 监控早停**搁置**（共识范围外，D-V4-13）。
> **审查状态**：草稿 v3 = v2 + spec-review 轮 2 回卷（R2-01~25 + UD-3 拍板，见附 A）。

---

## 0. 用户诉求与已确认决策（2026-08-25 会话拍板 + 评审轮 1/2 补钉）

1. **基线完整训练、非阻塞**：原始参数跑完、数据不变；节点在训练**启动并确认存活后即完成**，不等训练结束。
2. **基线可视化**：逐轮指标曲线在 web UI live 可见（chart sidecar，best-effort 不阻断）。
3. **业务逻辑 subagent**：`business_logic.md` 五段（任务语义/输入输出/架构动机/逐模块职责/训练目标与指标方向）。
4. **po_propose 多 subagent 内闭环**：瓶颈分析 → 业务逻辑+SOTA 提结构级提案（**绝不改超参**）→ 直接实现 → 复测时延打回改。
5. **po_verify 取消**；**po_probe 保留独立节点**；**一轮一批候选（≤3）**；**stop-at-k 学习率对齐**。
6. **UD-1 拍板（轮 1）**：非阻塞 + 可视化；GPU 串行守卫 + finalizer worker 三钉。
7. **UD-2 拍板（轮 1）**：预算指纹 = 值级（`full_train_budget{epochs, seed, data}`，data 恒 null 对）。
8. **UD-3 拍板（轮 2）**：早停项目**三层准入**（D-V4-19）——契约期 best-effort 早拒 + 明示准入条款 + 终检严格失败文案指向条款；不做软化、不做早停语义支持。

### 0.2 决策表（D-V4 系；未列者继承 v3.5 D 系）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D-V4-1 | 基线完整训练 | 项目原脚本原样运行、epochs = min(cap 或 ∞, full)、完整数据、固定 seed；曲线 `baseline/baseline_metrics.jsonl`；末 ckpt eval → `baseline_full_acc.json`；可寻址时第 k ckpt eval → `baseline_k_acc.json`。替代 v3.5 proxy 锚 + 懒满训双路径 | 用户拍板 |
| D-V4-2 | 基线非阻塞 + finalizer worker + GPU 串行守卫 | **节点内**（不等训练）：早期链（快照/导出/profile/analyze）+ 训练 launch + 存活确认（训练 pid 活 + finalizer.pid 活 + train.log 出现）+ business-logic-analyst 完成 → emit。**finalizer = 自持 detached 守护进程**（setsid + `baseline/finalizer.pid` + `baseline/finalizer.log`，**日志行首一律 ISO8601 UTC**（`date -u +%FT%TZ`）——心跳行含曲线点数、收尾链每步写 stage 行，⑧ 断言同格式解析比较）：①轮询训练 pid/rc（无 rc 死亡重派 ≤3 + per-attempt train.log 命名 + 按 train 契约 resume 规则 wipe partial out-dir）②逐 epoch **增量 extract**：每次从当前 attempt 的 train.log **全量重 derive、内容有变化才原子替换** baseline_metrics.jsonl（训练期 push_curves 有数据可推——R2-01）③每 poll 周期 push_curves ④rc=0 收尾链：终检（`--expected-epochs` = full 生效值，**实跑 ≠ 渲染即 fail**，文案指向 D-V4-19 准入条款）→ 末 ckpt eval → baseline_full_acc.json（值级指纹）→ 可寻址则第 k ckpt eval → baseline_k_acc.json → `train_final.json{status: done\|failed, rc, stage}`。**fail-safe**：任何内部步骤失败尽力写 train_final{failed} 再退；存活判定一律 /proc cmdline 归属校验。**GPU 串行守卫（po_probe 前置，探测目标 = finalizer.pid**，生存期 ⊇ 训练）：四象限 = 活 → bounded-wait（单调用 ≤480s + status message 续驱；停滞判据：训练活期 train.log mtime 与曲线点数均停滞 ≥30min → stalled error；finalizer 期 finalizer.log 停滞 ≥30min → error）/ 死 + train_final=done → 放行 / 死 + failed → error 路由 po_report / 死 + 缺失 → error fail loud。po_full_train 防御性同检 | UD-1 拍板；emit 后无 agent 驱动者 → 收尾必须自持 detached（R2-02）；守卫探测 finalizer.pid 防训练死 finalizer 活的中间态漏判 |
| D-V4-2b | 基线可视化 | `_po_scripts/push_curves.py` sidecar（幂等 best-effort）：读 baseline_metrics.jsonl + variants/*/metrics 曲线 → render_chart 单张 live 折线（hue=baseline/vid）。**socket connect/send 各 ≤5s 硬超时**（超时 = 失败 stderr + 退出 0——防 chart 挂起拖死 worker，R2-07）；每次成功推送追加审计行 `{ts, baseline_epochs, curves:[...]}` 到 `$ORCA_ARTIFACTS_DIR/.chart_push.log`（E2E 断言⑨载体）。调用点 = finalizer 每 poll 周期 / po_probe 等待循环 / po_report finalize（title 加 `(final)`）。ORCA_CHART_SOCK 缺失 → 静默退出 0 | UD-1 可视化诉求；失败/超时/缺 sock 全隔离，绝不进关键路径 |
| D-V4-3 | stop-at-k 渲染 | 变体渲染 `--set epochs=<full 生效值>` + `stop_at_epoch.sh` **幂等单次检查**（节点 bounded-poll 反复调用，**poll 间隔 ≤30s**）：收 `--contract`（与 metric_curve extract 同源读 contracts metric 正则——单源，禁独立 --pattern）解析日志 epoch 编号 ≥ k 首现 → /proc cmdline 归属校验 → `kill -TERM -<pid>`（进程组）→ 10s 宽限 → KILL → **重解析冻结日志**，`stopped_at_epoch` = 终态时刻可解析最大完整 epoch（≥k，非恒 k——R2-03 竞态防假淘汰）→ `stop_status.json{killed, stopped_at_epoch, rc: null}`；worker 自然结束 → `{natural_done, ..., rc}`（实际轮数 > k → `monitor_failed: true`）。**wrapper 形态：组长不 exec**——`setsid bash -c 'echo $$ > <pid>; bash train.rendered.sh; echo $? > <rc>'`（pid/rc 各有写者；killed 分支 rc 恒 null 与终态判定优先级自洽——R2-04）。probe 终态判定优先级 = 先 stop_status（killed 即成功终态）再 rc 文件 | 用户拍板；竞态下取实际解析深度 + extract `--expected-epochs` 用 stopped_at_epoch，两处一致不假失败 |
| D-V4-4 | ckpt 三态 + probe 判定 | 契约期指标格式实测快跑统一 **≥2 epochs**（一跑双用：指标格式 + ckpt 行为）；三态 `train.ckpt_output_rule`：逐轮多文件（可寻址）/ 滚动覆写 / 不可判定。**可寻址** → finalizer 产 baseline_k_acc.json；probe eval = 变体 k ckpt eval vs baseline_k_acc 双过才 promote；**eval@k 加载失败 → 重派 1 次，仍败 → `eval_failed: true` + eval_acc=null + 按不可寻址路径（曲线单判）+ 披露**（R2-18）。不可寻址/不可判定 → 曲线@k 单判 + `eval_skipped_no_epoch_ckpt: true` | 曲线是主判定；eval 是同深度加严项 |
| D-V4-5 | probe 深度 k | `proxy_budget.epochs` = k（`probe_epochs` 缺省 1，受 full 封顶） | 缺省保守 |
| D-V4-6 | 业务逻辑 subagent | `business-logic-analyst`（训练启动后 dispatch，并行）→ `baseline/business_logic.md` 五段 | 用户核心诉求 |
| D-V4-7 | po_propose 子 agent 内闭环 | bottleneck-analyst → structure-proposer → variant-implementer（+ run_latency_recheck.sh 批量复测）。配额：候选 ≤3/轮（**4→3 显式**）、结构修复 ≤2、时延打回 ≤2。**真 profiler 条件守卫**：`profile_script_path` 非空（外部 profiler 真测时延）时 Step 5 复测前置等基线 worker 退出（GPU 竞态；placeholder 默认空不等——R2-11） | 用户拍板；placeholder 复测是 CPU 秒级无竞态 |
| D-V4-8 | proposals 来源升级 | LLM 提案 + 机械准入三闸（predict_delta 严格负 / edited_files ⊆ shadow 闭包 / op_delta⊕change_spec 一致）；playbook → structural-levers.md（背景先验） | 机械匹配已证伪 |
| D-V4-9 | exhausted 判据 | proposer 声明 + 数 0；`exhausted_rationale` 结构化非空（≥1 已尝试方向条目，Validation 机械校验）；**机械闸过滤后 count==0 → 强制 true**（rationale 记过滤原因）。兜底 = gate stall | 防 LLM 谎报与 loop 空转 |
| D-V4-10 | 时延复测批量化 | `po_propose/scripts/run_latency_recheck.sh`（原 run_verify.sh 迁移改名）；**打回修复后复测前删该 vid verdict.json**；阈值实参 100/1/0.5 调用行显式 | 用户拍板；盘面格式不变 |
| D-V4-11 | po_full_train 锚简化 | 删 auto-trained 路径；锚 = baseline_full_acc.json（值级指纹校验）+ 防御性 train_final=done 检查；`baseline_full_acc_source` ∈ `["baseline", null]`；winner 预算同指纹；**winner 训练终了对称终检实跑 == full**（R2-25） | 锚天然存在 |
| D-V4-12 | 节点命名 | 沿用 `po_propose` | churn 最小 |
| D-V4-13 | v2 草稿搁置项 | review-agent / 硬件知识库 / mfu_adapter 四层链（保留 profile_script_path 直通）/ coarse-final 双预算 / full_train live 监控早停 | 共识范围外 |
| D-V4-14 | retire 清单 | po_implement/、po_verify/、po_gate/{agent.md,scripts/}、perturb_ckpt.py；保留 placeholder_profiler.py + mfu_benchmark.py。**pristine 快照（baseline/original_shadow/）保留**——结构对账锚（写回 diff 复验），非 v3.5 懒满训专用（R2-17） | 死代码清理 |
| D-V4-15 | gate_node.sh 修复 | 引号笔误修复（bash -n 必过） | 现状缺陷 |
| D-V4-16 | 渲染留痕 + 无缓冲 | 基线/变体/winner 渲染**同一 train 契约模板**、`--out` 显式三路径（baseline/variants\<vid\>/final 的 train.rendered.sh）；render_run.sh header 层 `export PYTHONUNBUFFERED=1` | 公平不变量可断言 |
| D-V4-17 | curve compare @k | `metric_curve compare` 增可选 `--at-epoch k`（任一曲线缺第 k 点 fail loud；po_probe 恒传）；compare 输出增 `at_epoch`（无条件写实际比较深度）+ `baseline_path`（锚来源——R2-13 让断言③可证伪） | 公平不变量比较端 |
| D-V4-18 | history probe 行字段 | PROBE_FIELDS 增可选 `eval_skipped_no_epoch_ckpt` / `monitor_failed` / `eval_acc` / `eval_failed`；`proxy_acc` 恒填曲线@k 值 | 排序键语义统一 |
| D-V4-19 | **早停项目三层准入（UD-3）** | ①契约期 best-effort 早拒：快跑实测（≥2 epochs）中检出 early-stopping 机制（argparse/config 扫描 + 实跑观察）→ viable=false 归因披露；②**明示准入条款**（workflow description + contracts reason）："训练须精确执行渲染 epoch 数，自带 early-stopping 的项目不在 v4 范围"；③finalizer 终检严格失败（实跑 ≠ 渲染）→ train_final{failed, stage: final_check}，文案指向条款。**不软化、不支持早停语义**（后续 phase 另议） | UD-3 拍板 a；检测不出的不承诺检测——划范围 + fail loud + 可定位文案优于静默不公平比较 |
| D-V4-20 | **profiling 子代理化（2026-08-26 用户拍板）** | `profile_script_path` 退役；新 inputs：`npu_chip`（[advanced] 空 = placeholder 本地估算模式[默认，无 NPU 环境/E2E 用]；"6613"/"1951" = mfu 真评测模式）、`npu_precision`（INT8/INT16/AMP，default INT8）、`npu_core_num`（1/2/4，default 1）。**mfu 模式**：profiling 统一经 **mfu-analyzer 子代理**（跑部署件 `scripts/mfu_benchmark.py`——用户真评测脚本载体，文件名锁定、内容随用户提供替换；原始产物落 profile_dir 只读 + 瓶颈报告 `mfu_bottleneck_report.md`）+ **确定性 `mfu_adapter.py`**（原始产物 → PROFILER_CONTRACT 四件套；canonical makespan = `schedule_result.json` 并行 cycles；缺文件/缺字段/不一致 fail loud）→ analyze.py / 时延门 / 裁判**零改动**。调用点 = po_baseline 基线 profile（早期链）+ po_propose Step 5 每变体 profiling（mfu 模式 = 逐变体 dispatch mfu-analyzer + adapter 产四件套，recheck 对该 vid 跳过内联 profile 只做门判定；**GPU 守卫条件源从 profile_script_path 改挂 npu_chip 非空**）。**placeholder 模式行为完全不变**（本地脚本直出四件套）。mfu-analyzer（执行层：跑评测+报告）与 bottleneck-analyst（富化层：referential 校验零重复；输入增读 mfu 报告[在场时]）分工正交 | 用户诉求：profiling 不写死脚本路径、子代理为唯一入口；**判定数字必须机械可审**（adapter 从原始文件提取，LLM 只做执行编排与定性分析——12-Rule #5）；E2E 移交用户真机自跑（本地仅 placeholder 模式） |

### 0.3 与 v2 草稿分歧处置（supersede 后仅剩）

| 议题 | v2 草稿 | v4 | 裁决 |
|---|---|---|---|
| 节点数 | 10→9（verify 保留） | **10→8** | 用户明示；复测是脚本批处理无独立节点必要 |
| 粗/终局预算 | 双 input 拆分 | accuracy_budget 共用 | 用户模型只有一个精度预算 |
| review-agent | 三轴对抗 | 不设 | 前提（知识库）搁置 |
| baseline 阻塞性 | 非阻塞 | 非阻塞 | 已一致（UD-1 + 三钉） |

---

## 1. 总体形态：8 节点 + 6 subagent

```
  po_flatten (agent)                po_propose (agent·循环体) ──────────────┐
    ↓                                 ├ Step1 analyze.py 机械刷新瓶颈        │
  po_contract (agent)                 ├ Step2 bottleneck-analyst (subagent) │
    ↓                                 ├ Step3 structure-proposer (subagent) │
  po_baseline (agent·非阻塞)          ├ Step4 variant-implementer (subagent)│
    ├ 早期链(快照/导出/profile/analyze)│    └ run_latency_recheck.sh 批量复测 │
    ├ 完整训练 detach + finalizer 守护 ↓                                     │
    ├ business-logic-analyst (subagent) po_probe (agent·GPU守卫+kill@k+晋升)│
    └ push_curves.py sidecar(live 图)   ↓                                   │
                                      po_gate (script·纯读现算) ──loop──────┘
                                      ↓ full-train / best-effort
                                      po_full_train (agent·锚=baseline 终值+对称终检)
                                      ↓
                                      po_report (agent·终态收割+reporter+finalize 图) → $end
```

| 节点 | 职责 | 相对 v3.5 |
|---|---|---|
| po_flatten | shadow 建立 + manifest + 锁 + 就绪检查 + memory-verifier | 不变 |
| po_contract | 三契约发现实测 + 预算落盘 + 模板 + paradigm-verifier + **早停 best-effort 早拒** | 增量 |
| po_baseline | 早期链 + 训练 launch + **finalizer 守护（增量曲线/双锚/终检/终态标记）** + business-logic-analyst + live 图 | **重构（非阻塞）** |
| po_propose | 机械瓶颈刷新 → 三 subagent 闭环 → 批量时延复测（+真 profiler 条件守卫） | **重构** |
| po_probe | GPU 串行守卫（finalizer.pid 四象限）+ kill@k + 曲线@k + eval（可寻址）+ 晋升 + 推进 | 骨架不变，语义更新 |
| po_gate | 纯读现算决策 + 硬帽 | 不变（bug 修复） |
| po_full_train | winner 完整训练（同指纹 + 对称终检）+ 终评 | 锚简化 |
| po_report | **终态收割**（孤儿进程）+ 磁盘读态 reporter + 写回 + 图（静态 + finalize chart） | 增收割 + 字段语义更新 |

**subagent**：memory-verifier（flatten）/ paradigm-verifier（contract）/ business-logic-analyst（baseline）/ bottleneck-analyst、structure-proposer、variant-implementer（propose）。回边 `po_gate --loop--> po_propose` 不变。

---

## 2. 基线完整训练与对比锚（核心机制）

### 2.1 数据流

```
po_baseline（节点内，不等训练）:
  pristine 快照 → 导出 → profile 四件套 → analyze.py
  （v3.5 step1 reference 交叉核对退役——warning-only 信息步骤，v4 无 input 入口且精度锚自产）
  完整训练 launch（同模板、--out baseline/train.rendered.sh、--set epochs=full 生效值、
    setsid 组长自写 pid、PYTHONUNBUFFERED）+ finalizer 守护 launch（setsid、finalizer.pid/log）
  business-logic-analyst dispatch（并行）
  emit executed（存活确认 = 训练 pid 活 + finalizer.pid 活 + train.log 出现）

  finalizer 守护（确定性，节点 emit 后自持运行）:
    轮询训练 pid/rc（no-rc 死亡重派 ≤3、per-attempt train.log、resume 规则 wipe partial out-dir）
    每 poll 周期：增量 extract（从当前 attempt train.log 全量重 derive，变化才原子替换
      baseline_metrics.jsonl）+ push_curves sidecar + alive 心跳行（含曲线点数）
    rc=0 收尾链（每步写 stage 行）:
      终检（--expected-epochs=full；实跑≠渲染 → failed[final_check]，文案指向 D-V4-19 条款）
      → 末 ckpt eval → baseline_full_acc.json（值级指纹）
      → ckpt 可寻址 → 第 k ckpt eval → baseline_k_acc.json
      → train_final.json{done}
    任何步失败 → 尽力写 train_final{failed, rc, stage} 再退

po_probe（每变体）:
  GPU 守卫（finalizer.pid 四象限，见 D-V4-2）
  渲染同模板 epochs=full 同值 → detach → bounded-poll 反复调 stop_at_epoch --stop-epoch k
    → 组 kill → stop_status.json（stopped_at_epoch = 实际解析深度）
  变体曲线 extract（--expected-epochs = stopped_at_epoch）
    → compare --at-epoch k vs baseline_metrics.jsonl（epoch_compare.json 含 at_epoch + baseline_path）
  （可寻址）第 k ckpt eval vs baseline_k_acc → 双过才 promote（eval 失败重派 1 次再降级披露）
  （否则）曲线@k 单判 + eval_skipped_no_epoch_ckpt
```

### 2.2 公平不变量（可机械断言）

- 基线与全部变体：同训练入口模板、同数据（完整）、同 seed、同 epochs 渲染值——唯一差异 = 结构改动 + 变体被外部停止在第 k 轮（实际深度 = stopped_at_epoch，比较恒 @k）。
- 断言对象 = 三类 rendered 文件的 `--set` 除 **{out_dir, vid, shadow_dir}** 外逐字段一致。
- 预算指纹（值级，UD-2）：`full_train_budget{epochs, seed, data:{dataset_knob:null, data_value:null}}`；baseline_full_acc / baseline_k_acc / winner 渲染均记录逐字段校验。
- 早停准入（D-V4-19）：训练必须精确执行渲染 epoch 数。

### 2.3 stop_at_epoch.sh 契约

见 D-V4-3（幂等单次检查 / 进程组 kill / 重解析冻结日志取实际深度 / stop_status 输出 / 10s 宽限 / cmdline 归属校验 / 终态判定优先级先 stop_status 后 rc）。

### 2.4 与 v3.5 的锚对照

| v3.5 | v4 |
|---|---|
| baseline_proxy_acc（短训单点锚） | baseline_metrics.jsonl 完整曲线（@k 对齐） |
| baseline_ref_acc 占位 + full_train 懒补训 | baseline_full_acc.json（finalizer，值级指纹） |
| probe 比较锚 = 基线短训曲线 | 基线完整曲线 @k + baseline_k_acc（可寻址时） |
| 变体短训 = 渲染 epochs=k | 渲染 epochs=full + stop_at_epoch 组 kill |

---

## 3. po_propose 子 agent 闭环（核心机制）

### 3.1 节点内编排

```
Step 0  reuse 守卫：proposals.json 存在且可解析 → 跳过 Step 3 从 Step 4 续做
        （DONE marker 逐提案幂等）→ Step 5 复测照跑
Step 1  analyze.py 机械刷新 base/bottleneck_report.json
Step 2  stamp（键 = base 版本标识[best.vid / base onnx sha] + 机械报告内容指纹）未变 → 复用；
        否则 dispatch bottleneck-analyst → check_bottleneck.py（败重派 1 次仍败 → error）
Step 3  dispatch structure-proposer → proposals.json（≤3 + 三闸 + exhausted 语义）
Step 4  dispatch variant-implementer 逐提案（结构修复 ≤2；失败记 skipped 不阻断）
Step 5  （profile_script_path 非空 → 前置等基线 worker 退出）run_latency_recheck.sh 批量复测
        → verdicts + history；未过打回 implementer 修（≤2；复测前删该 vid verdict.json）→ 仍不过淘汰
Step 6  emit（status==executed ⇔ error==''；exhausted 含 rationale）
```

- 内层尝试全落盘（history/verdicts）——dashboard/报告可见性不丢；子 agent 返回 compact 摘要。

### 3.2 子 agent 契约要点

- **bottleneck-analyst**：输入 = 四件套 + bottleneck_report.json + profile_dir 全部原始产物；输出 = `base/bottleneck_analysis.json`（零重复机械字段；`top_bottlenecks[i]={name:pattern_id, op_type, cycles:total_cycles}` = analyze.py hot_patterns **保序子集**）；校验 = check_bottleneck.py（封闭 schema + referential + 排序）。
- **structure-proposer**：输入 = business_logic.md + bottleneck_analysis.json + history（去重，**probe 配置重试指纹键随 D-V4-18 字段更新**）+ references/structural-levers.md；硬约束 = 结构级（禁训练超参——训练脚本不在闭包内物理不可达 + Δ=0 被拒双保险）、符合业务逻辑、围绕瓶颈；输出 = proposals.json（rationale/op_delta/edited_files/change_spec/sota_reference；exhausted_rationale 结构化）。
- **variant-implementer**：忠实实现单条提案；修复配额 = 结构 ≤2 / 时延 ≤2；declaration.json + DONE + compact 摘要。**三 subagent 失败矩阵**（校验败/超配额/产物缺失）在 agent.md 各自显式（重派 1 次 → error 披露——R2-18）。

### 3.3 阈值与配额常量（agent.md 写死）

候选 ≤3/轮（4→3 显式）；结构修复 ≤2；时延打回 ≤2；时延 gate = 改善量 ≥ max(100 cycles, 1%×base) 且 实测/预测比 ≥ 0.5。

---

## 4. 其余节点增量

- **po_contract**：指标格式实测快跑 ≥2 epochs（一跑双用）+ **best-effort 早停早拒**（D-V4-19①：argparse/config 扫描 + 实跑观察，检出即 viable=false 归因）；metric pattern 钉"数字组后须紧跟行尾/非数字界"（防 0.1234 截成 0.12）；快跑 best-effort 断言实跑 == 渲染；contracts 增 ckpt_output_rule 三态 + ckpt_per_epoch + full_train_budget（值级）；probe_cap_mechanism="stop-at-k"；**reuse 路径 contracts 缺 v4 新字段 → fail loud + fresh_start 提示**（R2-19）；check_contracts.sh 断言清单（接受 stop-at-k / epoch-only 全 null 保留 / 新字段校验 / 模板同源管线一致）。
- **po_probe**：GPU 守卫四象限（D-V4-2）；stop_at_epoch 轮询 + "stop_status 未出且组活 → 继续调"分支；compare 恒 --at-epoch k；promote/eval/披露按 D-V4-4；probe 行按 D-V4-18；advance_round/禁二次 detach/probe_status.md 继承。
- **po_full_train**：删补训路径；锚 = baseline_full_acc + 指纹 + 防御 train_final=done；winner 同指纹渲染（--out final/train.rendered.sh）+ **对称终检实跑 == full**；常驻机制继承。
- **po_report**：**终态收割**（emit 前读 finalizer.pid：死直过 / 活 bounded 等 ≤60s / 到点无终态 → 双组 kill[训练组+finalizer 组] + 扫 variants/*/ 在飞 pid 一并 kill + 披露 "aborted at terminal"——R2-12）；report_format 四处改写（row3 读 baseline_full_acc.json **三态判定**：train_final 缺失 → null+披露 / failed → 归因 / done → 读盘；ref_acc 直读 baseline_full_acc；Fairness Note 轮数读 full_train_budget.epochs；内归因 implement/verify 并入 propose）；baseline.proxy_acc = 曲线@k（不足 → null+披露）；finalize chart（push_curves `(final)` + makespan 趋势）；写回断言在无 promoted 时降级为"零写回+披露"形态（R2-23）。
- **po_gate**：零改动。

---

## 5. E2E 验收要点（v4 增量）

- inputs 钉 `full_train_epoch_cap=2, probe_epochs=1`；`max_rounds=2`；阈值调整落点 = run_latency_recheck.sh 调用行实参。
- **machinery 验收线（条件要求）**：8 节点预期终态清单（含 no-promotion 分支：gate finish-failed → report）；baseline_full_acc.json + train_final.json 恒在（done 路径）；baseline_k_acc.json 仅 ckpt_per_epoch=true 时在，否则 history 含 eval_skipped_no_epoch_ckpt；best.json 仅 ≥1 promoted 时在。**两 E2E 项目均非可寻址 ckpt 时 eval@k 路径仅单测覆盖**（显式声明，不算 E2E 缺口）。
- **内容验收线**：
  - `target`：①三类 rendered `--set` 除 **{out_dir, vid, shadow_dir}** 外逐字段一致 ②stop_status.json 存在 + stopped_at_epoch ≥ k + monitor_failed 时披露在场（严格 kill 语义归单测：组 kill/幂等/宽限/实际深度重解析/stop_status 全分支）③epoch_compare.json **`at_epoch` == k** 且 **`baseline_path` = baseline/baseline_metrics.jsonl** ④变体曲线来自变体自身 train.log + 有 ckpt 时哈希互异 ⑤business_logic.md 五段非空 ⑥bottleneck_analysis 过 referential ⑦promote 真实执行 + 非退化 + 写回断言（≥1 promoted 时；否则零写回+披露）⑧**非阻塞证据**：finalizer.log 存在 timestamp > tape(po_baseline.node_completed) 的任意带时间戳行（心跳 ∪ stage）；若 mtime(train_final) ≤ tape(node_completed) 则降级为存在 timestamp > tape(node_started) 的行 + 披露 N/A（非失败）⑨**live 图**：ORCA_CHART_SOCK 在场时 .chart_push.log 非空且末行 baseline_epochs == 曲线终稿点数（report finalize 终稿推送兜底）。
  - `mnist_kd`：Tier B(train)/A(eval) + 子 agent 提案；合法 exhausted 可接受（rationale 非空）；有提案同 target ①-④。
- **通用规则铁律**：修复只落 workflow 通用逻辑，禁项目特判。

---

## 6. 开放问题

1. 真 profiler 接入形态：profile_script_path 直通保留；mfu_adapter 挂起（远程实产入库再定）。
2. 多轮 ckpt 磁盘累积：E2E 观察；需要时加清理条款。
3. 早停项目支持（min-epochs 对齐）：v4 划出范围（D-V4-19），后续 phase 另议。

---

## 附 A：审查记录

- **草稿 v1 → v2（spec-review 轮 1）**：CONDITIONAL_PASS；8 阻塞 + 18 重大 + 4 轻微回卷（P1-P30，逐项见 v1 记录）；UD-1 拍板非阻塞+可视化（D-V4-2/2b）；UD-2 值级指纹（§2.2）。
- **草稿 v2 → v3（spec-review 轮 2）**：CONDITIONAL_PASS；轮 1 的 30 项修订全部核验落地 ✓；新发现 5 阻塞 + 10 重大 + 10 轻微回卷——R2-01 增量 extract 回填（→D-V4-2②）、R2-02 finalizer 自持守护完整契约（→D-V4-2）、R2-03 stopped_at_epoch 取实际解析深度（→D-V4-3）、R2-04 wrapper 组长不 exec（→D-V4-3）、R2-05 断言①豁免 shadow_dir（→§2.2/§5）；重大：R2-06 守卫 bounded/停滞判据（→D-V4-2 四象限）、R2-07 push_curves 硬超时（→D-V4-2b）、R2-08 断言⑧ finalizer.log 时序法（→§5⑧）、R2-09 machinery 条件要求（→§5）、R2-10 schema 属性级契约（→SPEC §2）、R2-11 真 profiler 条件守卫（→D-V4-7）、R2-12 终态收割 + row3 三态（→§4 po_report）、R2-13 compare at_epoch/baseline_path 输出（→D-V4-17）、R2-14 live 图断言⑨（→§5⑨）、R2-15 UD-3；轻微 R2-16~25 分散吸收（--at-epoch 单测强制取 k 分支 / pristine 快照保留声明（D-V4-14）/ 三 subagent 失败矩阵（§3.2）/ reuse 缺字段 fail loud（§4 contract）/ 去重指纹键（§3.2）/ 洁净记录判据（SPEC §6）/ 词表范围 / 写回降级形态（§4 report）/ eval_failed 字段（D-V4-18）/ winner 对称终检（D-V4-11））。**UD-3 拍板 a：三层准入划范围**（→D-V4-19）。设计权衡显式化：E3 守卫面条件化（placeholder 无 GPU 竞态不守卫）、E8 孤儿保留（placeholder_profiler/mfu_benchmark 留作 v2 搁置物）。
- **草稿 v3 → v3.1（spec-review 轮 3，PASS 收敛）**：R2-01~25 全部落地核验 ✓ + 三方一致性实证（inputs 12/schema 收缩/run_verify 331 行/gate_node bash -n 报错均实测吻合）；10+2 项措辞级回卷——E3-01 reference 交叉核对退役声明、E3-02 finalizer.log 行首 ISO8601 UTC、E3-06 stop_at_epoch 改收 --contract（单源）、E3-07 contracts reason 准入条款句（agent.md 唯一源 + 子串校验）、N-1 nodes 侧注释随 v4 重写总则 + 词表增 ref-input/auto-trained、E3-03/04/05/08 与锚定修正见 SPEC。零新增拍板，收敛建议：回卷 → 确认闸 → 计划环。
- **草稿 v3.2 → v3.3（2026-08-26 用户拍板 D-V4-20）**：profiling 子代理化——`profile_script_path` 退役 → `npu_chip/npu_precision/npu_core_num` 三 input；mfu-analyzer 子代理（用户自带 MFU Bottleneck Analyzer prompt 适配 Orca 协议，落 `workflows/subagents/prof-opt/mfu-analyzer.md`；`mfu_benchmark.py` 文件名锁定）+ `mfu_adapter.py` 确定性转换（并行 cycles → 四件套）；调用点 = 基线 + 变体复测；placeholder 模式不变。E2E 移交用户真机自跑（本地 E2E 仅 placeholder 模式）。
- **草稿 v3.1 → v3.2（v4 洁净审查回卷，2026-08-25）**：paradigm-verifier 允许改造白名单更新至 v4 语义——删 v3.5 预算压缩条目（step-or-batch cap / data-subset limit 开关 + proxy budget compression hook；v4 = stop-at-k 外部杀 + 预算旋钮恒 null），白名单收敛为 epochs/out-dir/seed 开关 + 路径参数化 + 工作区内 import 调整；SPEC §1 paradigm-verifier 行由「不变」改为「白名单更新至 v4 语义（预算压缩条目删除）」。
