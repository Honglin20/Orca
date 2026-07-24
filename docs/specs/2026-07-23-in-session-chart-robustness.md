# In-Session Chart 鲁棒出图 + 失败告知 agent（SPEC）

> 目标：让 in-session 路径下 workflow 的可视化（`viz_struct.py` / `render_chart`）**鲁棒出图**，且**任何出图失败都告知节点 agent**（写进该节点 output + report），杜绝「workflow done 假成功、前端零图、agent/用户无感」。
> 2026-07-23 草案。范围：**仅 in-session 路径**。落地 workflow：`agent-struct-exploration`（+ 抽 shared helper 供共性复用）。
> 关联：`docs/specs/phase-13-render-chart.md`（render_chart 契约）、`orca/iface/in_session/chart_daemon.py`（in-session chart 守护）、`docs/specs/agent-ask-user-sentinel.md`（失败信号编码进返回值的同形思路）。

---

## 0. 背景与根因（为何现状 in-session 出不了图）

- in-session 下节点子代理由**宿主 session（opencode/CC）派发**，不经 `ClaudeExecutor` → `ORCA_*` env 不会自动注入子代理 bash。bootstrap 已 detach 起 `chart_daemon`（持 socket）+ 写 `runs/<run_id>/orca_env.sh`（`cli.py:1174` / `cli.py:459-500`），但 **env 文件要子代理自己 source 才进 shell**。
- `render_chart` 推图硬依赖 4 个 env：`ORCA_RUN_ID / ORCA_NODE / ORCA_SESSION_ID / ORCA_CHART_SOCK`（`orca/chart/_render.py:33`），缺任一即 `raise RuntimeError`（`_render.py:96-101`，fail loud）。
- **opencode 的 bash 工具不跨调用保 env**（`workflows/agents/bit-curve-searcher/agent.md:61` 实证）：子代理若把 `source orca_env.sh` 与 `python3 viz_struct.py` 拆成两次 bash 调用，第二次调用 env 丢失 → `render_chart` raise。
- `agent-struct-exploration` 的三处出图调用（`struct-curator/agent.md:125-138` Step4、`agent-struct-exploration.yaml:390-400` finalize step5、`yaml:401-426` finalize step6）**均无 `source` 指令、无 env 兜底**，且失败被三层静默吞：`render_chart` raise → `viz_struct.py:335 except` 吞成 stderr WARN → bash 块 `|| true` → workflow 照常 done。
- 对照：`bit-curve` 做了双保险（`agent.md:50` source + `workflows/agents/bit-curve-searcher/scripts/run_bit_curve.py:108` `--env_file` 脚本自加载），但仍 **model-mediated**（依赖子代理记得传 `--env_file`，且 `source runs/${ORCA_RUN_ID}/...` 里 `${ORCA_RUN_ID}` 本身鸡生蛋）。

**结论**：现状 in-session 跑 agent-struct，图大概率不出且完全静默。本 SPEC 用「确定性自加载 + 失败可见」根治。

---

## 1. 范围

- **仅 in-session 路径**。web/tars-run 路径由 `ClaudeExecutor` 注 env + 单进程持 ingestor，本就正常，**不在范围、零改动**。
- 落地 workflow：`agent-struct-exploration`（curator 每轮 3 图 + finalize 终态 3 图 + 1 对比 bar）。
- 共性：自加载逻辑抽成 shared helper，`bit-curve` 等其他 viz 脚本迁移留作**后续**（§7），本 SPEC 不强制。

---

## 2. 设计原则

1. **确定性优先（deterministic over model-mediated）**：env 接驳不靠 LLM「记得 source / 传 `--env_file` / 不拆调用」，改从**已有确定输入**（`--ledger`，必传且已校验）派生。把易错步骤从 LLM 手里拿走。
2. **不破 render_chart 铁律 #2**（`_render.py:6`）：env 只能从 env 读、身份信息禁止经参数/文件被 agent 干扰。**自加载发生在 workflow 脚本层**（调 `render_chart` 之前，把 env 写进 `os.environ`），`render_chart` core **零改动**，照常从 env 读。
3. **sidecar 不阻断，但失败必可见**：viz 仍是 sidecar——失败**不阻断** workflow（结构搜索成果在 `ledger.jsonl` / `final_model`，不在图，此语义不变）；但失败状态**必须回流到节点 agent 的 output + report**（告知 agent + 用户），绝不静默。

---

## 3. 契约

### 3.1 shared helper：`orca/chart/_env.py`（新文件，light-touch）

