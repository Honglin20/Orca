# 2026-08-13 · nas-supernet-v3 删 AGENTS.md（信息无损融进对应节点）

> commit SHA 待回填。

## 背景

`AGENTS.md` 是上游 nas-agent 时代的**手动 runbook 残留**。nas-agent README 原设计（`D:\Projects\nas-agent\README.md` L133–141）：前 3 个 skill 生成产物 + `AGENTS.md`，然后人 `cd <output_dir>; ln -s AGENTS.md CLAUDE.md; claude`，靠读 `AGENTS.md` 手动跑「搜索 → 选型 → retrain」第 4 阶段。

Orca 的 `nas-supernet-v3` workflow 已把第 4 阶段**全自动化**：`ns3_run_search`（跑搜索 + 选型，**从不读 AGENTS.md**）+ `ns3_retrain_script`（生成 retrain 脚本）+ `ns3_retrain`（执行）。`AGENTS.md` 于是和自动化节点大量重复——它的内容只是「描述已生成的产物」。整个 v3 workflow 里读 `AGENTS.md` 的节点只有 `ns3_retrain_script` 一个，且只读其 "Final Weight Acquisition" 段，而该段内容已被它自己的 `references/workflows/retrain_script_generation.md` 完全覆盖（超集）。

## 决策（用户拍板）

**彻底删除 AGENTS.md**：不再生成、不再读取、删 template 资产。它承载的每条信息都已在对应节点或已生成产物里——**零信息丢失**。用户原话：「它本质上就是个 template，可以把对应的信息全部都补到对应的节点里面。」

## 零信息丢失覆盖矩阵

| AGENTS.md 段 | 现在信息已在何处 |
|---|---|
| §1 Project Context | `project_manifest.md` + `supernet_summary.md` + `supernet.py` + `{{ inputs.project_root }}` |
| §2 搜索流程 | 生成期落进 `search_config.yaml`（`objs`/`evaluator_cfg`/日志路径）；执行由 `ns3_run_search` 直接跑 `run_search_supernet.sh` |
| §3 选型 | 生成期落进 `select_architecture.py`（schema-aware + CLI + Pareto/max-acc）+ 共享 `search_record_schema.json`；由 `ns3_run_search` Step 2.8 调用 |
| §4 Final Weight Acquisition（retrain） | `ns3_retrain_script/references/workflows/retrain_script_generation.md`（超集） |
| §5 NPU Compatibility | retrain reference §1/§2/§6；§5 唯二未逐字覆盖的 2 点（可见设备 env 限制 + NPU bf16 GradScaler）并入 §2 |

## 改动

### 停止生成 AGENTS.md（`ns3_search_pipeline` 一族）

- `ns3_search_pipeline/agent.md`：删 Step 3「Generate AGENTS.md Scaffold」整段生成体（template 复制 / 占位符替换 / interactive 清理 / post-write 校验）；**保留**真正的 bookkeeping——把「Update `supernet_summary.md` + memory-verifier」作为新 Step 3（appended 产物清单去 `AGENTS.md`）。同步清理 desc / intro / Resource Anchors（`assets/` 引用）/ subagent 名单 / Step 0.5 `SKIP_C` 复用检查 / Subagent C 派发文案 / porter 禁写清单 / output JSON `agents_md_path` / `generated_artifacts` 描述。
- `scripts/check_search_pipeline.sh`：required-files 循环去 `AGENTS.md`（7→6），header/echo 注释同步。
- `assets/agents_template.md`：**删除**（纯 template，无代码 import）；空 `assets/` 目录一并移除。

### retrain 节点停止读取 AGENTS.md

