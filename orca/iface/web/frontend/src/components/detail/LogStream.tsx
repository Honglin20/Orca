// components/detail/LogStream.tsx —— 流式日志（SPEC §3）。
//
// 三约束（SPEC §3.2）：
//   1. **虚拟滚动**（react-window v2 ``List`` + rowComponent）：1000+ 条不卡。
//   2. **按 session 分组**：连续相同 session_id 的事件归一组，显示 session 头。
//   3. **replay 同步**：replay 模式只显示 events[0..replayPosition]（同一真相）。

import { useMemo } from "react";
import { List, type RowComponentProps } from "react-window";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { WorkflowEvent } from "@/types/events";

/** 格式化单条事件为日志行（HH:MM:SS [session] <desc>）。 */
export function formatLogLine(event: WorkflowEvent): string {
  const ts = new Date(event.timestamp * 1000).toLocaleTimeString("en-GB", {
    hour12: false,
  });
  const sess = event.session_id ? event.session_id.slice(0, 6) : "------";
  const node = event.node ?? "-";
  let detail = "";
  const d = event.data ?? {};
  switch (event.type) {
    case "workflow_started":
      detail = `workflow ${d.workflow_name ?? ""} started`;
      break;
    case "workflow_completed":
      detail = `workflow completed (${d.elapsed ?? "?"}s)`;
      break;
    case "workflow_failed":
      detail = `workflow FAILED: ${d.message ?? ""}`;
      break;
    case "node_started":
      detail = `node started`;
      break;
    case "node_completed":
      detail = `node completed`;
      break;
    case "node_failed":
      detail = `node FAILED: ${d.message ?? ""}`;
      break;
    case "node_skipped":
      detail = `node skipped (${d.reason ?? ""})`;
      break;
    case "agent_message":
      detail = `msg: ${String(d.text ?? "").slice(0, 60)}`;
      break;
    case "agent_thinking":
      detail = `thinking: ${String(d.text ?? "").slice(0, 60)}`;
      break;
    case "agent_tool_call":
      detail = `tool_call: ${d.tool}`;
      break;
    case "agent_tool_result":
      detail = `tool_result`;
      break;
    case "agent_usage":
      detail = `usage: in=${d.input_tokens} out=${d.output_tokens} $${d.cost_usd}`;
      break;
    case "route_taken":
      detail = `route: ${d.from} → ${d.to}`;
      break;
    case "foreach_started":
      detail = `foreach: ${d.item_count} items`;
      break;
    case "foreach_item_started":
      detail = `foreach item[${d.index}]`;
      break;
    case "foreach_item_completed":
      detail = `foreach item[${d.index}] done`;
      break;
    case "foreach_completed":
      detail = `foreach done (${d.count})`;
      break;
    case "human_decision_requested":
      detail = `GATE: ${d.prompt ?? ""}`;
      break;
    case "human_decision_resolved":
      detail = `gate resolved: ${d.answer}`;
      break;
    case "custom":
      detail = `custom[${d.kind}]`;
      break;
    case "error":
      detail = `ERROR: ${d.message ?? ""}`;
      break;
    default:
      detail = event.type;
  }
  return `${ts} [${sess}] ${node}: ${detail}`;
}

/** 单行渲染（react-window v2 rowComponent：props 直接展开 RowProps）。
 *
 * session 头是**独立行**（不是塞进事件行的 block div）—— 把头与事件都拍平进 items，
 * 每行单线高 28px。早先把头 ``<div>`` 和事件文本塞同一固定高行，导致 2 线内容挤进 28px
 * 视觉错位（头与事件文本粘连），且头与事件行内 ``[sess]`` 重复。
 */
function LogRow({ index, style, items }: RowComponentProps<LogRowData>): React.ReactElement {
  const item = items[index];
  if (item.kind === "header") {
    return (
      <div
        style={style}
        className="flex items-end px-2 pb-0.5 text-[10px] uppercase tracking-wide text-indigo-500"
        data-testid={`log-header-${index}`}
      >
        session {item.session}
      </div>
    );
  }
  const isErr = item.event.type.includes("failed") || item.event.type === "error";
  return (
    <div
      style={style}
      className={`flex items-center whitespace-nowrap px-2 font-mono text-xs ${
        isErr ? "text-red-600" : "text-slate-700"
      }`}
      data-testid={`log-row-${index}`}
    >
      {formatLogLine(item.event)}
    </div>
  );
}

/** 一个扁平行：session 分组头 或 事件。 */
type LogItem =
  | { kind: "header"; session: string }
  | { kind: "event"; event: WorkflowEvent };

interface LogRowData {
  items: LogItem[];
}

export function LogStream() {
  const events = useWorkflowStore((s) => s.events);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const replayPosition = useWorkflowStore((s) => s.replayPosition);

  // replay 模式只显示 events[0..replayPosition]（同一真相，SPEC §3.2）
  const visible = replayMode ? events.slice(0, replayPosition + 1) : events;

  // 按 session_id 分组：连续相同 session 前插一个 header 行（独立行，SPEC §3.2）。
  // 仅**真实** session（非 null）才插头——workflow_*  / route_taken 等无 session 的事件
  // 本就 ``[------]`` 内联标 sessionless，再插 ``session —`` 头只会每行一个噪声头。
  const items = useMemo(() => {
    const out: LogItem[] = [];
    let prevSession: string | null = null;
    for (const event of visible) {
      const sess = event.session_id ?? null;
      if (sess !== null && sess !== prevSession) {
        out.push({ kind: "header", session: sess });
      }
      out.push({ kind: "event", event });
      prevSession = sess;
    }
    return out;
  }, [visible]);

  if (items.length === 0) {
    return (
      <div className="p-4 text-sm text-slate-400" data-testid="log-empty">
        暂无事件
      </div>
    );
  }

  return (
    <div className="h-full" data-testid="log-stream">
      <List
        rowCount={items.length}
        rowHeight={28}
        rowComponent={LogRow}
        rowProps={{ items }}
        overscanCount={5}
        defaultHeight={400}
        className="h-full"
      />
    </div>
  );
}