```python
def load_run_env_from_artifacts(anchor_path: str | Path) -> dict[str, str]:
    """若 os.environ 已含 ORCA_CHART_SOCK → 幂等 no-op（尊重已注 env，真 orca-run 零影响）。
    否则从 anchor_path（任一 run 内产物路径：ledger / champions / snapshot 等）向上找首个
    祖先目录含 `orca_env.sh` **且该文件内容匹配 `^export ORCA_CHART_SOCK=` 行**的 run_dir
    （单一标志 + 内容校验，避免同名文件误匹配）；找到 → 解析其 `export K=V` 行，**仅补
    render_chart 需的 4 键**（ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK）写进 os.environ，返已补
    键集；找不到（非 orca-run / headless / 自定义 output_dir）→ 返 {} 不补（视为无 run env）。"""
```

- **依赖**：仅 stdlib（`os`/`pathlib`/`shlex`）。与 `_render.py` 同属 light-touch 客户端，零 Orca runtime 依赖，`import` 失败时调用方降级（见 3.2）。
- **锚点选择**：`anchor_path` 取调用方**已有**的产物路径（viz_struct 用 `--ledger`；finalize step6 内联用 `champions_path`）。不引入新 CLI 参数。
- **向上搜而非固定层级**：不假设 `ledger` 必在 `<run_id>/artifacts/` 下（防自定义 `output_dir` / 回退 `llm_artifacts`）；从 anchor 向上走，**首个祖先目录含 `orca_env.sh` 且该文件内容匹配 `^export ORCA_CHART_SOCK=` 行**即 run_dir（单一标志 + 内容校验，避免用户项目根碰巧有同名 `orca_env.sh` 误匹配；不依赖 `chart_daemon.log` —— 它是 daemon 产物，非 run_dir 必备，headless 自定义 output_dir 也无）。找不到 → 返 {}。
- **no-op 短路假设**：幂等短路只看 `ORCA_CHART_SOCK` 单键，假设 4 键同源同注（`ClaudeExecutor` / opencode executor spawn 时一起注入）。half-injection（SOCK 注了但其他 3 键缺）视为异常配置，本 SPEC 不解（KISS：4 键几乎不可能半注）。
- **只补 4 键**：`ORCA_ARTIFACTS_DIR` / `ORCA_KB_DIR` 等不在此函数职责（setup 节点 prompt 已处理其缺失回退）；只补 `render_chart` 强依赖的 4 个身份键。

### 3.2 `viz_struct.py`：自加载 + reason 结构化 + `_main` 兜底

- `render_all` 开头（`viz_struct.py:310` 之后、读 ledger 之前）：若 `_orca_render_chart is not None`，调 `load_run_env_from_artifacts(Path(ledger_path))`（`import` 失败 → 降级，跳过自加载，走原 `import_failed` 路径）。
- **stdout JSON 升级**（`viz_struct.py:318-339`）：
  - 顶层加 `"viz_env_status"`：`"ok"`（env 已注）/ `"env_loaded_from_file"`（自加载成功）/ `"env_missing"`（自加载后仍缺 `ORCA_CHART_SOCK`）/ `"import_failed"`（`orca.chart` 不可用）/ `"generic"`（`_main` 兜底路径：进程级异常被 catch 后构造的 fallback）。
  - `charts[name]` 从 `{"pushed": bool}` → `{"pushed": bool, "reason": str}`：
    - `pushed=true` → `reason=""`
    - `pushed=false` → `reason ∈ {env_missing, socket_unreachable, ack_failed, data_insufficient, import_failed, generic:<Type>:<msg>}`
- **reason 分类**（在 `_push_*` 调 `render_chart` 处细化，非统一 catch）：
  - 自加载后 `os.environ` 仍缺 `ORCA_CHART_SOCK` → **不调** `render_chart`，直接 `reason="env_missing"`。
  - env 齐但 `render_chart` raise：`FileNotFoundError`/`ConnectionRefusedError`（`_render.py:173-182` 转写的）→ `socket_unreachable`；ack 类（`_render.py:183-211`）→ `ack_failed`；其余 → `generic:<Type>:<msg>`。
  - 数据不足（`ledger < _MIN_ROWS` / 无有效点，`viz_struct.py:126/162/222/255`）→ `data_insufficient`（设计内，非错误）。
