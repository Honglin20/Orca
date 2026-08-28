# prof-opt prompt 洁净审查 findings（2026-08-28）

> 审查对象：`workflows/prof-opt/`（7 agent.md + 4 references + 8 subagents + PROFILER_CONTRACT.md + workflow.yaml，共 42 个 .md/.yaml/.py）。
> 方法（契约 §9 四层）：`tars validate`（窄表 0/0 ✓）→ `check_dev_residue.py`（宽口径）→ `check_agent_md_static.py`（静态启发）→ 3 个独立 sub-agent 受众翻转通读（§8），关键 finding 逐一回查原文。
> **本文件为精确口径：15 violations / 11 borderlines**（会话内速报 12/12 为粗计，以本表为准）。
> **清理闸门：等 workflows/ 目录隔离改造批 H 收口后开工**（CURRENT.md 并行协调协议——写权归 layout loop）。本文件只记录，不改 workflows/。

## 何时清理、怎么清

- **前置**：批 H 收口（commit 落地 + CURRENT.md 该段关闭）。
- **批次 1（纯文本，低风险）**：下表 B/C/D 类 + borderlines 中的死词/示例值/辩护分句。改完复跑 `tars validate`（须仍 0/0）+ 宽口径/静态两脚本 + 抽样复审通读。
- **批次 2（抽脚本，行为等价重构）**：A 类 9 处内联 → 新增 scripts + agent.md 单行化 + `deploy_scripts.sh` 部署清单同步 + 测试覆盖；建议跑一轮 E2E 冒烟（用户拍板）。
- **工具侧（独立小改动）**：`check_agent_md_static.py` 的 `[script-ref]` 规则补 `$ORCA_ARTIFACTS_DIR/scripts/` 部署模式豁免（见文末 lint gap）。

## Violations（15）

### A. 确定性代码内联（9）——重灾区 po_flatten / po_contract

| # | 位置 | 内容 | 修法 |
|---|---|---|---|
| A1 | `agents/po_flatten/agent.md:274-277` | shadow 顶层包枚举 bash（for/if/case） | 抽 `_po_scripts/list_shadow_pkgs.sh` |
| A2 | `agents/po_flatten/agent.md:284-297` | stdlib 撞名预检 python heredoc（if/exit） | 抽 `scripts/check_stdlib_clash.py`，退出码 fail loud |
| A3 | `agents/po_flatten/agent.md:305-336` | ~30 行 BASELINE.lock 写入器（def/for/推导式）——全 wf 最重内联 | 抽 `scripts/write_baseline_lock.py`（env 传参） |
| A4 | `agents/po_contract/agent.md:113-136` | sha256 快照 heredoc；**Step 8 原样重跑第二遍（同逻辑两份拷贝）** | 抽 `scripts/snapshot_tree.py --root --out`，两步各一行 |
| A5 | `agents/po_contract/agent.md:271-284` | 多行 `python3 -c`（for/if/raise）读 shadow_pkgs | 抽脚本或降为单行查询 |
| A6 | `agents/po_contract/agent.md:424-434` | exemptions diff heredoc（纯确定性计算） | 抽 `scripts/snapshot_diff.py pre post` |
| A7 | `agents/po_baseline/agent.md:149-155` | freeze-origin 的 `if` 包装——初判与幂等声明重复；**复审改判**：护栏第一条件（profile_summary 存在）防的是 mfu awaiting 态下 analyze.py 无 summary 误炸（analyze.py:71 无条件严格加载），是输入存在性护栏非冗余 | 抽 `agents/po_baseline/scripts/freeze_origin.sh`（护栏原样入脚本，prompt 留单行调用——确定性逻辑沉脚本的正规解） |
| A8 | `subagents/variant-implementer.md:86-91` | 6 行 DONE-marker 写盘内联（嵌套引号极脆；同文件 `diff_check.py` 是先例） | 抽 `scripts/write_done_marker.py <vid_dir>` |
| A9 | `agents/po_full_train/references/full_train_protocol.md:83-88` | train 状态机 if/elif/else 内联；**probe 侧同款已封 `stop_at_epoch.sh`，不对称** | 抽 `scripts/train_state.py <dir>`（输出 DONE/RUNNING/DEAD 单行） |

### B. 接口史口吻 "anymore"（3）——同一句式，口径统一全改现在时

| # | 位置 | 原文要点 | 修法 |
|---|---|---|---|
| B1 | `agents/po_propose/agent.md:289-290` | "there are NO threshold arguments **anymore**" | 改 "the recheck takes no threshold arguments" |
| B2 | `agents/po_full_train/references/full_train_protocol.md:156-158` | "there is no budget argument **anymore**" | 改现在时：脚本自读 `base/origin_anchor.json` |
| B3 | `agents/po_probe/references/probe_protocol.md:203-205` | 同上同款 | 同上 |

