# prof-opt v7 SPEC —— mfu 唯一链路 + 判断力归还 + 校验全覆盖

> 状态：待实现（2026-09-01 用户拍板，全部采纳推荐项）。
> 本文是实现的**唯一契约**。与 v6 spec / 2026-08-31 两份 release note 冲突处，以本文为准
> （两份 08-31 note 需在文首标注 superseded-by-v7）。
> 分支：`puzzle-supernet`。

## 0. 背景（为什么有 v7）

v6 真机首跑暴露四类病根，用户确认：

1. **对用户工具的误解**：`mfu_benchmark` 是用户**内网自研**的 profiling 工具——远程提交、
   直接返回完整结果、**与运行平台无关**（目标机不一定有 npu-smi）。v6 的「env → 本机
   npu-smi → fallback 本地估算」模式嗅探因此静默降级，整条链走了估算路径。
   **用户原话：这里不能有降级，没有使用 mfu-analyzer 调用 mfu-benchmark 就是不对的。**
2. **判断力与机械活分错工**：该 agent 看着办的事被写成死规则（设备占用解析、封闭
   top-N schema、MFU<30% 阈值）；该脚本算的事反而让 agent 手算（shape 分箱、置信度阶梯）。
3. **契约漂移**：同一规则抄三遍、说明书写岔（说四个列五个、读不存在的字段）、08-31
   刚建立的机械校验在 v6 重写中被静默丢弃两次（information-analyst 产出方、成员校验）。
4. **静默失败**：降级、推送失败、文件损坏、解析失败，全部不吭声。

用户抱怨的一一对应：撞 busy 卡→病根2；P1~P9 JSON→病根1+2；propose 读无关 md→病根3；
information_analysis.md 缺失→病根3+4；基线没做真实测量→病根1；md 没推 web→病根4。

## 1. 设计不变量（实现与后续演化的红线）

1. **mfu 唯一**：`mfu-analyzer` 调 `mfu_benchmark`（用户内网工具）是唯一 profiling 方式。
   禁止估算路径、禁止环境嗅探、禁止静默降级。本地 E2E 走同一链路（仓库内
   mfu_benchmark.py 是 CLI 形状替身，真机换真脚本，CLI 契约不变）。
2. **判断归 agent，机械归脚本**：解读 vendor CLI 原文、判断瓶颈根因、选择设备、判断
   结构语义 → agent；算术、加锁、去重、格式校验、置信度推导 → 脚本。
3. **分析产物 = 一份人读的 md 报告**；proposer 只读报告。机械 JSON 只作为定价过程件。
4. **每个产物都有明确的三元组**：写者 / 读者（消费者）/ 校验门。见 §13 对照表。
   缺任一格 = 设计错误。
5. **一处一真相**：判定谓词、准入条款、枚举、契约只有一处定义；提示文档引用不复抄。
6. **静默改披露**：降级、跳过、无法验证、推送失败、解析失败——必须写进产物或报告，
   不允许只有 stderr（或连 stderr 都没有）。

---

## 2. inputs 变更（workflow.yaml）

| input | 变更 | 说明 |
|---|---|---|
| `profile_chip` | **新增 [ask] required**，enum `6613\|1951` | 用户内网评测工具的芯片参数，显式给，不再嗅探 |
| `profile_precision` | **新增 [advanced]**，default `INT8`，enum `INT8\|INT16\|AMP` | 同上 |
| `profile_core_num` | **新增 [advanced]**，default `1`，enum `1\|2\|4` | 同上 |
| `max_rounds` | `[advanced]` → **`[ask] required`** | 它是全 workflow 唯一成本闸门，是「愿意烧多少」的业务决策 |
| `idle_round_cap` | **新增 [advanced]**，default `5` | 连续 N 轮零提案 → gate 出环收尾（见 §8），防搜索空间真耗尽时空转 |
| 其余 | 不变 | project_root / model_path / latency_reduction_min / accuracy_budget / seed / fresh_start / full_train_epoch_cap |

- 三个 profile 参数记入 `contracts.json`（值级指纹的一部分）；变更需 `fresh_start=true`
  （与达标线/精度预算同轨，origin 锚不可变）。
