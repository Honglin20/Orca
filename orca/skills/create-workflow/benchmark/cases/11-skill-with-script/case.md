# 11 — 转换：skill 含脚本 → 文件夹 agent（资产迁移 + 路径重写）

- **场景**：4+5（skill 当 agent + skill 自带 script 资产）
- **输入**：一个 CC skill 自带脚本：

```markdown
# assets/chartgen/SKILL.md
---
name: chartgen
description: 生成图表
tools: [Bash]
---
你是图表生成 agent。运行 ``python scripts/gen.py`` 生成图表并返回路径。
```
```python
# assets/chartgen/scripts/gen.py
print("chart generated → /tmp/out.png")
```
```markdown
# assets/orchestration.md
跑 chartgen。
```

用户："转成 Orca workflow，脚本要带过来。"

- **预期产物**：`expected/workflow.yaml` + `expected/agents/chartgen/agent.md` + `expected/agents/chartgen/scripts/gen.py`
- **不变量**（🔴 skill→文件夹 agent 的核心转换规则）：
  - 有脚本资源 → **文件夹 agent**（`agents/chartgen/agent.md` + `scripts/gen.py`）
  - 脚本**迁移**到 `agents/chartgen/scripts/gen.py`（原样）
  - 🔴 prompt 里脚本引用**必须重写**：CC skill 的相对路径 `scripts/gen.py` → Orca 的 `$ORCA_AGENT_RESOURCES/scripts/gen.py`（spawn 时 executor 注入该 env，agent Bash 工具据此定位资源）
  - workflow `agent: chartgen` 引用文件夹 agent
