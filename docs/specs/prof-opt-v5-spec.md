# Prof-Opt v5 SPEC —— 时延先行顺序门控

> 依据：[`prof-opt-v5-design-draft.md`](prof-opt-v5-design-draft.md)（D-V5-1~8 用户拍板）+ 评审轮 1 裁决（U1 恢复轮底座固定+组合式提案 / U2 规则双层池 / U3 入口重部署+节点对戳）。评审三轮 PASS 后回填两处 errata（§2.3 比对集收窄 / §6.1 撕裂恢复读法——planner/plan-adversary 三轮攻击验证，不改任何用户裁决）。
> 实现布局：per-wf 新布局（协调协议 2026-08-27：workflows/ 写权归目录迁移 loop 至批 H；**v5 的 Phase 3 实现等 `workflows/prof-opt/` 目录存在（迁移批 D 落地标志）后开工**，本 SPEC 全部旧路径按下表机械换算——plan 级调整；spec 评审只读 workflows/ 可并行）。
> 环境约束：pytest/tars 走 WSL `.venv`；不 push；改完 workflow 按洁净契约检查 warning 清零。

---

## 0. 范围与非目标

**范围**：`workflows/prof-opt.yaml` + 7 个 agent.md（flatten/baseline/propose/probe/report/contract/full_train）+ `workflows/subagents/prof-opt/`（改 structure-proposer、新增 accuracy-analyst）+ `workflows/agents/_po_scripts/`（改 7、增 3）+ per-agent scripts（reuse_check.sh / run_latency_recheck.sh / check_prerequisites.sh）+ `tests/test_po_scripts.py` + 新增 `tests/test_po_v5.py`。

**非目标**（明确不做）：
- wall-clock 帽 / 平台早退（用户否决；100 轮是唯一硬帽）
- DAG 结构 / 节点数 / 回边 / 路由语句变更（8 节点形态不动）
- 引擎层改动（round_state 等全部是 workflow 脚本层）
- 规则跨**用户**共享（池在 `$ORCA_HOME`，仅本用户本机范围）
- 真机 in-session E2E（留给用户 NPU 服务器；本 SPEC 验收 = 脚本级单测 + smoke 序列 + tars validate + 洁净检查，见 §11；真机清单见 §11.4）

**评审轮 1 关键裁决回填**（全文已体现）：
- **U1**：恢复轮**底座固定**（不因 fail 变体推进）；恢复轮提案升级为**组合式**（可叠加历史有效尝试 / 回退历史有害组件）；仅 accuracy_pass 者推进（推进即终局 full-train）。时延追击期链式推进不变（D-V5-8）。
- **U2**：规则双层池——项目镜像（docs/prof-opt）+ 全局池（$ORCA_HOME，model_hash 键控跨文件夹继承、generality 打标跨模型迁移、confirm/refute 机械计数）。
- **U3**：版本戳 = flatten 入口幂等重部署（含 REUSE 分支）+ 节点侧对戳 fail loud。

**DAG 不变量**：`po_flatten → po_contract → po_baseline → po_propose → po_probe → po_gate`，回边 `po_gate --loop--> po_propose`，`po_gate → po_full_train → po_report`，每节点 catch-all → po_report。routes 的 when 条件全部不变。

**路径换算表**（实现时旧→新机械映射，SPEC 正文沿用旧路径书写）：

| SPEC 中旧路径 | 实现目标（per-wf 新布局） |
|---|---|
| `workflows/prof-opt.yaml` | `workflows/prof-opt/workflow.yaml` |
| `workflows/agents/po_<name>/`（agent.md / scripts/ / references/） | `workflows/prof-opt/agents/po_<name>/` |
| `workflows/agents/_po_scripts/` | `workflows/prof-opt/agents/_po_scripts/` |
| `workflows/subagents/prof-opt/*.md` | `workflows/prof-opt/subagents/*.md` |
| `tests/test_po_scripts.py` / `tests/test_po_v5.py` | 不变（仓库根 tests/） |

开工门槛（fail loud）：coder 动手前断言 `workflows/prof-opt/workflow.yaml` 存在且 `workflows/prof-opt.yaml`（平铺旧文件）不存在；不符 → IMPL_STATUS=BLOCKED 上报，不自行猜测布局。

---

## 1. inputs 契约 v5（D-V5-1）

`workflows/prof-opt.yaml` inputs 块**整体替换**为以下 8 个（逐字）：

```yaml
inputs:
  project_root:
    type: string
    description: "[ask] 用户 PyTorch 项目根目录绝对路径。结构优化的全部勘察 / 影子复制 / 训练评估连接都以它为源项目锚点（agent.md 经 {{ inputs.project_root }} 引用）；亦作工作区解析锚点——workflow 工作区固定在其 artifacts/prof-opt/ 子树。必须用户给（绝对路径，否则启动失败）"
    required: true
  model_path:
    type: string
    description: "[ask] 目标模型定义文件路径（相对 project_root 或绝对）。结构优化的唯一修改对象：该文件及其本地依赖闭包被复制进影子工作区，一切结构修改只发生在影子副本，用户原文件全程只读。必须用户给"
    required: true
  latency_reduction_min:
    type: number
    description: "[ask] 时延达标线：相对原始基线 makespan 的最小降低比例（无量纲，0-1 开区间；如 0.5 = 最终时延至少降 50%）。达标线在基线建立时冻结为 origin 锚（base/origin_anchor.json 的 target_cycles），此后结构推进永不重锚。达标判定 = best.makespan ≤ target_cycles"
    required: true
  accuracy_budget:
    type: number
    description: "[ask] 精度预算：相对基线评估指标的最大可接受损失（无量纲，按指标方向归一；具体量纲由项目指标自带）。同样冻结进 origin 锚；粗训精度门与终局 full-train 判定共用。必须用户给"
    required: true
  seed:
    type: integer
    description: "[default] 复现性种子（默认 0）。导出 / 从头训练随机初始化 / 预算渲染等 seed 消费环节全链固定此值，并记入 full_train_budget 值级指纹"
    required: false
    default: 0
  max_rounds:
    type: integer
    description: "[advanced] 轮数硬帽（默认 100）。唯一循环出口之一：达到后永不回边，直接进入终态判定；无任何时间帽 / 平台早退（平台期的答案是换路径探索，不是停机）"
    required: false
    default: 100
  fresh_start:
    type: boolean
    description: "[advanced] 丢弃工作区既有可复用状态、从零重建（默认 false）。结构锚（模型文件）或 profiling 模式变化导致锁校验失败时的重置开关，修改达标线 / 精度预算同样需要它（origin 锚不可变）；true 会清空工作区内全部既有内容（仅保留 .run_lock）后重跑入口节点。注意：项目镜像与全局规则池不受影响（fresh_start 后 flatten 会重新回种）"
    required: false
    default: false
  full_train_epoch_cap:
    type: string
    description: "[advanced] 完整训练轮数上限（默认空 = 不截断）。非空 = 实际轮数取 min(本值, 项目全量训练轮数)——基线完整训练与 winner 完整训练共用的唯一预算阀（生效值记入 contracts.json 的 full_train_budget 值级指纹），真机长训练的成本保护"
    required: false
    default: ""
```

