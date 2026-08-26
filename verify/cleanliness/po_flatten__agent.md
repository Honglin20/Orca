# po_flatten/agent.md 洁净审查记录（prof-opt v4）

- **对象**：`workflows/agents/po_flatten/agent.md`（474 行）
- **依据**：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众翻转通读）+ `docs/specs/prof-opt-v4-spec.md` §4 po_flatten 行（v4 契约 = 零改动，锚 = v3.5 语义）
- **方法**：全文受众翻转通读（运行时执行者视角）+ 残留词表 grep + 契约逐条核对 + 引用资源存在性/哨兵/schema 机械核验
- **日期**：2026-08-25

---

## ① 逐段受众翻转结论表

| 段（行号） | 结论 | 备注 |
|---|---|---|
| frontmatter 1-4 | PASS | 产品语义 description；tools 清单 |
| 职责陈述 5-22 | **FINDING F1（16-18）** | 其余为可执行职责清单；F1 见下 |
| Resource Anchors 24-46 | PASS | `$ORCA_AGENT_RESOURCES`/`$ORCA_ARTIFACTS_DIR` 为引擎注入 env；`_po_scripts` 守卫式定位（解析失败 exit 2）是 v3.5 SPEC header 硬契约的落地形态，非内联确定性代码违规 |
| Path Iron Rules 48-58 | PASS | pathlib 强制，正反例齐全，可机械执行 |
| Workspace Layout 60-72 | PASS | 产物布局图，全部条目在 Workflow 步骤中有对应产出指令 |
| Subagent Call Protocol 74-81 | PASS | point-to-file 协议自包含（sentinel 机制在 md 文件自身，prompt 只指路不内联哨兵内容）；`{{ subagents_root }}` 为引擎渲染变量（v3.5 SPEC §3-4 骨架同款） |
| Lazy Loading 83-88 | PASS | 「不预读 + `PROFILER_CONTRACT.md` 本节点不需要」是运行时注意力指令 |
| Required Inputs 90-102 | **FINDING F2（100）** | 四类 inputs 与 `workflows/prof-opt.yaml` inputs（12 个）实际存在的五项一致；无悬空引用；F2 见下 |
| Pipeline Memory 104-124 | PASS | manifest 骨架 + Interpreter/metric-direction 两事实均为下游契约，可执行 |
| Workflow 总则 126-131 | PASS | todolist / at-least-once / 幂等重入语义，运行时指令 |
| Step 0 Reuse Gate 133-166 | PASS | 退出码映射完整（0/1/2/3）；REUSE 路径磁盘机械重derive；fresh_start 全工作区 wipe 的理由句（"leftovers from an older run would silently false-gate"）是运行时操作边界说明，非事故复盘 |
| Step 1 Deploy 168-180 | PASS | `deploy_scripts.sh` 存在于 `_po_scripts/`；JSON stdout 校验点明确 |
| Step 2 Survey 182-202 | **FINDING F3（200-202）** | 解释器探测/事实采集/manifest 写入均可独立执行；F3 见下 |
| Step 3 Shadow+Lock 204-308 | PASS | 闭包追踪/两种 copy form/排除规则/机械枚举/stdlib 预检/BASELINE.lock 六字段均可执行。内联 python heredoc（stdlib 预检、lock 写入）经核对为 v3.5 SPEC §4 po_flatten 行钦定的 agent 侧实现（节点 scripts/ 仅三件，预检/lock 逻辑 spec 明文落在 prompt），**非**洁净契约 §4 内联确定性代码违规；BASELINE.lock 四字段 `{model_path, pretrained_ckpt, ckpt_sha256, py_files_sha256}` 与 v3.5 契约逐字一致 |
| Step 4 assert_shadow 310-325 | PASS | `assert_shadow.py` 存在；env 名（ORCA_SHADOW_DIR/PKGS、PYTHONPATH 组装）与 v3.5 SPEC §1 一致；失败归因方向明确（修 Step 3 不修 assert） |
| Step 5 Readiness 327-390 | PASS | 四 gate 定义完整；readiness.json schema 逐字段钉死；check 3 vacuously-true 的表述是**当前事实陈述**（非历史考古）；`render_run.sh --template/--out/--set{shadow_dir,shadow_pkgs,project_root}` 旗标与脚本实参逐一核对存在 |
| Step 6 Flat View 392-400 | PASS | 可选分析视图 + "nothing may import or run the flat file" 边界明确 |
| Validation 402-414 | PASS | `check_flatten.sh` 存在；fix-loop ≤3 + fail loud 出口 |
| memory-verifier 416-438 | PASS | 哨兵字面量 `[subagent:memory-verifier v1 MF6TQ9]` 与 `workflows/subagents/prof-opt/memory-verifier.md` frontmatter（subagent/version/sentinel）**逐字符一致**；报告落盘校验 + 纠错回灌 Validation 的闭环自包含 |
| Guidelines 440-449 | PASS | 只读边界/产物保留/英文标识符/stderr 日志纪律 |
| Output 451-473 | PASS | emit 字段 8 个与 `workflows/prof-opt.yaml:113` po_flatten `output_schema.required` **完全一致**；失败路径同 emitter；`EMIT_PY` 回退覆盖 REUSE 路径 |

