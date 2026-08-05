# SPEC：kd-nas 引入 LLM 语义 fidelity 审计 + ID 化收敛环

> 状态：**REVIEWED**（spec-reviewer conditional-pass，11 issue 全闭环；S1 blocker + M3 契约歧义已修；O1–O4 决定已定）。待用户确认 4 决定后进实现。
> 范围：kd-nas workflow 的训练脚本生成/校验阶段（`gen_train_script` / `train_script_verify` 两节点）。
> 来源：借鉴 `workflows/subagents/nas-supernet/{project-fidelity-verifier,workflow-verifier,memory-verifier}.md` 体系，**只取适用件，明确排除不适用件**。

---

## 1. 背景与动机

kd-nas 的训练叶子生成（`kd-train-script` 产 4 叶子 loss/data/eval/optim）现有校验栈：

| 层 | 机制 | 性质 | 实际覆盖 |
|---|---|---|---|
| L1 | 逐叶子 `py_compile` + AST 自包含 + AST 签名 | 确定性 | import 白名单 / 函数签名 |
| L2 | 引擎 smoke（合成 ckpt 跑 `--mode teacher/eval` 1 epoch，验协议键） | 确定性 | 叶子能被引擎加载跑通 |
| L3 | `fidelity_check.py`：数值等价 + AST + 反造假 + kind 方向 | 确定性 | **单函数体内**偏差（loss: 同输入 allclose + AST body dump 双层；loader shape；eval allclose 或 skip；optim 类名） |
| L4 | `workflow-verifier` 子 agent（checklist review，4 叶子并行） | LLM，一次性 | 机械层（file/import/config key），auto-fixable 自收敛 |

L1–L3 是**确定性**的，覆盖「同一组合成输入下输出不同」「签名不符」「随机造假」。L3 对 **`compute_loss` 单函数体内**的丢项/换 reduction 抓得很死（numeric allclose + AST body dump 双层）。

**L3 真正的盲区**（=本 SPEC B1 要补的缺口，经 `fidelity_check.py:610-647` 实证确认）：
- **(a) helper 体外的 look-alike 替换**：`compute_loss` body 字面相同，但它调用的 module-level helper 被换成形似实不同的实现——AST 只比 `compute_loss` body，**不展开**被调 helper；
- **(b) data/eval transform 内容差异**：`LOADER_SHAPE_OK` 只比 batch shape 不比内容；`EVAL_ALLCLOSE` 在用户 eval 不可 import 时 **silent skip**；
- **(c) optim kwargs**：`OPT_TYPE_OK` 只比类名（`AdamW` vs `Adam`），**不比** `lr`/`weight_decay`；
- **(d) 控制流重排**：loop 内条件分支被省略，且恰好在 L3 合成输入未触发的路径上。

这些是**语义**偏差，需要 LLM 沿用户原调用链做静态比对审计（展开 helper、比 transform 内容、比 optim kwargs、追控制流）。nas-supernet 的 `project-fidelity-verifier` 正是干这个——带「mechanical vs semantic 偏差判定」+「Accepted Deviations / Unresolved」分类 +「Intended behavior」声明入口。

第二个缺口：kd-nas 的 L4 workflow-verifier 是**一次性**（`VERDICT: all-pass | unresolved`，无稳定 ID，无 resume）。nas-supernet 的 verifier 用**稳定 Item ID**（`[1] [2] …`）+ **Resumed Re-Check 协议**（`Fixed: [ids]` / `Context: [id] <text>`），caller 修完后只复查那几个 ID + 改过的文件——把一次性 fail 变成**收敛环**。

## 2. 范围

### 2.1 纳入（borrow）

| # | 件 | 来源 | 落点 |
|---|---|---|---|
| B1 | **LLM `project-fidelity-verifier` 子 agent**（语义静态比对 + differential probe + mechanical/semantic 偏差判定 + Accepted/Unresolved 分类 + Intended behavior 入口） | `nas-supernet/project-fidelity-verifier.md` | 新建 `workflows/subagents/kd-nas/project-fidelity-verifier.md`（KD 适配版，独立副本——O2 决定） |
| B2 | **ID 化 Resumed Re-Check 协议**（稳定 Item ID + `Fixed:[ids]`/`Context:[id]` resume） | nas-supernet 两 verifier 的 "Resumed Re-Check" 段 | B1 verifier 自带；在生成节点包一层有界收敛循环 |

