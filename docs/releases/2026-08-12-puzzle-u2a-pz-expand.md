# Release: Puzzle U2a — pz_expand 重构主体 + target spike

**日期**: 2026-08-12
**分支**: `puzzle-universal`
**SPEC**: `docs/specs/puzzle-universal-design-draft.md` §6 / §9 / §3 / §14 / §17

## 目标

把 pz_expand 从「跑确定性 expand_model.py（类名 regex 识别 slot）」重构为
「LLM 自适应产 flat + manifest + search_space + 跑 measure_baseline.py 测量 + 4 道
fidelity smoke」。这是通用化方向的命门——验证 LLM 能自适应产任意 transformer 项目的 flat。

## 实际改动

### 1. target spike（E13 命门）—— PASS

LLM（Claude 代理 deepseek-v4-flash，见下方诚实声明）读 `playground/target/model/model.py`
+ `train.py::eval_model` → 独自产 self-contained `target_flat_spike.py`（4 输入 → 单输入打包
facade + reparenting 避前缀）→ strict-load `pre_trained.pth`（63 裸 CrossFusion 键）：

- **0 missing / 0 unexpected**，forward 输出 `[2, 16]` embedding
- **0 fix-loop**（SPEC §16.1 要求 ≤2，实际 0）
- 证明：LLM 读源码能自适应推出 reparenting 技巧（`setattr(self, name, mod)` 避 `net.` 前缀）
  + 单输入打包，无需手写 adapter

**诚实声明（Rule 7 surface conflict）**：环境未配置 deepseek API key（`opencode` 已装但
`OPENCODE_API_KEY` 空），spike 由 Claude（本 agent）代行。证明的是「LLM 自适应 flatten 任务
可行」这一通用命题；deepseek-v4-flash 的具体能力是独立变量，Phase U4 in-session E2E 时验证。
spike 产物在仓库外（`playground/target/artifacts/puzzle_spike/`），不入 git。

### 2. measure_baseline.py（NEW，从 expand_model.py 拆出测量部分）

确定性测量脚本，4 道内嵌 fidelity smoke（SPEC §9.2）：

1. **strict-load**（E5/BLK-1）：father ckpt `load_state_dict` missing/unexpected **双零**
   （比 puzzle_common.load_father_model 的 20% 阈值更严——本节点是 fidelity 关卡）
2. **forward-determinism**（E25）：同输入 forward 两次 `torch.equal`
3. **per-slot identity allclose**（E5）：hook 每个 slot forward 两次逐元素 allclose
   （`father-loaded 模块 zero-intervention 可复现`——§16.4 identity 契约的前置必要条件；
   完整 §16.4 跨模型 allclose 在下游 build_selected）
4. **eval-stability**（E25）：eval_fn 跑两次 acc 一致

+ trace slot I/O 末维回填 search_space.yaml + 落 block_map.json + 测 acc/latency。
empty slots（E22）→ exit 2 路由 terminate_unsupported。

### 3. search_space_io.py（NEW）

search_space.yaml 读写（SPEC §4）：
- `load_search_space_yaml`：path↔parent_module_path 映射（E3）+ candidates 校验委托
  parse_block_candidates（E1 identity 必入）+ kind 开放标签校验 + 重复 id/path 检查。
  slots 非空但 candidates 空 → fail loud（禁静默填默认）。
- `save_search_space_yaml`：保留 YAML-only 元数据（id / kind_evidence）+ 回填 traced shape。
- `to_block_map`：slot_dicts → BlockMap（下游 bld/score/mip 既有格式）。

### 4. puzzle_common.py（EDIT）

Slot dataclass 加 `forward_arity: str = "single"`（SPEC §4.1 E2 记录字段；U1 漏加，本 phase 补）。
默认值保证既有 v1 路径（expand_model.py 构造 Slot 时不传 forward_arity）零回归。

### 5. pz_expand/agent.md（REWRITE）

从「跑 expand_model.py」改为「LLM 自适应」：
- Step 0 reuse-check（5 产物齐 + slot 数一致）
- Step 1 Discover & Flatten：LLM 读源码产 flat.py + manifest.yaml（5 段 YAML）+
  search_space.yaml（slots + kind + **确定性证据** + candidates）；含 manifest/search_space schema +
  flatten 自适应关键点（多输入打包 / reparenting 避前缀）+ kind 证据要求（attention 须 QK^T 缩放；
  ffn 须 Linear→Act→Linear）
- Step 2 跑 measure_baseline.py（4 smoke + self-heal 策略 ≤2 fix-loop）
- Step 3 workflow-verifier + memory-verifier
- 洁净契约：tars validate 0 warning + 受众翻转通读无残留

### 6. puzzle.yaml + checklist

- output_schema：加 `search_space_path` / `manifest_path` 必填（E10）+ `additionalProperties: false`
- inputs：build_fn/eval_fn/pretrained_ckpt 加过渡期 NOTE（agent 已切 manifest-discovered，input 留作
  下游 v1 路径消费，U3 迁移后退役）
- workflow-checklist：更新为新契约（search_space kind_evidence / manifest 5 段 / smokes_passed）

### 7. tests/test_puzzle_measure_baseline.py（NEW，11 测试）

- search_space_io：roundtrip + 4 路失败（缺文件/非法 kind/重复 id/缺 identity）+ 补 4 路
  （slots 非 list/缺字段/重复 path/空 candidates）
- measure_baseline：happy 4 smokes + empty slots E22 + strict-load missing fail + strict-load
  unexpected fail + forward-determinism fail（F.dropout training=True）+ eval-stability fail
  + per-slot allclose 分支（函数级 monkeypatch 隔离 smoke 2 干扰）+ baseline_metrics 审计字段

## 验收

- ✅ target spike：strict-load 零 missing，0 fix-loop
- ✅ measure_baseline 4 道 smoke fail-loud 全覆盖（11 测试）
- ✅ tars validate 0 error / 0 warning
- ✅ agent.md + checklist 受众翻转通读洁净（dev-residue 扫描 + §4 类别人工通读）
- ✅ 既有测试零回归（catalog/father_state 33 测试全过；expand_model.py v1 路径保留不动）
- ⏳ mnist_trf 回归：需 in-session E2E（Phase U4）；U2a 仅脚本级 + 单元测试

## 偏差 / 诚实声明

1. **spike 用 Claude 代 deepseek**：环境无 deepseek API key；任务可行性已证，deepseek 专项待 U4。
2. **smoke 4 语义澄清**：实现为「father-loaded zero-intervention 可复现」（§16.4 前置条件），
   非 §16.4 完整 AC（需 selected_arch，下游 build_selected）。错误信息已对齐实现边界。
3. **input 契约双轨**：yaml inputs 仍含 build_fn/eval_fn/pretrained_ckpt（下游节点需要），
   agent 已切 manifest-discovered。加 NOTE 标注过渡，U3 下游迁移完成后退役。
4. **DRY 遗留**：measure_latency 与 expand_model.py 重复（2 处），Phase U3 清理 expand_model.py
   时统一提到 puzzle_common。

## Commit SHA

见 `git log` 本分支顶部 commit。