- workflow.yaml **description 重写**：去掉一切「从 baseline 变异/血缘」措辞，改为
  「每轮独立提案（历史作证据非血缘，可大步修改）」；明确「唯一 profiling 方式 =
  mfu-analyzer 调用用户内网评测工具」；补静态 shape / opset17 准入条款（见 §11-F8）。

---

## 3. profiling 唯一链路

### 3.1 删除
- `agents/_po_scripts/resolve_profile_mode.sh`（整文件）
- `agents/_po_scripts/placeholder_profiler.py`（整文件）
- `subagents/bottleneck-analyst.md`（整文件）
- `agents/_po_scripts/check_bottleneck.py`（整文件）
- `profile_mode.json` 及其全部读方（po_propose Step1、structure-proposer、
  check_propose_emit 的 mode 分支、run_baseline_chain.sh 的 NPU_CHIP 逻辑、
  po_flatten 的解析调用与 reuse 比对）
- po_propose Step1-pre 的 `base/.bottleneck_stamp` 机制
- workflow.yaml / agent.md 中一切 placeholder|mfu 双模措辞

### 3.2 基线 profiling（run_baseline_chain.sh step2 重写）
```
base/model.onnx（step1 导出，不变）
  → 节点派发 mfu-analyzer（见 §4 派发协议）
  → 原始产物落在 base/profile/<onnx_stem>/（analyzer 只读约定不变）
  → scripts/mfu_adapter.py --profile-dir base/profile（不变，fail loud 语义保留）
  → 四件套 taskgraph.json / ops.csv / schedule.json / profile_summary.json
  → scripts/analyze.py --profile-dir base/profile（不变）
  → base/bottleneck_report.json（机械底册：predict_delta 定价依据，非 proposer 证据）
```
- chain 的 mfu 等待握手（rc 3 = awaiting analyzer raw products）**无条件保留**——不再有
  placeholder 分支；stdout running 消息含派发参数（onnx/profile_dir/report/chip/precision/
  core_num，值来自 contracts.json）。
- `base/profile/mfu_bottleneck_report.md` = 瓶颈分析报告（proposer 的唯一瓶颈证据源）。

### 3.3 变体 profiling（po_propose Step3）
- 每个变体无条件派发 mfu-analyzer（`variants/<VID>/onnx/model.onnx` →
  `variants/<VID>/profile/`）+ mfu_adapter。原「ONLY when mode==mfu」条件删除。

### 3.4 mfu-analyzer.md 重写（version 2）
- frontmatter `version: 2`，哨兵行 `[subagent:mfu-analyzer v2 MBA7K2]`（哨兵码不变，
  版本号升级；所有引用方与测试同步更新）。
- 报告模板重心反转：**「瓶颈根因」为主位**（1-3 个根因，区分表象与根因），算子级
  证据表降为「按显著性列行」（不固定 Top-5；说明列写「为什么它是/不是瓶颈」）。
- 删除：`MFU<30%` 硬阈值（改为「MFU 相对同类算子是否显著异常」的判断语言）、
  「增加 --core-num / 缩小 batch」等配置类建议（不是 model-source 结构修改，与 proposer
  硬约束 1 冲突）、「内存超 L1D 实际 cycles ≈ 测量值 × batch 缩放比」的心算教学、
  重复两遍的 CLI 参数表（保留一处用法 + 指向 `--help`）。
- 保留：H1（必须解析实际文件）、H2（量化说话）、H6（原始产物只读）、失败也要分析
  （H5）、哨兵协议、幂等复用。
- 知识库收缩为「根因类型提示」四条（DMA 搬运 / 小算子碎片 / 子图串行化 / 算力利用率），
  只作诊断词汇，不开结构药方（结构先验归 structural-levers 单一来源）。

---

## 4. 基线阶段：产物与校验门

### 4.1 基线产物（三份分析文档 + 既有链路产物）
| 产物 | 写者 | 时机 |
|---|---|---|
| `baseline/business_logic.md` | business-logic-analyst（**仅 baseline 模式**，variant 模式删除） | 训练启动确认后并行 |
| `base/information_analysis.md` | information-analyst（**仅 baseline 模式**；产出方接活） | 与 business-logic 并行 |
| `base/profile/mfu_bottleneck_report.md` | mfu-analyzer | step2 等待握手期间 |

