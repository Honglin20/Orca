// components/layout/TopBar.tsx —— 顶栏：run + status + elapsed + WS 指示 + 主题开关（SPEC §5.1 / §0 D5 / §P5）。
//
// 纯渲染（铁律 6）：所有值 = store fold 派生 + selector 派生。WS 连接态与主题态是
// transport / UI 派生（非 tape 真相，SPEC §1.1 sanctioned exception + §7 双触发）。
// - status icon：``<StatusIcon/>``（lucide，P1 替换原 emoji ●/✓/✗/⊘/⏸）；P3 改 badge
// - elapsed（D5）：running wall-clock tick；完成 snap（铁律 1）
// - P3 runId 复制（hover <Copy/>，成功 <Check/> 1.5s 反馈）
// - P3 WS 连接点（useWsStatus：绿 connected / 紫 reconnecting / 红 disconnected）
// - P3 主题三态 toggle（system/dark/light，use-theme 持久化）
//
// 单一 timer 约束（SPEC §5.2）：``useElapsedNow()`` 订阅模块级 singleton tick，自身不开 setInterval。

import { useState } from "react";
import { Timer, Copy, Check, Sun, Moon, Monitor, ArrowLeft } from "lucide-react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { useElapsedNow } from "@/hooks/use-elapsed-tick";
import { selectWorkflowElapsed, formatElapsed } from "@/selectors";
import { StatusIcon } from "@/components/icons";
import { statusColor } from "./status-style";
import { useWsStatus, type WsConnStatus } from "@/hooks/use-ws-status";
import { currentTheme, nextTheme, setTheme, type Theme } from "@/hooks/use-theme";

/** WS 连接态 → 圆点配色（绿/紫/红；connecting/reconnecting 同紫，中间态）。 */
function wsDotClass(status: WsConnStatus): string {
  if (status === "connected") return "bg-orca-done";
  if (status === "disconnected") return "bg-orca-failed";
  return "bg-orca-skipped"; // connecting / reconnecting
}

const THEME_ICON: Record<Theme, typeof Sun> = {
  system: Monitor,
  dark: Moon,
  light: Sun,
};

export function TopBar({ runId }: { runId?: string }) {
  const status = useWorkflowStore((s) => s.status);
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const wsStatus = useWsStatus();

  const now = useElapsedNow();
  const workflowElapsed = useWorkflowStore((s) =>
    selectWorkflowElapsed(s, now)
  );

  const [copied, setCopied] = useState(false);
  const [theme, setThemeState] = useState<Theme>(currentTheme());

  const handleCopyRunId = async () => {
    if (!runId) return;
    try {
      await navigator.clipboard.writeText(runId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      console.error("[orca] runId 复制失败", err);
    }
  };

  const handleToggleTheme = () => {
    const t = nextTheme(theme);
    setTheme(t);
    setThemeState(t);
  };

  const ThemeIcon = THEME_ICON[theme];

  return (
    <header
      className="orca-bg-surface orca-border orca-text flex h-12 items-center gap-4 border-b px-4"
      data-testid="top-bar"
    >
      {/* 返回列表页（SPEC §13 §6.1：TopBar 加「← 返回」） */}
      <button
        type="button"
        onClick={() => {
          window.location.href = "/";
        }}
        title="返回 run 列表"
        className="orca-text-faint hover:orca-text inline-flex items-center rounded border orca-border px-2 py-1"
        aria-label="返回 run 列表"
      >
        <ArrowLeft size={14} strokeWidth={1.5} aria-hidden />
      </button>
      <span className="orca-accent text-lg font-semibold tracking-wider">TARS</span>
      {runId && (
        <button
          type="button"
          onClick={handleCopyRunId}
          title="复制 runId"
          data-testid="top-runid"
          className="orca-text-muted hover:orca-text inline-flex items-center gap-1 font-mono text-sm"
        >
          {runId.slice(0, 8)}
          {copied ? (
            <Check size={12} strokeWidth={1.5} aria-hidden className="text-orca-done" />
          ) : (
            <Copy size={12} strokeWidth={1.5} aria-hidden />
          )}
        </button>
      )}
      {workflowName && (
        <span className="orca-text-faint text-sm">{workflowName}</span>
      )}
      <span
        className={`inline-flex items-center gap-1 rounded border border-current px-1.5 py-0.5 text-xs ${statusColor(status)}`}
        data-testid="top-status"
      >
        <StatusIcon status={status} />
        {status}
      </span>
      <span
        className="orca-text-faint inline-flex items-center gap-1 text-sm"
        data-testid="top-elapsed"
      >
        <Timer size={14} strokeWidth={1.5} aria-hidden />
        {workflowElapsed !== null ? formatElapsed(workflowElapsed, "tenths") : "—"}
      </span>
      {/* 右侧：spacer + WS 指示 + 主题开关 */}
      <span className="ml-auto flex items-center gap-3">
        <span
          className="inline-flex items-center gap-1"
          data-testid="top-ws"
          title={`WS: ${wsStatus}`}
        >
          <span className={`h-2 w-2 rounded-full ${wsDotClass(wsStatus)}`} />
        </span>
        <button
          type="button"
          onClick={handleToggleTheme}
          title={`主题：${theme}（切换）`}
          data-testid="theme-toggle"
          className="orca-text-faint hover:orca-text inline-flex items-center"
          aria-label="切换主题"
        >
          <ThemeIcon size={15} strokeWidth={1.5} aria-hidden />
        </button>
      </span>
    </header>
  );
}
