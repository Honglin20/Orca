# Puzzle U6 设计草稿 —— 治本：从「接口契约」翻转为「agent 移植 + 确定性算法壳」

> 跨阶段设计议题，U6 各步实施前必读。根因诊断见同目录分析（2026-08-12 双 agent 审计）。
> 范式对标：`nas-supernet-v3`（用户实测跑通）。本草稿不改 Puzzle 算法本征（BLD/score/MIP/GKD），只改**算法脚本与用户代码的契约边界**。

---

## 0. 根因一句话

Puzzle 把「算法运行所需的用户数据 / eval / loss / ckpt」误建成了「用户必须满足的接口契约」（零参工厂、单参 eval、单 tensor forward、双零 strict-load、写死 CE）。nas-supernet-v3 把同样的东西当成「agent 必须从用户源码 faithful 移植进生成脚本的产物」。U6 把 puzzle 翻转到 nas3 范式。

---

## 1. 铁律：通用性（最高优先级，覆盖一切）

**target 项目只是奇形怪状项目中的一个典型，不是特例。** 所有改动必须提取**通用规则**，禁止任何维度针对 target 的定制：

1. **禁止**在 agent.md / 脚本 / yaml 出现 target 项目的事实（类名 `CrossFusion` / `UserDataset` / 文件名 `train_and_eval.py` / 路径 / 输入路数 4 / InfoNCE 字面量 / `pre_trained.pth` 等）。示例只能用 `<...>` 占位符或泛化描述。
2. **禁止**「if 项目 == X」式分支。一切项目差异由 agent 读源码判定后写进 manifest / 适配器，脚本据 manifest/适配器分派。
3. 每修一个 bug，先问「这是一类项目的通病，还是 target 独有？」只修通病；target 独有的边缘情况由适配器层吸收，不进算法脚本。
4. **洁净度审查**必须专门查这条：grep target 事实字面量 + 审「该分支/常量是否隐含某类项目假设」。

违反铁律 = 改动作废。

---

## 2. 新契约边界（Architecture A —— 增强适配器）

算法脚本（bld / score / gkd / latency_table / build_selected / mip_select / gate_report）保持**确定性 + 可单测**，但**不再假设任何用户代码形态**。所有项目相关性收敛到 **agent 在 `pz_expand` 移植生成的一个适配器模块** `puzzle_adapters.py`（合并 F1 的 puzzle_data.py + puzzle_eval.py，扩权），它对用户代码形态做 faithful 移植，对脚本暴露**项目无关的稳定能力 API**。

### 2.1 适配器能力 API（脚本唯一项目接口）

脚本只通过以下能力调用项目逻辑（签名稳定，实现由 agent 移植用户源码）：

```python
# puzzle_adapters.py —— agent 读用户源码 faithful 移植生成
def build_model() -> nn.Module: ...                  # 零参实例化（agent 把 config 烧进去）
FORWARD_CALLING_CONVENTION: str = "positional"        # "positional" | "dict" | "single"
def forward_model(model, batch) -> output:            # 按 convention 调 model(...)，处理多输入/dict batch
def calib_iter(device=None) -> Iterator[batch]: ...   # 真实 calib 数据（agent 移植 Dataset 构造 + collate）
def train_iter(device=None) -> Iterator[batch]: ...   # 真实训练数据（同上，含 labels）
def extract_labels(batch) -> torch.Tensor | None: ... # 从 native batch 抽标签（classification 用）
def kd_loss(s_out, t_out, labels=None) -> Tensor: ... # agent 按任务移植正确 KD（cosine/KL/MSE/任务 loss）
def task_loss(s_out, labels) -> Tensor | None: ...    # 硬标签监督（agent 移植用户任务 loss；非分类返 None）
def evaluate(model) -> float: ...                     # 移植用户 eval 协议（含 device/kNN/metric/方向）
METRIC_DIRECTION: str = "higher-better"               # "higher-better" | "lower-better"
EVAL_NOISE_ATOL: float = 1e-9                         # eval-stability 容差（agent 据评估协议噪声推；kNN/采样 ≥1e-2）
def load_pretrained(model) -> "_LoadResult": ...      # ckpt 加载（宽松前缀剥离 + train_from_scratch 兜底）
DUMMY_INPUT: dict = {"shape": [...], "dtype": "float32"}  # 真实 I/O 维度（多输入用 list of shape + convention）
```

**关键**：脚本**不再**自带 `batch[0]`/`batch[1]` 取输入/标签、不再 `_flatten_model_output` 取首 tensor、不再写死 `cross_entropy`、不再 `model(single_tensor)`、不再 strict-load 双零断言。这些全由适配器消化。

