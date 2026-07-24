// components/detail/LogStream.tsx —— 流式日志尾，live，虚拟化（SPEC §5.5）。
//
// 三约束（SPEC §5.5）：
//   1. **虚拟滚动**（react-window v2 ``List`` + rowComponent）：1000+ 条不卡。
//   2. **降噪过滤**（SPEC web-presentation-refinement §P1）：行数 == 经 classifyLogLevel 过滤后的事件数
//      （过程事件 agent_*/foreach_item_*/prompt_rendered/agent_usage/custom/dialog_message/
//      unknown_event 归 ConversationView；route_taken 默认隐藏）；每行 ``{seq}·{type}·{摘要≤80}``。
//   3. **auto-scroll 策略**（闭 review #36 / SPEC §0 D6）：用户上滚→暂停 auto-scroll +
//      显示「跳最新」按钮；pinned-to-bottom→新事件到达 ``scrollToRow`` 到末 seq。
//
// pinned 状态机（最小可预测）：
//   - 初始：pinned=true（新事件到达 → scrollToRow 末行）
//   - wheel 上滚：pinned=false（新事件到达 → 显示「跳最新」按钮）
//   - 点「跳最新」按钮：pinned=true（滚回末行 + 清 pendingJump）
//
// 不通过 ``onRowsRendered`` 自动恢复 pinned：在「事件少、全部可见」的常见场景下，
// stopIndex 总是末行——自动恢复会让 wheel 上滚立即被覆盖。用户用按钮显式表达
// 「我要回底部」是更明确的语义（HIG 原则：predictable over magic）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  List,
  useListRef,
  type RowComponentProps,
} from "react-window";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectLog, type LogLevel, type LogLine } from "@/selectors";

interface LogRowData {
  items: LogLine[];
}

/**
 * LogLevel → tailwind 文字色（SPEC §P1：克制，与既有 ErrorBlock/AgentsRail 配色对齐）。
 * Record<LogLevel,_> 编译期穷尽 5 级；新增 level → 编译失败。
 *
 * P0b 白名单：LogLevel 与 NodeStatus 不是 1:1（info/warning 无对应 node status），
 * 故保留为独立真相源（plan §P0b）；_inline chrome_（seq/type 数字、jump-latest 按钮、
 * live badge）则走 token。
 */
const LEVEL_TEXT_COLOR: Record<LogLevel, string> = {
  error: "text-red-600",     // 与 ErrorBlock / AgentsRail failed 同色
  success: "text-emerald-600", // 与 AgentsRail done / TopBar completed 同色
  warning: "text-amber-600", // 与 AgentsRail blocked 同色
  debug: "text-slate-400",   // 与 seq 数字同色（muted）
  info: "text-slate-700",    // 默认正文色
};

function LogRow({
  index,
  style,
  items,
}: RowComponentProps<LogRowData>): React.ReactElement {
  const item = items[index];
  return (
    <div
      style={style}
      className={`flex items-center whitespace-nowrap px-2 font-mono text-xs ${
        LEVEL_TEXT_COLOR[item.level]
      }`}
      data-testid={`log-row-${index}`}
    >
      <span className="orca-text-faint">{item.seq}</span>
      <span className="ml-2 orca-text-faint">{item.type}</span>
      <span className="ml-2">{item.text}</span>
    </div>
  );
}

export function LogStream() {
  const state = useWorkflowStore();
  const lines = useMemo(() => selectLog(state), [state]);

  const [pinned, setPinned] = useState(true);
  // pendingJump：用户上滚时若新事件到达，记录待跳 index（按钮提示）；用户点跳最新→清。
  const [pendingJump, setPendingJump] = useState<number | null>(null);
  const listRef = useListRef(null);
  // 上次处理的 lines.length，用于「新事件到达」effect 判定。用 ref 持有，避免入 effect 依赖。
  const prevCountRef = useRef(0);

  // 新事件到达：pinned → scrollToRow 末尾；非 pinned → 显示「跳最新」按钮（记下末 index）。
  useEffect(() => {
    if (lines.length === 0) {
      prevCountRef.current = 0;
      return;
    }
    if (lines.length === prevCountRef.current) return;
    const lastIndex = lines.length - 1;
    if (pinned) {
      // pinned：滚到最新行（``end`` 对齐底部）。
      listRef.current?.scrollToRow({ index: lastIndex, align: "end" });
      setPendingJump(null);
    } else {
      setPendingJump(lastIndex);
    }
    prevCountRef.current = lines.length;
  }, [lines.length, pinned, listRef]);

  const jumpToLatest = useCallback(() => {
    setPinned(true);
    const lastIndex = lines.length - 1;
    if (lastIndex >= 0) {
      listRef.current?.scrollToRow({ index: lastIndex, align: "end" });
    }
    setPendingJump(null);
  }, [lines.length, listRef]);

  // wheel 上滚 → 取消 pinned（显示「跳最新」按钮）。
  // 注：react-window 的 wheel 滚动正常冒泡到外层滚动容器；此处只监听「上滚」语义。
  const handleWheel = useCallback((e: React.WheelEvent) => {
    if (e.deltaY < 0) {
      setPinned(false);
    }
  }, []);

  if (lines.length === 0) {
    return (
      <div className="p-4 text-sm orca-text-faint" data-testid="log-empty">
        暂无事件
      </div>
    );
  }

  return (
    <div
      className="relative h-full"
      data-testid="log-stream"
      onWheel={handleWheel}
    >
      <List
        rowCount={lines.length}
        rowHeight={28}
        rowComponent={LogRow}
        rowProps={{ items: lines }}
        overscanCount={5}
        defaultHeight={400}
        className="h-full"
        listRef={listRef}
      />
      {!pinned && pendingJump !== null && (
        <button
          type="button"
          onClick={jumpToLatest}
          className="absolute bottom-3 right-3 rounded-full border orca-border orca-bg-surface px-3 py-1 text-xs orca-text-muted shadow hover:orca-bg-surface-2"
          data-testid="log-jump-latest"
        >
          ↓ 跳最新 ({pendingJump + 1})
        </button>
      )}
      {pinned && (
        <span
          // P0b 白名单（intentional inverse）：live badge 是 white-on-dark 强对比浮层，
          // 与 ResolvedToast/AgentsRail DAG overlay 同类，不属于 surface scale。
          className="pointer-events-none absolute bottom-3 right-3 rounded-full bg-slate-900/60 px-2 py-0.5 text-[10px] text-white"
          data-testid="log-pinned"
        >
          live
        </span>
      )}
    </div>
  );
}

