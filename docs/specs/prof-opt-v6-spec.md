# Prof-Opt v6 SPEC —— 单变体收敛 + 异步训练流水线 + 流式早停自动评测

> 依据：[`prof-opt-v6-design-draft.md`](prof-opt-v6-design-draft.md)（D-V6-1~10 用户拍板 + O-1~O-8 默认采纳）。评审通过后逐字实现，不自作主张加字段。
> 关联：[`prof-opt-web-view-spec.md`](prof-opt-web-view-spec.md)（Web 展示，独立交付，依赖本 SPEC 的 P0/P4 产物契约）。
> 前作：[`prof-opt-v5-spec.md`](prof-opt-v5-spec.md)（未在本 SPEC 重述的 v5 契约继承生效）。
> 环境约束：pytest/tars 走 WSL `.venv`；不 push；改完按洁净契约检查 warning 清零。

---

## 0. 范围与非目标

**范围**：`workflows/prof-opt/workflow.yaml` + `agents/po_propose|po_probe|po_report|po_baseline` + `agents/po_gate`（script）+ `subagents/structure-proposer|business-logic-analyst|information-analyst|accuracy-analyst` + `agents/_po_scripts/`（改 9、新增 3、删除 2）+ `templates/run_full_finetune.template.sh` + `tests/test_po_scripts.py` + 新增 `tests/test_po_v6.py`。

**非目标**（明确不做）：
- **Web 展示**（基线/变体分析文档前端视图）→ 独立 SPEC `prof-opt-web-view-spec.md`，本 SPEC 只产出展示所需盘面文件与推送契约。
- 引擎层 / schema / exec / iface 改动（device 分配、watchdog 全部是 workflow 脚本层）。
- 规则跨用户共享（沿用 v5：池在 `$ORCA_HOME`，仅本用户本机）。
- 真机 in-session E2E（归用户 NPU/GPU 服务器，见 §16 真机清单）。

**DAG 形态（8 → 7 节点）**：

```
po_flatten → po_contract → po_baseline → po_propose → po_probe → po_gate
                                                            ↓        │
po_full_train（删除）                                        po_report ←┘
回边：po_gate --loop--> po_propose；po_gate 其余分支 → po_report；每节点 catch-all → po_report
```

**v5 退役资产清单**（实现时删除或禁用，逐项）：`advance_round.py`、`stop_at_epoch.sh`、`round_state.py mode` 命令、`.round_advanced`、`verdict_decide.py promote` 子命令、history 的 `advanced`/`promote_gate`/`proxy_acc` 字段、probe GPU 串行守卫四象限、po_full_train 节点及其 references/scripts。

**开工门槛（fail loud）**：`docs/specs/prof-opt-v6-spec.md` 与 `prof-opt-web-view-spec.md` 评审通过、`workflows/prof-opt/workflow.yaml` 存在；不符 → 上报不自行开工。

---

## 1. inputs 契约 v6（继承 v5 8 个，改 1 个）

8 个 inputs 全保留；唯一变更 = `max_rounds` description 语义改为**变体轮数硬帽（每轮 = 1 个变体）**，默认仍 100：

```yaml
  max_rounds:
    type: integer
    description: "[advanced] 变体轮数硬帽（默认 100；轮 = 变体）。达到后永不回边：无 success 变体时等在飞训练终态后收尾；无任何时间帽 / 平台早退（平台期的答案是换路径探索，不是停机）"
    required: false
    default: 100
```

`fresh_start`（清工作区重建）/ `full_train_epoch_cap`（训练成本阀）语义不变。**不新增 input**：训练设备后端自动解析（§3），并发上限 = 空闲卡数（不定死）。

**验收（机械）**：`grep -rn "max_rounds" workflows/prof-opt/workflow.yaml` 的 description 含"轮 = 变体"。

---

## 2. profiling 模式契约（继承 v5 §2，语义确认）

- `resolve_profile_mode.sh` / `profile_mode.json` **零改动**。
- **语义确认（D-V6-7）**：mfu 评测与机器无关（静态/远程），`profile_mode.json` 只服务"评测配置"（chip/precision/core_num），**不参与训练设备分配**；propose 的 mfu 复测不做资源检查。

---

## 3. 训练设备契约（新增，D-V6-3）

### 3.1 后端解析（新增 `resolve_train_device.sh`，部署件，flatten 首入解析一次）

优先级（first match wins，fail loud）：

