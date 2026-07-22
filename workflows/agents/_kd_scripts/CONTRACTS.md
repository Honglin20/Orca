# kd-nas workflow — 接口契约（CONTRACTS）

> 所有 build agent（scripts / kd-lib / students / agent-mds / KB）必须对齐本文件。
> 改接口 = 改本文件 + 通知所有依赖方。fail loud：任何脚本遇到契约不符的输入直接非零退出 + stderr 报因。

## 0. 目录布局

```
workflows/
  kd-nas.yaml                              # workflow DAG（本 workflow 入口）
  agents/
    kd-setup/agent.md                      # P7 合并（teacher_setup + profile_gate + kd_train_script_gen）
    kd-hypothesizer/agent.md               # Phase1 调 pick_student / Phase2 出 SelectionSpec
    kd-engineer/agent.md                   # 按 SelectionSpec 实现，零结构自由
    kd-curator/agent.md                    # P7 合并（analyst + curator + viz_round）
    _kd_scripts/                           # 确定性脚本 + KD 库 + student 模板
      CONTRACTS.md                         # 本文件
      _device.py                           # P7：resolve_device + ort_providers（inline 自 NAS）
      profile_onnx.py                      # P7：加 --device / --seed CLI
      teacher_setup.py                     # P7：加 --device / --seed / --strict-accuracy CLI；confidence=low 时标 teacher_accuracy_known=false
      measure_student.py                   # P7：解开 device="cpu" 硬编码，透传 --device
      pick_student.py
      viz_kd.py                            # P7：round 模式 db_gap/met_acc 移出默认列（短训占位）
      kd/
        losses.py
        wrapper.py
        compose.py
        ema.py
      students/
        _common.py
        deeprx_dilated.py
        lmmse_front.py
        eqdeeprx_shared.py
        mlp_mixer.py
        convnext_pointwise.py
        large_kernel.py
        ista_lista.py
        registry.json
      train_adapter_template.py            # kd-setup agent 生成 train_kd.py 的模板
```

**P7 节点合并**：原 `kd-teacher-setup` + `profile_gate` + `kd_train_script_gen` → `kd-setup`（一次性）；
原 `kd_trainer` + `measure_student` → `candidate_eval`（**latency-first**：先默认权重导 ONNX 测 latency，
不达标 FAIL_latency **不训练** → 通过才短训测 proxy_mse）；原 `analyst` + `viz_round` 折进 `curator`。
**不修改** `struct-*` 系列 agent md（它们被 `agent-struct-exploration` 复用）。

## 1. Student I/O 契约（所有 students 必须遵守）

每个 `students/<family>.py` 暴露：

```python
def build_model(**cfg) -> nn.Module: ...      # 实例化 student
DUMMY_INPUT = {"shape": [1,4,48,64,1], "dtype": "float32"}  # 与 teacher 同
BUILD_FN = "build_model"
```

**forward 契约**（与 teacher `SignalProcessingTransformer` 完全一致）：
- 输入 `[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]`
- 输出同形 `[B, 4, 48, 64, 1]`
- 内部自理 alpha 归一（`x = inp/(sqrt(mean(inp²)·2)+1e-6)`，出口 `*alpha`）——复用 `students/_common.py` 的 `AlphaNorm` / `signal_head`
- **禁用** attention 的 softmax（student 卖点）；Transpose 数最小化；pointwise 优先

**feature hook 契约**（供 OFD/FitNets 对齐）：
```python
class StudentXXX(nn.Module):
    def feature_hook_names(self) -> list[str]:
        return ["backbone.block2", "backbone.block4"]   # 对齐用的子模块名（≥1 个）
```
`KDStudentWrapper` 用这些名字注册 forward hook 拿中间 feature。

## 2. SelectionSpec schema（hypothesizer → engineer 的结构化契约）

hypothesizer（Phase2）或 `pick_student.py`（Phase1）输出 **JSON**：

