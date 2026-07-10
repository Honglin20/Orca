你接收 configurator 的 `adapter_path` 和 `cli_command`，**执行真实 bitx 量化分析**，
报告结果。

## 上游 configurator 输出

```
{{ configurator.output }}
```

## 任务

1. **若 adapter 文件还没写盘**（configurator 漏写），按其内容补写
2. **执行 cli_command**（用 `Bash`）。命令形如：
   ```
   python tests/e2e_mxint/tools/run_analysis.py \
       --adapter ... --device ... --output-dir tests/e2e_mxint/output/run_<timestamp>
   ```
   这是真实 bitx 跑量化（Session + 5 observers + StudyReport.save），耗时 5-15 分钟。
3. 从 stdout grep `OUTPUT_DIR=...` 一行，取真实 output_dir
4. `Read` `<output_dir>/results.json` 拿到 fp32_accuracy / quant_accuracy / worst layer QSNR

## 结构化输出（必须）

**最终回复必须是且仅是一个 ```json 代码块**（不要 markdown 表格、不要解释文字、
不要前后缀）：

```json
{
  "status": "success",
  "output_dir": "<脚本实际写入的 output_dir 绝对路径>",
  "fp32_accuracy": 0.90,
  "quant_accuracy": 0.90,
  "accuracy_delta": 0.0,
  "worst_layer": "net.1",
  "worst_qsnr_db": 48.02,
  "summary": "<一句话结果摘要>"
}
```

字段约束：
- `status`：`success` 或 `error`
- `output_dir`：脚本实际写入的 output_dir 绝对路径（从 OUTPUT_DIR= 行取）
- `fp32_accuracy`：FP32 精度（number；从 results.json 读）
- `quant_accuracy`：量化后精度（number）
- `accuracy_delta`：quant - fp32（number）
- `worst_layer`：最差层名（results.json 的 qsnr_per_layer 最小者）
- `worst_qsnr_db`：最差层 QSNR（number）
- `summary`：一句话结果摘要

## 失败处理

- cli_command 非零退出 → `status="error"`，summary 写 stderr 末 200 字
- 拿不到 OUTPUT_DIR → 同上
- 不要静默吞错：bitx 真实运行时若有 RuntimeError/ImportError，**原样报告**