### 4.2 information-analyst.md（version 2，baseline 模式唯一）
- 四节恢复（2026-08-31 设计）：`## 信息成分拆解` / `## 最小信息核心` / `## 冗余与可近似项`
  / `## 创新结构方向`。
- 删除「2-5 个方向」数量强制 → 改「至少一个实质性方向；若确无，需论证 lever 目录已
  覆盖并给出理由」。
- 删除枚举式负面清单（硬编码四个 lever 家族名）→ 改一句原则「不得把 structural-levers
  目录条目换皮重述」。
- variant 模式章节整体删除（并入新 variant-assessor，见 §5.3）。

### 4.3 校验门（新增 `agents/po_baseline/scripts/check_baseline_docs.sh`）
- 替换 `check_business_logic.sh`。逐一校验三份文档：文件存在、非空、首行哨兵正确
  （BLA / IXA / MFU v2 三条哨兵字面量在本脚本一处定义）、必需 section heading 齐全。
- chain step7（emit gate）从「business_logic.md 在场」扩为**三份全在场**；缺任何一份 →
  stdout running（列明缺哪份）+ 节点按失败矩阵补派，直到齐全或 re-dispatch 预算耗尽。
- 基线 executed 的 `generated_artifacts` 必须含三份文档路径。

### 4.4 设备接入（run_baseline_chain.sh）
- 新增必填参数 `--device <idx>`：由**节点 agent**先跑 `device_alloc.py probe`（§6），
  读原始占用原文判断空闲卡，把选定的 idx 传给 chain；chain 内部用该 idx 执行 claim。
  缺参 → fail loud（stderr 指引：先 probe 再选卡）。
- claim 返回 ok:false（该 idx 已被锁）→ chain stdout running（含原因 + 「re-probe 并换卡」
  指引），节点 park。**基线满卡不再 fail loud**——agent 现在能看见真实占用，park 是收敛的
  （下一轮重新 probe 选卡），与 probe 节点语义统一。
- 其余不变：claim→render(--set device)→detach wrapper→adopt→finalizer 终态 release。

### 4.5 baseline 首推 docs manifest
- 节点在 executed 前执行 `python3 scripts/push_curves.py --artifacts . --docs`（best-effort，
  失败只 stderr note）。保证 web 文档清单从基线阶段就开始可见，不必等第一个变体达线。

---

## 5. propose：单变体收敛环 v7

### 5.1 Step1 瓶颈证据（简化）
- 删除 placeholder 分支与 bottleneck-analyst 派发。Step1-pre 只做：
  `analyze.py --profile-dir base/profile` 刷新机械底册（幂等）+ 校验
  `base/profile/mfu_bottleneck_report.md` 存在且首行哨兵 = `[subagent:mfu-analyzer v2 MBA7K2]`
  （fail loud，缺失说明基线阶段未完成）。
- proposer 输入 `<info_analysis>`、`<baseline_doc>` 等不再「when it exists」静默可选：
  四份基线文档（business_logic / information_analysis / mfu 报告 + 机械底册路径）**必须
  在场**（§4.3 门保证）；仍可缺席的只剩 `<prev_analysis>`（round 1）与 `<prior_reports>`
  （首轮无变体），缺席语义在 proposer.md 写明。

### 5.2 预测降级（predict_delta.py）
- **准入硬门只剩一条**：`predicted_delta_cycles` 为负整数。「预测达线
  （base+delta ≤ target）」从 gate 降为**参考披露**——写入 round analysis 的
  校准注记，`check_propose_emit` 不再因预测不达线而拒。
- **关键路径加权**：predictor 消费 `base/bottleneck_report.json` 的 `critical_path`
  逐站点名单（analyze.py 已产出）。受影响站点在关键路径上 → 权重 1.0；不在 → 权重 0.25
  （启发式，stdout 披露 `{on_path_cycles, off_path_cycles_weighted}` 与所取权重）。
- **shape class 确定性推导**：受影响站点的 shape class 由 predictor 从
  `base/profile/taskgraph.json` 按 node name 自行推导；删除「LLM 手算 element count
  落箱 + 惩罚性全量上界」的 `--sites` 外包模式（参数删除）。
- structural-levers 与 proposer 文档同步：`sota_reference` 允许 null + 一句「为何无先例」；
  `exhausted` 恒 false 死字段删除（`exhausted_rationale` 保留）。