```json
{
  "candidate_id": "lmmse_front_r3_v1",
  "phase": 1,
  "family": "lmmse_front",
  "build_cfg": {"num_blocks": 3, "kernel": 3, "use_lmmse": true, "embed_dim": 16},
  "kd_config": {
    "kd_losses": ["mse", "rkd", "ofd"],
    "weights":  {"mse": 1.0, "rkd": 0.1, "ofd": 0.3},
    "ema":      true,
    "scheduler": {"target_weights": {"mse":1.0}, "start": 0, "warmup_length": 5}
  },
  "rationale": "<一句话，Phase2 必须引用 profile hotspot>"
}
```

`family` 必须是 `students/registry.json` 里已注册的 key。**engineer 不允许改 family / 不允许加 spec 外的结构**；实现不了 → fail loud 回 hypothesizer。

## 3. KD 库 API（`kd/*.py`）

### `kd/losses.py`
```python
def mse_kd(s_out, t_out) -> Tensor                    # ‖s−t‖²（已 detach teacher）
def rkd_distance_loss(s_feat, t_feat) -> Tensor       # pairwise distance 对齐
def rkd_angle_loss(s_feat, t_feat) -> Tensor          # triplet angle 对齐
def ofd_feature_loss(s_feats: list, t_feats: list) -> Tensor  # 多 stage MSE（带 1×1 adapter 自动对齐维度）
def fitnets_hint_loss(s_feat, t_feat) -> Tensor       # 单点 hint（带 adapter）
def ema_consistency_loss(s_out, ema_out) -> Tensor    # mean-teacher 一致性
class KDWeightScheduler: ...                          # 复用三段 anneal：get_weight(epoch)->float
```
所有 loss 对 teacher 自动 `.detach()`。维度不一致由内部 1×1 adapter（`ofd_feature_loss`/`fitnets_hint_loss`）对齐，adapter 不落盘、训练期丢弃。

### `kd/wrapper.py`
```python
class TeacherCache(nn.Module):
    @classmethod
    def build(cls, teacher_model_path, teacher_state_dict, hook_names, dummy_input_shape, build_fn=None) -> 'TeacherCache'
        # importlib 加载 teacher model.py + load state_dict + eval/冻结 + 注册 hook
    @classmethod
    def load(cls, path) -> 'TeacherCache'
        # 读 teacher_cache.pt（含 teacher_model_path/state_dict/hook_names/dummy_input）→ 调 build
    def forward(self, x) -> tuple[Tensor, list[Tensor]]
        # teacher 常驻内存，forward(x)=teacher(x) + hook 抓 feature（teacher 仅训练期用，不导 ONNX）

class KDStudentWrapper(nn.Module):
    def __init__(self, student: nn.Module, hook_names: list[str]|None=None)
        # hook_names=None 时读 student.feature_hook_names()
    def forward(self, x) -> tuple[Tensor, list[Tensor]]   # (out, feats)
```
**teacher_cache.pt 格式**（由 `teacher_setup.py` 写，`TeacherCache.load` 读）：`{teacher_state_dict, hook_names, teacher_model_path, build_fn, dummy_input, feature_dims, latency_ms, accuracy, ...}`。

### `kd/compose.py`
```python
def build_kd_loss(user_loss_fn, kd_config: dict) -> Callable:
    """返回 kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch) -> Tensor
    = user_loss_fn(s_out, y) + Σ_kd weight(epoch) * kd_term(...)"""
```

### `kd/ema.py`
```python
class MeanTeacherEMA:
    def __init__(self, student, decay=0.999)
    def update(self, student)            # 影子权重 EMA 更新
    def forward(self, x) -> Tensor        # EMA 副本前向
```

## 4. 确定性脚本 CLI（`_kd_scripts/*.py`）

统一：`python3 <script> <args>`，结果写文件 + stdout 打 `KEY: value` 供 agent 节点解析；非零退出 = fail loud。