### 2.2 排除（not applicable / already-have）

| # | 件 | 排除原因 |
|---|---|---|
| X1 | `memory-verifier` + 项目记忆文档 | kd-nas 单 run 内多轮迭代，已有 `ledger.jsonl`/`champions.jsonl` 真相源；跨 run 记忆价值低（维持） |
| X2 | self-heal 重试 + 审计字段 | kd-nas distill catch「失败即 continue 下一轮」与 self-heal「反复改单轮」语义冲突（维持；fail-loud 节点未来另立项） |
| X3 | severity + auto-fixable taxonomy | **kd-nas 已有**（维持） |
| X4 | `workflow-verifier` 子 agent 本体改造 | **不升 ID-resume**（O1 决定，见 §5）：scope 是机械层、auto-fixable 已自收敛；两 verifier scope 不同（机械 vs 语义），协议不一致是设计内分工，非债 |

## 3. 契约

### 3.1 B1：`project-fidelity-verifier`（KD 版）

**位置**：`workflows/subagents/kd-nas/project-fidelity-verifier.md`（独立副本，O2 决定；与 nas-supernet 版同级、独立，**不改动 nas-supernet 版**）。

**frontmatter（N7 闭环，必填，orca `_check_subagents_md` strict regex 校验）**：
```
---
subagent: project-fidelity-verifier-kd
version: 1
sentinel: <独立 sentinel，不复用 nas-supernet 版 PF8LK3>
---
```
（orca 用 point-to-file 协议经 `{{ subagents_root }}` 自读此 .md，**无名字 allowlist**——见 §3.1 末「spawn 接线」。）

**与 nas-supernet 版的差异**（仅这几处，其余照搬 audit procedure / deviation judgment / resume 协议）：

- **Out of scope**：把 nas-supernet 版的「`supernet.py` 不审计」替换为「**KD 引擎（`_kd_scripts/train_pipeline.py` + `kd/`）+ student 变体契约（`build_model`/`DUMMY_INPUT`/`KNOBS`）不审计**」。审计对象 = **4 个叶子**里搬运来的用户逻辑。
- **必须从 nas-supernet 版删除的段落**（KD 不适用）：
  - 「RL environment fidelity」段（KD 无 RL）；
  - 「auxiliary networks（GAN discriminator / KD teacher）」段（teacher 在引擎侧、非 auxiliary）；
  - 「reward formula」相关的 reward/metric 互参（KD 无 reward）。
- **Intended behavior（Input #3）固化声明**（KD 专属，caller 必传，渲染入 spawn prompt）：
  - 叶子须**逐字搬运**用户 loss / dataloader / eval metric / optim（公式、常量、符号、控制流、随机语义）；
  - **设计内差异**（不算偏差）：KD 的 distillation loss 组合在引擎侧（`kd.compose.build_kd_loss`），不进叶子；叶子不携带 KD recipe（无 `kd.*` 引用）；
  - eval_metric 返回 `(value, kind)`，kind 方向由 `inputs.accuracy_baseline_kind` 约束（已由 L3 硬校验，fidelity-verifier 不重复测方向，只审公式体 + transform 内容）。
- **Deviation Judgment**：照搬 nas-supernet 版 mechanical/semantic 二分。KD 语境典型 mechanical：epoch/batch_size 缩放、device 变更、re-iterable adapter 包裹。典型 semantic：丢 loss term、换 reduction、**helper 体外 look-alike 替换**、transform 内容差异、optim kwargs 漂移、控制流重排。

**Input**（caller 经 spawn prompt 注入）：
1. `leaves_dir`（4 叶子路径）+ 用户原 `train.py` / 发现到的 eval 脚本路径；
2. **source→generated mapping**（`compute_loss`↔用户 loss fn、`build_dataloader`↔用户 loader、`eval_metric`↔用户 eval、`build_optimizer/build_scheduler`↔用户 optim/sched）；
3. **Intended behavior**：上述 KD 专属声明；
4. `<user_project_root>`（differential probe 用）。

**Audit procedure**：照搬两层——(1) 静态比对审计（沿用户原调用链逐项比对 helper 完整性 / 训练语义 / metric 保真 / transform 内容 / optim kwargs / 控制流，**展开 module-level helper 比对**——这是补 L3 盲区 (a) 的关键）；(2) differential probe（对可 import 的纯函数跑合成输入数值比对；不可 import / stateful / expensive 则 skip 并报告，**绝不造假 probe**）。

