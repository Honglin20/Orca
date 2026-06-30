// components/gate/PermissionGate.tsx —— 工具权限弹窗（SPEC §1.3）。
//
// 铁律 1：gate 来自 store，本组件不存 gate。
// 铁律 2：用户答 → POST /gate/respond（前端只 forward，不决策）。
// 铁律 4（SPEC §1.6）：**不乐观更新** —— 点击后只置 submitting（UX 反馈），
// 不清 store.gate / 不关弹窗。弹窗关闭只能由 backend emit human_decision_resolved（store.gate→null）触发。

import { useState } from "react";
import type { GateState } from "@/types/events";
import { postGateRespond } from "./post-gate-respond";

/** 工具权限 4 选项（对齐 hook 桥 allow/deny + 扩展 edit/skip）。 */
type PermissionAnswer = "allow" | "deny" | "edit" | "skip";

const BUTTONS: { answer: PermissionAnswer; label: string; className: string }[] = [
  { answer: "allow", label: "批准执行", className: "bg-emerald-600 text-white hover:bg-emerald-700" },
  { answer: "deny", label: "拒绝", className: "bg-red-600 text-white hover:bg-red-700" },
  { answer: "edit", label: "编辑后批准", className: "bg-slate-200 text-slate-700 hover:bg-slate-300" },
  { answer: "skip", label: "跳过", className: "bg-slate-200 text-slate-700 hover:bg-slate-300" },
];

export function PermissionGate({ gate }: { gate: GateState }) {
  // submitting 仅驱动按钮 disabled + 文案（UX 反馈），**不清 gate、不关弹窗**（SPEC §1.6）。
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tool = String(gate.context?.tool ?? "<unknown>");
  const toolInput = gate.context?.tool_input ?? {};
  const node = String(gate.context?.node ?? gate.context?.session_id ?? "未知");

  async function handleClick(answer: PermissionAnswer) {
    if (submitting) return; // 防重复点
    setSubmitting(true);
    setError(null);
    try {
      await postGateRespond({ gate_id: gate.gate_id, answer, source: "web" });
      // 不乐观更新：等 backend emit human_decision_resolved 才关弹窗（SPEC §1.6）。
      // submitting 保持 true（按钮 disabled）直到弹窗消失（resolved 事件到达）。
    } catch (err) {
      // 网络失败：fail loud（记 error + 重新启用按钮让用户重试），不静默吞。
      console.error("[orca] POST /gate/respond 失败", err);
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      data-testid="gate-dialog"
    >
      <div className="w-full max-w-2xl rounded-lg bg-white shadow-xl" data-testid="permission-gate">
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <h2 className="text-base font-semibold text-slate-900">🔒 权限请求</h2>
        </div>
        <div className="px-5 py-4">
          <p className="mb-3 text-sm text-slate-700">
            节点 <span className="font-mono font-medium">{node}</span> 的 Claude 想调用工具：
          </p>
          <div className="rounded border border-slate-200 bg-slate-50 p-3">
            <div className="mb-1 text-xs text-slate-500">工具</div>
            <div className="font-mono text-sm font-medium text-slate-900" data-testid="gate-tool">
              🔧 {tool}
            </div>
            <div className="mt-2 mb-1 text-xs text-slate-500">参数</div>
            <pre
              className="max-h-48 overflow-auto rounded bg-white p-2 font-mono text-xs text-slate-700"
              data-testid="gate-tool-input"
            >
              {JSON.stringify(toolInput, null, 2)}
            </pre>
          </div>
          {error && (
            <p className="mt-2 text-xs text-red-600" data-testid="gate-error">
              提交失败：{error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-3">
          {BUTTONS.map((b) => (
            <button
              key={b.answer}
              type="button"
              disabled={submitting}
              onClick={() => handleClick(b.answer)}
              className={`rounded px-3 py-1.5 text-sm disabled:opacity-50 ${b.className}`}
              data-testid={`gate-${b.answer}`}
            >
              {submitting ? "提交中…" : b.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
