# Web Workflows 全资产展示设计（plan 2026-08-27 批 G）

状态：终稿（无人值守设计输入，批 G coder 按此实现）
上位契约：docs/plans/2026-08-27-workflow-per-dir-layout-plan.md §3 批 G + §4.7（既定约束逐字遵守）
行号基准：2026-08-27 实测（worktree post-批 F）；关键行号均已逐一核实。

## 0. 目标与范围

**目标**：workflows 浏览页（/workflows/:name）能看到每个 workflow 的全部资产——
workflow.yaml、agents（含 agents/<agent>/scripts/）、subagents（用户核心诉求）、
knowledge_base、脚本资产（<wf>/scripts/、agents/<agent>/scripts/、agents/_xxx_scripts）。

**现状缺口**（实测）：
- 中栏文件树只反映**单个 agent** 的 resources_root（store `openAgent` :200-223）。
  `agents/_quant_scripts/`（quant-sensitivity）、`<wf>/scripts/`（agent-struct-exploration）、
  `<wf>/knowledge_base/`、`<wf>/subagents/`、workflow.yaml 本身——全部不可见。
- detail 无 subagents 字段；左栏无 Subagents 区。

**非目标（不做）**：
- 不新增 subagents 独立列表端点（SPEC 钉死：进 detail response）。
- 不动 FileTree.tsx / CodeViewer / MarkdownText（现成组件直接复用）。
- 不动任何既有 placeholder 文案（§5.3 受保护字串清单）。
- 不改既有 5 个端点的 response 契约（只增不改；detail 只加键）。
- 不做树懒加载/分页（wf 目录规模小；单文件已有 1MB 上限）。
- 不引入 run/exec 依赖（依赖铁律：iface.web 只 import orca.compile + fastapi 标准件）。

## 1. 现状核实（行号锚点）

### 1.1 后端 orca/iface/web/routes/workflows.py（306 行）
- `_MAX_FILE_BYTES = 1_000_000`（:42）
- Endpoint 1 list `GET ""`（:50-56）；Endpoint 2 detail `GET /{name}`（:59-90，
  response :84-90 = name/description/entry/inputs_schema/agents_referenced）；
  Endpoint 3 agents fail-soft 列表（:93-135，`LocalPoolResolver().discover/resolve`，
  description 取 `handle.meta.description`，单 agent 失败 → `missing:true`）；
  Endpoint 4 agent tree（:138-150）；Endpoint 5 agent file（:153-185，守卫顺序
  404 越界/symlink/非文件 → 422 超 1MB → 422 二进制（前 2048 字节探 \x00））
- helper：`_resolve_context_for`（:193-210，`ResolveContext.workflow_dir =
  Path(yaml_path).parent`——**wf 目录 = yaml parent 的事实来源**）；
  `_resolve_agent_root`（:213-228）；`_safe_resolve`（:231-253）；
  `_build_tree`（:256-305，过滤 hidden/`__pycache__`/`.pyc`，目录先于文件、同 class 字典序）

### 1.2 可复用解析层（关键调研结论）
- `orca/compile/layout.py: resolve_subagents_dir(workflow_dir, wf_name)`（:30-48）：
  subagents 目录双形态单一真相源（per-wf `subagents/*.md` 直查 vs 旧平铺
  `subagents/<wf-name>/`，含「旧形态只查 is_dir 会误命中」的 glob 判据）。
  **必须复用**（DRY；validator/orchestrator 均消费它，web 不得自抄公式）。
- `orca/compile/agents.py: _parse_frontmatter`（:221-254）+ `_parse_meta_yaml`
  （:257-284）：**不可复用**——`AgentMeta(**data)`（:280）对未知字段 TypeError →
  ConfigurationError（fail loud）。实测 subagent md frontmatter 只有
  `subagent/version/sentinel` 三键（workflows/nas-supernet/subagents/ 5 个文件采样），
  直接复用会把**全部真实文件**打进 fail-soft，description 永远取不到。
- `orca/compile/validator.py: _parse_subagent_frontmatter`（:1191-1211）：**不可复用**——
  strict regex 只抽三协议键、无 description；缺任一键返 None（会丢「仅缺 sentinel 的文件」）。
- routes/workflows.py 自身**没有** frontmatter 解析函数（agents 列表的 description
  来自 resolver.resolve → handle.meta，解析在 compile 层）。

