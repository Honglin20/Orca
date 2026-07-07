// components/conversation/DiffView.tsx —— write/edit 文件变更展示（SPEC §5.3 工具展开）。
//
// 轻量自建（不依赖 react-diff-viewer-continued）：AH 也是手写逐行 diff，简单且零额外
// 运行时依赖。算法：行级对比，old/new 同 index 不同 → -/+ 双行；old 多出 → -；new 多出 → +。
//
// 不做 LCS 最长公共子序列——粗粒度但足够可读（YAGNI；若需精确 diff 后续 chunk 替换）。
// 决策理由：avoid 重型依赖；AH 同款手写已 production-proven。

interface DiffViewProps {
  oldText: string;
  newText: string;
  fileName?: string;
  mode: "create" | "edit";
}

function lineNum(n: number, w = 3): string {
  return String(n).padStart(w, " ");
}

export function DiffView({ oldText, newText, fileName, mode }: DiffViewProps) {
  const isCreate = mode === "create" || !oldText;
  if (isCreate) {
    const lines = newText.split("\n");
    return (
      <div
        className="rounded-md border border-slate-300 overflow-hidden text-xs font-mono dark:border-slate-600"
        data-testid="diff-view"
      >
        {fileName && (
          <div className="bg-emerald-500/10 px-2 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-300 border-b border-slate-300 dark:border-slate-600">
            + {fileName}
          </div>
        )}
        <div className="max-h-64 overflow-y-auto">
          {lines.map((line, i) => (
            <div
              key={i}
              className="flex hover:bg-emerald-500/5"
            >
              <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700">
                {lineNum(i + 1)}
              </span>
              <span className="pl-2 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200 whitespace-pre">
                {line || " "}
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");
  const maxLen = Math.max(oldLines.length, newLines.length);

  return (
    <div
      className="rounded-md border border-slate-300 overflow-hidden text-xs font-mono dark:border-slate-600"
      data-testid="diff-view"
    >
      {fileName && (
        <div className="bg-blue-500/10 px-2 py-1 text-xs font-medium text-blue-700 dark:text-blue-300 border-b border-slate-300 dark:border-slate-600">
          ~ {fileName}
        </div>
      )}
      <div className="max-h-64 overflow-y-auto">
        {Array.from({ length: maxLen }).map((_, i) => {
          const oldLine = i < oldLines.length ? oldLines[i] : undefined;
          const newLine = i < newLines.length ? newLines[i] : undefined;
          const isRemoved =
            oldLine !== undefined && (newLine === undefined || oldLine !== newLine);
          const isAdded =
            newLine !== undefined && (oldLine === undefined || oldLine !== newLine);

          if (isRemoved && isAdded) {
            return (
              <div key={i}>
                <div className="flex bg-red-500/10">
                  <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700">
                    {lineNum(i + 1)}
                  </span>
                  <span className="pl-2 text-red-800 dark:text-red-200 whitespace-pre">
                    - {oldLine || " "}
                  </span>
                </div>
                <div className="flex bg-emerald-500/10">
                  <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700">
                    {lineNum(i + 1)}
                  </span>
                  <span className="pl-2 text-emerald-800 dark:text-emerald-200 whitespace-pre">
                    + {newLine || " "}
                  </span>
                </div>
              </div>
            );
          }
          if (isRemoved) {
            return (
              <div key={i} className="flex bg-red-500/10">
                <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700">
                  {lineNum(i + 1)}
                </span>
                <span className="pl-2 text-red-800 dark:text-red-200 whitespace-pre">
                  - {oldLine || " "}
                </span>
              </div>
            );
          }
          if (isAdded) {
            return (
              <div key={i} className="flex bg-emerald-500/10">
                <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700">
                  {lineNum(i + 1)}
                </span>
                <span className="pl-2 text-emerald-800 dark:text-emerald-200 whitespace-pre">
                  + {newLine || " "}
                </span>
              </div>
            );
          }
          return (
            <div key={i} className="flex">
              <span className="shrink-0 w-8 text-right pr-2 text-slate-400 select-none border-r border-slate-200 dark:border-slate-700">
                {lineNum(i + 1)}
              </span>
              <span className="pl-2 whitespace-pre text-slate-700 dark:text-slate-300">
                {"  "}{oldLine || " "}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
