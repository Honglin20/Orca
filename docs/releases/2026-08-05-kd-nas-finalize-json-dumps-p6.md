# Release Note — finalize JSON 改 json.dumps 发射（P6）

**日期**：2026-08-05
**Commit**：`4cd2428`
**分支**：`in-session-unified-backend`

## 背景（真实失败定位）

KD-NAS 真跑 `examples/mnist_kd/`（MNIST [1,1,28,28]→[1,10]，max_rounds=2 / full_epochs=2 / cpu）
run_id `kd-nas-20260805-011253-6c2ebe` 全链路 **12/13 节点 PASS**（含真 10-epoch CPU 训练 +
2 轮真蒸馏，r2_student latency 25.83µs < target 28µs 达标），**仅 finalize FAIL**。

finalize 失败现象：agent 末条消息的 JSON **结构性畸形**——`json.loads` 报
`Expecting ',' delimiter: char 1522`，`final_depth=1`（缺一个根级 `}`）。用旧「全串接」语义重抽
同位置也 fail → 与 P5（engine result 抽取）无关，属 workflow-agent 层 bug。

根因：finalize 节点用**手写 ```` ```json ```` 模板**填值发射（`workflows/kd-nas.yaml` finalize 节点
inline prompt 末尾 output 模板）。agent 手填 JSON → 易漏逗号/括号。**其它节点（distill/decide）
早已用 `python3 -c 'import json; print(json.dumps({...}))'` 安全发射**（防 stderr 引号/裸换行注入
+ 结构保证），finalize 没遵循此模式。

## 实现

`workflows/kd-nas.yaml` finalize 节点 inline prompt 末尾两段重写：

1. **删** 原 ```` ```json ```` 手写模板（旧 528-536 行）。
2. **新增 Step 3：emit 最终 JSON（python json.dumps 防 injection / 结构保证）**——
   `python3 -c '... print(json.dumps({...}))' "$CHAMP_STUDENT" "$FINAL_ONNX" "$FINAL_LATENCY"
   "$FINAL_ACCURACY" "$FINAL_REPORT" "$VIZ_STDOUT"`，对齐 `workflows/agents/distill/agent.md`
   step 6（line 316-331）+ `workflows/agents/decide/agent.md` 的安全发射模式。
3. **viz 解析合并进 Step 3 单 try/except**——消除原 Step 2/3 双层兜底不对称：原 Step 2 用裸
   `json.loads(sys.argv[1])` 解析 viz_kd_stage stdout（无 try/except，失败时 `VIZ_STATUS` 静默变
   空串）；现在直接把 raw `VIZ_STDOUT` 传进 Step 3 的 emit python，单点 try/except 兜底
   `{"env_status": "generic", "charts": {}}` + stderr 显式告警（`[finalize] VIZ_STDOUT parse
   failed ({e!r}); fallback to generic`），与 schema 注释「viz 为 sidecar：失败值不阻断主流程」
   + Rule 7（易定位）一致。

### Schema / 业务逻辑零改动

- `output_schema`（final_model / final_onnx / final_latency_us / final_accuracy / final_report /
  viz_status）字段与类型**完全不变**。
- Step 1（`finalize_kd.py` 调用：champion eval/ONNX/latency/final_report.md）零改动。
- Step 2（`viz_kd_stage.py --stage final` 调用）零改动，仅删除其后的 inline python parse。
- `routes` / workflow `outputs` 零改动。
- 其它 12 节点零改动。

### fail loud 保证

- `float(sys.argv[3])` / `float(sys.argv[4])`：若 `FINAL_LATENCY` / `FINAL_ACCURACY` 为空
  （`finalize_kd.py` 异常路径），抛 `ValueError` → python 退出码非 0 → agent 末条消息非合法
  JSON → fail loud（与 Step 1 的 `exit 2` 守卫一致，Rule 12）。
- viz sidecar：解析失败走 generic 兜底但**显式 stderr 提示**，不静默吞错。

## 验证

- `tars validate workflows/kd-nas.yaml`：通过。
- 守门测试 `tests/workflows/test_kd_prompt_no_source_narrative.py`：1 passed（无 source-narrative
  / decision-tag 引入）。
- kd-nas 相关测试套件（finalize_kd / kd_engine_trainer / kd_redesign / kd_reducer / kd_train_script
  / struct_kd_p7 / viz_kd_stage_metrics_tail + 守门）：169 passed / 2 skipped。
- code-reviewer 一轮闭环：0 must-fix / 2 nice-to-have 已在本次合并（stderr 日志 + Step 2/3
  兜底对称合并）。
- 真实 e2e：见文末「真实 e2e 终态」（run_id 见 CURRENT.md）。

## 偏差

无。原计划「只改 finalize 节点 JSON 发射方式」严格落地；reviewer 建议的两处 🟡 鲁棒性补强
（stderr 显式告警 + 合并 Step 2/3 viz 解析）在本次一并合并——属同一 surgical fix 范围内的
自然收口，不扩大 blast radius（仍只动 finalize inline prompt 块）。
