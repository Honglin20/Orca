// App.tsx —— 单 run 根（SPEC §4 / §8 删除多 run 外壳）。
//
// 单 run/页：URL ``/runs/:runId`` 是唯一详情路由；``/`` 重定向到「最新 active run」
// （Chunk A 占位：临时显示「无活跃 run」提示，列表/选择 UI 后置）。多 run 外壳
// （run 列表页 / 侧栏 / 轮询 hook）已删，SPEC §8。
//
// GateDialog 挂在根（SPEC §5.6）：human_decision_requested → 中心模态浮层。

import { BrowserRouter, Route, Routes, Navigate } from "react-router-dom";
import { RunDetailPage } from "@/components/pages/RunDetailPage";
import { GateDialog } from "@/components/gate/GateDialog";

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
        {/* / 重定向占位：Chunk A 无 runs 列表（SPEC §2 后置），临时空提示。 */}
        <Route path="/" element={<SingleRunRoot><NoRunPlaceholder /></SingleRunRoot>} />
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

function NoRunPlaceholder() {
  return (
    <div className="flex flex-1 items-center justify-center text-sm orca-text-faint">
      无活跃 run —— 请通过 ``/runs/:runId`` 打开具体 run。
    </div>
  );
}