### C. 悬空/陈旧引用（2）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| C1 | `agents/po_report/references/report_format.md:6-9` | **`<write-back>` 占位符指示去 input anchors 找值——该锚点不存在**（write-back 是固定行为非输入；被撤销的输入开关残留）。执行 LLM 可能空转找锚点或捏造开关，**优先级最高** | 从占位符清单删除，改「write-back runs on every success terminal; no input controls it」 |
| C2 | `agents/po_flatten/agent.md:85` | 提及 `scripts/PROFILER_CONTRACT.md` "is NOT needed"——该路径运行时永不生成（`deploy_scripts.sh` 只部署 `*.py/*.sh`），且「你不需要某文件」是开发期噪音 | 整句删 |

### D. 测试布局泄漏（1）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| D1 | `agents/po_contract/agent.md:520-521` | "and **a test** pins this document and the gate together"——仓库测试布局信息给执行 agent 零价值 | 删 and 分句，保留「gate 校验常量子串、须逐字复制」 |

## Borderlines（11，建议顺手修但不阻断）

| # | 位置 | 要点 |
|---|---|---|
| b1 | `po_flatten/agent.md:183-184` | "this version no longer knows" 版本演进辩护分句——保留规则句删解释分句 |
| b2 | `po_contract/agent.md:369-376` | sitecustomize 合并 heredoc——是待写入文件的内容非工具逻辑；可抽 `scripts/merge_sitecustomize.py` 或标注「此围栏是文件内容」 |
| b3 | `po_contract/agent.md:501/508` | 示例具体轮数 `"train_epochs_full": 100` / `"epochs": 10` 有照抄风险——改占位符 |
| b4 | `po_propose/agent.md:205-230`（三处） | 多行 `-c` 调 `history_lib.append_implemented`，实参全占位——加 `append_impl_row.py` CLI |
| b5 | `subagents/variant-implementer.md:45-48` | copytree 内联（无控制流）——可并入 `make_variant_shadow.py`，非必须 |
| b6 | `subagents/structure-proposer.md:104-106` | `sys.path` hack + 三参数约定内联——签名漂移会静默过期，加薄封装 `build_sig.py` |
| b7 | `subagents/paradigm-verifier.md:11` | "tier-B" 设计期分类死词（全文仅此一处）——删，直接写 "an adapted entry" |
| b8 | `agents/po_probe/references/probe_protocol.md:241-249` | append results 行内联——紧邻 history 行已示范走 `history_lib` 的正确形态，加 `append_probe_result` |
| b9 | `agents/po_propose/references/structural-levers.md:21-24` | "Scope of this version" 文档修订史口吻——改 "This catalog covers six lever families" |
| b10 | `structural-levers.md:204-205` | 悬空复合 ID "N2-exact"（目录中无此子条目）——改 "(C2, and N2 when the redundancy is exact)" |
| b11 | `agents/po_report/references/report_format.md:112-116` | "currently / The field exists so" 字段存在性辩护——压缩为操作性规则 |

## 误报与白名单存档（勿在清理时误伤）

- 宽口径 2 × `[milestone] P1`：JSON 示例里的瓶颈 pattern ID（本 wf 自有命名约定）——保留。
- 静态启发 37 × `[script-ref]`：全是 prose 提及部署件；执行位已 grep 复核**零裸调用**（全部 `$ORCA_ARTIFACTS_DIR/scripts/` 前缀）——保留。
- 白名单命中（均合法）：`P1`/`P2` 瓶颈 ID、`Erf/Relu/GELU` 算子名、`6613/1951` NPU chip 枚举、AdamW/wd=0.01 教学示例、accuracy-analyst 示例 gap 0.61/预算 0.1（已泛化措辞）、`stop_at_epoch.sh` stall 监督阈值（运行时契约非事故叙事）、`predict_delta.build_change_sig` 单行调用、report_format 的 round/makespan 示例值（明示形状示例）、文献引用（Glorot/MobileNetV3/NFNet 等 sota_reference 素材）。
- `_po_scripts/PROFILER_CONTRACT.md`：判定 (c) 类开发期契约（无 agent 运行时 Read 指示，`deploy_scripts.sh` 不部署 .md）——洁净契约不适用。
- `_po_scripts/__pycache__/*.pyc`：git 未追踪且被 ignore——非问题。

## Lint gap（工具侧改进项）

`check_agent_md_static.py` 的 `[script-ref]` 规则只认 `$ORCA_AGENT_RESOURCES/scripts/` 前缀，不认 prof-opt 的部署模式（共享脚本部署到 `$ORCA_ARTIFACTS_DIR/scripts/`、prose 简写提及）。37 条噪音让该脚本对 prof-opt 失去信噪比。建议：规则加 artifacts 部署豁免或 prose 上下文判别（命令位 vs 提及位）。

## 验收口径（清理完成时）

1. `tars validate workflows/prof-opt/workflow.yaml` 仍 0 error / 0 warning；
2. 宽口径 + 静态两脚本对 prof-opt 零 error 级 finding（[warn] 顺序建议类不阻断）；
3. 复审通读：上表 V/b 逐条闭环（修或 waive 记录在案）；
4. 批次 2（抽脚本）另加：部署清单同步 + 测试绿 + E2E 冒烟（用户拍板是否跑）。

