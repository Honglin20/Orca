// components/conversation/FileContentView.tsx —— read 工具结果展示（SPEC §5.3 工具展开）。
//
// 轻量自建（不依赖 prismjs 全量高亮，避免动态 import 复杂度；YAGNI——read 工具结果
// 多为日志 / 文本，纯文本展示已可读，后续若需语法高亮再加 prismjs lazy import）。
// AH 版本动态 import prismjs + 语法 component——复杂度高、bundle 大；本版精简。

interface FileContentViewProps {
  content: string;
  filePath?: string;
}

export function FileContentView({ content, filePath }: FileContentViewProps) {
  const lines = content.split("\n");
  return (
    <div
      className="rounded-md border border-slate-300 overflow-hidden text-xs font-mono dark:border-slate-600"
      data-testid="file-content-view"
    >
      {filePath && (
        <div className="bg-slate-100 dark:bg-slate-700/60 px-2 py-1 text-xs font-medium text-slate-600 dark:text-slate-300 border-b border-slate-300 dark:border-slate-600 truncate">
          {filePath}
        </div>
      )}
      <div className="max-h-64 overflow-y-auto">
        {lines.map((line, i) => (
          <div key={i} className="flex hover:bg-slate-100/60 dark:hover:bg-slate-700/40">
            <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700 leading-[18px]">
              {i + 1}
            </span>
            <code className="pl-2 whitespace-pre text-xs leading-[18px] flex-1 min-w-0 text-slate-800 dark:text-slate-200">
              {line || " "}
            </code>
          </div>
        ))}
      </div>
    </div>
  );
}
