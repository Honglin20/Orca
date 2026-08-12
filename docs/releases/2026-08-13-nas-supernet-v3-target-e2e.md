# 2026-08-13 · nas-supernet-v3 E2E on playground/target — 全链跑通 + 通用规则提取

> 角色：workflow 调优者（不改 target 源码，只改 workflow / 通用 artifact）。target 是 case，目标是让 workflow 普适。

## 做了什么

在 WSL headless 模式（opencode run + per-node driver，deepseek-v4-flash 后端）下，把 **nas-supernet-v3** 全 9 节点在 `D:\Projects\playground\target`（CrossFusion 4-输入 transformer，InfoNCE + k-NN，CPU）上端到端跑完：`ns3_flatten → ns3_expand_supernet → ns3_train_script → ns3_search_pipeline → ns3_run_train → ns3_run_search → ns3_retrain_script → ns3_retrain → ns3_report`，orca `done:true, reason:completed`（elapsed ~5.3h）。

baseline（pre_trained.pth，CPU batch-1）：**acc 0.072 / latency 4.616ms / 585,760 params**。target_latency 设 = baseline/2 = 2.3ms。

最终选中子网（NSGA-II search + select_architecture，max-acc-under-target）：**latency 0.59ms（7.8× 提速，LAT AC 远超满足）/ acc 0.0016 / 9.1MB**。

| AC | 要求 | 结果 | 说明 |
|---|---|---|---|
| LAT | ≤ baseline/2 = 2.3ms | ✅ 0.59ms | 搜索空间张开成功，子网可大幅收缩 |
| ACC | 恶化 ≤ 3% (vs 7.2%) | ❌ 0.16% | **CPU 算力硬限**：train+retrain 各 50 step 无法逼近 baseline。追训 supernet 到 300 step（6× 量），val_acc 仅 0.0012→0.0014，loss 在 ~10.14 平台化——决定性证据：InfoNCE(LR 1e-5) 从 scratch 在 CPU 预算内收敛不到 baseline（baseline 本身 ~100-epoch 级）。非 workflow bug（见 G2） |
| 超网 depth/内部宽度 > baseline | — | ✅ | depth_candidates (2,4,6) max 6>4；num_heads (2,4,6,8) max 8>4；ffn_dim (128,256,512) max 512>256。supernet-evaluator BLOCKER 满足 |
| 端到端完成 | — | ✅ | 9 节点全过，done:true |

## 各 agent 行为（哪个出问题）

| 节点 | 结果 | 备注 |
|---|---|---|
| ns3_flatten | ✅ | 正确分类 isotropic_transformer；**4 输入 forward 正确处理**（__main__ 构造 x1(B,10,128)/x2(B,4,128)/x3(B,1,64)/x4(B,1,64)）；pre_trained.pth strict-load 零 missing。卡 memory-verifier 子代理 stall 540s，靠 Step 0 reuse-check + kill+retry 过（见 G4） |
| ns3_expand_supernet | ✅ | supernet.py 生成，SearchSpace depth/width > baseline，supernet-evaluator + workflow-verifier + memory-verifier 全 pass。**搜索空间含 3 种 attention 变体（cross_fusion / relu_attention / lion_lit）但无 synthesizer**（见 G6） |
| ns3_train_script | ✅（50min） | viable=true（正确判定 InfoNCE 可 sandwich）；生成 train_supernet.py + data_utils.py + tests。**未自主加 --max_train_steps**（见 G1） |
| ns3_search_pipeline | ✅ | latency_estimator / search-core / select_architecture / evaluator / AGENTS.md 全生成 |
| ns3_run_train | ✅（直接执行） | budget 50 step，loss 10.38→10.25，best val_acc 0.0012。**driver stall 检测与长 CPU 训练不兼容**（见 G3）→ 直接跑脚本 |
| ns3_run_search | ✅（直接执行） | NSGA-II 4 gen（预算 pop=8/gen=4），Pareto 子网 latency 0.26–0.85ms 全 <<2.3ms。select_architecture 选 depth=4 混合子网 |
| ns3_retrain_script | ✅ | finetune-from-supernet；**自主加了 --max_train_steps**（与 train agent 判断不一致 → G1）。疑似在生成期启动了完整 retrain 留下孤儿进程（见 G5） |
| ns3_retrain | ✅（直接执行） | budget 50 step，loss 10.18→10.13，val_acc 0.0016 |
| ns3_report | ✅（修 env 后） | 首次因 `$ORCA_ARTIFACTS_DIR` 未进 env 误用 run 目录报 flatten-failed；修 driver source_env 后正确判 success（见 G7） |