- `ns3_retrain_script/agent.md`：intro / Required Inputs 去 `AGENTS.md`；删 Step 1.1「Read the scaffold」并重编号（信息已在 retrain 自己的 reference）。→ retrain 节点改为**完全自洽**，不依赖任何上游生成的跨节点文件契约。
- `references/workflows/retrain_script_generation.md`：删 Source Evidence 的 `AGENTS.md` bullet；§2 并入 §5 的 2 个 NPU 小点（`CUDA_VISIBLE_DEVICES`/`ASCEND_RT_VISIBLE_DEVICES` 禁硬编码索引；NPU bf16 autocast 可能关 GradScaler、autocast flag 仍由 `args.amp` 驱动）。
- `ns3_retrain/agent.md`：forbidden-touch 导航列表去 `AGENTS.md`。

### yaml schema

- `nas-supernet-v3.yaml`：`ns3_search_pipeline` output_schema 去 `agents_md_path`（required + properties）+ 2 处 stale 注释。

### subagent 重命名（scaffold 概念已删，名实相符）

- `subagents/nas-supernet-v3/search-select-scaffold-gen.md` → **`search-select-gen.md`**（`git mv`）：删 Step 4「Generate AGENTS.md scaffold」；description / 「2 files」→「1 file」/ Output 报告清单 / 标题 / sentinel bracket 同步新名（sentinel 字面量 `NS2SS1` 保留）。`ns3_search_pipeline/agent.md` 的 2 处引用（名单 + invoke）已同步。

## 验证

- `tars validate workflows/nas-supernet-v3.yaml` ✓（schema 合法 + `_check_prompt_dev_residue` 零 warning）。
- 残留扫描：v3 全域 `grep "AGENTS\.md|agents_md|scaffold|select-scaffold"` 零命中（`ns3_*` / yaml / `subagents/nas-supernet-v3/`）；v1 `nas-search-pipeline/` 的 AGENTS.md skill 属不同谱系、不在 scope，未触。
- `pytest tests/workflows/` = **689 passed, 4 skipped, 0 failed**（基线 687 +2 新增；skip 均为 obsolete/real-artifacts-absent，与本次无关），无回归。`test_check_retrain_script.py` 18 passed（含 2 个 DDP/sync_random_seed 负例）。
- 信息无损人工 cross-check：对照覆盖矩阵，AGENTS.md §1-5 无唯一残留事实；2 个 NPU 点已在 retrain reference §2。
- **code-reviewer 闭环**：1 🟢（`retrain_script_generation.md` 两处 stale `manifest/scaffold` 措辞——`scaffold` = `AGENTS.md` 别名、已删，复合引用失所指）随本次清理；🔴/🟡 零。零信息丢失经字节级核验（对 `git show HEAD:agents_template.md` §1-5 逐节比对）；v1/v2 字节未动（含 v1 `nas-search-pipeline/` skill 不在 scope）；output 字段无下游消费者断裂；`ns3_retrain_script` 重编号 + Required Inputs fail-loud 清单 + reference Source Evidence 共同构成完整自洽。
- **洁净度审查**（受众翻转通读 + §3/§4/§6 grep 扫 5 个改动文件）：零残留命中；改动新增 prose 均为 operational 运行时指引（§5 保留类）。

## 仅 v3

v1（`ns_*`）/ v2（`ns2_*`）**未触动**（`git diff --name-only` 核验零 v1/v2 文件改动）——它们的 `emit_result.py` 仍 gate `AGENTS.md`，保留各自设计，与 2026-08-12 retrain-split「仅改 v3」策略一致。v1 `nas-search-pipeline/` skill 文件夹（自带 AGENTS.md skill）属 nas-agent 原始谱系，v3 不依赖，未触。

## 相关文件

- 改：`workflows/nas-supernet-v3.yaml`、`workflows/agents/ns3_search_pipeline/{agent.md,scripts/check_search_pipeline.sh}`、`workflows/agents/ns3_retrain_script/{agent.md,references/workflows/retrain_script_generation.md}`、`workflows/agents/ns3_retrain/agent.md`
- 重命名：`workflows/subagents/nas-supernet-v3/search-select-scaffold-gen.md` → `search-select-gen.md`
- 删：`workflows/agents/ns3_search_pipeline/assets/agents_template.md`（+ 空 `assets/` 目录）
