// components/runlist/ShowEmptyToggle.tsx —— 空桶显隐 toggle（SPEC §10.9/AC-25）。
//
// 复用工具栏按钮语言；active = §2.5 中选中态（``border-orca-accent/30`` + ``bg-[rgb(var(--accent)/0.08)]``）。
// 默认 off（隐藏 0-run 桶）。两视图都显（SPEC §10.9）。
//
// data-testid：``show-empty-toggle``。

import { EyeOff, Eye } from "lucide-react";

interface Props {
  value: boolean;
  onChange: (v: boolean) => void;
}

export function ShowEmptyToggle({ value, onChange }: Props) {
  const Icon = value ? Eye : EyeOff;
  return (
    <button
      type="button"
      data-testid="show-empty-toggle"
      onClick={() => onChange(!value)}
      title={value ? "隐藏空分组" : "显示空分组"}
      aria-label={value ? "隐藏空分组" : "显示空分组"}
      aria-pressed={value}
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 orca-text-muted hover:orca-text hover:orca-bg-surface-2 ${
        value
          ? "border-orca-accent/30 bg-[rgb(var(--accent)/0.08)] orca-accent"
          : "orca-border"
      }`}
    >
      <Icon size={14} strokeWidth={1.5} aria-hidden />
      <span className="text-xs">空</span>
    </button>
  );
}
