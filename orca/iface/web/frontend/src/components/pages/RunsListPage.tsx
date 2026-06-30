// components/pages/RunsListPage.tsx —— `/` Runs 列表（元数据表格，SPEC §6.2）。
//
// 铁律 1：useRunsList 只拉 /api/runs（元数据），**首页不拉 /events**（playwright 断言）。
// 点击行 → navigate(`/runs/<id>`)（push，后退语义铁律 3）。

import { useNavigate } from "react-router-dom";
import { useRunsList } from "@/hooks/use-runs-list";
import type { RunMeta } from "@/types/events";

export function RunsListPage() {
  const { metas, loading, error } = useRunsList();
  const navigate = useNavigate();

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 p-4">
        <h1 className="text-lg font-semibold">Runs</h1>
      </div>
      <div className="flex-1 overflow-auto p-4">
        {error && (
          <p className="mb-3 text-sm text-orca-failed">加载失败：{error}</p>
        )}
        {loading && metas.length === 0 ? (
          <p className="text-sm text-slate-400">加载中…</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="py-2 pr-4">Run</th>
                <th className="py-2 pr-4">Workflow</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2 pr-4">Progress</th>
                <th className="py-2 pr-4">Cost</th>
                <th className="py-2 pr-4">Elapsed</th>
              </tr>
            </thead>
            <tbody>
              {metas.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-4 text-slate-400">
                    暂无 run —— 点击右上角 "New Run" 创建。
                  </td>
                </tr>
              ) : (
                metas.map((m: RunMeta) => (
                  <tr
                    key={m.run_id}
                    data-testid="run-row"
                    className="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                    onClick={() => navigate(`/runs/${m.run_id}`)}
                  >
                    <td className="py-2 pr-4 font-mono text-xs">
                      {m.run_id.slice(0, 8)}
                    </td>
                    <td className="py-2 pr-4">{m.workflow_name}</td>
                    <td className="py-2 pr-4">{m.status}</td>
                    <td className="py-2 pr-4">{m.progress}</td>
                    <td className="py-2 pr-4">${m.cost.toFixed(4)}</td>
                    <td className="py-2 pr-4">{m.elapsed.toFixed(1)}s</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
