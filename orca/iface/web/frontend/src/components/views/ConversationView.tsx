// components/views/ConversationView.tsx —— 中栏「会话」页签（SPEC §5.3）。
//
// Chunk B：完整渲染（per-EventType §5.3 全表 + 折叠规则 + ▎ 流式光标 + 工具展开 +
// react-window 虚拟化 >500 条）。
//
// **P2（web-presentation-refinement §P2 方案 1）**：顶部子 agent 会话选择器
// （``All(N) | main(M) | ses_090e(208) | …``）—— 切 session → setSelectedSession →
// selectConversation(state, nodeId, selectedSession) 只 buildEntries 该 session 事件
// （~208 vs 4224，缓解症状 #3/#5）。sessions 数据来源 = nodesIndex 倒排索引（O(sessions)）。
//
// 单一数据通道（铁律 1 + 5）：
//   - selectNodeSessions(state, nodeId) → 会话行（从 nodesIndex 派生）
//   - selectConversation(state, nodeId, selectedSession) → 该 (node, session) 事件
//   - buildEntries(events) → 纯函数折叠为 ConvEntry[]
//   - selectStreamingCursor(state, nodeId) → 是否显示 ▎
//   - 用户交互（展开 / 切 session / 切 charts tab）通过 callback 上传
//
// 虚拟化：events.length > VIRTUALIZATION_THRESHOLD → react-window List（v2 rowHeight
// 支持函数 → 按 entry kind 估高，比固定值精确）。
//
// **fail loud 策略**：编译期 entry.kind 穷尽（TS never 保证）；运行时遇未知 kind（理论
// 不可达——events.ts codegen + buildEntries 不会产出）→ console.warn + 渲染 UnknownEventRow
// 兜底，**不 throw**（保渲染稳定，与 buildEntries 同哲学）。

import { memo, useMemo, useRef } from "react";
import {
  List,
  useDynamicRowHeight,
  type RowComponentProps,
} from "react-window";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  conversationEventsFromIndex,
  sessionsFromIndex,
  streamingCursorFromIndex,
} from "@/selectors";
import type { WebEvent } from "@/types/events";
import {
  buildEntries,
  type ConvEntry,
} from "@/components/conversation/entries";
import { reuseEntries } from "@/components/conversation/reuse-entries";
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

/** 超过此阈值启用虚拟化（SPEC 2026-08-28 C5.1：500 → 100——固定估高失真 + ≤500 全量渲染过慢）。 */
const VIRTUALIZATION_THRESHOLD = 100;

interface ConversationViewProps {
  nodeId: string | null;
  /** chart 引用行点击回调（切到 charts tab），由 RunDetailPage 注入（useCallback 稳定，C4.2）。 */
  onChartClick?: () => void;
}

