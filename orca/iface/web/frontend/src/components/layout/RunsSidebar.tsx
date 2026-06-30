// components/layout/RunsSidebar.tsx —— 常驻左侧 run 列表（元数据轮询，SPEC §6.3）。
//
// 关键（铁律 1）：useRunsList **只** fetch /api/runs（元数据），**不** 拉 events。
// 点击 run → navigate(`/runs/<id>`)（push，后退语义铁律 3）。
// data-testid=run-item 供 playwright 抓取。

import { useNavigate } from "react-router-dom";
import { useRunsList } from "@/hooks/use-runs-list";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { RunStatus } from "@/types/events";

const STATUS_COLOR: Record<RunStatus, string> = {
  queued: "bg-orca-pending",
  running: "bg-orca-running",
  completed: "bg-orca-completed",
  failed: "bg-orca-failed",
};

export function RunsSidebar() {
  const { metas, loading, error } = useRunsList();
  const navigate = useNavigate();
  const activeRunId = useWorkflowStore((s) => s.activeRunId);

  return (
    <aside className="flex w-64 flex-col border-r border-slate-200 bg-white">
      <div className="flex items-center justify-between p-3">
        <h2 className="text-sm font-semibold text-slate-700">Runs</h2>
        <button
          type="button"
          onClick={() => navigate("/runs/new")}
          className="rounded bg-slate-900 px-2 py-1 text-xs text-white hover:bg-slate-700"
        >
          + New
        </button>
      </div>
      {error && (
        <p className="px-3 pb-2 text-xs text-orca-failed">
          加载失败：{error}
        </p>
      )}
      {loading && metas.length === 0 ? (
        <p className="px-3 text-xs text-slate-400">加载中…</p>
      ) : metas.length === 0 ? (
        <p className="px-3 text-xs text-slate-400">暂无 run</p>
      ) : (
        <ul className="flex-1 overflow-y-auto">
          {metas.map((m) => (
            <li key={m.run_id}>
              <button
                type="button"
                data-testid="run-item"
                data-run-id={m.run_id}
                onClick={() => navigate(`/runs/${m.run_id}`)}
                className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-slate-100 ${
                  activeRunId === m.run_id ? "bg-slate-100" : ""
                }`}
              >
                <span
                  className={`inline-block h-2 w-2 rounded-full ${
                    STATUS_COLOR[m.status] ?? "bg-slate-300"
                  }`}
                />
                <span className="font-mono text-xs text-slate-700">
                  {m.run_id.slice(0, 8)}
                </span>
                <span className="ml-auto text-xs text-slate-400">
                  {m.progress}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
