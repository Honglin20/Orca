# Release: KD-NAS 重构为 Receiver KB 驱动的确定性蒸馏 sweep（2026-07-24）

## 背景

旧 `kd-nas` 是「搜索」workflow（Phase1 registry sweep + Phase2 LLM 自由变异 + finalize 全量裁定 + proxy_mse 排序）。用户要改成**确定性蒸馏 sweep**：model8 `.py` 变体放 `knowledge_base/families/receiver/`，workflow 遍历蒸馏；teacher(10层) 写死 repo 只当 KD 软标签源；精度基线用户给；时延超阈→最小缩量调参；完整训练（非 proxy）+ 实时图；跨 run 复用。

设计经 spec-review（CONDITIONAL-PASS，17 blocker + HIGH/SR/MED findings 全 fold）。计划见 `docs/plans/2026-07-24-kd-nas-distill-redesign.md`。

## 核心改动

### DAG：`setup → selector → distill → recorder → … → selector(all_done) → $end`
- 砍掉 hypothesizer/engineer/candidate_eval/curator/finalize 五节点 + search/Phase1/Phase2/proxy_mse 全套。
- **setup**（幂等）：测 baseline(4层) latency 参考线 + 校验/训 teacher(10层 t1/t2) + 适配 train_kd + 预检变体。跨 run 哈希校验跳过 teacher 重训。
- **selector**（gatekeeper）：`pick_variant` 取下一未蒸馏变体 + `tune_latency` 最小缩量调参。DRY 唯一「还有没有变体」判定点。
- **distill**：`distill_dispatch` 确定性门（noop|train，BLK-17）→ FAIL_latency 不训练 / ACCEPTED 复用 selector latency(HI-1) + 完整蒸馏训练（每-epoch render_chart 实时图）+ measure_student 测精度对比用户绝对基线。
- **recorder**：一致性断言(BLK-17) → 写 ckpt 验存在再 append ledger(BLK-11) → 推 viz → 回 selector。

### 角色 + 精度/时延语义
- baseline(4层)=时延参考；teacher(10层 t1/t2 交替)=KD 软标签源；students(KB 变体)=蒸馏候选。
- 精度基线 = 用户提供的**绝对值**（`accuracy_baseline`，方向由 kind 决定；可选 `accuracy_baseline_kind` 锁方向，SR3）。teacher 不再是精度参考 → 砍 teacher_accuracy/dB-gap 舞蹈。
- 时延 = 测 baseline 得参考线 + 用户指定 `target_latency_ms` 阈值；超阈→最小缩量（刚跨 target 即停，不过度缩）。

### 跨 run 复用（无引擎 `--reuse`，靠稳定 artifact 根 + 幂等护栏）
- 稳定 `kd_artifacts_dir`（默认 `<repo>/kd-nas-artifacts/`，+可覆盖 +.gitignore）持 teacher_cache/ledger/ckpts。
- done 谓词（`kd_common.is_variant_done`）：variant_sha256(BLK-12) + latency_provider_id(HI-12) + ckpt 存在(BLK-11) + target 校验；FAIL_latency 同 target 才算 done；SUCCESS target-monotonic(MED-4)。

## 关键脚本（新增/重写）

| 脚本 | 作用 |
|---|---|
| `kd_common.py` | 共享 helper：sha256/provider_id/read_ledger(fail-loud BLK-16)/is_variant_done/acquire_run_lock(BLK-13)/RANK |
| `pick_variant.py` | 确定性选变体（glob `_*.py` 排除 + done 谓词 + KNOBS 校验 BLK-1/2 + no_variants exit3 BLK-14） |
| `tune_latency.py` | 最小缩量调参（RANK 排序 + step<0 + 刚跨即停 + seed HI-2 + tune_cache HI-5 + median+std HI-13） |
| `distill_dispatch.py` | BLK-17 确定性门（noop|train） |
| `measure_student.py` | 绝对精度基线模式（方向 by kind）+ `--build_cfg` + `--skip_latency`(HI-1 复用 latency) + teacher_meta optional(HI-8) |
| `teacher_setup.py` | 瘦身（砍 accuracy/dB）+ teacher_model_hash/teacher_ckpt_sha256(HI-3) + `_dummy_shape` raise(BLK-4) |
| `train_adapter_template.py` | 按路径加载变体 + 每-epoch render_chart(U-4) + `--env_anchor` 自举(BLK-5) + seed |
| `viz_kd.py` | 新 ledger schema + sweep 散点(baseline/target/精度基线 参考线) + 表 + latency bar；删 champion/finalize |
| `export_onnx.py` | 加 `build_kwargs`（所有 latency 调优的前提，向后兼容 struct） |
| `teacher_model.py` | 10 层 t1/t2 交替 teacher（repo 写死） |

