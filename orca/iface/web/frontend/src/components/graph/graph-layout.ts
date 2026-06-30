// components/graph/graph-layout.ts —— dagre TB 自动布局 + 回环边识别（SPEC §1.4，抄 Conductor）。
//
// 回环边处理（铁律 4）：
//   dagre 默认把回环边（如 nas.yaml reviewer→optimizer）当普通边，导致节点排名混乱（节点
//   被排到祖先上方）。Conductor 的解法：DFS 识别 back edges（指向栈上祖先的边），把这些边
//   **反向喂给 dagre**（让它正确排名正向 DAG），**渲染时保持原方向**（画成弧形）。
//
// 纯函数模块（无 React 依赖），便于 graph.test.tsx 直接断言布局结果。

import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { NodeKind, WorkflowTopology } from "@/types/topology";
import { END_NODE_ID } from "./constants";

/** ReactFlow 节点 data 形状（widget 渲染读）。 */
export interface WorkflowNodeData {
  name: string;
  kind: NodeKind | "end" | "parallel-group";
  status?: string;
  /** parallel/foreach 进度（"done/total"），widget 读。 */
  progress?: string;
  /** parallel 组的分支节点名（仅 parallel-group 用）。 */
  branches?: string[];
  output?: unknown;
  [key: string]: unknown;
}

export type WorkflowFlowNode = Node<WorkflowNodeData>;
export type WorkflowFlowEdge = Edge;

/** dagre 布局参数。 */
const NODE_WIDTH = 200;
const NODE_HEIGHT = 60;
const RANK_SEP = 80; // 层间距
const NODE_SEP = 40; // 同层节点间距

/**
 * DFS 识别回环边（SPEC §1.4 / 抄 Conductor findBackEdges）。
 *
 * 回环边 = 指向「当前 DFS 栈上的祖先」的边。这些边会让 dagre 误把下游排到上游上方，
 * 故需识别后反向喂图。普通 DAG 无回环边，返回空集。
 *
 * @returns 回环边的集合，key=`${from}->${to}`，用于 applyDagreLayout 反向喂 dagre。
 */
export function findBackEdges(topology: WorkflowTopology): Set<string> {
  // 构建邻接表（from → [to,...]）。parallel 组的 branches 也是「组→分支」边。
  const adj = new Map<string, string[]>();
  const allNodes = new Set<string>();
  topology.nodes.forEach((n) => allNodes.add(n.name));
  topology.parallel.forEach((g) => {
    allNodes.add(g.name);
    g.branches.forEach((b) => allNodes.add(b));
  });

  const addEdge = (from: string, to: string) => {
    if (!adj.has(from)) adj.set(from, []);
    adj.get(from)!.push(to);
  };
  topology.routes.forEach((r) => addEdge(r.from, r.to));
  topology.parallel.forEach((g) => g.branches.forEach((b) => addEdge(g.name, b)));

  const backEdges = new Set<string>();
  const WHITE = 0,
    GRAY = 1,
    BLACK = 2;
  const color = new Map<string, number>();
  allNodes.forEach((n) => color.set(n, WHITE));

  const visit = (u: string, stack: string[]) => {
    color.set(u, GRAY);
    stack.push(u);
    for (const v of adj.get(u) ?? []) {
      if (color.get(v) === GRAY) {
        // 指向栈上祖先 → 回环边
        backEdges.add(`${u}->${v}`);
      } else if (color.get(v) === WHITE) {
        visit(v, stack);
      }
    }
    stack.pop();
    color.set(u, BLACK);
  };

  // 从每个白节点出发（图可能不连通）
  for (const n of allNodes) {
    if (color.get(n) === WHITE) visit(n, []);
  }
  return backEdges;
}

/** key 工具：构造边的唯一 id（渲染 + 反查）。 */
function edgeKey(from: string, to: string): string {
  return `e-${from}-${to}`;
}

/**
 * 把 WorkflowTopology（来自 workflow_started.data.topology）转成 ReactFlow nodes+edges，
 * 并用 dagre 计算 TB 布局坐标（回环边反向喂图）。
 *
 * @returns { nodes, edges } —— ReactFlow 可直接渲染的元素。
 */
