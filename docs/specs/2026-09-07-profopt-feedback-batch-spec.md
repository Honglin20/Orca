# SPEC：prof-opt 反馈批（web UX + 文档内容通道 + rounds 留档）

> 来源：用户 2026-09-07 八条反馈；全部决策已在计划阶段拍板并记录于
> [`docs/plans/2026-09-07-profopt-web-ux-and-artifacts.md`](../plans/2026-09-07-profopt-web-ux-and-artifacts.md)。
> 部署事实（用户确认）：serve 与 workflow 常态**异机**，run 产出的 artifacts 仅存在于
> 跑 workflow 那台机器的本地盘；tape/runs 目录对 serve 可见（run 列表与 chart 事件可达）。
> 本 SPEC 是实现契约（R1 版，已吸收 spec 评审环 16 项修订 + 4 项一行补钉），
> 逐字实现；发现契约问题按 SPEC_ISSUE 上报，不静默偏离。

---

## 0. 全局约束与风险披露

- **平台**：workflow 脚本（`_po_scripts/`）跑在 Linux/WSL，python3 stdlib-only（与现状一致）；前端构建/测试 win32 node 可跑。
- **测试命令**：后端 `wsl -e bash -lc "cd /mnt/d/Projects/Orca && .venv/bin/python -m pytest <file> -q"`；前端 `cd orca/iface/web/frontend && npx vitest run` + `npx tsc --noEmit`。
- **验收深度（用户拍板）**：仅单测自验，无真机 E2E。
- **依赖铁律**：不新增后端端点；不改 chart payload 结构校验（C3 内容以 table 数据行普通字段通过既有通道）；不改 workflow.yaml 拓扑（C1 挂在既有 PO-GATE script 节点的 `gate_node.sh` 内）。
- **数据通道单一真相**：文档面板的清单与内容一律来自事件流（chart 通道）；文件系统端点仅作同机回退。
- **风险披露（单测不可见类，R-16）**：
  1. 多次接近聚合预算（1.5MB/次）的成功推送可累计把 run 推过 5MB huge 阈值（`run_manager.py` huge 判定）→ 详情页每次打开走全量加载（慢、内存高）；huge 占位分支在当前 attach 流程不可达（`workflow-store.ts` attach 恒 `hugeFullyLoaded=true`），属防御性 UI；
  2. 内容通道体量 / 跨 run state 作用域类缺陷属单测盲区——**首次真机 in-session 短跑时人工核验一次**（第二次推送后面板内容仍可读、`rounds/<NNN>/<vid>/shadow` 存在）。

## 1. A1（#4 辅修复）artifacts 根回退解析 —— 后端

> **状态：已写码**（`orca/iface/web/run_manager.py`，工作树未提交）。coder 须纳入本批：复核实现与契约一致（含 R-5 补丁）+ 补齐测试 + 随本批 commit。

### 契约

`RunManager._recorded_artifacts_dir(run_dir: Path) -> Path | None`：
- 读 `run_dir / "orca_env.sh"`（utf-8，errors="replace"）；`OSError` → `None`。
- 逐行解析：仅认 strip 后以 `export ` 开头的行；`shlex.split` 后取形如 `ORCA_ARTIFACTS_DIR=<value>` 的 token；**多行取最后一条**（与 shell source 语义一致）。不 `exec`。
- **`shlex.split` 抛 `ValueError`（坏引号等）→ 该行忽略**，继续解析后续行（库内先例：`orca/chart/_env.py:96-99` 同款保守降级）；全部行无效 → `None`。
- **空 value 行**（`export ORCA_ARTIFACTS_DIR=`）按「最后写入为空」处理 → 无记录（与 shell source 语义一致；与现行实现行为逐字一致）。
- `<value>` 为绝对路径且 `is_dir()` → 返回该 `Path`；否则 `None`。

`RunManager.resolve_artifacts_root(run_id) -> Path | None` 解析顺序：
1. run 存在性守卫（`_runs` 无 → `None`）；
2. `_recorded_artifacts_dir(runs_dir / run_id)` 非 `None` → 返回之（project-scoped 真实落点优先）；
3. 回退 `artifacts_dir_for_run(runs_dir, run_id)`，不存在 → `None`（404 语义不变）。