**退役 6 输入的去向**：

| 退役输入 | 去向 |
|---|---|
| npu_chip / npu_precision / npu_core_num | §2 profiling 模式契约（环境变量 + 自动探测，落盘 profile_mode.json） |
| write_back | 固定 `true`：终态成功即写回（`<原名>_prof_optimized.<ext>` 新文件 + 同名冲突不覆盖），po_report prompt 以常量替代 `{{ inputs.write_back }}` |
| report_dir | 固定 `docs/prof-opt`，po_report prompt 以常量替代 |
| probe_epochs | 纯自动：契约期按项目全量训练轮数机械推定 k（contracts.json proxy_budget 仍是唯一预算来源），用户覆盖口移除 |

**验收（机械）**：`grep -rn "inputs\.npu_chip\|inputs\.npu_precision\|inputs\.npu_core_num\|inputs\.write_back\|inputs\.report_dir\|inputs\.probe_epochs" workflows/` 零命中。

---

## 2. profiling 模式契约（npu 退役，D-V5-1）

### 2.1 解析规则（新增 `workflows/agents/_po_scripts/resolve_profile_mode.sh`，部署件）

优先级顺序（first-match-wins）：

1. 环境变量 `ORCA_PO_NPU_CHIP` 非空 → **mfu 模式**。chip 枚举校验 `6613|1951`，非法值 exit 2 fail loud；`ORCA_PO_NPU_PRECISION`（默认 `INT8`，枚举 `INT8|INT16|AMP`）与 `ORCA_PO_NPU_CORES`（默认 `1`，枚举 `1|2|4`）同规则校验。`resolved_by: "env"`。
2. 否则 `command -v npu-smi` 成功 → **mfu 模式**。chip 从 `npu-smi info` 输出的**芯片型号字段**解析（解析该工具输出的型号列/Token，禁止裸子串匹配整个输出——防 "1951 MB" 类伪命中）：型号为 6613 → 6613；1951 → 1951；无法解析出型号或命令失败 → exit 2 fail loud（错误信息指引设置 `ORCA_PO_NPU_CHIP` 显式声明）。precision/cores 用默认值。`resolved_by: "npu-smi"`。
3. 否则 → **placeholder 模式**（内置估算器，无需 NPU 环境）。`resolved_by: "fallback"`。

输出：单行 JSON（经 emit_result.py）并写 `$ORCA_ARTIFACTS_DIR/profile_mode.json`：

```json
{"mode": "placeholder|mfu", "chip": "", "precision": null, "core_num": null,
 "resolved_by": "env|npu-smi|fallback"}
```

placeholder 模式下 `chip=""`、`precision=null`、`core_num=null`；mfu 模式三者必填。

### 2.2 消费者改读盘

po_baseline 的 profile 调用（现 `--npu-chip/--npu-precision/--npu-core-num "{{ inputs.* }}"`）、po_propose 的 mfu guard 与 mfu-analyzer dispatch（现 `{{ inputs.npu_chip }}` 判断 + 三参透传）、run_latency_recheck.sh 与 reuse_check.sh 的模式分支，全部改为读 `profile_mode.json` 字段。profile_mode.json 缺失或 mode 非法 → 消费节点 fail loud。

### 2.3 复用一致性（reuse_check.sh）

工作区复用时：重新解析一次模式（同 §2.1），与既有 `profile_mode.json` 就**测量配置四字段比对集 `{mode, chip, precision, core_num}`** 比对（`resolved_by` 是溯源字段不参与——同硬件下 env→npu-smi 来源翻转是测量等价、非漂移，不得强制 wipe）；**比对集内任一不一致、或该文件缺失 → exit 2 fail loud**（错误信息：profiling 模式变化会使跨 run 的 cycles 对比失效，指引 `fresh_start=true`）。一致 → 原样保留。

---

## 3. origin 锚契约（D-V5-4）

### 3.1 schema 与写入

`$ORCA_ARTIFACTS_DIR/base/origin_anchor.json`，po_baseline 早期链首次 profile 成功后经 `analyze.py --freeze-origin` 写入（write-if-absent）：

```json
{"baseline_makespan_cycles": 3208468,
 "latency_reduction_min": 0.5,
 "accuracy_budget": 0.1,
 "target_cycles": 1604235,
 "frozen_at_round": 0}
```

