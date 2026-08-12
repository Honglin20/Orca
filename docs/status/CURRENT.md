# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前：Puzzle universal 重构（SPEC v2，分支 `puzzle-universal`）

**任务**：把 puzzle workflow 从"假设标准写法"重构为"任意 transformer 工程自适应"——SPEC v2 `docs/specs/puzzle-universal-design-draft.md`，分 Phase U0-U5。v1 已 PASS（tag `puzzle-v1-pre-universal` 保护，见 CHANGELOG）作对照。

### ✅ 已完成

- **Phase U0**：tag `puzzle-v1-pre-universal` 保护 + 新分支 `puzzle-universal`。
- **Phase U1（契约层迁移，`50c79c4`）**：Slot `slot_type`→`kind`（开放标签，E3）+ 5 新字段（return_arity/original_intermediate/activation/ffn_struct/mask_load_bearing）；新增 `candidate_catalog.yaml` + `puzzle_blocks.py`（公开 make_* 工厂库）取代硬编码 registry；`load_catalog`/`functools.partial`/`_wrap` 统一 factory(slot)（E4）；`make_ffn` 修正 E7（intermediate=original_intermediate×ratio）+ E23；bld/score/latency/build/mip/expand 全 dispatch 迁移，jsonl `"slot"`→`"kind"`，MIP 算法零改动；D4（conv/moe/custom 仅 identity）+ E1（identity 必入每 slot）。验收：`tars validate` 0/0 + 44 测试全过。

### ⏳ 未完成（后续 Phase）

- [ ] **Phase U2**：pz_expand 重构——LLM 判断（flat/manifest/search_space 生成 + kind 识别带确定性证据，删 regex）+ `measure_baseline.py` 拆分 + 4 道 smoke（strict-load/forward-determinism/eval-stability/per-slot identity allclose）+ block-map/search-space evaluator（`subagents/puzzle/`）+ manifest.yaml schema + output_schema 扩 required（search_space_path/manifest_path，E10）+ evaluator fixture suite（E18）。
- [ ] **Phase U3**：下游迁移——bld calib 改真实数据（E14）；is_valid_ffn_prune（E6）+ mask_load_bearing is_valid（E8）；gkd argparse 5 args 改 manifest-discovered；identity allclose 支持。
- [ ] **Phase U4**：两项目 E2E 重跑（mnist_trf / target），target 不再靠手写 adapter。
- [ ] **Phase U5**：code-reviewer 洁净审查闭环。

**必读**：SPEC v2 `docs/specs/puzzle-universal-design-draft.md`（§4 search_space schema / §5 catalog / §6 kind 识别 / §9 pz_expand 重构 / §15 迁移表）；v1 设计 `docs/specs/puzzle-design-draft.md`；`workflows/agents/_puzzle_scripts/`（puzzle_common.py Slot + load_catalog / puzzle_blocks.py / candidate_catalog.yaml）。

---

## 遗留（nas-supernet，跨任务未决）

- [ ] 真机 E2E(in-session headless `latency_unit: us` + 用户 script → 4 图 label=us / compare 真测量 / `subnet_structure.md` / A6 fail-loud)——属 test-agent 范围。
- ℹ️ v3 P0 已修（`0ca1b3b`）；Task 2 enum 已提交（`7b120ee`+`131b294`）；v2 P1/P1（`a57190b`）；S4a（`d768879`）；S4b SDD 三项已提交。详见各 release note + CHANGELOG。

---

> 历史任务记录见 `CHANGELOG.md`（索引）+ 各 `docs/releases/*` release note。本文件仅保留当前任务快照。