### `profile_onnx.py`
```
python3 profile_onnx.py --onnx <teacher.onnx> --out <profile_report.json> --topk 5 \
  [--device cpu] [--seed 0]
```
（P7：`--device` 默认 cpu——profiling 看算子耗时，CPU 确定性最好；NPU=Ascend 走 CANNExecutionProvider。）
→ `profile_report.json = {op_histogram: {Conv:.., MatMul:.., Transpose:.., Softmax:..},
                          hotspots: [{node, op_type, dur_us}], transpose_count, conv1d_count, ascend_hints:[...],
                          device, providers}`
stdout: `PROFILE_REPORT: <path>`

### `teacher_setup.py`（确定性部分；6 层编辑 + 训练由 kd-setup agent 节点先做）
```
python3 teacher_setup.py \
  --teacher_model_path <6层 model.py 绝对路径> \
  --teacher_ckpt <ckpt> --build_fn <fn> --dummy_input '<json>' \
  --eval_command "<用户的 test/eval cmd，测 teacher 精度>" \
  --proxy_dataset_spec '<json: 用来跑 teacher 缓存的 proxy 数据规格>' \
  --output_dir <dir> --opset 17 \
  --latency_provider "workflows/agents/_struct_scripts/latency_onnxrt.py::measure" \
  --device auto --seed 0 [--strict-accuracy]
```
（P7：加 `--device` / `--seed` / `--strict-accuracy`。teacher_accuracy 解析失败 → 默认 stderr WARN +
`teacher_accuracy_known=false`（下游 dB gap 不可信，图表须标）；`--strict-accuracy` 时 fail loud。）
→ load ckpt（冻结）→ 注册 hook → 跑 proxy 集 → `teacher_cache.pt`；导 ONNX → 测 latency；跑 eval_command → accuracy。
stdout: `TEACHER_LATENCY_MS:`, `TEACHER_ACCURACY:`, `TEACHER_ACCURACY_KNOWN:` (P7), `TEACHER_DB_BASELINE:`, `TEACHER_ONNX:`, `TEACHER_CACHE:`, `TEACHER_META:`

### `measure_student.py`
```
python3 measure_student.py \
  --student_model_path <path> --student_ckpt <ckpt> --build_fn <fn> --dummy_input '<json>' \
  --eval_command "<用户的 eval cmd>" --teacher_meta <teacher_meta.json> \
  --output_dir <dir> --opset 17 \
  --latency_provider "workflows/agents/_struct_scripts/latency_onnxrt.py::measure" \
  --device auto --seed 0
```
（P7：解开 `device="cpu"` 硬编码，`--device` 透传给 export_onnx + latency_provider；`--seed` 加复现种子。）
→ 导 student ONNX → 测 latency；跑 eval_command → accuracy；算 dB gap vs teacher。
stdout: `STUDENT_LATENCY_MS:`, `STUDENT_ACCURACY:`, `STUDENT_DB_GAP:`, `MET_ACCURACY:`(gap≤0.5), `MET_LATENCY:`(lat≤target), `STUDENT_ONNX:`

**candidate_eval 节点（P7）短训阶段**：不传 `--eval_command` / `--eval_dataset`，measure_student 只测 latency
（db_gap/met_accuracy 为占位，curator 在 loop 里**不用**它们，只看 proxy_mse + latency）。candidate_eval
自己根据 latency_ms 与 target 比较判 FAIL_latency（不训练），通过才跑 train_kd.py 短训 → proxy_mse。

### `pick_student.py`（Phase1 确定性选 student）
```
python3 pick_student.py --registry students/registry.json --round <N> --out <spec.json>
```
→ 取 `registry[round]`（**线性 sweep，不取模**），吐 SelectionSpec（phase=1，kd_config 用 registry 默认）。
stdout: `SELECTION_SPEC: <path>`。`round ≥ len(registry)` → 退出码 1 + stderr `PHASE1_EXHAUSTED`（告诉 hypothesizer 进 Phase2）。

