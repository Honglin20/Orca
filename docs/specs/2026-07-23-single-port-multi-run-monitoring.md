# SPEC：单端口 + 多 Run 监控（注册表版 v3，含删除/同步/多用户预留）

> **状态**：v3（2026-07-23）。在 v2（注册表版）基础上**增量补入新需求**：run 删除 + 清单清除（D10）、删除后前端及时同步（D11）、多用户方案/接口预留（D12）、`orca open` 接口保留且永不新开端口（D13）、零回归保证（§11b）。v2 的 carry-over 闭环（I-3/I-4/I-6/I-7/I-9/I-18）全部保留。
> **supersede**：[`docs/plans/2026-07-23-orca-home-directory-layout.md`](../plans/2026-07-23-orca-home-directory-layout.md) 的 Phase B（集中存储）整体作废——单端口只需身份解耦，**不需要集中化**。详见 §3 D3。
> **前置依赖**：多 run 监控层（Phase C）依赖 Phase A（身份解耦）+ Phase B'（注册表）。
> **范围**：① 身份解耦→单端口 ② 轻量项目注册表 ③ web 多 run 监控（discovery + 懒挂载 + 列表层）④ 多用户威胁定性。
> **不是**：逐行实现 SPEC；迁移工具实现（本路线**无历史迁移**，§10）。

---

## 0. 一句话目标

**一个用户、一个端口、一套接口**：web 上看到该用户所有项目、所有 run 的元数据列表，点哪个才加载哪个；**run 历史留在各自项目仓库内聚存放**，`~/.orca/` 只放配置 + 全局资产 + 一个轻量项目注册表。tape 仍是唯一真相源。

---

## 1. 动机 + 路线选择（为什么不再集中化）

1. **端口碎片**：复用判定 `sha1(<项目>/runs)`，项目不同→不复用→新端口。**根因是身份指纹与存储路径耦合**。
2. **关键洞察（推翻 v1）**：身份解耦（指纹 = `sha1(ORCA_HOME)`）**独立解决单端口**，与 tape 存哪无关。v1 为"单端口"而集中化存储是**过度**——集中化唯一额外收益只剩"discovery 扫一棵树"，而一个轻量注册表即可替代，且不付集中的代价。
3. **v1 的 two-tree split 是裂缝**：v1 把 tape 搬 `~/.orca/`、产物留仓库，被迫发明 D3 特例兜底。spec-review 据此揪出 **5 条 split 制造的 issue**（I-1/I-5/I-13/I-21/I-22），本路线**全部消除**。
4. **本路线优势**：run 文件内聚（tape+产物+prompts+assets 同在 `./runs/<rid>/`，**零迁移**、可随项目备份/跨机）、`~/.orca/` 永不膨胀、gc 回到 trivial 单目录清理。
5. **缺口**：前端是刻意的单 run/页（`workflow-store` 单 fold）。看多个 run 需新增**与详情 fold 隔离的元数据列表层**。

---

## 2. 铁律（R1–R6）

| # | 铁律 | 本 SPEC 如何守 |
|---|---|---|
| R1 | **tape 唯一真相源** | 列表元数据 / 详情事件 / status 全部从 tape 派生。`projects.json` 只是**项目路径指针**（电话簿），**不索引 run 数据**——与 plan §9"不做 run 数据库"不冲突（那是禁 run 数据索引，非禁项目路径注册）。`_meta_cache` 是缓存非第二真相源。 |
| R2 | **一套接口** | 多 run 能力全落**既有**接口族：`GET /api/runs`（加 `scope`）、`GET /api/runs/<id>/{meta,events,assets}`、`WS subscribe`、`attach_run`。**无 sidecar/SSE/第二套 API**。 |
| R3 | **单 fold 不破** | `workflow-store`（单 store/单 fold/单 `activeRunId`）**零改**。新增 `runListStore` 只持元数据数组（无 reducer/fold/状态机）——是目录列表。 |
| R4 | **懒加载红线** | 列表只给元数据，**绝不返回事件**。事件只在详情页 mount 按需拉、unmount 清。 |
| R5 | **依赖单向** | discovery/registry 在 `iface/cli`（注册写入）+ `iface/web/run_manager`（discovery 读）；懒挂载复用 `attach_run`；前端**只发 run_id，不感知磁盘路径**。`_identity` 仅 stdlib。 |
| R6 | **fail loud** | 坏 tape 跳过+warn 不崩列表；懒挂载 0 命中→404、多命中→**500+错误体**（reviewer I-6，删 v1"概率0"措辞）；端口被 foreign 占→明确报错。 |

---

## 3. 决策（D1–D9）

### D1（身份解耦）→ `ORCA_HOME`（同 v1）
health 与 client 复用判定从 `sha1(runs_dir)` 改为 `sha1(ORCA_HOME)[:12]`（默认 `~/.orca/`）。同用户所有项目共享指纹→**单端口**。旧 server 无此字段→视为 foreign→spawn（不退化）。文件：`_identity.py` 加 `orca_home_fingerprint()`；`attach.py` health、`commands.py::_runs_dir_fp` 用之。

