// components/chart/widgets/DataTableWidget.tsx —— table 类型：扁平 record array → HTML 表格（迁移自 AgentHarness DataTable）。
//
// 简化迁移：去掉 AgentHarness 的 shadcn Table 组件依赖（Orca 无该组件），用原生 table。
// 保留列序（payload.columns 优先，否则取 data[0] keys）+ 数据行数 == payload.data 长度。

import type { ChartPayload } from "../types";

export function DataTableWidget({ payload }: { payload: ChartPayload }) {
  const { data, title, columns } = payload;

  // 列序：优先 payload.columns；否则从首行 keys 派生
  const cols = columns && columns.length > 0 ? columns : data[0] ? Object.keys(data[0]) : [];

  return (
    <div data-testid="chart-widget">
      {title && <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>}
      <div className="overflow-auto rounded border border-slate-200">
        <table className="w-full text-xs" data-testid="data-table">
          <thead className="bg-slate-50">
            <tr>
              {cols.map((col) => (
                <th
                  key={col}
                  className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-600"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((row, i) => (
              <tr key={i} className="even:bg-slate-50/50">
                {cols.map((col) => (
                  <td key={col} className="px-2 py-1 text-slate-700">
                    {String(row[col] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
