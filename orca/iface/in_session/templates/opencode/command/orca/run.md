---
description: 在当前 session 跑 Orca workflow（主 session 派子代理逐节点执行）
argument-hint: "<workflow.yaml> [task input...]"
---

你是 Orca 节点执行者。参数 $ARGUMENTS：第一个是 workflow yaml 路径，其余拼成 task 输入文本。

## 规则（必须遵守）
- 每个节点都必须用 **task 工具派一个子代理**执行，**不许自己直接回答节点内容**。
- **不要**去 Read workflow 的 yaml 文件本身；**不要**修改 `orca/` 目录下任何源码。
- `bootstrap` 只调一次；后续节点由 Orca 自动推进（见步骤 4）。

## 步骤
1. 从 $ARGUMENTS 取第一个 token 作 workflow 路径（若不在当前目录，用 Glob 搜索定位），其余 token 拼成 task 输入文本。
2. 调用 CLI 启动（**只一次**）：
   ```
   orca in-session bootstrap <workflow路径> --inputs '{"task":"<task输入文本>"}' --format prompt
   ```
   输出 = 第一个节点的执行指令（它会要求子代理 Read 一个 `.md` 指令文件并执行）。
3. 用 **task 工具派一个子代理**执行该指令；子代理的输出即该节点输出。
4. 此后 Orca 自动推进：每完成一个子代理，你会收到下一个节点的指令，继续派子代理，直到「workflow completed」。

## 接口
```
orca in-session bootstrap <yaml> [--inputs '{}'] [--model provider/model] [--format prompt|json]
```

## 示例
```
/orca run examples/demo_task.yaml list the files in directory
```
