// components/runlist/SortMenu.tsx —— 排序下拉（SPEC §4/§5.4/§6.4）。
//
// 契约：
//   - 触发器文案随状态：默认 ``↕ 排序``；选定后 ``↕ 开始时间 ↓``（字段名+方向箭头）。
//   - 点击字段名 → 切到该字段（默认 desc）；同字段二次点击反转方向；不循环回「无排序」。
//   - 下拉 portal 到 body 避裁剪；选中项尾部 Check size=12 orca-accent。
//   - 点外面 / Esc 关闭。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowDown, ArrowUp, Check, ChevronsUpDown } from "lucide-react";
import {
  SORT_FIELDS,
  type SortState,
} from "@/hooks/use-list-sort";

interface Props {
  sort: SortState;
  onSelectField: (field: SortState["field"]) => void;
}

export function SortMenu({ sort, onSelectField }: Props) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // 关闭：点外面 / Esc。
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (
        triggerRef.current?.contains(t) ||
        menuRef.current?.contains(t)
      ) {
        return;
      }
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // 计算 menu 定位（trigger 下沿）。
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);
  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const r = triggerRef.current.getBoundingClientRect();
    setCoords({ top: r.bottom + 4, left: r.left });
  }, [open]);

  const currentLabel = SORT_FIELDS.find((f) => f.field === sort.field)?.label ?? "排序";
  const isDefault = sort.field === "started_at" && sort.dir === "desc";

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        data-testid="sort-trigger"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="排序"
        className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 orca-text-muted hover:orca-text hover:orca-bg-surface-2 ${
          !isDefault
            ? "border-orca-accent/30 bg-[rgb(var(--accent)/0.08)] orca-accent"
            : "orca-border"
        }`}
      >
        <ChevronsUpDown size={14} strokeWidth={1.5} aria-hidden />
        <span className="text-xs">
          {isDefault ? "排序" : `${currentLabel}`}
        </span>
        {!isDefault &&
          (sort.dir === "desc" ? (
            <ArrowDown size={12} strokeWidth={1.5} aria-hidden />
          ) : (
            <ArrowUp size={12} strokeWidth={1.5} aria-hidden />
          ))}
      </button>

      {open && coords &&
        createPortal(
          <div
            ref={menuRef}
            data-testid="sort-menu"
            role="menu"
            aria-label="选择排序字段"
            style={{ position: "fixed", top: coords.top, left: coords.left, zIndex: 50 }}
            className="orca-bg-surface orca-border min-w-[180px] rounded-md border shadow-md py-1"
          >
            {SORT_FIELDS.map((f) => {
              const selected = f.field === sort.field;
              return (
                <button
                  key={f.field}
                  type="button"
                  role="menuitem"
                  data-testid={`sort-option-${f.field}`}
                  onClick={() => {
                    onSelectField(f.field);
                    setOpen(false);
                  }}
                  className={`flex w-full items-center justify-between px-3 py-1.5 text-left text-sm hover:bg-[rgb(var(--accent)/0.08)] ${
                    selected ? "orca-text" : "orca-text-muted"
                  }`}
                >
                  <span>{f.label}</span>
                  <span className="flex items-center gap-1">
                    {selected &&
                      (sort.dir === "desc" ? (
                        <ArrowDown size={12} strokeWidth={1.5} aria-hidden className="orca-accent" />
                      ) : (
                        <ArrowUp size={12} strokeWidth={1.5} aria-hidden className="orca-accent" />
                      ))}
                    {selected && <Check size={12} strokeWidth={1.5} aria-hidden className="orca-accent" />}
                  </span>
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