`runs_dir` = tape 所在目录（tape 不可得回退 `self._runs_dir`）——与现状一致。

### 验收标准

1. `orca_env.sh` 记录的目录存在 → 端点按该根解析文档（project-scoped 场景 200）。
2. 记录的路径不存在（异机）→ 回退 per-run 派生根；两处皆无 → 404。
3. 无 `orca_env.sh`（老 run）→ 行为与现状逐字一致。
4. 记录值为相对路径 / 非目录 / 坏引号行 → 视为无记录（坏行跳过不崩、不 500）。
5. 响应正文永不包含 artifacts 根绝对路径——**同时断言 recorded 根与 per-run 根两个绝对路径都不泄露**（既有 helper `_artifacts_root` 是 per-run 派生，recorded 根须另行造盘断言，R-17）。

### 验收用例（`tests/test_web_artifacts_docs.py` 追加）

- happy：造 project-scoped 目录 + 写 `orca_env.sh` 指向它 → `GET .../artifacts/file?path=variants/v1/assessment.md` 200 且正文一致。
- sad：`orca_env.sh` 指向不存在路径 → 回退 per-run 根（per-run 有文件则 200，无则 404）。
- edge：`orca_env.sh` 值带 shlex 引号（含空格路径）/ 多行重复 export（末行生效）/ 相对路径 / **坏引号行 + 其后有效行（坏行被跳过、有效行生效）** → 按契约。
- edge：无 `orca_env.sh` → 既有用例全绿（回归）。
- 红线：双根（recorded + per-run）绝对路径均不出现在响应正文。

## 2. B1（#8）R 徽标口径 —— 前端

### 契约

- `workflow-store.ts` `node_started` handler：为该节点维护 `execSessions: string[]`（按首次出现序去重收集 `event.session_id`；重复 seq 已由上层去重，数组去重保证 refold 幂等）。
- **存放点钉死 `NodeState`（`state.nodes[node].execSessions`），禁入 nodesIndex**——nodesIndex 只收 CONVERSATION_TYPES 事件且把缺席 session_id 转成 `"main"` 哨兵（`workflow-store.ts` `?? MAIN_SESSION`），node_started 入内会污染子代理计数。
- **仅非空 string `session_id` 入 `execSessions`**；null / 缺席 → 忽略（in-session 路径 node_started 无 session_id，`_step_io.py`）→ `execSessions` 为空 → 回退旧派生（诚实降级，与现状一致非回归）。
- **口径披露**：retry（executor 每次真实执行产出新 session_id）→ 计入 R（= 节点自身执行次数，计划 B1 既有拍板）；recoverable/resume 重发的 `node_started` 无 session_id → 被「仅非空 string」规则排除不计入；foreach/parallel 计数语义未验证，不在本批验收内。
- `selectors.ts` Loop 组：`iteration = execSessions.length > 0 ? execSessions.length : 既有 sessionCount 派生`。`sessionCount`（「N subs」）**不变**。

### 验收标准

1. agent 节点 3 次自身派发（3 个 `node_started` session）+ 21 个子代理 conversation session → R 显示 **3**（现状显示 24）。
2. script 节点 3 次执行 → R3（不变）。
3. 无 `execSessions` 或全 null session → 回退旧派生（不显示 0/Rundefined）。

### 验收用例（vitest，selectors/store 层）

- happy：3 own + N sub sessions → iteration===3。
- sad：仅有子代理 session、无 node_started → 回退旧值。
- edge：重复 session_id 的 node_started（refold 重放）→ 仍计 1。
- edge：`session_id` 为 null 的 node_started → 不入 execSessions。

## 3. B2（#2）图表区不再渲染 docs 清单 —— 前端

### 契约

- 过滤落在 **`ChartRenderer` 的分组处**（`ChartsView` 是薄壳）：`label === "prof-opt/docs"` 的 chart 在 **valid 与 huge placeholder 两个分支都被滤除**；**过滤须整组剔除（不留 0 项组壳），空态判定在过滤之后**——docs-only run（仅 `prof-opt/docs` 一个 chart）显示既有 `chart-empty` 空态（诚实空态，非过滤副作用；编排者代行拍板选项 a）。
- 推送侧（C3）不停推。过滤处注释写明该契约（推送是文档面板数据源）。

### 验收标准

