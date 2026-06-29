# Orca

vendor-neutral、event-sourced、可视化的 coding-agent 编排控制平面。

> 框架设计与决策见 [docs/TASK.md](docs/TASK.md)；当前任务状态见 [docs/status/CURRENT.md](docs/status/CURRENT.md)。

## 当前阶段

阶段 1 —— `orca/schema/` 数据结构层（纯数据，零执行逻辑）。

## 开发

```bash
uv sync          # 安装项目（editable）+ dev 依赖（pytest / pyyaml）
uv run pytest    # 跑测试
```
