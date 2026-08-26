// components/pages/WorkflowsPage.tsx —— workflow 列表页（plan idempotent-churning-lampson）。
//
// 照 RunListPage.tsx 的薄页壳：mount → loadWorkflows（不轮询，m7）；行 click → navigate
// 到 ``/workflows/:name``。**不** import workflow-store（R3 grep 守门）。

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useWorkflowBrowseStore } from "@/stores/workflow-browse-store";

export function WorkflowsPage() {
  const navigate = useNavigate();
  const {
    workflows,
    workflowsLoading,
    workflowsError,
    loadWorkflows,
    reset,
  } = useWorkflowBrowseStore();

  useEffect(() => {
    void loadWorkflows();
    return () => {
      reset();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="orca-bg-app orca-text flex h-full flex-col">
      <header className="orca-bg-surface orca-border orca-text flex h-12 items-center gap-3 border-b px-6">
        <h1 className="text-sm font-semibold">Workflows</h1>
        <span className="orca-text-faint text-xs">
          浏览 workflow 定义与 agent 资源（只读）
        </span>
        <button
          type="button"
          onClick={() => void loadWorkflows()}
          className="orca-text-muted hover:orca-text orca-border ml-auto rounded border px-2 py-1 text-xs"
          data-testid="refresh-btn"
        >
          刷新
        </button>
      </header>
      <main className="orca-bg-app flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-6 py-5">
          {workflowsLoading && workflows.length === 0 && (
            <div
              className="orca-text-faint flex items-center justify-center gap-2 py-12 text-sm"
              data-testid="loading"
            >
              <Loader2 size={14} strokeWidth={1.5} className="animate-spin" aria-hidden />
              <span>加载 workflows…</span>
            </div>
          )}
          {!workflowsLoading && workflowsError && workflows.length === 0 && (
            <div
              className="orca-border orca-bg-surface text-orca-failed rounded border px-4 py-3 text-sm"
              data-testid="error-banner"
            >
              加载失败：{workflowsError}
              <button
                type="button"
                onClick={() => void loadWorkflows()}
                className="orca-text-muted hover:orca-text ml-3 underline"
              >
                重试
              </button>
            </div>
          )}
          {!workflowsLoading && !workflowsError && workflows.length === 0 && (
            <p
              className="orca-text-faint py-12 text-center text-sm"
              data-testid="empty-state"
            >
              暂无 workflow（catalog 目录为空或 yaml 加载失败）
            </p>
          )}
          {workflows.length > 0 && (
            <ul className="space-y-2" data-testid="workflow-list">
              {workflows.map((wf) => (
                <li key={wf.name}>
                  <button
                    type="button"
                    onClick={() => navigate(`/workflows/${encodeURIComponent(wf.name)}`)}
                    className="orca-border orca-bg-surface hover:orca-bg-surface-2 block w-full rounded border px-4 py-3 text-left"
                    data-testid={`workflow-row-${wf.name}`}
                  >
                    <div className="orca-text text-sm font-medium">{wf.name}</div>
                    {wf.description && (
                      <div className="orca-text-muted mt-0.5 text-xs truncate">
                        {wf.description}
                      </div>
                    )}
                    <div className="orca-text-faint mt-1 text-xs">
                      entry: <code className="orca-accent">{wf.entry}</code>
                      {" · "}
                      <span>
                        {wf.inputs_count === 0
                          ? "无 inputs"
                          : `${wf.inputs_count} 个 input`}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  );
}

export default WorkflowsPage;