1. `prof-opt/docs` 清单不出现于图表分组渲染（valid 与 huge 占位两形态都不出现）。
2. `selectCharts` 输出不变——文档面板仍能从事件解析出同一清单（既有面板测试保持绿）。

## 4. B3（#6）单图尺寸自适应 —— 前端

### 契约

- `ChartGroup.tsx` 常量 `SINGLE_CHART_MAX_WIDTH = 560`。
- 当 `placeholders.length === 0 && visuals.length === 1` 时，该唯一 visual **外包一层 div 承载 `maxWidth: SINGLE_CHART_MAX_WIDTH`**（`LazyChartWidget` 无 style 通道；JS 判定，禁止 CSS 伪类 hack）。
- table 仍 `gridColumn: 1 / -1` 整行；占位卡组、≥2 visuals 组行为不变。

### 验收用例（vitest，ChartGroup 渲染）

- happy：组内 1 张 line 图 → 该 item style 含 maxWidth 560。
- edge：1 图 + 1 表 → 图受限、表整行；2 图 → 均无 maxWidth；仅占位卡 → 无 maxWidth；**1 visual + 1 placeholder → 无 maxWidth**。

## 5. B4（#5 + #4 主修复）文档独立页签 + 图标卡片网格 + 内容事件通道 —— 前端

### 契约

- `RunDetailPage`：新增「文档」页签，`ProfOptDocsPanel` 独占该页签；从「图表」页签移除。
- `ProfOptDocsPanel`：
  - 清单按组分区（既有 `docGroupOf`：基线/变体/轮次/规则），组内文件渲染为**图标卡片网格**（非一行一行）：卡片 = 文件类型图标（lucide：`.md`→FileText，`.json`→Braces，其余 FileText）+ 文件名 + 状态点（沿用 `statusClass` 配色）+ 可选 updated_at。
  - 点卡片 → 正文预览（面板内预览区）。`.md` → `MarkdownText`（图片重写逻辑保留）；其余 → `FileContentView`。
  - **内容合并（selectors 层，R-1）**：新增 selector（建议名 `selectDocRowsWithContent`，签名与既有 selectors 消费方式对齐）：以 max-seq 清单行（既有 `parseDocManifest` 输出）为基；行 `content` 为空且无 `content_omitted` 时，按 seq 升序扫同 label（`prof-opt/docs`）**全部历史 payload** 中同 `path` 行的**最近非空 content** 补上；历史亦无 → legacy 回退 fetch。`selectCharts` 输出不变（合并逻辑置于 selectors，保「selectors 唯一 view 输入」）。**数据源钉死 = `state.events` 原始 `custom` 事件序列**（含被 selectCharts identity upsert 覆盖的旧条目——同 identity 后到胜，扫 selectCharts 去重输出必丢首推 content），**非** selectCharts 输出。
  - **代码组织（R-A）**：`parseDocManifest` / `DocRow` / `docGroupOf` 随本批从 `ProfOptDocsPanel.tsx` 迁至 `selectors.ts` 导出（组件改 import；`GROUP_ORDER` 与渲染标签留组件——中文组名属展示层）；**禁止 selectors 反向 import 组件**（`ProfOptDocsPanel` 已 import selectors，反向即循环依赖）。既有测试的 import 路径同步更新。
  - **huge 模式**：`huge && serverOverview && !hugeFullyLoaded` 时沿用既有 placeholder 语义（面板保留 `docs-huge-hint` 分支），合并 helper **不得静默返空清单**（否则 UX 回归为「暂无清单」）。
  - **内容三态渲染**：合并后 `content` 非空 → 直接渲染，**零请求**；`content` 空且 `content_omitted === "true"` → 显式提示「内容过大或本批未推送，仅清单可读」（不发起 fetch——异机必 404，fail loud）；`content` 空且未标注（legacy run）→ 回退既有 artifacts fetch，404 提示保留。
- `parseDocManifest` 契约：payload 行是**字段名 dict**（非位置数组），按字段名解析；新增可选字段 `content: string`、`content_omitted: string`（值为 `"true"` 或空/缺席），前端判定统一 `=== "true"`。legacy 行（无这两个字段）与「有字段但无内容」行为等价（回退 fetch）——有意设计。
- 既有 data-testid 尽量保留；引用行式 DOM 的既有测试同步更新。