### 5.3 双 analyst 合并为 variant-assessor（新 subagent）
- 新文件 `subagents/variant-assessor.md`，version 1，哨兵新值（如
  `[subagent:variant-assessor v1 VAS4K9]`）。
- 输入：`<output_dir>`、`<doc_path>`=variants/<VID>/assessment.md、
  `<baseline_business_logic>`=baseline/business_logic.md 全文、
  `<baseline_information>`=base/information_analysis.md 全文、`<change_note>`=提案的
  change_sig/change_spec/rationale。
- 输出 `variants/<VID>/assessment.md` 六节：
  `## 任务语义` / `## 输入输出` / `## 架构动机` / `## 逐模块职责与物理意义`
  （含信息视角：该模块携带什么信息）/ `## 训练目标与指标方向` / `## 与基线差异`
  （内含 `### 被牺牲信息与预期精度代价` 子节）。
- 删除：business-logic-analyst 与 information-analyst 的 **variant 模式**章节；
  `variants/<VID>/conformance.md` 整体删除（哨兵复述是 stamp 套 stamp；对齐结论并入
  轮次 analysis.md，由节点 agent 写）。push_curves 的 conformance 行删除。
- 节点软对齐判断保留：读 assessment.md 的结论节（与基线差异 + 被牺牲信息），只有
  「破坏 I/O 契约 / 打破模块文档角色 / 自相矛盾」才打回（打回 = 重派 implementer with
  `analysis:` 前缀指令 + 删 stamp + 重评）。
- **stamp 修复假保证**：`.analysis_stamp.json` 键改为
  `<VID>|<change_sig>|sha256(variants/<VID>/declaration.json)`——两类修复（latency /
  structural）都会重写 declaration.json，键随之变化，机械防重入真正成立。

### 5.4 修复内环（语义不变，澄清两点）
- latency_fail 修复 ≤5（repair_trace 脚本计数不变）；structural_mismatch / variant_broken
  联合预算 ≤2（history dedup 机械计数不变）。
- `variant-implementer.md` 增补 `analysis:` 前缀语义；`latency:` 载荷描述改为
  「mfu 模式 = 最新 mfu 报告全文」。
- mfu 模式下 latency 修复时删除 `variants/<VID>/profile/` 的规则保留（analyzer 幂等
  复用会吃掉旧产物）。

### 5.5 提案派发洁净（用户点名）
- po_propose/agent.md 的 Subagent Call Protocol 重写：**每个被派 subagent 的返回契约**
  （哨兵字面量 + 返回格式 + 产物路径 + 校验门名称）直接内联在 po_propose 自己的说明书，
  并加一条明文规则：「**禁止读取 `{{ subagents_root }}/` 下任何文件**——你需要的契约
  都在本文件内」。现盘点：bottleneck-analyst（已删）、variant-assessor、
  variant-implementer（DONE/terminal-skip 返回语义）、mfu-analyzer、accuracy-analyst
  五个派发面全部补全契约描述。

---

## 6. 设备分配：agent 判定 + 账本锁（device_alloc.py）

### 6.1 操作面（v7）
```
probe  --artifacts <ws> --backend <npu|cuda>
    输出单行 JSON：{"backend","device_count","locks":[{idx,vid,pid,acquired_at}],
                    "raw":"<后端 CLI 原始完整 stdout>"}
    不做任何解析、不产 busy 集合。CLI 不存在/失败 → exit 2（fail loud，说明无法观测）。
claim  --artifacts <ws> --vid <VID> --idx <N>
    idx 越界 → exit 2；O_CREAT|O_EXCL 创建 devices/N.lock；已存在 →
    {"ok":false,"reason":"device N locked by vid=<...>"}（rc 0，调用方换卡）。
adopt  --artifacts <ws> --vid <VID> --pid <PID>   （不变）
release --artifacts <ws> --idx <N>                 （不变，幂等）
```
- **删除**：`free`、无 `--idx` 的自动 `claim`，及 `_npu_occupancy / _cuda_occupancy /
  _csv_rows / _real_occupancy` 全部解析代码（~90 行）。
