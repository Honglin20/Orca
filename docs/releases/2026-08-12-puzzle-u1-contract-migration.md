# Phase U1 — Puzzle 通用契约层迁移（Slot kind 化 + candidate catalog + dispatch 迁移）

**分支**：`puzzle-universal`
**Commit**：`50c79c4`
**日期**：2026-08-12
**SPEC**：[`docs/specs/puzzle-universal-design-draft.md`](../specs/puzzle-universal-design-draft.md) §4/§5/§15/§17（闭环 E1/E3/E4/E6/E7/E8/E15/E23）

## 做了什么

Phase U1 是 puzzle-universal 重构的**契约层迁移单元**——把 v1 的硬编码候选 registry
+ `slot_type` 二值标签，重构为 SPEC v2 的**声明式 candidate catalog + 开放 kind 标签**。
算法内核（BLD/MIP/GKD 的 loss/optimizer）零改动，只迁 dispatch/契约层。

### 1. Slot dataclass 迁移（`puzzle_common.py`）
- `slot_type: str`（"attention"|"ffn"）→ `kind: str`（开放标签 attention/ffn/conv/moe/custom，E3）
- 新增 5 字段：`return_arity`（E15）、`original_intermediate: int|None`（E7 ratio 基准）、
  `activation: str|None`（E23，ffn required）、`ffn_struct: str`（E6，standard/bypass/glu/dual）、
  `mask_load_bearing: bool`（E8）
- `parent_module_path` 保留脚本侧字段名（search_space.yaml 的 `path` 是 loader 别名）

### 2. candidate catalog（新文件）
- `workflows/agents/_puzzle_scripts/candidate_catalog.yaml` —— builtin 候选目录（取代硬编码
  `candidate_registry` dict）。每条目：name/kind(list)/source/factory(`module::func`)/
  params/align/trainable/mask_aware/requires_ffn_struct/description
- `workflows/agents/_puzzle_scripts/puzzle_blocks.py` —— 公开 `make_*` 工厂库（从 puzzle_common
  的私有 `_factory_*` 迁出并公开）+ `_VanillaMHSA`/`_ZeroBlock`/`_KwargPassthrough`/`_wrap`/
  `resolve_activation` + 公开 `ACTIVATION_CLASS_TO_NAME` 反向映射
- D4：conv/moe/custom 在 catalog 里只有 identity 适用（无 builtin，框架预留）
- E1：identity 必入 catalog（load_catalog 校验）+ 必入每 kind 候选列表（parse_block_candidates 校验）

### 3. catalog loader（`puzzle_common.py`）
- `CatalogEntry` dataclass + `load_catalog()`（读 YAML，builtin 用 `functools.partial` 绑定
  params 成 `factory(slot)`，再 `_wrap` 包 `_KwargPassthrough` 统一异构签名，E4）+ `get_candidate()`
- 依赖环切断：puzzle_blocks 运行时零 import puzzle_common（仅 TYPE_CHECKING Slot）；
  load_catalog 函数内 lazy import puzzle_blocks
- `parse_block_candidates` 从硬编码 `for key in ("attention","ffn")` 改为动态 kind-keyed dict

### 4. dispatch 迁移（所有 `slot.slot_type` → `slot.kind`）
- bld.py / score.py / latency_table.py / build_selected.py：`slot.slot_type` → `slot.kind`；
  `candidate_registry[variant]` tuple-unpack → `get_candidate(variant)` 的 CatalogEntry 属性
- mip_select.py：分组键 `(layer, slot_type)` → `(layer, kind)`；jsonl `"slot"` 字段 → `"kind"`；
  MIP 算法（pulp 求解器）零改动
- expand_model.py：Slot 构造 `slot_type=` → `kind=` + 追踪 ffn meta（`_infer_ffn_meta`：
  original_intermediate = 首个 Linear.out_features；activation 类→名）

### 5. E7/E23 修正（`puzzle_blocks.make_ffn`）
- E7：`intermediate = original_intermediate × ratio`（v1 bug 是 `in_dim × ratio`，对 d_ff=4×d_model
  的标准 FFN，ffn_50 从错误的 0.5×d_model 修正为正确的 2×d_model）