### 验收标准

1. 「文档」为独立页签，图表页签内无文档面板。
2. 卡片网格按四组分区渲染；点卡片出正文。
3. 带 `content` 行 → 预览渲染 content 原文，网络层零 fetch（mock fetch 断言未调用）。
4. `content_omitted="true"` → 显式提示，不 fetch。
5. legacy 行 → 回退 fetch 路径行为与现状一致（含 404 提示）。
6. **两连推合并**：第一推带 content、第二推同 path 行 content 为空 → 面板渲染第一推内容、mock fetch 零调用。
7. huge 未 loadFull → 显示既有 huge 提示分支（不显示「暂无清单」）（当前生产 attach 流程不可达，防御性用例）。

## 6. C1（#1）rounds 下每轮 shadow 源码留档 —— workflow 脚本

### 契约

- 新脚本 `workflows/prof-opt/agents/_po_scripts/archive_round_shadow.py`（stdlib-only，argparse：`--artifacts` 必填）。
- **round 解析**：复用 `_po_scripts/round_state.py` 现成函数取当前轮号（禁止新造启发式）；**round=0（rounds/ 无数字目录）→ no-op、exit 0、不建任何目录**。
- **vid 全集**：`rounds/<NNN>/proposals.json`——顶层 `{"round": R, "proposals": [...]}`；vid 列表 = `proposals` 各元素的 `vid` 字段（按 list 迭代；现状每轮恰一条提案，**不得假设长度 1**）。文件缺失/unparseable/顶层非 dict/`proposals` 非 list → error 文件（键 `__round__`）+ exit 0。
- **复制（原子 + 幂等）**：对每个 vid，源 `variants/<vid>/shadow`（须为目录）→ 先拷至 `rounds/<NNN>/<vid>/.shadow.tmp.<pid>`（**写前先清可能残留的同名 tmp**，防 pid 复用 `FileExistsError` 谎报失败），成功后 rename 为 `shadow`，失败删除 tmp；`dst` 为完整目录 → 跳过（幂等，重放安全）。**仅 shadow 源码树**：`shutil.copytree(ignore=ignore_patterns("__pycache__", "*.pyc"))`（不含 onnx/profile，不留字节码）。
- **失败路径（fail loud 不骑劫主流程）**：单 vid 失败（源缺失/复制 OSError）→ stderr 一行 + 合并写入 `rounds/<NNN>/shadow_archive_error.json`（`{"<vid>": "<error>"}`，多次调用合并）；`rounds/<NNN>/proposals.json` 缺失/不可读 → 同样落 error 文件（键 `__round__`）。本轮全部 vid 成功 → 写 error 文件前先删除既有 `shadow_archive_error.json`（stale 披露不跨重放残留）。**脚本自身恒 exit 0**。
- **挂点**：`gate_node.sh` 在 deploy `--verify` 块之后、incumbent promote 块之前插入：`python3 "$ART/scripts/archive_round_shadow.py" --artifacts "$ART" || echo "archive_round_shadow failed (non-zero; see stderr)" >&2`（意外崩溃也不阻断 gate，但 stderr 可见）。
- **披露（R-15）**：mid-run resume 的旧部署集无本脚本 → 挂点 stderr 一行、无留档、gate 不受阻。

### 验收标准

1. gate 后 `rounds/<NNN>/<vid>/shadow/` 与 `variants/<vid>/shadow/` 内容一致（仅源码，无 `__pycache__`/`*.pyc`）。
2. 重复执行（gate 重放）零副作用。
3. 任何留档失败：gate 决策不受阻，error 文件 + stderr 可见。

### 验收用例（tests/test_po_scripts.py 或按既有惯例新文件）

- happy：2 个 variant → 两份留档，内容一致；onnx/profile/`__pycache__` 未被复制。
- edge：重复执行 → 幂等；`variants/<vid>/shadow` 缺失 → error 文件含该 vid、其余照常、exit 0；`proposals.json` 缺失 → error 文件 + exit 0；round=0 → 零目录创建 + exit 0。
- 挂点：`bash -n gate_node.sh` 通过 + grep 断言挂点行位于 deploy `--verify` 块之后、promote 块之前。

## 7. C2（#7）帕累托图补 baseline 参照点 —— workflow 脚本

