// components/views/ConversationView.tsx —— 中栏「会话」页签（SPEC §5.3）。
//
// Chunk B：完整渲染（per-EventType §5.3 全表 + 折叠规则 + ▎ 流式光标 + 工具展开 +
// react-window 虚拟化 >500 条）。
//
// 单一数据通道（铁律 1 + 5）：
//   - selectConversation(state, nodeId) → 该 node 的 conversation 事件（seq 升序）
//   - buildEntries(events) → 纯函数折叠为 ConvEntry[]（工具配对 / 成组 / step 附着）
//   - selectStreamingCursor(state, nodeId) → 是否显示 ▎
//   - 用户交互（展开 / 切 charts tab）通过 callback 上传到 RunDetailPage
//
// 虚拟化：events.length > VIRTUALIZATION_THRESHOLD → react-window List（v2 rowHeight
// 支持函数 → 按 entry kind 估高，比固定值精确）。
//
// **fail loud 策略**：编译期 entry.kind 穷尽（TS never 保证）；运行时遇未知 kind（理论
// 不可达——events.ts codegen + buildEntries 不会产出）→ console.warn + 渲染 UnknownEventRow
// 兜底，**不 throw**（保渲染稳定，与 buildEntries 同哲学）。

import { useMemo } from "react";
import { List, type RowComponentProps } from "react-window";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectConversation, selectStreamingCursor } from "@/selectors";
import type { WebEvent } from "@/types/events";
import {
  buildEntries,
  type ConvEntry,
} from "@/components/conversation/entries";
import { PromptRow } from "@/components/conversation/PromptRow";
import { ThinkingBlock } from "@/components/conversation/ThinkingBlock";
import { MessageBlock } from "@/components/conversation/MessageBlock";
import { NodeOutputBlock } from "@/components/conversation/NodeOutputBlock";
import { ToolRow } from "@/components/conversation/ToolRow";
import { ToolGroup } from "@/components/conversation/ToolGroup";
import { ErrorBlock } from "@/components/conversation/ErrorBlock";
import { StatusLine } from "@/components/conversation/StatusLine";
import {
  DialogDivider,
  NodeDivider,
  StepMarker,
} from "@/components/conversation/NodeDivider";
import { DialogMessage } from "@/components/conversation/DialogMessage";
import { CustomRow } from "@/components/conversation/CustomRow";
import { UnknownEventRow } from "@/components/conversation/UnknownEventRow";

/** 超过此阈值启用虚拟化（SPEC §5.3 闭 review #17：>500 条）。 */
const VIRTUALIZATION_THRESHOLD = 500;

interface ConversationViewProps {
  nodeId: string | null;
  /** chart 引用行点击回调（切到 charts tab），由 RunDetailPage 注入。 */
  onChartClick?: () => void;
}

export function ConversationView({
  nodeId,
  onChartClick,
}: ConversationViewProps) {
  const state = useWorkflowStore();
  const group = useMemo(
    () => selectConversation(state, nodeId),
    [state, nodeId]
  );
  const cursorOn = useMemo(
    () => selectStreamingCursor(state, nodeId),
    [state, nodeId]
  );
  const entries = useMemo(() => buildEntries(group.events), [group.events]);

  // ▎ 光标显示位置：仅 entries 数组中**最后一条** message/thinking entry（SPEC §5.3 闭
  // review #4：单 session 最后事件 → 单一光标；多 message 时只在末尾那一条显示）。
  // 倒序找首个 streamable entry 的 index。
  const lastStreamableIdx = useMemo(() => {
    if (!cursorOn) return -1;
    for (let i = entries.length - 1; i >= 0; i--) {
      const k = entries[i].kind;
      if (k === "message" || k === "thinking") return i;
    }
    return -1;
  }, [entries, cursorOn]);

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
  if (entries.length === 0) {
    return (
      <div
        className="p-4 text-sm text-slate-400"
        data-testid="conversation-empty"
      >
        节点 {nodeId} 暂无会话事件。
      </div>
    );
  }

  if (entries.length > VIRTUALIZATION_THRESHOLD) {
    return (
      <div className="h-full" data-testid="conversation-view">
        <List
          rowCount={entries.length}
          rowHeight={(i) => estimateRowHeight(entries[i])}
          rowComponent={VirtualizedRow}
          rowProps={{
            entries,
            lastStreamableIdx,
            onChartClick,
          }}
          overscanCount={8}
          defaultHeight={600}
          className="h-full"
        />
      </div>
    );
  }

  return (
    <div className="p-3 space-y-1.5" data-testid="conversation-view">
      {entries.map((entry, i) => (
        <EntryRenderer
          key={entryKey(entry, i)}
          entry={entry}
          showCursor={i === lastStreamableIdx}
          onChartClick={onChartClick}
        />
      ))}
    </div>
  );
}