### 1.3 前端（实测路径：orca/iface/web/frontend/）
- `src/components/pages/WorkflowBrowsePage.tsx`（345 行）：三栏 PanelGroup；
  左栏 Workflow meta（:104-119）+ Agents 区（:121-173，referenced-only + missing 灰显）；
  中栏（:183-223，header `Files · ${activeAgent}` :186，空态 placeholder :189-196，
  FileTree 渲染条件 `activeAgent && fileTree && !treeLoading` :214）；
  右栏（:229-266，**空态/loading 均以 activeAgent 为门** :236/:252——wf 级文件必须解除此门）。
- `src/stores/workflow-browse-store.ts`（264 行）：inflightSeq gate（:114）；
  openWorkflow = Promise.all(detail, agents)（:164-167），m5 切换同步清空（:151-162）；
  openAgent（:200-223）；openFile 依赖 activeAgent 拼 URL（:225-242）；reset（:244-263）。
- `src/components/conversation/FileTree.tsx`（108 行）：props `{nodes, selectedPath,
  onSelect}`，行 testid `tree-dir-<path>` / `tree-file-<path>`——零改动直接喂 wf 树。
- 测试：`test/workflows-page.test.tsx`（147 行，**只测列表页 WorkflowsPage**，本设计
  预期零 diff）；`test/workflow-browse-store.test.ts`（406 行）；
  **WorkflowBrowsePage 无专测文件**（需新建）。
- `tests/iface/web/test_playwright_9b.py:180`：`input[placeholder='workflows/demo.yaml']`
  耦合在 /runs/new 表单（RunsNewPage），与 workflows 页无交集——约束按 plan §1
  「不重构既有 placeholder 文案」从宽执行（§5.3 清单）。

### 1.4 资产实况（14 个 wf 目录）
- nas-supernet：agents/（7 agent，ns_run_train 含 scripts/、ns_search_pipeline 含
  assets/+references/）+ subagents/（5 md）
- agent-struct-exploration：agents/ + knowledge_base/{README.md,common,families,index.json}
  + scripts/kb_graph.py + workflow.yaml
- quant-sensitivity：agents/_quant_scripts + agents/sensitivity-analyzer + workflow.yaml
  （`agents/_xxx_scripts` 型共享脚本资产——现状完全不可见，本设计核心受益者）

## 2. 后端 API 契约

### 2.1 Endpoint 2 修订：GET /api/workflows/{name}（detail 加 subagents）
- 路由/参数：不变。
- response 200（在 :84-90 基础上**只增一键**）：
  ```json
  {
    "name": "string（yaml name 字段）",
    "description": "string",
    "entry": "string",
    "inputs_schema": {"<key>": {"type","required","description","enum","default?"}},
    "agents_referenced": ["string"],
    "subagents": [{"name": "string", "description": "string"}]
  }
  ```
  - `subagents[].name`：**恒为 md 文件名 stem**（协议不变量 subagent==stem，
    validator :1273 强制；「缺 frontmatter 用文件名兜底」即 stem 恒出）。
  - `subagents[].description`：frontmatter `description` 键且为 str 时取值；
    否则空串（plan §3 批 G 钉死兜底：**不取 body 首行**——正文语义劫持）。
  - 排序：`sorted(dir.glob("*.md"))` 文件名字典序（稳定，golden 可测）。
  - 目录缺失（双形态均未命中）→ `[]`（无 subagents 的 wf 正常，非错）。
- 错误码：404 `workflow not found`（不变）。subagents 解析**逐文件 fail-soft**
  （读失败/编码错/YAMLError → `{name: stem, description: ""}` + logger.warning，
  仿 agents 列表 :121-134 模式）——detail 整体 200 不崩。
- 实现：模块级 helper（签名级）：
  ```python
  def _list_subagents(wf_dir: Path, wf_name: str) -> list[dict]:
      # resolve_subagents_dir（layout.py 复用）→ sorted(glob("*.md")) → is_file 守卫
      # → read_text + _subagent_description；except (OSError, UnicodeDecodeError) fail-soft
  def _subagent_description(text: str) -> str:
      # 首行 strip=="---" → 找闭合 "---" 行 → yaml.safe_load 块 → dict 且
      # description 为 str → 取值；frontmatter 未闭合/YAMLError/无键/非 str → ""
  ```
  新 import：`from orca.compile.layout import resolve_subagents_dir` + `import yaml`
  （compile ← iface.web 方向合法；yaml 是 FastAPI 环境必有依赖）。
  detail 端点内调用：`"subagents": _list_subagents(Path(yaml_path).parent, wf.name)`。

