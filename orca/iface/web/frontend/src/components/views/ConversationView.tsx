// components/views/ConversationView.tsx —— 中栏「会话」页签（SPEC §5.3）。
//
// Chunk A：占位实现——渲染 selectConversation 输出的事件列表（type+seq 一行），
// 证明 D2 按 node 分组的 selector 工作。完整 markdown / 折叠 / ▎ 流式光标 / 虚拟化
// 留给后续 chunk（SPEC §5.3 全表）。

import { useMemo } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectConversation } from "@/selectors";

export function ConversationView({ nodeId }: { nodeId: string | null }) {
  const state = useWorkflowStore();
  const group = useMemo(() => selectConversation(state, nodeId), [state, nodeId]);

  if (!nodeId) {
    return (
      <div
        className="p-4 text-sm text-slate-400"
        data-testid="conversation-empty"
      >
        请在左栏选择一个 agent 查看会话。
      </div>
    );
  }
  if (group.events.length === 0) {
    return (
      <div
        className="p-4 text-sm text-slate-400"
        data-testid="conversation-empty"
      >
        节点 {nodeId} 暂无会话事件。
      </div>
    );
  }
  return (
    <div className="p-3" data-testid="conversation-view">
      <ul className="space-y-1 font-mono text-xs">
        {group.events.map((e) => (
          <li key={`${e.seq}-${e.type}`} data-testid={`conv-row-${e.seq}`}>
            <span className="text-slate-400">{e.seq}</span>{" "}
            <span className="text-slate-600">{e.type}</span>{" "}
            <span className="text-slate-800">
              {String(e.data?.text ?? e.data?.tool ?? e.data?.message ?? "").slice(0, 60)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
