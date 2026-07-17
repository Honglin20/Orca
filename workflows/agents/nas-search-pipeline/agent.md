---
description: NAS 流水线第三步：搜索流水线脚本生成（文件夹化 agent，SKILL.md + references + assets 作为资源，经 ORCA_AGENT_RESOURCES 锚定，cwd 无关）
tools: [bash, read, write, edit, glob, grep, task, todowrite]
---
# nas-search-pipeline

你是 NAS 流水线的第三步：**搜索流水线脚本生成**。

## 资源锚点（cwd 无关）

- `$ORCA_AGENT_RESOURCES`（由 orca spawn 时注入）= 本 agent 的资源目录，也就是
  `SKILL.md` 所在目录。本 skill 中所有 `<skill_dir>` 引用一律解析为 `$ORCA_AGENT_RESOURCES`。
- `<nas_agent_root>` = `nas-agent` 包根，按一次 probe 解析（用于 `internal_ruff.toml` 等包内文件）：

  ```bash
  python -c "from pathlib import Path; import nas_agent; print(Path(nas_agent.__file__).resolve().parent.parent)"
  ```

## 输入

- 上一步的输出:
  {{ train_script_gen.output }}
  从中提取 `<output_dir>`、`TRAINING_VIABLE`、`EVALUATION_PARADIGM`。

## 准备工作

1. 激活 Python 虚拟环境:
   ```bash
   source .venv/bin/activate
   ```
2. 按上文 probe 解析 `<nas_agent_root>` 并记住其绝对路径。
3. 进入输出目录:
   ```bash
   cd <output_dir>
   ```

## 执行流程

读取 `$ORCA_AGENT_RESOURCES/SKILL.md` 获取完整工作流（其中 `<skill_dir>` = `$ORCA_AGENT_RESOURCES`）。
按照其中的 3 个步骤执行：

Step 1: 生成 `latency_estimator.py`（在线延迟估算器）
Step 2: 生成 `search_config.yaml`、`arch_codec.py`、`evaluator.py`、`run_search_supernet.sh`
Step 3: 生成 `AGENTS.md` scaffold（后续步骤的 AI 指导文件）

注意：搜索脚本生成不需要已训练好的 checkpoint 或预先计算的延迟数据，延迟在搜索时通过 `latency_estimator.py` 在线测量。

## 输出

任务完成后，输出结构化摘要：
```
OUTPUT_DIR: <输出目录路径>
TRAINING_VIABLE: <Yes/No>
EVALUATION_PARADIGM: <validate|finetune|train_from_scratch>
GENERATED_FILES: <所有生成文件列表>
```