1. `ORCA_PO_DEVICE_BACKEND` 非空 → 显式声明，枚举 `npu|cuda`，非法值 exit 2；
2. `command -v npu-smi` 成功 → `npu`；
3. `command -v nvidia-smi` 成功 **或** `python3 -c "import torch; torch.cuda.is_available()"` 为真 → `cuda`；
4. 均无 → **exit 2 fail loud**（可训练设备缺失是硬错误，不静默回落 placeholder——placeholder 只对 profiling 有效，训练无占位后端）。

落盘 `$ORCA_ARTIFACTS_DIR/train_device.json`（write-if-absent；reuse 时 `--stdout-only` 重解析比对 `{backend, device_count}`，不一致 → fail loud 指引 fresh_start）：

```json
{"backend": "npu|cuda", "device_count": 4, "resolved_by": "env|npu-smi|nvidia-smi|torch.cuda"}
```

### 3.2 设备分配账本（新增 `device_alloc.py`，确定性）

- **占卡**：`devices/<idx>.lock`（`O_CREAT|O_EXCL` 原子创建），内容 `{"vid": "...", "pid": <int>, "acquired_at": "<ISO8601>", "backend": "npu|cuda"}`；同一 idx 锁存在 → 换下一空闲卡；全部占用 → 返回 `{"ok": false}`。
- **空闲集合**：`device_alloc.py free --artifacts <ws>` = 后端真实状态（npu-smi / nvidia-smi 当前存活进程归属）∪ 锁账本取反；锁文件 pid 死亡（归属校验失败）→ 视为可回收并披露。
- **释放**：`device_alloc.py release --artifacts <ws> --idx <N>`——watchdog 终态调用；report 收割兜底。
- **绑卡**：`run_full_finetune.template.sh` **新增 `device` token**（渲染为 `CUDA_VISIBLE_DEVICES=<idx>`（cuda）或 NPU 设备 index（npu））。所有训练渲染（baseline / 变体）必须显式 `--set device=<idx>`；缺 token 渲染 → fail loud。
- **baseline 占卡**：po_baseline 完整训练启动前占首空闲卡（`vid=baseline`），finalizer 终态释放；变体训练经 po_probe 同一 allocator。
- **不跨 run 抢占**：只做 run 内账本 + 真实占用双重确认；跨 run 锁冲突 → fail loud 披露，不做抢占。

---

## 4. 数据契约（新增 / 变更）

### 4.1 新增文件

| 文件 | schema 要点 | 消费者 |
|---|---|---|
| `variants/<vid>/business_logic.md` | 首行哨兵（business-logic-analyst 变体模式）；五段业务逻辑 + 「与基线差异」节 | propose 校验 / dashboard / web |
| `variants/<vid>/information_analysis.md` | 首行哨兵（information-analyst 变体模式）；信息核心 / 冗余近似项 / **被牺牲信息与预期精度代价** | propose 校验 / dashboard / web |
| `variants/<vid>/conformance.md` | 简短核验记录：两文档哨兵 + 「与基线主要内容对齐结论」+ 差异披露；**非逐条一致清单** | propose 校验 |
| `variants/<vid>/train_status.json` | `{"vid", "stage": "waiting|training|killed|done|failed", "epoch", "metric", "gap", "over_budget_streak", "stopped_at_epoch", "device", "ts"}`，原子替换 | watchdog / dashboard / gate |
| `variants/<vid>/watchdog.pid` + `watchdog.log` | watchdog 生命周期（同 finalizer 模式：自写 pid、setsid、日志带 ISO8601） | probe 重入 / report 收割 |
| `variants/<vid>/eval/final_acc.json` | `{"vid", "final_acc", "baseline_full_acc", "full_train_budget": <指纹>, "within_budget": null|bool, "metric_direction"}` | verdict_decide final-budget |
| `train_device.json` / `devices/<idx>.lock` | §3 | probe / watchdog / report |

### 4.2 变更文件

