# Phase SPEC — Puzzle Workflow 实现 (P1–P2)

> 契约文件: `workflows/puzzle.yaml`(已落,6 agent + 4 terminate,schema 已通过 `tars validate`)。
> 设计草稿: `docs/specs/puzzle-design-draft.md`(必读,含算法 + 节点流水 + 决策)。
> 参考模板: nas-supernet v1 的 `workflows/agents/ns_*/agent.md` + `ns_run_train/scripts/*` + `workflows/subagents/nas-supernet/*`。
> 库资产: `nas-agent/nas_agent/{blocks,latency,train,search}/*`(勘察见 design-draft §4)。

## 架构总决策(最重要)

**预写确定性脚本 + 薄 agent**。Puzzle 算法(bld/score/mip/gkd)做成**通用、参数化**的预写脚本,放 `workflows/agents/_puzzle_scripts/`;agent.md 只编排(跑脚本 → 监控 → 自愈 → emit JSON),**运行时不让 LLM 生成算法代码**。项目特异性由 `block_map.json`(pz_expand 产出)+ inputs(project_root/model_path/build_fn/eval_fn)参数化,脚本对任意 transformer 族模型通用。

## P1 — 契约层:6 个 agent.md + 复用 runner 脚本 + verifier 体

### P1.1 agent.md(`workflows/agents/pz_*/agent.md`)
严格仿 ns-supernet 的 agent.md 结构(frontmatter `description`+`tools`;body: ⚠唯一任务 / 资源锚点 / Path 铁律 / Lazy Loading / Required Inputs / Pipeline Memory / Workflow(Step 0 reuse-check + Step 1..N) / Validation / 输出 JSON)。逐节点职责:

| 节点 | tools | 职责(跑哪些预写脚本) |
|---|---|---|
| `pz_expand` | [bash,read,write,edit,grep,glob,task] | 跑 `expand_model.py --project_root --model_path --build_fn --build_cfg --eval_fn --eval_kind --latency_unit --latency_script_path --seed` → 产 `<base>_flat.py` + `block_map.json` + `baseline_metrics.json`;写 `project_manifest.md`;跑 workflow-verifier + memory-verifier(point-to-file) |
| `pz_build_library` | [bash,read,edit,grep,glob,task] | 生成 `bld.py`+`run_bld.sh`(由 `bld_template.py` 渲染或直接调预写 `_puzzle_scripts/bld.py`)→ detach 跑 → bounded-polling + 无上限自愈 → `block_library/*.pt`+`bld_summary.json`;复用 ns_run_train runner scripts(改名 BLD_*) |
| `pz_score` | [bash,read,edit,grep,glob,task] | 跑 `score.py`+`latency_table.py` → `scores.jsonl`+`latency_table.jsonl`;推 3 图(block_score_bar/latency_dist/score_vs_latency) |
| `pz_select` | [bash] | 跑 `mip_select.py --scores --latency-table --target-latency` 一次,echo stdout JSON。zero-LLM |
| `pz_retrain` | [bash,read,write,edit,grep,glob,task] | 跑 `build_selected.py`(实例化异构架构)+ 生成 `gkd_retrain.py`+`run_retrain.sh` → detach 跑 → bounded-polling + 自愈 → `final_model.pt`;完成推 compare_table |
| `pz_report` | [bash] | 跑 `gate_report.py --final-model --baseline-metrics --eval_fn --eval_kind --latency_unit --accuracy_tolerance` → 断言 AC + 写 final_report.md + 推 metrics_bar。zero-LLM |

### P1.2 复用 runner 脚本(长跑节点)
`ns_run_train/scripts/` 的通用训练 runner 直接复用(拷贝到 `pz_build_library/scripts/` 与 `pz_retrain/scripts/`,变量名 BLD/RETRAIN):`status.sh` / `health.sh` / `launch.sh` / `warmup_poll.sh` / `eta.py` / `update_status_md.sh` / `progress_watcher.py` / `monitor_until_done.sh` / `kill_train_group.sh` / `check_progress_contract.py` / `emit_result.py`。progress_watcher 的 `--label`/`--title` 参数化(puzzle/bld, puzzle/retrain)。

### P1.3 verifier 体(`workflows/subagents/puzzle/`)
仿 `subagents/nas-supernet/` 建: `workflow-verifier.md` + `memory-verifier.md` + `project-fidelity-verifier.md`(point-to-file,sentinel + frontmatter)。pz_expand 调前两个;executor 改 logic 层后调 fidelity。

### P1.4 workflow-checklists
`workflows/agents/pz_expand/references/workflow-checklists/puzzle.yaml.md` 等(供 workflow-verifier 用,severity + auto-fixable + check + verify)。

### P1.5 验收
`tars validate workflows/puzzle.yaml` = **0 error / 0 warning**。

## P2 — 算法层:预写确定性脚本(`workflows/agents/_puzzle_scripts/`)

全部 `python -m` 或直接 `python path --args`;fail-loud(exit 2);stdout 关键行 `KEY: value` + `RESULT_JSON:`;**禁 sys.path/PYTHONPATH 魔改**(sibling import)。复用 `nas_agent.latency.measure_module_latency`、`nas_agent.blocks.*`、`nas_agent.train.distillation.*`。

### P2.1 `puzzle_common.py`(共享)
- `load_flat_model(flat_path, build_fn, build_cfg)` → nn.Module
- `Slot` dataclass: `{layer_idx, slot_type[attention|ffn], in_dim, out_dim, num_heads, head_dim, source_class, parent_module_path}`
- `BlockMap` 读写 JSON
- `candidate_registry`: name → (factory_fn, applicable_slot_types)。默认集:
  - attention: identity / random_synthesizer(`ElasticRandomSynthesizerBlock`) / relu_attention / fnet(`ElasticFNetFourierMixerBlock`) / softs_star / vanilla
  - ffn: identity / ffn_75 / ffn_50 / linear / no_op