export function ConversationView({
  nodeId,
  onChartClick,
}: ConversationViewProps) {
  // 细粒度订阅（SPEC 2026-08-28 C4.1，取消全 store 订阅——每条 WS 事件曾触发本组件全量
  // 重渲染 + O(N) selector 重算）。nodesIndex[nodeId]：immer copy-on-write 下兄弟 node 的
  // 事件不改变本引用（仅 in-order 增量路径；refold 整体重建属预期）——这是收窄的核心收益。
  // **禁止** selector 返回新建对象（zustand v5 引用不等 → 无限渲染），此处全部返回既有引用。
  const nodeIndex = useWorkflowStore((s) => (nodeId ? s.nodesIndex[nodeId] : undefined));
  const status = useWorkflowStore((s) => s.status);
  const selectedSession = useWorkflowStore((s) => s.selectedSession);
  const setSelectedSession = useWorkflowStore((s) => s.setSelectedSession);
  const activeRunId = useWorkflowStore((s) => s.activeRunId);

  const sessions = useMemo(() => sessionsFromIndex(nodeIndex), [nodeIndex]);
  // 总事件数 = 各 session count 之和（用于 "All(N)" 标签 DRY，不二次 filter events）
  const totalEventCount = useMemo(
    () => sessions.reduce((sum, s) => sum + s.eventCount, 0),
    [sessions]
  );

  const events = useMemo(
    () => conversationEventsFromIndex(nodeIndex, selectedSession ?? undefined),
    [nodeIndex, selectedSession]
  );
  const cursorOn = useMemo(
    () => streamingCursorFromIndex(status, nodeIndex),
    [status, nodeIndex]
  );

  // C4.3：buildEntries 产物经 reuseEntries 复用 prev entry 引用（append-only 前缀等价）
  // → EntryRenderer memo 对未变行跳过重渲染；全复用时数组引用也稳定。
  const prevEntriesRef = useRef<ConvEntry[]>([]);
  const entries = useMemo(() => {
    const built = buildEntries(events);
    const reused = reuseEntries(prevEntriesRef.current, built, entryKey);
    prevEntriesRef.current = reused;
    return reused;
  }, [events]);

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

  // C5.2：动态行高（ResizeObserver 自动测量；key 变化清缓存 + <List key> 同 key 重挂，
  // 防上下文切换滚动偏移残留）。hook 必须置于早 return 之前（hook 顺序不变式）。
  const vkey = `${activeRunId ?? ""}:${nodeId ?? ""}:${selectedSession ?? ""}`;
  const rowHeight = useDynamicRowHeight({ defaultRowHeight: 96, key: vkey });

  if (!nodeId) {
    return (
      <div
        className="p-4 text-sm orca-text-faint"
        data-testid="conversation-empty"
      >
        请在左栏选择一个 agent 查看会话。
      </div>
    );
  }

  // P2 §方案 1：仅 ≥2 session 时显示会话选择器（1 session 等价 All，省 UI）
  const showSessionTabs = sessions.length > 1;

  if (entries.length === 0) {
    return (
      <div className="h-full" data-testid="conversation-view">
        {showSessionTabs && (
          <SessionTabs
            nodeId={nodeId}
            sessions={sessions}
            totalEventCount={totalEventCount}
            selectedSession={selectedSession}
            onSelect={setSelectedSession}
          />
        )}
        <div
          className="p-4 text-sm orca-text-faint"
          data-testid="conversation-empty"
        >
          节点 {nodeId}
          {selectedSession && selectedSession !== "all"
            ? ` session ${selectedSession}`
            : ""}
          {" "}暂无会话事件。
        </div>
      </div>
    );
  }

  if (entries.length > VIRTUALIZATION_THRESHOLD) {
    return (
      <div className="flex h-full flex-col" data-testid="conversation-view">
        {showSessionTabs && (
          <SessionTabs
            nodeId={nodeId}
            sessions={sessions}
            totalEventCount={totalEventCount}
            selectedSession={selectedSession}
            onSelect={setSelectedSession}
          />
        )}
        <div className="flex-1 overflow-hidden">
          <List
            key={vkey}
            rowCount={entries.length}
            rowHeight={rowHeight}
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
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col" data-testid="conversation-view">
      {showSessionTabs && (
        <SessionTabs
          nodeId={nodeId}
          sessions={sessions}
          totalEventCount={totalEventCount}
          selectedSession={selectedSession}
          onSelect={setSelectedSession}
        />
      )}
      <div className="flex-1 overflow-auto p-3 space-y-1.5">
        {entries.map((entry, i) => (
          <EntryRenderer
            key={entryKey(entry, i)}
            entry={entry}
            showCursor={i === lastStreamableIdx}
            onChartClick={onChartClick}
          />
        ))}
      </div>
    </div>
  );
}

// ── SessionTabs：子 agent 会话选择器（P2 §方案 1）──────────────────────────────
// 渲染 ``All(N) | main(M) | ses_090e(208) | ses_09121e(199) | …``，点选切 selectedSession。
// 顺序 = sessions 数组顺序（store.nodesIndex 维护 = 首事件 seq 升序，稳定）。
// testid：``session-tab-all`` / ``session-tab-main`` / ``session-tab-<sessionId>``。
interface SessionTabsProps {
  nodeId: string;
  sessions: { sessionId: string; label: string; eventCount: number }[];
  totalEventCount: number;
  selectedSession: string | "all" | null;
  onSelect: (sid: string | "all" | null) => void;
}

function SessionTabs({
  nodeId,
  sessions,
  totalEventCount,
  selectedSession,
  onSelect,
}: SessionTabsProps) {
  return (
    <div
      className="flex flex-wrap items-center gap-1 border-b orca-border orca-bg-surface-2 px-2 py-1.5"
      data-testid={`session-tabs-${nodeId}`}
    >
      <button
        type="button"
        onClick={() => onSelect("all")}
        data-testid="session-tab-all"
        className={`rounded px-2 py-0.5 font-mono text-xs ${
          selectedSession === "all"
            // 选中态：accent 实色填充 + 白字（明暗 token 都成立；
            // 原 bg-slate-900 在暗模式 ≈ app-bg，选中与未选中几乎无区分）。
            ? "orca-bg-accent text-white"
            : "orca-text-muted hover:orca-bg-surface"
        }`}
      >
        All({totalEventCount})
      </button>
      {sessions.map((s) => (
        <button
          key={s.sessionId}
          type="button"
          onClick={() => onSelect(s.sessionId)}
          data-testid={`session-tab-${s.sessionId}`}
          className={`rounded px-2 py-0.5 font-mono text-xs ${
            selectedSession === s.sessionId
              ? "orca-bg-accent text-white"
              : "orca-text-muted hover:orca-bg-surface"
          }`}
        >
          {s.label}({s.eventCount})
        </button>
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

/** C4.2：memo——entry/showCursor/onChartClick 引用不变 → 跳过重渲染（依赖 reuseEntries
 * 的引用复用 + RunDetailPage 的 onChartClick useCallback；VirtualizedRow 因 index/style
 * 每帧变**不可** memo，memo 只放本组件）。 */
export const EntryRenderer = memo(function EntryRenderer({
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
});

/** entry 稳定 key：buildEntries 内 call/result 已带 seq；group 用首 pair seq。
 * 导出供 reuseEntries 单测同源复用（C4.3）。 */
export function entryKey(entry: ConvEntry, index: number): string {
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