**Output**（单条返回消息，给 caller 消费）：
1. **Coverage**：审计了哪些用户行为 + 经哪层（static / probe）。
2. **Static Fidelity**：`pass` 或 findings 列表（每条带稳定 ID `[1] [2] …` + artifact 位置 + source 引用 + 差异描述）。
3. **Runtime Fidelity**：`verified via differential probes (N probes)` 或 `not verified` + 原因。**KD 预期说明**：用户 `train.py` 几乎从不在 Orca venv import 成功（依赖用户项目模块）→ probe 大多 skip → Runtime Fidelity 长期 `not verified` **属预期**；B1 主要价值在 Static Fidelity，Runtime Fidelity 是次要层（O4 决定，无 probe fallback）。
4. **Accepted Deviations**（仅有则报）：每条带 ID + `semantic`/`caller-confirmed` tag + 推理。
5. **Unresolved**（仅有则报）：每条带 ID + 不确定点 + caller 须确认/修什么。
6. 全过 → `all-pass` + Coverage 摘要。

**统一 ID 空间**：Static Fidelity / Accepted / Unresolved 共享一套顺序稳定 ID（`[1] [2] …`），**不重编号**。

**Resume 报告 STATUS 契约（N2 闭环，机械可解析——禁靠 LLM 推断）**：Resume 模式下，每个重查 ID 的报告块**必须**以 `STATUS: closed | open | accepted` 起首行（`closed`=本轮改对了/原 finding 已消；`open`=reaffirm 仍 FAIL；`accepted`=本轮新判为 Accepted Deviation）。caller 据此机械判定 `fixed_ids`（取 `closed`），不读 prose 推断。
**ID 范围防御（hallucinate 防御）**：caller 校验 resume 报告里的 ID ⊆ 上轮 stash 的 ID 集；超出 → fail loud（verifier hallucinate 了不存在的 ID）。

**Resumed Re-Check**（照搬 nas-supernet 版）：
- `Fixed: [ids]`：caller 改了这些 ID 的代码 → 只复查这些 ID + 改过文件的静态/probe。
- `Context: [id] <text>`：caller 对某 ID push back / 补上下文 → 重判该 ID（可 reaffirm/reverse/newly accept；newly accept 且依据是 caller 推理而非独立源码阅读 → tag `caller-confirmed`）。
- Resume 只返回重查项的标准报告，**不重建全量审计**。

**spawn 接线（point-to-file 协议，orca `{{ subagents_root }}`）**：
- B1 经 orca point-to-file 协议接线：host agent（`kd-train-script` / `train-script-verify`）用 `{{ subagents_root }}/project-fidelity-verifier.md` **自读**该 .md 并 embed 进 spawn 上下文（非按名字 free spawn）。`subagents_root` 由 render 层解析为 `workflows/subagents/kd-nas/`。
- 引用 `{{ subagents_root }}` 的节点 host 必须在 tools 白名单含 `read`（orca `_check_subagents_md` 静态校验）。
- **spawn prompt 模板落点**（N4 闭环）：`kd-train-script/SKILL.md` 新增 `## L4-semantic — project-fidelity-verifier spawn` 段，含 **first-run** 和 **resume** 两份模板。first-run 渲染 4 个 Input；resume 模板额外渲染 `Fixed: <fixed_ids>` 行。
- **ID 传递路径**（M3/N2 闭环）：生成 agent 收上一轮 verifier 报告 → 按 `STATUS:` 行机械解析 IDs 进 stash（`closed`→fixed_ids，`open`→reaffirm_ids，见 §3.1 STATUS 契约）→ 下轮 spawn 把 `closed` IDs render 进 `Fixed:`。**必须在 SKILL.md + agent.md 显式写出**。

**红线**：read-only（不改叶子 / 引擎 / 用户项目代码）；probe 用 throwaway 代码、不留痕；不造假 probe 结果；不审 KD 引擎 / student 变体。

### 3.2 B2：生成节点的有界收敛循环（L4-semantic）

**落点**：`workflows/agents/kd-train-script/agent.md` Step 4（Validate）+ `SKILL.md` Step 4。

