// components/runlist/ErrorBanner.tsx —— refresh 失败提示（SPEC §4/§3.1/§5.1）。
//
// 契约：``error && !loading`` 时渲染——红字 + [重试]。fail-loud（§ AC-15）。

import { AlertCircle, RefreshCw } from "lucide-react";

interface Props {
  message: string;
  onRetry: () => void;
}

export function ErrorBanner({ message, onRetry }: Props) {
  return (
    <div
      data-testid="error-banner"
      role="alert"
      className="orca-bg-surface orca-border flex items-center gap-3 rounded border border-orca-failed/30 px-4 py-3"
    >
      <AlertCircle size={16} strokeWidth={1.5} aria-hidden className="text-orca-failed shrink-0" />
      <div className="flex-1">
        <p className="text-orca-failed text-sm font-medium">刷新失败</p>
        <p className="orca-text-muted text-xs">{message}</p>
      </div>
      <button
        type="button"
        data-testid="retry-btn"
        onClick={onRetry}
        className="orca-text-muted hover:orca-text inline-flex items-center gap-1 rounded border orca-border px-2 py-1 text-xs"
      >
        <RefreshCw size={12} strokeWidth={1.5} aria-hidden />
        重试
      </button>
    </div>
  );
}