- `target_cycles = int(baseline_makespan_cycles × (1 − latency_reduction_min)) + 1`（继承 v4 边界语义：`makespan ≤ target` ⇔ 严格低于达标线）。
- `analyze.py` 新增参数：`--freeze-origin --latency-reduction-min <f> --accuracy-budget <f>`。**量程校验**：`latency_reduction_min ∈ (0,1)`、`accuracy_budget ≥ 0`，非法 exit 2。文件已存在：内容逐字段一致 → no-op；不一致 → exit 2 fail loud（文案含「origin 锚不可变；修改达标线/预算需 fresh_start 重建工作区」）。不带 freeze 参数调用时**绝不触碰**该文件。
- 后续轮 propose Step1 的 `analyze.py --profile-dir base/profile`（重算当前 base 瓶颈报告）照常——它只写 `bottleneck_report.json`，与锚无关。

### 3.2 消费者（全部只读；缺失 → exit 2 fail loud）

gate_decide / advance_round / round_state(mode) / verdict_decide(两子命令的 budget) / po_probe(粗门预算) / po_report(报告基线块)。**时延与精度双锚全程不随 base 推进漂移**；粗训锚（baseline 曲线第 k 点 / baseline_k_acc）与 full-train 锚（baseline_full_acc）由 finalizer 一次性产出，天然冻结，机制不变。

---

## 4. round_state 单一来源（D-V5-5）

新增 `workflows/agents/_po_scripts/round_state.py`，propose Step0 / probe / gate / advance 的轮号与路径**全部经它**，`<RRR>` prompt 约定退役（agent.md 中手写 `rounds/<RRR>` 推导的段落改为调用本脚本）。

CLI（stdout 单行 JSON，bad input exit 2）：

```bash
round_state.py --artifacts <ws> current   # {"round": R, "round_dir": "rounds/RRR"}
round_state.py --artifacts <ws> working   # {"round": R_write, "round_dir": "rounds/RRR"}
round_state.py --artifacts <ws> mode      # {"mode": "latency|accuracy", "target_cycles": T,
                                          #  "best_makespan": M|null}
```

- `current`：`rounds/` 下最大纯数字目录名（无则 0；非纯数字目录名忽略，对齐 v4 容忍语义）；`round_dir` 为其 `%03d` 零填充形式（current=0 时 `round_dir: null`）。
- `working`：现 propose Step0 规则的唯一实现——`.round_advanced` 存在且 `round == current` → `current + 1`；否则 `max(current, 1)`。
- `mode`：`best.json` 存在且 `makespan_cycles ≤ origin_anchor.target_cycles` → `accuracy`；best 不存在或未达线 → `latency`。origin 锚缺失 → exit 2。

---

## 5. history 契约 v5（history_lib.py）

### 5.1 outcome 枚举变更

| 阶段 | v4 | v5 |
|---|---|---|
| latency 行（propose 复测） | latency_pass / structural_mismatch / variant_broken / unsupported_op / latency_fail | **不变**（判定标准变，见 §8.3） |
| probe 行（粗训精度门） | promoted / probe_insufficient | **accuracy_pass / accuracy_fail / probe_insufficient** |
| 推进标记 | promoted（兼作 probe 结果） | **advanced**（新 builder `append_advanced(path, vid)`，只写 `outcome: "advanced"`，字段集 = LATENCY_FIELDS） |

`PERMANENT_OUTCOMES = {"advanced", "promoted", "unsupported_op"}`（promoted 保留仅为读旧工作区兼容，v5 不再写入）。accuracy_fail **不进** permanent 集（恢复轮组合式提案产生的**新 change_sig** 与历史失败 sig 不同，按 dedup 精确匹配语义天然不被拦截，无需改 dedup；组合的机械刹车由 §6.2 failed_sigs 换路标记承担）。

### 5.2 probe 行新增字段

`PROBE_FIELDS` 增加 `gap`。**gap 定义（双门最差）**：curve 门与 eval 门各自的缺口（higher_better = anchor − value；lower_better = value − anchor）取**最大值**；eval 缺失降级 curve-only 时 gap = 曲线缺口并在行上保留 `eval_failed/eval_skipped_no_epoch_ckpt` 披露。pass ⇔ gap ≤ accuracy_budget（即两门都过）。`append_probe(..., gap: float | None = None)`，None 省略（probe_insufficient 行可无 gap）。`proxy_acc` 语义不变（曲线第 k 点）。

### 5.3 verdict_decide.py 变更

- `promote` 子命令：`--budget` 参数**移除**，budget 改读 `base/origin_anchor.json`；输出 `{"curve_pass", "eval_acc", "eval_pass", "line", "accuracy_pass": <bool>, "gap": <float>}`（v4 `promoted` 字段退役）。
- `final-budget` 子命令：`--budget` 移除，同读 origin 锚；输出不变。
- 其余（方向归一、slack=1.0×budget、curve-only 降级、fail loud 矩阵）逐字保留。

---

## 6. 推进契约 advance_round v5

### 6.1 双模判据（mode 经 round_state 推断；U1 语义）

**latency 态**（追击期链式推进，D-V5-8 不变）：候选 = 本轮（round == current）latest 行 `outcome == "latency_pass"` 且 `makespan_cycles` **严格小于** incumbent 的变体。incumbent = best.json 存在时其 `makespan_cycles`，否则 origin 锚 `baseline_makespan_cycles`。winner = makespan 最小（平手取 vid 字典序——latency 态无 proxy_acc）。无候选 → 不推进。

**accuracy 态**（U1：底座固定，仅过线者推进）：候选 = 本轮 latest 行 `outcome == "accuracy_pass"` 且 `makespan_cycles ≤ target_cycles` 的变体。winner = gap 最小（平手取 makespan 小，再取 vid 序）。**无 accuracy_pass 候选 → 不推进**（底座不动，恢复轮靠组合式提案 + 规则反馈继续，见 §8.3）。推进发生即终局：该轮 gate 决策 1 触发 full-train。