- `_pid_alive` 抽为共享模块 `_po_scripts/pid_lib.py`（device_alloc 与 check_probe_emit
  共用，消除两份漂移拷贝）：posix 用 `os.kill(pid,0)`；**无法判定（非 posix / 无 /proc）→
  返回 unknown，调用方必须披露「liveness unverifiable」，禁止当 alive**。

### 6.2 调用方
- **po_probe / probe_protocol.md 重写**：probe（看原文）→ agent 选卡 → `claim --idx` →
  render（`--set device=<idx>`；shadow_pkgs 经 `shadow_pkgs_csv.py` 获取，删除内联
  heredoc one-liner）→ detach → adopt → liveness → emit。锁被占 → 换卡或 park。
- **run_baseline_chain.sh** 见 §4.4。
- verdict 前提检查（probe_protocol 里 20 行 heredoc）落成部署脚本
  `agents/_po_scripts/check_verdict.py --vid <VID>`；同时作为「达线判定」的唯一实现，
  run_latency_recheck.sh 与 check_probe_emit.py 改为调用它（消除三处手抄）。
- **po_report 补上真实的卡释放兜底**：终态收割时对每把 `devices/*.lock` 检查 owner pid，
  已死 → `device_alloc release`（watch_variant 里「report sweep covers it」的注释从此为真）。

---

## 7. probe liveness 与 watchdog 重写

### 7.1 probe liveness（po_probe）
- 四条件 → **两条件**：训练 pid alive（归属校验）+ train.log 出现。窗口 ≤15×30s。
- 「epoch-1 指标行可解析」**从 probe 移除**，归 watchdog（它本来就 10s 一轮在解析；
  慢首轮不再被 7.5 分钟死窗冤杀成 probe_insufficient）。
- liveness 失败重试 ≤2 → probe_insufficient 语义保留。

### 7.2 watch_variant.sh → `watch_variant.py`（整体重写，stdlib-only 常驻进程）
契约逐条移植 bash 版并修复其已知缺陷：

| 机制 | v7 契约 |
|---|---|
| 轮询 | 10s 单循环；SIGTERM → kill 训练进程组 + 写终态 + release 卡 |
| 曲线 | metric_curve extract 增量；**同 epoch 多行 → 最后一行胜出并披露一次**；区分「还没有行」与「pattern 不匹配」（后者 fail loud 点名 pattern） |
| train_status | stage ∈ waiting\|training\|killed\|done\|failed；等 baseline 锚期间**保留最后已知 epoch/metric/gap**（修 B12 清空回退） |
| 流式早停 | warmup = ceil(0.1×E)；超预算连续 streak ≥ max(2, ceil(0.3×E)) → 归属校验后 kill → stage=killed（阈值进 contracts.json，不再写死 10） |
| 崩溃 | 训练进程无 rc 死亡 → 记录 + stage=failed（crash 归属与日志路径披露） |
| 终态链 | final_check（**stderr 原样落 watchdog.log**，失败归因不再猜测）→ full eval → k eval（ckpt_per_epoch）→ final_acc.json → stage=done → 写 `.rules_pending` → release 卡 |
| 心跳 | 每轮 `touch $ORCA_ARTIFACTS_DIR/.run_lock`（配合 baseline finalizer 同样 touch，修 §11-F5 长训练期间心跳无人续写 → 30 分钟后被 fresh_start 整仓 wipe 的雷） |
| 日志 | 全部诊断进 `variants/<VID>/watchdog.log`，stderr 不吞 |

- baseline finalizer（run_baseline_chain.sh --finalizer）保持 bash，最小修复：
  final_check stderr 落 finalizer.log、每轮 touch .run_lock、incremental_curve 失败原因
  落日志（不再静默 rm tmp）。

---

## 8. gate（gate_decide.py + workflow.yaml）

- 三分支保留：report（success 存在 / round ≥ max_rounds）/ loop / **新增 idle 出环**：
  从最新轮往回数，连续 `idle_round_cap` 轮 `proposals.json.proposals == []` →
  decision=report，reason=`idle_exhausted`（披露连续轮数）。永不回边原则不变。
- `round_state.py` / 三分支路由 / catch-all 兜底不变。

## 9. report 与 web 推送披露（po_report）

