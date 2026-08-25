# 洁净审查记录：workflows/agents/po_baseline/agent.md

- 审查对象：`D:\Projects\Orca\workflows\agents\po_baseline\agent.md`（193 行，含 frontmatter）
- 审查方法：受众翻转通读（假设执行 LLM 只拥有本文件 + 工作区事实，零开发历史）+ 词表 grep + 契约逐条核对（`docs/specs/prof-opt-v4-spec.md` §2/§3/§4/§5）
- 审查日期：2026-08-25

## ① 逐段受众翻转结论表

| 段（行号） | 内容 | 结论 |
|---|---|---|
| frontmatter description（L1-4） | 一句话产品式描述：非阻塞链 / pristine 导出+profile / detach 全预算训练 + finalizer 守护 / 并行派 analyst / 存活确认后 emit | 可执行，纯 v4 语义，零残留 |
| 引言（L7-25） | 非阻塞定义（executed ≠ 训练完成）+ 一个并行 subagent + 职责分界（chain 拥有确定性决策） | 每句自包含可执行；"incremental curve / live chart pushes / final check / both accuracy anchors / terminal marker" 均为 v4 现行机制（与 finalizer 契约一一对应） |
| Critical Protocol（L27-44） | baseline_status.md 跨 turn 真相源 / running→STATUS MESSAGE 禁 JSON / 禁手启动长任务 / 单 bash ≤10 min | 全部可独立执行；"do not call orca next" 为运行时 CLI 实名；与 spec「baseline_status.md 跨 turn 真相源」一致 |
| Resource Anchors（L46-62） | env 锚点、driver 脚本路径、共享脚本部署位、三个 inputs、上游 contracts.json/shadow | 全部 operational；`proxy_budget` k 为现行 contracts.json 键（chain L146 实读、check_contracts.sh L74 校验），非退役措辞 |
| Path Handling Rules（L64-67） | pathlib 强制、禁字符串拼路径 | 可机械执行 |
| Subagent Call Protocol（L69-82） | point-to-file + 首行哨兵 + 失败矩阵（重派 1 次 → failed 报 analyst 名） | 哨兵机制有实底（business-logic-analyst.md frontmatter `sentinel: BLA7K4`）；失败路径 fail loud 且声明训练不受影响——产品说明书式 |
| Lazy Loading（L84-87） | 先 invoke chain，仅验证时读 business_logic.md | 可执行 |
| Step 0 Preconditions（L91-100） | 七项上游产物清单，缺一即 failed | 所列文件名全部为现行 v4 实名（check_contracts.sh 校验同三模板；render_run.sh/analyze.py/readiness.json 均在树内被引用） |
| Step 1 Invoke Chain（L102-118） | 完整 bash 调用行（条件拼 --profile-script 用 printf %q）；stdout = 恰一行 JSON、九字段、error 折叠 step 号 | 可直接照抄执行；九字段与 chain emit 函数（run_baseline_chain.sh L610-622）逐字段核对一致 |
| Step 2 Dispatch analyst（L120-126） | train.pid 出现即派、与训练并行、不等训练结束 | 与 spec「business-logic-analyst 训练启动后 dispatch（并行）」一致；train.pid 为 chain 实际产物 |
| Step 3 Polling（L128-139） | running→状态消息+sleep 60-120s+重调；executed/failed→verbatim 转发；无 stdout JSON→fallback emitter | 与 chain 行为对齐（chain L731 确有 running 线："business_logic.md not yet on disk … re-invoke"） |
| Step 4 Validation（L141-149） | check_business_logic.sh = executed 路径必查；败→重派 1 次→仍败 fallback failed | 与 spec §4/§5「check_business_logic.sh（存在/非空/哨兵/五段标题）= Validation 必查」一致 |
| Guidelines（L151-158） | 除 chain/analyst 外零写入；诊断走 stderr；禁重测、禁碰 finalizer | 可执行；"crash-relaunch … is the contract" 正确把重派≤3 留给 finalizer |
| Output（L160-192） | verbatim 转发铁律（additionalProperties: false 论据）+ fallback emitter 模板（九字段齐）+ 失败行字段语义 | 可执行；与 output_schema 九字段零出入；`"$PY"` 取自 contracts.json interpreter.sys_executable 有实底 |