**共同动作**（仅在真实推进时执行，winner ≠ incumbent）：best.json 写入（tmp + os.replace）→ winner 的 onnx/profile/shadow 复制到 base/ 与 shadow/（`bottleneck_report.json` 故意不复制，沿用 v4）→ `append_advanced(winner_vid)` → marker 最后落盘。**winner == incumbent（如首入轮 best.vid 自身）或无候选 → 跳过全部共同动作，仅写 marker**。**崩溃撕裂恢复（errata）**：重入时若 marker 缺当前 (round, mode) 记录、`best.json.round == current`、且 best.vid 无本轮 `advanced` 行——判定为撕裂在途，按 best.vid 补齐复制与 `append_advanced` 后写 marker（§6.2 重放收敛要求）；「跳过共同动作」仅适用于序列完整的等位/无候选情形。已知良性残余（披露）：撕裂写已达线 + 首入 accuracy_fail 的子窗口会以 no-op marker 关闭未完成序列——best/base 分叉至多一轮、下轮真实推进追平、gate 不误开 full-train（§11.4 观察项）。

**best.json v5 schema**：沿用 v4 五字段 `{vid, makespan_cycles, proxy_acc, round, profile_dir}`；latency 态推进写入时 `proxy_acc=null`，accuracy 态推进写 winner 的 proxy_acc；gap 不入 best.json（从 history 行读）。

### 6.2 marker 与 direction 状态

`.round_advanced` v5 schema（幂等键 = **(round, mode) 二元组**——同轮内 latency 推进后 accuracy 推进各可执行一次；marker.round < current 的陈旧 marker 允许按当前 mode 重放收敛）：

```json
{"round": 3, "mode": "latency", "vid": "r3-02", "improved": true,
 "best_updated": true}
```

`improved ⇔ best_updated`（钉死定义，消除语义漂移）；无候选/winner==incumbent 时 `vid=null`、`improved=false`。

每次 advance（含 no-op 判定）写 `rounds/<RRR>/direction.json`（**最近一次 advance 的产物**，同轮后写覆盖先写）：

```json
{"round": 3, "mode": "latency", "improved": false, "advanced_vid": null,
 "failed_sigs": ["linear_to_conv:attn_qkv", "reduce_layers:2"]}
```

`failed_sigs` = 本轮 `latency_fail` **与 `accuracy_fail`** 变体（latest 行）的 change_sig 机械枚举——精度证伪方向与 时延证伪方向同等进入下轮换路依据。

### 6.3 顺带修复

`_rank_key` 的 `-proxy_acc` 硬编码 higher_better：tie-break 一律按 gap/proxy_acc 的**方向归一优值**（metric_direction 读 contracts.json），方向未知时退回 vid 序并 stderr 披露。

---

## 7. gate 契约 v5（gate_decide.py + gate_node.sh，D-V5-3）

### 7.1 决策序（first-match-wins，全读盘）

```
1. best 存在 AND best.makespan_cycles ≤ target_cycles
   AND best.vid 在 history 中存在（任意版本行）outcome == "accuracy_pass"
   → full-train
2. round ≥ max_rounds（硬帽，永不 loop）
   → full-train-best-effort（best 存在）/ finish-failed（无 best）
3. 其余 → loop（无其它出口）
```

决策序 1 用**任意版本行**（非 latest）：推进后 `append_advanced` 会覆盖 latest 行为 `advanced`，但该 vid 的 accuracy_pass 历史行仍在（history 行是 per-vid 全量快照 append）。

**不变量校验**（决策前）：`round_state mode` = accuracy 但 best.vid 无任何 probe 行 → exit 2 fail loud（probe 必然跑过——mode=accuracy 时 probe 要么训过 best.vid（首入）要么训过幸存者且 best 已有行；违反即盘面撕裂，兜底 po_report 披露）。

### 7.2 CLI 与输出

```
gate_decide.py --artifacts <ws> --max-rounds <int, default 100>
```

`--latency-reduction-min` / `--stall-rounds` 移除；不再读 proposals.json / exhausted / stall（exhausted 字段由 po_report 作为报告素材读取）。输出：

```json
{"decision": "full-train|loop|full-train-best-effort|finish-failed",
 "round": 3, "mode": "latency|accuracy", "best": {...}|null,
 "target_cycles": 1604235, "reason": "..."}
```

`workflows/prof-opt.yaml` po_gate command 相应简化为只传 `--max-rounds "{{ inputs.max_rounds }}"`；`gate_node.sh` 先跑 `deploy_scripts.sh --verify`（§9），失败按既有 fail 分支出 finish-failed 并在 reason 披露版本戳不符。

---

## 8. 节点契约变更（DAG/路由不变）

### 8.1 po_flatten

- 部署脚本后调用 `resolve_profile_mode.sh`（§2.1）落盘 profile_mode.json；reuse 路径走 §2.3 一致性校验（reuse_check.sh 的 npu 三参校验段整体替换为模式一致性校验）。
- **REUSE 分支出口前追加一次 `deploy_scripts.sh` 幂等重部署**（U3：cp -f 全量覆盖 + 重算 `.VERSION`——旧工作区跨版本不报废；fresh 路径本就部署）。
- **规则回种**（U2，仅 fresh 路径）：调 `rules_pool.py seed`（§8.5）——从项目镜像 + 全局池生成工作区 `accuracy_rules.json`；**仅在工作区无该文件时执行**。REUSE 分支保留工作区既有规则不重种（在轮规则属工作区真相源，终态才经 merge 落镜像与池——重种会覆盖丢失）。
- agent.md 中 `{{ inputs.npu_* }}` / `{{ inputs.fresh_start }}` 透传段相应改写。

### 8.2 po_baseline

- 早期链首次 profile 成功后：`analyze.py --profile-dir base/profile --freeze-origin --latency-reduction-min {{ inputs.latency_reduction_min }} --accuracy-budget {{ inputs.accuracy_budget }}`（§3.1）。
- profile 调用 npu 三参改读 profile_mode.json（§2.2）。

