// App.tsx —— 路由根（SPEC §4 / §13 §6.1：列表页 + 详情页）。
//
// 路由（§13 §6.1）：
//   - ``/`` → 多 run 列表页（dashboard，跨项目 discovery）
//   - ``/runs/:runId`` → 单 run 详情页（零改，懒挂载对详情页透明）
//
// ``orca open`` 无参 → 列表页；``orca open <rid>`` → 深链直达详情页（D13）。
// GateDialog 挂在根（SPEC §5.6）：human_decision_requested → 中心模态浮层。

import { BrowserRouter, Route, Routes, Navigate } from "react-router-dom";
import { RunDetailPage } from "@/components/pages/RunDetailPage";
import { RunListPage } from "@/components/pages/RunListPage";
import { GateDialog } from "@/components/gate/GateDialog";
import { initTheme } from "@/hooks/use-theme";

// 模块加载即 apply 持久化主题（减少首帧 FOUC；initTheme 内部防御 SSR 无 document）。
initTheme();

function SingleRunRoot({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen flex-col">
      {children}
      {/* GateDialog 挂在 app 根（SPEC §5.6）：fixed inset-0，覆盖三栏。 */}
      <GateDialog />
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
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
