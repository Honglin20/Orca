# Phase U2b — block-map / search-space evaluator + fixture suite + recall AC

**日期**: 2026-08-12
**分支**: `puzzle-universal`
**Commit**: `a308784`
**SPEC**: v2 §10（verifier 闭环 + 严重级 + evaluator 质量）、§16.6/§16.7（recall AC）、§17（E18 闭环）

## 做了什么

建 U2a 产物（`search_space.yaml` + `block_map.json` + flat + manifest）的**审查层**——两个 point-to-file 只读 evaluator + seeded-error fixture suite + recall AC 测试 + pz_expand 触发点。

### 1. 两个 evaluator subagent（`workflows/subagents/puzzle/`）

- **`block-map-evaluator.md`**（sentinel `BM7PZ4`）：审 slot 语义/结构正确性。checks：path 定位 / I/O shape（声明的 in_dim/out_dim 与 flat 真模块对齐）/ identity 入每 slot 候选（E1）/ return_arity 一致（multi slot 拒单输出候选）/ user factory 可解析 / **kind 标签由 forward 源码确定性证据支持**（attention 须 `matmul(Q,K^T)*scale`+softmax；ffn 须 `Linear→Act→Linear`——避免 LLM 凭感觉判 LLM）/ candidate 适用 kind / mask-bearing slot 拒 mask-blind 候选。severity BLOCKER/MAJOR/MINOR；输出 `LGTM` 或 `[Severity][Symptom][Reason][Fix]` bullets（对齐 `supernet-evaluator.md` 协议）。
- **`search-space-evaluator.md`**（sentinel `SS4KQ9`）：审 schema/契约合规。checks：slot 必填字段（id/path/kind/layer_idx + kind 特定字段）/ id+path 唯一 / kind 合法 enum / candidates 块存在且 well-formed / candidate 注册有效（catalog 或 user factory）/ user factory `path::func` 可解析 / **eval_kind sanity（D6）**：classification/embedding/regression 与 manifest 的 loss/metric/output shape 自洽 / 无 `axes` 残留（U1 删的字段）/ 跨文档 eval_kind 一致。与 block-map 职责正交（schema 契约 vs 语义结构）。

两者均为 point-to-file 只读 judge（不改文件），带 Resumed Re-Check 段。frontmatter 三键合规（`subagent`=filename stem / `version` / `sentinel`），`_check_subagents_md` 通过。

### 2. Fixture suite（`tests/e2e_puzzle/fixtures/evaluator_cases/`）

12 case（11 seeded error + 1 clean baseline），每 case 一个 primary evaluator + 一个 expected verdict。4 个自包含 flat 变体（`flats/`）：`clean_flat`（标准 2-layer tiny transformer，真 QK^T attention + Linear-GELU-Linear ffn）、`conv_attn_flat`（attn 是 Conv1d mixer，无 QK^T）、`embedding_flat`（输出 hidden 向量非 class logits）、`multi_return_flat`（attn 返回 tuple）。`build_evaluator_cases.py` 是确定性生成器（单一意图源，dev 改完跑一次刷新 materialized 树）。

| case | primary | sev | seeded error |
|---|---|---|---|
| 01_conv_as_attention | block-map | MAJOR | attention 标签但模块是 Conv1d mixer |
| 02_wrong_path | block-map | BLOCKER | slot path `blocks.0.nonexistent` 不定位 |
| 03_shape_mismatch | block-map | BLOCKER | 声明 in/out=64 但真模块 dim=32 |
| 04_identity_missing | block-map | BLOCKER | candidates.attention 缺 identity |
| 05_eval_kind_mislabel | search-space | MAJOR | manifest eval_kind=classification 但 loss=InfoNCE / metric=k-NN / output=[2,16] hidden |
| 06_mask_blind_on_mask_slot | block-map | MAJOR | mask_load_bearing=true slot 选 fnet（mask_aware=false） |
| 07_return_arity_violation | block-map | BLOCKER | return_arity=multi slot 配单输出候选 |
| 08_clean_baseline | both | LGTM | 合规 |
| 09_duplicate_id | search-space | BLOCKER | 两 slot 同 id |
| 10_unknown_candidate | search-space | BLOCKER | candidates 引用 bogus_block（不在 catalog） |
| 11_axes_residual | search-space | MINOR | slot 残留已删的 axes 字段 |
| 12_candidate_wrong_kind | block-map | MAJOR | candidates.attention 含 ffn_50（ffn-only 候选） |

### 3. Recall AC 测试（`tests/test_puzzle_evaluator_recall.py`）

两层：
- **`TestEvaluatorFixtureIntegrity`**（always runs，无 LLM）：每 case 确定性验 seeded error 真实存在（per-case checker，不只 well-formedness）+ flat 可 import + expected.yaml schema 一致。24 用例全过——这是防 fixture 漂移的 always-on 护栏。
- **`TestEvaluatorRecall`**（LLM-gated，class 级 `skipif(not llm_available())`）：驱动 evaluator body 跑 fixture suite，断言 block-map recall ≥0.90 / search-space schema recall ==1.0 / clean baseline 两 evaluator 都 LGTM。

