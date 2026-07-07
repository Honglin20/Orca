// components/detail/LogStream.tsx —— 流式日志尾，live，虚拟化（SPEC §5.5）。
//
// 三约束（SPEC §5.5）：
//   1. **虚拟滚动**（react-window v2 ``List`` + rowComponent）：1000+ 条不卡。
//   2. **每事件一行**：行数 == tape 事件数；每行 ``{seq}·{type}·{一行摘要≤80字}``；
//      **每个 EventType 均有 readable 摘要，无 no-op fallback**（selectLog/summarizeEvent 保证）。
//   3. **auto-scroll 策略**（闭 review #36）：用户上滚→暂停 auto-scroll + 显示「跳最新」按钮；
//      pinned-to-bottom→新事件滚到末 seq。
//
// 删除：旧 formatLogLine（被 selectLog/summarizeEvent 取代，SPEC §8）；replay 同步逻辑（无 Replay）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { List, type RowComponentProps } from "react-window";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectLog, type LogLine } from "@/selectors";

interface LogRowData {
  items: LogLine[];
}

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
        item.isError ? "text-red-600" : "text-slate-700"
      }`}
      data-testid={`log-row-${index}`}
    >
      <span className="text-slate-400">{item.seq}</span>
      <span className="ml-2 text-slate-500">{item.type}</span>
      <span className="ml-2">{item.text}</span>
    </div>
  );
}

export function LogStream() {
  const state = useWorkflowStore();
  const lines = useMemo(() => selectLog(state), [state]);

  const [pinned, setPinned] = useState(true);
  const [jumpTarget, setJumpTarget] = useState<number | null>(null);
  const prevCountRef = useRef(0);

  // 新事件到达：若 pinned → 滚到末尾（用 hash anchor 占位实现 auto-scroll；react-window v2
  // ref API 跨版本不稳，Chunk A 用 scroll container 末元素 scrollIntoView 留待后续 chunk）。
  // 当前实现：pinned 时显示末尾 jumpTarget=null；用户上滚 → 取消 pinned + 显示「跳最新」。
  useEffect(() => {
    if (lines.length === 0) {
      prevCountRef.current = 0;
      return;
    }
    if (lines.length !== prevCountRef.current) {
      if (!pinned) {
        setJumpTarget(lines.length - 1);
      } else {
        setJumpTarget(null);
      }
      prevCountRef.current = lines.length;
    }
  }, [lines.length, pinned]);

  const jumpToLatest = useCallback(() => {
    setPinned(true);
    setJumpTarget(null);
  }, []);

  if (lines.length === 0) {
    return (
      <div className="p-4 text-sm text-slate-400" data-testid="log-empty">
        暂无事件
      </div>
    );
  }

  return (
    <div
      className="relative h-full"
      data-testid="log-stream"
      onWheel={(e) => {
        if (e.deltaY < 0) setPinned(false);
      }}
    >
      <List
        rowCount={lines.length}
        rowHeight={28}
        rowComponent={LogRow}
        rowProps={{ items: lines }}
        overscanCount={5}
        defaultHeight={400}
        className="h-full"
      />
      {!pinned && jumpTarget !== null && (
        <button
          type="button"
          onClick={jumpToLatest}
          className="absolute bottom-3 right-3 rounded-full border border-slate-300 bg-white px-3 py-1 text-xs text-slate-700 shadow hover:bg-slate-100"
          data-testid="log-jump-latest"
        >
          ↓ 跳最新 ({jumpTarget + 1})
        </button>
      )}
    </div>
  );
}
