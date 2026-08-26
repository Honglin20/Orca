// components/pages/RunLoadError.tsx —— loader 终态失败错误组件（SPEC audit-c §4.1 INV-1）。
//
// 当 ``loadStatus === "error"`` 时由 RunDetailPage 渲染。显示失败原因（HTTP 状态码 / 网络错误 /
// parse 失败）+「重试」按钮（调 store ``loadRunWithMeta``）+「返回列表」入口。
//
// 不静默吞错（CLAUDE.md 规则 12 Fail loud）：用户必须看到「为什么空白」+ 自救入口。

import { useNavigate } from "react-router-dom";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { LoadError } from "@/types/store-types";

interface RunLoadErrorProps {
  runId: string;
}

function describeError(err: LoadError): { title: string; detail: string } {
  if (err.kind === "http") {
    return { title: `HTTP ${err.status}`, detail: err.message };
  }
  if (err.kind === "network") {
    return { title: "网络错误", detail: err.message };
  }
  return { title: "数据解析失败", detail: err.message };
}

export function RunLoadError({ runId }: RunLoadErrorProps) {
  const loadError = useWorkflowStore((s) => s.loadError);
  const retryCount = useWorkflowStore((s) => s.retryCount);
  const loadRunWithMeta = useWorkflowStore((s) => s.loadRunWithMeta);
  const navigate = useNavigate();

  if (!loadError) return null;
  const { title, detail } = describeError(loadError);

  return (
    <div
      className="orca-bg-app orca-text flex h-full flex-col items-center justify-center gap-3 p-6 text-sm"
      data-testid="run-load-error"
    >
      <div className="text-base font-medium text-orca-failed">加载失败</div>
      <div className="orca-text-muted">{title}</div>
      {detail && (
        <div className="orca-text-faint max-w-md text-center text-xs break-all">
          {detail}
        </div>
      )}
      {retryCount > 0 && (
        <div className="orca-text-faint text-xs">
          已重试 {retryCount} 次后仍失败（共 {retryCount + 1} 次尝试）
        </div>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => void loadRunWithMeta(runId)}
          className="rounded border orca-border orca-accent orca-bg-surface px-3 py-1 text-xs font-medium hover:orca-bg-surface-2"
          data-testid="run-load-error-retry"
        >
          重试
        </button>
        <button
          type="button"
          onClick={() => navigate("/")}
          className="orca-text-muted rounded border orca-border px-3 py-1 text-xs hover:orca-bg-surface-2"
          data-testid="run-load-error-back"
        >
          返回列表
        </button>
      </div>
    </div>
  );
}
