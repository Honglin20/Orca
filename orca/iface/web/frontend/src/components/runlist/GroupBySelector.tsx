// components/runlist/GroupBySelector.tsx —— 分组维度下拉（SPEC §10.8/AC-24）。
//
// 复用 ``SortMenu`` 样式语言（SPEC §10.8：「复用 SortMenu 样式」）：trigger 工具栏按钮；
// 下拉 portal 到 body 避裁剪；选中项尾部 Check size=12 orca-accent；点外面 / Esc 关闭。
//
// data-testid：trigger ``group-by-select``；菜单项 ``group-by-option-<dim>``。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronsUpDown } from "lucide-react";
import {
  GROUP_BY_OPTIONS,
  type GroupBy,
} from "@/hooks/use-group-by";

interface Props {
  value: GroupBy;
  onChange: (v: GroupBy) => void;
}

export function GroupBySelector({ value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // 关闭：点外面 / Esc（与 SortMenu 同模式）。
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t) || menuRef.current?.contains(t)) {
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
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );
  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const r = triggerRef.current.getBoundingClientRect();
    setCoords({ top: r.bottom + 4, left: r.left });
  }, [open]);

  const currentLabel =
    GROUP_BY_OPTIONS.find((o) => o.value === value)?.label ?? "状态";
  const isDefault = value === "status";

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        data-testid="group-by-select"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="分组方式"
        className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 orca-text-muted hover:orca-text hover:orca-bg-surface-2 ${
          !isDefault
            ? "border-orca-accent/30 bg-[rgb(var(--accent)/0.08)] orca-accent"
            : "orca-border"
        }`}
      >
        <ChevronsUpDown size={14} strokeWidth={1.5} aria-hidden />
        <span className="text-xs">{currentLabel}</span>
      </button>

      {open &&
        coords &&
        createPortal(
          <div
            ref={menuRef}
            data-testid="group-by-menu"
            role="menu"
            aria-label="选择分组维度"
            style={{
              position: "fixed",
              top: coords.top,
              left: coords.left,
              zIndex: 50,
            }}
            className="orca-bg-surface orca-border min-w-[180px] rounded-md border shadow-md py-1"
          >
            {GROUP_BY_OPTIONS.map((o) => {
              const selected = o.value === value;
              return (
                <button
                  key={o.value}
                  type="button"
                  role="menuitem"
                  data-testid={`group-by-option-${o.value}`}
                  onClick={() => {
                    onChange(o.value);
                    setOpen(false);
                  }}
                  className={`flex w-full items-center justify-between px-3 py-1.5 text-left text-sm hover:bg-[rgb(var(--accent)/0.08)] ${
                    selected ? "orca-text" : "orca-text-muted"
                  }`}
                >
                  <span>{o.label}</span>
                  {selected && (
                    <Check
                      size={12}
                      strokeWidth={1.5}
                      aria-hidden
                      className="orca-accent"
                    />
                  )}
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
