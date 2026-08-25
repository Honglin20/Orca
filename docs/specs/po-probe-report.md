# P0 探针报告：in-session（claude 宿主）DAG 回边循环实证

> **依据**：[prof-opt-spec.md](prof-opt-spec.md) §5 P0-1 / [prof-opt-design-draft.md](prof-opt-design-draft.md) §8.1-§8.2。
> **日期**：2026-08-20。**环境**：WSL2 + Windows claude CLI 2.1.207（`/mnt/c/.../npm/claude` 经 interop 驱动 WSL bash）+ Orca venv（`/mnt/d/Projects/Orca/.venv`）+ GLM 后端。
> **探针文件**（全部新增，未 commit）：`workflows/po-probe.yaml`、`workflows/agents/po_probe_a/agent.md`、`workflows/agents/po_probe_b/agent.md`。

## 结论：回边主方案可行，不触发 §8.2 降级

**五项全过。prof-opt 按 10 节点回边形态（po_gate→po_propose）实现。** 跑了 3 个真实 run + 1 个无 LLM 引擎仿真 + 1 个离线 chart 实验；完整回边循环（A↔B 三轮、同节点各执行 3 次）在 in-session + claude 宿主链路下真实走通并正常终态。

## 五项验证结果

| # | 项 | 结果 | 关键证据 |
|---|---|---|---|
| ① | 回边重入（同节点二次执行） | **过** | run `po-probe-20260820-201931-b7e0d1` tape：`route_taken {from: po_probe_b, to: po_probe_a}` ×2；`po_probe_a` 3 组 `node_started`/`node_completed`（count 1/2/3），`po_probe_b` 3 组（count 2/3/4）。节点重入无去重拦截、无死锁，`orca status` replay 显示 `2/2 done` 无重复计数 |
| ② | 同节点二次执行的 reducer（最终 state） | **过** | `workflow_completed.data.outputs`：`count="4"`（B 第 3 次输出）、`a_last_count="3"`（A 第 3 次输出）——同节点多次 `node_completed` 覆盖式收敛（last-writer-wins），与 `events/replay.py:13-14/134-135` 契约一致；run 2 / run 3 两次复现一致 |
| ③ | `orca next` 中断续跑 | **过** | run `po-probe-20260820-203036-2749c5`：B 执行中 kill 宿主 claude + `kill -9` chart daemon → 新进程 `orca next --run-id X`（无 output）幂等重发 pending prompt（不重复推进、不重渲染）→ 全新 claude 会话执行节点 → 续跑到 `$end`；期间 chart daemon 被 `next` 自动 respawn（kill 后 pid 2095283 消失 → respawn 为 2095446），respawn 后 2 条 chart 事件照常入 tape |
| ④ | chart daemon 推送 | **过** | 在线：B 每轮经 `$ORCA_CHART_SOCK` push → tape 落 `custom {kind: chart}` 事件（run 2 共 3 条，data 随轮次增长 1..2/1..3/1..4）；守护从 bootstrap 存活到终态自退（`chart_daemon.log`: 启动 20:19:32 → terminal 退出 20:26:01）。离线：daemon 被 kill 且未 respawn 时 `render_chart` **fail loud**（`RuntimeError: 无法连接 Orca chart socket…连接被拒`，无静默回退；in-session 路径无静态 fallback——那是 `tars run --background` 路径的行为） |
| ⑤ | 跨节点引用回边节点 output 的取版 | **过（语义=每次渲染取「最近一次完成」的版本）** | B 的 agent.md 含 `{{ po_probe_a.output.count }}`，三轮渲染实测值 **1 → 2 → 3**（prompt 文件逐轮快照 + B 产出 `upstream_count_at_render` 双证）：既不是首轮值、也不是「末轮值」，而是**渲染时刻该节点最近一次完成的输出**（advance_step 用 `outputs_acc[pending]={output}` 覆盖后再渲染，`run/step.py:817-818`）。prof-opt 设计规避之（gate 读盘）依然正确；若未来引用，语义良定义、无坑 |

## 实际驱动命令（复现用，均 WSL 内）

```bash
# 主形态（run 2，skill 全链路）：WSL 内 headless claude 起会话，意图触发 tars skill
cd /mnt/d/Projects/Orca
export PATH="/mnt/d/Projects/Orca/.venv/bin:$PATH"
export ORCA_BOOTSTRAP_OPEN_WEB=0
claude -p "用 TARS 跑 po-probe" --dangerously-skip-permissions
# skill 内部即：orca po-probe --inputs '{}' → 逐节点派 Task 子代理 → orca next --run-id … --output '<子代理最终消息>'

# 备选形态（run 3，中断续跑）：bash 侧 bootstrap + claude -p 逐节点（镜像宿主派子代理）
orca po-probe --inputs '{}'                       # bootstrap（或由 claude 侧调）
timeout 240 claude -p "你是 Orca workflow 的节点执行子代理。完整节点指令已写入：<prompts/<node>.md 绝对路径>。请先 Read 该文件，按其要求执行。运行任何脚本前先 source <runs/<rid>/orca_env.sh>。你的最终消息 = 本节点产出。" --dangerously-skip-permissions
orca next --run-id <rid> --output '<claude 最终消息原样>'
```

## 附加引擎事实（P0 之外的实证发现，prof-opt 实现必须遵守）

