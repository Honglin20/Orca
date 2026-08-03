# kd-nas 候选生成重构 设计草稿

> 状态：草稿（2026-08-03）。各阶段 SPEC 撰写前必读。对应实施计划 `docs/plans/2026-08-03-kd-nas-candidate-gen-rework.md`。

## 1. 背景（问题）

`kd-nas` 的 student 候选当前来自 KB 预置的 14 个 `knowledge_base/families/receiver/spt_*.py`：

- **shape 写死**：每个 `spt_*.py` 都硬编码 `DUMMY_INPUT = {"shape":[1,4,48,64,1],...}`（demo model8 的 64 子载波泄漏）。gate 测时延时 shape 取自变体自己的 `DUMMY_INPUT`（`gate_all.py:114` → `tune_latency.py:157` → `export_onnx`），导致所有候选按 64 维导 ONNX，算子量近乎翻倍 → 系统性高估时延 → **全 `FAIL_latency` 不达标**。
- **候选与用户模型脱节**：flatten 已把用户真实模型展平成契约（`baseline_contract_path`，含正确 shape），但它只喂给 teacher_gen / train_script_gen / setup；**gate 读的 student 候选池根本没碰用户模型**——候选是 KB 里与用户任务无关的预置结构。

## 2. 目标

student 候选改成**两阶段从 flatten 产物派生**，所有变体 shape 跟用户真实输入；KB 降级为「结构模板参考库」。训练/评估链路（gate/train/select）契约不变。

## 3. 设计

### 3.1 DAG

```
flatten ──┬→ candidate_gen ──────────────────┐
          └→ teacher_gen → train_script_gen → setup → gate → train → select
```

- `flatten.routes` = `[{to: candidate_gen}, {to: teacher_gen}]`（并行分叉，互不依赖）
- `candidate_gen.routes` = `[{to: setup}]`
- `setup` 同时依赖 `train_script_gen`（既有）+ `candidate_gen`（新增 `candidates_dir`）

### 3.2 candidate_gen 节点 I/O

```yaml
output_schema:
  required: [candidates_dir, n_variants]
  properties:
    candidates_dir: {type: string}  # 候选变体目录绝对路径（末尾带 /），$ORCA_ARTIFACTS_DIR/candidates/
    n_variants: {type: integer}     # 候选总数；0 → fail loud
```

单节点内编排两阶段，确定性逻辑全在脚本（阶段一）/ LLM 只做结构合成判断（阶段二）。

### 3.3 阶段一：轻量化变体派生（确定性脚本 `derive_lightweight.py`）

- **输入**：`--baseline_contract <flatten 产物>` `--out_dir <candidates_dir>` `[--cap 8]` `[--device auto]`
- **机制**：读 baseline 的 `KNOBS` + `DUMMY_INPUT`；沿每 knob 的 `step`（<0）方向，生成代表性缩放组合：
  - 每 knob 单独缩到 `min`（其余保持 default）→ K 个（K = knob 数）
  - 全 knob 同时缩到 `min` → 1 个
  - 总数 ≤ `cap`；超 cap 时优先保留单 knob 缩放，全缩仅在名额内
- **wrapper 生成**（teacher-gen wrapper 镜像，方向相反 = 缩小）：
  - `_BASELINE_CONTRACT_PATH` + `_load_baseline_module()` + `_baseline_build_model`
  - `def build_model(**cfg): return _baseline_build_model(**cfg)`（一行委托）
  - `DUMMY_INPUT` 逐字复制 baseline（KD 硬约束：teacher/student 同 I/O shape）
  - `KNOBS`：`default` = 缩放值，`min/step/leverage` 继承 baseline
  - **省略 `__main__`**：候选的 latency 由 gate 统一测（`tune_latency.py`），candidate 不自测（职责单一）
  - 文件名 `lw_<knob>_<value>.py` / `lw_all_min.py`；`variant_id` = stem，`lw_` 前缀避免与历史 `spt_`/`kb_` 冲突
- **校验**：每个 wrapper 调 `model-flatten/scripts/validate_contract.py`（复用通用门），PASS 才写入；FAIL → stderr WARN 丢弃（不杀整批）

### 3.4 阶段二：KB 模板合成（LLM agent）

