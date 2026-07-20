# 子 agent 过程在 web 端不可见 —— 诊断说明（远程定位用）

> 适用:用 cac（或 claude code）跑 orca,web 只看到主 session 最终输出,**看不到子 agent 的 message / tool / thinking**。
> 目标:把"子 agent 进 web 需要什么、怎么核实、断了什么影响"讲清,照着在远程逐环定位。

---

## 1. 链路全景(断任何一环都会导致此现象)

```
cac 派子 agent ──①──> sidechain jsonl（数据源）
                              │
            sidechain daemon ──②── tail + ingest ──> tape ──③──> web 渲染
            （需 host_session + dotdir）              （唯一真相源）
```

- **① 数据源**:cac 把子 agent 完整过程写到 sidechain jsonl 文件。
- **② daemon**:sidechain 守护进程 tail 这些文件 → ingest 进 tape。它要知道**用哪个 host_session、读哪个 dotdir**。
- **③ tape → web**:tape 是唯一真相源,web 读 tape 渲染。tape 没有 `agent_*` 事件,web 就没子 agent。

**只有最终输出 = 主 session 的 summary 进了 tape,但子 agent sidechain 没被 ② ingest 进 tape。** 问题在 ① 或 ②。

---

## 2. 每一环:需要什么 · 如何获取 · 断了什么影响

### ① 数据源 —— cac 写 sidechain jsonl

- **需要**:cac 派子 agent 时,把子 agent 完整过程(thinking / tool_use / tool_result / text)落盘到 sidechain。
- **位置**(你说"目录与 claude 一致" → 沿用 `~/.claude`):
  ```
  ~/.claude/projects/<encoded-cwd>/<host_session>/subagents/agent-<task_id>.jsonl
  ```
  - `<encoded-cwd>` = 项目绝对路径的 `/` 替成 `-`(`/root/foo` → `-root-foo`)
  - `<host_session>` = cac 本次会话 id(**就是目录名**)
  - `<task_id>` = 每个子 agent 的 id(一个子 agent 一个文件)
- **如何核实**:
  ```bash
  find ~/.claude/projects -name "agent-*.jsonl" -printf "%T+ %p\n" 2>/dev/null | sort -r | head
  ```
  - 有文件 → 数据源 OK。`head -1 <file>` 应是合法 JSON 行(`{"type":"assistant",...}`)。
  - 无文件 → cac 没写 sidechain(cac 配置/版本问题,或本次 run 没真正派子 agent)。
- **影响**:这一步没有,后面全白搭。但你说能跑 orca + 有最终输出,说明 cac 本身在跑;重点核实它**派子 agent 时**有没有写 sidechain。

### ② host_session id —— 最常见的命门 ⚠️

- **需要**:sidechain daemon 拿到 cac 会话 id,且**等于 ① 里目录名的 `<host_session>`**。两边对得上,daemon 才能定位到正确的 sidechain 目录。
- **如何获取**:这是宿主会话**注入给它派生的子进程**的环境变量。CC 用 `CLAUDE_CODE_SESSION_ID`;cac 若"与 claude 一致",大概率同名,**但也可能 cac 改了自己的 env 名 —— 这是你这种情况最可能的断点**。
- **如何核实**(**必须在 cac 会话派生的 bash 子进程里跑**,不是外部独立终端):
  ```bash
  env | grep -iE "session|claude|cac|child" | sort
  ```
  找到形如 `CLAUDE_CODE_SESSION_ID=<hex>` 的行(或 cac 自有的 `*_SESSION_ID`)。
- **交叉验证(关键)**:env 里的 session id **必须 = ① 路径里的 `<host_session>` 段**。不一致或拿不到 → 这就是断点。
- **影响**:daemon 拿不到 host_session → 直接 skip 不 ingest(或读错 session 的空目录)→ tape 无 `agent_*` → web 空。**这是"目录结构一致却看不到子 agent"最常见的原因。**

### ③ sidechain daemon 起来 + 读对路径

