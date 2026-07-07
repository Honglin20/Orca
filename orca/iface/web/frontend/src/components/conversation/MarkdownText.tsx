// components/conversation/MarkdownText.tsx —— markdown 渲染（gfm + math + katex + prism）。
//
// SPEC §5.3：agent_message / agent_thinking / dialog_message / prompt_rendered 展开体
// 用此组件渲染——学术输出 quartet（表格 / LaTeX / 代码高亮）。
//
// 抄 AgentHarness MarkdownText 设计（memoized），不抄其多 store 依赖。
// - memo：streaming 期间只在 string identity 变化时重 parse（React.memo 默认浅比较）。
// - lazy image：``<img loading="lazy">``。
// - Tailwind prose 紧凑覆盖（p / ul / ol / code / pre / table / img）。
//
// 待 D10（image URL rewrite）落地：``<img src>`` 经 endpoint 改写——此处当前直通，
// 后续 chunk 在 components.img 内加 URL rewrite hook（OCP：新增策略不改本组件结构）。

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypePrism from "rehype-prism-plus";

interface MarkdownTextProps {
  children: string;
  className?: string;
}

const components: Components = {
  p: ({ children }) => (
    <p className="my-2 whitespace-pre-wrap break-words">{children}</p>
  ),
  ul: ({ children }) => (
    <ul className="my-2 ml-5 list-disc">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 ml-5 list-decimal">{children}</ol>
  ),
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  h1: ({ children }) => (
    <h1 className="mt-3 mb-2 text-base font-semibold">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-3 mb-2 text-sm font-semibold">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-2 mb-1.5 text-sm font-semibold">{children}</h3>
  ),
  code: (props) => {
    const { className, children, node, ...rest } = props as {
      className?: string;
      children?: React.ReactNode;
      node?: { position?: { start: { line: number }; end: { line: number } } };
    };
    // react-markdown v9 移除了 inline prop。判断 inline vs block：
    // - 有 language-xxx className → 肯定是带语言围栏的块代码
    // - node.position 跨多行（start.line != end.line）→ 块代码（无语言围栏）
    // - 否则 → 行内 code
    const hasLang = /language-/.test(className ?? "");
    const pos = node?.position;
    const isBlock = hasLang || (pos != null && pos.start.line !== pos.end.line);
    if (isBlock) {
      return (
        <code className={`${className ?? ""} font-mono text-[12px]`} {...rest}>
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded bg-slate-200/60 px-1 py-0.5 font-mono text-[12px] dark:bg-slate-700/60"
        {...rest}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-md bg-slate-100 p-2 text-[12px] dark:bg-slate-800/80">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-slate-300 bg-slate-100 px-2 py-1 text-left font-medium dark:border-slate-600 dark:bg-slate-700/60">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-slate-300 px-2 py-1 dark:border-slate-600">
      {children}
    </td>
  ),
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-blue-600 underline hover:text-blue-500 dark:text-blue-400"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-slate-300 pl-3 text-slate-600 dark:border-slate-600 dark:text-slate-300">
      {children}
    </blockquote>
  ),
  img: ({ src, alt }) => (
    <img
      src={typeof src === "string" ? src : undefined}
      alt={alt ?? ""}
      loading="lazy"
      decoding="async"
      className="my-2 max-w-full rounded"
    />
  ),
};

function MarkdownTextImpl({ children, className }: MarkdownTextProps) {
  return (
    <div
      className={`markdown-body text-sm leading-relaxed text-slate-800 dark:text-slate-100 ${
        className ?? ""
      }`}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex, [rehypePrism, { ignoreMissing: true }]]}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export const MarkdownText = memo(MarkdownTextImpl);