| 文件 | 变更 |
|---|---|
| `round_state.py` | `mode` 命令退役；`current` = rounds/ 最大数字目录；`working` = `max(current+1, 1)`（无 `.round_advanced` 联动） |
| `gate_decide.py` | 决策序改 §8；删 mode / accuracy_pass invariant / best 依赖 |
| `verdict_decide.py` | `promote` 退役；`final-budget` 保留（读 `variants/<vid>/eval/final_acc.json` 与 origin 锚） |
| `history_lib.py` | 退役 `append_advanced`；新增 `append_terminal(vid, outcome, gap, stopped_at_epoch, ...)`；`append_probe` 移除 |
| `run_latency_recheck.sh` | 判定统一为 `latency_pass ⇔ makespan ≤ target_cycles`（读 origin 锚）；双模退役 |
| `push_curves.py` | §10：top-10 line + 全量 pareto 双图 + 分析文档清单 chart（§10.4，推相对 path 不推正文）；盘面全量保留 |
| `dashboard_snapshot.py` / `experiment_ledger.json` | 增字段：`change_summary` / `latest_epoch` / `latest_metric` / `gap` / `status` / `device`；ledger 改为分片聚合派生物（§7.5） |

**新增脚本**：`ledger_aggregate.py`（§7.5，确定性聚合 `variants/*/ledger_entry.json` → 共享 `experiment_ledger.json`）。
| `check_propose_emit.py` / `check_probe_emit.py` | 对齐新产物（§5.5 / §6.2） |
| `templates/run_full_finetune.template.sh` | 新增 `device` token（§3.2） |

### 4.3 history 行语义 v6

| outcome | 含义 | 关键字段 |
|---|---|---|
| `impl` | 提案实现（含修复迭代，同一 vid 可多行） | change_sig / predicted_delta_cycles / change_summary / repair_count |
| `latency_pass` | 时延实测达线（≤ target_cycles），进入训练队列 | measured_makespan_cycles |
| `latency_fail` | 修复耗尽未达线 → 淘汰 | measured_makespan_cycles |
| `success` | 完整训练 + 最终 eval within_budget | final_acc / gap / stopped_at_epoch = E |
| `accuracy_fail` | 早停（连续 10 超预算被杀）或完整训练超预算 | gap / stopped_at_epoch / over_budget_streak |
| `probe_insufficient` | liveness / 训练失败重试耗尽 | stage / max_retries_hit |

退役：`advanced` / `promote_gate` / `proxy_acc`。**dedup 键不变**：`(vid, change_sig)`，修复迭代同 vid 同 sig 覆盖 latest。

---

## 5. po_propose 契约（单变体收敛环，D-V6-1/2/6）

### 5.1 Step 流程

- **Step0**：`deploy_scripts.sh --verify` + `round_state.py working`（R = 新变体轮）。
- **Step1 提案**：structure-proposer 以 `base/profile/mfu_bottleneck_report.md` 为瓶颈证据源，输入 = rules / history / 前序变体 profile 报告 / 上轮 `rounds/<R-1>/analysis.md`；**每轮恰 1 个 proposal**；准入 = `predicted makespan ≤ target_cycles`（origin 锚）。proposals.json：
  ```json
  {"round": R, "proposals": [{"vid": "r<R>-01", "lever": "...", "change_sig": "...",
    "target_modules": [...], "target_pattern_id": "<mfu 自由标签>", "rationale": "...",
    "change_spec": "...", "op_delta": {...}, "predicted_delta_cycles": -1234,
    "prediction_basis": "...", "edited_files": [...], "predicted_acc_impact": "low|medium|high",
    "accuracy_evidence": "...", "sota_reference": "..."}],
   "exhausted": false, "filtered_count": 0, "exhausted_rationale": []}
  ```
- **Step2 变体业务逻辑/信息分析（软对齐，D-V6-6）**：dispatch `business-logic-analyst`（变体模式）+ `information-analyst`（变体模式），产出 §4.1 两文档；node 机械校验（哨兵 + 非空 + 结论节存在）后写 `conformance.md`（含与基线差异摘要）。**口径**：与基线**主要内容对得上、讲得通即可**；不逐条一致、不要求信息完全保留；只有**主要语义冲突或文档自相矛盾** → 打回（repair_directive 注明原因）。stamp 键 = `vid + change_sig + repair_count`，修复后结构变化即重验。
- **Step3 实现 + 实测**：variant-implementer 实现 → mfu-analyzer 实测 → `variants/<vid>/verdict.json`。
- **Step4 修复内环（≤5，O-1）**：`makespan > target_cycles` → `repair_trace.json` 计数 + repair_directive = **最新 mfu 报告全文**（实测/预测差、剩余差距、瓶颈根因）→ implementer 修**同一 vid** → 重验（Step2 口径）→ 复测；第 5 次仍未达线 → `latency_fail` + failed_sigs 落 direction.json，本轮结束。
- **Step5 rules 增量刷新（D-V6-9）**：入口先处理"未消费的 terminal 结果"（history 中 `success`/`accuracy_fail`/`probe_insufficient` 且未被 rules 刷新标记消费的行）→ dispatch accuracy-analyst 增量更新 `accuracy_rules.json`（`rules_pool.py check` 校验，失败重派 1 次，再失败剔除坏行披露，不阻断轮次）。
- **Step6 落盘与 emit**：达线 → `append_impl` + `append latency_pass` 行、ledger 更新 `change_summary` → emit。

