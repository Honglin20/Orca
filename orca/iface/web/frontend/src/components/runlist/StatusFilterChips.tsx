// components/runlist/StatusFilterChips.tsx —— 状态 chips（SPEC §4/§6.3）。
//
// 契约：
//   - dot 色一律从 ``STATUS_DOT_BG[RunStatus]`` 取（DRY，D1 m2），不硬编码 bg-orca-*。
//   - 「运行中」chip 匹配 running||queued，dot 用 running 色 + tooltip「含排队中」。
//   - 选中态：``border-transparent bg-orca-accent text-[rgb(var(--app-bg))]``（暗模式自动反相，
//     避免 text-white 在浅 accent 上 AA 失败，§2.5）。

import {
  STATUS_DOT_BG,
  type RunStatus,
} from "@/components/layout/status-badge";

export type StatusFilter = "all" | "running" | "blocked" | "completed" | "failed";

interface ChipDef {
  key: StatusFilter;
  label: string;
  /** chip 激活态 dot 取色源 status（无则不显 dot） */
  sourceStatus?: RunStatus;
  /** chip 激活态 dot tooltip（可选） */
  dotTooltip?: string;
}

const CHIPS: ChipDef[] = [
  { key: "all", label: "全部" },
  {
    key: "running",
    label: "运行中",
    sourceStatus: "running",
    dotTooltip: "含排队中",
  },
  { key: "blocked", label: "待决策", sourceStatus: "blocked" },
  { key: "completed", label: "已完成", sourceStatus: "completed" },
  { key: "failed", label: "失败", sourceStatus: "failed" },
];

interface Props {
  active: StatusFilter;
  onChange: (s: StatusFilter) => void;
}

export function StatusFilterChips({ active, onChange }: Props) {
  return (
    <div className="flex items-center gap-1.5">
      {CHIPS.map((c) => {
        const isActive = active === c.key;
        const dotCls = c.sourceStatus ? STATUS_DOT_BG[c.sourceStatus] : undefined;
        return (
          <button
            key={c.key}
            type="button"
            data-testid={`status-chip-${c.key}`}
            onClick={() => onChange(c.key)}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs ${
              isActive
                ? "border-transparent bg-orca-accent text-[rgb(var(--app-bg))]"
                : "orca-border orca-text-muted orca-bg-surface hover:orca-bg-surface-2"
            }`}
          >
            {dotCls && (
              <span
                title={c.dotTooltip}
                className={`h-1.5 w-1.5 rounded-full ${
                  isActive ? "bg-[rgb(var(--app-bg)/0.8)]" : dotCls
                } ${c.sourceStatus === "running" ? "animate-pulse" : ""}`}
              />
            )}
            {c.label}
          </button>
        );
      })}
    </div>
  );
}
