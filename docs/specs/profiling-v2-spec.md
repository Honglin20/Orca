# profiling-v2 SPEC —— 三面融合（结构/特征/loss）+ 机械派生 history

> 契约文档，逐字实现。前身 = `workflows/prof-opt`（v8 语义）的隔离副本
> `workflows/profiling-v2/`（2026-09-11 复制完成，`tars validate` 通过，副本内
> prof-opt 字样零残留；prof-opt 原目录零改动）。
> 用户 2026-09-11 拍板：①搜索空间扩为结构/特征/loss 三面，一个候选 = 一个有机
> 融合设计（selector 三面融合，不设技术目录，业务逻辑驱动的提示词）②蒸馏一切
> 形式禁止③时延门 = 纯推理时延（预处理时延不计，测量链路零改动）④出口 shape/
> 语义焊死，输入 shape 可变⑤facet 能力不可用时**静默降级**（workflow 不退出）
> ⑥history.md = 每轮 ≤2 句的机械派生总结层并推 web，取代 v8 的 LLM 叙事层
> （history-curator / digest_stamp 退役）。
> 经 spec-reviewer 两轮对抗审查闭环（2026-09-11，6 BLOCKER / 6 MAJOR / 10 MINOR
> 全部落入本稿；原「medium/high ⇒ 必须带精度 facet」emit 门规则被证伪删除，
> 改为 selector 提示词软指引 + `distillation_free_ack` 声明，见 §3 Step 2）。
> 改动完全限 `workflows/profiling-v2/**` + `tests/test_pv2_*` + 本 docs。
> 引擎（orca/）零改动。

## 0. 语义总纲

- **门不变**：准入线恒 = `origin_anchor.baseline_makespan_cycles`（严格低于），
  精度 = `gap ≤ accuracy_budget`。时延量纲 = 纯推理（mfu_benchmark 对导出
  ONNX 的实测，与 v8 完全同链）；特征/预处理耗时**不计入**时延账（用户拍板，
  交付物携带特征管线即部署方自证途径）。
- **三面搜索空间**：`structure`（模型源码）/ `features`（特征管线文件）/
  `loss`（loss 定义文件）。**facet 副本全程住在 shadow 之外**：origin 副本落
  工作区根 `facets/`（保持相对路径结构），变体可编辑副本落
  `variants/<vid>/facets/`——`shadow/` 恒 origin 树零接触（锁 sha256 全量
  比较、`list_shadow_pkgs` 顶层包枚举、`assert_shadow` 断言、diff_check 双树
  扫描四套既有机制因此零改动）。`loss` / `features` 永远不能脱离 `structure`
  单独成 facet（见 §3 R1）；`loss` 零推理收益，本质只能是精度侧facet。
- **不变量**：输出 shape/语义焊死（变体导出 ONNX 输出 shape == origin，指标
  算在输出上，输出变则指标不可比）；eval 数据集与指标定义冻结；输入 shape
  **允许**随特征 facet 变化；opset17 静态 shape 导出照旧（per-variant 输入
  shape 合法，`dynamic_axes` 仍禁）。
- **能力自检 + 静默降级**：facet 可用性由 po_contract **机械证明**（非 LLM
  判断），落 `contracts.json` 的 `facets` 块。不可用 facet 自动收窄搜索空间
  ——不 exit、不报错，但 contracts.json 与 report 首段**必须披露**能力矩阵
  （静默 = 不中断，不 = 隐藏）。注意与 description 中 mfu 链的「无静默降级」
  是两回事：后者指 profiling 不降级，前者指搜索空间按能力收窄——
  workflow.yaml 重写时两条语分列。
- **蒸馏全形式禁止**：logits / 中间特征 / 关系型知识迁移一律不得出现在提案、
  实现、提示词中；提案必须携带 `distillation_free_ack: true` 显式声明
  （§3 Step 2）。