### 8.3 po_propose（回边重入目标）

- **Step0**：R 与路径经 `round_state.py working`；reuse guard 语义不变。
- **Step1/2**：不变（analyze 刷新当前 base 瓶颈；stamp guard 不变）。
- **Step3 proposer dispatch 输入增补**：
  - `accuracy_rules.json` 全文（存在时；规则集是提案的精度依据）
  - 换路指令：历史全部 `direction.json` 的 `failed_sigs` **并集** + 「以下改动方向已被实测证伪（时延或精度），本轮提案不得与其同族；自觉穷尽时必须提出更深的重写/不同算子族，轮帽前不存在穷尽退出」。（sig 与 change_pattern 双重去重自然收敛，不设截断——100 轮 × ≤3/轮 上限可控）
  - `round_state mode` = **accuracy（恢复轮）**时：底座固定声明（不推进 fail 变体）+ 当前 gap 数值 + `makespan ≤ target_cycles` 硬约束 + **组合式提案语义**：可叠加历史有效尝试、可回退历史有害组件（沿血缘链部分还原）、可提名 KD 型改动——组合产生的新 change_sig 不受历史失败 sig 的 dedup 拦截（这是恢复策略本身）
  - 精度影响预判责任：每条提案必带 `predicted_acc_impact`（low/medium/high 风险 + 一句理由，承接现 structure-proposer 的 `accuracy_risk`/`accuracy_evidence` 字段族——`accuracy_risk` 更名 `predicted_acc_impact`，`expected_accuracy_impact`/`accuracy_confidence` 退役，`accuracy_evidence` 保留承接理由），proposer prompt 明示「尽可能保证精度，不为一味降时延牺牲精度」
- **exhausted 退役**：proposals.json 的 `exhausted` 字段保留但 v5 恒 `false`；structure-proposer.md 删除声明穷尽的出路。
- **Step5 复测判定 v5（v4 参数族退役）**：v4 的 `--min-improvement`（100 cycles 绝对门槛）/ `--min-pct`（1% 相对门槛）/ `--min-ratio`（预测比 ≥0.5）/ `predicted_delta_cycles >= 0` 守卫**全部退役**——小步改进合法（D-V5-8「每次可以只下降一点」）。判定唯一依据：latency 态 `latency_pass ⇔ makespan < incumbent makespan`（严格改进）；accuracy 态 `latency_pass ⇔ makespan ≤ target_cycles`（恢复轮过滤线）。不满足 → `latency_fail`。`pred_actual_ratio` 降级为信息字段（有则记录，不参与判定）。incumbent 定义同 §6.1；mode 读取经 round_state。
- **Step6 机械推进（新增；原 Step 6 Emit 顺延为 Step 7）**：`round_state mode` = latency → 跑 `advance_round.py`（含 direction.json 产出）；= accuracy → 不跑（恢复轮推进在 probe 判定后由 probe 跑）。崩溃边界：Step6 写 marker 后崩溃 → 重入 `working = R+1`，本轮 probe/gate 顺延至下一轮——**合法路径**（首入判据「best.vid 无 probe 行」按盘面重求值天然兼容顺延；gate 不变量不受破坏）。
- **output_schema 增量**：新增 `mode`（enum latency/accuracy）与 `advanced_vid`（string，本轮推进者，空串=零推进）；`latency_pass_count` / `exhausted` 等字段保留（exhausted 描述改为恒 false 的兼容字段）。
- **check_prerequisites.sh**：前置清单加入 `round_state.py` / `resolve_profile_mode.sh` / `rules_pool.py`。

### 8.4 po_probe（条件精度门）

- **Step0 mode 分派**（`round_state.py mode`，**于 probe 入口时点求值**——达线翻转轮（propose Step6 推进后 best 首次 ≤ target）的 probe 即 accuracy 首入，不存在「翻转轮直通」）：
  - `latency` → **直通 emit**：不训任何变体、不等 GPU 守卫。`survivors_probed=0`，assessment 注明 passthrough。
  - `accuracy` → 进入粗训精度门。
- **训练集规则**（机械判据，弃用时序推断）：`best.vid` **无任何 probe 行** → 首入（只训 `best.vid`）；**已有 probe 行** → 恢复轮（训本轮全部 `latency_pass` 幸存者，排除已有终态 probe 行的 vid——「终态」= `{accuracy_pass, accuracy_fail, probe_insufficient}` 全算已训）。**幸存者轮 = `round_state current`**（恢复轮 working==current 成立；首入分支先短路不查幸存者）。
- **粗训流程**：GPU 串行守卫（finalizer.pid 四象限）→ stop-at-k 渲染/训练/停止 → 曲线 extract → `verdict_decide promote`（读锚预算，出 accuracy_pass/gap，§5.2 双门最差 gap）→ `append_probe` 行 → 全员判定后跑 `advance_round.py`（accuracy 态判据：仅 accuracy_pass 推进，§6.1）。
- **规则提取（D-V5-7）**：每轮粗训判定完成后 dispatch `accuracy-analyst` 子代理（新），输入 = 本轮 probe 行（vid/gap/accuracy_pass）+ 血缘 change_sig + 现有 accuracy_rules.json；产出 = 更新后的 accuracy_rules.json（含 direction/generality 打标，§8.5）。返回后 `rules_pool.py check` 机械校验，失败重派 1 次；再失败 → 剔除坏行继续 + 披露（不阻断轮次——规则是增量资产）。
- **output_schema 增量**：`mode`（enum）；`promoted` 字段更名 `accuracy_pass_vids`（array）；新增 `advanced_vid`；其余字段（survivors_probed/best_updated/base_advanced/artifacts/assessment/max_retries_hit/healed_files）保留。routes 的 when（`status == 'executed'`）不变。
- `references/probe_protocol.md` 同步改写（mode 分派 / 训练集规则 / accuracy 态 advance 时机 / accuracy-analyst dispatch 协议）。