- **推送披露**：报告首段固定披露三行——profiling 来源（「mfu 实测 via 用户内网评测工具」）、
  训练设备后端、chart daemon 状态（读 `.chart_push.log` 末行 + 本次推送的 pushed 字典；
  离线/失败要写明，不许沉默）。
- **卡释放兜底**见 §6.2。
- **产物清理**：`pretrained_ref_acc` 输出字段删除（v5 遗留，workflow.yaml schema 同步）。
- report_format.md 与 agent.md 的三处命令失败语义统一：experiment_ledger /
  dashboard_snapshot 失败 = builder 失败（fail loud），仅 push_curves best-effort；
  rules merge 失败写进 reason。
- 归档名单去掉 `accuracy_rules.md`（Step2b 是唯一写者，消除三写两策略）。
- rules archive/merge 语义见 §10。

## 10. 规则池简化（rules_pool.py + accuracy-analyst.md）

- **删除跨模型 pool 机制**：SOURCE_RANK 四级来源、pool_borrowed 降级、confirm/refute
  集合、general/quarantine 阈值、跨模型反向 refute——单用户单机现实下不可达，且制造了
  破坏性覆盖路径。规则真相源 = 项目镜像 `docs/prof-opt/accuracy_rules.json` + 工作区
  `accuracy_rules.json`（快照进 `base/accuracy_rules_snapshot.json` 供面板，不变）。
- **破坏路径修复**：merge 时源文件 unparseable → exit 2 拒绝（不再以空规则集覆盖镜像）；
  空规则集覆盖非空镜像需显式 `--allow-empty`。
- **accuracy-analyst.md 重写（version 2，瘦身）**：LLM 只产出三个判断值——
  pattern 归一化、有无可沉淀教训（唯一真判断）、statement 文本；**confidence 由
  `rules_pool.py apply` 按证据轮数机械推导**（阶梯实现已在 `_confidence_by_evidence`，
  提为 apply 子命令）；`generality` 死字段删除。
- history dedup 对 probe_insufficient 的提示改为「该签名已永久消费」（数据旋钮机制
  删除后不存在重试路径，见 §12）。

## 11. 一处一真相与说明书修正（逐条执行）

**po_flatten**
- F1 「in your reply 的 checklist」与「final reply 只能单行 JSON」矛盾 → 改「中间轮内部
  checklist，最终回复仅 JSON」。
- F2 **预训练 ckpt 空壳整体删除**：pretrained_loadable 恒真检查、readiness 的
  pretrained_ckpt/ckpt_sha256/container_key 字段、write_baseline_lock `--ckpt`、
  reuse_check/check_flatten 的有/无 ckpt 双分支、恒真布尔校验。
- F3 agent.md 不复抄 ORCA_PO_NPU_*/设备枚举（枚举校验归脚本一处）。
- F4 extract_user_pkg 改用 `$ORCA_PYTHON` + `PYTHONPATH=$PROJECT_ROOT`，stderr 落日志，
  判定不确定的名字显式列出交 agent 复核。
- F5 `.run_lock` 心跳改由 watchdog + finalizer 续写（§7.2）；reuse_check 的 stale 判定不变。
- F6 两个解析器统一重建语义：NO_REUSE 重建路径上 profile 参数与 train_device **都**
  重新解析并覆盖写入（训练设备后端不再静默沿用旧文件）。
- F7 `<base_name>_flat.py`（Step 6）删除——零消费者。
- F8 静态 shape / opset17 / 禁 dynamic_axes 保留为准入条款，但必须写进 workflow.yaml
  description 显式告知用户（含「动态 shape 模型不在范围」）。
- F9 check_flatten.sh 解释器优先级注释与代码对齐。
- F10 metric direction 校验按 manifest 清单逐指标核对（不再全文 grep 一次）；manifest 与
  contracts 的 higher-better/higher_better 拼写统一为一套。
- F11 emit 示例数组删除字面 `...`，给出完整可照抄清单（含 profile 三参数落盘产物）。

**po_contract**
- C1 REUSE 分支「read readiness_path」→「read contracts_path」（本节点 schema 字段）。
- C2 「four subagents」→ 实数（五）。
- C3 reuse gate 补 `viable is true` 检查（viable=false 工作区禁止被复用为 true）。
- C4 exit 码分语义：版本旧 / sha 漂移分码（对齐 flatten 门禁的 0/1/2/3 风格），agent.md
  同步。