### 2.2 新 Endpoint 6：GET /api/workflows/{name}/tree（wf 级资产树）
- 路由：`GET /api/workflows/{name}/tree`（与 `/{name}/agents/{agent}/tree` 无路径冲突）。
- 参数：无。
- response 200：
  ```json
  {"workflow": "string", "root": "string（yaml parent 绝对路径）", "nodes": [TreeNode]}
  ```
  TreeNode（与 agent tree 完全同构，:288-304）：`{path, name, is_dir, size,
  children: [TreeNode]|null}`；path 为相对 root 的 POSIX 路径；过滤
  hidden/`__pycache__`/`.pyc`；目录先于文件、同 class 按 name 字典序。
- 错误码：404 `workflow not found`。
- 语义注记：root = `_resolve_context_for(name).workflow_dir`（yaml parent）。
  per-wf 形态即 `<wf-dir>`（迁移后仓库与安装态的常态）；**旧平铺形态下 root 是
  workflows 根本身**（SPEC 公式字面，过渡期可接受——测试钉死该语义防歧义）。
- 复用方式（推荐：**参数化直调，不抽新共享函数**）：`_build_tree` 本就是以 root
  为参数的模块级纯函数（:256-305），wf 级端点体仅 3 行——`_build_tree(root, rel="")`。
  不存在需要抽的 wrapper 逻辑；envelope 键名差异（agent vs workflow）在各自端点组装。

### 2.3 新 Endpoint 7：GET /api/workflows/{name}/file?path=<rel>
- 路由：`GET /api/workflows/{name}/file`，Query `path`（相对 wf root 的 POSIX 路径，必填）。
- response 200（与 agent file envelope 完全同构，:179-185）：
  `{path, text, ext, size, truncated: false}`。
- 错误码：404 `workflow not found`；404 `file not found`（越界/symlink/非文件/空 path，
  经 `_safe_resolve` :231-253）；422 `file too large: N bytes (limit 1000000)`；
  422 `binary file`（前 2048 字节含 \x00）。
- 与 agent file 端点的关系（plan §3 批 G 两选项的裁决）：**新增平行端点 + 抽共享读取函数**。
  - 为什么不复用 agent file 端点：其 root 是 agent resources_root（须先 resolve agent）；
    workflow.yaml、`scripts/kb_graph.py`、`agents/_quant_scripts/*` 多数不在任何 agent
    root 下；给 agent 端点加 scope 参数会破坏既有契约（约束 3 精神：既有耦合不碰）。
  - DRY 落点：把 `get_agent_file` :163-185 的守卫+读取段抽为模块级
    `_read_text_file(root: Path, rel: str) -> dict`（raise HTTPException 语义不变），
    `get_agent_file` 与 `get_workflow_file` 共同调用——两处同构的 1MB/二进制/404
    逻辑是 review 必挑的重复；现有 5 个 agent file 用例即重构回归网。
- 模块 docstring（:1-21）endpoint 清单补第 6/7 条。

### 2.4 依赖铁律核查
新增 import 仅 `orca.compile.layout`（compile ← iface.web 合法）+ `yaml`。
零 run/exec/events 依赖；零 orca/compile 侧改动（layout/agents/validator/catalog 全部只读复用）。

## 3. 前端设计

### 3.1 左栏 Subagents 区（同 Agents 列表样式）
- 位置：Agents 区块（:121-173）之后，同款 `border-t` 分节 + uppercase 小标题
  `Subagents（N）`。
- 行样式：与 agent 行一致（name + 下方 faint description 截断）；
  `data-testid="subagent-list"` / `subagent-row-<name>"`；
  空态文案 `该 workflow 无 subagents`（`data-testid="subagent-list-empty"`，
  镜像 agent-list-empty :127-134 模式；新文案不在受保护清单内）。