- **`_main` 异常兜底（deterministic stdout 保证，B1 命门）**：现状 `_main`（`viz_struct.py:344-378`）异常路径仅 stderr 写错误 + exit 2，**stdout 无 JSON** → agent 拿不到 viz_status → 依赖 LLM「合成」违反 §2.1。改造为：`except Exception` 分支先构造 fallback result（`viz_env_status="generic"` + `charts` 全 `{pushed:false, reason:"generic:<Type>:<msg>"}`），**先 `print(json.dumps(result)); sys.stdout.flush()` 再 `return 2`**。退出码保留 2（进程级失败信号仍在），但 stdout 必有合法 JSON。agent 改为 **dumb copy stdout JSON → output.viz_status**，零合成、零解读（与 §3.3 必填契约配套：必填但失败值合法，缺字段=节点 fail）。

### 3.3 失败告知 agent：节点 output_schema + report

- **`struct-curator` output_schema**（`yaml:322-332`）新增字段 `viz_status`（**必填**，schema 严化）：

  ```yaml
  viz_status:
    type: object
    required: [env_status, charts]   # 必填：agent 漏写 → next output_schema 校验 fail loud
    additionalProperties: false
    description: "本轮可视化产出状态。失败值（env_missing/generic 等）合法产出；缺字段=节点 fail（agent 未遵守契约）。viz 为 sidecar：失败值不影响 continue_loop。"
    properties:
      env_status: {type: string, enum: [ok, env_loaded_from_file, env_missing, import_failed, generic]}
      charts:
        type: object
        description: "{图名: {pushed: bool, reason: str}}；全 pushed=true 即成功。"
        additionalProperties:            # dynamic key（图名）的 value schema（D2：内层 additionalProperties:false 严化）
          type: object
          required: [pushed, reason]
          additionalProperties: false
          properties:
            pushed: {type: boolean}
            reason: {type: string}
  ```
  （**关键语义**：「必填」指 agent 必须写此字段；「失败值合法」指字段值可以是 `env_missing`/`generic` 等。即「状态必填，但状态值可以是失败」——既守住 sidecar 不阻断（失败值仍合法产出），又杜绝静默漏写。）

- **`finalize` output_schema**（`yaml:348-356`）同样新增 `viz_status`（终态 3 图 + 对比 bar 的状态合并；schema 同上，必填 + 严化）。

- **`final_report.md`**（finalize step4，`yaml:388-389`）顶部新增 `## 可视化产出` 段：
  - 全成功 → 列出推送的图（label/title）。
  - 任一失败 → `⚠️ 可视化未生成：<失败图 + reason 汇总>`，并附修复指引（「检查 `runs/<run_id>/chart_daemon.log`；in-session 下图依赖 `orca_env.sh`，本 SPEC 后由脚本自加载」）。

- **告知链路（deterministic dumb copy）**：`viz_struct` stdout（`viz_env_status` + `charts.reason`，含 §3.2 `_main` 兜底路径的 `generic` JSON）→ 节点 agent **dumb copy** stdout JSON → 写进 `output.viz_status`（零合成、零解读）→ 经 `orca next` 落 tape → 下游/用户可见。agent 因此「知道」图出没出及原因，且不依赖 LLM 合成（§2.1 deterministic-over-model-mediated 不破）。

### 3.4 调用块去盲吞（`|| true` 收窄）+ step6 抽脚本

- `struct-curator/agent.md:128-136` Step4、`yaml:391-399` finalize step5、`yaml:406-424` finalize step6（行号以当前 `agent-struct-exploration.yaml` 为准）：
  - 保留「viz 失败不阻断节点产出」语义（continue_loop / finalize 不因 viz 失败而 fail）。
  - 但**去掉无条件 `|| true` 盲吞**，改为：捕获 viz_struct 的 stdout JSON → **dumb copy** 写进 `output.viz_status`（零合成、零解读，配合 §3.2 `_main` 兜底保证 stdout 永远有合法 JSON）。仅当 viz_struct 进程**非零退出（exit 2）**时 `|| true` 兜底不阻断（进程级失败仍不阻断；stdout 已由 `_main` 兜底含 `generic` JSON → agent 仍能 dumb copy）。
