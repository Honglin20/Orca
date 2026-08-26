// components/runlist/DeleteConfirmDialog.tsx —— 删除确认对话框（SPEC §4/§5.6/§5.7）。
//
// a11y 契约（§5.7 + NM2）：
//   - Esc → 取消；Enter（焦点在框内）→ 确认；focus trap（Tab 循环限框内，首末元素 wrap）。
//   - ``aria-describedby`` 关联描述 <p>；``aria-labelledby`` 关联 title。
//   - **关闭后焦点回触发元素**（NM2）：mount 时记录 ``document.activeElement``，unmount 还原。
//   - **背景 inert**：组件 createPortal 到 body；调用方在 page 根 div 上设 ``inert={open}``
//     让背后 UI 对屏幕阅读器/键盘不可达（React 19 原生支持 inert prop）。
//
// 视觉契约（§2.1/§2.2/§2.7）：
//   - 圆角 ``rounded``；阴影 ``shadow-lg``；遮罩 ``bg-[rgb(var(--text)/0.4)]``（不用 slate-*）。
//   - 批量预览 ≤5 + 「…还有 N」（可展开，modal 内滚动）。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Trash2 } from "lucide-react";

export interface DeleteTarget {
  runId: string;
  workflowName: string;
}

interface Props {
  /** null = 单条；非空数组 = 批量；空数组不应传入（调用方守门） */
  targets: DeleteTarget[] | { single: DeleteTarget };
  onCancel: () => void;
  onConfirm: () => void;
  /** 执行中（禁用按钮 + Loader2 + 「删除中…」） */
  busy?: boolean;
}

const PREVIEW_LIMIT = 5;

export function DeleteConfirmDialog({ targets, onCancel, onConfirm, busy }: Props) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const cancelBtnRef = useRef<HTMLButtonElement | null>(null);
  // NM2：记录触发元素，关闭时还原焦点。
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [expanded, setExpanded] = useState(false);

  const isBulk = !("single" in targets);
  const list = isBulk ? targets : [targets.single];
  const preview = expanded ? list : list.slice(0, PREVIEW_LIMIT);
  const hiddenCount = list.length - preview.length;

  // mount：记录触发元素 + focus 取消按钮（最安全选项，避免误确认）。
  useEffect(() => {
    if (typeof document !== "undefined") {
      previousFocusRef.current = document.activeElement as HTMLElement | null;
    }
    cancelBtnRef.current?.focus();
    // unmount：还原焦点到触发元素（若仍在 DOM 内）。
    return () => {
      const el = previousFocusRef.current;
      if (el && typeof el.focus === "function" && document.contains(el)) {
        el.focus();
      }
    };
  }, []);

  // Esc/Enter/Tab(focus trap) 全局键盘处理。
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
        return;
      }
      if (e.key === "Enter") {
        // Enter 在 dialog 内 → 确认；除非焦点在取消按钮上（那是取消语义）。
        if (document.activeElement === cancelBtnRef.current) return;
        e.preventDefault();
        onConfirm();
        return;
      }
      if (e.key === "Tab") {
        // focus trap：root 内可 focus 元素间循环（首末 wrap）。
        const focusables = root.querySelectorAll<HTMLElement>(
          'button, [href], input, [tabindex]:not([tabindex="-1"])',
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel, onConfirm]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      data-testid="delete-dialog-backdrop"
      className="fixed inset-0 z-50 flex items-center justify-center bg-[rgb(var(--text)/0.4)]"
      onClick={onCancel}
      role="presentation"
    >
      <div
        ref={rootRef}
        data-testid="delete-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="del-title"
        aria-describedby="del-desc"
        onClick={(e) => e.stopPropagation()}
        className="orca-bg-surface orca-border w-full max-w-sm rounded border p-5 shadow-lg"
      >
        <h3 id="del-title" className="orca-text text-base font-semibold">
          {isBulk ? `删除选中的 ${list.length} 个 run？` : "删除该 run？"}
        </h3>
        <p id="del-desc" className="orca-text-muted mt-2 text-sm">
          将永久删除 tape 与产物目录，不可恢复。
        </p>
        {isBulk && list.length > 0 && (
          <ul className="orca-text-muted mt-3 max-h-40 space-y-1 overflow-y-auto text-xs">
            {preview.map((t) => (
              <li key={t.runId} className="flex items-center gap-2">
                <span className="truncate">{t.workflowName}</span>
                <span className="orca-text-faint font-mono">{t.runId.slice(0, 8)}</span>
              </li>
            ))}
            {hiddenCount > 0 && !expanded && (
              <li>
                <button
                  type="button"
                  onClick={() => setExpanded(true)}
                  className="orca-accent hover:underline"
                >
                  …还有 {hiddenCount} 项
                </button>
              </li>
            )}
          </ul>
        )}
        {!isBulk && (
          <p className="orca-text-faint mt-2 font-mono text-xs">{list[0].runId}</p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            ref={cancelBtnRef}
            type="button"
            data-testid="cancel-delete"
            onClick={onCancel}
            disabled={busy}
            className="orca-text-muted hover:orca-text rounded border orca-border px-3 py-1.5 text-sm disabled:opacity-40"
          >
            取消
          </button>
          <button
            type="button"
            data-testid="confirm-delete"
            onClick={onConfirm}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded bg-orca-failed px-3 py-1.5 text-sm font-medium text-[rgb(var(--app-bg))] hover:opacity-90 disabled:opacity-40"
          >
            <Trash2 size={14} strokeWidth={1.5} aria-hidden className={busy ? "animate-pulse" : ""} />
            {busy ? "删除中…" : isBulk ? `删除(${list.length})` : "删除"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