- C5 Step7 补齐 `proxy_budget_selection.json` 完整字段集（含 rationale）。
- C6 **数据旋钮机制整体删除**：dataset_knob/data_value/max_steps 字段、一致性校验、
  模板 token 对称校验（约 50 行不可达逻辑）；proxy_budget 固定
  `epochs=min(1,...)=1`、其余 null 的现状成为唯一合法形态并写死在门禁。
- C7 sitecustomize 合并后重跑：由节点用既有模板重渲染 eval dry-run 并覆盖
  eval_dual_ckpt.json 证据文件；`sitecustomize_merge` 字段纳入 check_contracts 校验。
- C8 准入条款改稳定键：contracts.json 记 `admission_clause_ack: true`，条款全文只在
  po_contract/agent.md 一处；check_contracts 不再做中文句逐字 checksum。
- C9 双训练模板留一份（probe/full 各自渲染命名），删逐字节比对门。
- C10 `shadow_pkgs_csv.py` 接入 probe 渲染（§6.2），不再是孤儿。

**po_probe**
- P1/P2/P3/P4/P5：见 §6.2、§7.1；heal 台账（`.po_probe_healed.txt` + healed_files.py）
  删除——只写不读。
- P6 pid_lib 共享（§6.1）。
- P7 deploy_scripts --verify 失败文案按语境区分：中游节点指向「重新部署/人工介入」，
  不诱导 fresh_start wipe 在飞训练。
- P8 见 §7.2 曲线行。

**po_propose**
- check_propose_emit.py：assessment.md（新哨兵 + 六节 + `### 被牺牲信息与预期精度代价`）
  替换两文档+conformance；删除预测达线检查（保留 delta<0 负整数）；profile_mode 分支
  删除；emit 产物清单同步（删 bottleneck_analysis.json / conformance，加 assessment.md）。
- analysis.md 软对齐结论并入（替代 conformance.md 的记录职能）。

**文档同步**
- `docs/releases/2026-08-31-prof-opt-information-analysis.md` 与
  `...-mfu-bottleneck-source.md` 文首加一行 `> SUPERSEDED by docs/specs/prof-opt-v7-spec.md`。
- structural-levers.md：删「Proposal admission checklist」与「Pareto ranking contract」
  两节（字段名与 proposer 矛盾 + 多提案时代遗留）；精度词汇统一为 low/medium/high；
  重组为「按 trigger op 索引」的按需字典，删除「read it first / 每轮通读」要求；与
  mfu 根因类型四条对齐标注。
- workflow.yaml 节点注释全面更新（§3/§4/§5/§6/§7/§8 对应）。

## 12. 删除清单（汇总，实现后须全部不存在）

```
agents/_po_scripts/resolve_profile_mode.sh      agents/_po_scripts/placeholder_profiler.py
agents/_po_scripts/check_bottleneck.py          subagents/bottleneck-analyst.md
profile_mode.json 读写与双模分支（全链）          base/.bottleneck_stamp 机制
variants/<vid>/conformance.md 及其校验/清单行     .analysis_stamp 旧键
ckpt 空壳链（F2 全项）                           proxy_budget 数据旋钮（C6 全项）
第二份训练模板 + 逐字节比对门                     po_flatten Step6 flat.py
healed_files.py + .po_probe_healed.txt           best.json 读取与字段
pretrained_ref_acc 输出字段                      exhausted 恒 false 字段
rules_pool 跨模型机制 + generality 字段          device_alloc free/acquire 自动选卡与占用解析
.run_lock 旧心跳独占写者假设（改为续写模型）        verdict 谓词三处手抄（收 check_verdict.py）
base/_flat.py 招牌产物                            structural-levers 两节遗留契约
```

## 13. 产物 → 写者 → 消费者 → 校验门（对照表，校验审查的验收基准）