### 5.2 修复内环计数

`variants/<vid>/repair_trace.json`：`{"vid", "repair_count", "attempts": [{"round": R, "measured_makespan_cycles", "target_cycles", "gap_cycles", "reason"}]}`；`repair_count ≥ 5` → 禁止再修复（fail loud if attempted）。

### 5.3 校验（check_propose_emit.py v6）

- proposals.json 恰 1 提案、round 正确、`predicted_delta_cycles` 使预测 makespan ≤ target；
- 两分析文档 + conformance.md 在场且哨兵合法；
- 达线 vid 有 `latency_pass` 行；`repair_trace` 计数 ≤5；
- analysis.md（时延节）落盘。

---

## 6. po_probe 契约（资源分配 + 启动即放行，D-V6-3）

### 6.1 Step 流程

- **Step0**：`deploy_scripts.sh --verify` + 读 `variants/<vid>/verdict.json`：`makespan ≤ target_cycles` 是放行硬前提；不符 → fail loud（盘面撕裂：propose 与 probe 间 verdict 被改）。
- **Step1 资源检查**：`device_alloc.py free` + 逐卡 `O_EXCL` 占卡；无空闲卡 → **保持节点**（status 消息含 `do not call orca next`，每 turn 重查一次，不做 busy-loop）。
- **Step2 启动**：以 `full_train_budget`（epochs/seed 指纹不变式）render `run_full_finetune.template.sh`（`--set device=<idx>`）→ setsid 训练 wrapper（自写 pid/rc，同 v5 模式）+ **watchdog wrapper**（`watch_variant.sh --vid <VID> --device <idx>`，§7）。
- **Step3 liveness（更严一格）**：训练 pid 存活 + cmdline 归属 + train.log 出现 + **metric_curve 能解析出 epoch 1 指标行**（有界等 ≤15 轮询，每轮 ≤30s）；失败 → 重试预算 2 次（re-render 修正参数白名单，清部分产物）→ `probe_insufficient`。
- **Step4 emit executed**（不等训练完成；后续全归 watchdog）。

### 6.2 校验（check_probe_emit.py v6）

- verdict 达线、device 锁存在（vid 匹配）、训练 pid/watchdog pid 存活（或已终态 + 终态文件在场）、liveness 记录存在。

---

## 7. watchdog 契约（新增 `watch_variant.sh`，detached，D-V6-4/5/8）

### 7.1 生命周期

每变体一个 detached 守护（setsid + 自写 `watchdog.pid` + `watchdog.log` ISO8601）。职责 = 训练监督 + 流式早停 + 终局判定 + 状态推送 + 资源释放。训练进程死亡无 rc → 重派 ≤3（沿用 baseline finalizer 语义，部分产物清后重启）；重试耗尽 → `probe_insufficient`。

### 7.2 逐 epoch 判定

每周期（sleep 10）：

1. `metric_curve.py extract`（contracts 正则，全量重解析）→ 与 `baseline/baseline_metrics.jsonl` **同深度** compare（budget = origin 锚 `accuracy_budget`，方向 = contracts `eval.metric_direction`）→ `gap = normalized_loss`；
2. **warmup**：`epoch ≤ ceil(0.1 × E)`（E = 生效轮数）不判、不计数；
3. 计数：`gap ≤ budget → over_budget_streak = 0`；`gap > budget → over_budget_streak += 1`；
4. **早停**：`over_budget_streak ≥ 10` → 杀进程组（TERM → 10s grace → KILL，`/proc` cmdline 归属校验，复用 v5 stop_at_epoch 的 kill 语义）→ `stopped_at_epoch` = 冻结日志重解析值 → terminal `accuracy_fail`。

### 7.3 终局判定

