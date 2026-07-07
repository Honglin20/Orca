# 16 — 单 script → 文件夹 agent

- **场景**：F（脚本封装成 agent，且要带脚本走）
- **输入**：

```python
# assets/analyze.py —— 复杂分析脚本
print("analysis result: ...")
```

用户："把这个 analyze.py 封成一个 agent 来跑，脚本要带过来。"

- **预期产物**：`expected/workflow.yaml` + `expected/agents/analyze/agent.md` + `expected/agents/analyze/scripts/analyze.py`
- **不变量**：
  - 与 case 15 的区别：用户要"带脚本走" → 封**文件夹 agent**（非 script 节点）
  - 脚本迁移到 `agents/analyze/scripts/analyze.py`（原样）
  - agent.md prompt 用 `$ORCA_AGENT_RESOURCES/scripts/analyze.py` 引用（spawn 时 env 注入）
