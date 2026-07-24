# KD-NAS 重构：Receiver KB 驱动的确定性蒸馏 sweep

> 计划日期 2026-07-24。spec-review verdict: **CONDITIONAL-PASS**，17 blocker + HIGH/SR/MED findings 全部 fold（见各节「✚」）。
> 权威副本同步在 `~/.claude/plans/`；本文件是项目内持久版本。

## Context

当前 `kd-nas` 是「搜索」workflow（Phase1 registry sweep + Phase2 LLM 变异 + finalize 裁定 + proxy_mse）。改造成**确定性蒸馏 sweep**：model8 `.py` 变体放 `knowledge_base/families/receiver/`，workflow 遍历蒸馏；teacher(10层) 写死 repo 只当 KD 软标签源；精度基线用户给；时延超阈→最小缩量调参；完整训练（非 proxy）+ 实时图；跨 run 复用。全栈在 GPU 机，`train_kd.py` 子进程直接 `render_chart`。

## 已确认决策（含 U-1..U-5）

| 维度 | 决策 |
|---|---|
| 角色 | baseline(4层)=时延参考；teacher(10层 t1/t2 交替)=KD 软标签源；students(KB 变体)=蒸馏候选 |
| 精度基线 | 用户给绝对值 `accuracy_baseline`；`accuracy_baseline_kind` 可选 [advanced] override（U-2/SR3） |
| 时延 | 测 baseline(4层) 参考线；`target_latency_ms` 用户指定；超阈→最小缩量调参 |
| latency 调参 | 默认 cfg 起，按 leverage 高→低、每次缩一档，刚跨 target 即停；耗尽→FAIL_latency |
| 精度不达标 | 记账后下一个（不重训） |
| 训练 | 完整蒸馏（非 proxy），无 finalize |
| 记忆 | 跨 run 复用（稳定 artifact 根 + 幂等护栏 + 哈希校验） |
| teacher | 10 层 `SignalTransformerBlock` 交替 t1/t2，余同 `baseline_model.py` |
| U-1 artifact 根 | 稳定默认 `<repo_root>/kd-nas-artifacts/`（编译期从 yaml 路径推）+ input 可覆盖 + .gitignore |
| U-3 FAIL_latency 门 | 确定性 `distill_dispatch.py` gate（BLK-17，强制） |
| U-4 实时曲线 | 每变体一张图（title=variant_id）；latency-vs-accuracy sweep 另一张单图 |
| U-5 force_rerun | 仅 variants（teacher/baseline 各自哈希正交） |

## 用户硬约束（铁律）

1. **dummy_input 用户指定**：变体/baseline `.py` 声明 `DUMMY_INPUT`，全程透传 export_onnx，禁硬编码 shape。✚BLK-4：清掉 `teacher_setup._dummy_shape` fallback（→raise）、删 `students/_common.py`、模板 placeholder 加 PLACEHOLDER 守卫。
2. **latency 必用用户脚本**：`latency_provider`(path::func) 必填、删 YAML 默认。✚BLK-3：不删 `latency_onnxrt.py`（struct 用 + test 钉）——只删 kd-nas.yaml 默认、`required:true`。✚BLK-10：编译期 validator 断言 required+无默认；argparse required=True；agent.md 无 fallback。

## 目标架构（DAG）

```
setup → selector → distill → recorder → selector → … → selector(all_done) → $end
```

- **setup**（幂等）：✚BLK-13 取 `kd_artifacts_dir/orca.lock`；测 baseline latency（median+std ✚HI-13）；✚BLK-6 baseline_model_path 须契约文件；校验 repo `teacher_model.py`；幂等护栏（✚HI-3 hash+ckpt 校验）；跑 teacher_train + teacher_setup(瘦身)；适配 train_kd（✚BLK-7 user_train_import/loss 可选 input + 持久化）；✚BLK-14 预检 ≥1 变体。
- **selector**：`pick_variant.py`（下一未蒸馏 / ALL_DONE / ✚BLK-14 no_variants exit3）→ `tune_latency.py`（最小缩量 ✚HI-2 seed ✚HI-5 cache ✚HI-13 median+std）。路由 `all_done`→$end（首条 when），否则→distill。
- **distill**：✚BLK-17 先 `distill_dispatch.py`（noop|train）；FAIL_latency→不训练出 FAIL_latency；ACCEPTED→✚HI-1 复用 selector latency、跑 train_kd（完整+实时图 ✚BLK-5 env_anchor）+ measure_student（绝对基线 ✚HI-8 teacher_meta optional）。
- **recorder**：✚BLK-17 断言 tune_status↔distill.status 一致；✚BLK-11 先写 ckpt 验存在再 append ledger；推 viz；→selector。