- **历史双层（取代 v8 digest）**：`history.jsonl` 仍是唯一真相（append-only，
  impl 行扩三个 facet 短语字段）；`base/history.md` 是**机械渲染的派生视图**
  （`render_history.py`，数据源仅 history.jsonl + train_status.json + rounds/
  目录），每轮 ≤2 句，推 web。**绝不**让 LLM 写 history.md。
- **隔离铁律不变**：用户原文件全程只读；`shadow/` 恒 origin 树；每轮变体
  独立快照（含 facets 副本），一切编辑只落 `variants/<vid>/`；胜出者
  写回才触碰用户项目，新文件名 `<stem>_profiling_v2` 后缀、冲突不覆盖。
- **谱系字段划分**：`edited_files` 恒指 shadow 内模型闭包编辑（v8 语义不变，
  ⊆ shadow 校验与 diff==edited 等值门零改动）；facet 编辑由**新增**字段
  `facet_edited_files` 承载（§2.1 / §3 R3 / §3 Step 3 recheck facets 等值门）。
- **锁版本不 bump**：v2 按 workflow 名隔离工作区（`artifacts/profiling-v2/`），
  不存在 v2 旧版工作区；prof-opt 的 version-3 锁工作区在 `artifacts/prof-opt/`，
  v2 永不读取。`BASELINE.lock` version 恒 3。

## 1. facet 能力检测（po_contract）

**定义（可判定，不诉诸感觉）**：

- `features` facet 可用 ⇔ 「原始数据 → 模型输入张量」的变换可定位为**有限个
  用户项目文件**（相对 project_root 的路径集合），且 **train、eval 模板**均可
  参数化指向一个可编辑副本、swap 后全链仍跑通；**导出输入若必须经特征管线
  构造**，export 模板同样必须可参数化——否则 features=false。该 export 侧
  判定独立落盘为 `export_consumes_features`（schema 见下），export 模板的
  token 携带与渲染供给系于它（§4，与 features 布尔同一来源不双轨）。
- `loss` facet 可用 ⇔ 训练 loss 的定义可定位为有限个文件（或训练入口内可
  整体摘出的定义段——以 A/B/C 档参数化判定已有的适配入口机制承载），且模板
  可参数化指向可编辑副本。

**流程（全部落盘，证明式而非声明式）**：

1. **定位**：contract 勘察训练入口的 import 图与 eval/export 数据路径，产出
   候选 facet 文件集（零个 = 直接 facet=false）。
2. **复制**：facet 文件复制进 **`$ART/facets/`**（工作区根，shadow 之外，
   保持相对路径结构），origin 绝对路径 + sha256 记入 `contracts.json` 的
   `facets` 块。`shadow/` 零接触（§0）。
3. **证明**：以参数化模板做 **≥1 epoch 适配 dry-run**（train + eval 双跑，
   一跑双用复用 v8 的实测快跑机制；**独立证据落
   `contract_work/facet_dryrun.json`，不改 `train_quickrun.json` 的既有字段
   契约**）：训练读 `facets/` 下的特征管线与 loss 定义、eval 喂同一特征管线，
   指标行格式与 ckpt 行为正常。**证明失败（跑不通 / 指标路径断裂）⇒ 对应
   facet=false**，已复制文件保留无害（shadow 之外，不参与任何锁/枚举/断言）。
4. **落盘**：`contracts.json.facets = {"features": bool, "loss": bool,
   "export_consumes_features": bool, "facets_dir": "facets",
   "feature_files": [...], "loss_files": [...],
   "origin_hashes": {...}, "evidence": "contract_work/facet_dryrun.json"}`。

**facet 指纹的复用校验输入面（与 profile_chip 同轨）**：`check_contracts.sh`
的复用校验在既有 profile 三参数之外增加 facets 块——从
`contracts.json.facets.origin_hashes` 记录的路径**重算用户项目 facet 文件
sha256** 并与记录比对 + 校验 `$ART/facets/` 副本在场且逐文件 hash 一致；
任一 mismatch → 披露并要求 `fresh_start=true`（同 profiling 参数轨道）。
`VERSION_KEYS` 增 facets 块版本判别。propose Step 0/1 读到缺 `facets` 块的
`contracts.json` → fail loud（撕裂工作区）。facet 指纹不入 BASELINE.lock
（锁写于 flatten，先于 facet 检测）。