### 8.5 规则双层池（U2；accuracy-analyst 子代理 + rules_pool.py）

**accuracy-analyst**（新增 `workflows/subagents/prof-opt/accuracy-analyst.md`，按 v4 子代理协议惯例带 frontmatter sentinel）：从实测精度结果提取/更新规则并打标。**规则只从实测提取**（probe 行 + 血缘），不得凭空预置模型论先验。

**工作区规则文件** `$ORCA_ARTIFACTS_DIR/accuracy_rules.json`（run 内真源）：

```json
{"rules": [
  {"id": "rule-0001",
   "change_pattern": "reduce_layers>=2",
   "statement": "对该模型降层数 ≥2 精度崩（gap 0.61 远超预算 0.1）",
   "direction": "harmful",
   "generality": "model_specific",
   "evidence_rounds": [3, 5], "vids": ["r3-01", "r5-02"],
   "confidence": "high", "metric_gap": 0.61}
]}
```

字段（全部必含）：`id` / `change_pattern` / `statement` / `direction`（harmful|benign——该 pattern 对精度有害/无害）/ `generality`（model_specific|plausibly_general——analyst 判断该教训是否可能跨模型成立）/ `evidence_rounds` / `vids` / `confidence`（升级规则机械可复核：单轮证据 low、2 轮一致 medium、≥3 轮或 gap>3×budget high）/ `metric_gap`（有限数）。同 `change_pattern` 已存在 → 合并证据（rounds/vids 并集去增）+ 重算 confidence + statement 保留最新，不新增条目。

**rules_pool.py**（新增，_po_scripts；池操作全机械，无 LLM）：

- `check --artifacts <ws>`：规则文件 schema 校验（全字段 / change_pattern 去重 / direction·generality·confidence 枚举 / metric_gap 有限数），违规行 fail loud 报行号。
- **池条目 schema**（`$ORCA_HOME/prof-opt/accuracy_rules_pool.json`，单文件）：

  ```json
  {"entries": [
    {"change_pattern": "reduce_layers>=2", "direction": "harmful",
     "statement": "…（最新）", "generality": "model_specific",
     "evidence": [{"model_hash": "ab12…", "rounds": [3, 5], "vids": ["r3-01", "r5-02"]}],
     "confirm_models": ["cd34…"], "refute_models": [],
     "general": false, "quarantined": false}]
  }
  ```

  confirm/refute 的载体是 **model_hash 集合**（非整数计数）：`general: true ⇔ |confirm_models| ≥ 2`，`quarantined: true ⇔ |refute_models| ≥ 2`——同 run at-least-once 重入重复合并不增集合元素，**天然幂等**。`quarantined` 条目永不被 seed 选中。
- `seed --artifacts <ws> --project-root <root>`：**flatten 回种（仅工作区无 accuracy_rules.json 时执行，§8.1）**。初始集合成（按优先级）：
  1. 项目镜像 `<project_root>/docs/prof-opt/accuracy_rules.json` 条目**原样并入**（视为本项目实测）；
  2. 池 `$ORCA_HOME/prof-opt/accuracy_rules_pool.json` 中 `model_hash` 精确匹配条目；
  3. 池中 `general: true` 未隔离条目；
  4. 池中 `generality == "plausibly_general"` 未隔离条目（置信降一档——**下限 low，low 不再降**——并标 `borrowed: true`；borrowed 条目的 `evidence_rounds`/`vids` 填来源池记录、`id` 加 `pool-` 前缀以过 check 去重）。

  同 `change_pattern` 冲突裁决：**项目实测（来源 1/2）优先于 borrowed（来源 3/4）**；direction 冲突时保留项目实测条目并在 statement 末尾追加披露句。`model_hash` 配方：对 BASELINE.lock 的 `py_files_sha256` 映射（`{rel_path: sha256}`）按 rel_path 排序做 `(rel_path, sha256)` 序列的 sha256 **单值**；首跑 seed 时 lock 未写则对刚复制的原始 shadow `*.py` 闭包（排除 `__pycache__`/`.pyc`）同法直算——两时刻同一棵树，值等价；**池键锚定原始模型闭包，不随 advance 推进漂移**（禁止对已推进的 shadow 直算）。两源皆无 → 空集冷启；池/镜像缺失或不可解析 → best-effort：stderr 披露 + 视为空源；**可解析但含坏行 → 逐条过 check，坏行剔除 + stderr 披露**（并入行同样受校验）。
- `merge --artifacts <ws> --project-root <root>`：**po_report 终态合并**。工作区规则 → 写项目镜像（全文覆盖，机器可读真源）+ 并入全局池：同 `(change_pattern, direction)` 的证据按 `model_hash` 并入对应池条目 `evidence`，且本 model_hash **新出现**于该条目时加入 `confirm_models`（跨模型证实）；同 change_pattern 但 direction 相反的本项目观测 → 本 model_hash 加入 `refute_models`（跨模型反驳）。合并后按集合大小重算 `general`/`quarantined`。合并失败 best-effort 披露，不影响终态。

**po_report 终态镜像**：项目镜像即 `<project_root>/docs/prof-opt/accuracy_rules.json`（机器可读真源）+ `docs/prof-opt/accuracy_rules.md`（人类可读表，与报告同目录）。

### 8.6 po_report

- 读 origin_anchor 填报告基线块；读全部 direction.json 统计 `zero_improvement_rounds`（informational，写进 reason/assessment 文本，不改 output_schema）；读 proposals.json 的 exhausted 与 accuracy_rules.json 作报告素材；报告首段披露 profiling 模式（profile_mode.json 全文）与 scripts `.VERSION` 戳（§9）。
- 终态调 `rules_pool.py merge`（§8.5；成功失败皆合并——失败 run 的教训更值钱）+ 人类可读镜像。
- `{{ inputs.write_back }}` → 常量 true；`{{ inputs.report_dir }}` → 常量 `docs/prof-opt`。
- 终态收割 / 写回 / no-promotion 零写回披露等 v4 语义不变。

