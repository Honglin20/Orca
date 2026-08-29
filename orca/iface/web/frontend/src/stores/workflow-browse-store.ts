// stores/workflow-browse-store.ts —— workflow / agent 浏览 store（只读）。
//
// plan idempotent-churning-lampson 闭环。**纯只读浏览**：list workflows → 看
// referenced agents + 全量 agents → 点 agent 看 tree → 看文件内容。无写入、无编排。
//
// **铁律（R3 grep 守门，复制 run-list-store.test.ts:81-102 范式）**：本 store **绝不**
// import workflow-store（详情页那套执行状态 store）。浏览页与执行页状态正交，混在一起
// 会把 read-only 浏览与 run-scoped 事件 fold 耦合。
//
// **m7：不轮询**（不像 run-list-store 4s 轮询）：workflow 定义是静态文件，每次 list
// 要解析全部 yaml + 物化 agent 引用，成本远高于 run 元数据查询。用户主动 refresh 即可。
//
// **m5：openWorkflow 切换时同步清空 activeAgent/fileTree/activeFile**——防闪现上一 wf
// 的文件树（旧数据短暂出现在新 wf UI 里造成困惑）。
//
// **批 G（2026-08-27）**：treeScope 状态机（"workflow" | "agent"）——中栏文件树双数据源。
// openWorkflow 成功后自动加载 wf 级全资产树（默认态）；openAgent 切 agent 树；
// openWorkflowTree 回全部资产。openFile 按 treeScope 分流 URL（agent root vs wf root）。
// 慢到守卫：openWorkflow 树写回查 inflightSeq + treeScope 双条件，openWorkflowTree
// 用独立 treeSeq gate——防「用户已点 agent、慢到的 wf 树覆盖 agent 树」。
//
// **批 H（2026-08-27）review 修复：竞态守卫矩阵**——写回守卫（身份快照）：openFile
// 查 wf 名 + agent 名 + scope + fileSeq，openAgent 查 wf 名 + agent 名 + scope；
// 作废 bump（上下文更换即作废在途请求）：openWorkflow/reset bump treeSeq+fileSeq，
// openAgent bump treeSeq，openWorkflowTree bump fileSeq。覆盖跨 wf 竞态（openWorkflow
// 复位 treeScope 后 scope 快照失效）、同 wf 连点、切 agent 复活、同名 wf 重开四个
// 窗口族。
//
// 设计：plain zustand（无 immer），照 run-list-store.ts。模块单例 + unmount reset。

import { create } from "zustand";

export interface WorkflowMeta {
  name: string;
  description: string;
  entry: string;
  inputs_count: number;
  inputs_schema: Array<{ name: string; type: string; description: string }>;
}

export interface SubagentSummary {
  name: string;
  description: string;
}

export interface WorkflowDetail {
  meta: {
    name: string;
    description: string;
    entry: string;
    inputs_schema: Record<
      string,
      { type: string; required: boolean; description?: string; default?: unknown }
    >;
  };
  agents_referenced: string[];
  all_agents: AgentSummary[];
  subagents: SubagentSummary[];
}

export interface AgentSummary {
  name: string;
  is_folder: boolean;
  description: string;
  missing: boolean;
}

export interface TreeNode {
  path: string;
  name: string;
  is_dir: boolean;
  size: number;
  children: TreeNode[] | null;
}

// 批 G：wf 级树 envelope 键是 ``workflow``（agent 树是 ``agent``）——两者都只消费
// nodes，envelope 标识键按端点各自可选。
export interface TreeResponse {
  agent?: string;
  workflow?: string;
  root: string;
  nodes: TreeNode[];
}

export interface FileResponse {
  path: string;
  text: string;
  ext: string;
  size: number;
  truncated: boolean;
}

interface WorkflowBrowseState {
  workflows: WorkflowMeta[];
  workflowsLoading: boolean;
  workflowsError: string | null;

  activeWorkflow: WorkflowDetail | null;
  workflowLoading: boolean;
  workflowError: string | null;

  activeAgent: string | null;
  treeScope: "workflow" | "agent";
  fileTree: TreeNode[] | null;
  treeLoading: boolean;
  treeError: string | null;

  activeFile: FileResponse | null;
  fileLoading: boolean;
  fileError: string | null;

  loadWorkflows: () => Promise<void>;
  openWorkflow: (name: string) => Promise<void>;
  openWorkflowTree: () => Promise<void>;
  openAgent: (agent: string) => Promise<void>;
  openSubagent: (name: string) => void;
  openFile: (relPath: string) => Promise<void>;
  reset: () => void;
}