### D2（项目身份）→ `project_id`
`project_id = sha256(str(resolve(project_root)))[:16]`。project_root 检测优先级：`ORCA_PROJECT_ROOT` env > 向上找含 `workflows/` 或 `.orca/config.json` 的目录 > git root > `Path.cwd()`。**检测结果不再写 project.json**（v1 的 project.json 取消）——项目名/路径只存注册表条目（D4）。

### D3（历史留仓库——**核心，推翻 v1 集中化**）
- **run 历史/资源全部留在项目仓库** `<project_root>/runs/<rid>/`：tape `.jsonl`、`prompts/`、`assets/`、`artifacts/`（重型产物，含 NAS 模型/搜索结果）、`orca_env.sh`、`*_daemon.log`、`mcp_<session>.json`、激活标记 `orca-<rid>.json`、run 元数据 `<rid>.json`。**内聚，与现状一致，零迁移。**
- **`$ORCA_ARTIFACTS_DIR` 仍 = `<runs_dir>/<rid>/artifacts/`（现状签名 `artifacts_dir_for_run(runs_dir, run_id)` 零改）**（reviewer I-1：v1 改签名牵连 5 处调用点的问题在本路线**不存在**）。
- **`~/.orca/` 不收任何 run 文件**。

### D4（项目注册表）→ `~/.orca/projects.json`
`~/.orca/` 顶层新增**轻量注册表**（唯一新增中央文件）：
```json
{"version":1, "projects": {"<project_id>": {"path":"<abs resolved>", "name":"<dirname>", "first_seen":<ts>, "last_seen":<ts>}}}
```
- **写入**：`orca run`/`open`/`bootstrap` 在某项目执行时，`register_project(project_root)` 计算 id + upsert（更新 `last_seen`/`path`）。
- **三用途**（内聚，非三套机制）：① discovery 枚举根（扫各 `entry.path/runs/*.jsonl`）② `attach` 的 allowlist（tape 须在某注册项目的 `runs/` 下）③ 列表的项目名来源。
- **并发**：读写经 `fcntl.flock`（Linux/macOS）/ `msvcrt.locking`（Windows）保护（reviewer I-15）。
- **陈旧**：`entry.path` 不存在 → discovery skip + 标 stale（不自动删，gc 清，§8）。

### D5（discovery = 单端点加 scope，同 v1 + legacy 退化）
`GET /api/runs?scope=all`：读注册表 → 对每个存在项目扫 `runs/*.jsonl`（memoized）→ 合并内存 live run → 返回 `list[RunSummary]`。支持 `project/status/q/limit/offset`。
- **legacy 退化**（reviewer I-8）：扫 `~/.orca/runs/*.json`（旧 BgRunMeta）→ `project_id=null, project_name="<legacy>", source="legacy"`，归"Legacy"分组，不崩。
- RunSummary 字段白名单（reviewer I-12，schema validator 拒其他字段）：`{run_id, workflow_name, project_id, project_name, status, progress, cost, elapsed, started_at, event_count, source}`。

### D6（端口登记上移，同 v1）
单端口后端口登记从每项目 `<runs_dir>/.orca-web.json` 上移到 `~/.orca/.orca-web.json`（`{port, runs_dir_fp}`），同样经 flock 保护（reviewer I-15）。

### D7（懒挂载 server 端触发，统一入口）
新增 `manager.ensure_attached(run_id) -> None`（幂等）：已在 `_runs` → 直接返；否则 `resolve_run_path(run_id) -> Path`（先查 discovery 轮询期构建的内存 `run_id→(project_id,tape_path)` map；miss 则扫注册项目 `runs/<rid>.jsonl`）→ `attach_run`（read-only + follow task）。
- **触发面**（reviewer I-3，扩含 assets）：`{GET /api/runs/<id>/meta, /events, /assets/<path>, WS subscribe}` 任一遇 unknown run_id 先 `ensure_attached`。
- **多命中**（reviewer I-6）：`resolve_run_path` 命中 >1 → **HTTP 500 + 错误体列所有命中路径**（fail loud；run_id 设计为全局唯一，多命中=数据异常如手工 cp，不静默选一份）。
- **attached run `writable=false`**（gate 模态禁提交，既有契约）。
- WS 改动点（reviewer I-7）：`ws_handler._handle_subscribe` 首行 `await manager.ensure_attached(run_id)` 替换现状 `get_handle None→warn+return`。

### D8（run_id 全局唯一——措辞订正）
`run_id = <slug>-<YYYYMMDD-HHMMSS>-<nanoid6>`，**设计为全局唯一**（时间戳+随机后缀）。**删 v1"碰撞概率为0"数学措辞**（reviewer I-6：不严谨）；改为"设计唯一，多命中视为数据异常→fail loud"。

### D9（列表实时性 = 轮询元数据 + 失效信号，同 v1 + 节流）
列表页 mount 后轮询 `GET /api/runs?scope=all`（~4s）；unmount 停。**client 节流**：`Date.now()-lastFetch < 2000` 返缓存（reviewer I-16，防多 tab 风暴）。**不开 per-event 全局广播**（R2）——但允许单条"列表失效"控制帧（D11，非事件洪流）。

