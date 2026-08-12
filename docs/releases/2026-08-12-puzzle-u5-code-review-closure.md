# Phase U5 —— Puzzle code-review 闭环（1 BLOCKER + 2 MAJOR + 2 MINOR + 2 可选 + 1 残留 BLOCKER）

**日期**: 2026-08-12
**分支**: `puzzle-universal`
**Commit**: `2f6d938`
**前置**: U4 cleanup（`eee7125`）

## 背景

U4 cleanup 后对 puzzle-universal 做全面 code-review，发现 1 BLOCKER + 2 MAJOR + 2 MINOR + 2 可选 issue。本 phase 逐项修复，并经 code-reviewer 双轮复审（实现 + 测试覆盖），复审又抓出 #1 的残留 BLOCKER（`infeasible_reason` 字段漂移），一并闭环。

## 改动点

### #1 BLOCKER —— mip_select target-too-aggressive enum 漂移（E12 早警失效）

**问题**：`mip_select.py:232` 早警路径 emit `select_reason="target-too-aggressive"`，但 `puzzle.yaml` output_schema enum 只有 `[mip-optimal, infeasible, none]` → output_schema 校验 fail → node_failed → catch-all terminate_select_failed，E12 LAT 早警 intent 被 silently 击落。

**修法**（选 **Option A**，KISS/YAGNI；未选 Option B「新加 terminate_latency_too_aggressive 节点」因路由守卫已能兜底，加节点增 DAG 面积无增益）：
- `puzzle.yaml:199` enum 加 `target-too-aggressive`。
- description 加语义说明（target_latency > baseline/2 结构性不可达）。
- `mip_select.py:13` docstring select_reason 列表同步。
- `mip_select.py:219-223` 早警注释改掉「承诺 terminate_latency_too_aggressive 终态」的 stale 描述（该节点不存在）——改为「select_reason 字段标根因，terminate_select_failed 兜底」。

### #1 残留 BLOCKER —— `infeasible_reason` 字段不在 schema properties（code-reviewer 复审抓出）

**问题**：第一轮只加 enum，但 `mip_select.py:235` 早警 dict 还 emit 诊断字段 `infeasible_reason`，不在 pz_select output_schema 的 6 个 properties 内（`additionalProperties: false`）→ **仍会 schema 校验 fail**，原 BLOCKER 症状实质未消除。`tars validate` 的 `_check_output_schema_field_alignment` 是静态字段拼写校验，无法捕获「脚本 stdout 字段集 vs schema 运行期漂移」，故 static validate 过但运行期会炸。

**修法**：`puzzle.yaml` pz_select properties 加 `infeasible_reason`（`type: ["string","null"]`，optional 不进 required —— 仅 target-too-aggressive 携带，其余 reason 省略）。

**回归守卫**：`test_puzzle_yaml_pz_select_schema_covers_mip_select_early_warning`（新测，移至 test_puzzle_delta_review.py）锁两层契约：(a) enum ⊇ 脚本 emit 值；(b) 早警 emit 字段集 ⊆ schema properties。本测试能抓回归的两类漂移（enum 删 target-too-aggressive / properties 漏 infeasible_reason）。

### #2 MAJOR —— slot→kind 迁移残留（Step 0 Reuse-Check 必崩）

**问题**：实现已 kind-keyed（score.py / latency_table.py / mip_select.py emit `kind`），但文档 slot-keyed——含 pz_score/agent.md Step 0 Reuse-Check Python `r['slot']`（第二次 run KeyError）。

**修法**：全量 jsonl 字段名 `slot`→`kind`：pz_score/agent.md（含 Step 0 Python r['kind']）+ pz_select/agent.md + score.py docstring/assessment + latency_table.py docstring。prose「slot」指 Slot 对象概念保留。

**验证**：`grep -rn "r\['slot'\]" workflows/agents/` = 空；`{layer, slot,` = 空。

### #3 MAJOR —— agent.md 决策 ID 残留

**问题**：pz_build_library/agent.md E14 + pz_retrain/agent.md E19/E9/E24 + pz_report/agent.md D5 决策 ID（开发期 issue 编号）残留在 agent prompt body（受众分离契约违反）。

**修法**：删决策 ID 保留 intent 描述。pz_build_library 的两行按 reviewer 指定改法（`E14 calib 数据桥接` → `calib 数据桥接`；`缺——E14 calib 数据契约` → `缺——calib 数据契约（BLD teacher 须真实数据）`）。

**边界**：`_check_prompt_dev_residue` regex 不含 E\d+/D\d+（只查 §N.M / plan §N / [INB]\d / 源码路径 / examples 路径），故 tars validate 不拦——洁净靠人工受众翻转通读 + reviewer 复审。扩 regex 影响 ALL workflows，out of scope（单独 task）。

**验证**：`grep -rEn "E([1-9]|1[0-9]|2[0-5])|D[56]" workflows/agents/pz_*/agent.md` = 空。

