// components/conversation/CodeViewer.tsx —— workflow 浏览页专用代码查看器（prism 高亮）。
//
// plan idempotent-churning-lampson §M7(a) + §M8 闭环。**新写**（**不动 FileContentView.tsx**
// ——conversation read 工具结果仍用纯文本版；本组件是 browse 页专用，prism 高亮 + 行号）。
//
// **D1（spec review 决策）**：prism 主题 CSS **在本文件内** ``import "prismjs/themes/prism.css"``
// （Vite code-split 进 browse chunk）。**不改 main.tsx**——全局 import 会顺带改已发布
// ``/runs/:runId`` 代码块配色（无评审视觉回归），违反「纯增量不影响现有功能」铁律。
//
// **M8**：超 50KB 文件回退 plain ``<pre>`` + 小字提示（防 prism 主线程冻结）。
//
// **行级 highlight**：每行独立 ``Prism.highlight``——多行 token（多行字符串/块注释）会被
// 切碎，但行号与代码行严格对齐（整段 highlight 后注入单元素会让行号错位）。浏览场景代码
// 块普遍 <200 行，行级退化可读即可（YAGNI）。

import { useMemo } from "react";
import Prism from "prismjs";
import "prismjs/themes/prism.css";
import "prismjs/components/prism-python";
import "prismjs/components/prism-yaml";
import "prismjs/components/prism-json";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-javascript";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-toml";
import "prismjs/components/prism-markdown";

const HIGHLIGHT_MAX = 50_000;

const EXT_TO_LANG: Record<string, string> = {
  py: "python",
  yaml: "yaml",
  yml: "yaml",
  json: "json",
  js: "javascript",
  ts: "typescript",
  sh: "bash",
  bash: "bash",
  toml: "toml",
  md: "markdown",
};

interface CodeViewerProps {
  text: string;
  ext: string;
  filename?: string;
}

export function CodeViewer({ text, ext, filename }: CodeViewerProps) {
  const lang = EXT_TO_LANG[ext];
  const tooLarge = text.length > HIGHLIGHT_MAX;
  const grammar = lang && !tooLarge ? Prism.languages[lang] : null;

  // 每行独立 highlight（行号 / 代码行严格对齐；多行 token 切碎可接受）。
  const highlightedLines = useMemo(() => {
    if (!grammar) return null;
    return text.split("\n").map((line) => {
      try {
        return Prism.highlight(line, grammar, lang ?? "");
      } catch {
        return escapeHtml(line); // fail-soft：prism 抛错降级为 escaped 纯文本
      }
    });
  }, [text, grammar, lang]);

  const lines = text.split("\n");

  return (
    <div
      className="orca-border orca-bg-surface flex h-full flex-col border"
      data-testid="code-viewer"
    >
      {filename && (
        <div
          className="orca-bg-surface-2 orca-border orca-text-muted border-b px-3 py-1.5 text-xs font-medium truncate"
          data-testid="code-filename"
        >
          {filename}
        </div>
      )}
      {tooLarge && (
        <div className="orca-border orca-text-faint border-b px-3 py-1 text-xs">
          文件超过 {HIGHLIGHT_MAX} 字节，已回退纯文本展示（防高亮卡死）。
        </div>
      )}
      <div className="orca-bg-app flex-1 overflow-auto">
        <table className="w-full border-collapse text-xs leading-[18px]">
          <tbody>
            {lines.map((line, i) => (
              <tr key={i}>
                <td className="orca-text-faint orca-border w-10 select-none border-r pr-2 text-right align-top">
                  {i + 1}
                </td>
                <td className="orca-text whitespace-pre pl-3 pr-3 align-top">
                  {highlightedLines ? (
                    <code
                      className={`language-${lang}`}
                      // 行级 highlight 后的 HTML（prism token span）。prism 输出经审计
                      // 仅产生 <span class="token ..."> 嵌套，无脚本/事件处理器——dangerously
                      // 安全。
                      dangerouslySetInnerHTML={{
                        __html: highlightedLines[i] || "&nbsp;",
                      }}
                    />
                  ) : (
                    <code>{line || " "}</code>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