- 无 missing 灰显（后端已 fail-soft 兜底，subagents 列表没有缺失态）。
- 点击行为：`openSubagent(name)` → treeScope 切 "workflow"（必要时补载 wf 树，
  见 3.3）+ `openFile("subagents/<name>.md")` → 右栏 MarkdownText 渲染；
  FileTree 高亮 `subagents/<name>.md` 节点（path 天然对齐）。

### 3.2 资产树数据源共存（推荐：点击切源 + 默认 wf 树，不引 tab）
- **方案**：store 增加 `treeScope: "workflow" | "agent"`。
  - 进入页面 `openWorkflow` 成功后自动加载 wf 级树（scope="workflow" 为默认态）——
    用户落地即见全部资产（workflow.yaml/agents/subagents/knowledge_base/scripts）。
  - 点 agent 行 → `openAgent`（现有）+ scope="agent"（中栏收窄为该 agent 视图）。
  - 回全部资产：Agents 区块 header 下方、列表上方加一行「全部资产」入口
    （`data-testid="asset-scope-all"`，样式同 agent 行，scope==="workflow" 时高亮），
    onClick → `openWorkflowTree()`。
- **为什么不 tab**：tab 引入新 UI 元素 + 双 loading/错误态副本，改动面大于收益；
  「点击切源」复用现有行高亮语义，状态机仅一个 string 判别字段。
- **为什么不把 agent 树嵌进 wf 树**：wf 树本身已含 agents/ 全子树（信息不缺）；
  嵌套方案需二次拉取/去重，且失去「单 agent 聚焦」既有能力（只能加不能减）。
- 中栏 header：`Files · ${activeAgent ?? activeWorkflow.meta.name}`（"Files" 裸态
  仅在 meta 未加载时短暂出现；无测试耦合）。
- 中栏渲染条件改写（解除 activeAgent 门）：
  - FileTree：`fileTree && !treeLoading && !treeError`（不再要求 activeAgent）。
  - 既有 placeholder「选择左侧 agent 查看其资源目录」（:190-195 文案**原样保留**），
    仅在 `treeScope==="workflow" && !fileTree && !treeLoading && !treeError` 的
    极端态（树未载且无错）渲染——文案不改、出现频率降为边缘态。
- 右栏门同样解除 activeAgent 依赖（:236 `!activeAgent && preview-empty` →
  `!activeFile && !fileLoading && preview-empty`；:252 同理）——否则 wf 级文件无法预览。
  「选择文件查看内容」文案原样保留。

### 3.3 store 改动点（workflow-browse-store.ts）
- 类型：`interface SubagentSummary { name: string; description: string }`；
  `WorkflowDetail` 加 `subagents: SubagentSummary[]`；detailBody 局部类型（:168-174）
  同步加 `subagents`。
- 新 state：`treeScope: "workflow" | "agent"`（初始 "workflow"）。
- 新/改 action：
  - `openWorkflow`（:148-198 改）：m5 清空段（:151-162）追加 `treeScope: "workflow"`；
    Promise.all 成功、inflightSeq gate 通过并 set activeWorkflow（含 subagents）后，
    **同 action 内**发起 wf 树请求（独立 try/catch——树失败只写 `treeError`，
    meta/agents 照常可用，fail-soft 分层）。树写回守卫：`mySeq === inflightSeq &&
    get().treeScope === "workflow"`（防「用户已点 agent、慢到的 wf 树覆盖 agent 树」）。
  - `openWorkflowTree()`（新）：module 级 `treeSeq` 递增 gate（照 inflightSeq :114
    范式；openAgent 现无 gate，属既有行为不在本批修）；set
    `{treeScope:"workflow", activeFile:null, fileError:null, treeLoading:true,
    treeError:null}` → fetch `/{name}/tree` → `{fileTree, treeLoading:false}`；
    失败 → `{fileTree:null, treeLoading:false, treeError}`。
  - `openAgent`（:200-223 改）：追加 `treeScope: "agent"`。
  - `openSubagent(name)`（新）：`treeScope` 非 "workflow" 或 fileTree 为空时先
    `void openWorkflowTree()`（补载，容树曾失败）；随后 `openFile("subagents/"+name+".md")`。
  - `openFile`（:225-242 改）：按 `treeScope` 分流 URL——
    `"agent"` → 现有 `/{wf}/agents/{agent}/file?path=`；
    `"workflow"` → `/{wf}/file?path=`（activeWorkflow 判空保留）。
  - `reset`（:244-263 改）：清空段加 `treeScope: "workflow"`。