export function applyDagreLayout(
  topology: WorkflowTopology
): { nodes: WorkflowFlowNode[]; edges: WorkflowFlowEdge[] } {
  const backEdges = findBackEdges(topology);

  // ── 构造 dagre 图（回环边反向）──
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", ranksep: RANK_SEP, nodesep: NODE_SEP, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));

  // 注册节点（含 $end 哨兵）
  const nodeSet = new Set<string>();
  topology.nodes.forEach((n) => nodeSet.add(n.name));
  topology.parallel.forEach((g2) => {
    nodeSet.add(g2.name);
    g2.branches.forEach((b) => nodeSet.add(b));
  });
  // 任何 route.to 引用的节点（如 $end）也注册
  topology.routes.forEach((r) => nodeSet.add(r.to));

  nodeSet.forEach((id) =>
    g.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT })
  );

  // 注册边（回环边反向，让 dagre 正确排名正向 DAG）
  topology.routes.forEach((r) => {
    const key = `${r.from}->${r.to}`;
    if (backEdges.has(key)) {
      g.setEdge(r.to, r.from); // 反向
    } else {
      g.setEdge(r.from, r.to);
    }
  });
  // parallel 组 → branches（组→分支，永远正向；这些不是回环边）
  topology.parallel.forEach((grp) => {
    grp.branches.forEach((b) => g.setEdge(grp.name, b));
  });

  dagre.layout(g);

  // ── 生成 ReactFlow nodes ──
  const nodeKindMap = new Map<string, NodeKind | "parallel-group" | "end">(
    topology.nodes.map((n) => [n.name, n.kind])
  );
  topology.parallel.forEach((grp) => nodeKindMap.set(grp.name, "parallel-group"));
  const branchesMap = new Map<string, string[]>(
    topology.parallel.map((g) => [g.name, g.branches])
  );

  const nodes: WorkflowFlowNode[] = [];
  g.nodes().forEach((id) => {
    const gn = g.node(id);
    if (!gn) return;
    let kind = nodeKindMap.get(id);
    if (id === END_NODE_ID) kind = "end";
    const data: WorkflowNodeData = {
      name: id,
      kind: kind ?? "end",
      status: "pending",
      ...(branchesMap.has(id) ? { branches: branchesMap.get(id) } : {}),
    };
    nodes.push({
      id,
      type: kindToNodeType(kind ?? "end"),
      position: { x: gn.x - NODE_WIDTH / 2, y: gn.y - NODE_HEIGHT / 2 },
      data,
      // dagre 算好的位置，禁止 ReactFlow 拖动改变（避免布局被用户拖乱）
      draggable: true,
    });
  });

  // ── 生成 ReactFlow edges（渲染保持原方向；回环边标 animated 弧形）──
  const edges: WorkflowFlowEdge[] = [];
  topology.routes.forEach((r) => {
    const key = `${r.from}->${r.to}`;
    const isBack = backEdges.has(key);
    edges.push({
      id: edgeKey(r.from, r.to),
      source: r.from,
      target: r.to,
      type: isBack ? "animated-back" : "animated",
      data: { isBackEdge: isBack, taken: false },
    });
  });
  // parallel 组 → branches 边（无 route 语义，普通连线）
  topology.parallel.forEach((grp) => {
    grp.branches.forEach((b) => {
      edges.push({
        id: edgeKey(grp.name, b),
        source: grp.name,
        target: b,
        type: "animated",
        data: { isBackEdge: false, taken: false, isParallel: true },
      });
    });
  });

  return { nodes, edges };
}

/** kind → ReactFlow node type（注册名，对齐 NODE_TYPES map）。 */
export function kindToNodeType(
  kind: NodeKind | "parallel-group" | "end"
): string {
  switch (kind) {
    case "agent":
      return "agent";
    case "script":
      return "script";
    case "set":
      return "set";
    case "foreach":
      return "foreach";
    case "parallel-group":
      return "parallel";
    case "end":
      return "end";
    default:
      return "script";
  }
}

/**
 * 增量更新节点 data（铁律 5：不全量 rebuild elements）。
 *
 * 给定当前 flowNodes + 派生 nodes 状态，返回**只更新变化节点 data** 的新数组。
 * 未变化的节点保持原对象引用（React.memo 跳过重渲染）。
 *
 * 透传 status / output / progress（progress 给 foreach/parallel widget，SPEC §1.2）。
 */
export function mergeNodeStatus(
  flowNodes: WorkflowFlowNode[],
  nodeStatus: Record<string, { status?: string; output?: unknown; progress?: string }>
): WorkflowFlowNode[] {
  let changed = false;
  const next = flowNodes.map((n) => {
    const s = nodeStatus[n.id];
    if (!s || s.status === undefined) return n; // 无状态，保持原引用
    // 未变判定：status / progress / output 都没变才保持原引用（增量更新核心）
    const d = n.data as WorkflowNodeData;
    if (d.status === s.status && d.progress === s.progress && d.output === s.output) {
      return n;
    }
    changed = true;
    return {
      ...n,
      data: { ...n.data, status: s.status, output: s.output, progress: s.progress },
    };
  });
  return changed ? next : flowNodes; // 全未变 → 返回原数组（零重渲染）
}

/**
 * 增量标记 route_taken 走过的边（铁律 5）。
 *
 * @param routeEdges 走过的边集合 key=`${from}->${to}`
 */
export function markTakenEdges(
  flowEdges: WorkflowFlowEdge[],
  takenKeys: Set<string>
): WorkflowFlowEdge[] {
  let changed = false;
  const next = flowEdges.map((e) => {
    const key = `${e.source}->${e.target}`;
    const taken = takenKeys.has(key);
    const prevTaken = (e.data as { taken?: boolean })?.taken ?? false;
    if (taken === prevTaken) return e;
    changed = true;
    return { ...e, data: { ...(e.data as object), taken } };
  });
  return changed ? next : flowEdges;
}
