// components/runlist/ListTopBar.tsx —— 双行顶栏（SPEC §1.1/§6.1）。
//
// 品牌行（h-12，跨页一致）+ 工具行（h-10，列表页专属）。
// 主题按钮**真调** use-theme（修 D1 FATAL F1：旧实现只切本地 state，不写 localStorage 也不改 <html> class）。

import { useState } from "react";
import { Loader2, RefreshCw, Sun, Moon, Monitor } from "lucide-react";
import {
  currentTheme,
  nextTheme,
  setTheme,
  type Theme,
} from "@/hooks/use-theme";
import type { RunListView } from "@/hooks/use-runlist-view";
import type { GroupBy } from "@/hooks/use-group-by";
import { SearchInput } from "./SearchInput";
import { StatusFilterChips, type StatusFilter } from "./StatusFilterChips";
import { SortMenu } from "./SortMenu";
import { GroupBySelector } from "./GroupBySelector";
import { ShowEmptyToggle } from "./ShowEmptyToggle";
import type { SortState } from "@/hooks/use-list-sort";
import type { LucideIcon } from "lucide-react";
import {
  LayoutGrid,
  List as ListIcon,
} from "lucide-react";

const THEME_ICON: Record<Theme, LucideIcon> = {
  system: Monitor,
  dark: Moon,
  light: Sun,
};

interface Props {
  q: string;
  onQ: (v: string) => void;
  status: StatusFilter;
  onStatus: (s: StatusFilter) => void;
  /** 分组维度（SPEC §10.8，两视图共用同一 dim） */
  groupBy: GroupBy;
  onGroupBy: (v: GroupBy) => void;
  /** 空桶显隐（SPEC §10.9，两视图共用） */
  showEmpty: boolean;
  onShowEmpty: (v: boolean) => void;
  refreshing: boolean;
  onRefresh: () => void;
  sort: SortState;
  onSelectSortField: (field: SortState["field"]) => void;
  /** 当前视图（看板/列表）—— segmented toggle 状态 */
  view: RunListView;
  onView: (v: RunListView) => void;
}

export function ListTopBar({
  q,
  onQ,
  status,
  onStatus,
  groupBy,
  onGroupBy,
  showEmpty,
  onShowEmpty,
  refreshing,
  onRefresh,
  sort,
  onSelectSortField,
  view,
  onView,
}: Props) {
  // 主题：本地 state 镜像 module-level currentTheme()，点击 → setTheme（写 localStorage + 改 <html> class）
  // + setThemeState 同步 icon。SPEC §6.1 修复闭环。
  const [theme, setThemeState] = useState<Theme>(() => currentTheme());
  const ThemeIcon = THEME_ICON[theme];
  const onToggleTheme = () => {
    const t = nextTheme(theme);
    setTheme(t);
    setThemeState(t);
  };

  return (
    <header data-testid="topbar" className="orca-bg-surface orca-border orca-text shrink-0 border-b">
      {/* 品牌行 h-12（跨页一致，§1.1） */}
      <div
        data-testid="brand-row"
        className="flex h-12 items-center gap-3 px-4"
      >
        <span className="orca-accent text-lg font-semibold tracking-wider">TARS</span>
        <span className="orca-text-faint text-sm">/ Orca Runs</span>
        <span className="ml-auto flex items-center gap-2">
          {/* 视图切换 segmented toggle（SPEC §10.1/AC-19）：默认看板 */ }
          <span
            data-testid="view-toggle"
            className="orca-border inline-flex items-center gap-0.5 rounded border p-0.5"
            role="group"
            aria-label="视图切换"
          >
            <button
              type="button"
              data-testid="view-toggle-board"
              onClick={() => onView("board")}
              aria-pressed={view === "board"}
              title="看板视图"
              aria-label="看板视图"
              className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs ${
                view === "board"
                  ? "border-transparent bg-orca-accent text-[rgb(var(--app-bg))]"
                  : "orca-text-muted hover:orca-text hover:orca-bg-surface-2"
              }`}
            >
              <LayoutGrid size={12} strokeWidth={1.5} aria-hidden />
              看板
            </button>
            <button
              type="button"
              data-testid="view-toggle-list"
              onClick={() => onView("list")}
              aria-pressed={view === "list"}
              title="列表视图"
              aria-label="列表视图"
              className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs ${
                view === "list"
                  ? "border-transparent bg-orca-accent text-[rgb(var(--app-bg))]"
                  : "orca-text-muted hover:orca-text hover:orca-bg-surface-2"
              }`}
            >
              <ListIcon size={12} strokeWidth={1.5} aria-hidden />
              列表
            </button>
          </span>
          <button
            type="button"
            data-testid="refresh-btn"
            onClick={onRefresh}
            title="刷新"
            aria-label="刷新"
            className="orca-text-muted hover:orca-text inline-flex items-center rounded border orca-border px-2 py-1"
          >
            {refreshing ? (
              <Loader2 size={14} strokeWidth={1.5} className="animate-spin" aria-hidden />
            ) : (
              <RefreshCw size={14} strokeWidth={1.5} aria-hidden />
            )}
          </button>
          <button
            type="button"
            data-testid="theme-btn"
            onClick={onToggleTheme}
            title={`主题：${theme}`}
            aria-label="切换主题"
            className="orca-text-muted hover:orca-text inline-flex items-center rounded border orca-border px-2 py-1"
          >
            <ThemeIcon size={14} strokeWidth={1.5} aria-hidden />
          </button>
        </span>
      </div>
      {/* 工具行 h-10（列表页专属，§1.1） */}
      <div
        data-testid="tools-row"
        className="flex h-10 items-center gap-2 px-4"
      >
        <SearchInput value={q} onChange={onQ} />
        <div className="hidden md:block">
          <StatusFilterChips active={status} onChange={onStatus} />
        </div>
        <span className="ml-auto flex items-center gap-2">
          <SortMenu sort={sort} onSelectField={onSelectSortField} />
          {/*
            分组维度选择器（SPEC §10.8）+ 空桶显隐 toggle（§10.9）——
            两视图都显（dim/showEmpty 看板列表共用同一值）。
          */}
          <GroupBySelector value={groupBy} onChange={onGroupBy} />
          <ShowEmptyToggle value={showEmpty} onChange={onShowEmpty} />
        </span>
      </div>
    </header>
  );
}

