# Release Note：kd-nas 串行迭代重写（2026-08-03）

> SPEC：`docs/specs/kd-nas-serial-iteration-rework.md` (v3)
> Plan：`docs/plans/2026-08-03-kd-nas-candidate-gen-rework.md`（注：plan 是早期批量版，**以 SPEC v3 为准**——串行化覆盖 plan）
> 上一版：批量并发（gate_all → train_pool → select）；本版完全重写为串行迭代。

## 干了什么

把 `kd-nas` 从批量并发模式（gate 一次性 gate 全部 KB 候选 → 并发蒸馏池 → select）
重写为**串行迭代 KD 蒸馏**：每轮 1 个 student → KD 蒸馏 → 测 latency+accuracy → 决策。
student 由结构变换派生（首轮固定规则缩1层+FFN→pointwise；迭代轮 KB+perf 驱动），
shape 跟用户真实输入。teacher 用用户默认 lr/epochs（从 user_train_script 提取，非硬编码）。
全程 metrics 推 web（每节点 viz_kd_stage sidecar）。终止：latency≤target ∧ accuracy 达 baseline
（min-latency ratchet + FIFO tiebreak） / max_rounds。

## 解决了什么问题

| 旧版（批量并发）问题 | 新版（串行迭代）解 |
|---|---|
| 无迭代回路（一次性 gate+train，不学习） | decide 节点 back-route → gen_student 下一轮 KB+perf 驱动 |
| student 候选与用户模型脱节（KB 预置 shape 写死） | gen_student 从 flatten baseline 派生，DUMMY_INPUT 字节级校验防漂移 |
| teacher 参数自定（hardcoded epochs=1 lr=1e-3） | kd-train-script Step 4 grep user_train_script 提 lr/epochs |
| 无 web 推送 | 每节点 viz_kd_stage + metrics_tail sidecar（baseline/teacher/student/distill/decide/final） |
| 6 节点并发蒸馏池（VRAM/worker 管理 + device_plan 复杂） | 串行 1 个 student/轮（SPEC §3.1 串行化决策） |
| 失败路径模糊 | 显式 catch 协议（业务失败 vs 系统失败，SPEC §15） |

## DAG（10 节点）

```
flatten → setup → gen_teacher → gen_train_script → train_script_verify → train_teacher
                                                                       ↓
          ┌── gen_student → distill → decide ───────────────────────┐
          │                                                           │ when: decide.output.continue_loop
          └───────────────────────────────────────────────────────────┘
                              ↓ 兜底（continue_loop=false）
                           finalize → $end
```

- **前置 6 节点**：flatten / setup / gen_teacher / gen_train_script / train_script_verify / train_teacher
- **循环体 3 节点 × max_rounds**：gen_student / distill / decide（continue_loop=true 时回 gen_student）
- **收尾 1**：finalize（champion=baseline 兜底 + eval+ONNX+latency + final_report.md）
- **MaxIterations** = `3 × max_rounds + 7`；默认 max_rounds=10 → 37 visits，引擎兜底 100 安全。

## 新写组件

| 组件 | 路径 | 职责 |
|---|---|---|
| kd_reducer.py | `_kd_scripts/` | KD 专版决策 reducer（min-latency ratchet + FIFO tiebreak N12，非复用 struct ledger_reducer——schema 与排序键不同） |
| viz_kd_stage.py | `_kd_scripts/` | 每节点 web push sidecar，按 --stage 派发 7 张图，_main 兜底永远 emit 合法 JSON |
| metrics_tail.py | `_kd_scripts/` | SPEC §9 可配置模板 metrics 摘取（regex named group），无模板走默认 loss |
| finalize_kd.py | `_kd_scripts/` | finalize 确定性后端（champion=baseline 兜底 + eval+ONNX+latency + final_report.md），独立脚本规避 yaml literal block 多行 python 缩进陷阱 |
| train-script-verify/agent.md | `agents/` | grep mode 函数 + 用户函数搬入 + live push helpers + micro eval |
| train-teacher/agent.md | `agents/` | 独立训 teacher（用户默认 lr/epochs + --out_ckpt + --env_anchor + 幂等 + metrics_tail） |
| gen-student/agent.md | `agents/` | 首轮固定规则缩1层+FFN→pointwise / 迭代轮 KB+perf + DUMMY_INPUT 字节级校验 + validate 3-strike catch FAIL_build |
| distill/agent.md | `agents/` | tune→FAIL_latency 跳训练→distill+--kd_config recipe→eval，catch FAIL_train，N21 status 映射 |
| decide/agent.md | `agents/` | 调 kd_reducer.py + dumb copy viz_kd_stage --stage decide stdout |

