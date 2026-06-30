import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import { useWorkflowStore } from "@/stores/workflow-store";

// 调试入口（opt-in）：URL 带 ?debug=1 时把 store 挂在 window.__orcaStore，让 playwright
// 集成测试能注入事件（processEvent）验证前端渲染（gate 弹窗 / chart）。**opt-in**：默认 URL
// 不带 ?debug=1 → 不暴露（prod 行为不变，前端不持有真相的铁律不受影响）；仅集成测试访问时开启。
if (typeof window !== "undefined" && new URLSearchParams(window.location.search).has("debug")) {
  (window as unknown as { __orcaStore?: typeof useWorkflowStore }).__orcaStore =
    useWorkflowStore;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