1. **in-session 路由 when 表达式不能引用 `inputs.*`**（代码级核实，未活体测——会 fail loud 崩 run）：`orchestrator.py:577-579` `_next_node_for_resume` 构造 `RunContext(inputs={})`，`router.resolve` 用 StrictUndefined → 引 `inputs.x` 直接 `RouteError`。prof-opt 路由只引节点 output（`po_gate.decision == 'loop'`）——**现状设计恰好合规，实现时不得引入 inputs 引用的 when**。阈值类比较应在 agent 侧脚本算成 output 布尔字段（探针 B 的 `enough` 即此形态）。
2. **中断后节点重执行是 at-least-once**：run 3 中一次「已执行但未上报」的 B（counter 1→2、chart 已推）之后被重执行（2→3），引擎只见过 3。**节点副作用必须幂等/可判活**（reuse_check、状态文件、DONE marker）——prof-opt 继承 ns3 的幂等骨架是硬前提不是优化项。
3. **幂等重发（`orca next` 无 output）不重渲染 prompt**：resume 时 prompt 文件保持「最近一次渲染」的版本（stale 上游值照用）。跨会话续跑要意识到子代理读到的是中断前的渲染版。
4. **recoverable 失败链路真实工作**：B 脚本 exit 2 → 子代理哨兵自报（`agent_blocked`，`tried[]` 诊断完整）→ 引擎重 arm 同节点（run 1）；A 产出带散文前言 → `output_schema_mismatch` recoverable 重 arm → 干净单行 JSON 通过（run 3）。连续失败升级与哨兵检测与 SPEC 一致。宿主侧（skill）在 run 1 选择 `orca stop` 后重新 bootstrap——协议内行为。
5. **`_parse_output` 对「散文 + JSON 混合」不容忍**（run 3 实测：prose 前言 + JSON → `output_schema_mismatch`）。prof-opt 全节点「emit 单行 JSON、stdout 纯 JSON」硬契约必须逐字执行，围栏/前言都会触发 recoverable 重试（烧轮次）。
6. **本环境 host_session 探测为 None（环境事实，非引擎 bug）**：Windows claude.exe 经 interop 驱动 WSL bash 时 `CLAUDE_CODE_SESSION_ID` 不跨 interop（claude Bash 内 venv python `os.environ.get(...)` 实测 `None`）→ `workflow_started.host_session=null` → sidechain 守护 skip（B2 子代理过程不进 web）+ Stop-hook nudge 归属匹配退化。对五项无影响（chart 走 socket）。E2E 若需子代理过程进 web：装原生 Linux claude 于 WSL 或 `WSLENV` 桥接该变量。
7. **prompt 文件同节点重入即覆盖**（`step.py:375-395` 注释明示「loop 时同节点覆盖，逐次历史在 tape」）——跨轮渲染取证要外部快照（本次用 `/tmp` 轮询器，已存 `D:\Projects\po-probe-scratch\snapshots\`）。

## 无 LLM 引擎仿真（预检，同 advance_step 决策层）

`/tmp/po-sim/sim.py`（手工喂 output）先于真跑验证了回边路由序列 / 终态输出 / B 末轮渲染值，与真跑结果逐项一致——引擎决策层无 LLM 依赖问题。

## Run 清单与证据路径

| run | 形态 | 结果 | tape（`/mnt/d/Projects/Orca/runs/`） |
|---|---|---|---|
| po-probe-20260820-201423-126f13 | claude -p + skill（全程） | A 过 → B 失败（探针旧 body 设计缺陷：A 零写入 + B 要求文件存在）→ recoverable 重 arm → 宿主 stop；**失败链路证据** | `po-probe-20260820-201423-126f13.jsonl` |
| po-probe-20260820-201931-b7e0d1 | claude -p + skill（全程，B body 修复后） | **完整回边循环 ×3 轮 → completed**（①②④⑤主证据） | `po-probe-20260820-201931-b7e0d1.jsonl` |
| po-probe-20260820-203036-2749c5 | bash bootstrap + claude 逐节点 + 中断续跑 | kill 宿主 + kill daemon → 续跑 → completed（③主证据 + 发现 2/5） | `po-probe-20260820-203036-2749c5.jsonl` |
| po-probe-20260820-204333-baa0c3 / 204448-6a2fc3 | host_session 实验 / chart 离线实验 | 已 stop（发现 6 / ④-b） | 同目录 |

B 三轮渲染 prompt 快照（⑤逐轮证据）：`D:\Projects\po-probe-scratch\snapshots\po-probe-20260820-201931-b7e0d1__po_probe_b__{202001,202229,202438}.md`（内含 `upstream_count = \`1|2|3\``）。

## 遗留与边界

- 探针 yaml/agent **未 commit**（任务要求）；`tars validate workflows/po-probe.yaml` 通过（agent.md 修订后复验通过）。
- run 1 失败根因是探针 B 初版 body 自身缺陷（要求 counter.json 存在而 A 只读不写），已由 agent.md 修订（B 以渲染注入的 upstream 值兜底初始化）修复，与引擎无关。
- 长任务 detach+有界轮询（§8.1 清单第 3 项）未在本探针覆盖（极简探针无长任务）；该形态已在 ns3/psu 系 workflow 长期实证，prof-opt 沿用其骨架，风险敞口由后续单测 + E2E 覆盖。