### 2.2 适配器生成职责（agent，faithful 移植 ≠ 改写）

对齐 `project-porter.md` 的「faithful mover」契约：保留用户公式 / 常数 / 符号 / 特征索引 / 控制流 / 随机性语义；**禁止**简化、近似、替换相似工具、丢「看起来不重要」的项。允许的机械适配（与 porter 一致）：重写项目内 import 为同级 import、参数化硬编码路径、device 用传入或 `resolve_device`、剥 DDP/rank/barrier 保留计算、把网络构造抬成 caller-injected。

适配器由 `pz_expand` 生成，落 `$ORCA_ARTIFACTS_DIR/puzzle_adapters.py`；下游脚本经 manifest 指向它（脚本不解析 manifest，agent 桥接 CLI args）。

---

## 3. 确定性脚本改造清单（逐条映射根因）

| 根因 ID | 当前病灶 | U6 改造 |
|---|---|---|
| **A 零参工厂** | `build_real_calib_loader` 零参 `fn()` + `first_batch[0]` 强取单 tensor | 删 `build_real_calib_loader`；脚本调 `adapters.calib_iter()` 得 native batch，`forward_model(model, batch)` 喂模型。无零参工厂契约 |
| **K 单 tensor forward** | 全链 `model(randn)` + `batch[0]` | 全部改走 `forward_model(model, batch)`；多输入/dict 由适配器的 convention 处理 |
| **B 单参 eval + 1e-9** | `fn(model)` + atol=1e-9 逐位相等 | 脚本调 `adapters.evaluate(model)`；stability 容差读 `adapters.EVAL_NOISE_ATOL`（agent 据协议噪声推），默认 1e-9 仅纯确定性 eval |
| **D loss 写死** | gkd `cross_entropy(s_out, labels)` / `logits_kd_loss` 仅分类 | 脚本调 `adapters.kd_loss(...)` + `adapters.task_loss(...)`；agent 移植正确 loss。删 `is_classification`/`eval_kind` 分支 |
| **C 双零 strict-load** | `measure_baseline` missing/unexpected 双零 raise；哨兵仅 `blocks.`/`patch_embed.` | 移除脚本侧 strict-load 双零硬门；加载走 `adapters.load_pretrained` 返回 `_LoadResult(missing, unexpected, from_scratch)`；前缀剥离由适配器处理（`module.`/`_orig_mod.`/`ema.`/多字段 dict）。flatten 阶段对齐 ns3：只跑前向 dummy smoke，ckpt 非双零不 fatal（仅记录 + WARN） |
| **E make_zero 全 slot** | `latency_table` 无条件 floor + 主循环缺 `is_candidate_valid_for_slot` | 主循环 + floor 循环都过 `is_candidate_valid_for_slot`；非方 slot 的 floor 用「原块实测 latency」兜底（禁 `make_zero` raise） |
| **F mask 塌缩** | catalog 全 `mask_aware:false` → mask-bearing slot 塌缩 identity | catalog 加 `mask_aware:true` builtin（causal/padding 兼容的 mixer 变体）；mask-bearing slot 的候选至少含 1 个 mask_aware + identity |
| **G target 时序悖论** | `target_latency>[ask]` 必填 + `>baseline/2` 早警 terminate | `target_latency` 改 **[advanced]**（可选，空则 MIP 取 baseline\*0.7 作软目标 = 对齐 LAT AC 70%）；删 `target-too-aggressive` 早警硬 terminate；MIP 只报 feasibility，LAT AC 由 gate 判 |
| **H eval_kind 枚举** | `[ask]` 三枚举 + 脚本全信用户 | `eval_kind` 从 user input 移除；agent 读源码判定写进 manifest（`metric.direction` + `paradigm`）；脚本读 manifest/适配器（`METRIC_DIRECTION`） |
| **I gate 方向** | `gate_report` 无视方向（regression 必失败） | gate 读 `adapters.METRIC_DIRECTION`；lower-better 用「final ≤ baseline×(1+tol)」公式；ACC 容差 baseline-dependent（高 baseline 绝对 tol / 低 baseline 相对 tol，tol 量级按 metric 方差） |
| **J outputs 收敛** | `outputs` 直引 `pz_report.output.*`，早 terminate 崩 | 引入单一确定性 reporter 模式（见 §5）：所有 terminate 路径 + pz_report 的终态都落盘 `final_status.json`；`outputs` 只引 `pz_expand.output`（entry 恒跑）+ 磁盘 final_status（reporter 读盘填，对齐 `ns3_report` first-match 状态机） |