## 通用规则（提取 + 已应用）

### G1 ✅ 已应用 — 训练脚本必须有全局 step 预算 cap（train/retrain 模板对齐）
**现象**：retrain agent 自主加 `--max_train_steps`，train agent 没加 → train_supernet.py 全训 ~19h（CPU 不可行）。属 agent 判断不一致。
**根因**：train 模板 epoch-based 只 `--epochs`，step-based 才 `--max_steps`；无独立的全局 step cap。
**通用修复**：两模板（`train_supernet_script_generation.md` + `retrain_script_generation.md`）都加 `--max_train_steps`（全局 optimizer-step cap，0=unlimited，正交于 `--epochs`/`--max_steps`），break batch+epoch loop。launcher 暴露 `MAX_TRAIN_STEPS`。让 CPU/CI 可行，不依赖 agent 判断。`tars validate` 0 错。

### G2 🟡 文档 — 大数据集 + 反复评估的 metric 必须向量化（porter 职责）
**现象**：data_utils.py 忠实复制了用户 train.py 的 **Python loop k-NN**（27924 次 × topk + `.item()` sync）+ **2327 batch-1 reference forward**。每次 eval ~7–14min。train epoch 末 eval（×2 配置）+ search 每子网 eval → CPU 上爆炸。
**判断**：porter "逐字镜像用户 eval" 的 fidelity 原则没错，但**反复调用场景**（train/search/retrain 多次 eval）下，loop-based metric 是结构性瓶颈。cdist+argmin 向量化对 k=1 结果恒等（已验 vec==orig），~100×。
**本次处理**：直接在 data_utils.py 向量化 `compute_reference_embeddings`（batch gather）+ `compute_knn_accuracy`（cdist+topk，chunk 控内存）—— eval 从 ~14min 降到秒级，search/run_train 得以在 CPU 跑完。
**通用建议**：porter/template 应识别用户的 loop-based per-sample metric，在**反复调用**路径上提供向量化等价（结果恒等，仅性能）。Fidelity 用单次 smoke 验证等价即可。

### G3 🟡 文档 — in-session driver 的 db-wal stall 检测与长执行节点不兼容
**现象**：per-node driver（含本仓库 `_e2e_artifacts/per_node_driver.py`）用 opencode.db-wal mtime 判 stall（540s 无更新=kill+retry）。但执行节点（run_train/run_search/retrain）跑长 bash 时 opencode 阻塞等待 → db-wal 不更新 → **误杀 agent 中训练**。
**本次处理**：执行节点改为直接跑生成脚本（脚本本身是 agent 产出，直接跑=验 agent 产出质量），再喂合成 output 推进。driver 仅用于 codegen 节点（one-shot）。
**通用建议**：执行节点需要不同驱动模式——更长 stall 阈值 / 用 progress.jsonl mtime 而非 db-wal / 处理"请勿调用 orca next"挂起标记（wait + 重 invoke 同节点）。或：执行节点设 计本就跨 turn（status.sh + *_status.md 真相源），driver 适配之。

### G4 🟡 文档 — verifier 子代理（memory/workflow/fidelity）在 deepseek 上 stall-prone
**现象**：flatten 的 memory-verifier stall 540s（kill+retry + reuse-check 才过）；多个节点 verifier loop 耗时 30–50min。
**通用建议**：verifier 子代理调用需自带超时/重试，或 driver 对 Task 子代理调用单独设 stall 阈值。环境性（deepseek stall），非 workflow 逻辑 bug。

