# Release: puzzle-supernet（PSU）workflow——预训练模型 choice-only 超网组件搜索

**日期**：2026-08-18　**分支**：`puzzle-supernet`　**commit**：`6b89820`

## 动机

用户 2026-08-17 拍板：以 nas-supernet-v3 为基底 **1:1 fork** 出 `puzzle-supernet`——
choice-only 超网（不搜维度：层数/头数/FFN 维度全部钉原模型值，每个层槽只在
{original(冻结) + 注意力变体块} 中选）、预训练权重继承、teacher = 原模型冻结 KD、
retrain = finetune+KD。**搜组件不搜超参**；需要预训练 ckpt（权重继承 + teacher 同源）。
puzzle-universal 分支的 decomposed 后半冻结，不合并 puzzle workflow 任何组件。

## 方案：9 节点流水线（entry `psu_flatten` → 唯一终端 reporter `psu_report`）

| # | 节点 | 职责 |
|---|---|---|
| 1 | `psu_flatten`（entry） | flatten 用户 PyTorch 模型 + 施 mandatory supernet readiness 规则 → prepared_model |
| 2 | `psu_expand_supernet` | classify model_type + 生成 `supernet.py`（choice-only）+ refine SearchSpace + summary；权重继承 + original 等价 gate |
| 3 | `psu_train_script` | 判定训练前置（数据管道/eval 入口可移植 + ckpt 可加载）→ porter 移植 → 生成固定 KD 范式训练脚本（冻结 teacher 蒸馏只训变体分支） |
| 4 | `psu_search_pipeline` | 产 `search_record_schema.json` 共享 schema → 3 子代理并行生成（latency_estimator / search-core 5 文件 / select）→ 汇总固化校验 |
| 5 | `psu_run_train` | 有界轮询 + 无上限自愈常驻执行 KD 蒸馏训练（单卡 plain python，无 torchrun） |
| 6 | `psu_run_search` | 跑搜索 → 推 3 图 → anchor 候选幂等补评（all-original + 每 slot 单换，共 L+1 条）→ select → emit |
| 7 | `psu_retrain_script` | 纯生成：恒 finetune-from-supernet（物化选定子网 + 按 selected choice 提取分支权重 strict 载入 + 冻结 teacher KD 微调）；无 from-scratch 回退 |
| 8 | `psu_retrain` | 有界轮询 + 无上限自愈常驻执行 retrain；self-heal 仅补丁 run_retrain.sh，训练逻辑 fail loud |
| 9 | `psu_report` | 唯一终端 reporter：读磁盘状态文件 first-match 判终态（零跨节点 output 引用）→ 结构化报告 → `$end` |

配套：8 个 subagents（project-porter / supernet-evaluator / search-{core,latency,select}-gen /
workflow-verifier / memory-verifier / project-fidelity-verifier）；inputs 三档标签
（[ask] project_root/model_path/pretrained_ckpt 等）+ 跨字段不变量
（`latency_unit∈{us,s} ⇒ latency_script_path` 必填，bootstrap fail-loud）。

## 过程

审计 fan-out（147 文件）→ 设计 draft（D1-D18，`docs/specs/puzzle-supernet-design-draft.md`）
→ spec-reviewer 三轮闭环 → P0-P4 实现 → 二次 fan-out 复查 → 修复。
E2E 五类问题修复（F1 收敛曲线三层 / F2 final_metrics 双保险 / F3 search_table choice-arch /
F4 sys.path bootstrap / F5 模块调用）；第一轮洁净度审查（3 major+6 minor）+ C1-C7 修复
（run_search 内联 bash 下沉三脚本、select 契约单一权威化、watcher --title 自推导、
report 表 informational、考古措辞清零、kill_search_group 补 run 归属门）。

P7 收尾再做了一轮**按节点逐个派 agent 的洁净度复审**（10 个审查 agent：9 节点 + 8 个
subagents 清单扫描，按
[agent-prompt-cleanliness-contract](../../orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md)
受众翻转通读），共 **39 处残留**（设计论证句 / 派单字母标签 / 事故复盘标签 / 版本考古 /
悬空引擎引用 / fixture 属性硬编码）全部修复；其中 1 处高优先——search 文档的 `self.cfg.lr`
示例与同文件 forbidden-fields 硬契约（零训练范式）自相矛盾。复审同时暴露并修复 2 个
环境耦合测试（search_table HTML 断言按 stdlib 兜底渲染器书写，plotly 装机时静态回退走
JS 嵌数据层导致断言失真——测试子进程经 PYTHONPATH shim 强制两级可视化依赖 ImportError，
确定性走 pure-html floor；此前「88 全绿」系 chart daemon 在线时 HTML 断言被整段跳过所致）。

## 验收

- **E2E 实测通过**（mnist_trf，run `puzzle-supernet-20260817-162124-dbae9f`，9/9 节点）：
  selected=[random_synthesizer×3, original]（非退化）、LAT 0.699ms ≤ 0.7、
  ACC 0.9297 ≥ 0.9177、gate E 56 键零漂移；真实产物 20 张图表落盘实证。
- pytest：psu 测试 88 用例全绿（7 个测试模块 + `_psu_test_fixtures`）。
- `tars validate workflows/puzzle-supernet.yaml` 0 error / 0 warning；审计附录
  `docs/specs/psu-fork-audit-inventory.json`。

## 关键产物

- `workflows/puzzle-supernet.yaml`（9 节点 DAG）
- `workflows/agents/psu_{flatten,expand_supernet,train_script,search_pipeline,run_train,run_search,retrain_script,retrain,report}/`
- `workflows/subagents/puzzle-supernet/`（8 个）
- `docs/specs/puzzle-supernet-design-draft.md`（D1-D18 + §2 节点契约 + §8 开放问题）
- `tests/test_psu_*.py`（7 测试模块 + fixture）