async function fetchJsonOrThrow(url: string): Promise<Response> {
  const r = await fetch(url);
  if (!r.ok) {
    let detail = "";
    try {
      detail = ((await r.json()) as { detail?: string })?.detail ?? "";
    } catch {
      // ignore JSON parse error（detail 字段缺失）
    }
    throw new Error(`HTTP ${r.status}${detail ? `: ${detail}` : ""}`);
  }
  return r;
}

// ── inflightSeq gate（防 openWorkflow 并发覆盖）──────────────────────────────────
// 用户快速切 wf-A → wf-B 时，如 wf-A 响应后到，会覆盖 wf-B 的 activeWorkflow → URL 是
// wf-B 但显示 wf-A 内容。模块级递增 seq：入口 ++seq、出口比对 seq !== inflightSeq 丢弃。
// 抄 run-list-store.ts 的 inflightSeq 模式（轻量、单模块变量）。
let inflightSeq = 0;

// ── treeSeq gate（批 G：防 openWorkflowTree 并发覆盖）────────────────────────────
// 照 inflightSeq 范式：openWorkflowTree 入口 ++treeSeq、出口比对，丢弃过期树响应。
let treeSeq = 0;

// ── fileSeq gate（批 H review 修复：防 openFile 并发覆盖）──────────────────────
// 照 treeSeq 范式：openFile 入口 ++fileSeq、出口比对——同 scope 连点两文件时
// （#2 快 #1 慢）旧响应不覆盖新文件（wf/scope/agent 身份快照拦不住「身份全同」
// 的连点场景，须自身 seq）。
let fileSeq = 0;

