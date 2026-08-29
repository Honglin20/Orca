# Release: prof-opt prompt 洁净清理 + 轮末结论闭环（2026-08-28）

> commit：`94378e8`（实现，31 文件 +1021/-184）+ 本文所在 docs commit。
> 缘起：用户要求对 profiling workflow（prof-opt）做洁净审查 → 批 H 收口后清理；随后追加「轮末结论闭环」最小版。

## 一、洁净审查（方法与结论）

按 [agent-prompt-cleanliness-contract](../../orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md) §9 四层执行：`tars validate`（窄表 0/0）→ `check_dev_residue.py` 宽口径 → `check_agent_md_static.py` 静态启发 → 3 个独立 sub-agent 受众翻转通读（§8）。**15 violations / 11 borderlines**（精确口径见 findings 文档），编号/考古/引擎路径/夹具/评审泄漏八大类基本清零，债务集中在确定性内联与接口史口吻。

## 二、清理内容

- **批次 1 纯文本**（8 文件）：anymore×3、`<write-back>` 悬空占位符（最高危——执行 agent 会被指去不存在的 input anchor）、PROFILER_CONTRACT 陈旧引用、test-pins 泄漏、tier-B 死词、示例值占位化、版本辩护分句。
- **批次 2 抽脚本 ×12**（行为等价，`tests/test_po_prompt_scripts.py` 11 测钉契约）：
  - `po_flatten/scripts/`：list_shadow_pkgs.sh / check_stdlib_clash.py / write_baseline_lock.py
  - `po_contract/scripts/`：snapshot_tree.py（Step1/8 两步共用）/ snapshot_diff.py / shadow_pkgs_csv.py
  - `po_baseline/scripts/`：freeze_origin.sh——**复审改判**：原 findings 建议去 if（判幂等冗余），实现时发现护栏第一条件（profile_summary 存在性）防 mfu awaiting 态 analyze.py 误炸，改为护栏原样入脚本
  - `po_full_train/scripts/`：train_state.py（DONE/RUNNING/DEAD 三态）
  - `_po_scripts/`（部署 glob 自动收编 + .VERSION manifest 覆盖）：write_done_marker / append_impl_row / build_sig / healed_files
- **waive 3 条**（留痕于 findings 文档）：sitecustomize 合并围栏（内容即待写文件本体）、copytree 单函数内联、probe_results append（history_lib 单一职责边界）。
- **lint 增强**（check_agent_md_static.py）：env 豁免表加 `$ORCA_ARTIFACTS_DIR` 形态；文件级「部署约定」判别——body 含 artifacts 连续绝对形态的文件，裸 `scripts/<file>` prose 提及降级单条汇总 warn，**命令位裸引用仍 error**（`_CMD_PREFIX_RE`）；非部署 workflow 行为零变化（nas-supernet 全量 script-ref 与 HEAD 逐条一致 22=22）。
- **宽口径 P-ID 豁免口径**：`--allow '"P[0-9]+"'`（JSON 示例瓶颈 pattern ID 裁定误报）。

## 三、code-reviewer 闭环

1 轮 11 findings 全修：F2 MAJOR `--outcome` 无 `--not-implemented` 时静默兑现会永久烧掉 sig 联合重试额度 → argparse 强制依赖 + 回归测试；F1 MAJOR 部署判别改连续形态正则 + 命令位不降级 + 4 个新 lint 测试（含 ns_retrain 型反例）；F4 `--ckpt ""` 字面量钉死；F5 JSON 占位符 null 化；F6 freeze_origin 三态测试；N1-N4 全修。

## 四、轮末结论闭环（用户拍板最小版）

指令：时延/精度任务跑完必须有分析结论加入 workflow，不是跑完不管；多轮运行内容要受管理。初稿（lessons.json 双轴 / latency-analyst / top-N）被裁定过度设计，收敛为：

- `rounds/<NNN>/analysis.md` 双节落盘：`po_propose` Step6b 时延节（准入/淘汰归因、预测 vs 实测校准、下轮方向）+ `po_probe` 精度节（逐 vid 结论、误差结构诊断、规则更新、下轮方向），幂等整节重写、≤15 行；
- 回流：po_propose Step3 输入带上轮 analysis.md 全文 → structure-proposer `<prev_analysis>`（round 1 缺省）；po_report 新 **Round Conclusions** 节（每轮一行 + 跨轮杠杆族/校准蒸馏，缺文件跳过不捏造）；
- 体量管理：分析 prose 永住盘面，prompt 只进上一轮一份——不随轮数累积；结构化教训仍走既有 accuracy_rules 通道，两层不混。

## 五、用户裁决（留痕）

1. **项目镜像预置规则（原选项 1）否决**——会话期经验/夹具数据（KD 排除、已试方案、MSE 具体值等）不允许写入产物，不符合洁净原则；作废不执行。
2. **E2E 不跑**——用户将自行替换 mfu-benchmark 脚本后真机运行（Step6b / analysis.md 回流等新路径的实跑验证归用户侧）。

## 六、验证数据

`tars validate` 0/0；静态检查 error 级 0（余顺序建议 warn + 部署约定汇总 warn）；宽口径（P-ID 豁免）0 finding；pytest：新 11 + lint 回归 55（含 4 新例）+ benchmark/po_scripts/v5 251 全绿。

## 七、遗留关注（不阻断）

`rules_pool` 无条数上限（change_pattern 去重 + 来源优先级已有）——跨 run 池显著增长时再议 top-N 截断（备查于设计记录文档）。