> UD-1 已由编排者按用户授权代行拍板 = 评审推荐选项 (b)（计划「y=基线 gap」在 metric-basis 下量纲错位；(b) 在所有阶段给出维度正确的原点参照）。

### 契约

- `push_curves.py` `collect_pareto`：当 `base/origin_anchor.json` 存在且可读出基线时，追加锚点行：`vid="baseline"`、`status="baseline"`、`x=0`、`color=_BASELINE_COLOR`（**新常量，钉死 `#0ea5e9`**，与 `_STATUS_COLORS` 六色及中性色均不同，注释写明选色理由）。
- **y 值 basis（选项 b）**：
  - 存在任一 variant 行 gap 非空 → `y=0`（gap basis，基线 gap 恒 0，维度一致）；
  - 否则（全体 metric basis）→ `y = baseline/baseline_metrics.jsonl` 末行 metric（复用现成 `_load_curve`，与 variant y 同族）；
  - 基线曲线缺失/空 → `y=null` 占位（沿既有 caption 披露），**禁退化成 y=0 谎报**。
- caption 追加一句 mixed-basis 披露，**逐字**：`the baseline anchor (x=0) reads y=0 on the gap basis (baseline gap is 0 by definition) or the baseline's latest metric on the metric basis`。
- 已存在 `vid=="baseline"` 行 → 不重复追加（幂等）。
- anchor 缺失/不可读 → 不加锚点（行为与现状一致）。

### 验收用例（既有 push_curves 测试文件内追加）

- happy：有 anchor + 2 variant（含非空 gap）→ 行数 +1，baseline 行 x==0、y==0、color==`#0ea5e9`。
- edge：有 anchor、variant 全走 metric basis 且基线曲线存在 → baseline y == 末行 metric；曲线缺失 → y 为 null。
- edge：无 anchor → 行数不变；重复调用 → 仍只 1 条 baseline 行。

## 8. C3（#4 主修复）文档内容随清单走事件通道 —— workflow 脚本

### 契约

- `push_curves.py --docs` 清单行（字段名 dict）新增可选字段：`content: str`（文档全文或空串）、`content_omitted: str`（`"true"` 或空串/缺席）。既有五字段不变。
- **聚合预算（R-3）**：`MAX_MANIFEST_CONTENT_BYTES = 1_500_000`（**utf-8 字节**，低于 2MB 通道硬上限留 envelope 余量）；按白名单顺序填充，超预算行 `content=""` + `content_omitted="true"` + **每行一行 stderr（含 path，与超 256KB 行粒度一致）**；稳态总量持续 >1.5MB → 尾部行每次 trigger 重试（stderr 可见），已知行为非缺陷。预算按 content **原始 utf-8 字节计（序列化前）**；`ensure_ascii=False` 下残留转义（引号/反斜杠/换行）在 md/json 白名单膨胀 ≈5%，由 0.5MB envelope 覆盖，不做序列化后二次计量。**`_send` 的 `json.dumps` 改 `ensure_ascii=False`**（`push_curves.py:324`；transport 为 utf-8 行解码 + `json.loads`，ingestor 兼容 raw CJK——否则默认 ensure_ascii=True 把中文 content 膨胀 2-3 倍击穿 2MB，整包 NACK）。
- **columns 不变（有意，R-J）**：payload 顶层 `columns` 保持既有 5 列——docs 表经 §3 不进图表渲染，前端按字段名解析行；既有 columns 逐字断言（`tests/test_po_scripts.py`）零改动。
- **单文档上限**：`MAX_DOC_CONTENT_BYTES = 256 * 1024`（字节）。
- **三态真值表（R-9 + 终轮补钉，唯一权威）**：
  - 行**带 content** ⇔ 需推送（不在 state 或 (mtime,size) 有变）**且** size ≤ `MAX_DOC_CONTENT_BYTES` **且** 未超聚合预算；
  - `content_omitted="true"` ⇔（size > `MAX_DOC_CONTENT_BYTES`）**或**（本应带 content 但超聚合预算被截断）——逐次按当前 size/预算判定，与 state 无关；
  - content 空且无 omitted ⇔ 未变行（走 §5 R-1 前端合并，不重推内容）。