## 2. history.jsonl 扩展 + `render_history.py`

### 2.1 schema 扩展（history_lib.py）

- `IMPL_FIELDS` 追加三个短语字段：
  `structure_change` / `feature_change` / `loss_change`（str，**≤80 字符
  （80 个 Unicode 码点，`len(str)` 判定）**）。取值：改动一句话总结，或字面
  `未改`，或 facet 不可用时的字面 `n/a (facet unavailable)`。
- `append_impl_row.py` CLI 增加 `--structure-change` / `--feature-change` /
  `--loss-change`（默认 `未改`）；超 80 码点 exit 2。
- terminal 行 schema 零改动（facet 短语只住 impl 行；渲染层按 vid join）。
- **`facet_edited_files`**：`declaration.json` 新增字段（str 数组；变体
  facets 副本内被编辑文件的相对路径集）。与 `edited_files` 分轨：后者恒
  shadow 内模型闭包（既有 ⊆ shadow 校验与 diff==edited 等值门零改动）。
  消费门见 §3 R3 与 §3 Step 3 recheck facets 等值门。
- 洁净性：v2 工作区不存在旧 schema 行（工作区按名隔离，无迁移逻辑，不做
  兼容读取——缺字段的历史行 = 撕裂，render fail loud）。

### 2.2 `render_history.py`（新脚本，_po_scripts/，deploy_scripts.sh 自动收编）

```
用法: render_history.py --artifacts <ws>
效果: 原子写 base/history.md（tmp + os.replace），stdout 打印同一内容；失败 exit 2
```

- **数据源仅三处**：`history.jsonl`（history_lib.read_rows）+
  `variants/<vid>/train_status.json`（in_flight 行）+ `rounds/` 目录（轮枚举，
  与 round_state.py 同谓词：纯数字目录名）。零训练侧改动，零 LLM。
- **轮集合** = 1..current（current = rounds/ 下最大纯数字目录）。每轮渲染
  一个块：
  - 该轮**无任何 history 行** → `### r<N> · 无提案 · 详见 rounds/<RRR>/analysis.md`
    （只给指针，不读内容——零提案轮的 rationale 在 analysis.md，机械不解析）。
  - 该轮有 vid → 每个 vid 一块（同轮多 vid 按 seq 排序；v2 一轮一 vid，多块
    仅在异常重放出现，照实渲染）：
    ```
    ### r<N> · <vid> · <outcome> · 时延 <Δ%> · acc <final_acc>（gap <g>）
    结构: <structure_change 短语>；特征: <feature_change 短语>；loss: <loss_change 短语>
    ```
  - `outcome` 取该 vid 最新终态行（success / accuracy_fail / latency_fail /
    probe_insufficient）；`latency_improved` 且无终态行 → `in_flight · stage
    <s> · epoch <e> · gap <g>（<risk>）`——谓词与 risk 分级**以 import 复用
    `frontier_snapshot.py` 的谓词函数（同目录部署），禁止复制谓词逻辑**（含
    pending_launch 态与 unparseable fail loud，单一实现防漂移）。
  - `时延 <Δ%>` = (variant_makespan − baseline_makespan) / baseline_makespan，
    方向带符号百分数，数据源：终态行 `makespan_cycles` / 实测行
    `makespan_cycles` / latency_fail 行 `measured_makespan_cycles`；分母 =
    `origin_anchor.baseline_makespan_cycles`。无任何 makespan 的行渲染 `时延 -`。
  - facet 短语从该 vid 最新 **impl 行**取；impl 行缺失（理论不可能：终态行
    前必有 impl 行）→ fail loud。
- 文件头固定一段说明（本文件是机械派生视图、真相在 history.jsonl、生成脚本
  名），防止误当手写文档续写。