**层命名**（M2 闭环，避免「L3.5 误导深度」）：B1 = **L4-semantic**；原 L4 workflow-verifier = **L4-mechanical**。两 LLM 层并列在 L3 之后，后缀反映机械/语义分工。

**执行顺序**（M5 闭环）：L1 → L2 → L3（确定性）→ **L4-semantic (B1) 收敛环**（仅当 L3 PASS 才进——L3 FAIL 立即 fail loud，不跑 B1，避免与确定性层重叠双报）→ **L4-mechanical (workflow-verifier) 一次性**。

**为什么落在生成节点而非 `train_script_verify`**：生成节点有**改叶子权限**（它是生成者）；`train_script_verify` 是只读独立闸（红线「❌ 修叶子」）。fix loop 必须落在有编辑权的一方——与 nas-supernet `ns_train_script` 单节点 generate+verify+self-heal 同构。

**循环逻辑**（确定性控制流，非 LLM 自驱）：

```
turn = 0
fixed_ids = []
reaffirm_count = {}   # N1 闭环：ID -> 连续 reaffirm 次数
loop:
    turn += 1
    if turn == 1:
        spawn L4-semantic fidelity-verifier（首次全量审计）
    else:
        spawn L4-semantic fidelity-verifier resume（Fixed: <fixed_ids>）
    if verifier spawn 自身崩（rc≠0 / sentinel 缺失 / 产出无 all-pass/Static Fidelity 段）:  # M1+N5
        fail loud 不重试 + stderr 报 raw 产出 + ask-user 哨兵（协议层崩非 transient）
    校验 resume 报告 ID ⊆ stash（hallucinate 防御）；解析 STATUS 行 → closed/open/accepted
    if verifier == all-pass: break（进 L4-mechanical）
    # N1 闭环：同一 ID 连续两轮 open（reaffirm）→ agent 已尽力，转 ask-user 不盲目重试
    for id in open_ids: reaffirm_count[id] += 1 else reaffirm_count[id] = 1
    if any reaffirm_count[id] >= 2: fail loud + ask-user 哨兵（报「ID 反复 reaffirm，agent 改不动」）
    if turn >= MAX_TURNS:                              # S6/O3 闭环
        fail loud + stderr 报「未关环 IDs + 上轮 findings」+ 退非零，不 emit JSON
    # 只修「caller 可独立判定」的 semantic findings（open_ids）
    # Unresolved 项（verifier 缺 basis）→ 不擅自改，fail loud + ask-user 哨兵
    apply fixes to leaves（仅叶子，禁碰引擎/KD 库）
    重跑 L1 py_compile + L3 fidelity_check
    if L1/L3 FAIL:                                     # N1 补：fix 改坏了确定性层
        fail loud + ask-user 哨兵（报「fix 破坏 L1/L3，回滚或人工」），不继续盲目改
    fixed_ids = closed IDs（机械从 STATUS 行取）
```

**`MAX_TURNS = 3`**（O3 决定，对齐 nas-supernet self-heal；KD 语义偏差通常 1–2 轮收敛，3 给 partial-fix slack；overshoot = fail loud 不降级 pass，下行风险有界）。成本评估（M6）：每轮 ~1 次 deepseek-v4-flash spawn + L1/L3 确定性秒级重跑，3 轮 wall-clock 增量 < 2 分钟、token < 10k，相对 gen 整体非主导。

**Unresolved 处理**：project-fidelity-verifier 的 Unresolved = verifier 缺 basis 的项目特定约束。生成 agent **不擅自改**（无依据），转 `fail loud + ask-user 哨兵`（复用 kd-nas 现有 ask-user 协议）——与「严禁造假、缺数据问用户」铁律一致。

### 3.3 `train_script_verify`：独立二闸（L4-semantic 一次性，无循环）

`train_script_verify` 加一个 **step 3.5**：一次性 spawn `project-fidelity-verifier`（**非 resume**，全量审计），verifier `unresolved` 或 Static Fidelity 非 pass 或 spawn 自身崩（rc≠0/sentinel 缺/产出无 all-pass 段，N5 闭环）→ `verified=false` + 退非零。