- **state 写回资格（终轮补钉，防内容永久丢失）**：仅当本次 socket 发送成功（既有 `_send` 成功路径）后写回——**写回 = 带 content 的行 ∪ 因 size>MAX 而 omitted 的行**（重试无益）；**聚合预算截断行不入 state**（下次 trigger 预算空闲时自然重试补齐）。
- **state 作用域（R-2）**：`$ART/.docs_push_state.<ORCA_RUN_ID>.json`（`relpath -> {"mtime": float, "size": int}`）——`$ART` 是 project-scoped 跨 run 共享，per-run 后缀防 run 间污染。`ORCA_RUN_ID` 未设（脱离 run 手跑）→ 视为空 state、全量带 content、不入盘。写 state 用 tmp + `os.replace` 原子替换；不加锁（三触发点分属 DAG 串行节点，无并发面）。
- **异常路径（R-12/R-22）**：文档读取 OSError/decode 失败 → `content=""` + stderr 一行 + 按无内容行处理（走合并）；清单为空 → 不推送（现状 `if docs_rows:` 行为）且 state 不写；state 文件缺失/损坏 → 视为空 dict 全量重推 + stderr 一行（不崩）。**「首推」定义 = state 为空时的第一次成功推送**。
- **触发点（R-E）**：既有两处 `--docs` 调用（`po_baseline/scripts/run_baseline_chain.sh` 首推、`po_report/agent.md` 终稿 `--title "(final)"`），不改推送频率、不加触发点。`push_curves.py` docstring 所称 propose emit 触发实未接线（陈旧描述），随本批修正 docstring（纯文档变更）。同 run 两推 identity 不同（title 后缀）→ 前端 max-seq 取终推、合并跨 identity 扫同 label（§5 R-1 已覆盖）。

### 验收标准

1. 首推：全部文档带 content。
2. 二推（无变更）：content 全空；touch 一份后三推：仅该份带 content。
3. >256KB 文档：content 空 + omitted 标注，**且该行入 state**（二推仍标 omitted，前端不误判 legacy）。
4. 预算截断行：omitted 标注 + **不入 state**（下轮重试带 content）。
5. 发送失败（socket 不可达，既有 fail-soft 路径）→ state 未写入，stderr 有行。
6. 中文为主的**多份**文档（各 ≤256KB、合计贴近但不超过 1.5MB utf-8，如 7×214KB）：`ensure_ascii=False` 编码后整包 < 2MB、单次发送成功，且断言这些行 `content` 非空。**单份 1.5MB 文档不得作为本用例素材**（必被 256KB 上限截断 → 假绿）——**禁用 ASCII 大文档测**（ASCII 测试绿 / 中文生产红的错位）。

### 验收用例（tests/test_po_scripts.py 既有 docs 测试处追加/修订）

- happy / sad / edge 如上六条 + state 文件损坏 → 全量重推不崩。
- **修订既有断言（R-10，测试同步不静默）**：`tests/test_po_scripts.py:2396-2397`（5 字段集断言 → 7 字段集）、`:2399-2407` 与 `:2410-2415`（二推 payload 逐字一致 → 二推未变行 content 为空 + 状态语义断言）。
- `_send` 编码：中文 content payload 断言序列化字节量与可被 `json.loads` 还原。

## 9. 非目标（本次不做）

- 不停推 docs 清单（面板数据源）；不加 #7 新图表/报告节；`verdict_distribution` 保留；
- AgentsRail「N subs」口径（own session 计入 subs）不动；
- 不做图片/二进制文档的内容通道（白名单均为 md/json 文本）；
- 不改 workflow.yaml 拓扑、不加后端端点；
- C3 的 per-run state 文件随 run 在 `$ART` 根累积（点前缀、KB 级、不进清单白名单），本批不清理；state 内 stale relpath 键无害（行仅来自白名单现存文件）。

## 10. 交付物

1. 上述 8 项代码 + 测试（后端 pytest、前端 vitest+tsc 全绿）。
2. 一次 commit（含已写码的 A1 与计划文档；**基线无关改动不入 commit**：`workflows/prof-opt/agents/po_baseline/agent.md`、`.e2e_*/`）。
3. 独立验证（test-agent 实跑三套测试）证据日志。
4. 状态文档收尾（release note + CHANGELOG + CURRENT.md 清空）。
