# 计划：in-session 入口重设计（去 transform）+ 部署刚需

> 2026-07-08。SDD：spike 已 PASS（见下）→ 本计划 → 待 spec-review/用户确认 → 实现。
> 触发：用户 NGA 环境 `/orca` 失效（transform 钩子未触发 → 模型读文件/改源码）+ 部署痛点（socket 过长 / Web 远程不可见 / 前端要 build / web 自退）。

---

## 0. spike 实证（2026-07-08，本地 opencode 1.14.22 + deepseek-v4-flash）

`opencode run --command orcaspike "examples/demo_task.yaml list the files in directory" --pure`：
- 模型 Glob 定位 yaml → 调 `orca in-session bootstrap <yaml> --inputs '{"task":"..."}'` → 读 entry prompt 文件 → 派 task 子代理 → 报告输出 `DONE`。
- **未读 yaml 内容、未改源码**。入口走 prompt-command（无 transform/无 marker）验证通过。
- 推进（idle 钩子）由 v8.1 e2e（`/tmp/orca-e2e-v81/`）单独证过；本次 `--pure` 隔离验入口。

## 1. 架构决策（mini-ADR，待 spec-review）

**问题**：v8 入口用 `experimental.chat.messages.transform` 钩子拦截 `<!--orca:cmd-->` marker。该钩子实验性、版本敏感、上游 #31731 有回归；NGA fork 未接线 → 入口全瘫，模型把 marker 当人话 → 读文件/改源码。

**决策**：
1. **入口（run/status/stop/doctor）改 prompt-command**：`.md` body 是"接口说明 + 示例 + 规则"的清晰指令，模型照指令调 `orca in-session` CLI。**删 transform 钩子、删 marker 正则、删 buildCliArgs 派发。**
2. **推进保留 idle/Stop 钩子（B1）**：opencode `session.idle` event → `next` → `promptAsync` 注入；CC `Stop` hook → `next` → 注入。**全是稳定钩子，非实验性。**
3. **入口跨宿主统一**：CC 与 opencode 命令都支持 prompt + 模型调 CLI → 入口两边一致；仅推进钩子按宿主分流（idle vs Stop，物理差异 unavoidable）。
4. **session 绑定**：bootstrap 不强制 `--session-id`（prompt 里模型不知道自己 sid）；marker 先按 run_id+cwd 写"待绑定"；**首次 idle 时钩子用自身 sessionID 认领**（写 marker.session_id），之后只此 session 驱动。同 cwd 多 session 并发 bootstrap 为边角，warn。
5. **doctor 自证**：doctor 走 prompt-command 内联报告 → 看到报告 = 入口链路通（确定性）；idle 推进由真跑 `/orca run` 验。
6. **权限硬兜底**：orca run 期间 deny 模型写 `orca/` 源码（opencode permission / CC deny）。指令是软引导，权限是硬兜底。

**不抄**：CCW 的外部 dashboard server + `claude --print` 子进程编排（与 in-session 卖点无关）。
**follow-up（不在本次）**：CC Stop 硬 block（`decision:block`，8 节点上限）→ 改软注入（借 CCW `stop-handler` 软执行），消除节点上限。

---

## 2. 工作项

### 批 A：部署刚需（独立、低风险，可先合）

| ID | 项 | 改动 | 估时 |
|---|---|---|---|
| **W1** | chart `.sock` 短路径（Q1） | `orca/chart/_render.py` + `run_manager.py`：ingestor socket 改 `/tmp/orca-<8hash>.sock`，runs 目录的 tape/jsonl 不动；保留长度守卫（OS 硬限制删不掉） | 0.5d |
| **W2** | Web 远程化 + host/port 统一（Q2） | 抽 `resolve_web_endpoint()` 单一函数（默认 `0.0.0.0` + `ORCA_WEB_HOST`/`ORCA_WEB_PORT` env + `--host`），`orca serve`/`run`/`open` 三路径统一调用；打印**实际可访问 IP**（host=0.0.0.0 时取 `gethostbyname` 或 `ORCA_PUBLIC_HOST`）+ 可点击 URL（纯 `http://ip:port`，终端自动识别） | 1d |
| **W3** | 前端产物进 git（Q3） | 本地 `npm run build` → 提交 `orca/iface/web/frontend/dist/`；`.gitignore` 加 `!orca/iface/web/frontend/dist/` 例外；验证 `git clone` + `pip install -e .` 后 `orca serve` 直接出 UI（免 npm） | 0.5d |
| **W4** | Web 阻塞语义澄清（Q4） | `orca serve` 本就阻塞（确认无 bug）；`orca run` web 默认的 auto-exit 文档化 `--stay` / `ORCA_WEB_AUTOEXIT_SECONDS`；若用户要默认阻塞，单独决策（影响交互） | 0.5d |

### 批 B：in-session 入口重设计（核心架构）