### 8.7 po_full_train / po_contract

- po_full_train：`{{ inputs.accuracy_budget }}` 消费点改为读 origin 锚（verdict final-budget 已改读锚，prompt 同步）；**终局判定后 dispatch accuracy-analyst 提取最后一轮规则**（U2：full-train 是第三个实测点，终局教训对跨 run 最有价值；within_budget 与否都提取，提取后 `rules_pool.py check` 机械校验，失败重派 1 次再披露），随后由 po_report 统一 merge。其余不变。
- po_contract：`{{ inputs.probe_epochs }}` 消费点移除（k 纯机械推定）；output_schema 的 proxy_budget 描述微调（「自动推定，不可覆盖」）。

---

## 9. 部署件版本戳（D-V5-6 + U3）

- `deploy_scripts.sh` 复制完成后计算 manifest：对部署到 `scripts/` 的全部 `*.py`/`*.sh` 按「文件名排序 → (name, sha256(content))」序列做 sha256，写 `scripts/.VERSION`（单行 JSON `{"manifest": "<sha256>"}`）。
- 新增 `--verify` 模式：重算当前部署集 manifest 与 `.VERSION` 比对；缺失/不符 → exit 1 + stderr 指明（篡改或半部署检测）。
- **入口重部署（U3）**：po_flatten 无论 fresh 还是 REUSE 路径，出口前都跑一次 `deploy_scripts.sh`（幂等 cp -f 全量 + 重算戳）——旧工作区跨版本升级不报废。已核实运行节点（gate/probe/propose）无 `_po_scripts` 源访问权，节点侧热更新不可行，不做。
- 消费点：gate_node.sh 决策前、po_propose Step0、po_probe Step1 各跑一次 `--verify`（秒级）；不符 → 该节点 fail loud 披露「部署件版本戳不符，需 fresh_start 重建工作区」。

---

## 10. workflow.yaml 变更汇总

1. 顶部 description：双门槛句改为顺序门控句（「时延链式推进达标后粗训过精度门、精度不过进恢复轮（底座固定、组合式提案、时延达标线为硬约束），双达标进完整训练；轮数硬帽内无其它出口」）；补规则沉淀一句。
2. inputs 块替换（§1）。
3. po_propose / po_probe / po_gate 的注释块与 output_schema 增量（§8.3/8.4/7）。
4. po_gate command：`--max-rounds` 唯一参数（§7.2）。
5. outputs 块不变（全部仍读 po_report.output）。
6. **洁净检查**：全部 agent.md 按 [agent-prompt-cleanliness-contract] 通读，`tars validate` warning 清零。

---

## 11. 测试与验收

### 11.1 单测（WSL .venv，`pytest tests/test_po_scripts.py tests/test_po_v5.py`）

新增 `tests/test_po_v5.py`（v5 机制）+ 改造 `tests/test_po_scripts.py` 既有用例：

| 域 | 用例（每条可机械断言） |
|---|---|
| gate | accuracy_pass（任意版本行）+达线 → full-train；round≥帽 → best-effort / finish-failed；其余一律 loop（含连续多轮零推进仍 loop——替代删除的 stall 用例）；不变量破坏（mode=accuracy 但 best.vid 无 probe 行）→ rc2；origin 锚缺失 → rc2；传 `--latency-reduction-min` → argparse 拒绝 |
| advance | latency 严格改进才推进；**小步改进（如 50 cycles 且严格 < incumbent）也推进**（v4 参数族退役反例）；零改进 → marker improved=false + direction.json（failed_sigs 含 latency_fail 与 accuracy_fail 的 sigs）；accuracy 态仅 accuracy_pass 推进、无过者不推进不复制；winner==incumbent 只写 marker 不 append_advanced；(round, mode) 幂等键（同轮先 latency 后 accuracy 各一次）；tie-break 方向归一 |
| round_state | working/current 推导（含 marker 联动、空 rounds）；%03d 零填充；mode 两态推断 + 锚缺失 rc2 |
| history | append_advanced 字段集与 permanent 集；append_probe gap 字段 |
| verdict | promote 读锚预算、输出 accuracy_pass/gap（双门最差：curve 过 eval 不过 → accuracy_pass=false 且 gap=eval 缺口）；锚缺失 rc2 |
| analyze | --freeze-origin 首写 / 幂等 no-op / 内容冲突 rc2 / 量程非法（r≤0、r≥1、budget<0）rc2；不带 flag 不触碰 |
| deploy | .VERSION 写入；--verify 通过；篡改一个部署文件 → rc1 |
| rules | rules_pool check 全字段/去重/枚举校验、坏行报行号；seed 四来源优先级合成（镜像原样 / model_hash 精确匹配 / general / plausibly_general 降档标 borrowed + `pool-` 前缀）、同 change_pattern 冲突项目实测优先、**REUSE 不重种**；merge（confirm_models/refute_models **集合计数幂等**、confirm≥2 → general、refute≥2 → quarantined、镜像覆盖写、坏行剔除披露） |
| profile_mode | env 优先（含非法枚举 rc2）；npu-smi 探测（PATH 注入 stub：型号字段命中 → mfu；**"1951 MB" 类伪命中 → 不识别 → rc2**）；fallback；复用不一致/文件缺失 rc2 |
| inputs 退役 | §1 的 grep 验收 |

既有用例改造：gate 的 stall/exhausted 系（test_gate_best_effort_when_exhausted_with_best / test_gate_finish_failed_when_no_promoted_anywhere / test_gate_stall_resets_on_promoted_round 等）按 v5 决策序重写；advance 系 fixture 从 promoted 行改为 v5 行；history builder 字段集断言更新。

### 11.2 smoke 序列（核心状态机，脚本级端到端）

