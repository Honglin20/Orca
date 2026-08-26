# Release: Puzzle pz_materialize 节点 —— optimized_flat 自包含最优架构交付件

**日期**：2026-08-13　**分支**：`puzzle-universal`　**commit**：（待回填）

## 动机

用户需求：Puzzle 寻优完成后，产出一份**与原始 `<base>_flat.py 同构、自包含、可独立跑通**的最优模型文件
——选中的 slot 已替换为新 variant 块，权重载入即用，包含模型全部信息。

现状隐患：最终产物只有 `final_model.pt`（state_dict），重建需 flat + puzzle_blocks + block_map +
selected_arch 多件耦合；且 GKD「训练时重建 student（build_student_from_arch）」与「交付件重建」是两条
路径，存在分叉风险。

## 方案：正确性由构造保证

新增确定性节点 **`pz_materialize`**（插在 pz_select 与 pz_retrain 之间），产 `<base>_optimized_flat.py`。
**optimized_flat 成为 GKD / gate / 交付的唯一执行基底**——被训练、被测、被交付的是同一文件，消除重建分叉。

```
pz_select → pz_materialize → pz_retrain(GKD) → pz_report(gate)
              │ build_selected 合成 selected_model.pt（父⊕BLD，上移自 pz_retrain）
              │ materialize_optimized.py 装配 optimized_flat.py
              │ 自检：key 对齐 + in-process forward + workflow-verifier
              └ 失败 → terminate_materialize_failed