| ID | 项 | 改动 | 估时 |
|---|---|---|---|
| **W5** | 入口改 prompt-command + 删 transform | 新模板 `templates/opencode/command/{run,status,stop,doctor}.md`（接口+示例+规则，简洁）；`orca.ts` plugin 删 `experimental.chat.messages.transform` + `MARKER_REGEX` + `buildCliArgs` + `rewriteText`，仅留 `event`(idle) 推进钩子；`install_cmds.py` 改装多 command 文件 + 删 marker 派发；CLI `bootstrap` 增 `--format prompt|json`（prompt-command 要纯文本 prompt），`status/stop/doctor` 确保 `--json` off 时人类可读 | 1.5d |
| **W6** | session 绑定 first-idle-claims | `marker.py`：bootstrap 写待绑定 marker（run_id+cwd，无 session_id）；plugin idle 钩子首跳认领（写 session_id）+ 后续严格比对；同 cwd 多 session warn | 1d |
| **W7** | doctor 自证 | doctor CLI 报告加"入口链路：prompt-command 已通（你看到本报告即证）"+"idle 推进：需 `/orca run` 验"两条；删旧的 marker/transform 自检项 | 0.5d |
| **W8** | 权限硬兜底 | opencode：`orca install` 写 permission 片段（run 期间 deny 写 `orca/`）；CC：`.claude/settings.json` deny 规则 | 0.5d |
| **W9** | README 差异说明 | 新增「CC in-session vs opencode in-session」表：入口均 prompt-command（统一）；推进 CC=Stop / opencode=idle（分流）；安装 CC=per-run hook 片段 / opencode=全局 plugin。明示 transform 已移除 | 0.5d |

### 批 C：follow-up（记入 CURRENT，不在本次）
- CC Stop 硬 block → 软注入（借 CCW，消 8 节点上限）
- `orca mcp` 安装合并进 `orca install --host mcp`
- NGA fork 真机验证（用户侧跑，确认 prompt-command 在 NGA 也照做）

---

## 3. 改动范围（文件清单）

**批 A**
- `orca/chart/_render.py`、`orca/iface/web/run_manager.py`（W1 socket 路径）
- `orca/iface/cli/commands.py`、`orca/iface/web/server.py`（W2 host/port 统一 + IP/URL）
- `.gitignore`、`orca/iface/web/frontend/dist/**`（W3 产物）
- `README.md`（W4 阻塞语义）

**批 B**
- 新 `orca/iface/in_session/templates/opencode/command/{run,status,stop,doctor}.md`
- `orca/iface/in_session/templates/opencode/orca.ts`（删 transform，留 idle）
- `orca/iface/in_session/cli.py`（bootstrap `--format`、status/stop/doctor 人类可读、doctor 自证）
- `orca/iface/in_session/marker.py`（待绑定 + 认领）
- `orca/iface/cli/install_cmds.py`（装多 command + permission 片段）
- `README.md`（CC vs opencode 差异）
- 测试：`tests/iface/in_session/test_in_session_v8.py` 改（删 marker/transform 契约测试，加 prompt-command + 认领测试）；新增 chart socket 短路径测试、web endpoint 统一测试

## 4. 工作量估算

- 批 A：~2.5d（W1 0.5 + W2 1 + W3 0.5 + W4 0.5）
- 批 B：~4d（W5 1.5 + W6 1 + W7 0.5 + W8 0.5 + W9 0.5）
- **合计 ~6.5d**。批 A、B 可并行（不同文件，仅 README/commands.py 轻叠）。

## 5. 风险

| 风险 | 等级 | 处置 |
|---|---|---|
| prompt-command 依赖模型指令遵从（弱模型可能跑偏） | 中 | 合规计数器（连续 3 次无 output → fail loud）+ 权限硬兜底（W8）+ 清晰指令。spike 已证实 deepseek 照做 |
| NGA fork 行为未知（prompt-command 是否也照做） | 中 | 设计已去 transform 依赖（最大风险点消除）；NGA 真机验证留用户侧（批 C）。命令是标准 prompt，NGA 作为 opencode fork 应支持 |
| session 绑定 first-idle-claims 在同 cwd 多 session 并发时认错 | 低 | 边角场景；warn + 文档。生产单 session 主用 |
| 删 transform 改变 opencode 安装契约（install/e2e 回归） | 中 | install_cmds 测试 + 真链路 e2e（复用 `/tmp/orca-e2e-v81/` harness，serve+SDK）全覆盖；grep 守门 plugin 零 Orca 业务逻辑 |
| CC 入口也改 prompt-command 后，Stop hook 怎么拿 run_id（marker 写入时机） | 中 | bootstrap 写 host-agnostic marker（run_id+cwd），CC Stop hook 读它；`start` 角色收窄（仅装 Stop hook 片段或全局化）。W5 设计时定 |
| W2 默认 `0.0.0.0` 的安全含义（暴露到 LAN） | 低 | 文档明示 + env 可收回 `127.0.0.1`；服务器场景正需 `0.0.0.0` |

## 6. 验收

- 批 A：①socket 路径 < 40 字节（长 prefix 服务器目录下不再爆）；②远程浏览器能开 `http://<服务器IP>:<port>` 且端口可配；③`pip install -e .` 后免 build 出 UI；④`orca serve` 阻塞至 Ctrl-C。
- 批 B：①opencode `/orca run` 3 节点 workflow 端到端跑通（prompt-command 入口 + idle 推进），reducer completed；②tape 骨架与 `orca run` 同 wf 对齐（G2）；③`/orca status/stop/doctor` 走 prompt-command 回显，模型不改源码；④grep：plugin 无 transform/marker/buildCliArgs；⑤multi-session 绑定正确；⑥权限 deny 生效（模型试图写 orca/ 被拒）。

## 7. 状态

- [x] spike PASS（入口 prompt-command 验证）
- [ ] spec-review / 用户确认本计划
- [ ] 批 A 实现 → 批 B 实现