- **输入**：baseline 契约 + KB `spt_*.py`（glob `$ORCA_KB_DIR/families/receiver/*.py` 作结构模板参考）+ baseline `DUMMY_INPUT`
- **任务**：LLM 读 `spt_*.py` 提取结构技术点（SE attention / dilated conv / large kernel / unet skip / pointwise / ...），结合 baseline 的 IO 语义 + block 结构，合成 N（默认 4）个「借鉴 KB 技术点的新结构」变体 `.py`
- **硬约束**：
  - 每个变体 **standalone**（不 import baseline / `_kd_scripts` / 用户项目；只依赖 torch + 3rd-party pip）
  - `build_model(**cfg)` + `DUMMY_INPUT.shape` **必须 = baseline shape**（禁写死 64）
  - `KNOBS` 非空（`step<0` / `leverage∈{high,medium,low}` / `build_model(**mins)` 可 forward）
- **校验**：每个变体 `validate_contract.py` PASS（fail loud；不过则迭代修到 PASS 或丢弃 + WARN）
- 文件名 `kb_<technique>.py`；**不用 worktree**（本节点不训练）

### 3.5 候选契约（与现有 receiver spt_*.py 完全一致，下游零改动）

```python
DUMMY_INPUT = {"shape": [<baseline 逐字复制>], "dtype": "float32"}
BUILD_FN = "build_model"
KNOBS = {<knob>: {"default","min","step(<0)","leverage(∈high/medium/low)"}, ...}  # 非空
def build_model(**cfg) -> nn.Module: ...
```

### 3.6 候选目录真相源

- `candidates_dir` = `$ORCA_ARTIFACTS_DIR/candidates/`（per-run，与 flatten 产物同级）
- 经 `setup.output.candidates_dir`（原 `receiver_dir` 重命名）传给 gate；下游 gate/train/select 全部经此透传，**不依赖 `$ORCA_KB_DIR`**

## 4. 下游契约不变性（已由 Explore 验证）

| 脚本 | 是否改动 | 说明 |
|---|---|---|
| `gate_all.py` / `pick_variant.py` / `train_pool.py` | **零改动** | `--receiver_dir` 是 CLI 参数，从 `setup.output` 透传；语义=「装候选 .py 的目录」 |
| `tune_latency.py` / `measure_student.py` / `distill_dispatch.py` | **零改动** | manifest-driven + 纯路径 import + `build_fn(**cfg)` |
| `kd_common.is_variant_done` | **零改动** | 身份用 `variant_sha256` + `latency_provider_id`，与目录路径无关 |
| `kd-setup/agent.md` | **改 step1/step6** | 候选源从 `$ORCA_KB_DIR/families/receiver` 改为 `candidate_gen.output.candidates_dir`；output 字段 `receiver_dir`→`candidates_dir` |
| `kd-gate/agent.md` | **改引用** | `{{ setup.output.receiver_dir }}` → `candidates_dir` |
| `kd-nas.yaml` | **加节点 + 重命名** | 新增 `candidate_gen`；`flatten.routes` 分叉；setup `output_schema`/`outputs` 字段重命名 |

## 5. KB 角色重定位

- 14 个 `spt_*.py` 不再被 gate 直接当候选测；降级为阶段二的「结构模板参考」（LLM glob 读其源码）
- shape 写死 `[1,4,48,64,1]` 问题**自动消失**（合成出的变体 shape 跟 flatten）
- 第一版**不引入** struct-exploration 的 `directions/latency_moves` md 技术点库（未来增强）

## 6. 边界与 fail-loud

- 阶段一生成 0 个合法 wrapper（baseline 无可缩 KNOBS / 全 FAIL validate_contract）→ 不阻塞，仅 WARN
- 阶段二 LLM 合成 0 个 PASS → 不阻塞，仅 WARN
- `candidate_gen.output.n_variants == 0`（两阶段合计 0）→ **fail loud**（无候选是配置错误，不静默继续 → 不让 gate 跑空 KB 误以为健康）
- 阶段二变体若需共享积木，必须把 `_*.py` 复制进 `candidates_dir`（loader 只把变体所在目录加 `sys.path`，见 `pick_variant.py:60`）

## 7. 未决 / 未来增强

- 阶段一缩放策略：当前 = 单 knob 缩 min + 全缩（K+1 个，cap 8）。未来可加"每 knob 多档 + 笛卡尔积"（需更智能的 cap 策略避免爆炸）
- 阶段二合成数量 / 模板选择：当前 LLM 自选 N=4。未来可让用户指定族/技术点
- KB 技术点结构化：未来引入 struct-exploration 的 `directions/` md，让阶段二读结构化技术点索引而非裸 `spt_*.py` 源码
