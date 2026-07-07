# 15 — 散 script → script 节点链

- **场景**：F（standalone scripts → script 节点）
- **输入**：用户有 3 个脚本组成 pipeline：

```python
# assets/fetch.py —— 拉数据
print("data: ...")
```
```python
# assets/process.py —— 处理
print("processed")
```
```python
# assets/report.py —— 出报告
print("report done")
```

用户："把这 3 个脚本串起来跑。"

- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - 3 个 `script` 节点链式 fetch→process→report→`$end`
  - `command` 用 shell 调脚本（``python fetch.py``）；脚本须在 cwd（script 节点无资源打包机制——与文件夹 agent 不同，**不迁移脚本**，仅引用）
  - 🔴 语义边界：要"带脚本走"见 case 11/16（文件夹 agent）；script 节点只引用 cwd 下的脚本
