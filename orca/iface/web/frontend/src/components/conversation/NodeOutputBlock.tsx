// components/conversation/NodeOutputBlock.tsx —— node_completed.data.output 渲染（SPEC-B B1）。
//
// 背景：``node_completed`` 原被 entries.ts 归入 NODE_DIVIDER_TYPES，output 文字被丢弃
// （只画一条 dim 分隔线）。B1 把它升格为 output block，真正显示节点产出。
//
// 按 ``typeof data.output`` 分支（spec-reviewer BLOCKER）：
//   - ``string``  → MarkdownText（自由文本 output，与 MessageBlock 同渲染管线，
//                   保留表格 / LaTeX / Prism code）
//   - ``object``  → ``<pre>`` JSON 预览（节点声明 output_schema 时，后端
//                   ``extract_and_validate`` 返解析后的 dict / list）
//   - ``null`` / undefined → dim「（无 output）」占位
//   - 其它原始类型（number / boolean） → ``<pre>`` JSON 字符串（防御性，不静默丢）
//
// **永不折叠**（同 MessageBlock 哲学，完整 output 直接可见）。
// **不读 data.elapsed**：in-session 路径 ``node_completed.data`` 只含 output
// （spec-reviewer MAJOR）；标准 executor 路径虽带 elapsed，本组件无消费需求，不依赖。

import type { WebEvent } from "@/types/events";
import { MarkdownText } from "./MarkdownText";

interface NodeOutputBlockProps {
  event: WebEvent;
}

export function NodeOutputBlock({ event }: NodeOutputBlockProps) {
  const output = event.data?.output;
  const nodeLabel = event.node ?? "node";

  let body: React.ReactNode;
  // 用 testid 区分三分支，便于单测 DOM 断言（不依赖文本匹配）。
  if (output == null) {
    body = (
      <span
        data-testid="node-output-empty"
        className="italic text-slate-400 dark:text-slate-500"
      >
        （无 output）
      </span>
    );
  } else if (typeof output === "string") {
    body = <MarkdownText>{output}</MarkdownText>;
  } else {
    // object（dict / list）/ number / boolean 一律走 JSON 预览：
    // 避免 React 渲染 object 报错 / 显 [object Object]，并保原始类型信息。
    body = (
      <pre
        data-testid="node-output-json"
        className="overflow-x-auto rounded bg-slate-50 p-2 text-xs leading-snug text-slate-700 dark:bg-slate-900 dark:text-slate-200"
      >
        {JSON.stringify(output, null, 2)}
      </pre>
    );
  }

  return (
    <div className="py-1" data-testid="node-output">
      {/* 顶细线 + dim 标签：承担「节点产出」边界感（取代 node_completed 旧的 divider 角色） */}
      <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-400 dark:text-slate-500">
        <span className="h-px flex-1 bg-slate-200 dark:bg-slate-700" />
        <span>■ {nodeLabel} output</span>
        <span className="h-px flex-1 bg-slate-200 dark:bg-slate-700" />
      </div>
      <div className="min-w-0 text-sm">{body}</div>
    </div>
  );
}