### D10（run 删除 + 清单清除）→ `DELETE /api/runs/<id>`
清单每行提供删除（二次确认）。删除 = 从内存注册表移除（停 follow task）+ 删磁盘 `<project>/runs/<rid>.jsonl` + `<project>/runs/<rid>/` 整目录 + 清 `~/.orca/projects.json` 中该 run 的任何派生缓存条目（**不动项目注册**——项目仍存在，只删该 run）。
- **契约**：`DELETE /api/runs/<id>` → `{ok:true, run_id}`；未知 run_id（不在内存、磁盘也无）→ 404（幂等：重复删同 id 第二次 404）。
- **安全**：单 run 删除，**绝不递归删项目目录或别的 run**；越界守卫同 attach（必须在某注册项目 `runs/` 下）。
- **live run 删除**：running run 删除前先 `cancel_run`（emit cancelled 写 tape）再删文件，避免 follow task 写已删文件。
- **批量清除**（清单）：`DELETE /api/runs`（无 body）不启用（危险）；改前端逐行 + 可选"清除本项目已完成 run"客户端聚合（循环调单删）。服务端只暴露单删。

### D11（删除后前端及时同步）→ 单条 WS 失效帧 + 轮询兜底
删除需跨 tab/客户端"及时"反映（< 1s），又不能违反 R2（不广播 run 事件）。方案：
- **WS 控制帧 `run_changed`**（非事件、非洪流）：任一 run 被 delete/cancel/attach 时，server 向**所有连着的 WS** 推一帧 `{type:"run_changed", run_id, action:"deleted"|"changed"}`。客户端列表页收到 → 立即 `refresh()`（或乐观移除）。**这是单条信号帧，不是事件流，不违反 R2**（R2 禁的是广播每个 run 的 agent_message/tool_call 等业务事件）。
- **轮询兜底**：未连 WS / 帧丢失 → 4s 轮询自然收敛（删除的 run 不再出现在 `scope=all`）。
- **不进 tape**：`run_changed` 是控制平面帧（同 `resume_ok`），不落 tape、不进 reducer。
- 乐观 UI：发起 delete 的客户端立即从 `runs[]` 移除该行 + 回滚 on 失败。

### D12（多用户：方案 + 接口预留，当前不启用）
当前单用户；但接口/路径**预留**多用户，未来启用零破坏性改动：
- **路径预留**：`ORCA_HOME` 已可 env 覆盖 → 多用户场景每人/每租户独立 `ORCA_HOME`（如 `~/.orca/` 天然按 OS 用户分；容器/租户用 `ORCA_HOME=/data/orca/<tenant>`）。注册表/端口登记/tape 全在各自 `ORCA_HOME` 下，天然隔离。
- **接口预留**：所有 web 端点（health/attach/events/assets/delete）接受**可选** `Authorization: Bearer <token>` 头（当前**忽略**，no-op；未来 server 启动生成 token 写 `$ORCA_HOME/.orca-token` 仅本用户可读，启用校验）。WS connect 同理接受可选 token query。
- **指纹已按 ORCA_HOME**（D1）：不同 `ORCA_HOME` → 不同指纹 → 不同 server，天然不串。
- **当前不实现**鉴权逻辑（no-op pass-through）；§9 威胁保留登记。预留的契约：header 字段名固定、版本化（`/api/v1/...` 预留前缀可选）。

### D13（`orca open` 接口保留 + 永不新开端口）
- **`orca open <run_id>`**：保留，深链直达 `http://<host>:<port>/runs/<run_id>`（单 run 详情页，现状零改）。
- **`orca open`（无参）**：打开 `http://<host>:<port>/`（多 run 列表页）。
- **永不新开端口**：open 始终复用**用户既有 server**（D6 注册表 `~/.orca/.orca-web.json` 记录用户 server 端口）。仅当用户**无任何活 server**时 bootstrap 一次（默认 7428；被占则空闲端口并登记）。此后所有 open/run 复用该端口。**禁止** open 因"项目不同"而 spawn——D1 身份解耦已消除该路径。
- 兼容：既有 `--host`/`--port`/`--tape` 参数保留语义。

---

## 4. 目录规划（目标布局）

```
~/.orca/                                   ← 用户级（按 OS home 天然隔离用户；永不在膨胀）
├── config.json                            ← 用户级配置
├── .orca-web.json                         ← 端口登记（一用户一 server；flock 保护）
├── projects.json                          ← ★ 轻量项目注册表（路径指针，非 run 数据；flock 保护）
├── workflows/                             ← 全局：workflow yaml + agents/（子 agent 定义 + references）
├── knowledge_base/                        ← 全局 KB
├── pools/                                 ← Phase 15 预留
└── runs/                                  ← legacy BgRunMeta（过渡兼容；source=legacy）

<project_root>/runs/                       ← ★ run 历史/资源全在这（内聚，现状不变）
├── <run_id>.jsonl                         ←   tape（唯一真相源）
├── <run_id>.json                          ←   run 元数据
├── orca-<run_id>.json                     ←   激活标记
└── <run_id>/
    ├── prompts/  assets/  artifacts/      ←   artifacts = 重型产物（NAS 模型等），与 tape 同处
    ├── orca_env.sh  chart_daemon.log  sidechain_daemon.log  mcp_<session>.json
```