## 机械核验（词表 grep + 契约一致性）

- **残留词表 grep**（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft，i 大小写不敏感）：**0 命中**。增补（懒补训 / epoch-only / proxy / perturb / mfu / auto.train / v3 / v4 / removed）：仅 "Lazy Loading"（章节名，合法）、"removed"（→F2）、"verify/memory_verifier_report.md"（路径名，合法），无机制残留词。
- **v4 SPEC §4 po_flatten 行（零改动）逐条核对 v3.5 SPEC §4 行**：ns3 骨架复用 ✓；deploy_scripts 落 scripts/+orca_inject ✓；shadow_pkgs 机械枚举（Step 3.4 bash + Step 0 REUSE 重derive 双处）✓；stdlib 撞名预检 ✓；BASELINE.lock 四字段 + py 限 shadow 闭包 + 未提供双空串 ✓；.run_lock 心跳（gate 与 validation gate 刷新，agent.md 层语义一致，实现归脚本）✓；pretrained_loadable 未提供恒真（Step 5 check 3）✓；memory-verifier 报告入 generated_artifacts + 哨兵不符 fail loud ✓。**无一处与 v4 契约冲突，无 v4 已删机制措辞。**
- **引用存在性**：`_po_scripts/{deploy_scripts.sh, assert_shadow.py, render_run.sh, emit_result.py}` 与节点 `scripts/{reuse_check.sh, check_flatten.sh, extract_user_pkg.sh}` 全部在盘。

## ② Findings 清单（3 条，均 LOW；只记录不修复）

| # | 位置 | 问题 | 建议修法 |
|---|---|---|---|
| F1 | `workflows/agents/po_flatten/agent.md:16-18` | 职责陈述含 "(and, when a pretrained checkpoint is provided, that it is loadable — reference-only, never a training starting point)"——本 pipeline 无该 input（行 100 明示 + yaml 12 inputs 无此项 + PO_CKPT 恒 ""），该分支运行时**不可达**，是被删 pretrained-ckpt input 机制的措施残留，且与行 100 同文内不一致，徒耗执行者注意力 | 删该括号从句，或改为陈述当前事实："checkpoint anchor fields (`pretrained_ckpt`/`ckpt_sha256`) stay empty in this pipeline; training always starts from a fixed-seed random initialization" |
| F2 | `workflows/agents/po_flatten/agent.md:100` | "No pretrained-checkpoint input exists **(removed)**"——括号 "(removed)" 是版本考古（契约 §4「版本考古」类：只有知道开发史才看得懂；执行者需要的是现状事实，不是删除事件） | 删 "(removed)" 二词，语句其余部分自足 |
| F3 | `workflows/agents/po_flatten/agent.md:200-202`（对照 96-97） | `extract_user_pkg.sh "{{ inputs.project_root }}/{{ inputs.model_path }}"` 字符串拼接假定 model_path 为相对路径，但行 96-97 声明 "relative to project root **or absolute**"；绝对路径渲染成 `<root>//<abs>` 悬空路径，且该脚本 "never fails hard"（`|| true`）→ `.user_pkg` 静默空文件，闭包判定池丢失。对照：行 137-139 / 408-409 两处调用均只传 `model_path` 单参，唯此调用点做拼接 | 调用点改为与另两处一致（只传 `{{ inputs.model_path }}`，或先解析再传），或在 Required Inputs 收窄 model_path 为仅相对路径 |

**严重度说明**：三条均 LOW——F1/F2 为单行措辞级（不动任何行为）；F3 为边角鲁棒性（相对路径标准场景不受影响）。注意与 v4 SPEC「po_flatten 不变」的张力：F1/F2/F3 均不改变 v3.5 §3.1 语义（标准场景行为不变），是否顺手清理由用户拍板。

## ③ 裁决（首轮，2026-08-25）

词表 grep 0 命中、契约逐条一致、哨兵/schema/引用全对——机械面全绿；受众翻转通读抓出 2 处已删机制/考古措辞残留 + 1 处拼接边角悬空路径。

VERDICT: ISSUES (3)

---

## ④ 复验（2026-08-26，针对 commit `24eb711`）

- **核验对象**：`git diff 2de195e..24eb711 -- workflows/agents/po_flatten/`；HEAD = `24eb711` 且该目录工作区干净（on-disk == 复验状态）。
- **逐条 findings 闭环**：