// ── entry 渲染：discriminated union 穷尽 switch ─────────────────────────────────

interface EntryRendererProps {
  entry: ConvEntry;
  showCursor: boolean;
  onChartClick?: () => void;
}

export function EntryRenderer({
  entry,
  showCursor,
  onChartClick,
}: EntryRendererProps): React.ReactElement {
  switch (entry.kind) {
    case "prompt":
      return <PromptRow event={entry.event} />;
    case "thinking":
      return (
        <ThinkingBlock
          event={entry.event}
          stepMarker={entry.stepMarker}
          streaming={showCursor}
        />
      );
    case "message":
      return (
        <MessageBlock
          event={entry.event}
          stepMarker={entry.stepMarker}
          showCursor={showCursor}
        />
      );
    case "tool-single":
      return <ToolRow pair={entry.pair} />;
    case "tool-group":
      return <ToolGroup pairs={entry.pairs} />;
    case "dialog-message":
      return <DialogMessage event={entry.event} />;
    case "dialog-divider":
      return <DialogDivider event={entry.event} />;
    case "chart-ref":
      return <CustomRow event={entry.event} onChartClick={onChartClick} />;
    case "custom-generic":
      return <CustomRow event={entry.event} />;
    case "node-divider":
      return <NodeDivider event={entry.event} />;
    case "node-output":
      return <NodeOutputBlock event={entry.event} />;
    case "node-error":
      return <ErrorBlock event={entry.event} />;
    case "status-line":
      return <StatusLine event={entry.event} />;
    case "step-marker":
      return <StepMarker event={entry.event} />;
    case "unknown":
      return <UnknownEventRow event={entry.event} />;
    default: {
      // 编译期：TS never（entry.kind 穷尽）。运行时：理论不可达，但 fail loud 不崩溃——
      // 与 buildEntries 同哲学：warn + UnknownEventRow 兜底（保渲染稳定）。
      const _exhaustive: never = entry;
      const kind = (_exhaustive as ConvEntry).kind;
      console.warn(`[orca] ConversationView 未渲染 entry kind=${String(kind)}`);
      const fallbackEvent: WebEvent = {
        seq: -1,
        type: "unknown_event",
        timestamp: 0,
        node: null,
        session_id: null,
        data: { source: "frontend", raw: { kind } },
      };
      return <UnknownEventRow event={fallbackEvent} />;
    }
  }
}

/** entry 稳定 key：buildEntries 内 call/result 已带 seq；group 用首 pair seq。 */
function entryKey(entry: ConvEntry, index: number): string {
  if (entry.kind === "tool-single" || entry.kind === "tool-group") {
    const pairs = entry.kind === "tool-single" ? [entry.pair] : entry.pairs;
    const firstSeq = pairs[0]?.call?.seq ?? pairs[0]?.result?.seq;
    if (firstSeq != null) return `t-${firstSeq}`;
    return `t-idx-${index}`;
  }
  const evt = (entry as { event?: WebEvent }).event;
  if (evt?.seq != null) return `e-${evt.seq}`;
  return `idx-${index}`;
}

/** 虚拟化行高估计（按 entry kind）。 */
function estimateRowHeight(entry: ConvEntry): number {
  switch (entry.kind) {
    case "message":
      // markdown 渲染高度变化大；保守给 160px（多数 message 单屏可见）。
      return 160;
    case "node-output":
      // output 可能是 markdown（同 message）/ JSON pre，保守给 160px（同 message）。
      return 160;
    case "thinking":
    case "tool-single":
    case "tool-group":
      return 64;
    case "prompt":
    case "dialog-message":
      return 80;
    case "node-error":
      return 96;
    case "node-divider":
    case "dialog-divider":
    case "step-marker":
    case "status-line":
      return 32;
    case "chart-ref":
    case "custom-generic":
    case "unknown":
      return 28;
    default:
      return 64;
  }
}

// ── 虚拟化 row（react-window v2）──
interface VirtualizedRowData {
  entries: ConvEntry[];
  lastStreamableIdx: number;
  onChartClick?: () => void;
}

function VirtualizedRow({
  index,
  style,
  entries,
  lastStreamableIdx,
  onChartClick,
}: RowComponentProps<VirtualizedRowData>): React.ReactElement {
  const entry = entries[index];
  return (
    <div style={style} data-testid={`conv-vrow-${index}`}>
      <EntryRenderer
        entry={entry}
        showCursor={index === lastStreamableIdx}
        onChartClick={onChartClick}
      />
    </div>
  );
}

// 暴露 buildEntries 供单测（保 entry 序列化检查 oracle）
export { buildEntries };
