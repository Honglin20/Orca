# 外挂知识库（Agent 结构性探索）

按**模型族**组织，供 Hypothesizer / Engineer / Analyst 按需切片读取，避免冗余读取。
完整设计见 [`docs/specs/agent-structural-exploration-design-draft.md`](../docs/specs/agent-structural-exploration-design-draft.md) §7。

## 加载规则（四重过滤：冗余 + 硬件 + 弱 LLM）
1. **族级过滤**：Setup 时 **LLM 读 model.py 判断族**（可多族取并集）→ 只加载命中族 + common，未命中族永不读。
2. **方向选择（三层族）**：命中族若有 `tiers`（当前仅 wireless_receiver），按 `meta.json` 标签（`ascend/latency_tier/risk/physics/attention`）**规则化筛 2–4 个 direction** 作为 `{selected}`——少依赖 LLM，适配弱 LLM。
3. **任务级切片**：每个 agent 只注入 `index.json → agent_slices` 里它声明的文件（见下表）；token 走 **load-if-exists**（单层族与三层族兼容）。
4. **硬件过滤**：所有 agent 先过 `common/ascend_constraints.md`——变异前提是昇腾友好，`ascend==hostile` 默认排除。
5. **run 级缓存**：本 run 需要的每个文件只读一次，agent 引用路径由编排器注入切片。

## 目录
- `index.json` — 总索引（v2：族→文件+tiers、agent→切片、`direction_selection` 策略、`slice_semantics`）。
- `common/` — 跨族通用：`principles.md`（结构-性能原则）/ `latency_heuristics.md`（通用降时延手法）/ `primitives.md`（通用结构原语）/ **`ascend_constraints.md`（昇腾硬件铁律，所有族变异前的过滤层）**。
- `families/<name>/` — **单层族**（`cnn` / `transformer`）每族 4 件：
  - `primitives.md` / `patterns.md` / `latency_moves.md`（**本族降时延 move，workflow 核心**）/ `failures.md`
- `families/wireless_receiver/` — **首个三层族**（Family→Direction→RAW，见下节）：
  - base：`primitives.md` / `latency_moves.md`（31 条变异算子）/ `failures.md`
  - `directions/` × 12（架构模板/变异锚点：baseline / DeepRx / EqDeepRx / A-MMSE / FNet / Channelformer / NVIDIA NRX / windowed-axial / ISTA-LISTA / Mamba / residual-around-LMMSE / KD-to-conv）
  - `raw/` × 13（实现示例，带变异提示，给弱 LLM 当 few-shot 锚点）
  - `meta.json`（每 direction 的 ascend/risk/physics 标签，供规则化检索）

## 三层结构（Family → Direction → RAW）
- **Family（大族）**：cnn / transformer / wireless_receiver。LLM 检测。
- **Direction（方向）**：架构级模板（DeepRx 风格、A-MMSE 折叠线性、FNet FFT-mix…）。**薄而多**——"大部分方向可能没用"所以广撒网，靠 `meta.json` 标签规则化筛 2–4 个，少依赖 LLM。
- **RAW（实现示例）**：可跑骨架 + 变异提示，让较弱 LLM 也能"照着改"而非"从零发明"；每方向 ≥1 份、风格多样防"抄死"。
- **Move（原子变异算子，`latency_moves.md`）**：跨 direction 的扁平算子库（pointwise 化 / BN-fold / windowed-attn / soft-threshold…），与 direction 正交——一个变异可以是"换 backbone（direction）"或"改单个模块（move）"。

> 检索四重过滤：族检测 → 标签筛 direction → agent slice(load-if-exists) → run 缓存。**绝不整库灌入**（弱 LLM + 小 context 必须精准）。

## agent 切片表（v2；token 走 load-if-exists，三层族多出 `{selected}` 方向/RAW）
| agent | 读 | 写 |
|-------|----|----|
| Hypothesizer | common.ascend_constraints, common.principles, common.latency_heuristics, {family}.primitives, {family}.latency_moves, {family}.directions/{selected} | — |
| Engineer | common.ascend_constraints, {family}.patterns, {family}.primitives, {family}.directions/{selected}, {family}.raw/{selected} | — |
| Analyst | common.ascend_constraints, common.principles, {family}.failures | common.principles, {family}.failures（追加） |

## 新增一个族
- **单层族**（例 mamba）：`families/mamba/` 下建 `primitives.md` / `patterns.md` / `latency_moves.md` / `failures.md`，在 `index.json → families` 注册。
- **三层族**（例后续无线变体）：再加 `directions/` + `raw/` + `meta.json`（每 direction 标 ascend/latency_tier/risk/physics/attention），并在 `index.json` 该族下加 `tiers`。
- 族检测靠 LLM，无需指纹表；`detect_hints` 字段给 LLM 提示。

## 内容来源
- transformer / cnn 族：参考 [`references/nas/`](../references/nas/)（ASI-ARCH / EvoPrompting / LLMatic / NNGPT / LLM-NAS / LAPT 等）+ 业界 SOTA 效率技巧。
- wireless_receiver 族：DeepRx / EqDeepRx / A-MMSE / Channelformer / NVIDIA NRX / Yellapragada axial / ISTA-Net / FNet / MEAN / SPiNN 等 + 昇腾 CANN/GE 机制（详见各 direction 来源与 `common/ascend_constraints.md`）。规划与全部 direction/move 清单见 [`docs/plans/2026-07-16-wireless-ascend-latency.md`](../docs/plans/2026-07-16-wireless-ascend-latency.md)。
- `failures.md` 初始即含本族 AVOID 清单（DW/group/dynamic-shape/手搓-attn/N=64-linear-陷阱/INT4/BNN…），Analyst 随 run 持续追加。