- **需要**:daemon 进程在跑,且它算出的 resolved 路径 = ① 的实际目录。
- **daemon 何时起**:`orca <wf>` / `orca run`(bootstrap)时 detach spawn;`orca next` 时若发现它死了会 respawn。
- **daemon 如何定路径**:backend(从 env 推断)+ host_session(②)+ dotdir(`sidechain.family` 配置或探测)→ `~/.<dotdir>/projects/<enc>/<host_session>/subagents/`。
- **如何核实**:
  ```bash
  orca doctor            # 看输出里的 sidechain_backend check
  ```
  关注 detail 里的:`family=` / `source=` / `resolved_root=` / `root_exists=` / `available=`。
  - `resolved_root` **应该正好是 ① 的那个目录**。
  - `available=True` 才会真 ingest。
- **daemon 日志**:
  ```bash
  find . ~/.orca -name "sidechain_daemon.log" -printf "%T+ %p\n" 2>/dev/null | sort -r | head
  ```
  看启动行(`backend=... host_session=... family=...`)和 resolved root,以及任何错误。
- **影响**:daemon 没起 / resolved_root 与 ① 不符 / `root_exists=False` → 不 ingest。

### ④ tape + web

- **需要**:tape(唯一真相源)里有 `agent_*` 事件(daemon ingest 的产物)。
- **如何核实**:
  ```bash
  find . ~/.orca -name "*.jsonl" -path "*runs*" -printf "%T+ %p\n" 2>/dev/null | sort -r | head
  # 拿到最新 tape 路径后:
  grep -c '"agent_' <最新tape>
  ```
  - `>0` → tape 有子 agent 事件 → 链路通了,问题在 web 端(刷新 / 前端连接 / 渲染器)。
  - `=0` → daemon 没 ingest,回 ①–③ 排查。
- **影响**:tape 无 `agent_*` → web 渲染器没数据可渲染。

---

## 3. 远程快速定位(按序执行)

1. **① 数据源**:`find ~/.claude/projects -name "agent-*.jsonl" ...` —— 有没有?记下**完整路径**(尤其 `<host_session>` 那段)。
2. **② env**:在 cac 会话内 `env | grep -iE "session|claude|cac"` —— 拿到 session id,**和 ① 目录名比对**。
3. **③ doctor**:`orca doctor` —— `resolved_root` 和 ① 比对;看 daemon 日志启动行。
4. **④ tape**:`grep -c '"agent_' <tape>` —— 判断是 ingest 断(回 ①–③)还是 web 端。

---

## 4. cac 特别注意

- **「cac 后端」≠「.cac 目录(family)」**:cac 是 CC 换皮。"用 cac 后端" = exec 走 cac binary;但你观察到"目录与 claude 一致" = sidechain 写 `~/.claude`(family=**cc**)。
  → **你的 family 应保持默认 cc,不要设 `orca sidechain family cac`**(那是给 cac 真的换目录到 `~/.cac` 的情况)。之前讨论的"切 cac"不适用于你这种"目录同 claude"的 cac。
- **env 名**:若 cac 沿用 `CLAUDE_CODE_SESSION_ID`,daemon 自动识别(backend=cc + host_session)。
  若 cac 改了 env 名 → daemon 漏检 host_session → **需要适配**(把 cac 的 env 名告诉我,我在 `_host_session_from_env` 加识别,或在 cac 会话内临时 `export CLAUDE_CODE_SESSION_ID=<cac 的 session id>` 验证)。

---

## 5. 反馈清单(跑完贴回,我定位精确断点 + 给修复)

1. ① 的 `agent-*.jsonl` 路径(或"无")
2. ② 的 `env | grep -iE "session|claude|cac"` 输出
3. `orca doctor` 里 `sidechain_backend` check 的 detail 行
4. ③ 的 `sidechain_daemon.log` 启动行 + 任何错误
5. ④ 的 tape `agent_` 计数

**最可能结论(按你的描述预判)**:目录同 claude → ① 应该有数据 → 断点大概率在 ②(cac 的 session env 名 daemon 不认)或 ③(daemon 没起 / host_session 拿不到)。先跑 ② 确认 env 名。
