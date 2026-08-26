# 实施计划：kd-nas 候选生成重构（2026-08-03）

> 设计草稿：`docs/specs/kd-nas-candidate-gen-design-draft.md`。本计划已与用户对齐批准。

## 目标

student 候选从 flatten 产物两阶段派生（阶段一确定性缩放 + 阶段二 KB 模板合成），shape 跟用户真实输入；解决「KB 预置 spt_*.py 写死 [1,4,48,64,1] 导致全 FAIL_latency」+「候选与用户模型脱节」。训练评估链路契约不变。

## 改动清单

| # | 文件 | 改动 |
|---|---|---|
| 新增 | `workflows/agents/candidate-gen/agent.md` | 两阶段编排（阶段一调脚本 + 阶段二 LLM 合成）+ output_schema |
| 新增 | `workflows/agents/_kd_scripts/derive_lightweight.py` | 阶段一确定性派生（KNOBS 缩放组合 + teacher-gen wrapper 镜像 + validate_contract 校验） |
| 新增 | `workflows/agents/_kd_scripts/test_derive_lightweight.py` | 单测（KNOBS 组合枚举 / wrapper 生成 / validate_contract PASS / shape 逐字复制） |
| 改 | `workflows/kd-nas.yaml` | 新增 `candidate_gen` 节点；`flatten.routes` 分叉；setup `output_schema`/`outputs` `receiver_dir`→`candidates_dir`；删过时注释 |
| 改 | `workflows/agents/kd-setup/agent.md` | step1/step6 候选源改 `candidate_gen.output.candidates_dir`；output 字段重命名 |
| 改 | `workflows/agents/kd-gate/agent.md` | `{{ setup.output.receiver_dir }}` → `candidates_dir` |
| 复用 | `model-flatten/scripts/validate_contract.py` | 候选通用契约门（零改动） |

**脚本零改动**：`gate_all.py / pick_variant.py / tune_latency.py / train_pool.py / measure_student.py / distill_dispatch.py / kd_common.py / select_and_report.py`。

## 实施步骤

1. **derive_lightweight.py + 单测**（task #2）
   - 读 baseline 契约 KNOBS + DUMMY_INPUT；枚举缩放组合（单 knob 缩 min + 全缩，cap 8）
   - 生成 teacher-gen wrapper 镜像（委托 baseline.build_model，DUMMY_INPUT 逐字复制，省略 __main__）
   - 逐个过 `validate_contract.py`，PASS 才写入
   - 单测：fixture baseline 契约（2 KNOBS）→ 断言 wrapper 数 = K+1、shape 逐字相等、每个 PASS、`build_model(**mins)` 可 forward
2. **candidate-gen/agent.md**（task #3）
   - step1 跑 derive_lightweight.py（阶段一）
   - step2 LLM 读 KB spt_*.py 模板 + baseline，合成 N=4 standalone 变体，逐个 validate_contract PASS
   - output: candidates_dir + n_variants（0 → fail loud）
3. **接线 yaml + setup + gate**（task #4）
   - kd-nas.yaml：加 candidate_gen 节点 + flatten.routes 分叉 + setup.input 加 candidates_dir + 字段重命名
   - kd-setup/agent.md：step1 CANDIDATES_DIR 来源 + step6 + output 重命名
   - kd-gate/agent.md：引用重命名
4. **tars validate + code-reviewer**（task #5）
   - `tars validate kd-nas`（0 error）
   - 派 code-reviewer 查依赖铁律/契约一致性/fail-loud/DRY；修复全部反馈
5. **E2E + 收尾**（task #6）
   - E2E 真机（opencode + deepseek-v4-flash）：断言候选 shape=[1,4,48,32,1] 非 64、n_accepted≥1
   - commit（即改即 commit）+ release note + CHANGELOG + CURRENT

## 验证（成功判据）

- **核心**：`candidates_dir` 下变体 `DUMMY_INPUT.shape` == 用户真实 shape（非 64）
- gate `n_accepted ≥ 1`（shape 对了，时延不再系统性高估）
- train 跑完、select 产出 `final_report.md`
- ledger `variant_id` 含 `lw_`/`kb_` 前缀，无 `spt_`（确认候选来自派生）
- 单测全绿；`tars validate kd-nas` 0 error
