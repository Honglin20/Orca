# Orca In-Session 使用指南

> In-session 模式：你照常在自己的 **Claude Code / opencode 主 session** 里工作，TARS（一个 skill）把你的话翻译成「调哪个 workflow、带什么参数」，然后用 Orca 引擎在**同一个 session 内**把 workflow 一节点一节点跑完——每个节点派一个子代理干活，TARS 只负责调度、把产出回传推进。不开新终端、不启动后台守护进程托管业务逻辑。

整个过程已经很简单：**装一次 → 用一句话触发**。

---

## 1. 安装（一次性）

Orca 是一个 Python 包，连同一个叫 **TARS** 的 skill 一起装进你的主 session。

```bash
# 1. 装 Orca 包（任一方式）
pip install -e .                 # 从本仓库源码装
# 或：pip install orca           # 从发版装（若有）

# 2. 把 TARS skill + nudge hook 装进你的主 session
tars install --target all        # 装到所有前端（cc / opencode / cac / nga）
# 可选：--scope project 只装当前项目；默认 --scope user 全局
```

`--target` 选一个前端或 `all`：

| target | 宿主 |
|---|---|
| `cc` / `cac` | Claude Code（`.claude` / `.cac`） |
| `opencode` / `nga` | opencode（`.opencode` / `.nga`） |
| `all` | 全装（默认） |

装完自检一下：

```bash
orca doctor          # 诊断集成层（skill 落点 / CLI imports / hook 心跳）
orca list            # 应列出全部 workflow（name + description）
```

> `tars` 是安装/运维入口（`tars install` / `tars list` / `tars validate` / `tars serve`）；`orca` 是 in-session 驱动入口。**启动/推进 workflow 永远走 `orca`，由 TARS skill 编排调用。**

---

## 2. 使用（每次）

### 方式 A：一句话意图（推荐）

在主 session 里直接说想做什么，TARS 会自动匹配 workflow：

```
用 TARS 帮我分析这个模型的量化敏感层
用 TARS 对 vit_tiny 做一轮 PTQ 扫描
TARS，找一下位宽和精度的折中曲线
```

TARS 的匹配逻辑：`orca list` 拿到全部 workflow 的 `description` → 据你的话**语义匹配** → 命中唯一就启动；有多个可能就简短问你选哪个（最多 2 问，不会把列表甩给你）。

### 方式 B：直接点名 workflow

```
用 TARS 跑 quant-qat
```

### 它在背后做了什么（三步，你不用手动跑）

```
1. orca list                         # 选 workflow（按 description 匹配）
2. orca <wf>                         # 不带 --inputs → 拿 inputs_schema，据此从你的话抽参数
3. orca <wf> --inputs '{...}'        # 启动 → 拿到 run_id + 首节点指令
   → 派 Task 子代理执行该节点
   → orca next --run-id <id> --output '<子代理产出>'   # 回传产出，推进到下一节点
   → 循环，直到 done:true
```

整个流程在主 session 内闭环。**你只需回答 TARS 偶尔的澄清问题**（比如参数填不准时）。

---

## 3. 7 个命令（TARS 调，你也可以手动调）

```
orca list                          # 列可用 workflow（name + description）
orca <wf>                          # 不带 --inputs → 返 inputs_schema（查参数）
orca <wf> --inputs '{...}'         # 启动 workflow（返 run_id + 首节点指令）
orca next --run-id <id> --output '<产出>'   # 推进一步（回传上一步子代理产出）
orca status [--run-id <id>]        # 看进度
orca stop --run-id <id>            # 停掉一个 run
orca open [--run-id <id>]          # 打开 web 监控面板（可选，可视化看进度/图）
```

---

## 4. 可视化监控（可选）

跑的时候想看实时进度和图表：

```bash
orca open              # 浏览器打开 web 面板，attach 到当前活跃 run
```

量化 workflow 产出的图（line / bar / heatmap / table）会实时推到这个面板。

---

## 5. 续跑

session 断了、或一个 run 没跑完：

```bash
orca status                 # 看哪个 run 还在 / 可续
orca next --run-id <id> --output ''   # 无 output 重发当前节点指令，继续推进
```

---

## 小结

- 装一次：`pip install -e . && tars install --target all && orca doctor`
- 用：跟主 session 说「**用 TARS 做 X**」
- 看：`orca open`

> 各 workflow 的具体用法、算法原理、结果示例见 [`docs/workflows/`](workflows/) 下每篇文档。