```

### 权重链（单次 strict load）

optimized_flat.build_model() 权重无关；`selected_model.pt`（= 父⊕BLD，build_selected 合成）已统一两源。
全生命周期只一次 strict `load_state_dict`：流水线内 GKD 起点载 `selected_model.pt`，交付态载 `final_model.pt`。

## 实现（脚本 + LLM 分工）

- **`materialize_optimized.py`（新，确定性装配 + 自检）**：
  - flat 架构类逐字照抄（build_fn → `_puzzle_flat_build` 重命名，flat `__main__` 抽出挪末尾——rewire 后
    `build_model()` 调用落到 wrapper，自动测最优模型）。
  - 选中 variant 块源**整模块 AST 内联**：puzzle_blocks 的 wrapper helper（`_KwargPassthrough` 等）按名 AST
    抽取；用到的 `nas_agent.blocks.*` + `primitive_blocks` 抽 top-level ClassDef/FunctionDef/常量 Assign
    （丢模块级可执行 demo 代码 + import，`__future__` 提顶，跨模块去重）。
  - `_build_variant` dispatcher 精确镜像 `puzzle_blocks.make_*` + catalog 包装语义（`_KwargPassthrough` /
    `_MaskPassthrough`）→ state_dict key 与 `build_student_from_arch` 对齐。
  - `build_model()` = `_puzzle_flat_build()` + 确定性 setattr 换 slot；`load_model(ckpt)` strict 载入。
  - 自检：`key_alignment_passed`（build_model state_dict 逐 key+shape 对齐 `build_student_from_arch` live
    reference）+ `forward_selfcheck_passed`（in-process `adapters.forward_model` 鲁棒任意 forward convention）。
  - `--check-only`：agent 手动 edit optimized_flat.py 后只重跑自检、不重新装配（self-heal 回路）。
- **`pz_materialize/agent.md`（新节点）**：跑 build_selected + materialize + workflow-verifier；自检失败仅 edit
  optimized_flat.py（白名单）补全内联边界 case → `--check-only` 重验（≤2 轮）；预写脚本 bug → fail loud。
- **`puzzle_common.load_optimized_flat`（新 helper）**：gkd/gate 共用，import optimized_flat + 校验 build_model。
- **`gkd_retrain.py` / `gate_report.py`（改，严格只走 optimized_flat）**：student 构造从 `build_student_from_arch`
  → `optimized_flat.build_model()` + strict 载 ckpt；删旧 `--flat_model`/`--build_fn`/`--build_cfg`/`--block_map`/
  `--block_library` 入参，加 `--optimized_flat`（required）。
- **`puzzle.yaml`（改）**：插 pz_materialize 节点 + output_schema；pz_select 路由 → pz_materialize；新增
  `terminate_materialize_failed`；outputs 增 `optimized_flat_path`；description 更新（7 agent + 6 terminate）。
- **`pz_retrain/agent.md`（改）**：移除内部 build_selected 调用（上移 materialize）；launcher 只调 gkd_retrain
  `--optimized_flat`；禁碰清单增 optimized_flat.py / selected_model.pt。
- **`pz_report/agent.md`（改）**：launcher 切 `--optimized_flat`。

## 死不变量（fail loud）

1. **state_dict key 逐 key+shape 对齐** optimized_flat.build_model() vs `build_student_from_arch`（= build_selected
   产 selected_model.pt 的同一逻辑）。过此门 → final_model strict 可载（key 对齐 ⇒ load 必成功）。
2. **forward 自检**：build_model + adapters.forward_model → 有限 tensor。
3. **架构/slot 合规**（workflow-verifier）：替换 slot 运行时类 = variant；identity 零侵入；非-slot 参数不变。
4. **幂等**：两次 materialize 产出字节级一致（确定性装配）。
5. **自包含**：optimized_flat 仅依赖 torch + stdlib（无 puzzle_blocks / nas_agent import）。

## 验收

- `tars validate workflows/puzzle.yaml` 0 error。
- pytest：`test_puzzle_materialize.py`（4 项：key 对齐+自包含 / 幂等 / identity allclose / dispatcher 全 variant key 对齐）全过；
  `test_puzzle_scripts_smoke.py::test_puzzle_full_chain_cpu`（全链含 materialize）过；
  `test_puzzle_u3_migration.py` gkd/gate 测试同步新 args 后 29 passed；`test_puzzle_delta_review` /
  `test_puzzle_father_state` 无回归。
- target playground 产物实测：materialize 产 optimized_flat（fnet+linear 内联），key 对齐 build_student_from_arch，
  standalone forward（子进程 exit 0，项目真实 4 输入）通过，`load_model` strict 载 fresh selected_model 通过，幂等 md5 一致。

## code-reviewer 闭环（审查 → 修复）

必修（全修）：
- **BLOCKER-1**：forward_selfcheck 契约（subprocess standalone）vs 实现（in-process）不一致 → 改回真子进程
  `python optimized_flat.py`（干净子进程 exit 0 为权威 forward_selfcheck），in-process 降级 `forward_inprocess_detail`
  诊断；optimized_flat **始终**发 `__main__`（flat 有则用其项目真实 forward 签名，无则发通用单输入 fallback）。
- **MAJOR-2**：build_cfg 未透传 optimized_flat.build_model() → bake 成模块级 `_BUILD_CFG`，build_model 零参用之
  （消除 zero-arg 默认 vs 训练 cfg 不一致致 strict load 失败）。
- **MAJOR-1**：dispatcher 10 variant 只测 2 → 新增 `test_dispatcher_all_variants_match_puzzle_blocks_keys`
  （exec 生成的 dispatcher，逐 variant 比 state_dict keys vs catalog factory，不依赖 e2e）。
- MINOR-3：`_load_optimized_module` 复用 `puzzle_common.load_optimized_flat`（自检与下游同加载路径）。
- MINOR-4：脚本 docstring 去 `docs/plans/<date>` 日期锚点。

延后（当前 verified scope 内不构成 bug，记此处待后续）：
- MAJOR-3：`_inline_module_source` 的 `needed_imports` 硬编码（blocks 实测纯 torch + typing.Any，自检兜底）→ 未来按 AST 扫引用名按需生成 import。
- MAJOR-4：`_rewire_flat_build_fn` 全文正则替换（现 flats 的 build_fn 名出现在字符串 lookup key 的概率低）→ 未来改 AST NodeTransformer 节点级改名。
- MAJOR-5：dispatcher 与 `puzzle_blocks.make_*` 的数据化 spec 驱动（消除三处手工同步）→ MAJOR-1 测试已兜底漂移。
- MAJOR-6：`puzzle.yaml` description 单行过长（历史，本次仅顺延）→ 未来拆短 + 引用 spec。

## 交付形态

`optimized_flat.py` + `final_model.pt`：用户 `from optimized_flat import load_model; m = load_model('final_model.pt')`
即得最优模型（与原 flat 同构、自包含、含全部模型信息）。

## 设计依据

`docs/plans/2026-08-13-puzzle-materialize-optimized-flat.md`（SDD 计划，风险 #1 blocks 依赖闭包实证排除）。