- 不轮询/不 import workflow-store 等 R3/m7 约束不变（既有 grep 守门测试 :48-92 覆盖）。

## 4. 实施顺序（批 G coder 执行序）

1. **后端重构先行**：`_read_text_file` 抽取（get_agent_file 改为薄 wrapper）→ 跑既有
   `tests/iface/web/test_workflows_routes.py` 全绿（重构回归网）。
2. **后端新增**：`_list_subagents` + `_subagent_description` + detail 加键 →
   `/{name}/tree` + `/{name}/file` 两端点 + 模块 docstring 更新。
3. **后端测试**：§5.1 用例落盘，WSL 跑绿。
4. **store**：§3.3 改动 + `workflow-browse-store.test.ts` 扩展跑绿。
5. **页面**：§3.1/3.2 改动 + 新建 `workflow-browse-page.test.tsx` 跑绿
   （`wsl bash -c "cd /mnt/d/Projects/Orca/orca/iface/web/frontend && npx vitest run test/workflow-browse-page.test.tsx test/workflow-browse-store.test.ts test/workflows-page.test.tsx"`）。
6. **placeholder 静态断言**：grep 确认 §5.3 清单字串在本次 diff 中零触及
   （playwright 不可跑环境的替代验收，plan §4.7 验收命令第 3 条）。
7. 批 H 衔接（不在本批）：TestClient/serve curl 冒烟——detail 含 subagents、
   tree 含 workflow.yaml+agents+subagents+scripts（样本：nas-supernet /
   agent-struct-exploration / quant-sensitivity）。

## 5. 测试清单

### 5.1 后端（tests/iface/web/test_workflows_routes.py 扩展，新增 ~13 用例）
fixture：现有 `wf_dir`（平铺 mywf，:54-105）追加 `subagents/mywf/legacy-sub.md`
（legacy 形态）+ 一个坏 frontmatter md；新增 per-wf 形态 fixture `pf_dir`：
`workflows/pfwf/workflow.yaml + subagents/{sa-with-desc.md 带 description 键,
sa-plain.md 仅三协议键} + scripts/s.py + knowledge_base/index.json +
agents/agent-x/agent.md + agents/_shared_scripts/h.py` + hidden/`__pycache__`/`.pyc` 污染。

1. `test_detail_subagents_per_wf_form` — 两 md 均列出；name=stem；
   带 description 键取值、仅协议键者 description=""。
