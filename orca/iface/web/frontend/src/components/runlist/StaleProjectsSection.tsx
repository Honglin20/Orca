// components/runlist/StaleProjectsSection.tsx —— Stale projects 折叠区（SPEC §13.3 P3）。
//
// 只读折叠区——显示注册表里 path 已失效的项目 + ``tars gc`` 提示。默认折叠（仅当 stale > 0 时渲染）。

import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, FolderX } from "lucide-react";
import type { StaleProject } from "@/stores/run-list-store";

interface Props {
  items: StaleProject[];
}

export function StaleProjectsSection({ items }: Props) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;
  return (
    <section className="orca-border orca-bg-surface rounded border border-dashed p-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        {open ? (
          <ChevronDown size={14} strokeWidth={1.5} aria-hidden className="orca-text-faint" />
        ) : (
          <ChevronRight size={14} strokeWidth={1.5} aria-hidden className="orca-text-faint" />
        )}
        <AlertTriangle
          size={14}
          strokeWidth={1.5}
          aria-hidden
          className="text-orca-failed/70"
        />
        <span className="orca-text text-sm font-medium">Stale projects</span>
        <span className="orca-text-muted text-xs">· {items.length} 个失效注册项</span>
      </button>
      {open && (
        <div className="mt-2 space-y-1.5">
          <p className="orca-text-muted text-xs">
            这些注册项的 path 已失效（目录被删 / 重命名 / marker 丢失）。运行
            <code className="orca-text mx-1 font-mono">tars project rebuild</code>
            重建或
            <code className="orca-text mx-1 font-mono">tars gc</code>
            清理。
          </p>
          {items.map((s) => (
            <div
              key={s.project_id}
              className="orca-bg-app orca-border flex items-center gap-2 rounded border px-3 py-1.5"
            >
              <FolderX
                size={14}
                strokeWidth={1.5}
                aria-hidden
                className="orca-text-faint shrink-0"
              />
              <span className="orca-text-muted text-sm font-medium">{s.name}</span>
              <span className="orca-text-faint truncate font-mono text-xs">
                {s.path}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