> 删 engineer；selector 唯一 gatekeeper（DRY）；FAIL_latency 并入 distill（避免 recorder 读陈旧 distill.output）。

**稳定 artifact 根**（U-1）：`<repo_root>/kd-nas-artifacts/`（+可覆盖 +.gitignore）。内含 teacher_cache/teacher_meta/teacher_ckpt/ledger/ckpts/baseline_latency/tune_cache/user_train/orca.lock。

## 脚本（含 BLK/HI 修复）

- ✚`export_onnx.py` 加 `build_kwargs: dict|None=None`（`factory(**kw) if kw else factory()`，向后兼容）。**latency 调优前提**。
- ✚`pick_variant.py`（新）：glob `families/receiver/*.py`（✚HI-10 非递归+stem 唯一）；预校验 build_model + ✚BLK-1/2 KNOBS（leverage∈{high,medium,low}、step<0）；done 谓词（✚BLK-16 坏行 raise）；stdout VARIANT_SPEC/ALL_DONE/✚BLK-14 no_variants(exit3)；--force_rerun。
- ✚`tune_latency.py`（新）：最小缩量（✚BLK-1 RANK、✚BLK-2 step<0、✚BLK-8 测验刚跨即停）；✚HI-2 seed；✚HI-5 tune_cache.json；✚HI-13 median+std+cudnn.benchmark=False；--max_measurements/--env_anchor/--latency_provider(required)/--device/--seed。
- ✚`distill_dispatch.py`（新，BLK-17）：--selector_output → DISTILL_ACTION: noop|train。
- ✚`measure_student.py`：--build_cfg；绝对基线 --accuracy_baseline(+--accuracy_baseline_kind ✚SR3)；✚HI-8 --teacher_meta optional、删 _compute_db_gap；保留 latency-only。
- ✚`teacher_setup.py` 瘦身：砍 accuracy/dB；✚HI-3 加 teacher_model_hash+teacher_ckpt_sha256；✚BLK-4 _dummy_shape raise；留 teacher_latency_ms(展示)。
- ✚`train_adapter_template.py`：_build_student 按路径加载、删 --student_family；每-epoch render_chart（✚U-4 label kd-distill-<variant_id>）；✚BLK-5 --env_anchor+自举；✚HI-2 seed；✚BLK-4 placeholder 守卫。
- ✚`viz_kd.py`：新 ledger schema；sweep 散点+baseline/target/精度基线 参考线；✚HI-7 删 champion；✚HI-15 CLI 加参考线 args、删 --final_*/--champions。

## KB 改造 + KNOBS 约定