自然跑完（rc=0）：最终 ckpt eval（复用 baseline finalizer 的 eval 链：resolve_ckpt + render eval + extract_metric）→ 写 `variants/<vid>/eval/final_acc.json`（`within_budget: null` 先行）→ `verdict_decide.py final-budget` 回填 → `success`（within_budget）或 `accuracy_fail`。

**baseline 锚点等待语义（P3 实现定案，2026-08-31 review 补录）**：`baseline_full_acc.json` 未落盘时**等待**（stage=final_eval_waiting，状态照常推送），绝不拿猜测值判定；bounded 判据 = baseline 自身终态文件（`train_final.json`）在场而锚点缺席（baseline 训练失败或盘面撕裂，锚点永不再产）→ 该变体终态 `probe_insufficient`、root cause = `baseline_anchor_unavailable`，不无限持卡。

### 7.4 每周期副作用

原子更新 `train_status.json` + `variants/<vid>/ledger_entry.json`（§7.5 单写者分片）→ `push_curves.py`（top-10 + pareto，§10）→ 终态时：聚合 ledger（§7.5）+ 释放 device 锁 + `append_terminal` history 行 + 写"rules 待刷新"标记（`variants/<vid>/.rules_pending`）。

### 7.5 ledger 并发写（单写者 + 派生聚合，2026-08-31 拍板：不丢信息）

多 watchdog 并行时**禁止**直接写共享 `experiment_ledger.json`（read-modify-write 互覆盖 = 丢行）。契约：

- **每个变体一个分片**：`variants/<vid>/ledger_entry.json`，仅该变体的 watchdog 独占写（原子替换，内容 = §7.4 的 epochs/metric/gap/status/device/change_summary）；单写者语义天然无竞争。
- **共享 `experiment_ledger.json` 变为派生物**：由**确定性聚合脚本** `ledger_aggregate.py` 生成——collect 所有 `variants/*/ledger_entry.json` + baseline 行 → 原子替换写共享文件。聚合为纯函数（同分片集 → 同输出），无锁、可重入。
- **触发**：①各 watchdog 终态时调用一次；②`dashboard_snapshot.py` 每次 collect 前先跑聚合（读路径顺带刷新，单一入口）；③propose 入口（Step5）兜底调用一次。竞态下最坏结果 = 聚合时刻略旧，下个触发点自动收敛，不丢任何分片数据。
- 分片文件是**真相源**，共享 ledger 任何时刻可由分片全量重建。

### 7.6 幂等

重入先读 `train_status.json` / pid / rc：终态在场 → 重放终态（不重启、不重杀）；训练存活 → 续监督；锁已释放 → 不再释放。

---

## 8. po_gate 契约（gate_decide.py v6，D-V6-10 / O-8）

### 8.1 决策序（first-match-wins）

```
1. history 任意 vid 存在 success 行        → report（winner = gap 最优 success，并列取 makespan 最优）
2. round ≥ max_rounds（硬帽，永不 loop）   → report（无 success → 等在飞训练终态后收尾，§9）
3. 其余                                  → loop（无其它出口）
```

读盘：history（terminal 行）/ `round_state current` / origin 锚。**不再读 best.json / mode / .round_advanced**。

**出环不打断存量（2026-08-31 拍板）**：success 行在场 → 下一次 gate 决策即出环进 report；loop 期间已放行的在飞训练**不停止**（不杀、不撤资源），由 report 终态收割（§9）等全部在飞终态后统一判 winner——出环只截断后续新提案轮，不影响已放行变体的训练与判定资格。

```json
{"decision": "report|loop", "round": 5, "target_cycles": 1604235,
 "success_vids": ["r3-01"], "in_flight": ["r5-01"], "reason": "..."}
```

### 8.2 gate_node.sh

先 `deploy_scripts.sh --verify`（不符 → finish-failed 分支披露版本戳不符）→ 调 `gate_decide.py --max-rounds "{{ inputs.max_rounds }}"` → route 按 `decision`：`report` → po_report，`loop` → po_propose。

---

## 9. po_report 契约（终态收割 + winner，D-V6-10）