- `capture_parent_activations(model, block_map, calib_loader)` → per-slot (input_tensor, output_tensor) 缓存(喂 BLD teacher 信号)

### P2.2 `expand_model.py`
- import 用户 model_path → flatten(单文件化,仿 model-flatten skill 精神)
- AST/inspection 识别 sub-block:类名/继承命中 {MultiheadAttention/Attention/TransformerEncoderLayer 含 attn → attention slot;FeedForward/MLP/Linear-Act-Linear → ffn slot};记录 I/O shape(用 dummy_input trace)
- 测基线: `eval_fn`(用户)→ acc; `measure_module_latency` 或包装 `latency_script_path` → latency
- 输出 `block_map.json` + `baseline_metrics.json` + `project_manifest.md`
- exit 2 if 无任何 slot → pz_expand 据此判 model_type_supported=false

### P2.3 `bld.py`(Blockwise Local Distillation)
- 读 block_map + parent activations + flat_model
- 对每 (layer_idx, slot, variant):实例化候选块(维度对齐 in_dim/out_dim)→ 冻结 parent 为 teacher → **normalized MSE** `MSE(o_p,o_c)/MSE(o_p,0)` 蒸馏到收敛 → save `block_library/L<layer>_<slot>_<variant>.pt`
- Decoupled: attention/ffn 独立训(不解耦 attention×ffn 组合)
- per-variant 写 progress.jsonl `{"step":N,"metrics":{"loss":..,"layer":..,"variant":..}}`
- 输出 `bld_summary.json`(每 variant 最终 BLD loss)

### P2.4 `score.py`(replace-1-block)
- 读 block_library + flat_model + calib_loader + eval_kind
- 对每 (layer,slot,variant):把该 slot 替换成 variant(载块库权重),其余冻结父模型,calibration 上算 block-distance 分:
  - classification → `logits_kd_loss`(KL)vs 父 logits
  - embedding → hidden cosine distance `1 - cos(h_var, h_parent)`
  - regression → output MSE
- 输出 `scores.jsonl`: `{layer,slot,variant,score,valid}`(score 越大越好 = 距离越小,统一为 `-distance`)

### P2.5 `latency_table.py`
- 对每 (layer,slot,variant):`measure_module_latency` 单块(或 `export_and_measure_latency` ONNX)→ median
- 或包装 `latency_script_path`(`path::func`,ONNX 单文件契约)
- 输出 `latency_table.jsonl`: `{layer,slot,variant,latency_ms}`(按 latency_unit 标注,不换算)

### P2.6 `mip_select.py`(pulp grouped-knapsack)
```
max   Σ_layer Σ_variant  score[l,v] · x[l,v]
s.t.  Σ_latency[l,v]·x[l,v] ≤ target_latency
      Σ_variant x[l,v] = 1  ∀ layer
      x[l,v] ∈ {0,1}
```
- 每层分组(attention slot 组 + ffn slot 组各独立)
- 读 scores+latency jsonl,解 MIP,stdout 单行 JSON `{selected_arch, total_score, selected_latency, feasible, select_reason}`
- infeasible(预算太紧)→ feasible=false,select_reason=infeasible
- exit 2 if scores/latency 缺

### P2.7 `build_selected.py`
- 读 selected_arch + block_library + flat_model
- 实例化异构架构:逐层把 attention/ffn slot 换成选定 variant(载块库权重),identity 保留父权重
- 输出 `selected_model.pt`(完整 state_dict,可独立 eval)

### P2.8 `gkd_retrain.py`
- 读 selected_model + flat_model(teacher,冻结)+ 用户 train data
- 端到端 KD: `cosine_kd_loss`(hidden,逐层)+ `logits_kd_loss`(分类才有,`KDWeightScheduler` warmup)
- 写 progress.jsonl `{"step":N,"metrics":{"loss":..,"kd_cos":..,"acc":..}}`
- 输出 `final_model.pt`

### P2.9 `gate_report.py`
- 读 final_model + baseline_metrics + eval_fn
- 测 final acc + latency → `gate_result.json`: acc_delta=|final-baseline|, latency_ratio=final/baseline
- 断言 `acc_delta ≤ accuracy_tolerance` AND `latency_ratio ≤ 0.5` → gate_status pass/fail
- 写 `final_report.md`;推 baseline_vs_optimized metrics_bar(ACC/LAT)

### P2.10 验收
- 每个 `_puzzle_scripts/*.py` 有 `__main__` + `--help`;`python -m py_compile` 全过
- 一个 `tests/test_puzzle_scripts_smoke.py`:用 mnist_trf fixture,跑 expand→bld(2 variant)→score→mip→build→gkd(1 epoch)→gate 全链 dry-run,断言产物存在 + AC 字段类型正确。

## P3 — mnist_trf fixture
`tests/e2e_puzzle/fixtures/mnist_trf/`: `model.py`(patch-embed Conv2d 3x3 stride 1 + N×`TinyTransformerBlock` + pool Linear→10;`build_model(**cfg)`+`KNOBS={num_blocks,embed_dim,num_heads}`+`DUMMY_INPUT={"shape":[1,1,28,28]}`);`train.py`(CE+Adam);`eval.py`(`evaluate()`→top-1 acc,打印 `ACCURACY:`)。参考 `examples/kd-nas-demo/knowledge_base/families/receiver/_demo_blocks.py`。

## 实现 order
P1.1–P1.4 → P1.5 validate → P2 脚本 → P2 smoke → P3 fixture → P3 dry-run → target 适配 → in-session E2E 两项目 → code-reviewer 洁净审查 → 闭环。
