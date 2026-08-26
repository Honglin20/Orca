# Release: prof-opt workflow v4 重构（含 D-V4-20 profiling 子代理化）

> 日期：2026-08-26。SDD 全流程闭环：SPEC 3 轮对抗评审（CP→CP→PASS，65+ 项问题回卷，3 次用户拍板）→ 计划环 2 轮（含 E2E 回退修订）→ 实现（coder 内环 code-reviewer 全程闭环）→ 逐文件洁净审查（每文件独立 reviewer，记录入库 `verify/cleanliness/`）→ E2E 2 轮真执行。

## 一、变了什么（v3.5 → v4，10 节点 → 8 节点）

| 变更 | 内容 |
|---|---|
| **基线完整训练（非阻塞）** | 基线用原始参数后台跑完整训练（不再短训锚），逐轮精度实时成曲线；节点启动确认后即放行；**finalizer 守护进程**确定性收尾（增量曲线提取 → 终检实跑==渲染 → 末 ckpt 终局锚 `baseline_full_acc.json` + 值级指纹 → 可寻址时 k 轮参考锚 → 终态标记）；GPU 串行守卫（probe 前等基线训练退出，四象限判定 + 30min 停滞检测） |
| **po_propose 子 agent 内闭环** | 删 `po_implement`/`po_verify` 两节点；节点内三子代理（bottleneck-analyst 瓶颈富化[referential 校验锚定机械报告] → structure-proposer 结构级提案[业务逻辑×SOTA，禁超参，≤3/轮] → variant-implementer 实现+时延打回修 ≤2 次）；机械闸保留（predict_delta 严格负 / 编辑范围 ⊆ 影子 / 声明一致）；`run_latency_recheck.sh` 批量复测（原 run_verify 迁移） |
| **业务逻辑分析** | 新 subagent `business-logic-analyst`：训练期并行产出五段业务逻辑文档（任务语义/输入输出/架构动机/逐模块职责与物理意义/训练目标），作提案出发点 |
| **stop-at-k 短训** | 变体渲染与基线同 epochs（学习率安排天然同路径）→ `stop_at_epoch.sh` 幂等监控到第 k 轮杀进程组 → 曲线与基线完整曲线 @k 对齐比较（`--at-epoch` + at_epoch/baseline_path 可审计输出） |
| **po_full_train 锚简化** | 删自动补训基线路径；锚 = 基线完整训练终值（指纹逐字段校验）+ winner 对称终检 |
| **live 可视化** | `push_curves.py` sidecar：基线+各变体曲线实时推 web 图表（finalizer 每轮/ probe 等待/ report 终稿三调用点；5s 超时保护 + .chart_push.log 审计） |
| **D-V4-20 profiling 子代理化** | `profile_script_path` 退役 → `npu_chip`（空=placeholder 模式；6613/1951=mfu 模式）/`npu_precision`/`npu_core_num` 三 input；**mfu 模式**：`mfu-analyzer` 子代理（用户 MFU Bottleneck Analyzer 适配 Orca 协议：跑 `mfu_benchmark.py`[文件名锁定，内容随用户真脚本替换] + 三阶段解析 + 瓶颈报告）+ `mfu_adapter.py` 确定性转换（schedule_result.json 并行 cycles = canonical makespan → 四件套）；判定数字全程机械可审 |
| **早停三层准入（UD-3）** | 契约期 best-effort 早拒 + 明示准入条款 + 终检严格失败指向条款 |
| **退役清单** | po_implement/、po_verify/、po_gate/{agent.md,scripts}/、perturb_ckpt.py、profile_script_path、baseline proxy 锚/懒补训/epoch-only 语义 |

## 二、质量与验证证据

- **SPEC**：`docs/specs/prof-opt-v4-spec.md` + 语义权威 `prof-opt-v4-design-draft.md` v3.3（D-V4-1~20 决策表 + 附 A 全程审查记录）
- **单测**：134 项绿（test_po_scripts 123 + diff_check + inject）；引擎侧 73+ 绿
- **洁净**：`tars validate` 0 error 0 warning；残留 grep 0 命中；**18 份 prompt 文件逐一独立 reviewer 全 CLEAN**（记录 `verify/cleanliness/`）
- **E2E（本地 placeholder 模式，2 轮真执行）**：mnist_kd 全链 **success**（时延 -46%、精度 gap 0.0046、写回逐字节==winner shadow、gate 零手工解堵自然 exit 0）；target 合法 failed@full-train 终态；断言①-⑨全有盘面证据；顺带验证 engine recoverable 自愈
- **引擎修复（随本任务落地）**：in-session script 节点 spawn env 接 project-scoped artifacts 派生（修 po_gate 必现 127，SPEC addendum `2026-08-21-in-session-script-node.md` §2.4.2）；tars skill 逐字传递铁律（修驱动转发层 schema 违约）；reuse_check BASELINE.lock 校验 TypeError 修复 + 失败两态文案
- **mfu 模式 E2E**：归属用户真机自跑（本地无 NPU 提交通道）；适配层字段映射基于 mfu_benchmark.py 文档契约，真实产物若有出入会 fail loud 并在 stderr 指名

## 三、commit 索引（v4 链）

`86ccf99` v3.5 基线 → `7861a89..2de195e` v4 实现 8 commits → `24eb711` 洁净修复 → `fa3b686` 审查记录 → `cad9ef9` D-1 引擎修复 → `8709dd1` D-2 emit 强化 → `a295ca9` D-B reuse 修复 → `a851068` D-A tars 逐字铁律 → `3d57c24`/`de2a723` SPEC 回卷 → `6da08d7` D-V4-20 实现 → `fc6bb89` D-V4-20 洁净修复 → `9ae6438` 增量审查记录

## 四、用户真机跑 mfu 模式（E2E 指引）

1. 替换 `workflows/agents/_po_scripts/mfu_benchmark.py` 内容为真实脚本（**文件名不变**；`tars install` 后部署件自动同步）；
2. 启动 inputs 增 `npu_chip=6613`（或 1951）、可选 `npu_precision`/`npu_core_num`；其余同 SPEC §7 钉值；
3. 首跑关注两点：`mfu_adapter.py` 若 exit 2 会指名缺失/矛盾字段（适配层 vs 真实产物的契约对齐点）；`base/profile/mfu_bottleneck_report.md` 应在场含哨兵 `[subagent:mfu-analyzer v1 MBA7K2]`；
4. 验收断言集见 SPEC §7（mfu 侧新增：报告在场含哨兵 + 四件套 makespan == schedule_result.json 并行 cycles）。