---

## 4. per-节点改造要点

### pz_expand（入口，最大改动）
- 生成 `puzzle_adapters.py`（合并 + 扩权 F1 的 puzzle_data/eval），按 §2.1 API + porter faithful 契约。
- manifest 加 `metric.direction` / `paradigm` / `forward_calling_convention` / `eval_noise_atol` / `ckpt_load_strategy`；删 `eval_kind`/`evaluation_entry`/`data_loader_entry` 的「用户接口」语义，改记「适配器能力」。
- `eval_kind` 不再要求用户填；agent 从源码判定。
- flatten：对齐 ns3 —— 多输入不靠「拼成 1D 向量」hack（破坏 forward 语义且 smoke 查不出），改由适配器 `forward_model` + convention 处理；flat 文件只保模型结构 + build_model + DUMMY_INPUT（DUMMY_INPUT.shape 支持多输入 list 形态）。
- fidelity smoke：删 strict-load 双零硬门（改 `_LoadResult` 记录）；保留 forward-determinism / eval-stability（atol 读适配器）/ per-slot identity allclose。

### pz_build_library / pz_score / pz_retrain（executor）
- 全部脚本入参从「`--calib_loader_fn`/`--train_loader_fn`/`--eval_fn`/`--eval_kind`」改为「`--adapters <path>` + `--manifest <path>`」。
- bld：teacher 激活捕获走 `forward_model`；calib 走 `calib_iter`。
- score：replace-1-block 打分走 `forward_model` + `kd_loss`（agent 移植的 distance）。
- gkd：训练循环走 `train_iter` + `extract_labels` + `kd_loss` + `task_loss`；删 `is_classification` 与写死 CE。
- build_selected：实例化逐层异构架构，从 block_library load 权重（unchanged 算法）。

### pz_select（确定性，zero-LLM）
- mip_select：删 `target-too-aggressive` 早警；`--target-latency` 可选（空→baseline×0.7 软目标）；输出加 `feasible` + `select_reason`（mip-optimal/infeasible/none）。
- 路由守卫不变（selected_arch 非空 + feasible）。

### pz_report（确定性 gate）
- 读 `adapters.METRIC_DIRECTION` + `evaluate`；方向感知公式；ACC 容差 baseline-dependent。
- 落盘 `final_status.json`（统一终态）。

### 终态收敛（§5）

---

## 5. 终态收敛（对齐 ns3_report）

新增确定性逻辑：所有 terminate 节点 + pz_report 在落盘时写 `final_status.json`（first-match 状态机字段：`stage` / `status` / `reason` / `metrics`）。`puzzle.yaml.outputs` 重构为只引 `pz_expand.output`（entry 恒跑，绝不 StrictUndefined）+ 一个「读 final_status.json」的确定性字段。terminate 节点 reason 文案保留人读诊断。

实现选项（实施 agent 自选，满足「早 terminate 不崩 + 成功路径有 gate 结果」即可）：
- (a) terminate 节点 body 内置写 `final_status.json`；outputs 读盘。
- (b) 或保留 outputs 引 pz_report，但 Orca 引擎保证 terminate 路径不求值 outputs（先确认引擎语义再选）。

---

## 6. 测试 / 验收（U6 完成标准）

- 确定性脚本单测：每个改造点有 fail-loud 用例（多输入 batch / lower-better metric / 非方 slot / 无 ckpt / mask-bearing slot）。
- `tars validate` 0 error / 0 warning（含 prompt 洁净 + 通用性 grep）。
- 通用性 grep：脚本/agent.md/yaml 不得出现 target 事实字面量。
- E2E（U6 之后 phase）：playground/target 上 headless 跑通，时延降 ≥70% + 精度不降（headless in-session：opencode run + tars skill + orca CLI）。

---

## 7. 不改的东西（防过度设计）

- Puzzle 算法本征（BLD normalized-MSE 蒸馏 / replace-1-block 打分 / MIP grouped-knapsack / GKD 末段 KD）—— 不动。
- 节点拓扑（pz_expand → build_library → score → select → retrain → report + 4 terminate）—— 不动。
- in-session 执行契约（orca CLI / chart daemon / artifact 约定 / bounded-polling / self-heal 白名单 / progress.jsonl）—— 不动（对齐 nas3 的薄壳）。
- candidate_catalog 的 kind 体系（attention/ffn/conv/moe/custom + identity 必入）—— 只加 mask_aware builtin，不改架构。
