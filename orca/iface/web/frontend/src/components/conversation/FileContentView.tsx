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
      className="orca-border rounded-md border text-xs font-mono"
      data-testid="file-content-view"
    >
      {filePath && (
        <div className="orca-bg-surface-2 orca-border orca-text-muted border-b px-2 py-1 text-xs font-medium truncate">
          {filePath}
        </div>
      )}
      <div className="max-h-64 overflow-y-auto">
        {lines.map((line, i) => (
          <div key={i} className="hover:orca-bg-surface-2 flex">
            <span className="orca-text-faint orca-border shrink-0 w-8 select-none border-r pr-2 text-right leading-[18px]">
              {i + 1}
            </span>
            <code className="orca-text min-w-0 flex-1 whitespace-pre pl-2 text-xs leading-[18px]">
              {line || " "}
            </code>
          </div>
        ))}
      </div>
    </div>
  );
}