**判据**：
- `~/.orca/` 全树**无任何 run 的 tape/artifacts/assets**（只配置 + 全局资产 + 注册表 + legacy 元数据）。
- 单个 run 的全部文件内聚在 `<project>/runs/<rid>[/...]`，tar 项目即带走全部历史。
- **零迁移**：现状 `./runs/` 布局即目标布局，无 Phase D 历史搬家。

---

## 5. 后端契约

### 5.1 `GET /api/health`（D1）
`{app:"orca", version, pid, runs_dir_fp: <sha1(ORCA_HOME)[:12]>}`。

### 5.2 `GET /api/runs?scope=all`（D5）→ `list[RunSummary]`
字段白名单见 D5。**无 events**（schema validator 守门，reviewer I-12）。
- **性能契约**（reviewer I-11/I-18，绝对值）：N=1000 tape、avg 500KB、SSD，**首次 discovery 墙钟 ≤ 3s**；超限触发 §12 per-project lazy enumerate。`_meta_cache`：per-process dict、key=`(tape_path, mtime, size)`、暴露 `cache_stats()->{hits,misses,evictions}`。二次 GET（mtime/size 不变）墙钟 < 50ms（N≤100）且 `cache_stats().hits ≥ N`。
- **discovery 真相源**（reviewer I-9）：`_scan_meta_overview` 是 events 层 fold 的**只读派生投影**，留 iface 层（不抽 events 层，避免大改）；**新增 contract test**：断言 scan 覆盖所有 status-affecting EventType（新增 event 类型必须同步本函数 + 加测试守门）。

### 5.3 懒挂载触发面 + `ensure_attached`（D7，reviewer I-3/I-6/I-7）
- 触发面集合：`{GET /api/runs/<id>/meta, /events, /assets/<path>, WS subscribe}`。
- `ensure_attached(run_id)` 幂等；`resolve_run_path(run_id)`：0 命中→404；>1 命中→500+错误体列路径。
- WS `_handle_subscribe` 首行调 `ensure_attached`。

### 5.4 `POST /api/run`（reviewer I-4，新契约）
共享 server 须知道 run 写哪个项目。body schema：
```json
{"yaml_path":"<abs>", "inputs":{}, "task":null, "max_iter":null, "resume":false,
 "project_id":"<sha256[:16]>", "project_path":"<abs resolved project_root>"}
```
- `project_id`/`project_path` **必填**（client `orca run` 经 `_project.py` 算好传入）；缺→400 fail loud。
- server 写 tape 到 `<project_path>/runs/<rid>.jsonl`；同时 `register_project`（idempotent upsert）。
- `start_run(yaml_path, inputs, task, max_iter, *, resume, project_id, project_path)` 签名增两必填参数。
- 安全（reviewer I-10）：`project_path` 须 resolve 为绝对；单用户定位下 server 写用户自己的目录；attach allowlist 由注册表提供（tape 须在某注册项目 `runs/` 下）。hardlink 攻击单用户不成立；§9 多用户依赖 OS `protected_hardlinks=1`（文档化）。

### 5.5 资源/ tape 解析（无单一 runs_dir）
- `resolve_asset_path(run_id, rel)`：经 `ensure_attached` 拿 handle → `assets_root = handle.tape.path.parent / run_id / "assets"`（tape 在 `<project>/runs/<rid>.jsonl` → assets 在 `<project>/runs/<rid>/assets/`）。**不再依赖单一 `manager.runs_dir`**（消除 v1 I-2 跨项目缓存问题）。
- `resolve_tape_path(tape_path)`：边界检查改为"tape 须在某**注册项目**的 `runs/` 子树下"（注册表即 allowlist）。既有 symlink/越界/TOCTOU 守卫不变。
- `assets_dir_for_run` 与 `artifacts_dir_for_run` 在 `orca/chart/_paths.py` 对称定义（reviewer I-20；本路线 artifacts 签名零改，assets 维持 `<runs_dir>/<rid>/assets`）。

### 5.6 注册表 API（D4，`iface/cli/_project.py` 新文件）
- `detect_project_root() -> Path`、`project_id(root) -> str`、`register_project(root)`、`list_registered() -> dict`、`is_registered_runs_dir(path) -> bool`。
- 读写 `~/.orca/projects.json` 经 flock。

### 5.7 删除 + 失效帧契约（D10/D11）
- `DELETE /api/runs/<id>`（reviewer I-4 同族，project 信息从 handle/懒挂载解析）：
  - 在内存 → `cancel_run`（若非终态）→ 停 follow task + 移除 `_runs[id]` → 删 tape + run 目录 → 推 `run_changed{action:deleted}` → `{ok:true, run_id}`。
  - 不在内存但磁盘有（dormant）→ `ensure_attached` 解析路径 → 直接删文件 → 推帧 → `{ok:true}`。
  - 都无 → 404（幂等）。
  - 越界/安全：路径须在某注册项目 `runs/` 下（同 §5.5）；**禁止**删项目根或兄弟 run。
