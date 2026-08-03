// components/runlist/BulkActionBar.tsx —— 选中非空时的批量操作条（SPEC §4/§5.5）。
//
// 契约：
//   - 作为 main 滚动区**首子**渲染，``sticky top-0 z-30``，贴顶栏下沿。
//   - 视觉：``-mx-6 px-6 py-2 orca-bg-surface orca-border border-b shadow-md rounded-md``。
//   - 内容：``已选 N · [🗑删除(N)] · [✕取消]``；首次 hint（Shift 范围选，3s 淡出，localStorage 记忆）。
//   - 失败 fail-loud：父级 toast；本组件仅触发 onBulkDelete。

import { useEffect, useState } from "react";
import { Trash2, X } from "lucide-react";

const HINT_KEY = "orca-runlist-shift-hint-seen-v1";

interface Props {
  selectedCount: number;
  totalCount: number;
  onBulkDelete: () => void;
  onClearSelection: () => void;
  onSelectAll: () => void;
}

export function BulkActionBar({
  selectedCount,
  totalCount,
  onBulkDelete,
  onClearSelection,
  onSelectAll,
}: Props) {
  // Shift 范围选 hint：首次见时显示 3s 后淡出，localStorage 记忆不再显。
  const [showHint, setShowHint] = useState(false);
  useEffect(() => {
    if (selectedCount === 0) return;
    try {
      const seen = window.localStorage.getItem(HINT_KEY);
      if (!seen) {
        setShowHint(true);
        const t = setTimeout(() => {
          setShowHint(false);
          try {
            window.localStorage.setItem(HINT_KEY, "1");
          } catch {
            // 隐私模式：本次会话已淡出即可，不阻断。
          }
        }, 3000);
        return () => clearTimeout(t);
      }
    } catch {
      // localStorage 不可用：不显 hint（静默降级）。
    }
    return;
  }, [selectedCount]);

  return (
    <div
      data-testid="bulk-bar"
      className="orca-bg-surface orca-border sticky top-0 z-30 -mx-6 rounded-md border-b px-6 py-2 shadow-md"
    >
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span className="orca-text-muted">
          已选{" "}
          <span className="orca-accent font-semibold tabular-nums">{selectedCount}</span>{" "}
          项
        </span>
        <button
          type="button"
          data-testid="bulk-delete-btn"
          onClick={onBulkDelete}
          disabled={selectedCount === 0}
          className="inline-flex items-center gap-1.5 rounded bg-orca-failed px-3 py-1.5 text-[rgb(var(--app-bg))] hover:opacity-90 disabled:opacity-40"
        >
          <Trash2 size={14} strokeWidth={1.5} aria-hidden />
          删除({selectedCount})
        </button>
        <button
          type="button"
          data-testid="clear-selection"
          onClick={onClearSelection}
          className="orca-text-muted hover:orca-text inline-flex items-center gap-1 rounded border orca-border px-2 py-1"
        >
          <X size={12} strokeWidth={1.5} aria-hidden />
          取消选择
        </button>
        <button
          type="button"
          data-testid="select-all"
          onClick={onSelectAll}
          className="orca-text-muted hover:orca-text"
        >
          全选当前筛选（{totalCount} 项）
        </button>
        {showHint && (
          <span className="orca-text-faint ml-auto transition-opacity duration-700">
            提示：Shift+点击可范围选
          </span>
        )}
      </div>
    </div>
  );
}
