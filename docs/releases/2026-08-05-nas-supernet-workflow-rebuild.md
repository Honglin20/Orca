# nas-supernet workflow 重建 + MNIST 端到端验证

> 日期：2026-08-05 ｜ 分支：`in-session-unified-backend` ｜ plan：`docs/plans/2026-08-04-nas-agent-pipeline-rebuild.md`（v5，3 轮 spec-review PASS）

## 背景
旧 `nas-agent-pipeline.yaml` 训练 acc 不符预期，根因：跑 nas-agent **重构前过期 skill 快照**，缺 `project-fidelity-verifier`（生成代码 vs 原项目语义守门）+ `project-porter` + `memory-verifier` + Non-Searchable Model Logic 处理，5 subagent 一个没迁 → 生成脚本静默偏离原项目语义。

## 交付（纯增量，零覆盖零删除——不碰 nas-hp-search 等现有 workflow/agent）
- **新 workflow `nas-supernet.yaml`**（8 节点 DAG：ns_expand_supernet→ns_train_script→ns_search_pipeline→ns_run_train→ns_run_search→ns_select→ns_retrain→ns_visualize→$end，含 terminate_unsupported/select_failed/retrain_failed 兜底）。`tars validate` 通过。
- **8 node agent**（`workflows/agents/ns_*/`）：3 生成节点从 nas-agent SKILL.md 最小适配（8 条替换：`<output_dir>`→`$ORCA_ARTIFACTS_DIR`、read+embed subagent 协议、删 ask-user/A-B consent、todolist 退化等）+ 3 auto-run/retrain（self-heal 白名单+禁碰清单+审计字段）+ ns_select（确定性）+ ns_visualize（6 图）。
- **5 subagent**（`workflows/_nas-supernet_subagents/`，body 逐字迁）+ `install_cmds.py` 扩展 `_install_bundled_subagents`（`workflows/_<wf>_subagents/`→`~/.orca/<wf>/subagents/`），node 用 `$HOME` read+embed（host-agnostic，claude/opencode/codex 同一套，零 host 注册）。
- **ns_visualize 6 图**：帕累托前沿 / 搜索过程表 / 超网训练 loss / 跨阶段指标 / 张开前后对比 / latency 分布；`push_chart` 三档（live socket → 静态 HTML/PNG → skipped），headless 下回退静态文件。31 单测。
- **MNIST fixture**（`tests/e2e_nas_supernet/fixtures/mnist/`）：小 CNN，train 最小化 CE loss，test 打印 accuracy。
- **时延脚本规则**：`latency_script_path` 提供则包装用户脚本（onnx 单文件禁 .data，`onnx.save_model(save_as_external_data=False)`）；否则 nas-agent 内置 PyTorch `measure_module_latency`。
- 旧 `nas-agent-pipeline.yaml` description 标 DEPRECATED。

## 验证（3 轮 spec-review + logic review 闭环）
- spec-reviewer 三轮（v3→v4→v5）全 PASS，闭环 I1-I12 + N1-N6 + B1-B3。
- logic review（vs 源 nas-agent）：7 维度 pass，7 issue 闭环（ns_retrain 失败 terminate、status/守卫/ckpt 契约对齐、ask-user 残留清剿等）。
- 5 subagent body + references `diff -rq` 字节级一致。

## MNIST 端到端（headless `tars run`/`tars resume`，CPU）
**workflow_completed，全 8 节点成功**：
- 超网生成（CNN）+ 训练脚本 + 搜索脚本（fidelity/workflow-verifier 全 pass）
- **超网训练 executed**（torchrun，多 epoch ckpt）
- **NAS 搜索 executed**：640 候选，acc≈0.95-0.97，latency 0.13-0.82ms
- **架构选定**：latency=0.384ms / acc=0.973（max-acc under target）
- **retrain executed**：最终 test_acc=**0.9866**
- **时延下降成立**：选定 0.384ms < 全开 0.8247ms（约 47%）
- **6 图全部渲染**（静态 HTML，`runs/<id>/artifacts/charts/*.html`）

## 关键发现：opencode.db 膨胀是 E2E 长会话问题的根因
E2E 过程中 opencode session DB 膨胀到 **786MB**（长会话累积），导致 opencode 查询变慢 → 节点7 fidelity dispatch 陷入病态长 deepseek call + 节点3 累加器边缘表现。**reset opencode.db（保留 auth.json）建全新小库后**，节点7 顺利过 fidelity → retrain → visualize，全链完成。已存 reference memory `nas-supernet-e2e-env-bridge`。

## 主要 commit
`1c1f291`(subagents+install) · `83552da`(3 gen agents) · `3505844`(run/select/retrain) · `e02245c`/`67a8286`(yaml) · `705d494`(Jinja 模板 fix) · `43679fc`(logic review 闭环) · `08d0439`(ns_visualize) · `11e173d`(静态图回退) · deprecated note。
