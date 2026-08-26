// components/gate/ApprovalDialog.tsx —— in-session 权限审批弹窗（SPEC in-session-permission-hook §4.3）。
//
// **铁律**：复用 PermissionGate.tsx 视觉渲染审批卡；数据源 = approval store（**非** workflow
// store.gate）。SPEC §4.3 末段：独立 useApprovalStore，避免两真相源。
//
// 多卡渲染：broker 可同时有多个 pending approval（broker 并发模型 §3.2）；按 created_at 升序
// 列出。每张卡复用 PermissionGate 视觉但绑定 approval_id（而非 gate_id），点击调
// ``POST /approval/respond`` 或 WS 反向 ``approval_respond``。
//
// 与 PermissionGate.tsx 视觉一致的取舍：复用「弹窗 + 工具/参数 + allow/deny 按钮」结构；
// 不复用组件实例（PermissionGate 强绑 GateState 形状）。此处用同款 className + layout。

import { useState } from "react";
import { Lock, Wrench } from "lucide-react";
import {
  selectPendingForRun,
  useApprovalStore,
  type ApprovalEntry,
} from "@/stores/approval-store";
import { postApprovalRespond } from "./post-approval-respond";

function ApprovalCard({ entry }: { entry: ApprovalEntry }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick(answer: "allow" | "deny") {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await postApprovalRespond({
        approval_id: entry.approval_id,
        answer,
        source: "web",
      });
      // 不乐观移除：等 broker 广播 approval_resolved（store.pending 自然清，弹窗消失）。
      // submitting 保持 true（按钮 disabled）直到弹窗消失。
    } catch (err) {
      // 网络失败：fail loud（重新启用按钮让用户重试）。
      console.error("[orca] POST /approval/respond 失败", err);
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  }

  return (
    <div className="rounded-lg orca-bg-surface shadow-xl" data-testid="approval-card">
      <div className="flex items-center justify-between border-b orca-border px-5 py-3">
        <h2 className="text-base font-semibold orca-text inline-flex items-center gap-1.5">
          <Lock size={16} strokeWidth={1.5} aria-hidden /> 权限请求
        </h2>
        <span className="text-xs orca-text-faint font-mono" data-testid="approval-run">
          {entry.run_id}
        </span>
      </div>
      <div className="px-5 py-4">
        <p className="mb-3 text-sm orca-text-muted">
          in-session CC 子代理想调用工具：
        </p>
        <div className="rounded border orca-border orca-bg-surface-2 p-3">
          <div className="mb-1 text-xs orca-text-faint">工具</div>
          <div
            className="mb-2 font-mono text-sm font-medium orca-text inline-flex items-center gap-1.5"
            data-testid="approval-tool"
          >
            <Wrench size={13} strokeWidth={1.5} aria-hidden /> {entry.tool}
          </div>
          <div className="mb-1 text-xs orca-text-faint">参数</div>
          <pre
            className="max-h-48 overflow-auto rounded orca-bg-surface p-2 font-mono text-xs orca-text-muted"
            data-testid="approval-tool-input"
          >
            {JSON.stringify(entry.tool_input, null, 2)}
          </pre>
        </div>
        {error && (
          <p className="mt-2 text-xs text-orca-failed" data-testid="approval-error">
            提交失败：{error}
          </p>
        )}
      </div>
      <div className="flex justify-end gap-2 border-t orca-border px-5 py-3">
        <button
          type="button"
          disabled={submitting}
          onClick={() => handleClick("deny")}
          className="rounded px-3 py-1.5 text-sm bg-orca-failed text-white hover:bg-orca-failed/90 disabled:opacity-50"
          data-testid="approval-deny"
        >
          {submitting ? "提交中…" : "拒绝"}
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => handleClick("allow")}
          className="rounded px-3 py-1.5 text-sm bg-orca-done text-white hover:bg-orca-done/90 disabled:opacity-50"
          data-testid="approval-allow"
        >
          {submitting ? "提交中…" : "批准执行"}
        </button>
      </div>
    </div>
  );
}

/**
 * 审批弹窗：渲染当前 run 所有 pending approval 卡（多卡 stack，最新在底部）。
 * 无 pending → 不渲染（return null）。
 */
export function ApprovalDialog({ runId }: { runId: string | undefined | null }) {
  const pendingMap = useApprovalStore((s) => s.pending);
  const pending = selectPendingForRun(pendingMap, runId);
  if (pending.length === 0) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      data-testid="approval-overlay"
    >
      <div className="flex w-full max-w-2xl flex-col gap-3">
        {pending.map((entry) => (
          <ApprovalCard key={entry.approval_id} entry={entry} />
        ))}
      </div>
    </div>
  );
}