- **终态收割**：终态前**无 kill 等待**在飞训练（每轮询 ≤60s/次，不主动杀——训练已占独立卡、成本已付）；全部在飞终态后统一判定；平台外部停机场景例外（kill + `aborted at terminal` 披露）。
- **winner**：success 变体中 `gap` 最小（并列 `makespan_cycles` 最小）；无 success → no-promotion 披露（dashboard 引用）。
- **写回**：`<原名>_prof_optimized.<ext>` 新文件名、冲突不覆盖、写回前复验结构锚——继承 v5 不变。
- **报告**：引用 dashboard（改动摘要 / 曲线 / 帕累托 / gap 表 / 分析文档清单）；rules → per-wf KB 同步（成功失败皆同步）——继承 v5。
- 报告首段披露 profiling 模式 + 训练设备后端 + 部署件版本戳。

---

## 10. 前端推送契约（D-V6-5，web 侧消费）

### 10.1 top-10 曲线（line）

`push_curves.py` collect 扩展：baseline 恒在 + 至多 9 个变体曲线；选择策略（O-2 采纳）：①在飞训练（有曲线且未终态）按最近更新时间排序优先；②终态达线（success）次之；③其余按 gap 升序。盘面全量曲线文件保留，只收窄推送。

### 10.2 全量帕累托（pareto）

`chart_type="pareto"`，x = 相对基线降幅 %（`1 - makespan/baseline_makespan`，负值 = 更慢），y = 最终指标或 gap；全量变体一个点，状态着色（success / in-flight / accuracy_fail / latency_fail / probe_insufficient / 达线未训占位 y=null 披露）。payload 复用现有 chart socket 字段（label/title 幂等替换）。

### 10.3 推送节奏

watchdog 每周期推 live（`title` 后缀空）；report 终稿推 `(final)` 后缀——幂等替换语义不变。`dashboard_snapshot.py` 同步扩展（§4.2），供 web 静态读取。

### 10.4 分析文档清单（chart 相似路径，2026-08-31 拍板）

分析文档（business_logic / information_analysis / mfu 报告 / conformance）的推送**复用 chart 通道**——与 render chart 相似路径，区别只是 payload 指向 md 文件而非图表数据：

- `chart_type="table"`，label `prof-opt/docs`；行 = vid（或 baseline）/ 文档名 / 状态 / **相对 path**（相对 artifacts 根）。
- **只推 path 不推正文**——面板列表只显示名称/状态，正文点开后由 web 端经 artifacts 只读端点拉取渲染（见 `prof-opt-web-view-spec.md` §2），避免推送体量爆炸。
- 触发时机：propose 达线 emit 前 + report 终稿；幂等替换（label/title 语义与 chart 一致）。
- path 白名单约束：只列本 run artifacts 内相对路径，前端不得自行拼任意路径。

---

## 11. workflow.yaml 变更汇总

| 项 | v5 | v6 |
|---|---|---|
| 节点数 | 8 | **7**（删 po_full_train） |
| po_gate routes | full-train / full-train-best-effort / finish-failed / loop | report / loop |
| po_propose output_schema | status/error/generated_artifacts（含 mode/advanced_vid） | status/error/generated_artifacts（+ repair_count） |
| po_probe output_schema | status/error/generated_artifacts（含 mode/accuracy_pass_vids） | status/error/generated_artifacts（+ device/epoch1_ok） |
| 顶层 description | 双相门控 + full-train | 单变体收敛 + 异步流水线 + 流式早停（逐字更新） |
| outputs | 全读 po_report.output | 不变（仍全读 po_report.output） |

回边、路由 when（`status == 'executed'`）、catch-all → po_report 不变。

---

## 12. 子代理面变更

| 子代理 | 变更 |
|---|---|
| `structure-proposer` | ≤3 → **每轮恰 1 提案**；准入 `predicted makespan ≤ target_cycles`；组合式设计每轮可用；输入含上轮 analysis.md / rules / failed_sigs 并集；`target_pattern_id` 保持 mfu 自由标签 |
| `business-logic-analyst` | 新增**变体模式**：产出 `variants/<vid>/business_logic.md`（五段 + 与基线差异节），输入 = 基线 business_logic.md + 变体 shadow + 改动说明；哨兵不变 |
| `information-analyst` | 新增**变体模式**：产出 `variants/<vid>/information_analysis.md`（信息核心 / 近似与牺牲项 / 预期精度代价），输入 = 基线 information_analysis.md + 变体 shadow；哨兵不变 |
| `accuracy-analyst` | 触发时机改为"terminal 结果未消费时由 propose 入口增量 dispatch"（D-V6-9）；schema 不变 |
| 新增：无 | conformance 核验由 node 机械校验 + 两 analyst 结论节完成（不新增子代理） |