**S2 事实修正（二闸写权限）**：现有 `train-script-verify` step 4 spawn 的 `workflow-verifier`（经 SKILL.md 内联模板）**对 artifacts 有写权限且 auto-fix 机械项**（`workflow-verifier.md` 契约）——它**不是**纯只读闸，会静默改叶子后报 `all-pass (with Fixed)`。故 §3.3 的「独立二闸 = 只读闸」名不副实。

**二闸独立性论证（修正后）**：二闸的价值 = (1) 防gen 节点**叙述假 pass**（self-review 的 self-confirmation bias，二闸在独立 spawn 上下文强制重跑兜底）+ (2) workflow-verifier 的机械层 auto-fix 兜底（gen 节点 L4-mechanical 漏掉的机械项）。**不在**「能发现 gen 漏判的 semantic 偏差」上论证——若 gen 真跑了 all-pass，二闸同 prompt 同叶子大概率同 verdict。

**设计决定（见 §5 D1）**：二闸 spawn fidelity-verifier + workflow-verifier 时**是否禁用 auto-fix**（report-only），以名副实归「独立二闸」。本 SPEC 推荐禁用（report-only），理由：独立闸的价值在「复现/暴露」，不在「悄悄改」；auto-fix 留给 gen 节点（有完整上下文）。

**verdict 冲突处理**（M5/N8 闭环）：B1（L4-semantic）已 all-pass 后 L4-mechanical 报 unresolved → **并集 fail loud**，issues 标来源 `[mechanical]`/`[semantic]`；**B1 的 Accepted Deviations 列表须传给 L4-mechanical spawn prompt**（`Accepted IDs: [n], ...`），L4 对 Accepted IDs 不重复审计（否则机械层不认 Accepted 概念会误报 unresolved）。

## 4. 验收标准

A1. `workflows/subagents/kd-nas/project-fidelity-verifier.md` 存在，含 Input / Audit procedure / Deviation Judgment / 统一 ID / Output / Resumed Re-Check / 红线 段；Out-of-scope + Intended behavior 按 §3.1 KD 化；删除 RL/auxiliary/reward 段。

A2. `kd-train-script/agent.md` + `SKILL.md` Step 4 在 L3 与 L4-mechanical 之间插入 L4-semantic 收敛环；层命名 L4-semantic/L4-mechanical；`MAX_TURNS=3` 显式；ID 传递路径（解析上轮 IDs → render 下轮 `Fixed:`）显式；Unresolved → fail loud + ask-user；verifier 自身崩分支；每轮 apply fixes 后重跑 L1+L3。

A3. `train_script_verify/agent.md` 加 step 3.5（一次性 fidelity-verifier spawn，unresolved → verified=false 退非零）。

A4. `kd-nas.yaml` 两节点 `description` / 注释同步更新；output_schema **不变**（fidelity-verifier 是过程，不进节点 output）。

A5. **洁净**：B1 verifier .md 与改动后的 agent.md body 无开发期残留（无 plan/SPEC 编号、无 Orca 源码路径、无 `workflows/agents/...` 内部路径、无测试项目名）；警惕词清单见 §6。经 `tars validate` 的 `_check_prompt_dev_residue` 0 warning。

A6. **E2E 增量价值（S1 修正后的测试对象 + N3 固定 demo）**：用**固定 adversarial demo** `examples/mnist_kd_adversarial/`（决策 D3）：复制 mnist_kd，仅在叶子注入一处 L3 确定漏的偏差——推荐 `optim.py::build_optimizer` 用同类名 `Adam` 但 `weight_decay` 与用户原值不同（`fidelity_check.py:821-825` `OPT_TYPE_OK` 只比类名 → L3 PASS；fidelity-verifier 比 kwargs → 命中）。备选：`data.py` 丢一个 `Normalize` transform（`LOADER_SHAPE_OK` 只比 shape → L3 PASS；verifier 比 transform 内容 → 命中）。验证 L3 PASS 而 B1 命中。**用固定 demo 而非运行时 mutation**，CI 可复现。

A7. **E2E 收敛环**（M4 闭环）：固定 demo 注入 2 处 semantic findings → 生成节点 turn 1 发现、turn 2 修复后 `Fixed:[1],[2]` resume `STATUS: closed` all-pass，不超 `MAX_TURNS=3`。

A8. **E2E Unresolved 路径**（M4 闭环）：构造一个 verifier 无法判定的项目特定约束 → 转 ask-user 哨兵，不擅自改、不降级 pass。