- **WS 失效帧**：server 维护"广播回调列表"（所有连着的 WS）；delete/cancel/attach 时向每条 WS 发 `{type:"run_changed", run_id, action}`。**控制平面帧，不落 tape、不进 reducer、client processEvent 不处理**（列表页 onmessage 专门分支处理 → refresh）。

### 5.8 多用户 header 预留（D12）
- 所有 REST 端点签名接受可选 `Authorization` 头（FastAPI `Depends` 注入 no-op stub：当前直接 pass，不做校验）。
- WS accept 时读可选 `?token=`（当前忽略）。
- stub 集中在 `iface/web/_auth.py`（新，~20 行 no-op），未来替换为真实校验**只改这一处**，业务路由零改（OCP）。

---

## 6. 前端契约

### 6.1 路由（同 v1）
`/` → 列表页（dashboard）；`/runs/:runId` → 详情页（零改）。`orca open <rid>` 深链直达；`orca open`（无参）→ `/`。详情页 TopBar 加"← 返回列表"。

### 6.2 `runListStore`（新增，与 `workflow-store` 物理隔离）
```ts
interface RunListState {
  runs: RunSummary[]; loading: boolean; filter:{...}; lastFetch: number;
  refresh(): Promise<void>;
  deleteRun(runId: string): Promise<void>;   // 乐观移除 + DELETE + 失败回滚
  onRunChanged(frame: {run_id, action}): void; // 收 WS run_changed → action=deleted 乐观移除 / else refresh
}
```
- mount → `refresh()` + ~4s 轮询（client 节流 2s，reviewer I-16）；**unmount → `runs=[]` 清空 + 停轮询**（reviewer I-14）。
- **WS `run_changed` 处理**：列表页挂一条专用 WS（或复用），onmessage 见 `type==="run_changed"` → `onRunChanged`（**不进 processEvent/reducer**，控制帧）。这是删除及时同步的客户端入口。
- **绝不 import/写入 `workflow-store`**（R3）。

### 6.3 列表页布局（专业设计见 §6.5）
顶栏（刷新/搜索/status chips/项目分组）+ 按项目 collapsible 分组（含 "Legacy" 桶）+ 每行元数据卡片（**行内删除按钮 + 二次确认弹窗**）+ 行点击 navigate + 底部分页。配色/布局/组件细节由独立页面设计 agent 产出（§6.5），与既有 web 主题统一（`--surface-*`/`--accent`/`orca-*` utility class）。

### 6.5 页面设计（独立 agent 产出，配色与现状统一）
- 输出物：列表页高保真设计规范（布局栅格、status 徽章配色、删除确认交互、空态、加载骨架、项目分组折叠样式），**复用既有 CSS 变量与 utility class**，产 TSX/HTML mockup + 设计说明。
- 约束：与 `RunDetailPage`（三栏 + TopBar + `react-resizable-panels` + lucide-react）视觉一致；不引入新设计系统。coder 据此实现 `RunListPage.tsx`。

### 6.4 详情页（零改 + 懒挂载透明）
`useRunEvents` + `useWebSocket` 不变；底层 `/meta`、`/events`、`/assets`、WS 已透明懒挂载。

---

## 7. 验收标准（AC，可验证，已吃进 reviewer）

| AC | 验收点 | reviewer |
|----|--------|----------|
| AC1 | 同用户两不同项目 `orca open`，第二次不 spawn（共用端口） | — |
| AC2 | run 全部文件落 `<project>/runs/<rid>[/...]`（tape+artifacts+assets 同处）；`~/.orca/` 无任何 run 的 tape/artifacts/assets | I-1/I-21 |
| AC3 | `GET /api/runs?scope=all` 返回跨项目全部 run；响应经 **schema 白名单 validator**（拒 events/其他字段）；legacy run 可见且 `source=legacy` | I-8/I-12 |
| AC4 | 同组 tape 二次 GET 墙钟 <50ms（N≤100）且 `cache_stats().hits ≥ N` | I-11 |
| AC5 | dormant run（未 attach）直接 `GET /runs/<id>/{meta,events,assets/<p>}` 均 200 | I-3 |
| AC6 | running run（他进程 in-session）web 打开能 WS live tail；subscribe 前 `_runs` 无该 id、后 attached + follow task alive + 推送 ≥1 增量 | I-7 |
| AC7 | 坏 tape（截断/首行非 workflow_started）不崩列表，该条降级 warn | — |
| AC8 | `resolve_run_path`：0 命中→404；构造多命中→**500+错误体列路径** | I-6 |
| AC9 | 列表→详情→返回→详情→返回 双循环：每次列表 mount 首屏来自 `refresh()` 非残留；unmount 后 `runs=[]` | I-14 |
| AC10 | 跨项目 `/api/runs/<id>/assets/<p>` 各自可取不串 | I-2(简化) |
| AC11 | grep `runListStore` 不 import `workflow-store`；**reducer fuzz**：向 workflow-store 注 100 条 RunSummary 形态事件→store 状态零变化 | I-17 |
| AC12 | N=1000 tape（avg 500KB, SSD）首次 discovery 墙钟 ≤3s | I-18 |
| AC13 | 两并发 `orca open` 只起一个 server（端口登记 flock）；两并发 `orca run` 写 projects.json 无 corruption | I-15 |
| AC14 | `_scan_meta_overview` contract test 覆盖所有 status-affecting EventType | I-9 |
| AC15 | project_root 检测：env > workflows/.orca > git root > cwd 优先级链 | — |
| AC16 | `DELETE /api/runs/<id>`：删 tape+run 目录；重复删→404 幂等；**不删项目根/兄弟 run**（越界守门） | D10 |
| AC17 | 删除后 ≤1s 内其他 tab 列表反映（WS `run_changed`）；断 WS 时 ≤4s 轮询收敛 | D11 |
| AC18 | `orca open <rid>` 深链直达详情页且**不 spawn 新端口**（复用既有 server）；`orca open`（无参）开列表页 | D13 |
| AC19 | 多用户预留：端点接受可选 `Authorization` 头（当前 no-op）；grep 守门 stub 集中 `_auth.py` | D12 |
| AC20 | **零回归**：既有 `orca run`/drive_loop/executor/in-session/TARS skill/详情页行为全过既有测试套件，无改动无失败（已知 `test_v3_step1` pre-existing 失败除外） | §11b |

