# 计划：Puzzle 新增 pz_materialize 节点 —— 产出 optimized_flat 自包含交付件

> SDD：读现状 → 写计划 → 确认 → 实现。本文件不写代码，只列做什么 / 怎么做 / 怎么测。
> 分支：`puzzle-universal`。日期：2026-08-13。

---

## 1. 目标

在 `pz_select` 与 `pz_retrain` 之间插入确定性节点 **`pz_materialize`**，产出一份与 `<base>_flat.py`
同构、自包含、可独立跑通的最优模型文件 **`<base>_optimized_flat.py`**——架构定义里选中的 slot 已按
`selected_arch` 替换为新 variant 块（块类源码内联），权重经 `final_model.pt` 单次 strict 载入即可运行。

**正确性由构造保证**：optimized_flat 是 GKD 蒸馏 / gate 实测 / 最终交付**唯一**的执行基底——被训练、
被测、被交付的是同一个文件，不再有"训练时重建 vs 交付时重建"两条路径分叉的隐患。

## 2. 依据

- 设计草稿：[`docs/specs/puzzle-design-draft.md`](../specs/puzzle-design-draft.md)（§3 流水、§4 组件映射）
- 现状代码（逐行读过）：
  - `workflows/puzzle.yaml`（节点 / 路由 / output_schema）
  - `workflows/agents/_puzzle_scripts/puzzle_common.py::build_student_from_arch`（:919-986，现状重建逻辑）
  - `workflows/agents/_puzzle_scripts/build_selected.py`（合成 `selected_model.pt` = 父⊕BLD）
  - `workflows/agents/_puzzle_scripts/gkd_retrain.py`（现状 GKD，内部重建 student）
  - `workflows/agents/pz_retrain/agent.md`、`pz_report/agent.md`（节点契约格式）
  - `workflows/subagents/puzzle/project-fidelity-verifier.md`（明确 flat/block_map 非 fidelity 件，
    架构/slot 合规归 workflow-verifier）

## 3. 背景：现状权重链（已与用户对齐）

```
① pre_trained.pth        → adapters.load_pretrained(model)        整模父权重
② block_library/*.pt     → load_variant_state_dict(替换 slot)     BLD 块权重（覆盖①在 slot 上）
③ selected_model.pt      = ① ⊕ ②（build_selected 合成，整模 state_dict）
④ final_model.pt         = GKD(载③) 训练后的整模 state_dict
```

**关键简化**：①+② 已在 `selected_model.pt` 合成。optimized_flat **不分别懂父权重 / BLD 权重**，
全生命周期只做**一次** strict `load_state_dict`：
- 流水线内（GKD 起点）：载 `selected_model.pt`
- 交付态（用户手里）：载 `final_model.pt`

## 4. 设计：pz_materialize 节点

### 4.1 流水位置

```
pz_select
  └→ pz_materialize (新)
       build_selected.py 合成 selected_model.pt（父⊕BLD）   ← 从 pz_retrain 上移
       materialize_optimized.py 产 optimized_flat.py          ← 新确定性脚本
       自检：optimized_flat.build_model() strict-load selected_model.pt + forward DUMMY_INPUT
       + workflow-verifier（架构/slot 合规）
       失败 → terminate_materialize_failed
  └→ pz_retrain (瘦身：只剩 GKD)
       from optimized_flat import build_model; student = build_model()
       载 selected_model.pt → GKD 训练 → final_model.pt
  └→ pz_report (gate)
       optimized_flat.build_model() + 载 final_model.pt → adapters.evaluate + latency
  └→ 交付：optimized_flat.py + final_model.pt
```

### 4.2 脚本 + LLM 分工（用户已确认：机械性判断用脚本，LLM 保正确性）

- **确定性脚本 `materialize_optimized.py`（新，`_puzzle_scripts/`）做机械装配**：
  1. 读 `<base>_flat.py` 全文 → 作为架构类源**逐字照抄**（flat 是黑盒，不解析其类体）。
  2. 读 `selected_arch` + `block_map` → 确定每个 slot 选中的 variant。
  3. 内联选中 variant 的块类源码（从 `puzzle_blocks.py` + `nas_agent/blocks/*.py` 经 AST 抽取
     `make_<name>` / `_wrap` / 块类 + 其传递依赖，粘贴成 import-free 仅依赖 torch 的源）。
  4. 生成 `build_model()` 覆盖：原 flat 的 `build_model()` 建骨架 → 按 `block_map` 的
     `parent_module_path` 确定性 `setattr` 循环把非-identity slot 换成内联 variant 块（**与
     `build_student_from_arch` 同一套 factory 语义**，保证 state_dict key 对齐）。
  5. 生成 `load_model(ckpt_path)` 辅助 + `__main__` 自检（build → load → forward `DUMMY_INPUT`）。
  6. 自检并打印单行 JSON（含 `key_alignment_passed` / `forward_selfcheck_passed`）。