- **fail loud 清单**：history.jsonl unparseable；origin_anchor 缺失/unparseable
  /缺字段；在飞 vid 的 train_status.json unparseable；impl 行缺 facet 字段。
- 文件尾 `render_stamp`：`{"lines": <history.jsonl 总行数>, "last_line_no":
  <末行行号>}`；零行历史 = `{"lines": 0, "last_line_no": 0}`（round 1 合法态）。
  供 emit 门核对新鲜度（§3 Step 4）。

### 2.3 web 推送（push_curves.py）

- v8 的 `_DIGEST_ROW`（digest 层行）替换为 `("digest", "history.md",
  "base/history.md")`——digest 已退役，history.md 接位。
- 分析文档 manifest 增加 `contracts.json` 的 facets 能力行（能力矩阵运行中
  可见，静默降级不隐藏）。
- candidates manifest：`facets.features == true` 时增加
  `candidates/feature.md`（与三个结构候选同列）；false 时维持三键。
- 图表类型与其余推送时机零改动（emit 门 best-effort + report 终推）。

## 3. po_propose 契约改动

### Step 0（开局机械）

`frontier_snapshot.py` + **`render_history.py`**（新增，读前刷新）+
`round_state.py`。**digest seal 检查删除**（digest_stamp.py 退役）。断点续跑
判定照旧（parseable proposals.json 复用）。`contracts.json.facets` 块缺失
→ fail loud（§1）。

简报（读入上下文的全部历史/前沿证据，**替代** v8 的三件套）：

1. `base/frontier.json` 全文（数字层：frontier / in_flight / avoid）
2. `base/history.md` 全文（人话总结层：每轮 ≤2 句 + 数字）
3. `accuracy_rules` 快照（照旧）

**history_digest.md 直读、raw history、前序 variant MFU 报告照 v8 一样出局**；
v2 再删掉 digest 本身——上下文不随轮数膨胀且比 v8 更小。

### Step 1（并行候选）

- 三个结构提案者（semantic / hardware / sota）照旧派出，brief 照旧。
- **`feature-architect`（新 subagent）**：`facets.features == true` 时**固定
  派出**（确定性规则，不由主 agent 临时判断）；产出
  `candidates/feature.md`，与结构候选同 schema（信息不变量 / 根因 / 受影响
  文件 / 时延机制 / 风险 / 实现草图 / absorbs / 业务依据），竞争进同一融合池，
  由 selector 裁决。brief 的指导方向（提示词，**不设技术目录**）：
  > 从 `business_logic.md` 与 `information_analysis.md` 出发审视当前特征的
  > 缺点（冗余？与业务无关？昂贵变换可否前移出推理图？），提出输入侧设计，
  > **优先给出特征与模型结构联动的方案**（特征瘦身必须连带输入层适配，接口
  > 一致由脚本校验）。蒸馏禁止。
- **loss 审视指令**：`facets.loss == true` 时写进 selector 的 brief（loss
  想法不设独立提案者，由 selector 在融合时判断）：
  > 审视当前 loss（定义见 `facets/` 与 contracts.json 定位）的缺点、与业务
  > 逻辑可结合之处，判断本设计是否需要 loss facet。蒸馏禁止；loss 单独不成
  > 候选；当主设计的 `predicted_acc_impact` 偏高（medium/high）时，应主动
  > 考虑引入可用的精度侧 facet（特征或 loss）为结构改动托底。
  > （最后一句是**提示词软指引**，不是门——门的确定性规则见 R1-R5。）
- facet 不可用时：对应指令与 subagent **整个缺席**（不是降级为「请忽略」），
  impl 行对应短语固定 `n/a (facet unavailable)`。

### Step 2（selector 融合）

- 融合语义升级：把候选（结构 ×3 + 特征 ×1）与 loss 判断融合为**恰一个有机
  设计**——三面要么互相成全（如特征瘦身 + 输入层收窄 + 去相关正则），要么
  不如不 bundle；禁止不相关改动的拼盘。`architecture_decision.md` 照旧带
  `## absorbs` / `## avoids` 两节。