---

## 8. 失败路径

| 场景 | 行为 |
|------|------|
| `ORCA_HOME` 不可写/不存在 | bootstrap `mkdir -p`；权限不足→fail loud |
| project_root 检测歧义 | D2 优先级链确定；检测不稳定→以 `ORCA_PROJECT_ROOT` 显式钉死 |
| 项目重命名/mv → path 变 | 注册表 entry.path 陈旧→discovery skip+标 stale；`orca gc` 清死项 + 孤儿 runs |
| 项目删除 → `runs/` 没了 | discovery skip；该 run 不进列表（数据随项目走，符合"内聚"语义） |
| 注册表 `projects.json` 损坏 | JSON parse 失败→fail loud + 提示修复；不静默清空（reviewer I-19） |
| 懒挂载反查磁盘慢（首查 N 大） | `run_id→project` 内存 map（discovery 轮询期填充）+ memoize；首查可接受 |
| 懒挂载多命中 | 500+错误体（D8/I-6），不静默选一份 |
| 端口 7428 被 foreign orca 占 | 指纹不匹配→视为 foreign→起空闲端口（不抢不串） |
| discovery 坏 tape | 跳过该条+warn，不崩列表 |
| server 无单一 runs_dir | 资源/tape 经 handle.tape.path + 注册表 allowlist 解析（§5.5） |
| gc（Phase D） | 清一处 `<project>/runs/<rid>/`（整目录，trivial）；清注册表死项 + 孤儿（**无 v1 两处清理问题**） |

---

## 9. 多用户：当前单用户，接口/路径已预留（D12）

**当前定位：单用户/受信任机器。** 但**接口与路径已为多用户预留**，未来启用零破坏：
- **路径隔离**：一切状态在 `ORCA_HOME` 下（默认 `~/.orca/`，按 OS 用户天然分）；多租户/容器用 `ORCA_HOME=/data/orca/<tenant>`。注册表/端口登记/tape/产物全在各自 `ORCA_HOME`，物理隔离。
- **server 隔离**：指纹 = `sha1(ORCA_HOME)`，不同 `ORCA_HOME` → 不同指纹 → 不同 server，天然不串。
- **鉴权预留**：所有端点 + WS 接受可选 `Authorization`/`?token=`（当前 `_auth.py` no-op stub，D12/§5.8）；未来换真实 token 校验只改 stub 一处。
- **残留威胁（未启用鉴权前的已知面）**：loopback 按机器共享，同机另一 OS 用户扫到端口可读 run。启用 D12 token 后消除。hardlink 跨 home 依赖 OS `protected_hardlinks=1`。

---

## 10. 分阶段实施

- **Phase A**（前置，最小）：D1 身份解耦 + D6 端口登记上移。`_identity.py`/`attach.py`/`commands.py` ~10 行 + 测试。验收 AC1/AC13。**可独立先开工。**
- **Phase B'**（替代 v1 Phase B 集中化）：D2 + D4 注册表 + §5.4 `POST /api/run` + `start_run` 增 project 参数 + §5.5 注册表 allowlist。`iface/cli/_project.py` 新增；`bg_runner`/`in_session/cli.py` bootstrap 调 `register_project`。验收 AC2/AC15。**无历史迁移、无产物搬家。**
- **Phase C**（web 监控）：D5 discovery + D7 懒挂载 + D9 列表层 + `_scan_meta_overview` contract test（I-9）。`run_manager` 加 discovery/ensure_attached；前端加列表页 + `runListStore`。验收 AC3–12/AC14。
- **Phase D**（未来）：`orca gc`（清注册表死项 + 孤儿 runs）+ `orca project list`。

> **关键**：本路线**无 v1 的历史迁移 phase**（现状即目标），Phase D 只做注册表/孤儿清理。