| # | 原位置 | 修复内容（diff 证据） | 复验结论 |
|---|---|---|---|
| F1 | agent.md:16-18 | 职责陈述括号从句整段删除，收敛为 "proof that the shadow resolves, constructs, and exports."；与 L100 的同文不一致（不可达分支描述）随之消除 | **RESOLVED** |
| F2 | agent.md:100 | "(removed)" 二词删除，语句其余不动（现 L97-98） | **RESOLVED** |
| F3 | agent.md:200-202 + extract_user_pkg.sh | 调用点改两参单传（`project_root` + `model_path`）；脚本重写：①脚本内解析相对/绝对（`[[ "$MODEL_PATH" = /* ]]` 分支，调用方永不拼接）②`set -euo pipefail` + 去 `|| true`；模型入口缺失 / grep rc>1 → FATAL exit 2（fail loud）③grep rc=1（零 import 行）= 唯一合法空 marker 场景，WARN 披露非静默。`tests/test_po_scripts.py:2932-2996` 四分支全覆盖（相对解析 / 绝对直通 / fail-loud / WARN），测试与脚本签名一致 | **RESOLVED** |

- **回归面复查**：更新后 agent.md 残留词表 grep（含增补 `removed` / `when a pretrained`）= **0 命中**；改动为纯删除/换签名，无新残留引入、无契约面变更（v3.5 §3.1 语义不动，标准场景行为不变）；`extract_user_pkg.sh` 其余调用方仅 ns3_flatten / psu_flatten（各自 workflow 独立副本，不在本文件审查范围）。

VERDICT: CLEAN

---

## ⑤ D-V4-20 复验（2026-08-26，增量 `6da08d7`；核验区间 `3d57c24..de2a723`，HEAD = `de2a723`，po_flatten 工作区干净）

**背景**：D-V4-20（profiling 子代理化，用户拍板）——`profile_script_path` input 退役 → `npu_chip`（空 = placeholder 估算模式 / `6613`/`1951` = mfu 真评测模式）+ `npu_precision` / `npu_core_num` 两旋钮；po_flatten 侧改动 = Step 0 第 4 参换轨 + 启动期枚举守卫。逐 hunk 审：

| hunk | 位置 | 内容 | 受众翻转结论 |
|---|---|---|---|
| 1 | agent.md:96-98（Required Inputs） | `profile_script_path`（empty = built-in estimator）→ `npu_chip`（empty = placeholder estimation mode；`6613`/`1951` = mfu real-evaluation mode；anything else fails Step 0） | PASS——自包含产品式语义，交叉引用 Step 0 真实成立（exit 3）；与 `workflows/prof-opt.yaml:53-65` input description 及草稿 D-V4-20（`prof-opt-v4-design-draft.md:45/213`）逐项一致（枚举/默认/启动即失败） |
| 2 | agent.md:136-139（Step 0 调用行） | 前缀 `NPU_PRECISION/NPU_CORE_NUM` env + 第 4 参 `{{ inputs.npu_chip }}` | PASS——可执行；三个 `{{ inputs.npu_* }}` 均为 yaml 真实 input；脚本侧 env 消费带防御默认（INT8/1），调用方显式传值优先 |
| 3 | agent.md:154-156（exit-3 映射） | "missing profiler script" → "illegal profiling-mode inputs (npu_chip / npu_precision / npu_core_num)" | PASS——与脚本三个 FATAL 分支（chip 枚举 / precision 枚举 / core_num 枚举）逐一对应；退役措辞随机制同步删除，无悬空 |
| 4-5 | reuse_check.sh（守卫替换） | 旧 profiler 存在性守卫 → profiling-mode 守卫：`case` 枚举 空/6613/1951，chip 非空时连带 INT8/INT16/AMP 与 1/2/4，非法 exit 3 且 stderr 带实际值 | PASS——fail loud、启动期早拒（"fail it now, loud and early" 为运行时行为陈述非事故复盘）；注释为脚本受众非 prompt prose；`tests/test_po_scripts.py:2282-2318` `test_reuse_check_npu_chip_enum_gate` 全分支覆盖（合法三值 / 非法含尾空格 typo `6613·` / mfu 模式 FP4、core 3） |

**增补词表 grep**（原词表 + `profile_script_path`）：agent.md **0 命中**——退役 input 名全文清除（含旧 exit-3 措辞）。残留的 `profiler` 字样仅 L85/L449 的 `PROFILER_CONTRACT.md` 懒加载边界（D-V4-20 下该契约仍存在、仍是下游业务，语义未过期）；`npu` 仅出现在上述三处新内容。（注：grep 调试中 `npu` 子串会误命中 "i**npu**t"，已人工滤除。）

**契约一致性**：D-V4-20 已回卷 `prof-opt-v4-design-draft.md`（:45 决策表 + :213 变更记录）与 `prof-opt-v4-spec.md` §1（mfu_benchmark/mfu_adapter/PROFILER_CONTRACT 条目）；yaml inputs 三增一退役与 agent.md 引用面吻合。无越权字段、无 v4 已删机制措辞回流（`mfu` 相关词为 D-V4-20 重新引入的现行机制名，属 operational，不计残留）。

VERDICT: CLEAN