- `proposals.json` 单提案字段扩展：`facets: {structure: bool, features:
  bool, loss: bool}`（≥1 true 且 structure 恒 true，见 R1）+ 三个 facet
  短语字段（与 impl 行同规，≤80 码点）+ **`distillation_free_ack: true`**
  （必填布尔；缺失或 false → 非法；与 v8 `admission_clause_ack` 同轨同构
  ——蒸馏不做机械检测，残余风险以显式声明 + 提示词层约束披露）。
  `parent_vid`/`base_at_proposal` 保持退役，`absorbs` 语义照旧。
  `predicted_acc_impact` 回归 v8 纯披露语义（不承载门逻辑）。
- **确定性规则（emit 门机械校验，R1-R5）**：
  - **R1** `facets.structure == false` → 非法。理由：纯 loss 不入推理图、
    零时延收益；纯 features 必然要求输入层适配（接口换不动 = 管线换不上），
    输入层编辑本身就是 structure facet。structure-only 恒合法（v8 语义）。
  - **R2** facet 声明 true 但 `contracts.json.facets` 对应 false → 非法
    （声明了工作区不具备的能力）。
  - **R3** `facet_edited_files` 双向校验：`facets.features == true` ⇒ 含
    feature facet 至少一文件；`false` ⇒ 不含任何 feature facet 文件；loss
    同理（声明与编辑清单互证）。
  - **R4** facet 不可用 ⇒ 对应短语恒 `n/a (facet unavailable)`；可用 ⇒
    禁止该值。
  - **R5** facet 声明 true ⇒ 对应短语 ≠ `未改`；声明 false ⇒ 短语 ==
    `未改`（防 `features=true + feature_change=未改` 撕裂态；R4 支配——
    不可用 facet 的短语恒 `n/a (facet unavailable)`，R5 的「未改」仅适用
    于能力在场而未声明的态）。
- `check_propose_emit.py` 增加：R1-R5 全集 + facet 短语在场/长度帽/取值
  合法（`未改` / `n/a (facet unavailable)` / 非空 ≤80 码点）+ 行-提案 facet
  短语等值（对称，全 outcome 路径，同 absorbs 等值检查模式）+
  `distillation_free_ack` 校验。
  **candidates manifest 哨兵条件化**：候选文件哨兵键集 = 三结构候选 +
  （`facets.features == true` ? `candidates/feature.md` : ∅；字典键 = 该相对
  路径，哨兵字面值 = `[subagent:feature-architect v1 <tag>]`（与全仓哨兵同款
  格式，`check_baseline_docs.sh` 一处一真相惯例），字面值住
  `check_propose_emit.py`，`feature-architect.md` 首行同值）。**seal 校验删除**（含 `import digest_stamp` 与其调用点），
  代之以 `render_history.py` 新鲜度核对：`render_stamp` 与当前
  history.jsonl 行数/末行号比对；**stale → 重跑 `render_history.py` 一次
  再验，仍 stale → fail loud**（对应 v8 curator re-dispatch once 语义）。

### Step 3（实现 + 实测）

- `variant-implementer`：编辑面扩展到 `variants/<vid>/facets/`（变体 facets
  副本）；`declaration.json` schema 增加 `facets` + 三个 facet 短语 +
  `facet_edited_files`（与 proposals.json verbatim 一致）。蒸馏禁止照旧入
  其契约。