## ② 词表 grep 结果（命中 = finding）

对目标文件跑全词表（不区分大小写）：`mnist_kd` `playground` `prof_opt_demo` `run_verify` `baseline_proxy_acc` `baseline_ref` `mfu_adapter` `perturb_ckpt` `playbook` `ref-input` `auto-trained` `docs/specs` `D:\Projects` `/mnt/d` `spec-review` `SPEC-R1` `ns3` `psu` `kd-nas` `nas-supernet` `prof-opt-design-draft` → **0 命中**。

增补类别 grep（受众翻转兜底项）：`v3|v4|V4|§|（issue 括号引用）|迁移|前身|前作|analogue|legacy|deprecated|retired|no longer|formerly|used to|previous version|stall|deepseek|kill+retry|MNIST|CIFAR|accuracy=|orca/exec|orca/compile|examples/` → **0 命中**。

## ③ 契约一致性核对（prof-opt-v4-spec.md §4 po_baseline 行 + §2 schema + §5 subagent 行）

| 契约项 | agent.md 落点 | 结论 |
|---|---|---|
| 非阻塞 executed = 早期链过 + 训练/finalizer 双存活 + business_logic.md 落盘（不含训练完成） | L10-16、L122-126 | 一致（"(or already terminal)" 为幂等重入的合理补语，不矛盾） |
| 九字段 schema（status/base_onnx/makespan_cycles/baseline_metrics/business_logic_path/profile_dir/bottleneck_report/error/generated_artifacts） | L113-117、emitter L176-187 | 一致；chain emit 函数逐字段核对同集 |
| status enum [executed, failed]（schema）vs 链内 running | L113 显式标注 "running is agent-internal" | 显式调和，一致 |
| 完整训练同模板/--out 显式/wrapper 不 exec/PYTHONUNBUFFERED/ISO8601 日志/重派≤3/per-attempt log/终检/双锚/指纹/train_final/fail-safe | 均为 chain+finalizer 内部契约，agent.md 正确只做摘要（L14-16、L117-118、L157-158）不复制实现细节 | 高度正确（altitude 对）；摘要措辞与机制一一对应，无退役物 |
| baseline_status.md 跨 turn 真相源 | L29-34 | 一致 |
| business-logic-analyst 训练启动后并行 dispatch | Step 2（L120-126） | 一致 |
| check_business_logic.sh 五段校验 = Validation 必查 | Step 4（L141-149） | 一致 |
| 引用资源实名（run_baseline_chain.sh / check_business_logic.sh / business-logic-analyst.md / 三模板 / render_run.sh / analyze.py / emit_result.py / readiness.json / full_train_budget / proxy_budget） | — | 全部在现行 v4 树中实证存在（po_baseline/scripts/ 两脚本在位；subagent md 带 sentinel；check_contracts.sh 校验同名键/模板） |

## ④ Findings 清单

**零 finding。**

（非计数观察，不构成洁净/契约违规，仅备案：）

1. `agent.md:172-187` — Output 节把 fallback emitter 的适用面写作 "Only when the chain exited WITHOUT a parseable stdout JSON line"，而 L79-81（失败矩阵）与 L147-149（Step 4 二次门败）在 chain **有**可解析 executed 行时也指示用 emitter 重组 failed。执行者可调和（emitter 是通用重组工具，失败矩阵是显式 override），且模板字面 `base_onnx=""` 在门败路径会抹掉已存在的产物路径——影响极低（failed 路由 po_report，report 从盘面收割）。如后续想消除歧义，可在 emitter 模板处补一句"门败路径按盘面实际产物填 path/numeric 字段"。
2. `agent.md:3` — frontmatter tools 含 `write`/`edit`，而 Guidelines（L153-154）声明本节点除 chain/analyst 外零写入。属防御性工具授权，非契约项，不影响执行。

## ⑤ 裁决

受众翻转通读全段通过：每条指令自包含可独立执行，语气为产品说明书式，零开发期残留，零 v4 已删机制措辞（run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / 懒补训 / epoch-only proxy 均不在场；唯一 "proxy" 出现是现行键 `proxy_budget`），与 prof-opt-v4-spec §4 po_baseline 行逐条一致。

VERDICT: CLEAN