## 5. train_kd.py adapter 契约（kd-setup agent 生成，每个项目一份）

读用户的 `train.py` → 生成 `train_kd.py`，**复用**用户 train 里的 loss/optimizer/scheduler/dataloader/train-loop，**只加** KD 包裹。固定 CLI：
```
python3 train_kd.py \
  --student_family <family> --student_cfg '<json>' \
  --kd_config '<json>' --teacher_cache <teacher_cache.pt> \
  --student_model_path <path> --build_fn <fn> \
  --epochs <短训epoch数> --out_ckpt <path> \
  [--user_train_import '<module.path>' --user_loss_fn '<name>']   # 从用户 train 脚本 import
```
→ 短训 student（teacher 冻结、teacher forward 走 TeacherCache）→ `out_ckpt`。
stdout: `STUDENT_CKPT:`, `KD_LOSS_FINAL:`, `KD_PROXY_MSE:`（soft-MSE-vs-teacher，当短训代理指标）

模板见 `train_adapter_template.py`。kd-setup agent 读用户 train.py 后把 import 路径/loss 名/dataloader 构造填进模板。

## 6. 节点 I/O（workflow 层，P7 精简后 6 节点）

| 节点 | kind | 关键输出字段 |
|---|---|---|
| setup | agent | output_dir / project_root / build_fn / dummy_input / teacher_cache / teacher_meta{latency,accuracy,accuracy_known,db_baseline,onnx} / snapshots_dir / worktree_root / ckpts_dir / ledger_path / champions_path / kb_cache_dir / profile_report_path / train_kd_path / kd_recipe_path |
| hypothesizer | agent | selection_spec(SelectionSpec json), family, phase, candidate_id, rationale |
| engineer | agent | candidate_id, student_model_path, snapshot_path |
| candidate_eval | agent | status (SUCCESS/FAIL_latency/FAIL_train/FAIL_export), latency_ms, met_latency, proxy_mse, kd_loss_final, student_ckpt, student_onnx |
| curator | agent | round, phase, continue_loop, route_finalize, exhausted, champion_id, champion_latency_ms, champion_db_gap |
| finalize | agent | final_ckpt, final_db_gap, final_latency_ms, met_target, loop_back(bool), final_report |

**回环路由**（curator）—— **简化门**：proxy_mse 只用于 champion ratchet 排序，**不参与 finalize 门**（真实精度门推迟到 finalize 全量训练）：
```
route_finalize = new_champion_this_round ∧ champion.met_latency ∧ (phase == 2)
exhausted      = (round ≥ max_rounds) ∧ (not route_finalize)
continue_loop  = (not route_finalize) ∧ (not exhausted)
```
- **P7 phase==2 门**：Phase1（registry sweep）只 ratchet champion、不送 finalize（先把固定 student 全扫一遍拿最优 Phase1 champion），进 Phase2 后才送 finalize 全量裁定，避免 round 0 烧 50 epochs。
- 每当诞生新的、时延达标的 champion（且 phase=2）→ 送 finalize 全量裁定
- `continue_loop=true` → 回 hypothesizer（phase 由 round vs registry 长度决定：1=sweep，2=发挥）
- finalize `met_target=true` → $end
- finalize `met_target=false`（loop_back=true）→ finalize 节点 append `finalized_failed_mark` → 该 champion 不再触发 finalize → curator `continue_loop=true, phase=2` → hypothesizer 换方向
- `max_rounds` 耗尽仍未达标 → `exhausted=true` → fail loud + best-effort 报告

## 7. Teacher 6 层 hint

kd-setup 节点提示：复制 `baseline_model.py`（model8 的 `SignalProcessingTransformer`）→ 把 `self.main = nn.Sequential(...)` 里 4 个 `SignalTransformerBlock` 改成 6 个（其余不动）→ 得 teacher model.py → 跑用户 teacher_train_command 从头训。这是一行结构改动，agent 在 setup 节点内完成，不另开 workflow 节点。