export const useWorkflowBrowseStore = create<WorkflowBrowseState>((set, get) => ({
  workflows: [],
  workflowsLoading: false,
  workflowsError: null,

  activeWorkflow: null,
  workflowLoading: false,
  workflowError: null,

  activeAgent: null,
  treeScope: "workflow" as const,
  fileTree: null,
  treeLoading: false,
  treeError: null,

  activeFile: null,
  fileLoading: false,
  fileError: null,

  loadWorkflows: async () => {
    set({ workflowsLoading: true, workflowsError: null });
    try {
      const r = await fetchJsonOrThrow("/api/workflows");
      const data = (await r.json()) as WorkflowMeta[];
      set({ workflows: data, workflowsLoading: false });
    } catch (e) {
      set({
        workflowsLoading: false,
        workflowsError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openWorkflow: async (name: string) => {
    // inflightSeq gate 入口：本次 openWorkflow 拿递增序号；响应回来若过期则丢弃。
    const mySeq = ++inflightSeq;
    // 批 H review 修复：同步作废在途 openWorkflowTree——treeSeq 只被 openWorkflowTree
    // 自身 bump，切 wf 不 bump 时旧 wf 在途树的 myTreeSeq === treeSeq 仍成立，旧树
    // 会覆盖新 wf 视图。与 reset() 的作废语义同构（上下文更换即作废一切在途树）；
    // 本函数自身的树写回走 inflightSeq + treeScope 守卫，不查 treeSeq，不受影响。
    // fileSeq 同理（二轮修复）：同名 wf 重开窗口在途文件会绕过 wf 身份快照复活
    // （wfName 恢复同名），须 seq 作废。
    treeSeq += 1;
    fileSeq += 1;
    // m5：进入前同步清空 activeAgent/fileTree/activeFile，防闪现上一 wf 的文件树。
    // review 闭环：同时清空 activeWorkflow——防 loading 期间左栏短暂显示上一 wf 元信息。
    // 批 G：treeScope 复位 "workflow"（落地即全部资产视图）+ treeLoading 复位
    // （detail 失败 early-return 时，上一 wf 在飞的树响应被 inflightSeq 丢弃后
    // 无人再复位 treeLoading，中栏会永转 loading——review 闭环 #2）。
    set({
      workflowLoading: true,
      workflowError: null,
      activeWorkflow: null,
      activeAgent: null,
      treeScope: "workflow",
      fileTree: null,
      treeLoading: false,
      activeFile: null,
      fileError: null,
      treeError: null,
    });
    try {
      const [detailR, agentsR] = await Promise.all([
        fetchJsonOrThrow(`/api/workflows/${encodeURIComponent(name)}`),
        fetchJsonOrThrow(`/api/workflows/${encodeURIComponent(name)}/agents`),
      ]);
      const detailBody = (await detailR.json()) as {
        name: string;
        description: string;
        entry: string;
        inputs_schema: WorkflowDetail["meta"]["inputs_schema"];
        agents_referenced: string[];
        subagents: SubagentSummary[];
      };
      const agentsBody = (await agentsR.json()) as AgentSummary[];
      // inflightSeq gate 出口：期间用户又切到其它 wf → mySeq 已过期 → 丢弃本次响应。
      if (mySeq !== inflightSeq) return;
      set({
        activeWorkflow: {
          meta: {
            name: detailBody.name,
            description: detailBody.description,
            entry: detailBody.entry,
            inputs_schema: detailBody.inputs_schema,
          },
          agents_referenced: detailBody.agents_referenced,
          all_agents: agentsBody,
          subagents: detailBody.subagents ?? [],
        },
        workflowLoading: false,
      });
    } catch (e) {
      if (mySeq !== inflightSeq) return;
      set({
        workflowLoading: false,
        workflowError: e instanceof Error ? e.message : String(e),
      });
      return;
    }
    // 批 G：wf 级全资产树自动加载（scope="workflow" 为默认态，用户落地即见全部资产）。
    // fail-soft 分层：树失败只写 treeError，meta/agents 照常可用。
    set({ treeLoading: true });
    try {
      const treeR = await fetchJsonOrThrow(
        `/api/workflows/${encodeURIComponent(name)}/tree`,
      );
      const treeBody = (await treeR.json()) as TreeResponse;
      // 写回双守卫：期间用户切 wf（inflightSeq 过期）或已点 agent（treeScope 切走）
      // → 丢弃，防慢到的 wf 树覆盖 agent 树。
      if (mySeq !== inflightSeq || get().treeScope !== "workflow") return;
      // treeError: null（批 H 三轮修复）：中途 openWorkflowTree 失败写过 treeError
      // 时，本成功写回不清会让错误横幅残留在已恢复的树上。
      set({ fileTree: treeBody.nodes, treeLoading: false, treeError: null });
    } catch (e) {
      if (mySeq !== inflightSeq || get().treeScope !== "workflow") return;
      set({
        fileTree: null,
        treeLoading: false,
        treeError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openWorkflowTree: async () => {
    // 批 G：切回 wf 级全资产树（「全部资产」入口 / openSubagent 补载共用）。
    // activeAgent 同步清空——scope 切回 workflow 后「当前聚焦 agent」语义失效
    // （中栏 header 回显 wf 名、agent 行取消高亮；openFile 分流不依赖 activeAgent）。
    const wf = get().activeWorkflow;
    if (!wf) return;
    const myTreeSeq = ++treeSeq;
    // 批 H 二轮修复：点「全部资产」清空 activeFile 即上下文更换——作废在途
    // openFile（wf scope 下在途文件的 scope 快照拦不住本入口，treeScope 置回
    // "workflow" 后仍相等）。
    fileSeq += 1;
    set({
      treeScope: "workflow",
      activeAgent: null,
      activeFile: null,
      fileError: null,
      treeLoading: true,
      treeError: null,
    });
    try {
      const r = await fetchJsonOrThrow(
        `/api/workflows/${encodeURIComponent(wf.meta.name)}/tree`,
      );
      const body = (await r.json()) as TreeResponse;
      if (myTreeSeq !== treeSeq) return;
      set({ fileTree: body.nodes, treeLoading: false });
    } catch (e) {
      if (myTreeSeq !== treeSeq) return;
      set({
        fileTree: null,
        treeLoading: false,
        treeError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openAgent: async (agent: string) => {
    const wf = get().activeWorkflow;
    if (!wf) return;
    // 批 G：treeScope 切 "agent"（openFile 分流 + openWorkflow 树写回守卫依赖）。
    // 批 H review 修复（身份快照）：treeScope 单守卫拦不住 ①同 wf 连点两 agent
    // （慢到的旧 agent 树覆盖新 agent 树）②跨 wf 重入（旧 wf 的 agent 树落进新
    // wf 的 agent 视图）——写回前比对 wf 名 + agent 名，与 openFile 守卫范式对齐。
    // 二轮修复：同步作废在途 openWorkflowTree（点 agent 是上下文更换，慢到的 wf
    // 树不得覆盖已落地的 agent 树——与 openWorkflow bump treeSeq 同语义）。
    const wfName = wf.meta.name;
    treeSeq += 1;
    set({
      activeAgent: agent,
      treeScope: "agent",
      activeFile: null,
      fileError: null,
      treeLoading: true,
      treeError: null,
    });
    try {
      const r = await fetchJsonOrThrow(
        `/api/workflows/${encodeURIComponent(wf.meta.name)}/agents/${encodeURIComponent(agent)}/tree`,
      );
      const body = (await r.json()) as TreeResponse;
      // 批 G 对称守卫：期间用户已切回 wf scope（openWorkflowTree）→ 丢弃，
      // 防慢到的 agent 树覆盖 wf 全资产树（与 openWorkflow 树写回守卫互为镜像）。
      // 批 H：wf/agent 身份比对拦「scope 同但上下文已换」的重入窗口。
      if (
        get().treeScope !== "agent" ||
        get().activeWorkflow?.meta.name !== wfName ||
        get().activeAgent !== agent
      ) {
        return;
      }
      set({ fileTree: body.nodes, treeLoading: false });
    } catch (e) {
      if (
        get().treeScope !== "agent" ||
        get().activeWorkflow?.meta.name !== wfName ||
        get().activeAgent !== agent
      ) {
        return;
      }
      set({
        fileTree: null,
        treeLoading: false,
        treeError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openSubagent: (subagent: string) => {
    // 批 G：点 subagent 行 → wf scope 下打开 subagents/<name>.md。
    // 树不在 wf scope 或曾加载失败（fileTree 空）→ 先补载 wf 树（fire-and-forget，
    // openWorkflowTree 的首个 set 同步把 treeScope 置回 "workflow"，随后 openFile
    // 即走 wf 级 URL）。
    const s = get();
    if (s.treeScope !== "workflow" || !s.fileTree) {
      void s.openWorkflowTree();
    }
    void s.openFile(`subagents/${subagent}.md`);
  },

  openFile: async (relPath: string) => {
    const wf = get().activeWorkflow;
    if (!wf) return;
    // 批 G：按 treeScope 分流——"agent" 走 agent resources_root，"workflow" 走 wf root
    // （workflow.yaml / scripts / agents/_xxx_scripts 等不在任何 agent root 下）。
    const agent = get().activeAgent;
    const scopeAtRequest = get().treeScope;
    // wf 身份快照（批 H review 修复）：openWorkflow 切 wf 时复位 treeScope 为
    // "workflow"，scope 快照守卫拦不住跨 wf 场景 → 快照 wf 名，写回前身份比对，
    // 旧 wf 的文件不落进新 wf 视图（成功/失败两分支对称）。
    const wfName = wf.meta.name;
    // fileSeq gate 入口（批 H review 修复）：同 scope 连点两文件（身份全同）时
    // 只有自身 seq 能作废旧响应。
    const myFileSeq = ++fileSeq;
    const base =
      scopeAtRequest === "agent" && agent
        ? `/api/workflows/${encodeURIComponent(wf.meta.name)}/agents/${encodeURIComponent(agent)}/file`
        : `/api/workflows/${encodeURIComponent(wf.meta.name)}/file`;
    set({ fileLoading: true, fileError: null });
    try {
      const r = await fetchJsonOrThrow(
        `${base}?path=${encodeURIComponent(relPath)}`,
      );
      const body = (await r.json()) as FileResponse;
      // scope 快照守卫（review 闭环）：期间用户已切 scope（如点 subagent 文件未达时
      // 点 agent 行）→ 丢弃，防旧 scope 的文件在新 scope 下复活（高亮/内容错位）。
      // wf 身份守卫（批 H）：期间用户切 wf → 丢弃（openWorkflow 入口清空
      // activeWorkflow 后落地新 wf，两态均 ≠ wfName）。agent 身份守卫（批 H）：
      // 期间用户切 agent（openAgent 清空 activeFile）→ 丢弃。fileSeq：连点两文件
      // → 旧响应丢弃。
      if (
        myFileSeq !== fileSeq ||
        get().activeWorkflow?.meta.name !== wfName ||
        get().activeAgent !== agent ||
        get().treeScope !== scopeAtRequest
      ) {
        set({ fileLoading: false });
        return;
      }
      set({ activeFile: body, fileLoading: false });
    } catch (e) {
      if (
        myFileSeq !== fileSeq ||
        get().activeWorkflow?.meta.name !== wfName ||
        get().activeAgent !== agent ||
        get().treeScope !== scopeAtRequest
      ) {
        set({ fileLoading: false });
        return;
      }
      set({
        fileLoading: false,
        fileError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  reset: () => {
    // inflightSeq / treeSeq / fileSeq 自增让任何 inflight openWorkflow /
    // openWorkflowTree / openFile 的写回作废（防 unmount 后到达的 stale 响应覆盖
    // 已清空的 store）。
    inflightSeq += 1;
    treeSeq += 1;
    fileSeq += 1;
    set({
      workflows: [],
      workflowsLoading: false,
      workflowsError: null,
      activeWorkflow: null,
      workflowLoading: false,
      workflowError: null,
      activeAgent: null,
      treeScope: "workflow",
      fileTree: null,
      treeLoading: false,
      treeError: null,
      activeFile: null,
      fileLoading: false,
      fileError: null,
    });
  },
}));
