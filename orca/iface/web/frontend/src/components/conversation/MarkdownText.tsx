// components/conversation/MarkdownText.tsx —— markdown 渲染入口（稳定 import surface）。
//
// SPEC §5.3：agent_message / agent_thinking / dialog_message / prompt_rendered 展开体
// 用此组件渲染——学术输出 quartet（表格 / LaTeX / 代码高亮）。
//
// **角色**：本文件只是 re-export ``MarkdownTextImpl.tsx`` 的稳定 import 入口——所有重依赖
// （react-markdown / remark-math / rehype-katex / rehype-prism-plus）在 impl 内 import。
//
// **D5 bundle split 策略**：chunk 切分点抬到 **view 层**——``RunDetailPage`` 用
// ``React.lazy`` 包装整个 ``ConversationView``（含本组件及其传递依赖的 markdown 全家桶），
// 让首屏（仅 TopBar + AgentsRail + LogStream）不加载 markdown。ChartsView（recharts）/
// WorkflowGraph（xyflow）同款 lazy 拆 chunk。三大家族（markdown / recharts / xyflow）
// 独立 chunk，首屏 ~290KB。
//
// 不在单组件层 lazy（MarkdownText 自身）：React.lazy + Suspense 在 vitest 下需异步断言，
// 抬到 view 层既得最优切分（整个 conversation 树不进首屏），又保留本组件 sync API（测试
// 用 getByText 即可，不需 findByText）。
//
// D3 image URL rewrite（SPEC §0 D10）：见 ``MarkdownTextImpl.rewriteImageSrc``。
// 抄 AgentHarness MarkdownText 设计（memoized），不抄其多 store 依赖。

export { MarkdownTextImplMemoized as MarkdownText } from "./MarkdownTextImpl";
export { rewriteImageSrc } from "./MarkdownTextImpl";
export type { MarkdownTextProps } from "./MarkdownTextImpl";