### #4 MINOR —— no_op 非方 slot 收缩

**问题**：`is_candidate_valid_for_slot` 未拦非方 slot 的 no_op 候选 → 进 BLD/score 期 `puzzle_blocks.make_zero` factory raise 崩整链。

**修法**（选 **Option A** 直接 name 检查；未选 Option B「catalog 加 requires_square_dims 字段」——单候选 YAGNI，现有 catalog 字段 requires_ffn_struct/mask_aware 是 candidate 属性，in_dim==out_dim 是 slot 形状约束，不强行套 catalog 模式）：
- `puzzle_common.py:is_candidate_valid_for_slot` 加 `if name == "no_op" and slot.in_dim != slot.out_dim: return False`（passthrough 检查之后、cross-kind 之前）。
- `get_default_candidates` stale 注释更新（现状：is_valid 已上线收缩候选，非 factory 期崩整链）。

**测试**：`test_is_candidate_valid_for_slot_rejects_no_op_on_non_square_slot`（移至 test_puzzle_delta_review.py）—— 方 slot no_op valid / 非方 no_op rejected / 非方 identity 仍 valid（passthrough 铁律）/ 非方 ffn slot no_op 同样被拦（锁检查顺序，cross-kind 前 square 检查对所有 kind 生效）。

### #5 MINOR —— runtime-facing 决策 ID 清零

**问题**：脚本 raise/result["error"]/argparse help 文案含决策 ID（E1/E7/E12/E14/E22/E23/E24/D5/§16.4），runtime 涌回 LLM 污染 prompt（受众分离）。

**修法**：清 raise/error/help 文案为 descriptive。**保留**：module docstring SPEC 锚点（reviewer 追溯用）+ function/class docstring + inline code comments（runtime LLM 不读 script 源，只读 stdout/stderr）。

涉及文件：puzzle_common.py / measure_baseline.py / puzzle_blocks.py / mip_select.py / gate_report.py / gkd_retrain.py / build_selected.py / bld.py。gate_report.py report.md body 的 D5 前缀顺手清（acc_tol_kind 已 descriptive，D5 标签冗余）。

### #6（可选）—— build_student_from_arch docstring 澄清

`puzzle_common.py:build_student_from_arch` 入参 `selected_arch` 形态偏松（`selected_arch.get("selected_arch", selected_arch) if isinstance(...) else {}`）。docstring 明示三种合法形态：mip_select 结果 dict（`{"selected_arch": {...}}`）/ 裸架构 dict（`{layer:{kind:variant}}`）/ 非 dict → 空（全 passthrough）。unwrap 逻辑是 pre-existing（本 phase 未动行为），仅 doc 澄清。

### #7（可选）—— `_derive_slot_id` fail-loud

`search_space_io.py:_derive_slot_id` 的 fallback `d.get('kind', 'slot')` 中 `'slot'` 是 v1 已退役字段名，作 fallback 双重误导 → 改为缺 `kind` 时 raise ValueError（fail-loud，Rule 12）。

**已知边界（Rule 7 surface）**：caller `out["id"] = d.get("id") or _derive_slot_id(d)` 的 `or` 短路使「有 id 缺 kind」绕过本 raise；但下游 `to_block_map` 的 `kind=str(d["kind"])` KeyError 兜底，系统级 fail-loud 仍成立（仅错误从描述性 ValueError 降级为裸 KeyError）。重构到派生点 fail-loud 属 over-engineering（生产路径不可达：load 强制 id+kind 必填），保持现状。

## 验证

- `tars validate workflows/puzzle.yaml` = **0 errors / 0 warnings**（含 `_check_prompt_dev_residue` + `_check_subagents_md`）。
- `pytest tests/ -k puzzle` = **57 passed + 18 skipped**（skipped 全是 nas_agent env-gated + deepseek 余额 0，非回归）。比 U4 多 2 个新测（#4 + #1 契约守卫）。
- grep `r['slot']` / `{layer, slot,` / agent.md 决策 ID = 全空。
- #1 enum + 残留 + #4 逻辑 standalone 验证 + 新测 green。
- code-reviewer 双轮复审：实现 review 抓出 #1 残留 BLOCKER（已修）+ #7 边界（surface 保持）；测试覆盖 review 确认 #4/#5 COVERED，#1 加契约守卫后闭环。

## 偏差

- **#1 残留 BLOCKER 是第一轮修复未发现，由 code-reviewer 复审抓出**：原修只加 enum 未加 `infeasible_reason` property。教训：schema 修改不只看 enum 字段对齐，要核对脚本所有 emit 路径的完整字段集 vs `additionalProperties: false`。已加契约守卫测防回归。
- #6/#7 属可选，#6 仅 doc 澄清（unwrap 逻辑 pre-existing 未动），#7 按最小修法（raise 取代 fallback）。
