// components/gate/AskGate.tsx —— agent 主动问弹窗（SPEC §1.4）。
//
// 铁律 1：gate 来自 store，不存 gate。
// 铁律 2：提交 → POST /gate/respond（前端不决策）。
// 铁律 4（SPEC §1.6）：不乐观更新 —— 提交后只置 submitting，等 resolved 事件才关弹窗。
//
// 渲染分支：
//   - gate.options 非空 → radio 选择（取选中项作为 answer）
//   - gate.options 空 → textarea 自由文本（输入作为 answer）

import { useEffect, useState } from "react";
import type { GateState } from "@/types/events";
import { postGateRespond } from "./post-gate-respond";

export function AskGate({ gate }: { gate: GateState }) {
  const hasOptions = Array.isArray(gate.options) && gate.options.length > 0;
  const [selected, setSelected] = useState(hasOptions ? gate.options![0] : "");
  const [text, setText] = useState("");
  // submitting 仅 UX 反馈，不清 gate（SPEC §1.6）。
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // gate 切换（不同 gate_id）→ 重置 selected 到新 options 首项。
  // 正常情况 resolved→requested 之间 gate 会先变 null（GateDialog 不渲染 AskGate），下次是新 mount；
  // 但若 store 直接换 gate 而不经 null（罕见），此 effect 保证 selected 同步到新 gate 的 options。
  useEffect(() => {
    setSelected(hasOptions && gate.options && gate.options.length > 0 ? gate.options[0] : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只依赖 gate_id（options 随 gate_id 变）
  }, [gate.gate_id]);

  async function handleSubmit() {
    const answer = hasOptions ? selected : text;
    if (!answer.trim()) return;
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await postGateRespond({ gate_id: gate.gate_id, answer, source: "web" });
      // 不乐观更新：等 backend emit human_decision_resolved。
    } catch (err) {
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
      <div className="w-full max-w-xl rounded-lg bg-white shadow-xl" data-testid="ask-gate">
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <h2 className="text-base font-semibold text-slate-900">💬 Agent 提问</h2>
        </div>
        <div className="px-5 py-4">
          <p className="mb-3 text-sm text-slate-700" data-testid="gate-prompt">
            {gate.prompt}
          </p>
          {hasOptions ? (
            <div className="space-y-1.5" data-testid="gate-options">
              {gate.options!.map((opt) => (
                <label
                  key={opt}
                  className={`flex cursor-pointer items-center gap-2 rounded border px-3 py-1.5 text-sm ${
                    selected === opt
                      ? "border-indigo-400 bg-indigo-50 text-indigo-900"
                      : "border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <input
                    type="radio"
                    name="gate-option"
                    value={opt}
                    checked={selected === opt}
                    onChange={() => setSelected(opt)}
                    disabled={submitting}
                  />
                  <span>{opt}</span>
                </label>
              ))}
            </div>
          ) : (
            <textarea
              className="w-full rounded border border-slate-200 p-2 text-sm focus:border-indigo-400 focus:outline-none disabled:opacity-50"
              rows={3}
              placeholder="输入回答…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              disabled={submitting}
              data-testid="gate-textarea"
            />
          )}
          {error && (
            <p className="mt-2 text-xs text-red-600" data-testid="gate-error">
              提交失败：{error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-3">
          <button
            type="button"
            disabled={submitting}
            onClick={handleSubmit}
            className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-700 disabled:opacity-50"
            data-testid="gate-submit"
          >
            {submitting ? "提交中…" : "提交回答"}
          </button>
        </div>
      </div>
    </div>
  );
}