2. `test_detail_subagents_legacy_flat_form` — 平铺 mywf + subagents/mywf/*.md →
   经 resolve_subagents_dir legacy 分支列出（双形态回归）。
3. `test_detail_subagents_missing_dir_empty_list` — 无 subagents 的 wf → `[]`。
4. `test_detail_subagents_fail_soft_bad_frontmatter` — 坏 YAML frontmatter md →
   仍在列表（stem + ""），detail 整体 200（fail-soft 意图，仿 :170-192 范式）。
5. `test_wf_tree_golden_per_wf` — root=pfwf 目录；顶层
   `[agents, knowledge_base, scripts, subagents, workflow.yaml]`（目录先文件+字典序）；
   hidden/pycache/pyc 过滤；**agents 子树含 `_shared_scripts`**（核心诉求锁定）；
   children 结构 / size>0 断言（仿 :204-233 golden 深匹配）。
6. `test_wf_tree_unknown_workflow_404`。
7. `test_wf_tree_legacy_flat_root_is_workflows_root` — 平铺形态 root=workflows 根
   （钉死「root=yaml parent」字面语义）。
8. `test_wf_file_read_workflow_yaml` — path=workflow.yaml → 200，ext="yaml"，
   text 含 name 字段。
9. `test_wf_file_reads_shared_scripts` — path=agents/_shared_scripts/h.py → 200
   （核心诉求锁定）。
10. `test_wf_file_traversal_404` — 参数化 `../other.yaml` / 绝对路径 / 空 /
    `%2e%2e%2f` / null byte → 全 404 file not found（wf 级越界=可逃到 workflows 根，
    必须锁死；仿 :282-325 八范式）。
11. `test_wf_file_symlink_404` — root 内 symlink → 404（OSError skip 守卫，仿 :303-313）。
12. `test_wf_file_binary_422`（仿 :328-335）。
13. `test_wf_file_oversize_422` — >1MB → 422，detail 含 limit（仿 :338-348）。
（既有 agent file 5 用例即 `_read_text_file` 重构回归，无需新增。）

### 5.2 前端 store（workflow-browse-store.test.ts 扩展，新增 ~8 用例）
1. openWorkflow 成功 → activeWorkflow.subagents 填充 + wf 树自动加载 +
   treeScope==="workflow"（mock detail/agents/tree 三路由）。
2. openWorkflow 树请求失败 → treeError 写入，activeWorkflow 照常（fail-soft 分层）。
3. openAgent → treeScope==="agent"；随后 openFile 走 `/agents/<a>/file` URL
   （断言 fetch URL）。
4. scope="workflow" 时 openFile 走 `/api/workflows/<wf>/file?path=` URL。
5. openSubagent("x") → activeFile.path === "subagents/x.md"。
6. m5 扩展：openWorkflow 入口同步清空并 treeScope 复位 "workflow"（扩 :133-182）。
7. reset 清 treeScope（扩 :384-405）。
8. 慢到守卫：openWorkflow 树响应晚于用户 openAgent → 不覆盖 agent 树
   （treeScope 守卫；review 闭环风格，仿 :285-338）。

### 5.3 前端页面（新建 workflow-browse-page.test.tsx，~6 用例；MemoryRouter 包
/workflows/:name，mock fetch 三路由，照 workflows-page.test.tsx 范式）
1. 渲染 detail meta + Subagents 区（subagent-row-* 行 + 描述截断）。
2. 中栏自动渲染 wf 级 file-tree（mock nodes 含 workflow.yaml/agents/subagents/scripts；
   断言 tree-file-workflow.yaml 存在）。
3. 点 subagent 行 → 右栏 file-markdown 渲染（mock file ext=md）。
4. subagents 空 → subagent-list-empty 文案。
5. 点 agent 行 → 中栏切 agent 树（header `Files · foo`；断言 fetch 到 agent tree）。
6. AC-16 视觉禁令 grep（渲染 HTML 无 bg-slate-/rounded-lg|xl|2xl/text-[10|11|13px]/
   裸 shadow，照 :125-146 逐字）。

**受保护 placeholder 字串（diff 零触及，静态 grep 验收）**：
`workflows/demo.yaml`（test_playwright_9b.py:180）、`选择左侧 agent 查看其资源目录`、
`选择文件查看内容`、`该 workflow 未引用任何 agent`、`加载 workflow…`、`加载文件树…`、`加载文件…`。

### 5.4 测试总数
后端 ~13 + store ~8 + 页面 ~6 ≈ **27 个新增用例**；既有 349 行路由测试 + 406 行
store 测试全量回归绿为完成条件。

## 6. 影响面文件清单
| 文件 | 动作 |
|---|---|
| orca/iface/web/routes/workflows.py | modify（抽 `_read_text_file`、加 `_list_subagents`/`_subagent_description`、detail 加键、2 新端点） |
| tests/iface/web/test_workflows_routes.py | modify（fixture 扩展 + ~13 用例） |
| orca/iface/web/frontend/src/stores/workflow-browse-store.ts | modify（§3.3） |
| orca/iface/web/frontend/src/components/pages/WorkflowBrowsePage.tsx | modify（§3.1/3.2） |
| orca/iface/web/frontend/test/workflow-browse-store.test.ts | modify（~8 用例） |
| orca/iface/web/frontend/test/workflow-browse-page.test.tsx | new（~6 用例） |
| orca/iface/web/frontend/test/workflows-page.test.tsx | 预期零 diff（只测列表页；列入验证范围） |
| orca/compile/*（layout/agents/validator/catalog） | 零改动（只读复用） |
| FileTree.tsx / CodeViewer / test_playwright_9b.py / RunsNewPage | 零改动 |

## 7. 风险与回退
- 最大风险：store 树写回竞态（慢到的 wf 树覆盖 agent 树）→ §3.3 双守卫
  （treeSeq + treeScope）+ §5.2 用例 8 锁死。
- wf 树体积（agents 全子树递归）：14 wf 实测最大 ~几十节点量级，整树 JSON 一次性
  返回可接受；不做分页（YAGNI，非目标已声明）。
- 回退：`git revert <G>`（单 commit；设计文档独立无害）。
