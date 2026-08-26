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
// 设计：plain zustand（无 immer），照 run-list-store.ts。模块单例 + unmount reset。

import { create } from "zustand";

export interface WorkflowMeta {
  name: string;
  description: string;
  entry: string;
  inputs_count: number;
  inputs_schema: Array<{ name: string; type: string; description: string }>;
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

export interface TreeResponse {
  agent: string;
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
  fileTree: TreeNode[] | null;
  treeLoading: boolean;
  treeError: string | null;

  activeFile: FileResponse | null;
  fileLoading: boolean;
  fileError: string | null;

  loadWorkflows: () => Promise<void>;
  openWorkflow: (name: string) => Promise<void>;
  openAgent: (agent: string) => Promise<void>;
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

export const useWorkflowBrowseStore = create<WorkflowBrowseState>((set, get) => ({
  workflows: [],
  workflowsLoading: false,
  workflowsError: null,

  activeWorkflow: null,
  workflowLoading: false,
  workflowError: null,

  activeAgent: null,
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
    // m5：进入前同步清空 activeAgent/fileTree/activeFile，防闪现上一 wf 的文件树。
    // review 闭环：同时清空 activeWorkflow——防 loading 期间左栏短暂显示上一 wf 元信息。
    set({
      workflowLoading: true,
      workflowError: null,
      activeWorkflow: null,
      activeAgent: null,
      fileTree: null,
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
        },
        workflowLoading: false,
      });
    } catch (e) {
      if (mySeq !== inflightSeq) return;
      set({
        workflowLoading: false,
        workflowError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openAgent: async (agent: string) => {
    const wf = get().activeWorkflow;
    if (!wf) return;
    set({
      activeAgent: agent,
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
      set({ fileTree: body.nodes, treeLoading: false });
    } catch (e) {
      set({
        fileTree: null,
        treeLoading: false,
        treeError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  openFile: async (relPath: string) => {
    const wf = get().activeWorkflow;
    const agent = get().activeAgent;
    if (!wf || !agent) return;
    set({ fileLoading: true, fileError: null });
    try {
      const r = await fetchJsonOrThrow(
        `/api/workflows/${encodeURIComponent(wf.meta.name)}/agents/${encodeURIComponent(agent)}/file?path=${encodeURIComponent(relPath)}`,
      );
      const body = (await r.json()) as FileResponse;
      set({ activeFile: body, fileLoading: false });
    } catch (e) {
      set({
        fileLoading: false,
        fileError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  reset: () => {
    // inflightSeq 自增让任何 inflight openWorkflow 的写回作废（防 unmount 后到达的
    // stale 响应覆盖已清空的 store）。
    inflightSeq += 1;
    set({
      workflows: [],
      workflowsLoading: false,
      workflowsError: null,
      activeWorkflow: null,
      workflowLoading: false,
      workflowError: null,
      activeAgent: null,
      fileTree: null,
      treeLoading: false,
      treeError: null,
      activeFile: null,
      fileLoading: false,
      fileError: null,
    });
  },
}));