| 产物 | 写者 | 消费者 | 校验门 |
|---|---|---|---|
| readiness.json + project_manifest | po_flatten | 全链 | check_flatten.sh（逐指标 direction 修复后） |
| contracts.json + templates | po_contract | 全链 | check_contracts.sh（viable 复用检查 + admission ack） |
| base/model.onnx | chain step1 | profiling/export | chain 产品存在检查 |
| base/profile/ 四件套 + bottleneck_report.json | mfu_adapter + analyze.py | predictor / 报告 | chain 四件套存在检查 + adapter fail loud |
| base/profile/mfu_bottleneck_report.md | mfu-analyzer | proposer / web | check_baseline_docs.sh（哨兵+节） |
| baseline/business_logic.md | business-logic-analyst | assessor / proposer / web | check_baseline_docs.sh |
| base/information_analysis.md | information-analyst | proposer / assessor / web | check_baseline_docs.sh |
| base/origin_anchor.json | freeze_origin.sh | gate/verdict/report | 脚本自身（不可变校验） |
| rounds/<RRR>/proposals.json | structure-proposer | 全环 | check_propose_emit + 节点 Step1 校验 |
| variants/<VID>/assessment.md | variant-assessor | 软对齐 / web | check_propose_emit |
| variants/<VID>/shadow + declaration + onnx + DONE | variant-implementer | 测量/训练 | diff_check + DONE sha + append_impl_row |
| variants/<VID>/profile/ + 报告 | mfu-analyzer + adapter | verdict / 修复 | 节点 Step3（哨兵+schedule_result+adapter） |
| verdict.json / verdicts.jsonl | run_latency_recheck（经 check_verdict.py） | gate/probe/report | check_verdict.py 唯一谓词 |
| repair_trace.json | recheck 脚本 | emit/校准 | check_propose_emit |
| accuracy_rules.json | accuracy-analyst（apply 子命令） | proposer / 面板 | rules_pool check |
| devices/<idx>.lock | device_alloc claim | probe/baseline/watchdog | O_EXCL 原子性 + report 兜底 sweep |
| train_status.json / ledger 分片 | watchdog.py | 曲线/pareto/dashboard | ledger_aggregate + dashboard fail loud |
| baseline_metrics.jsonl / train_final.json | finalizer | 报告/锚 | final_check（stderr 落日志） |
| prof_opt_report.md | po_report | 用户/web | report_format.md 章节门 + 披露三行 |
| .chart_push.log | push_curves | report 披露 | push 结局落盘（离线/失败可见） |

## 14. 兼容与迁移

- BASELINE.lock / reuse 版本串升版：旧工作区 reuse 校验失败 → 唯一出路 `fresh_start=true`
  （文案保持 fail loud；不得静默迁移旧盘面）。`fresh_start` 文档补一句「会清空在飞训练
  工作区，先确认无在飞 run」。
- 旧 run 的 web 面板：docs manifest 行集变化（conformance → assessment）由前端泛化渲染
  兼容，不迁移旧数据。

## 15. 测试要求（实现完成的判定标准）

1. 既有套件中所有引用被删产物的用例同步删除/改写；`tests/test_po_v6.py`、
   `tests/test_po_scripts.py`、`tests/compile/test_subagents_md.py`、`tests/test_po_v5.py`
   相应收敛（v5 面已声明退役，允许删除其专属死用例）。
2. 新增用例必须覆盖：
   - device_alloc：probe 原文透传（含 CLI 失败 fail loud）、claim --idx 锁占用/越界、
     adopt/release 不回归；pid_lib unknown 语义披露。
   - check_baseline_docs：三文档缺一/哨兵错/缺节 各拒。
   - check_verdict.py：达线谓词 inclusive 边界 + 三处调用一致（recheck / probe emit / 协议文档引用）。
   - predict_delta：关键路径加权（on 1.0 / off 0.25 披露）、shape class 从 taskgraph 推导、
     负值要求。
   - gate_decide idle 出环：连续 N 轮零提案 → report；不足 N → loop。
   - watchdog.py：duplicate epoch last-wins、streak 阈值按 E 推导、等锚保留最后已知值、
     SIGTERM 终态、final_check stderr 落日志。
   - rules_pool：unparseable 拒绝覆盖镜像；apply 置信度阶梯；check schema。
   - check_propose_emit：assessment 门 + 预测仅负值 + stamp 新键。
3. `tars validate workflows/prof-opt/workflow.yaml` 通过。
4. workflow 创建/修改洁净契约检查（`_check_prompt_dev_residue`）warning 清零。
5. 全量 pytest（repo 惯用解释器）绿。
