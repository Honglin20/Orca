// components/conversation/ToolRow.tsx —— 单个工具行（call + 可能 result）。
//
// SPEC §5.3：
//   - 行：``⟳/✓ <Tool> <smart arg> <duration>``（pending=⟳ / done=✓）
//   - 展开：args block（文件类隐藏，因 diff/content IS the args）+ 流式输出（while running）
//     + result（write/edit→DiffView；read→FileContentView；其它→pre）
//   - 自动展开 while streaming（pending）+ 自动折叠 on done（AH pattern）。
//
// 调用方：ToolGroup 渲染每个 pair 时复用此组件。

import { useEffect, useState } from "react";
import {
  FILE_TOOLS,
  formatArgsBlock,
  normalizeArgs,
  previewArgs,
} from "./tool-args";
import { toolStatus, type ToolPair } from "./entries";
import { DiffView } from "./DiffView";
import { FileContentView } from "./FileContentView";

interface ToolRowProps {
  pair: ToolPair;
  /** 是否默认展开（ToolGroup 内通常默认折叠；单 tool 默认折叠）。 */
  defaultOpen?: boolean;
}

function getStringArg(args: unknown, key: string): string | undefined {
  const norm = normalizeArgs(args);
  if (!norm) return undefined;
  const v = norm[key];
  return typeof v === "string" ? v : undefined;
}

/** 渲染 result 区（按工具类型分支）。 */
function renderToolResult(toolName: string, toolArgs: unknown, result: unknown) {
  const resultStr = typeof result === "string" ? result : safeJson(result);

  if (toolName === "write" || toolName === "write_file") {
    const path = getStringArg(toolArgs, "path");
    const content = getStringArg(toolArgs, "content") ?? resultStr;
    return <DiffView oldText="" newText={content} fileName={path} mode="create" />;
  }
  if (toolName === "edit" || toolName === "edit_file") {
    const path = getStringArg(toolArgs, "path") ?? getStringArg(toolArgs, "file_path");
    const norm = normalizeArgs(toolArgs);
    const edits = norm?.edits;
    if (Array.isArray(edits) && edits.length > 0) {
      return (
        <div className="space-y-2">
          {edits.map((edit, i) => (
            <DiffView
              key={i}
              oldText={String((edit as Record<string, unknown>)?.oldText ?? (edit as Record<string, unknown>)?.old_string ?? "")}
              newText={String((edit as Record<string, unknown>)?.newText ?? (edit as Record<string, unknown>)?.new_string ?? "")}
              fileName={i === 0 ? path : undefined}
              mode="edit"
            />
          ))}
        </div>
      );
    }
    const oldStr =
      getStringArg(toolArgs, "old_string") ?? getStringArg(toolArgs, "oldText") ?? "";
    const newStr =
      getStringArg(toolArgs, "new_string") ?? getStringArg(toolArgs, "newText") ?? "";
    return <DiffView oldText={oldStr} newText={newStr} fileName={path} mode="edit" />;
  }
  if (toolName === "read" || toolName === "read_file" || toolName === "read_text_file") {
    const path = getStringArg(toolArgs, "path") ?? getStringArg(toolArgs, "file_path");
    return <FileContentView content={resultStr} filePath={path} />;
  }

  // 通用 fallback
  if (!resultStr) {
    return (
      <div className="text-xs text-slate-400 italic" data-testid="tool-no-output">
        (no output)
      </div>
    );
  }
  return (
    <pre className="overflow-x-auto whitespace-pre-wrap text-xs text-slate-700 dark:text-slate-200">
      {resultStr}
    </pre>
  );
}

function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export function ToolRow({ pair, defaultOpen = false }: ToolRowProps) {
  const status = toolStatus(pair);
  const call = pair.call!;
  const toolName = String(call.data?.tool ?? "");
  const args = call.data?.args;
  const argPreview = previewArgs(toolName, args);

  // pending 默认展开（流式输出可见），done 默认折叠（SPEC §5.3 + AH pattern）。
  const [open, setOpen] = useState(defaultOpen || status === "pending");

  // 状态变化时自动展开/折叠（pending→展开看流式；done→折叠到摘要）。
  useEffect(() => {
    if (status === "pending") setOpen(true);
    else setOpen(defaultOpen);
  }, [status, defaultOpen]);

  const isFileTool = FILE_TOOLS.has(toolName);
  const hideArgs = isFileTool || toolName === "render_chart";

  const Icon = status === "pending" ? "⟳" : "✓";
  const iconClass =
    status === "pending"
      ? "text-amber-500 animate-spin-slow"
      : "text-emerald-500";

  return (
    <div
      className="ml-2 border-l-2 border-slate-200 pl-2 dark:border-slate-700"
      data-testid="tool-row"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-xs hover:bg-slate-100/60 dark:hover:bg-slate-700/40"
        aria-expanded={open}
      >
        <span className={`shrink-0 font-mono ${iconClass}`}>{Icon}</span>
        <span className="font-medium text-slate-600 dark:text-slate-300">
          {toolName || "(tool)"}
        </span>
        {argPreview && (
          <span className="min-w-0 truncate font-mono text-xs text-slate-500 dark:text-slate-400">
            {argPreview}
          </span>
        )}
        <span className="ml-auto shrink-0 text-slate-400">
          {open ? "▲" : "▼"}
        </span>
      </button>
      {open && (
        <div className="mt-1 rounded-md border border-slate-200 dark:border-slate-700 bg-slate-50/80 dark:bg-slate-800/40 p-2 text-xs max-h-80 overflow-y-auto">
          {args != null && !hideArgs && (
            <div className="mb-1.5">
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                Args
              </div>
              <pre className="overflow-x-auto whitespace-pre-wrap text-xs max-h-32 overflow-y-auto">
                {formatArgsBlock(args)}
              </pre>
            </div>
          )}
          {pair.result ? (
            <div>
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                Result
              </div>
              {renderToolResult(toolName, args, pair.result.data?.result)}
            </div>
          ) : (
            <div className="italic text-slate-400">running…</div>
          )}
        </div>
      )}
    </div>
  );
}