`tests/test_po_v5.py` 内构造 fixture 工作区，按真实顺序驱动脚本链并断言盘面（LLM 子代理产物不做断言对象——规则产出归真机清单）：

1. freeze-origin（base=1000, r=0.5, budget=0.1 → target=501）
2. 轮 1（latency）：verdict 判 pass（900<1000）→ Step6 advance → best=900、direction improved=true；probe 入口 mode 仍 latency → 直通；gate loop
3. 轮 2（latency）：verdict pass（450 < incumbent 900 且 ≤501 达线）→ Step6 advance（latency）→ best=450；**probe 入口 mode 已翻 accuracy 且 best.vid 无 probe 行 → 首入只训 best.vid** → 判 accuracy_fail（gap 0.5）→ accuracy advance 无过者 → 不推进、marker (R2, accuracy, improved=false)、failed_sigs 含该 vid 的 sig → gate loop（mode=accuracy）
4. 轮 3（恢复轮：best.vid 已有 probe 行 → 训本轮幸存者）：survivor accuracy_pass（gap 0.05，makespan 460≤501）→ advance 换 best + append_advanced → gate：best.vid 存在 accuracy_pass 任意版本行 → full-train
5. 轮帽 fixture：max_rounds=3（R1 latency 改进 + R2 翻转首入 fail + R3 恢复无过者）→ best-effort；无 best fixture → finish-failed

### 11.3 工作流级验收

- `tars validate`（workflow + agents + subagents）零 error、洁净 warning 清零。
- 全部相关 pytest 真实跑绿（WSL .venv）。

### 11.4 用户真机 E2E 清单（不阻塞本 SPEC 验收，归属用户 NPU 服务器）

承接草稿 §5 中脚本级测试覆盖不到的点：链式推进证据（轮 2 瓶颈报告变更 + 血缘链）/ 追击期零训练断言 / 回退与组合式提案准入 / accuracy-analyst 真实产出规则（含 direction/generality 打标）与跨 run 回种 / 换路不重复 / 远端 run `…-3c2dd3` gate 根因核对（部署 scripts 与 HEAD 差异 + proposals.json 落点）。

---

## 12. 失败路径汇总（fail loud 矩阵）

| 场景 | 行为 |
|---|---|
| ORCA_PO_NPU_* 非法枚举 / npu-smi 解析不出芯片型号 | resolve_profile_mode exit 2，启动即失败 |
| 复用工作区 profiling 模式漂移 / profile_mode.json 缺失 | reuse_check exit 2，指引 fresh_start |
| origin 锚缺失被 gate/advance/verdict/probe 读 | 各脚本 exit 2，节点 fail loud |
| --freeze-origin 内容冲突 / 量程非法 | exit 2（锚不可变，文案指引 fresh_start） |
| gate 不变量破坏（mode=accuracy 但 best.vid 无 probe 行） | exit 2 → 兜底 po_report 披露 |
| deploy --verify 不符 | 消费节点 fail loud + 指引 fresh_start |
| accuracy-analyst 产物违规 | 重派 1 次 → 剔除坏行继续 + 披露（不阻断轮次） |
| rules_pool seed/merge 的池或镜像缺失/损坏 | best-effort：stderr 披露 + 视为空源/跳过合并（资产不阻断主链） |
| 恢复轮变体超 target 线 | 复测 latency_fail，机械淘汰，不进粗训 |
| 恢复轮无 accuracy_pass 幸存者 | 不推进（底座固定），failed_sigs 换路标记，gate loop |
| 单变体 probe 不可证 | 沿用 v4：记录终态继续，节点不因单点失败 |

---

## 13. 文件触达清单

| 文件 | 动作 |
|---|---|
| `workflows/prof-opt.yaml` | 改（§1/§10） |
| `workflows/agents/po_flatten/agent.md`、`scripts/reuse_check.sh` | 改（§2/§8.1） |
| `workflows/agents/po_baseline/agent.md` | 改（§3/§8.2） |
| `workflows/agents/po_propose/agent.md`、`references/structural-levers.md`（如引用穷尽语义）、`scripts/run_latency_recheck.sh`、`scripts/check_prerequisites.sh` | 改（§8.3） |
| `workflows/agents/po_probe/agent.md`、`references/probe_protocol.md` | 改（§8.4） |
| `workflows/agents/po_report/agent.md` | 改（§8.6） |
| `workflows/agents/po_full_train/agent.md`、`po_contract/agent.md` | 改（§8.7） |
| `workflows/subagents/prof-opt/structure-proposer.md` | 改（穷尽出路删除 + predicted_acc_impact 字段族 + 组合式提案 + 换路指令） |
| `workflows/subagents/prof-opt/accuracy-analyst.md` | **新增**（§8.5） |
| `workflows/agents/_po_scripts/`：gate_decide.py / gate_node.sh / advance_round.py / history_lib.py / verdict_decide.py / analyze.py / deploy_scripts.sh | 改（§3/§5/§6/§7/§9） |
| `workflows/agents/_po_scripts/`：round_state.py / resolve_profile_mode.sh / rules_pool.py | **新增**（§2/§4/§8.5） |
| `tests/test_po_scripts.py` | 改（§11.1） |
| `tests/test_po_v5.py` | **新增**（§11.1/11.2） |

---

## 14. 遗留与依赖

1. **远端 gate bug 根因**（run `…-3c2dd3` proposals.json 失联）：D-V5-5 单一来源 + D-V5-6 入口重部署双防已入本 SPEC；服务器实测确认归用户真机（§11.4）。
2. **规则跨用户/跨机器共享**：池限本机 `$ORCA_HOME`；多机同步（NAS/git）另议。
3. 恢复轮推进收紧为「仅 accuracy_pass」已由 U1 裁决定案；如后续实测发现恢复期梯度断裂问题，升级为单调棘轮属 SPEC 变更（须重新过确认闸）。