## 改动组件

- `kd-setup/agent.md`：精简（删 step2-6 baseline 检查移 flatten / teacher 训练移 train_teacher），
  保留 step1 路径+lock + step7 GPU 探测；加 baseline champion seed + 全下游路径字段。
- `model-flatten/agent.md` + `teacher-gen/agent.md`：扩展 output_schema 加 viz_status，末尾 sidecar
  调 viz_kd_stage --stage baseline/teacher。
- `kd-train-script/agent.md`：加 Step 4 提取 teacher_default_lr/epochs（grep argparse default/赋值，
  提取不到 fail loud，非硬编码 1/2）。
- `workflows/kd-nas.yaml`：重写（10 节点串行 DAG + finalize inline prompt）。

## 废弃

- `_kd_scripts/derive_lightweight.py` + `tests/workflows/test_derive_lightweight.py`（批量调参路线）。
- 旧 `kd-nas.yaml` 批量节点（gate_all/train_pool/pick_variant 在串行版不接线）—— `_kd_scripts/`
  下的 gate_all.py / train_pool.py / measure_student.py / pick_variant.py / distill_dispatch.py /
  select_and_report.py 文件保留供回滚，串行 yaml 不引用。
- `tests/workflows/test_kd_redesign.py` 中 11 个针对旧 yaml 结构的测试标 `pytest.mark.skip`
  （`obsolete after 2026-08-03 kd-nas serial rework`）；其余 73 个仍绿（脚本逻辑不变量）。

## 验证

- `tars validate workflows/kd-nas.yaml` → 0 error。
- 43 单测全绿（新增：test_kd_reducer 16 + test_viz_kd_stage_metrics_tail 17 + test_finalize_kd 10）。
- 73 旧 kd_redesign 测试通过（11 obsolete 跳过）。
- code-reviewer 一轮反馈闭环：2 FATAL（F1 train-teacher $BASELINE 未赋值 / F2 FAIL_build → distill 必崩）
  + 5 MAJOR（M1 accepted_cfg schema / M2 跨 bash 变量丢失 / M3 heredoc JSON 注入 / M4 /tmp 固定路径 / M5 死代码）
  + 2 MINOR（m1 死代码 / m3 regex 锚定）全修。

## 偏离 SPEC 处

无偏离。SPEC v3 是契约，逐字实现命令 flag + catch 协议 + 字段。

## 待用户跑（本环境跑不了）

E2E 真机（task #6）：需 GPU + opencode + deepseek-v4-flash，用一个 baseline 不达标的 fixture
（target_latency=baseline×0.7 / accuracy_baseline=baseline+margin）强制 ≥2 轮 distill→decide 循环 →
finalize；验收 decide 在 max_rounds 耗尽时正确终止。

## Commit SHAs

- `b3c3c91` feat(kd-nas): 串行迭代 reducer + viz sidecar（SPEC §6.9/§8/§9）
- `aa6e5d7` feat(kd-nas): 串行迭代 DAG + 5 新 agent + setup 精简（SPEC §5/§6）
- `a230ebe` test(finalize_kd): 10 单测覆盖 SPEC §6.10 + N10/N19
- `d03e4c9` fix(kd-nas): code-reviewer 反馈闭环（2 FATAL + 5 MAJOR + 2 MINOR）