---

## 11. 非目标

- 不集中化历史进 `~/.orca/`（D3，推翻 v1/plan Phase B）。
- 不做 run 数据库/SQLite 索引（plan §9；注册表非 run 数据索引）。
- 不做 WS 全局广播/SSE 监控通道（R2）。
- 不做多用户鉴权（§9）。
- 不做历史迁移工具（本路线无迁移需求）。
- 不改 `orca install` host dotdir 部署。

## 11b. 零回归保证（功能增加，不影响既有）

本 SPEC 是**纯增量**，既有功能零改动：
- **`orca run`/drive_loop/executor/in-session/TARS skill**：行为零改；既有测试套件全过（AC20）。
- **既有 web 详情页**（`/runs/:runId` 三栏 + TopBar + gate + charts）：路由/组件/store 零改；懒挂载对详情页透明。
- **`orca open <rid>`**：保留深链直达（D13），仅复用单端口。
- **改动面收口**：新增 `_project.py`/`_auth.py`/列表页/`runListStore`/discovery/ensure_attached/DELETE；`run_manager`/`ws_handler`/`_identity`/`commands.py` 为**增量扩展**（新方法/新分支），不改既有方法签名语义（`start_run` 加 project 参数除外——既有调用方 CLI 侧同步）。
- **守门**：CI grep 确认 `workflow-store` 未被列表层 import；既有 web/in-session 单测全绿回归。

---

## 12. 开放问题（spec-review 已闭合项标注）

