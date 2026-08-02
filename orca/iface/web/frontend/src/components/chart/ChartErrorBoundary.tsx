// components/chart/ChartErrorBoundary.tsx —— 单 chart ErrorBoundary（SPEC audit-c §4.2 INV-6）。
//
// SPEC 钉死位置（M16）：包在 LazyChartWidget **内**、ChartWidget **外**——IO 已 disconnect
// （无循环重挂），且仅在 chart 真正进入视口后才需 boundary。
//
// recharts 或任何 chart widget 抛错 **不得**冒泡卸载整个 charts tab。fallback UI：
// 「该图表渲染失败」+ 折叠 stack（dev 模式展开），data-testid ``chart-error-fallback``。

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}
interface State {
  hasError: boolean;
  error: Error | null;
}

export class ChartErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // fail loud：单 chart 抛错必须可见（不静默吞）
    console.error("[orca] chart 渲染抛错（ErrorBoundary 兜底）", error, info);
  }

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    return (
      <div
        className="orca-text-failed flex aspect-[4/3] w-full items-center justify-center rounded border orca-border p-2 text-xs"
        data-testid="chart-error-fallback"
      >
        <div className="text-center">
          <div>该图表渲染失败</div>
          {import.meta.env.DEV && this.state.error && (
            <details className="orca-text-faint mt-1 text-[10px]">
              <summary className="cursor-pointer">stack</summary>
              <pre className="max-h-40 overflow-auto text-left whitespace-pre-wrap">
                {this.state.error.stack ?? this.state.error.message}
              </pre>
            </details>
          )}
        </div>
      </div>
    );
  }
}