### KB 改造
- 删 `knowledge_base/families/receiver/*.md`（5 个 LLM slice）+ `index.json` 删 receiver 条目（KB 机制 LLM-prompt 驱动，运行时不读这些）。
- 新 `_model8_blocks.py`（共享积木）+ `spt_t1.py`/`spt_alt.py`（seed 变体）+ README（契约：build_model/KNOBS/DUMMY_INPUT）。

### 编译期护栏
- `orca/compile/validator.py` 加 `_check_required_inputs_no_default`（required+default 矛盾 → **warning**；latency_provider 真正护栏是 YAML `required:true`+无默认 + 运行时强制）。

## 测试
- `tests/workflows/test_struct_kd_p7.py`：kd 部分重写（4 节点 / latency_provider required / 新 viz_kd schema / 绝对基线方向 / CLI flags）；struct 部分不动。
- `tests/workflows/test_kd_redesign.py`（新）：BLK-8 最小缩量（贪心跳步实现会挂）/ BLK-17 门 / done 谓词边沿（target 变化重试、target-monotonic、sha/provider 不匹配重做）/ KNOBS 校验 / HI-11 agent.md field∈schema / teacher 10-block 交替。
- 全绿：compile(218) + workflows(含 kd 51) + e2e_redesign contract 全过；195 合并跑 0 失败。

## 验证状态
- ✅ `tars validate` 等价（load_workflow + validate_workflow）：4 节点、路由合法、Jinja 无未声明引用、latency_provider required+无默认、entry 可达 $end。
- ✅ 脚本 AST + `--help` + 端到端 smoke（pick_variant 谓词、tune_latency 缩量 + cache 复用、distill_dispatch、measure 绝对基线）。
- ✅ code-reviewer 自检：4 个 🔴（功能性 bug）+ 关键 🟡 已全修 + 回归守门测试补齐（见下）。
- ⏳ 真机 E2E（opencode+deepseek-v4-flash，GPU 机，2 seed 变体 + counter-shim 跨 run 复用断言）：待用户在 GPU 机执行。

## code-reviewer 闭环（🔴 全修 + 回归守门）
- 🔴 `train_adapter_template.py` ckpt 引用已删的 `args.student_family` → 改 `variant_id`；训练循环不再链入硬编码 shape 的 placeholder（BLK-4）。
- 🔴 `kd-setup/agent.md` 无条件 `: > ledger` 截断（跨 run 复用失效）→ create-if-absent 守卫；`$BASELINE`/`$PROJECT_ROOT` 显式赋值（PROJECT_ROOT 从 baseline 路径向上找 .git/pyproject.toml）。
- 🟡 tune_latency cache key 加 `provider_id` 维度（HI-12 一致）。
- 🟡 measure_student 删 `_compute_db_gap`/teacher-relative legacy + `--teacher_meta`/`--accuracy_gap_db` CLI（HI-8）。
- 🟡 kd-nas.yaml `outputs` 删 `recorder.output` 引用（HI-4 终态 recorder 不跑）。
- 🟡 distill agent.md：FAIL_train 跳过 measure（无 ckpt 可测）。
- 🟡 recorder agent.md：force_rerun upsert 实现（HI-9，按 variant_id+cfg_hash+run_id 删旧行再 append）。
- 回归测试：`test_train_adapter_no_student_family_regression` / `..._loop_no_placeholder_leak` / `test_kd_setup_ledger_not_truncated` / `test_acquire_run_lock_idempotent_and_rejects_other`。

**未修（🟢 可选，不阻塞）**：teacher_model.py 与 _model8_blocks.py 类定义重复（DRY，跨目录 import 需 sys.path 注入，留后续）；三份 `_ACC_PATTERNS`/`_load_measure` 未下沉（DRY nice-to-have）；`acquire_run_lock` 无自动 release（心跳锁 max_age=3600s 自愈，单写者假设文档化）。

## 测试
- `tests/workflows/test_struct_kd_p7.py`：kd 部分重写（4 节点 / latency_provider required / 新 viz_kd schema / 绝对基线方向 / CLI flags）；struct 部分不动。
- `tests/workflows/test_kd_redesign.py`（新）：BLK-8 最小缩量 / BLK-17 门 / done 谓词边沿 / KNOBS 校验 / HI-11 field∈schema / teacher 10-block / 🔴 回归守门。
- 全绿：compile + workflows(kd) + e2e_redesign contract **199 passed / 0 failed**。

## 铁律落实
- dummy_input 用户指定（禁硬编码 shape，BLK-4：`_dummy_shape` raise、删 `_common.py`）。
- latency 必用用户脚本（`latency_provider` 必填无默认，BLK-3/10）。
- 确定性路由（`all_done`/`tune_status` 由确定性脚本算，纯函数 router 求值；agent 不自定，LO-5）。
- FAIL_latency 确定性门（distill_dispatch + recorder 一致性断言，BLK-17）。
- 跨 run 单写者（`acquire_run_lock` 心跳锁，BLK-13）。