- **step6 抽脚本（D1 决策 = b，B2 / E2 / E3 blocker）**：现状 finalize step6 是 inline `python3 -c "..."`（`yaml:406-424`），**无 stdout JSON 协议、无 reason 捕获**——最 headline 的「Baseline vs Champion vs Final」对比 bar 反而最不可观测；且 agent 须拷贝自加载行 + 替换 `<step3 final_latency_ms>` / `<step2 final_accuracy>` 占位 + 处理 Jinja-in-Python-in-bash 三层引号（三条易错路径，SPEC 自加载只修第一条）。改为：**抽进 `viz_struct.py --mode compare`**（default = 三图；`--mode compare` = 仅终态对比 bar），两模式共享 stdout JSON schema（`charts` 里加 `compare_bar` 项），共享 §3.1 自加载 + §3.2 `_main` 兜底 + import try/except。
  - 新 CLI：`python3 viz_struct.py --mode compare --champions <path> --baseline_latency_ms <num> --baseline_accuracy <num> --final_latency_ms <本节点实测> --final_accuracy <本节点实测>`（final 两值由 agent 本节点 step 2/3 算出后作 CLI 参数传入，**不再替换 inline 占位**）。
  - 删除 yaml 原 inline `python3 -c "..."` 块（`yaml:406-424`）。
  - struct-curator/agent.md Step4 同步：去 `|| true` 盲吞，dumb copy stdout JSON 写 output.viz_status。

---

## 4. 各层改动清单

| 层 | 文件 | 改动 | 改动性质 |
|---|---|---|---|
| shared helper | `orca/chart/_env.py`（新） | `load_run_env_from_artifacts`（§3.1：单一标志 + 内容校验 `^export ORCA_CHART_SOCK=`） | 新增 |
| viz 脚本 | `workflows/agents/_struct_scripts/viz_struct.py` | 自加载 + `reason` + `viz_env_status`（含 `generic`）；加 `--mode compare`（终态对比 bar，default=三图）；`_main` 异常兜底（先 print fallback JSON 再 exit 2，§3.2 B1） | 改 |
| workflow yaml | `workflows/agent-struct-exploration.yaml` | curator/finalize output_schema 加 `viz_status`（必填 + 严化）；finalize step5 去盲吞 dumb copy；**step6 删 inline `python3 -c` 块 → 改 `viz_struct.py --mode compare` CLI 调用**（D1=b） | 改 |
| agent.md | `workflows/agents/struct-curator/agent.md` | Step4 调用块去盲吞、dumb copy stdout JSON 写 output.viz_status | 改 |
| 测试 | `tests/`（新模块） | 自加载单测 + reason 分类单测 + `_main` 兜底单测 + `--mode compare` 单测 | 新增 |
| **render_chart core** | `orca/chart/_render.py` | **零改动**（铁律 #2 不破） | — |
| chart_daemon / cli | `orca/iface/in_session/{chart_daemon,cli}.py` | **零改动**（daemon/respawn/orca_env.sh 已就绪；§5 invariant「host 串行契约」依赖既有串行模型，本 SPEC 不引入新机制） | — |

---

## 5. 失败路径与边界

| 场景 | 自加载 | render_chart | `viz_status` | 阻断 workflow？ |
|---|---|---|---|---|
| 真 orca-run，env 已注 | no-op | 成功 | `env_status=ok`，全 pushed | 否 |
| in-session，env 缺，`orca_env.sh` 在 | 补 4 键成功 | 成功 | `env_loaded_from_file`，全 pushed | 否 |
| in-session，拆调用，`orca_env.sh` 在 | 脚本内补（不依赖 shell source） | 成功 | `env_loaded_from_file` | 否 |
| `orca_env.sh` 找不到（headless/非 orca-run/自定义 output_dir） | 返 {} | 不调，`reason=env_missing` | `env_missing`，全 not pushed | 否（report 标注 ⚠️） |
| daemon 挂（socket refused / stale FileNotFoundError） | env 齐但连不上 | raise → `socket_unreachable`（`_render.py:173-182` 两类异常均转此） | 标注；next 推进时 respawn daemon（`cli.py:264-312`），下轮自愈 | 否 |
| 数据不足（ledger<2 行） | — | 脚本内跳过 `data_insufficient` | 如实 | 否（设计内） |
| `orca.chart` import 失败 | 降级不补 | 全跳过 `import_failed` | `import_failed` | 否 |
| viz_struct 进程 exit 2（I/O 硬错） | — | — | `generic`（`_main` 兜底 stdout 已含 JSON，§3.2），`|| true` 兜底不阻断 | 否 |

**关键 invariant（E4）—— host 串行契约**：subagent 完成（output 落 tape）后 host 才调 `orca next`，env 文件无并发读。`cli.py:1488` next 路径按下一节点身份重写 `orca_env.sh`（含 ORCA_NODE），但写入发生在 subagent 已退出之后，子代理不会读到错节点身份的 env。本 SPEC **不引入新机制**，依赖既有串行模型（与 chart_daemon respawn 同款不变量，见 `cli._ensure_chart_daemon` 注释；契约由 `tests/iface/in_session/test_in_session_chart.py` ORCA_NODE-per-node 断言守）。

---

## 6. 验收标准（可验）