- E23：`activation is None → raise`；`original_intermediate is None → raise`

## 测试

新增 `tests/test_puzzle_catalog.py`（25 用例），覆盖：
- **E7 ratio 数学**（BLOCKER）：`make_ffn` intermediate = original_intermediate × ratio 断言
- **E23/E7 fail-loud**（BLOCKER）：activation/original_intermediate None → raise
- **load_catalog 全 fail-loud 分支**（MAJOR）：缺文件 / 非 list / 缺字段 / 未知 source /
  缺 identity / factory 不可解析 / 模块非 puzzle_blocks
- resolve_activation / get_candidate / BlockMap round-trip 新字段 / Slot 默认值 /
  _KwargPassthrough 剥 kwargs / catalog E4 partial 绑定

适配既有测试：Slot 构造 slot_type→kind + 新字段；jsonl "slot"→"kind"；
parse_block_candidates E1 语义；`test_score_preserves_parent_state` 改名诚实化（Rule 9）。

**结果**：44 puzzle 测试全过（19 既有 + 25 新）。

## 验收

- ✅ `tars validate workflows/puzzle.yaml` = 0 error / 0 warning
- ✅ 所有脚本 `py_compile` 通过 + `__main__` 在
- ✅ `test_puzzle_scripts_smoke` 过（新 schema 全链 expand→bld→score→mip→build→gkd→gate）
- ✅ grep `slot_type` 零业务残留（仅 docstring 历史说明）
- ✅ identity 在每 slot 候选的契约 + fail-loud 生效（parse + catalog 双层）

## 与计划的偏差（Rule 7 surface）

经 code-reviewer 两轮（实现 + 测试覆盖）review，以下项**有意识保留**，理由如下：

1. **no_op 对非方 slot 整链 raise（m2）**：reviewer 建议 bld 加 `continue + WARN` 优雅收缩。
   保留 fail-loud（exit 2）。理由：(a) 任务边界明确"不动 is_valid FFN 结构验证（U3 的 E6）"——
   候选收缩是 is_valid 职责；(b) 静默 continue 会掩盖配置错误（违反 Rule 12）。U3 的 is_valid
   上线后自动按 slot 形状收缩。已在 `get_default_candidates` docstring 标注 caveat。
2. **`_resolve_builtin_factory` 限定 puzzle_blocks 模块（m4）**：reviewer 指出轻微 OCP 摩擦。
   保留。理由：builtin factory 是框架资产，限定单模块可审计；用户扩展性走 source=user +
   `load_external_callable`（U2 落地），不受此限。
3. **E6/E8 is_valid 未消费（m1/m5）**：`CatalogEntry.requires_ffn_struct`/`mask_aware` +
   `Slot.ffn_struct`/`mask_load_bearing` 仅承载声明。SPEC §15 明确 is_valid 是 Phase U3 交付。
   已在字段加 `TODO(U3)` 注释。

## 文件清单（绝对路径）

**新增**：
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\puzzle_blocks.py`
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\candidate_catalog.yaml`
- `D:\Projects\Orca\tests\test_puzzle_catalog.py`

**修改**：
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\puzzle_common.py`（Slot + catalog loader + parse）
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\{bld,score,latency_table,build_selected,mip_select,expand_model}.py`（dispatch 迁移）
- `D:\Projects\Orca\tests\{test_puzzle_delta_review,test_puzzle_father_state,test_puzzle_scripts_smoke}.py`（适配）

## 下一阶段（Phase U2）

pz_expand 重构：LLM 判断（flat/manifest/search_space 生成 + kind 识别带确定性证据）+
`measure_baseline.py` 拆分 + 4 道 smoke（strict-load / forward-determinism / eval-stability /
per-slot identity allclose）+ block-map/search-space evaluator + manifest.yaml schema +
output_schema 扩 required（search_space_path/manifest_path，E10）+ evaluator fixture suite（E18）。
