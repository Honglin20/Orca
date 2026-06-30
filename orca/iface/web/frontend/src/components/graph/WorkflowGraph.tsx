// components/graph/WorkflowGraph.tsx —— DAG 主组件（SPEC §1.3，抄 Conductor 双 effect 增量）。
//
// 五条铁律对应：
//   - **铁律 1（live/replay 同 fold）**：本组件只读 store 派生态（workflowDef/nodes），不
//     自己 fold 事件。状态来自 store 的唯一 handler 表。
//   - **铁律 5（DAG 增量更新）**：双 effect —— Effect 1 拓扑首次出现/变化才全量 build +
//     dagre 布局；Effect 2 节点状态变化只更新对应 node 的 data（mergeNodeStatus 保持未变
//     节点原引用，React.memo 跳过重渲染）。不全量 rebuild elements。
//
// 拓扑来源：store.workflowDef（由 workflow_started handler 从 data.topology 提取）。

import { useEffect, useMemo, useState } from "react";
import { ReactFlow, ReactFlowProvider, Background, Controls } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  applyDagreLayout,
  markTakenEdges,
  mergeNodeStatus,
  type WorkflowFlowEdge,
  type WorkflowFlowNode,
} from "./graph-layout";
import { AgentNodeWidget } from "./nodes/AgentNodeWidget";
import { ScriptNodeWidget } from "./nodes/ScriptNodeWidget";
import { SetNodeWidget } from "./nodes/SetNodeWidget";
import { ForeachGroupWidget } from "./nodes/ForeachGroupWidget";
import { EndNodeWidget } from "./nodes/EndNodeWidget";
import { AnimatedEdge } from "./AnimatedEdge";

// kind → widget 注册表（ReactFlow nodeTypes，按 kind 分派，SPEC §1.1）
const NODE_TYPES = {
  agent: AgentNodeWidget,
  script: ScriptNodeWidget,
  set: SetNodeWidget,
  foreach: ForeachGroupWidget,
  parallel: ForeachGroupWidget, // parallel 组复用 foreach widget（branches/progress 语义同）
  end: EndNodeWidget,
};

const EDGE_TYPES = {
  animated: AnimatedEdge,
  "animated-back": AnimatedEdge, // 回环边复用 AnimatedEdge（内部按 data.isBackEdge 区分）
};

function WorkflowGraphInner() {
  const workflowDef = useWorkflowStore((s) => s.workflowDef);
  const nodes = useWorkflowStore((s) => s.nodes);
  const events = useWorkflowStore((s) => s.events);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const replayPosition = useWorkflowStore((s) => s.replayPosition);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);

  const [flowNodes, setFlowNodes] = useState<WorkflowFlowNode[]>([]);
  const [flowEdges, setFlowEdges] = useState<WorkflowFlowEdge[]>([]);

  // Effect 1: 拓扑首次出现 / workflow 变化 → 全量 build + dagre 布局（SPEC §1.3）
  useEffect(() => {
    if (!workflowDef) {
      setFlowNodes([]);
      setFlowEdges([]);
      return;
    }
    const { nodes: laid, edges } = applyDagreLayout(workflowDef);
    setFlowNodes(laid);
    setFlowEdges(edges);
  }, [workflowDef]);

  // Effect 2: 节点状态变化 → 只更新该节点 data（铁律 5，不全量 rebuild）
  useEffect(() => {
    setFlowNodes((prev) => mergeNodeStatus(prev, nodes));
  }, [nodes]);

  // Effect 3: route_taken 走过的边高亮（增量标记，不全量 rebuild）
  // replay 模式只看 events[0..replayPosition]，live 看全部
  const takenEdgeKeys = useMemo(() => {
    const end = replayMode ? replayPosition + 1 : events.length;
    const keys = new Set<string>();
    for (let i = 0; i < end && i < events.length; i++) {
      const e = events[i];
      if (e.type === "route_taken") {
        const from = String(e.data?.from ?? "");
        const to = String(e.data?.to ?? "");
        if (from && to) keys.add(`${from}->${to}`);
      }
    }
    return keys;
  }, [events, replayMode, replayPosition]);

  useEffect(() => {
    setFlowEdges((prev) => markTakenEdges(prev, takenEdgeKeys));
  }, [takenEdgeKeys]);

  if (!workflowDef) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-slate-400">
        等待 workflow_started 事件以获取拓扑…
      </div>
    );
  }

  return (
    <ReactFlow
      nodes={flowNodes}
      edges={flowEdges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      onNodeClick={(_, n) => setSelectedNode(n.id)}
      fitView
      proOptions={{ hideAttribution: true }}
      data-testid="workflow-graph"
    >
      <Background />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}

/** 带 Provider 的导出组件（ReactFlow 要求 Provider 包裹）。 */
export function WorkflowGraph() {
  return (
    <ReactFlowProvider>
      <WorkflowGraphInner />
    </ReactFlowProvider>
  );
}
