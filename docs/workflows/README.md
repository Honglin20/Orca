# Orca Workflows

## 1. 安装与使用（TARS）

Orca 通过 **TARS**（一个 skill）在你的 AI 编码工具（Claude Code / opencode）内驱动 workflow。装一次，之后一句话触发。

### 安装

```bash
# 1. 克隆仓库
git clone <repo-url> orca && cd orca

# 2. 安装 Orca 核心包
pip install -e .

# 3. 将 TARS skill 装进你的主 session 工具
tars install --target all
```

`--target` 选择前端：

| target | 宿主 |
|--------|------|
| `cc` / `cac` | Claude Code（`.claude` / `.cac`） |
| `opencode` / `nga` | opencode（`.opencode` / `.nga`） |
| `all` | 全装（默认） |

装完验证：

```bash
orca doctor          # 诊断集成层（skill 落点 / CLI imports）
orca list            # 列出全部可用 workflow
```

### 使用

在主 session 里直接用自然语言告诉 TARS 要做什么。TARS 有两项能力：

**驱动已有 workflow** — 匹配并执行：

```
用 TARS 帮我分析这个模型的量化敏感层
用 TARS 对 vit_tiny 做一轮 PTQ 扫描
用 TARS 跑 quant-qat
```

**创建新 workflow** — 从描述生成，或把已有 agent prompt 集合转换过来：

```
帮我新建一个 workflow：先调研论文，再生成代码，最后写测试
把 frontend/ 里那堆 agent prompt 转成 Orca workflow
```

生成后自动跑 `orca validate` 自校验（0 error 才算完成），画草 DAG 报告，直接落盘到 `./workflows/`。

运行中可随时打开可视化面板：

```bash
orca open          # 浏览器打开 web 面板，实时查看进度和图表
```

> 更多细节见 [in-session 使用指南](../in-session-usage.md)。

---

## 2. Orca 与 Claude Code / opencode 的关系

### 定位

```
┌───────────────────────────────────────────────────────┐
│              你的 AI 编码会话                           │
│  （Claude Code / opencode 主 session）                  │
│                                                         │
│   "用 TARS 帮我做 NAS"                                  │
│          │                                              │
│          ▼                                              │
│  ┌───────────────┐                                     │
│  │  TARS (skill) │  ← 一个 skill，装在 opencode/CC 内   │
│  │  意图→编排    │     把自然语言翻译成 orca CLI 调用    │
│  └───────┬───────┘                                     │
│          │ 调 orca CLI                                  │
│          ▼                                              │
│  ┌───────────────────────────────────────┐             │
│  │           Orca 编排引擎                │             │
│  │  ┌─────────────────────────────────┐  │             │
│  │  │  DAG 解析 → 单指针推进          │  │             │
│  │  │  ┌──────┐  ┌──────┐  ┌──────┐  │  │             │
│  │  │  │Node 1│─▶│Node 2│─▶│Node 3│  │  │             │
│  │  │  │子代理│  │子代理│  │子代理│  │  │             │
│  │  │  └──────┘  └──────┘  └──────┘  │  │             │
│  │  └─────────────────────────────────┘  │             │
│  │                │                       │             │
│  │                ▼                       │             │
│  │  ┌─────────────────────────────────┐  │             │
│  │  │  Event Tape（唯一真相源）         │  │             │
│  │  │  → 可 replay / 时间旅行调试      │  │             │
│  │  └─────────────────────────────────┘  │             │
│  └───────────────────────────────────────┘             │
│          │                                              │
│          ▼ 子代理实际执行                                │
│  ┌───────────────────────────────────────┐             │
│  │   AI 后端（opencode / claude / codex） │             │
│  │   → vendor-neutral，可混用              │             │
│  └───────────────────────────────────────┘             │
└───────────────────────────────────────────────────────┘
```

### 三者关系

| 组件 | 是什么 | 角色 |
|------|--------|------|
| **Claude Code / opencode** | AI 编码工具（你的主 session） | 承载 TARS skill，派子代理干活 |
| **TARS (skill)** | 一个 install 进主 session 的 skill 文件 | 意图翻译层：自然语言 → `orca` CLI 调用 |
| **Orca (Python 包)** | vendor-neutral 编排引擎 | 解析 YAML DAG → 单指针推进 → 事件 tape，不执行具体任务 |

### 原理简述

1. **工作流用 YAML 描述**：每个 workflow 是一个 DAG（有向无环图），节点定义子代理做什么、用什么工具、产出什么。
2. **TARS 做两件事**：(a) **驱动**已有 workflow —— `orca list` 匹配 → `orca <wf>` 取参数 → `orca <wf> --inputs` 启动 → 逐节点派子代理循环到 done；(b) **创建**新 workflow —— 自然语言/已有素材 → 建归一化 DAG → 生成 YAML + agent 文件 → `orca validate` 自校验通过落盘。
3. **Orca 做编排**：解析 YAML 成 DAG，按拓扑序推进，每到一个节点生成子代理指令，交给主 session 派 Task 执行，子代理产出回传后推进到下一节点。
4. **Event Tape**：每一步的状态变化都写入 event tape（单文件 append-only），是唯一真相源——可随时 replay 看历史、调试失败原因。

---

## 3. 已有 Workflow

| Workflow | 类别 | 说明 |
|----------|------|------|
| `quant-sensitivity` | 量化 | 低精度敏感层分析：逐层打分排序，找出量化最敏感的层，供下游 PTQ/混精决策 |
| `quant-ptq-sweep` | 量化 | 训练后量化算法扫描：SmoothQuant / QuaRot / GPTQ / AutoRound / Q2N 等，固定位宽对比 |
| `quant-bit-curve` | 量化 | 混合精度 Pareto 位宽-精度曲线：INT8 / W4A8 / INT4 / MX4 / MX8，找最优精度-位宽折中 |
| `quant-qat` | 量化 | 量化感知训练 + CAGE 后校正：rtn / duquantpp 两方案，QAT 恢复低比特精度 |
| `nas-hp-search` | NAS | 轻量超参搜索：Elastic 超网只搜宽度/深度，脚本化挑 top-K |
| `nas-agent-pipeline` | NAS | 端到端 NAS：超参 + block 组件全搜，LLM 评估选择 |
| `agent-struct-exploration` | NAS | LLM agent 驱动结构探索：AST 级改模型结构，实测时延+精度，迭代逼近目标 |

每个 workflow 的详细文档（算法原理、输入输出、公式推导）见本目录下同名 `.md` 文件。
