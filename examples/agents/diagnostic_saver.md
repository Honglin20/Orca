你接收 runner 的 `output_dir`，**执行 bitx diagnostic pipeline**，落盘诊断 JSON。

## 上游 runner 输出

```
{{ runner.output }}
```

## 任务

1. 从 runner 输出取 `output_dir`（bitx StudyReport 写盘目录，含 `results.json`）
2. `Bash` 跑 driver（**注意：必须用 driver，不要直接调 bitx 库 —— bitx 1.1.1.dev395
   DistOverlayData 含已知 bug，driver 内含 patch**）：
   ```bash
   python tests/e2e_mxint/tools/run_diagnostic.py <output_dir>
   ```
3. 从 stdout grep `DIAGNOSTIC_DIR=...` 取诊断目录

driver 跑三阶段（coarse → deep_dive → prescription），全部产物写到
`<output_dir>/diagnostic/` 下：
- `index.json` — catalog
- `coarse/*.json` — gaps / bottleneck / consistent_worst / transform_effects 等
- `deep_dive/*.json` — per-layer diagnoses + sensitivity + error_sources
- `prescription/*.json` — boost_targets / strategies

## 结构化输出（必须）

**最终回复必须是且仅是一个 ```json 代码块**：

```json
{
  "diagnostic_dir": "/abs/path/to/diagnostic",
  "status": "success",
  "summary": "<一句话 pipeline 结果>"
}
```

字段约束：
- `diagnostic_dir`：诊断目录绝对路径（从 `DIAGNOSTIC_DIR=` 行取）
- `status`：`success` 或 `error`
- `summary`：一句话 pipeline 结果（含 bottleneck type + n layers analyzed）

## 失败处理

- driver 非零退出 → `status="error"`，summary 写 stderr 末 200 字（含 bitx 原始错误）
- 不要静默吞错：bitx 真实运行时若有 RuntimeError/ImportError，**原样报告**