---

## 13. 测试与验收

### 13.1 单元/脚本测试（新增 `tests/test_po_v6.py`，改 `tests/test_po_scripts.py`）

1. `device_alloc.py`：占卡/释放/pid 死亡回收/满卡阻塞语义（synthetic）。
2. `watch_variant.sh`：warmup 前不判；连续 9/10/11 超预算边界（mock 曲线）；自然完成 → final-budget 判定 success/fail；kill 归属校验拒绝误杀。
3. `round_state.py working = current+1`；`gate_decide.py` 决策序 3 分支。
4. `history_lib.append_terminal` 行语义 + dedup 键。
5. `run_latency_recheck.sh`：判定 = makespan ≤ target（含边界 =）。
6. `push_curves.py`：top-10 选择策略、pareto payload、mock socket 幂等。
7. `dashboard_snapshot.py` 扩展字段。

### 13.2 场景验收（脚本级 smoke 序列）

1. **单变体收敛环**：构造距 target 有差距的 mfu 报告 → 同一 vid 迭代修复达线（≤5），全程不派生新 vid；一次"与基线不同但讲得通"的改动放行、一次"主要语义冲突"被拦。
2. **异步多卡**：2 卡 + 2 变体先后进 probe → 并行；卡满 probe 阻塞（status 消息）；终态释放锁后新变体进。
3. **流式早停**：前 10% 不判；连续 10 超预算被杀；曲线/终态/dashboard 更新。
4. **gate/report**：success 出现 → report；轮帽 + 在飞 → 等终态后 report。

### 13.3 回归

`tars validate` 0 error 0 warning；prof-opt 相关现有单测全绿；洁净检查（prompt 无开发期残留）warning 清零。

---

## 14. 失败路径矩阵（fail loud）

| 场景 | 行为 |
|---|---|
| train_device.json 解析失败 / 无后端 | flatten fail loud → po_report |
| 设备锁 pid 死亡残留 | free 时回收 + 披露 |
| probe 无空闲卡 | 保持节点等待（status 消息），不 emit |
| 训练 liveness epoch1 超时 | 重试 ≤2 → probe_insufficient |
| watchdog 训练崩无 rc | 重派 ≤3 → probe_insufficient |
| 早停 kill 归属校验失败 | 拒绝杀 + FATAL（盘面撕裂） |
| 修复内环超 5 次仍尝试 | fail loud（脚本拦截） |
| verdict 与 target 不符进入 probe | fail loud（盘面撕裂） |
| gate 决策前 deploy 戳不符 | finish-failed 分支 + 披露 |
| report 等在飞超平台期 | 外部停机 → kill + aborted 披露 |

---

## 15. 文件触达清单

**新增**：`agents/_po_scripts/resolve_train_device.sh`、`agents/_po_scripts/device_alloc.py`、`agents/_po_scripts/watch_variant.sh`、`tests/test_po_v6.py`。
**修改**：`workflow.yaml`、`agents/po_propose/agent.md`、`agents/po_probe/agent.md`、`agents/po_report/agent.md`、`agents/po_baseline/agent.md`、`subagents/structure-proposer.md`、`subagents/business-logic-analyst.md`、`subagents/information-analyst.md`、`subagents/accuracy-analyst.md`、`agents/_po_scripts/round_state.py`、`gate_decide.py`、`verdict_decide.py`、`history_lib.py`、`run_latency_recheck.sh`、`push_curves.py`、`dashboard_snapshot.py`、`experiment_ledger.py`、`check_propose_emit.py`、`check_probe_emit.py`、`deploy_scripts.sh`、`templates/run_full_finetune.template.sh`、`tests/test_po_scripts.py`。
**删除**：`agents/po_full_train/`（agent.md + references/ + scripts/）、`agents/_po_scripts/advance_round.py`、`stop_at_epoch.sh`。

---

## 16. 遗留与依赖

- **Web 展示**：依赖本 SPEC §4.1（分析文档盘面文件）+ §10（曲线/帕累托推送）；后端文档端点 + 前端面板见 `prof-opt-web-view-spec.md`。
- **实施排期**：P0 数据契约与共享脚本 → P1 propose 收敛环 → P2 probe 资源分配 → P3 watchdog → P4 前端推送 → P5 收尾回归。
- **真机清单（归用户）**：npu/cuda 多卡分配、真实 mfu 评测、长训练早停、web 文档面板联调。
