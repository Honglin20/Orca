// App.tsx —— 路由导航栈 + Layout 壳（SPEC §2 §6.1）。
//
// 铁律 3：BrowserRouter（URL 路由，后退 = 浏览器原生后退，绝不回错页）。
// 三个路由：`/`（列表）· `/runs/new`（表单）· `/runs/:runId`（详情，懒加载）。
// navigate 全部 push（非 replace）—— 浏览器后退栈正确（playwright 断言）。

import { BrowserRouter, Route, Routes } from "react-router-dom";
import { TopBar } from "@/components/layout/TopBar";
import { RunsSidebar } from "@/components/layout/RunsSidebar";
import { StatusBar } from "@/components/layout/StatusBar";
import { RunsListPage } from "@/components/pages/RunsListPage";
import { NewRunPage } from "@/components/pages/NewRunPage";
import { RunDetailPage } from "@/components/pages/RunDetailPage";

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen flex-col">
      <TopBar />
      <div className="flex flex-1 overflow-hidden">
        <RunsSidebar />
        <main className="flex-1 overflow-hidden bg-slate-50">{children}</main>
      </div>
      <StatusBar />
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/"
          element={
            <Layout>
              <RunsListPage />
            </Layout>
          }
        />
        <Route
          path="/runs/new"
          element={
            <Layout>
              <NewRunPage />
            </Layout>
          }
        />
        <Route
          path="/runs/:runId"
          element={
            <Layout>
              <RunDetailPage />
            </Layout>
          }
        />
      </Routes>
    </BrowserRouter>
  );
}
