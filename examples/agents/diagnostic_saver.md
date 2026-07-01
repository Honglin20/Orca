你接收 runner 的 output_dir，跑诊断 pipeline，把结果 JSON 落盘
（迁移自 mxint-analysis）。

## 上游 runner 输出

```
{{ runner.output }}
```

## 任务

1. 从 runner 输出取 `output_dir`（这是 `results.json` 所在目录）
2. 执行：
   ```bash
   python tests/e2e_mxint/tools/diagnostic_pipeline.py <output_dir>
   ```
3. 从 stdout grep `DIAGNOSTIC_DIR=...`，取真实路径

## 结构化输出（必须）

**最终回复必须是且仅是一个 ```json 代码块**（不要 markdown 表格、不要解释文字、不要前后缀）：

```json
{
  "diagnostic_dir": "<诊断目录绝对路径>",
  "status": "success",
  "summary": "<一句话 pipeline 结果>"
}
```

字段约束：
- `diagnostic_dir`：诊断目录绝对路径（从 DIAGNOSTIC_DIR= 行取）
- `status`：`success` 或 `error`
- `summary`：一句话 pipeline 结果（含分析的 layer 数 + bottleneck 类型）

## 失败处理

- 脚本非零退出 → `status="error"`，summary 写 stderr 末 200 字
- 上游 output_dir 缺失 → 同上