---

## 清理结果（2026-08-28 执行，批 H 收口后开工）

**全部 violations 闭环 + borderlines 修 8 / waive 3**：

- **批次 1（纯文本）**：B1/B2/B3（anymore ×3）、C1（`<write-back>` 悬空占位）、C2（PROFILER_CONTRACT 陈旧提及）、D1（test-pins 泄漏）、b1/b3/b7/b9/b10/b11 全部修毕。
- **批次 2（抽脚本，12 个新脚本 + prompt 单行化）**：
  - `po_flatten/scripts/`：list_shadow_pkgs.sh（A1）、check_stdlib_clash.py（A2）、write_baseline_lock.py（A3）
  - `po_contract/scripts/`：snapshot_tree.py（A4，两步共用）、snapshot_diff.py（A6）、shadow_pkgs_csv.py（A5）
  - `po_baseline/scripts/`：freeze_origin.sh（A7 复审改判后正规抽取）
  - `po_full_train/scripts/`：train_state.py（A9）
  - `_po_scripts/`（deploy glob 自动部署 + .VERSION 覆盖）：write_done_marker.py（A8）、append_impl_row.py（b4）、build_sig.py（b6）、healed_files.py（计划外：po_full_train:174 / po_probe:231 两处 error 级 `[inline-python]` 单行三元）
- **waive 记录（3 条）**：
  - b2（po_contract sitecustomize 合并围栏）：围栏内容即待写入 workspace 副本的文件本体（路径参数运行期发现），非工具逻辑，抽取改变语义——保留；
  - b5（variant-implementer copytree）：单函数调用无控制流，契约豁免面内——保留；
  - b8（probe_protocol probe_results append）：单文件单站点无控制流；且 history_lib 的章程是 history.jsonl 专属写路径，塞 results 行破坏单一职责——保留。
- **lint gap 修复**：`check_agent_md_static.py`——① `_ENV_REF_FORMS` 增 `$ORCA_ARTIFACTS_DIR` 形态（部署型 wf 命令位绝对引用合法）；② 文件级部署约定判别：body 含 `$ORCA_ARTIFACTS_DIR/scripts/` 绝对形态的文件，裸 `scripts/<file>` 提及按部署件 prose 简写降级为每文件一条汇总 warn（不阻断）；非部署 workflow 行为零变化（回归测试过）。
- **宽口径 P-ID 豁免口径**：`check_dev_residue.py workflows/prof-opt --allow '"P[0-9]+"'`（JSON 示例里的瓶颈 pattern ID，裁定为误报）。

**验收数据**：
1. `tars validate`：✓ 0/0；
2. 静态检查：error 级 0（余 [warn] 顺序建议类 + 2 条部署约定汇总 warn）；宽口径（带 P-ID 豁免）：0 finding；
3. 测试：test_po_prompt_scripts.py 11 passed（新）/ test_skill_v1_checks 55 passed（含 lint 新 4 例）/ test_skill_benchmark + test_po_scripts + test_po_v5 251 passed（含 deploy 清单与 manifest 对新增脚本的适配）。

**code-reviewer 闭环（1 轮，11 findings 全修/已记录）**：
- F2 MAJOR（append_impl_row `--outcome` 无 `--not-implemented` 时静默兑现 → 污染 sig 联合重试账本）：argparse 层 `ap.error` 强制依赖 + 回归测试；
- F1 MAJOR（lint 部署判别用同行 token 巧合，误伤「资源脚本 + artifacts 旗标」文件如 nas-supernet ns_retrain，且命令位裸引用被降级）：改 `_DEPLOYED_FORM_RE` 连续形态正则 + `_CMD_PREFIX_RE` 命令位不降级 + 4 个新 lint 测试（含 ns_retrain 型反例）；nas-supernet 全 workflow script-ref 计数与 HEAD 逐条一致（22=22），非回归；
- F4（`--ckpt` 占位符把确定性决策交还 LLM）：调用行改 `--ckpt ""` 字面量（本 wf 无 ckpt 输入，po_flatten:104 明示）；
- F5（JSON 旗标占位符 Python 拼写 `None`）：×2 改 `null`；
- F6（freeze_origin.sh 零测试）：补三态测试（summary 缺失跳过 / 双条件满足按序传参调 analyze / anchor 已存在 no-op）+ 用法守卫断言；
- F7（waive 留痕）：本文件「清理结果」节的 b2/b5/b8 waive 记录即闭环（reviewer 读到的是补记前版本）；
- N1 freeze_origin `${1:?}` 用法守卫 / N2 `_ENV_REF_FORMS` 补 artifacts 带引号形态 / N3 po_flatten:183 超长行回折 / N4 `_nullable_int/_nullable_value` 转公共名 `nullable_*`（两消费者同步）——全部修毕。

**E2E 冒烟**：是否跑待用户拍板（验收口径 4）。
