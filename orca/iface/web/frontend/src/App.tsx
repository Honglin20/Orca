// App.tsx —— 路由根（SPEC §4 / §13 §6.1：列表页 + 详情页）。
//
// 路由（§13 §6.1）：
//   - ``/`` → 多 run 列表页（dashboard，跨项目 discovery）
//   - ``/runs/:runId`` → 单 run 详情页（零改，懒挂载对详情页透明）
//
// ``orca open`` 无参 → 列表页；``orca open <rid>`` → 深链直达详情页（D13）。
// GateDialog 挂在根（SPEC §5.6）：human_decision_requested → 中心模态浮层。

import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes, Navigate, useParams } from "react-router-dom";
import { RunDetailPage } from "@/components/pages/RunDetailPage";
import { RunListPage } from "@/components/pages/RunListPage";
import { GateDialog } from "@/components/gate/GateDialog";
import { ApprovalDialog } from "@/components/gate/ApprovalDialog";
import { initTheme } from "@/hooks/use-theme";

// D1（plan idempotent-churning-lampson）：workflow 浏览页 lazy 拆独立 chunk——CodeViewer
// 直接 ``import "prismjs/themes/prism.css"`` + prismjs 组件依赖，若静态 import 会进
// 首屏 index chunk，顺带改 ``/runs/:runId`` 已发布代码块配色（rehype-prism-plus 也产
// token span 但当前无 prism CSS——纯增量铁律要求不污染）。lazy 拆 chunk 后 prism CSS
// 仅在用户实际进入 ``/workflows`` 时加载。
const WorkflowsPage = lazy(() =>
  import("@/components/pages/WorkflowsPage").then((m) => ({ default: m.WorkflowsPage })),
);
const WorkflowBrowsePage = lazy(() =>
  import("@/components/pages/WorkflowBrowsePage").then((m) => ({
    default: m.WorkflowBrowsePage,
  })),
);

// 模块加载即 apply 持久化主题（减少首帧 FOUC；initTheme 内部防御 SSR 无 document）。
initTheme();

function SingleRunRoot({ children }: { children: React.ReactNode }) {
  // SPEC in-session-permission-hook §4.3：ApprovalDialog 按 URL runId 取（route-scoped），
  // broker 已 run-scoped 投递，前端再用 URL 双保险过滤。
  const { runId } = useParams<{ runId: string }>();
  return (
    <div className="flex h-screen flex-col">
      {children}
      {/* GateDialog 挂在 app 根（SPEC §5.6）：fixed inset-0，覆盖三栏。 */}
      <GateDialog />
      {/* ApprovalDialog 挂在 app 根（SPEC in-session-permission-hook §4.3）：独立 store，
          非 workflow gate；按 URL runId 渲染当前 run 的 pending。 */}
      <ApprovalDialog runId={runId} />
    </div>
  );
}

function RouteFallback() {
  // 极简骨架（不引 lucide-react 以免污染首屏 chunk）。
  return (
    <div
      className="orca-bg-app orca-text-faint flex h-full items-center justify-center text-sm"
      data-testid="route-fallback"
    >
      加载中…
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* SPEC §13 §6.1：/ → 多 run 列表页。 */}
        <Route path="/" element={<RunListPage />} />
        <Route
          path="/runs/:runId"
          element={
            <SingleRunRoot>
              <RunDetailPage />
            </SingleRunRoot>
          }
        />
        {/* workflow / agent 资源只读浏览（plan idempotent-churning-lampson）。 */}
        <Route
          path="/workflows"
          element={
            <Suspense fallback={<RouteFallback />}>
              <WorkflowsPage />
            </Suspense>
          }
        />
        <Route
          path="/workflows/:name"
          element={
            <Suspense fallback={<RouteFallback />}>
              <WorkflowBrowsePage />
            </Suspense>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