- **AC1a（核心单测，ledger anchor）**：造 tmp run_dir + `orca_env.sh`（含 `export ORCA_CHART_SOCK=...` 等 4 键）+ `ledger.jsonl`，**清空 os.environ 的 ORCA_***，调 `load_run_env_from_artifacts(tmp_ledger)` → 断言 4 键被补进 `os.environ`；mock `render_chart` 断言其能读到 env。
- **AC1b（核心单测，champions anchor）**：同 AC1a 但 anchor 用 `champions.jsonl`（step6 `--mode compare` 用的路径），断言行为一致（防 step6 锚点失效）。
- **AC2（幂等）**：os.environ 已含 `ORCA_CHART_SOCK` 时，`load_run_env_from_artifacts` no-op，不改 env。
- **AC3（找不到 fallback）**：anchor 指向无 `orca_env.sh` 的 tmp 目录（或 `orca_env.sh` 存在但内容**不含** `^export ORCA_CHART_SOCK=` 行）→ 返 {}，`os.environ` 不变；viz_struct 标 `reason=env_missing`，stdout `viz_env_status=env_missing`。
- **AC4（reason 分类）**：mock render_chart 抛 socket 类异常（FileNotFoundError / ConnectionRefusedError）→ `reason=socket_unreachable`；抛 ack 类 → `ack_failed`；数据不足 → `data_insufficient`。
- **AC5a（脚本 stdout 字段断言，单测）**：viz_struct 各失败路径（env_missing / socket_unreachable / data_insufficient / `_main` 兜底 generic）下，stdout JSON 含 `viz_env_status` + `charts[name].reason` 字段且分类正确；`--mode compare` 路径下 `charts.compare_bar` 同款断言。
- **AC5b（agent 转写后 output.viz_status，集成测试）**：任一图 `pushed=false` 时，curator/finalize 的 `output.viz_status` 非空且含失败 reason（dumb copy 链路验）；`final_report.md` 含 `## 可视化产出` 段（成功列图 / 失败 ⚠️ + 原因）。纳入 `tests/e2e_redesign/` stage3 headless harness（schema_faker 合成 + fake daemon）。
- **AC6（sidecar 不阻断）**：viz 全失败时，`continue_loop` 仍由 champion/预算正常驱动；finalize 仍产出 `final_model`/`final_report`，不因 viz 失败 fail。
- **AC7（不破铁律 / 零回归，具体测试模块清单）**：`orca/chart/_render.py` 零改动；web/tars-run 路径行为不变（env 已注 → no-op）。**回归测试集**：`tests/chart/`、`tests/events/test_chart_ingestor.py`、`tests/iface/in_session/test_chart_daemon.py`、`tests/iface/in_session/test_in_session_chart.py`、`tests/e2e_redesign/` stage3 契约闸（既有 chart 路径全绿）。
- **AC8（端到端集成，纳入 stage3）**：in-session fake daemon + mock socket 跑 agent-struct 一轮（schema_faker 合成 + headless DAG walk），断言 `runs/<run_id>/` tape 出现 `custom(chart)` 事件（label=`struct-explore`）≥1；或 viz 失败时 report 顶部明确标注原因。**降级为集成测试**（原「手动」），纳入 stage3 harness，**阻塞合并**。
- **AC9（ORCA_NODE 漂移 invariant，E4）**：单测断言「`orca next` 推进后，env 文件 `ORCA_NODE` 行 == 返回的下一节点名」（验 host 串行契约，防 next 重写 env 错位）。

---

## 7. 范围外 / 后续

- `bit-curve`（`workflows/agents/bit-curve-searcher/scripts/run_bit_curve.py`）/ `viz_kd.py` 等迁移到 `load_run_env_from_artifacts`，退役各自 `--env_file`（DRY）。
- in-session viz 回归纳入 `tests/e2e_redesign/` stage3 契约闸（需 fake chart_daemon / mock socket）。
- `viz_status` 若下游需程序解析，可从 object 再细化；当前 object schema 已够。
- **已知债（E6）**：`viz_kd.py`（`workflows/agents/_struct_scripts/viz_kd.py:527` 同款 stdout 模式）与 `bit-curve` 的 stdout schema 与本 SPEC 升级后的 `viz_struct`（`viz_env_status` + `charts.reason` + `_main` 兜底）**漂移**；KD workflow 的 viz 失败告知链路暂未接入（viz_kd 未走 `viz_status` output_schema 必填契约）。留后续 DRY 统一（迁移时一并补齐 schema）。本 SPEC 仅解 `agent-struct-exploration`，不阻塞。