删 `families/receiver/*.md`(5)；`index.json` 删 receiver（留 wireless_receiver/cnn/transformer）。新 receiver/*.py 变体自包含（不依赖 _common.py ✚BLK-4）：

```python
KNOBS = {"num_blocks":{"default":3,"min":1,"step":-1,"leverage":"high"},
         "embed_dim":{"default":16,"min":8,"step":-4,"leverage":"medium"}}  # step<0, leverage∈{high,medium,low}
BUILD_FN="build_model"; DUMMY_INPUT={"shape":[...],"dtype":"float32"}  # 用户指定
def build_model(**cfg)->nn.Module: ...
def feature_hook_names(self)->list[str]: ...  # 可选
```

seed 2 样例（spt_t1/spt_alt）。teacher 单独 `_kd_scripts/teacher_model.py`。✚BLK-6 baseline_model_path 也须契约文件。删 registry.json+students/*.py+_common.py+pick_student.py；profile_onnx kd 引用移除。

## Teacher artifact（现在写）

`_kd_scripts/teacher_model.py`：拷 baseline_model.py 的 SignalAttention1D/SignalFeedForward1D/SignalTransformerBlock；main=10 block 交替 t1/t2；build_model+BUILD_FN+DUMMY_INPUT+feature_hook_names()。

## Ledger + done 谓词（跨 run 安全）

行 schema：`{variant_id, variant_path, variant_sha256✚BLK-12, accepted_cfg, status, latency_ms_median✚HI-13, latency_ms_std, accuracy, accuracy_kind, met_latency, met_accuracy, ckpt, target_latency_ms✚MED-3<float>, accuracy_baseline, latency_provider_id✚HI-12, run_id, ts}`。✚HI-9 force_rerun 按 (variant_id,cfg_hash,run_id) upsert。

done(v) = 行 status∈{SUCCESS,FAIL_accuracy,FAIL_train} ∧ ckpt 存在✚BLK-11 ∧ variant_sha256 匹配✚BLK-12 ∧ latency_provider_id 匹配✚HI-12；或 FAIL_latency 行 ∧ target 匹配✚MED-3。✚MED-4 target-monotonic（任一 SUCCESS latency≤target→skip）。✚HI-13 ±5%/±2σ 等价。

## 实时图（U-4）

每变体一张 loss/mse 图（每 epoch 刷新）；sweep 单图（baseline 参考线+target 阈值+精度基线）。

## 输入→脚本→flag 接线矩阵（✚MED-1）

| input | 脚本 | flag | owner |
|---|---|---|---|
| latency_provider(必填) | tune/measure/teacher_setup/baseline | --latency_provider | setup/selector/distill |
| target_latency_ms | tune/pick_variant/viz | --target_latency_ms | selector/recorder |
| accuracy_baseline | measure/viz | --accuracy_baseline | distill/recorder |
| accuracy_baseline_kind(可选) | measure/viz | --accuracy_baseline_kind | distill/recorder |
| full_epochs | train_kd | --epochs | distill |
| latency_tune_budget | tune | --max_measurements | selector |
| kd_force_rerun | pick_variant | --force_rerun | selector |
| kd_artifacts_dir(默认+覆盖) | pick/setup/recorder(lock) | 位置/--artifacts_dir | all |
| baseline_model_path | baseline_measure | --model_path | setup |
| user_train_import/user_loss_fn(可选) | train_kd | --user_train_import/--user_loss_fn | setup→distill |
| device/seed | 全 latency+train | --device/--seed | all |

## 文件改动清单

teacher NEW `_kd_scripts/teacher_model.py` · KB DEL `families/receiver/*.md`(5) · KB NEW `families/receiver/{spt_t1,spt_alt}.py`+README · KB MOD `index.json`(删 receiver) · YAML REWRITE `kd-nas.yaml`(✚HI-4 outputs.result=ledger_path) · agent REWRITE `kd-setup` · agent NEW `kd-selector/kd-distill/kd-recorder` · agent DEL `kd-hypothesizer/kd-engineer/kd-curator` · script NEW `pick_variant/tune_latency/distill_dispatch` · script MOD `measure_student/teacher_setup/train_adapter_template/viz_kd/export_onnx` · script DEL `pick_student/students/*` · doc REWRITE✚HI-6 `CONTRACTS.md` · test REWRITE `test_struct_kd_p7` · test NEW `test_kd_redesign` · test AUDIT✚SR1 `tests/e2e_redesign/` · repo ADD `.gitignore` kd-nas-artifacts/。

## 风险（实现盯）

✚BLK-10 编译期 latency_provider validator · ✚HI-11 agent.md field∈output_schema 测试 · ✚BLK-4 grep 无 [1,4,48,64,1] 残留 · ✚BLK-5 train_kd --env_anchor · ✚HI-4 outputs 不引 recorder.output · BLK-13 lock 兜底并发 · 路由全布尔 when: 确定性脚本算+纯函数 router（✚LO-5 可验证）。

## 实现顺序

1. 地基：teacher_model.py + KB 改造 + export_onnx.build_kwargs + .gitignore + 编译期 latency_provider validator。
2. 脚本：pick_variant/tune_latency/distill_dispatch/measure_student/teacher_setup/train_kd/viz_kd。
3. workflow：kd-nas.yaml + 4 agent.md + 删旧 3 + CONTRACTS.md。
4. 测试：改 test_struct_kd_p7 + 新 test_kd_redesign + e2e_redesign audit。
5. 状态：release note + CHANGELOG + CURRENT。

## 验证

1. 单测：pick_variant 谓词/✚BLK-16 坏行/✚BLK-14 no_variants/✚HI-10 stem 撞；✚BLK-8 tune 最小缩量（贪心跳步实现挂）；✚HI-2 seed；measure 绝对基线方向/✚SR3 kind override；export_onnx build_kwargs；teacher shape；viz 参考线。
2. 脚本 smoke：✚MED-2 test_cli_flags_exposed 加 pick_variant/tune_latency；tune 端到端 mock。
3. `tars validate`：编译+Jinja+路由+✚BLK-10 latency_provider required+✚HI-14 selector.output={}→RouteError。
4. DAG dry-run：`_yaml_nodes`=`[setup,selector,distill,recorder]`；✚SR2 全 cycle（2-3 变体→ledger N 行、selector N+1、all_done→$end）。
5. E2E（opencode+deepseek-v4-flash）：2 seed 变体+dummy train/eval+✚BLK-9 counter-shim；全 cycle+实时图；第二 run_id 同 kd_artifacts_dir 断言 teacher/variant count 不增→跨 run 复用。
6. 架构自检：code-reviewer 验单 tape/确定性路由/无反向依赖/fail loud/DRY/✚BLK-17 dispatch 一致性。
