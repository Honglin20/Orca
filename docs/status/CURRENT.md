# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle universal 重构（SPEC v2，分支 `puzzle-universal`）

**任务**：把 puzzle workflow 从"假设标准写法"重构为"任意 transformer 工程自适应"——SPEC v2 `docs/specs/puzzle-universal-design-draft.md`，分 Phase U0-U5。v1 已 PASS（tag `puzzle-v1-pre-universal` 保护，见 CHANGELOG）作对照。

### ✅ 已完成

- **Phase U0**：tag `puzzle-v1-pre-universal` 保护 + 新分支 `puzzle-universal`。
- **Phase U1（契约层迁移，`50c79c4`）**：Slot `slot_type`→`kind`（开放标签，E3）+ 5 新字段（return_arity/original_intermediate/activation/ffn_struct/mask_load_bearing）；新增 `candidate_catalog.yaml` + `puzzle_blocks.py`（公开 make_* 工厂库）取代硬编码 registry；`load_catalog`/`functools.partial`/`_wrap` 统一 factory(slot)（E4）；`make_ffn` 修正 E7（intermediate=original_intermediate×ratio）+ E23；bld/score/latency/build/mip/expand 全 dispatch 迁移，jsonl `"slot"`→`"kind"`，MIP 算法零改动；D4（conv/moe/custom 仅 identity）+ E1（identity 必入每 slot）。验收：`tars validate` 0/0 + 44 测试全过。
- **Phase U2a（pz_expand 重构主体 + target spike）**：target spike PASS（LLM 自适应产 target flat，strict-load 零 missing，0 fix-loop——Claude 代 deepseek，U4 验真）；新增 `measure_baseline.py`（4 道 fidelity smoke：strict-load/forward-determinism/eval-stability/per-slot identity allclose + 测 acc/latency + trace slot I/O 回填）；新增 `search_space_io.py`（yaml↔Slot 映射 + candidates 校验）；Slot 加 forward_arity（E2）；pz_expand/agent.md 全文重写为 LLM 自适应（Step 0-3 + manifest/search_space schema + kind 确定性证据）；puzzle.yaml output_schema 加 search_space_path/manifest_path 必填（E10）；11 新测试（4 smoke fail-loud 全覆盖）。验收：tars 0/0 + 44 测试全过。详见 release note。
- **Phase U2b（evaluator 审查层，`a308784`）**：新增 `block-map-evaluator` + `search-space-evaluator`（`subagents/puzzle/`，point-to-file 只读 judge，severity BLOCKER/MAJOR/MINOR，输出对齐 supernet-evaluator）；fixture suite 12 case（11 seeded error + clean baseline）+ 4 自包含 flat 变体 + 确定性生成器（`tests/e2e_puzzle/`）；recall AC 测试两层（deterministic 完整性 always runs 24 用例 + LLM recall env-gated，driver 支持 anthropic+opencode + runtime 故障 skip）；pz_expand Step 3.0 接触发点（measure_baseline 后、workflow-verifier 前）。验收：tars 0/0 + 24 完整性测试过；recall 层本环境无 key + deepseek 余额 0 skip，U4 复测真实数字。详见 release note。
- **Phase U3（下游迁移 + 算法增强，commit 待回填）**：bld calib 改真实数据（E14，`build_real_calib_loader` + required `--calib_loader_fn`）；`is_valid_ffn_prune` + `is_candidate_valid_for_slot`（cross-kind + E6 + E8）在 bld/score/build_selected 三处过滤；§16.4 全 identity 跨模型 allclose（build_selected）；gkd_retrain `_flatten_model_output`（E24 dict/list → raise）；gate_report D5 baseline-dependent ACC AC；mip_select E12 LAT 早警；measure_whole_model_latency DRY 抽到 puzzle_common；puzzle.yaml 退役 5 inputs（标 [DEPRECATED U3]）。Rule 7 surface：D5 SPEC formula 与示例内部矛盾→按示例+用户 intent 落地。验收：tars 0/0 + pytest 98 passed（17 新测试）+ code-reviewer 2🟡+4🟢 全修。详见 release note。

### ⏳ 未完成（后续 Phase）

- [ ] **Phase U4**：两项目 E2E 重跑（mnist_trf / target），target 不再靠手写 adapter；deepseek 专项 spike 复核；删 puzzle.yaml 退役的 5 个 deprecated inputs；删 expand_model.py v1 路径。
- [ ] **Phase U5**：code-reviewer 洁净审查闭环。

**必读**：SPEC v2 `docs/specs/puzzle-universal-design-draft.md`（§4 search_space schema / §5 catalog / §6 kind 识别 / §9 pz_expand 重构 / §15 迁移表）；v1 设计 `docs/specs/puzzle-design-draft.md`；`workflows/agents/_puzzle_scripts/`（puzzle_common.py Slot + load_catalog / puzzle_blocks.py / candidate_catalog.yaml）。

---

## 遗留（nas-supernet，跨任务未决）

- ✅ v3 retrain 拆分——ns3_retrain_script（纯生成）+ ns3_retrain（执行），策略定死二元（commit 待回填，见 CHANGELOG + release `2026-08-12-nas-supernet-v3-retrain-split.md`）。
- [ ] 真机 E2E(in-session headless `latency_unit: us` + 用户 script → 4 图 label=us / compare 真测量 / `subnet_structure.md` / A6 fail-loud)——属 test-agent 范围。
- ℹ️ v3 P0 已修（`0ca1b3b`）；Task 2 enum 已提交（`7b120ee`+`131b294`）；v2 P1/P1（`a57190b`）；S4a（`d768879`）；S4b SDD 三项已提交。详见各 release note + CHANGELOG。

---

> 历史任务记录见 `CHANGELOG.md`（索引）+ 各 `docs/releases/*` release note。本文件仅保留当前任务快照。
