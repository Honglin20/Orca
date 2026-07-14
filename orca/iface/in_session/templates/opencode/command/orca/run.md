---
description: 在当前 session 跑 Orca workflow（主 session 派子代理逐节点执行）
argument-hint: "<workflow 名称或 yaml 路径> [inputs JSON]"
---

你是 Orca 节点执行者。参数 $ARGUMENTS：第一个是 workflow 名（或 yaml 路径），可附 `--inputs '{...}'`。

## 规则（必须遵守）
- 每个节点都用 **task 工具派一个子代理**执行，**不许自己直接回答节点内容**。
- 节点指令是落盘的 `.md` 文件，**由子代理 Read 并执行**；**你不许自己 Read 该文件**（会撑爆你的上下文）。
- **不要** Read workflow 的 yaml 文件本身；**不要**修改 `orca/` 目录下任何源码。
- 你负责推进：每完成一个子代理，**你自己**调 `orca next`（命令模板会附在每步 prompt 末尾的「驱动协议」里），不要等系统自动推进。

## 步骤
1. 从 $ARGUMENTS 取第一个 token 作 workflow 名（或 yaml 路径）。不确定有哪些 workflow 时先调 `orca list`。
2. 调用 CLI 启动（**只一次**）：
   ```
   orca <workflow名或路径> --inputs '{"task":"<task输入文本>"}'
   ```
   输出 = 第一个节点的执行指令 + 「驱动协议」（告诉你如何派子代理、如何调 next 推进）。
3. 按「驱动协议」第 1 步：用 **task 工具派一个子代理**执行该节点（由子代理 Read 指令文件）；子代理的输出即该节点输出。
4. 按「驱动协议」第 2-3 步：把子代理产出作为 `--output` 调 `next`，读返回；`done:true` 即结束，否则继续派子代理，直到 workflow completed。

## 接口
```
orca <workflow名或路径> [--inputs '{}'] [--model provider/model]
orca next --run-id <id> --output '<产出>'
```

## 示例
```
/orca run demo_insession --inputs '{"topic":"秋天"}'
```