- **LLM（agent）做正确性保证**：
  - 跑确定性脚本；若 AST 内联命中边界 case（blocks 模块内部 import / helper 漏抽）导致自检 fail，
    agent 在**白名单内**（仅 `optimized_flat.py`）补全内联源，重跑自检（类比 pz_retrain 的 self-heal）。
  - 派 `workflow-verifier`（point-to-file）查架构 / slot 合规：每个替换 slot 的运行时类与
    `selected_arch` 一致、非替换 slot 保留父类、identity slot 零侵入。

### 4.3 节点 output_schema（新增）

```
required: [status, optimized_flat_path, selected_model_path,
           key_alignment_passed, forward_selfcheck_passed,
           workflow_verifier_passed, artifacts, assessment, error]
```
- `status ∈ {executed, failed}`
- `key_alignment_passed` (bool)：**核心死不变量**——`optimized_flat.build_model()` 的 state_dict
  keys 与 `selected_model.pt` 逐 key 对齐（strict load 零 missing/unexpected）。
- `forward_selfcheck_passed` (bool)：`__main__` forward DUMMY_INPUT 产出有限 tensor（无 NaN/inf）。
- `workflow_verifier_passed` (bool)：workflow-verifier all-pass。
- 路由：`status == 'executed'`（且两自检 true）→ `pz_retrain`；否则 → `terminate_materialize_failed`。

## 5. 改造点（最小化）

### 5.1 新增

| 文件 | 内容 |
|---|---|
| `workflows/agents/_puzzle_scripts/materialize_optimized.py` | §4.2 确定性装配 + 自检脚本 |
| `workflows/agents/pz_materialize/agent.md` | 新节点 agent body（镜像 pz_report 确定性风格 + self-heal 白名单 = 仅 optimized_flat.py） |
| `workflows/agents/pz_materialize/scripts/` | （若需）launcher / verify 包装，沿用现有 folder-agent 约定 |
| `terminate_materialize_failed` | puzzle.yaml 新 terminate（status: failed） |

### 5.2 修改

| 文件 | 改动 |
|---|---|
| `workflows/puzzle.yaml` | 插入 `pz_materialize` 节点；`pz_select` 路由 `→ pz_materialize`；`pz_materialize` 路由 `→ pz_retrain` / `→ terminate_materialize_failed`；outputs 增 `optimized_flat_path` |
| `workflows/agents/_puzzle_scripts/gkd_retrain.py` | student 构造：新增 `--optimized_flat` 入参；提供则 `from optimized_flat import build_model; student = build_model()` 替代 `build_student_from_arch`；其后载 `selected_model.pt` → 训练 → `final_model.pt` 不变。无 `--optimized_flat` 时保留旧路径（向后兼容 dry-run） |
| `workflows/agents/_puzzle_scripts/gate_report.py` | 同理：`--optimized_flat` 提供则用其 `build_model()` + 载 `final_model.pt`；`adapters.evaluate(model)` / latency 测量不变 |
| `workflows/agents/pz_retrain/agent.md` | 移除内部 `build_selected.py` 调用（上移到 materialize）；launcher 只调 `gkd_retrain.py --optimized_flat ...`；EPOCHS / self-heal / 轮询机制全不变 |
| `workflows/agents/pz_report/agent.md` | launcher 增 `--optimized_flat` 参数桥接 |

**不改**：`build_selected.py`、`bld.py`、`score.py`、`mip_select.py`、`puzzle_common.py`、
`pz_build_library` / `pz_score` / `pz_select` / `pz_expand` 节点。

## 6. verify 不变量（fail loud 死门）

1. **state_dict key 对齐**（最关键）：`optimized_flat.build_model()` 与 `selected_model.pt` 做
   `load_state_dict(strict=True)`，任一 missing/unexpected key → 节点 failed。
   - 对齐前提：内联 variant factory 与 `build_student_from_arch` 用的 `puzzle_blocks` factory **同模块类同参数名**。
2. **forward 自检**：`build_model() → load selected_model.pt → forward(DUMMY_INPUT)` 产出有限 tensor。
3. **架构/slot 合规**（workflow-verifier）：替换 slot 运行时类 = `selected_arch` 指定 variant；
   identity slot 保留父块（零侵入）；非 slot 参数（embedding/norm/head）类不变。