1. ✅ **(reviewer I-1 闭合)** artifacts 签名零改——本路线不搬产物，5 处调用点零改。
2. ✅ **(reviewer I-2 闭合)** 资源解析走 handle.tape.path——无单一 runs_dir 跨项目缓存问题。
3. ✅ **(reviewer I-5/I-13/I-22 闭合)** split 消除——assets/artifacts 同处，无阈值、无两处 gc。
4. **(待 Phase C 实测)** N=1000 discovery 墙钟 ≤3s 是否现实（AC12）；超限→per-project lazy enumerate（先扫注册表项目列表→前端懒加载各项目 runs）。
5. **(待 Phase B' 实证)** `projects.json` flock 在 Windows `msvcrt.locking` 的行为一致性。

---

## 13. v4 修订（权威——覆盖 v3 冲突条款；coder 实施唯一基准）

> 来源：spec-reviewer 6 blocker + 17 major（闭环）+ 架构最优性 P0–P3。**凡与本节冲突的 v3 措辞，以本节为准。** 3 个用户决策已采纳 reviewer 推荐。

### 13.1 已采纳决策
- **U-1（attached live run 删除）**：→ **409 Conflict**（不删，错误体 `run is live in process <pid>; stop the host first`）。**in-process run** → `cancel_run`（终态）+ 删 tape/run 目录。
- **U-2（health 字段）**：兼容期 health **同发** `runs_dir_fp`（值=orca_home_fp）+ 新 `orca_home_fp` 两字段；下个版本去旧名。
- **U-3（控制帧通道）**：→ **每 WS 一个 `asyncio.Queue` + 单 writer task 串行化出站**（复用 `/ws`，不新端点；符合 R2）。

### 13.2 Blocker 修订（覆盖 v3）
- **B-1（start_run 签名，覆盖 §5.4）**：`start_run(yaml_path, inputs=None, task=None, max_iter=None, *, resume=False, project_path: str|None=None)` —— `project_path` **keyword-only 可选**；缺省时 manager 内 `detect_project_root()`+`register_project()` 自填。**既有 40+ 调用面零改**。web `POST /api/run` body 仍**必填** `project_path`（缺失→400）。`project_id` 不进函数签名（内部从 project_path 派生）。AC20 = 「既有调用形态零改」。
- **B-2（依赖铁律，覆盖 §5.6）**：注册表下沉到中立层 **`orca/runtime/_project.py`**（仅依赖 stdlib + `orca/schema`），cli 与 web 都从此 import。**禁止**放 `iface/cli/`（web 反向 import cli 违单向依赖）。
- **B-3（RunManager 字段演化，覆盖 §5.5）**：`runs_dir` 字段语义从「唯一根」**退化为「in-process run 默认根」**（兼容保留）。新增 `is_allowed_tape_path(path) -> bool` 走**注册表 allowlist**（仅 attached/external 路径用）。`resolve_tape_path` 增 allowlist 分支（注册表任意项目 `runs/` 下即放行）；in-process run 仍写 `runs_dir`。受影响方法：`resolve_tape_path`/`resolve_asset_path`/`runs_dir` property（语义演化，非删除）。
- **B-4（WS 控制帧基础设施，覆盖 §5.7/D11）**：每 WS 一个 `asyncio.Queue` + **单 writer task** 串行化所有出站帧（pump 把 bus 事件 enqueue；广播回调把 `run_changed` enqueue）。新增 **WS connection registry**（独立于 `_subs`，广播遍历此 registry）。解决 FastAPI 单 WS 不支持并发 send 的 `RuntimeError`。列入 Phase C 工作量。
- **B-5（DELETE 二分支，覆盖 §5.7）**：见 U-1。in-process → `cancel_run`+删盘；attached 且 live（follow task alive / 他进程）→ **409**。attached 且已终态（terminal）→ 直接删盘。
- **B-6（端口登记临界区，覆盖 D6）**：**「决策+spawn+bind+socket-ready+写回 ready 信号」必须在同一 exclusive flock 临界区**（持锁到 server 监听 socket ready 后释放）。AC13 增「两并发 open 的 loser 读到 winner 的 port 并 health check 通过」。

### 13.3 Optimality P0–P3
- **P0（discovery 性能，覆盖 §5.2/AC12）**：增**派生缓存** `<project>/runs/.orca-meta-cache.json`（**cache 非 index**：按 `(mtime,size)` 校验，失配重扫，可删可重建，**不违 R1/§9**）。`_meta_cache` per-process 内存 + 持久层兜底。**Phase C 第一周 spike**：1000 tape 实测冷扫墙钟；≤3s 则仅内存缓存，超 3s 启用持久缓存。AC12 改 `pytest -m perf` 默认 skip + ±50% 容差。
- **P1（注册表鲁棒，覆盖 §5.6/§8）**：`projects.json` 原子写 `tmp+os.replace` + 保留 `.bak`；读时 parse 失败→读 `.bak`→仍坏 fail loud + 提示 `orca project rebuild`（扫已知项目重新注册）。模块内**单一 `_with_lock()` helper，公开 API 禁嵌套调用**（防 Windows msvcrt 死锁）。
- **P2（覆盖 D2/D4）**：`project_id` 是 path 的**派生指纹，path 是真实身份**；**禁止**用 project_id 跨重命名做去重/合并。D2 排除 `resolve(project_root)==resolve(ORCA_HOME)`（fail loud，防 cwd=ORCA_HOME 锚定）。
- **P3（列表可见性，覆盖 §6.3）**：列表加「Stale projects」只读折叠区（path-missing 注册项 + `orca gc` 提示）。

### 13.4 关键 Major 修订
- **M-1（多用户预留，覆盖 §5.8/AC19）**：改 **FastAPI middleware 全局兜底**（`AuthMiddleware` no-op stub），路由层零 Depends；AC19 = `app.user_middleware` 含 AuthMiddleware 单测。
- **M-3（DELETE 响应，覆盖 §5.7/AC16）**：200 `{ok:true, run_id, existed_before:true}`；404 `{ok:false, never_existed:true}`；409（attached live）`{ok:false, live:true, pid}`；Windows file-locked → 409 + pid。
- **M-5（RunSummary schema，覆盖 §5.2/AC3）**：Pydantic `Config.extra="forbid"` + `response_model_exclude_unset=True`；AC3 加反向 fixture。
- **M-6/M-7（迁移）**：D1 调用点迁移清单（`_identity.py`/`attach.py`/`commands.py`）；D6 读时 **fallback 旧 `<runs_dir>/.orca-web.json` 静默迁移一次**（不破坏既有）。
- **M-8（控制帧标识，覆盖 D11）**：`run_changed` 帧 `kind:"control", action:"deleted"|"changed"`；前端 `processEvent` 见 `kind==="control"` 即拒（不进 reducer）。
- **M-12（懒挂载索引，覆盖 D7）**：`_run_path_index` per-process dict，**每次 `GET /api/runs?scope=all` 重建**；miss 后扫注册项目成本上限 = N（项目数）。
- **M-15/M-16（注册防 poisoning，覆盖 §5.4/§5.6）**：`register_project` 拒绝 OS 顶层目录（`/etc`/`/usr`/`/bin`/`/var`/`/sys`/`/`/`/home` 等）+ 要求 path 下含 `workflows/` 或 `.orca/config.json` 之一。
- **M-17（contract test，覆盖 AC14）**：从 `orca.schema.EventType` **自动派生** status-affecting 子集（白名单之外都算），新增 EventType 强制进测试。
- **AC4** 显式「针对 dormant/terminal tape」（live run mtime/size 持续变不命中缓存，另立 tail 契约）。**AC9** 加「列表页 unmount 后无 orphan pump/writer task」。**M-14**：列表页 WS 不订阅任何 run（仅收控制帧）；详情页 WS 订阅 run 也收控制帧。

### 13.5 §11b 修正（零回归边界）
本 SPEC 为**功能增加，既有行为零回归**；但因结构演化，以下调用面**需同步**（非破坏性）：
- `start_run` 签名扩展 `project_path` keyword-only 可选（**既有调用零改**，B-1）。
- `RunManager.runs_dir` 语义演化为「in-process 默认根」（兼容，B-3）。
- `resolve_tape_path` 增 allowlist 分支（既有 in-process 路径行为不变）。
- health 增 `orca_home_fp` 字段（旧 `runs_dir_fp` 兼容期保留，U-2）。
- **删除** v3「纯增量、既有方法签名语义零改」字面声明（不实）。AC20 = 「既有调用形态零改 + 受影响调用点同步清单 + 既有测试套件全绿（`test_v3_step1` pre-existing 失败除外）」。
