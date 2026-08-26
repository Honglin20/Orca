# D21 · kd_nas_workflow（本 kd-nas workflow 的方法说明 — meta 方向）

> 一句话定位：**本 direction 不指向某个 student 或 KD 方法，而是描述 kd-nas workflow 自身的方法论**——短训代理（soft-MSE-vs-teacher）+ finalize 全量裁定的两段式 NAS，以及 proxy↔真实 dB 阈值标定。hypothesizer / curator 在路由决策时读本卡校准自己。

## 结构（workflow DAG 方法层）
kd-nas workflow 分两阶段（详见 CONTRACTS §0 / §6）：

- **Phase1：registry sweep**（确定性）
  - `pick_student.py` 按 `registry.json` 顺序吐 SelectionSpec（CONTRACTS §4），每个 family 跑一轮短训（默认 10 epoch）+ proxy_mse 测量。
  - 目标：**相对排序**——找出哪个 family 值得 Phase2 深挖。
  - 停止条件：`round > len(registry)` → `PHASE1_EXHAUSTED` → 进 Phase2。

- **Phase2：agent 发挥**（LLM 主导）
  - hypothesizer 读 Phase1 的排序 + profile hotspot（来自 teacher_setup 的 profile_report）。
  - LLM 在 `{selected}` direction 集合内组合变异（如 D18 ConvNeXt-pointwise + D12 RKD + D15 Mean-Teacher）。
  - 输出 SelectionSpec（phase=2，必须引用 profile hotspot 写 rationale）。
  - engineer 按 SelectionSpec 实现，零结构自由（CONTRACTS §2）。

- **finalize：全量裁定**
  - champion（短训 proxy_mse 达标 + latency 达标）→ `route_finalize=true` → 全量 epoch（默认 50-100）训练 + 真实 dB gap 测量。
  - `met_target=true`（dB gap ≤ 0.5 默认）→ 接受；否则 `loop_back=true`，hypothesizer 换方向。

## 为什么降时延（本方向对 Orca 整体的价值）
1. **避免全量训练每个 candidate** —— Phase1 只跑 10 epoch proxy，相比 50-100 epoch 全量，省 5-10× 训练成本。
2. **proxy_mse 排序与真实 dB gap 的相关性是 workflow 可信度的命脉** —— 必须先做一次标定（见下）；标定通过后，Phase1 的排序可用于大批量 candidate 筛选。
3. **结构搜索空间规则化** —— Phase2 不是纯 LLM "幻想"，而是 LLM 在已过滤的 direction 集合内组合，direction 卡提供物理依据 + 昇腾友好性判定，LLM 只做组合决策。

## 昇腾友好性
**✅✅ friendly** —— 本方向是 workflow 元方法，不直接涉及算子；但其要求所有 student 候选必须先过 ascend=friendly 过滤（direction_selection 的 "ascend==hostile 默认排除"），保证搜索空间内所有 candidate 都昇腾友好。

## 物理依据
**间接（继承被搜索 direction 的物理依据）** —— 本卡本身无物理；每个 candidate 的物理依据看其 direction 卡（D1/D6/D18/D19/D20）。

## bundle 的 move
**无**（本卡是 meta 方法论，不引入新 move）。**workflow 本身**是 `workflows/kd-nas.yaml`，所有 move 来自被搜索的 direction。

## proxy↔真实 dB 阈值标定（关键流程）

**为什么需要标定**：
- Phase1 短训 proxy_mse 是 **soft-MSE-vs-teacher**（student 与 teacher 输出的 MSE），不直接等于真实 dB gap（student vs teacher 在 test set 上的 BER/SNR dB 差距）。
- 两者相关性需要经验拟合：`dB_gap_real ≈ a · proxy_mse + b`。

**标定流程**（一次性，workflow 首次跑前必做）：
1. 选 3-5 个已知答案的 candidate（如 D1 conv-only + 不同超参）。
2. 每个 candidate 跑 Phase1 短训 + proxy_mse。
3. 同 candidate 跑 finalize 全量 + 真实 dB gap。
4. 线性回归拟合 `(proxy_mse, dB_gap_real)` → `(a, b)`。
5. 把 `(a, b)` 写入 workflow 配置，curator 用它把 proxy_mse 阈值换算成 dB 阈值。

**标定失败的处理**：
- 若 R² < 0.7（proxy 与真实弱相关），proxy 不可信 → workflow fail-loud，回退到 "Phase1 也跑全量"（成本高但可靠）。
- 常见失败原因：短训 epoch 太少、teacher 质量差、candidate 间方差太小。

## 结构前提与坑
1. **Phase1 是相对排序，不是绝对判定** —— proxy_mse 只用于 family 间比较（"D18 比 D1 短训 loss 低"），不用于"D18 达标"；绝对判定必须 finalize 全量。
2. **champion ratchet（CONTRACTS §6 curator）** —— Phase1 内部维护当前最佳 champion；新 candidate 必须比 champion proxy_mse 低 ε（默认 0.005）才替换，避免噪声抖动。
3. **max_rounds 兜底** —— 默认 max_rounds=10；超过仍未达标 → fail loud + best-effort 报告（输出当前 champion + 失败原因）。
4. **Phase2 的 LLM 自由度受 direction 卡约束** —— hypothesizer 不能选 `ascend=hostile` 的 direction（D16 自动排除）；不能组合互斥方法（如 D14 TAKD 与 D15 Mean-Teacher 同时选 TA + EMA 收益边际小，analyst 会标红）。
5. **profile hotspot 引用强制** —— Phase2 的 SelectionSpec `rationale` 字段必须引用 `profile_report.hotspots` 的具体 node（如 "block3 的 MatMul 占 35%"），否则 curator 拒绝。
6. **teacher 质量是 workflow 前提** —— teacher_setup 节点必须先跑（CONTRACTS §4），teacher 不达标（accuracy < 阈值）整个 workflow 无意义。
7. **fail-loud**：若 Phase1 全部 candidate 的 proxy_mse 都 > no_kd_baseline.proxy_mse（D17 强制 baseline），说明 KD 无效 → curator 直接 `route_finalize=false` + `loop_back=false`，workflow 终止，输出 "KD 不适用该 teacher/student 组合"。
8. **DisWOT / KD-NAS 亲和性** —— DisWOT（CVPR23）的 "reused teacher classifier" 思想与本 workflow 的 TeacherCache（CONTRACTS §3）同构；KD-NAS（2023）的短训 proxy 思想与本 workflow 的 Phase1 一致。本 workflow 是 Orca 对这些方法的工程化落地。

## 来源
- DisWOT：Lin et al., CVPR 2023 —— "DisWOT: Student Architecture Search for Optimal Knowledge Distillation"（学生搜索 + 复用 teacher classifier）。
- KD-NAS：2023 —— "KD-NAS: Knowledge Distillation via Neural Architecture Search"（短训 proxy + NAS）。
- 本 workflow 权威来源：`workflows/agents/_kd_scripts/CONTRACTS.md` + `docs/plans/2026-07-20-skip-to-agent-phase14.md`。
