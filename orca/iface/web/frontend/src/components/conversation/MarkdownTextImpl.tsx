// components/conversation/MarkdownTextImpl.tsx —— markdown 渲染实现（重依赖，lazy-loaded）。
//
// **此模块刻意独立**（D5 bundle split）：react-markdown + remark-math + rehype-katex +
// rehype-prism-plus 全家桶约 ~2MB，进首屏 chunk 是浪费——绝大多数 run 直到 agent_message
// 到达才需要 markdown。``MarkdownText.tsx`` 用 ``React.lazy`` 包装本模块，首次渲染时
// 拉取独立 chunk；首屏 initial bundle 大幅瘦身（charts/graph 仅需 recharts/xyflow）。
//
// D3 image URL rewrite（SPEC §0 D10）：markdown 内 ``![](rel.png)`` / ``file://...`` /
// 裸文件名经 ``rewriteImageSrc`` 改写到后端 endpoint ``/api/runs/<id>/assets/<hash>``。
// runId 从 store.activeRunId 派生（单一真相），不进 prop drilling。
//
// 抄 AgentHarness MarkdownText 设计（memoized），不抄其多 store 依赖。
// - memo：streaming 期间只在 string identity 变化时重 parse（React.memo 默认浅比较）。
// - lazy image：``<img loading="lazy">``。

import { memo, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypePrism from "rehype-prism-plus";
import { useWorkflowStore } from "@/stores/workflow-store";

export interface MarkdownTextProps {
  children: string;
  className?: string;
}

/**
 * 把 markdown 内的图片相对 / file:// / 裸文件名路径改写到后端 assets endpoint。
 *
 * 绝对 http(s) / data: / blob: 不改写（已是 web 可访问 URL）。其余一律视作「run 局部
 * 资源」→ ``/api/runs/<runId>/assets/<encoded>``。后端按 encoded path 在 run dir 解析
 * 文件，找不到 fail loud 404（SPEC §0 D10）。
 *
 * hash 编码：用 ``encodeURIComponent`` 保留路径分隔符语义（``/`` → ``%2F``），让后端
 * 单一 ``<path:path>`` param 拼回原相对路径。这与「hash」名义略有偏差——保留路径结构
 * 比单向 hash 更可调试（后端 log 看得出原始路径），且 SPEC 用语 ``<hash>`` 是占位非强约束。
 */
export function rewriteImageSrc(src: string, runId: string | null): string {
  if (!src) return src;
  // 已是 web 可访问 URL → 直通
  if (/^(https?:|data:|blob:|\/api\/)/.test(src)) return src;
  // file:// → 剥前缀取 path
  let rel = src;
  if (rel.startsWith("file://")) {
    rel = rel.slice("file://".length);
    // 去掉前导 host 段（如 file:///tmp/x.png → /tmp/x.png；file://localhost/x.png → x.png）
    rel = rel.replace(/^\/?(?:localhost|[^/]+)\/+/, "");
    // 绝对路径：取 basename 作相对资源（后端 assets 在 run dir 内，不允许越界）
    rel = rel.replace(/^\/+/, "");
  } else {
    // ./ 前缀剥离，纯粹相对资源
    rel = rel.replace(/^\.\//, "");
  }
  if (!runId) return src; // 无活跃 run → 不改写（fail loud：图片大概率 404，但不崩渲染）
  const encoded = encodeURIComponent(rel);
  return `/api/runs/${runId}/assets/${encoded}`;
}

function MarkdownTextImpl({ children, className }: MarkdownTextProps) {
  // D3：runId 从单一 store 派生（不进 prop drilling，避免污染 4 个调用方）。
  const runId = useWorkflowStore((s) => s.activeRunId);

  // components 对象依赖 runId → 在 closure 内构造（确保 img renderer 见到最新 runId）。
  // useMemo 避免每 render 重建 components 引用（ReactMarkdown 会 deep-compare，但省一次对象构建）。
  const components = useMemo<Components>(() => ({
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
      const { className: cls, children: c, node, ...rest } = props as {
        className?: string;
        children?: React.ReactNode;
        node?: { position?: { start: { line: number }; end: { line: number } } };
      };
      // react-markdown v9 移除了 inline prop。判断 inline vs block：
      // - 有 language-xxx className → 肯定是带语言围栏的块代码
      // - node.position 跨多行（start.line != end.line）→ 块代码（无语言围栏）
      // - 否则 → 行内 code
      const hasLang = /language-/.test(cls ?? "");
      const pos = node?.position;
      const isBlock = hasLang || (pos != null && pos.start.line !== pos.end.line);
      if (isBlock) {
        return (
          <code className={`${cls ?? ""} font-mono text-[12px]`} {...rest}>
            {c}
          </code>
        );
      }
      return (
        <code
          className="orca-bg-surface-2 rounded px-1 py-0.5 font-mono text-[12px]"
          {...rest}
        >
          {c}
        </code>
      );
    },
    pre: ({ children }) => (
      <pre className="orca-bg-surface-2 my-2 overflow-x-auto rounded-md p-2 text-[12px]">
        {children}
      </pre>
    ),
    table: ({ children }) => (
      <div className="my-2 overflow-x-auto">
        <table className="border-collapse text-xs">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className="orca-border orca-bg-surface-2 border px-2 py-1 text-left font-medium">
        {children}
      </th>
    ),
    td: ({ children }) => (
      <td className="orca-border border px-2 py-1">
        {children}
      </td>
    ),
    a: ({ children, href }) => (
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        className="orca-accent hover:orca-text underline"
      >
        {children}
      </a>
    ),
    blockquote: ({ children }) => (
      <blockquote className="orca-border orca-text-muted my-2 border-l-2 pl-3">
        {children}
      </blockquote>
    ),
    img: ({ src, alt }) => {
      // D3：相对 / file:// / 裸文件名 → /api/runs/<runId>/assets/<encoded>
      const raw = typeof src === "string" ? src : "";
      const finalSrc = raw ? rewriteImageSrc(raw, runId) : undefined;
      return (
        <img
          src={finalSrc}
          alt={alt ?? ""}
          loading="lazy"
          decoding="async"
          className="my-2 max-w-full rounded"
        />
      );
    },
  }), [runId]);

  return (
    <div
      className={`markdown-body orca-text text-sm leading-relaxed ${
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

export const MarkdownTextImplMemoized = memo(MarkdownTextImpl);
export default MarkdownTextImplMemoized;