4. **幂等**：重跑 materialize 产出的 optimized_flat.py 字节级一致（确定性装配，无随机）。

> 不变量 1 过了门，下游 GKD / gate / 交付的权重加载全是确定性 strict load——不再有重建分叉。

## 7. optimized_flat.py 产物契约（自包含，对齐 flatten 习惯）

- **依赖**：仅 `torch` + stdlib（不依赖用户项目源、不依赖 `_puzzle_scripts/`、不依赖 run artifacts）。
- **结构**：① flat 架构类源（逐字）+ ② 内联 variant 块类源 + ③ `build_model()`（建骨架 + setattr 换 slot）
  + ④ `DUMMY_INPUT`（透传）+ ⑤ `load_model(ckpt)` + ⑥ `__main__` 自检。
- **权重不内嵌**（与 flatten 一致——flatten 也不内嵌，靠 `load_pretrained`）：交付 =
  `optimized_flat.py` + `final_model.pt`，用户 `load_model('final_model.pt')` 即得最优模型。

## 8. 测试清单

- **unit（`tests/puzzle/test_materialize.py`，新）**：
  - 用 mnist_trf fixture dry-run 产物（`selected_arch.json` + `block_map.json` + `selected_model.pt` + flat）
    跑 `materialize_optimized.py` → 断言 optimized_flat 生成 + key_alignment_passed=true + forward_selfcheck_passed=true。
  - 断言 optimized_flat 不含 `puzzle_blocks` / `nas_agent` import（自包含）。
  - 断言 identity-only arch 的 optimized_flat forward 与父模型 allclose（零侵入）。
  - 断言幂等：两次 materialize 产出 md5 一致。
- **集成**：现有 puzzle E2E（mnist_trf）加 materialize 节点后端到端跑通到 gate；`final_model.pt` 经
  optimized_flat 载入测得 acc/latency 与 gate_report 一致（证明交付件 = gate 实测件）。
- **回归**：gkd_retrain / gate_report 无 `--optimized_flat` 时走旧路径（dry-run 不回归）。

## 9. 验收

- [ ] puzzle.yaml `tars validate` 0 error（新节点 + terminate + 路由）。
- [ ] mnist_trf E2E：materialize 自检两 bool 全 true → GKD → gate pass。
- [ ] 交付件 `optimized_flat.py` + `final_model.pt` 在干净 env（仅 torch）独立 `python optimized_flat.py` 通过。
- [ ] code-reviewer 洁净审查闭环（依赖铁律 / fail loud / DRY）。
- [ ] release note + CHANGELOG + CURRENT.md 更新。

## 10. 风险 / 疑问（实现前先确认）

1. **内联边界 case**（2026-08-13 实证排除）：4 个 mixer 块仅依赖 `nas_agent.blocks.primitive_blocks`
  （torch-only 自包含，1006 行纯 torch，`__init__.py` 空）+ torch；vanilla/masked/linear/ffn/no_op 仅用
   puzzle_blocks helper + nn。**无跨包深依赖**。采用**整模块拼接**（puzzle_blocks helper + 用到的 block
   模块 + primitive_blocks，保留 torch/math/typing import、剥离包内 import、跨模块去重）替代选择性 AST 抽取，
   更稳。LLM self-heal（白名单仅 optimized_flat.py）+ workflow-verifier 兜底拼接边界 case。
2. **build_selected 上移影响**：现 pz_retrain 把 build_selected 当"预写脚本禁 edit"调用；上移到
   materialize 后语义不变（同样脚本同样入参），但 pz_retrain agent.md 的资源锚点 / 铁律措辞要同步。
3. **gate_report 改造范围**：gate 现经 `build_student_from_arch` 重建；改 optimized_flat 后需确保
   `adapters.evaluate` 收到的 model 对象与旧路径同型（forward convention 一致）——应一致（同架构），
   但实现时需 diff 验证。
4. **是否保留向后兼容的旧路径**（`--optimized_flat` 可选）：**用户拍板 2026-08-13 = 严格只走
   optimized_flat**——gkd_retrain.py / gate_report.py 删 `build_student_from_arch` 旧路径，
   `--optimized_flat` 改为 required 入参，最干净。dry-run 须先经 materialize 产 optimized_flat。
5. **selected_model.pt 是否随交付件一起交**：optimized_flat 的 `__main__` 自检在交付态应载
   `final_model.pt`（非 selected_model.pt）。流水线内自检载 selected_model.pt。两 ckpt 在 optimized_flat
   视角是"同一结构的两个权重快照"，载哪个都 strict 对齐——实现时 __main__ 默认载 `final_model.pt`，
   缺失才回退提示。
