# Release: prof-opt v5 —— 时延先行顺序门控（2026-08-27）

> 真机首跑复盘驱动的循环语义重设计。SPEC：[`docs/specs/prof-opt-v5-spec.md`](../specs/prof-opt-v5-spec.md)（3 轮对抗 PASS）｜决策记录：[`prof-opt-v5-design-draft.md`](../specs/prof-opt-v5-design-draft.md)（D-V5-1~8 + U1/U2/U3 + errata 附 A）｜计划：[`docs/plans/2026-08-27-prof-opt-v5-plan.md`](../plans/2026-08-27-prof-opt-v5-plan.md)。
> 实现 commits：`fdd7a52 → d46e9d5`（7 个）+ `d259668`/`b03d8fb`（进度/契约入库）。与 per-wf 目录迁移任务并行完成（批 D `56d0db1` 后按新布局开工）。

## 背景

Run `prof-opt-20260826-145057-3c2dd3`（TDD 接收机 / NPU 真机）暴露三问题：① 双门槛晋升把 22%/18% 时延改进的变体全拦在精度门外 → base 不推进 → 提案原地空转，1 轮即终；② stall/exhausted 早退 + max_rounds=5 与「没达标必须继续」相悖；③ gate 读盘路径 bug（propose 成功后找不到 proposals.json）+ inputs 14 个启动面过重。用户拍板 D-V5-1~8 + 三项裁决（U1/U2/U3）。

## 核心变更

| # | 变更 | 契约 |
|---|---|---|
| 1 | **inputs 14 → 8**：npu 三输入退役（自动解析）、write_back/report_dir/probe_epochs 退役（固定行为）；`[ask]` 只剩 project_root / model_path / latency_reduction_min / accuracy_budget；max_rounds 默认 **100** | §1/§2 |
| 2 | **时延先行顺序门控**：追击期零训练、makespan 严格改进即链式推进（D-V5-8，小步合法）；达线后 probe 首入粗训过精度门；恢复轮**底座固定 + 组合式提案**（可叠加/回退历史尝试，U1）；仅 accuracy_pass 推进（推进即终局 full-train）；gate 出口只剩「双达标 / 轮帽」 | §6/§7/§8 |
| 3 | **origin 双锚冻结**：`base/origin_anchor.json`（baseline makespan + 双预算 + target_cycles）首跑冻结，修 v4「晋升不重锚」目标线漂移 | §3 |
| 4 | **round_state.py 单一来源**：R 推导 + `%03d` 路径 + mode 两态推断收编一处，`<RRR>` prompt 约定退役（远端 gate 失联根因温床） | §4 |
| 5 | **精度规则双层池**（U2）：工作区 `accuracy_rules.json`（direction/generality 打标）→ 终态 merge 项目镜像 `docs/prof-opt/` + 全局池 `$ORCA_HOME/prof-opt/`；model_hash（BASELINE.lock 同配方）键控**跨文件夹自动继承**；generality 打标跨模型迁移；confirm/refute 为 model_hash **集合**（幂等），confirm≥2 → general、refute≥2 → quarantined；flatten 仅 fresh 回种、REUSE 不重种 | §8.5 |
| 6 | **profiling 模式自动解析**：`ORCA_PO_NPU_*` 环境变量 > npu-smi **列感知**型号解析（防 "1951 MB" 伪命中）> placeholder；复用比对集 = 测量配置四字段（resolved_by 溯源字段不参与，errata） | §2 |
| 7 | **部署件版本戳**：`.VERSION` manifest + `--verify` 篡改检测 + flatten 入口幂等重部署（含 REUSE 分支，U3） | §9 |
| 8 | **gate v5 决策序**：accuracy_pass（任意版本行）+达线 → full-train；round≥帽 → best-effort/finish-failed；其余一律 loop；不变量破坏 rc2 兜底 | §7 |
| 9 | **v4 参数族退役**：recheck 的 min-improvement/min-pct/min-ratio/绝对门槛全删，判定唯一依据 = 与 incumbent/target 的比较 | §8.3 |

## SDD 过程

spec 评审环 3 轮（轮1 FAIL 9 阻塞 → 轮2 CONDITIONAL 2 阻塞 + 用户裁决 U1/U2/U3 → 轮3 定点 PASS）；计划环 adversary 3 轮 18 质疑闭环（含 P7 errata 两处回填、P8 协议宽读法裁决）；coder 内环 code-reviewer 2 轮 CLEAN（1 MAJOR round_state 单一来源收编 + 6 MINOR，m7 deferred）；E2E test-agent 纯验证 **PASS**。

## 验证证据（test-agent 独立真实执行）

- pytest 两文件全量：**197 passed / 0 failed**（WSL .venv，4m33s）
- `tars validate`：零 error 零 warning（其内部含 agent prompt 洁净 lint）
- §1 退役 grep：六输入全 workflows/ 零残留
- 对抗 spot-probe **7/7**：gate 锚缺失 rc2 / 退役参数 argparse 拒 / advance 零候选 marker+direction（failed_sigs 含时延+精度两类）/ rules 坏行报行号 / 模式解析四分支含伪命中免疫 / 版本戳篡改检测 rc1 / gate 不变量 rc2
- smoke 定点：轮 2 翻转首入 marker `(2, accuracy, improved=false)`、gate 决策序 1 任意版本行——两断言源码核实 + 定点重跑绿
- 证据目录：`C:\Users\mozzie\AppData\Local\Temp\po_v5_e2e_20260827\`（01-06 日志 + runner 脚本可重放）

## 口径裁定（E2E 唯一发现，PLAN_ISSUE，非代码缺陷）

plan S6 加严判据「洁净元层脚本 exit 0」与 prof-opt 集中部署架构错配：`check_agent_md_static` 面向 create-workflow 产线的 `$ORCA_AGENT_RESOURCES/scripts` 惯例，而 prof-opt 是 `$ORCA_ARTIFACTS_DIR/scripts` 集中部署——**v4 基线（e31ec2a 提取件）同口径同样 rc1**，且无豁免参数。`[milestone] P1` 两命中为 JSON schema 示例值误报（git blame v4 时代）。裁定：元层脚本对 prof-opt 为 **advisory**，SPEC §11.3 真实锚（tars validate）通过即验收；coder C6 commit 声明「双脚本 exit 0」未披露口径、不成立（如实更正）；收敛需改公共检查器（layout 线资产）或加豁免机制，超出 v5 范围。

## 遗留

1. **真机 §11.4 清单**（归用户 NPU 服务器）：链式推进证据（轮 N 瓶颈报告变更+血缘链）/ 追击期零训练断言 / 回退与组合式提案准入 / accuracy-analyst 真实规则产出与跨 run 回种 / 换路不重复 / run `…-3c2dd3` gate 根因核对（§D-V5-5/6 双防已落，服务器实测确认）
2. **m7 deferred**：probe 训练集推导为 prompt 层实现（SPEC §8.4 判据在盘面）；若实测漏训幸存者触发 gate 不变量 rc2，按 SPEC 变更流程升级为固化脚本
3. 已知良性子窗口（§6.1 errata 披露）：撕裂写已达线 + 首入 accuracy_fail → no-op marker 关闭未完成序列，best/base 分叉至多一轮自愈
4. v4 既有 flake `test_baseline_chain_relaunch_budget_is_three`（kill 竞态，与 v5 无关，本轮两跑均绿）
5. 并行竞态事故记录：C5 首次 commit 误卷迁移 loop 已 staged 的 31 文件，soft reset 后精确重提交（`ff86ef0`），双方内容零损失
