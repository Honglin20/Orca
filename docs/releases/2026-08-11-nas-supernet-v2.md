# Release Note: nas-supernet-v2 — in-session 友好 / 单卡非 DDP / 弱模型友好的 NAS supernet 全链

> Date: 2026-08-11
> SPEC: `docs/specs/2026-08-11-nas-supernet-v2.md` (REVIEWED-PASS, 21 issue 闭环)

## What was done

新建 `nas-supernet-v2` workflow（8 agent 节点 + 0 terminate），根治 v1 在 in-session 实跑暴露的
四类问题：末端 terminate 节点在 in-session 崩 / DDP 脚本经常坏 / 伪 agent 浪费 LLM spawn /
生成节点单 turn 上下文过重。

### C1 — entry 拆分：ns2_flatten + ns2_expand_supernet
- v1 `ns_expand_supernet` 7 步单节点 → 拆成 2 节点：ns2_flatten (Step 0-3) + ns2_expand_supernet (Step 4-7)
- ns2_flatten 是新 entry；产 prepared_model；flatten_passed=false → 路由 ns2_report
- ns2_expand_supernet 有独立 reuse-check（查 supernet.py + summary），不重做 flatten
- 两节点各有固化校验脚本（check_flatten.sh / check_expand.sh）

### C2 — 合并 ns_select 进 ns2_run_search
- v1 ns_select（伪 agent）合并进 ns2_run_search：Step 2.8 运行 select_architecture.py
- 失败安全网（CRITICAL #4）：select 崩/rc≠0/无候选时 emit falsy JSON（禁 node_failed）→ ns2_report 归因
- select 结果落 .selected_arch.json marker（供 ns2_report 读终态）
- output_schema 加 select 5 字段

### C3 — ns2_search_pipeline 内部 3 子代理拆分
- 父 agent 改为编排者：产 search_record_schema.json 共享 schema → 派 3 子代理（A=latency / B=search-core / C=select+scaffold）
- 3 新 subagent 文件：search-latency-gen.md / search-core-gen.md / search-select-scaffold-gen.md
- 父拥有 fix-loop

### C4 — 单设备默认（CRITICAL #3 根治）
- launcher 模板换 plain `python3`（删 torchrun/NPROC/MASTER_PORT），AMP=false
- DDP wrap 改条件式 `if is_distributed():`
- sync_random_seed guarded 版（`if not is_distributed(): return local_random`）
- §4/§6/§7/§8/§9 全标「when is_distributed()」
- 固化 check_launcher.sh（行锚定 grep：AMP=false / 无 torchrun / NUM_WORKERS=0 / python3 entry）

### C5 — 每节点固化校验脚本门
- 7 个 check_*.sh 脚本（check_flatten / check_expand / check_train_script / check_launcher / check_search_pipeline / check_retrain / check_report）
- .user_pkg marker 机制（ns2_flatten 写，各 check 读做「禁 import 用户包」检查）
- LLM verifier 仅留语义项

### C6 — 单一 ns2_report reporter（CRITICAL #1 + #2）
- v1 三 terminate + 成功 $end → 单一 ns2_report agent 节点
- 零跨节点 output 引用（只引 inputs + bash/read 读磁盘判终态）
- 终态映射表判 success/failed + stage（flatten_failed / unsupported / select_failed / train_failed / retrain_failed / success）
- output_schema 扩字段（13 字段，从盘读填）
- workflow outputs 全读 ns2_report.output（CRITICAL #2）

## Verification
- `tars validate nas-supernet-v2` 0 error / 0 warning
- v1 零改动（git diff 空）
- bash -n 全 check_*.sh 通过
- prompt dev residue 扫描清零

## Deviations from plan
- ns2_run_train / ns2_run_search / ns2_retrain 执行脚本镜像 v1 不改逻辑（SPEC §3 C5 要求）
- ns2_search_pipeline/agent.md 的 Step 1-3 生成详情保留作子代理 reference（子代理 Read contract 指引）

## Commits
- `976e132` feat(workflow): nas-supernet-v2 — in-session 友好 / 单卡非 DDP / 弱模型友好的 NAS supernet 全链
- `de5878b` fix(workflow): nas-supernet-v2 review MUST-FIX + SHOULD-FIX 闭环（7 MF + 10 SF + MINOR）

## Next steps
- E2E（projects/playground 双项目真跑）属下一阶段 test-agent 范围