- `variant-assessor`：软对齐范围含 facet 文件（改了什么就要对得上什么）。
- **`facet_check.py`（新脚本）**：
  ```
  用法: facet_check.py --artifacts <ws> --vid <vid>
  职责①(无条件): 变体导出 ONNX 输出 shape == origin base/model.onnx 输出
          shape（出口焊死，含 structure-only 变体——输出断言对每轮每个变体
          生效，不随 facets 收窄）
  职责②(仅 contracts.facets.features == true): 以 eval 契约的
          `eval.sample_inputs`（[{"name","shape","dtype"}]）+
          `eval.facet_builder`（facets 目录内 {"module","factory"} 可调用，
          raw 样本按名进、张量序列/dict 出）在变体特征管线
          （variants/<vid>/facets/）上构造输入张量，与变体 ONNX 输入逐维
          比对 shape/dtype（两键由 po_contract 在 features 证明通过后实测
          落盘；features 不可用则两键均不写）
  exit 0 = 通过；exit 2 = fail loud（stderr 根因）
  ```
- **facet_check 时序**：implementer 返回后、**impl 行与 DONE marker
  写入前**执行。失败 ⇒ 不写 `implemented=True`、不写 DONE，改走既有
  `append_impl_row.py --not-implemented --outcome structural_mismatch` 通道
  （`history_lib.append_outcome`，joint retry ≤2 与 variant_broken 共享预算
  ——不存在 implemented=True→False 的 merged-snapshot 版本回退）。
- `diff_check.py`：**零改动**（file 层职责照旧；输入 shape 差异的判定不在
  diff_check——由 facet_check 职责②承载）。
- **recheck facets 等值门**：`run_latency_recheck.sh` 在既有 shadow 双树
  diff 之外，对 `$ART/facets` vs `variants/<vid>/facets` 目录对**复用
  `diff_check --layer file` 再跑一次**（脚本本体零改动，传参即可），结果须
  与 `declaration.json.facet_edited_files` 等值（与 shadow 侧 diff==edited
  门同构）——不等值 = 捏造/漏报 facet 编辑 → structural_mismatch 修复环。
- 其余照旧：mfu-analyzer、`run_latency_recheck.sh` 时延判定（推理口径，零
  改动）、修复环 ≤5、latency_fail 终态行 → avoid 机械派生。
- **`archive_round_shadow.py`**：每轮归档范围纳入 `variants/<vid>/facets`
  （轮次留存「verdict 背后的确切代码」含 facet 面）。

### Step 4（记账出场）

impl 行带三短语落账（`append_impl_row.py` 新参数）发生在 **Step 3 末端**
——facet_check 通过后、DONE marker 移交前（DONE 写权归 propose caller，
使「失败 ⇒ 不写 implemented=True、不写 DONE」的通道可机械达成）；本 Step
链：`analysis.md` 照旧 → `render_history.py` 刷新（历史层就此新鲜）→
`check_propose_emit.py`（R1-R5 + history.md 新鲜度）→ docs manifest
best-effort 推送（含 history.md + facets 能力行 + feature.md 条件行）→
emit。**history-curator 派发、digest_stamp 封印全部删除。**

`po_propose/agent.md` 文档级改动：Invariants 段删除 digest 相关条
（history_digest/digest_stamp 叙事层描述），替换为 history.md 派生视图条；
Step 4 的 curator 派发与封印段整段删除。

## 4. 其余节点

- **po_flatten**：零改动（facet 定位归 contract；`facets/` 在 shadow 之外，
  锁/枚举/断言/diff 四机制天然无感；锁逻辑不动，version 恒 3）。