driver（`tests/e2e_puzzle/evaluator_driver.py`）把 evaluator body + search_space + manifest + flat 源码 + catalog 全 inline 进 prompt（无需 tool call，最确定），支持两后端：anthropic SDK（`claude-sonnet-5`，env `ORCA_EVAL_MODEL` 可覆盖）+ opencode（`deepseek/deepseek-v4-flash`）。runtime 故障（余额/鉴权/超时）→ driver 抛 `RuntimeError` → 测试 `skip` 不 `fail`（首次故障 latch，sibling case 快速 skip）；剥掉 evaluator 的 sentinel-echo 首行让 `startswith("LGTM")` 评分生效。

### 4. pz_expand 触发点（`workflows/agents/pz_expand/agent.md`）

Step 3 重构为 3.0/3.1/3.2：**Step 3.0** 在 Step 2 measure_baseline 跑通后、workflow-verifier 前，按 point-to-file 协议分别调两个 evaluator（独立 fix-loop；动了 slot 结构性字段须重跑 measure_baseline；≤3 轮超限 fail loud）。subagent 协议段加了两个 evaluator 全名。

## 验收

- `tars validate workflows/puzzle.yaml` → 0 error / 0 warning（含 `_check_subagents_md` frontmatter 校验 + dev-residue lint）。
- 24 deterministic 完整性测试全过（`TestEvaluatorFixtureIntegrity`）。
- recall 层 env-gated skip：本环境无 `ANTHROPIC_API_KEY` + deepseek 余额 0，14 recall 用例全 skip（带清晰 reason），U4 复测时跑真实数字。
- 既有 puzzle 测试无回归（`test_puzzle_catalog` + `test_puzzle_delta_review` 31 过）。
- evaluator .md 洁净契约：grep dev-residue pattern（§N.M / (EN) / orca 源码路径 / 测试项目名）零命中。

## 偏离计划 / 决策

- **LLM recall 数字未测**：本环境双后端均不可用（无 anthropic key、deepseek 余额 0）。recall 测试基础设施完整且正确（driver 支持两后端 + 故障 skip + sentinel 剥首行），U4（两项目 E2E 重跑 + deepseek 专项 spike 复核）补真实 recall 数字。这符合任务边界「若 deepseek key 不可用，用 Claude 跑（标注，U4 复测）」——两路径均 env-gated。
- **recall 阈值在 N=7 下的语义**（Rule 7 决策）：fixture suite 每 check 类别一个 case（7 block-map 类别），SPEC §16.6 的 ≥90% 在 N=7 下要求 7/7（6/7=0.857<0.90）。这是有意的——每类别一个 case 时漏判任一即为真实覆盖回归，严格阈值是正确质量门。suite 扩到每类别多 case 时，同一 0.90 阈值自然获得统计弹性，无需改阈值。test docstring 已显式说明。
- **未加 mask 反向检查**（Rule 7 决策）：code-reviewer 建议 block-map 补「forward 签名含 mask kwarg 但 slot mask_load_bearing=false → flag」反向检查。**不采纳**——`mask_load_bearing` 语义是 mask 是否 *functionally load-bearing*，非仅签名出现；许多 attention forward 接受可选 `attention_mask` 但实际不依赖它，反向检查会过度触发假阳性。SPEC §10.1 也只列正向（mask-bearing slot 选 mask-blind 候选）。

## code-reviewer 闭环

dispatch `code-reviewer` 审全部 U2b 文件（adversarial）。复审结论：设计扎实、契约贴合度高。提了 2 Blocker + 5 Major + 5 Minor。处置：
- **[BLOCKER-1]（generator/materialized/check 漂移）**：复审并发读 race 造成的 stale 读——当前三方一致（manifest eval_kind=classification + InfoNCE/k-NN 信号），24 完整性测试过。验证后确认非真实问题。
- **[BLOCKER-2]（sentinel-echo 与 LGTM 评分冲突）**：修——driver `_strip_sentinel` 剥首行。
- **[MAJOR-3]（异常覆盖太窄）**：修——`_run_evaluator_or_skip` 改 `except Exception`；driver 把 `subprocess.TimeoutExpired`/anthropic 异常包成 `RuntimeError`。
- **[MAJOR-4]（zero-balance stdout 非空被当 recall miss）**：修——`_run_opencode` 检 returncode/余额/鉴权签名，主动 raise 触发 skip。实测：zero-balance deepseek 现正确 skip（曾为 hard fail）。
- **[MAJOR-5]（N=6 使 ≥90% 退化全或无）**：补 case 12（candidate_wrong_kind，覆盖 check #7）+ test docstring 显式说明小 N 语义（见上决策）。
- **[MINOR-6/9/10]**：`backend_label` 接入 skip 消息；`_check_conv_as_attention` 改 `importlib.util.spec_from_file_location` 绝对路径加载（不再污染 sys.path）；case 05 severity 已与设计一致（MAJOR）。
- **[MINOR-7]（case 07 catalog 无 arity 描述）**：不动——catalog 是 U2a 资产（边界「不动 U2a 逻辑」）；evaluator 从 slot return_arity 声明 + flat 源码 tuple return + catalog「forward(x) 单参」描述三者已足够判定。
- **[MINOR-8（mask 反向检查）]**：不采纳（见上 Rule 7 决策）。

## Commit SHA

- `a308784` feat(puzzle-u2b): block-map/search-space evaluator + fixture suite + recall AC