A9. **E2E reaffirm 防呆**（N1）：构造一个「agent 越改越错」场景（fix 把 kwarg 加到错位置）→ verifier `STATUS: open` reaffirm → 第 2 轮 reaffirm 转 ask-user 哨兵，不盲目耗满 3 轮。

## 5. 决定（spec-reviewer + evaluator 闭环）

**已定（技术性，无歧义）**：
- **O2（独立副本 vs 共享）→ 独立副本**（语义差异非参数化能解：nas-supernet「搜索空间不审」vs KD「引擎+student 变体不审」；drift 防护：第三个项目出现再提共享）。
- **O3（MAX_TURNS）→ 3**（与 N1 reaffirm→ask-user 联动：同一 ID 两轮 reaffirm 即转 ask-user，不盲重试；overshoot = fail loud 下行有界）。**成本边界**（N9）：仅 gen_train_script 一次性前置节点用，不进 distill 循环，总成本上限 = 3 轮，不随 max_rounds 放大。
- **O4（probe fallback）→ 无**（KD 用户 train.py 几乎不可 import → probe 大多 skip → Runtime Fidelity `not verified` 属预期；B1 价值集中在 Static Fidelity；fallback 要 import user module 正是 KD 不可得，引入即诱发造假 probe）。
- **N6（KD 化清单）**：KD 版须从 nas-supernet 版**删除/替换**这些段（不只 Out-of-scope）：「RL environment fidelity」全删；「auxiliary networks（GAN discriminator / KD teacher）」→ KD teacher 改 out-of-scope；「reward formula / rollout buffers / discriminators」删；「subnet sampling / candidate loops」→ 换 KD 语境（loss term / optim kwargs / dataloader transform / eval metric 公式）。

**用户已拍板（2026-08-05）**：

- **D1（二闸禁 auto-fix）→ 禁用（report-only）**。二闸 spawn fidelity-verifier + workflow-verifier 时显式 `do not modify artifacts; report only`。二闸 = 独立复审层，发现即 fail loud 暴露 gen 漏判/叙述假 pass；auto-fix 留给 gen 节点。
- **D2（L4 加 ID）→ 不加**。D1=report-only 后，二闸 findings 自带来源描述，ID 路由价值减弱；不改 SKILL.md workflow-verifier 模板 / checklist，范围最小。L4 维持 `VERDICT: all-pass | unresolved`。
- **D3（固定 adversarial demo）→ 建**。新建 `examples/mnist_kd_adversarial/`（复制 mnist_kd，仅在叶子注入一处 L3 漏/B1 抓的偏差），CI 可复现。

## 6. 洁净契约（coder 必守）

按 [`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`](../../orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md)：

- B1 verifier .md 与改动 agent.md 的 body 是 **LLM 运行时指令**，禁出现：本 SPEC 编号 / plan 引用 / Orca 内部路径（`workflows/agents/_kd_scripts/...`）/ 测试项目名（`mnist_kd`）/ 过程推理（「借鉴 nas-supernet」「为了补 L3 盲区」）。**只留运行时它需要知道的**。
- Intended behavior 段写**运行时事实**（「KD 设计内差异：distillation loss 组合在引擎侧、叶子不携带 KD recipe」），**不写过程推理**（「因为 kd-nas 把生成和校验拆两节点所以…」）。
- **改编 nas-supernet 版时警惕的过程推理气味词**（M7）：`the nas-supernet analogue of` / `in contrast to supernet.py` / `ported from nas-supernet` / 「对应 nas-supernet 的…」——一律删。
- 受众翻转通读：把每个 .md 当「cold-start 召醒的 LLM 唯一能看到的指令」读一遍，删一切它不需要的历史/对比/动机。

## 7. 测试意图（E2E 覆盖）

- **意图 A（正向）**：正常 mnist_kd 叶子 → L4-semantic `all-pass`，workflow 不回归。
- **意图 B（增量价值，A6）**：注入 (a) data transform look-alike 或 (b) optim kwargs 漂移 → L3 PASS、B1 命中。
- **意图 C（收敛环，A7）**：2 处 semantic findings → turn 1 发现、turn 2 `Fixed:[1],[2]` resume all-pass，不超 `MAX_TURNS`。
- **意图 D（Unresolved，A8）**：项目特定不可判项 → ask-user 哨兵，不擅自改、不降级 pass。