- **po_contract**：§1 全部；**模板 facet 参数（token 按消费面携带）**：run
  模板增加单一 token `<<facet_dir>>`（features/loss 两 facet 同根注入，目录
  内保持相对路径结构），携带条件**逐模板钉死**（与 §1 同一规则来源，
  `check_contracts.sh` 的 token 清单校验照此条件化）：
  - **train 模板**：`features ∨ loss` 任一可用 ⇒ 带 token
  - **eval 模板**：`features` 可用 ⇒ 带 token（eval 只消费特征管线，不引用
    loss）
  - **export 模板**：`export_consumes_features == true` ⇒ 带 token（此时
    features 恒 true——不可参数化即 features=false 且不带 token）；false ⇒
    模板无 token
  两 facet 均不可用且 export 不消费特征 ⇒ 三模板全无 token、渲染点不供给。
  **渲染调用点的 facet 参数值（全列——模板带 token 而调用点缺供给 =
  render_run.sh 未替换 token fail loud）**，train/eval 四点 + export 三点：
  1. contract 期 dry-run（train/eval 渲染）→ `facet_dir=$ART/facets`
  2. baseline 链（po_baseline 训练/eval 渲染）→ `facet_dir=$ART/facets`
     （基线恒用 origin facet）
  3. po_probe 训练 wrapper 渲染 → `facet_dir=$ART/variants/<vid>/facets`
  4. **watchdog `render_eval`**（final_eval 与 k_eval 共用）→
     `facet_dir=$ART/variants/<vid>/facets`（watch_variant.py 的 `--set`
     清单增此项——§8 唯一例外）
  5. contract 期 export 契约证据渲染 → `facet_dir=$ART/facets`
  6. baseline 早期链影子导出渲染 → `facet_dir=$ART/facets`
  7. propose 内 implementer 变体导出渲染 →
     `facet_dir=$ART/variants/<vid>/facets`
  （5-7 仅 `export_consumes_features == true` 时存在供给；false 时 export
  模板无 token，三点照 v8 无参渲染）
  **eval 模板必须喂变体特征管线**（train 与 eval 同管线，指标可比性前提，
  写进 eval 契约）；**出口焊死检查**由 facet_check 职责①承载（§3，每变体
  无条件）。
- **po_baseline**：`business-logic-analyst.md` 与 `information-analyst.md`
  两个 subagent 模板扩展——分别增「特征现状」章（business_logic.md 内）与
  「特征信息量与 loss 现状」章（information_analysis.md 内），**仅覆盖可用
  facet**（章集随 `contracts.json.facets` 条件化；两 facet 均不可用则不加
  章节）。`check_baseline_docs` 门措辞 = 必需章集随 facets 条件化（非一律
  放行）。这是 Step 1/2 业务驱动提示词的证据基础。基线训练/锚语义零改动
  （基线恒用 origin 特征与 loss，渲染点 2 的 facet_dir 即此语义）。
- **po_probe**：逻辑零改动，唯渲染 `--set` 扩展一处 `facet_dir`
  （渲染点 3）。
- **po_gate**：`gate_node.sh` 决策前增加 `render_history.py` 调用——
  **stdout 必须 `>/dev/null 2><err 文件>` 重定向**（po_gate `parse_json:
  true`，stdout 纯净是硬约束）；非零退出 → 映射进既有 finish-failed payload
  （与 frontier snapshot 失败同款处理，`reason` 注明 history render failed）。
  `gate_decide.py` 决策序零改动。
- **po_report**：写回集合扩为最多三件——变体模型文件 + facets 特征文件 +
  loss 文件（facet 不可用则对应件缺席），`_profiling_v2` 后缀、冲突不覆盖、
  写回前复验结构锚锁（既有机制）；**facet 件写回前按
  `contracts.json.facets.origin_hashes` 复验用户项目 facet 文件未漂移，
  漂移 → conflicts 条目不写**（与模型件 lock 复验同构）；报告附
  `history.md` 全文作轮次总结附录；**首段固定披露增加 facet 能力矩阵**
  （features/loss 各自可用与否 + dry-run 证据路径）；**三件配组披露**：
  明示模型/特征/loss 三件须同组接线采纳（单取模型件而弃特征件不可运行）。
  winner.lineage 照旧（absorbs）。
- **workflow.yaml**：description 重写为三面融合语义（三面 / 门不变 / facet
  能力自检与静默收窄披露 / history 派生视图 / 蒸馏禁止 / 出口焊死）；
  **mfu 链「无静默降级」与 facet「静默收窄」两条语分列**（同词不同指，
  显式消歧）；7 节点注释同步。

## 5. 删除清单（v2 副本内）

- `_po_scripts/digest_stamp.py`（deploy_scripts.sh 的 orphan retirement 机制
  自动清理已部署工作区的旧副本）