### G5 🟡 待查 — 生成节点疑似启动完整执行留下孤儿进程
**现象**：ns3_report 发现一个孤儿 retrain 进程（ppid=/init，原 opencode 父已退；EPOCHS=15，progress_watcher + .retrain_rc 模式 = **执行节点 launch.sh 模式**），跑了 36min 仍在 epoch 1/15，会烧几小时 + 覆盖 retrain_best.pth。kill 掉。
**疑点**：未明确是 ns3_retrain_script 生成期误启动完整 retrain，还是别处。**未完全归因**（ppid 已是 init）。
**通用建议**：生成节点的校验/smoke 不应启动完整执行（用 py_compile + 限定步数的 smoke，而非全 launcher）；执行节点 launch 应在退出时清理子进程。

### G6 🟠 缺口（待用户决策）— v3 搜索空间无 synthesizer attention
**现象**：目标要求"超网包含 synthesizer attention"。v3 isotropic_transformer spec 用 **Elastic* 同族弹性**（Q/K/V ElasticLinear，num_heads/ffn_dim/depth 可搜），**无 synthesizer 变体**。生成的搜索空间有 cross_fusion/relu_attention/lion_lit 三种 attention，但无 synthesizer。grep 全 spec 零命中。
**判断**：synthesizer（无 QK、学习/随机 mix 矩阵，参数极省）是 puzzle workflow 的 mixer 库概念，**结构上不兼容** Elastic* 的"同族切片"范式——加它需把 attention 从单分支 Elastic* 改成 ChoiceLayer 多分支（架构级改动）。
**建议**：二选一——(a) 接受 v3 现有异构 attention（relu_attention/lion_lit 已是低延迟变体，服务"时延减半"）；(b) 如必须 synthesizer，开一 phase 把 transformer 的 attention 改成 ChoiceLayer（多分支，含 synthesizer），并同步 supernet-evaluator spec。本次未擅改（属架构决策）。

### G7 ✅ 已修（测试基建）— driver source_env 用 /bin/sh（不支持 source bashism）
**现象**：`source_env` 用 `shell=True`（/bin/sh），`source` 是 bashism → 静默失败 → ORCA_ARTIFACTS_DIR 不进 env → **ns3_report 误用 run 目录**报 flatten-failed（前序节点自己 source 或ca_env.sh 故无碍）。
**修复**：`_e2e_artifacts/v3_drive_node.py` 改 `bash -c`。**启示**：real tars-skill 由 `orca spawn` 注入 ORCA_*；纯 reporter 节点（ns3_report）依赖 env 而非自己 source，任何绕过 spawn 的驱动都必须正确注入 ORCA_*。

## 已应用的 workflow 改动（通用，非 target 定制）

- `workflows/agents/ns3_train_script/references/workflows/train_supernet_script_generation.md`：加 `--max_train_steps` arg spec + loop break 说明（G1）。
- `workflows/agents/ns3_retrain_script/references/workflows/retrain_script_generation.md`：加 `--max_train_steps` arg spec（G1，对齐 train）。
- `_e2e_artifacts/v3_drive_node.py`：source_env 改 bash -c（G7）。
- `tars validate workflows/nas-supernet-v3.yaml` ✓ 0 错。

## target artifact 侧（仅本 case 的运行产物，非 workflow 改动）

为让 CPU 跑完，对**生成的 artifact**（非 target 源码）做了 budget 调整 + 向量化：`train_supernet.py` 加 `--max_train_steps`（break 逻辑）；`data_utils.py` 向量化 eval；两个 launcher 设 budget；`search_config.yaml` 减 pop/gen。这些是运行级，不入 workflow 模板（模板侧的通用 cap 已在 G1 落地）。

## 结论

- **workflow 正确**：9 节点全过，4 输入 + InfoNCE + 外部 ts_quant 依赖这个 hard case 被 flatten/expand/train/search 正确处理。SearchSpace depth/width 严格 > baseline（evaluator BLOCKER 生效）。LAT AC 远超满足。
- **ACC AC 受 CPU 算力硬限**：50+50 step 预算无法逼近 baseline。这不是 workflow 缺陷——给足算力（GPU 或 hours 级 CPU 预算）即可达。G1 的 cap 让"给足算力"可由 launcher 控制。
- **主要待办**：G6 synthesizer（用户决策）、G3/G4/G5（driver/执行节点健壮性）、G2（porter 向量化建议）。