- `subagents/history-curator.md`
- `base/history_digest.md` / `history_digest.stamp.json` 语义（v2 工作区不
  产生、不读取；seal 检查随删）
- **digest 消费面全清单**（逐处，防漏）：`check_propose_emit.py` 的
  `import digest_stamp` 与 seal 校验调用；`po_propose/agent.md` Invariants
  段 digest 条目 + Step 4 curator 派发/封印段；`push_curves.py` 的
  `_DIGEST_ROW` 行（→ history.md 行，§2.3）。

## 6. 新增 / 扩展清单（v2 副本内）

- 新增：`_po_scripts/render_history.py`（§2.2）、`_po_scripts/facet_check.py`
  （§3 Step 3）、`subagents/feature-architect.md`（§3 Step 1；产品说明书体，
  无开发考古，洁净契约自查 warning 清零；首行哨兵与 `check_propose_emit.py`
  哨兵字面值同值——一处一真相）
- 扩展：`subagents/business-logic-analyst.md`（特征现状章模板）、
  `subagents/information-analyst.md`（特征信息量与 loss 现状章模板）、
  `subagents/variant-implementer.md`（facets 编辑面 + declaration schema）、
  `subagents/variant-assessor.md`（facet 软对齐）、
  `subagents/architecture-selector.md`（三面融合 + loss 审视指令 +
  facet 声明 schema）

## 7. 测试契约

- `tests/test_pv2_history.py`（新）：渲染各 outcome 块 / in_flight 态（含
  pending_launch；谓词 import 复用断言）/ 无提案轮指针 / 时延百分比带符号 /
  facet 短语 join / ≤80 码点帽 / 原子写 / render_stamp（含零行态）/ fail
  loud 矩阵（history 撕裂 / 锚缺失 / impl 行缺 facet 字段 / train_status
  unparseable）。
- `tests/test_pv2_facets.py`（新）：R1-R5 全矩阵正负路径；facet 短语等值
  检查（对称）；`n/a (facet unavailable)` 固定值（可用/不可用两向）；两
  facet 均不可用时规则悬挂的正路径；`distillation_free_ack` 缺失/false
  拒绝；feature.md 哨兵条件化（有/无 features）；facet_check 职责①②
  正负（出口不等 / 输入不等 / structure-only 走①）；facets 目录 diff
  等值门正负；**锁/枚举/断言零改动回归**（`facets/` 落地后二次 reuse 通过）；
  **模板 token 按消费面正负**（train/eval/export 三条件各自满足时全调用点
  供给渲染通过、不满足时无 token 照 v8 渲染通过；带 token 而调用点缺供给
  = fail loud 负路径）；gate stdout 纯净（render_history 重定向后 po_gate
  parse_json 不受污染）。
- 迁移：从 `tests/test_po_scripts.py` / `tests/test_po_v8_frontier.py` 复刻
  受影响用例改 v2 路径与契约（IMPL_FIELDS 扩展 / seal 删除 / digest 缺席 /
  `_DIGEST_ROW` 替换）；**prof-opt 既有套件零接触零恶化**（含
  `tests/test_po_history_digest.py`——它指向 prof-opt 原目录，v2 不触碰；
  HEAD 既有失败 `test_gate_node_sh_parses_after_quote_fix` 与 v2 无关，
  不计入）。

## 8. 非目标

引擎（orca/）、web 前端组件、rules_pool 机制、dashboard_snapshot /
experiment_ledger（仅随副本改名被动携带）、mfu_benchmark 链路、prof-opt
原目录、锁版本。**训练侧/watchdog 非目标开唯一例外**：`watch_variant.py`
的 `render_eval` `--set` 清单增加 `facet_dir` 一项（文件在
`workflows/profiling-v2/agents/_po_scripts/` 内，引擎零接触）。push_curves
仅限 §2.3 三处（digest→history 行替换、facets 能力行、feature.md 条件行）。
特征/loss 的**具体技术目录**不做（用户拍板：提示词业务驱动，目录化留给未来
证据积累）。
