// components/runlist/ProjectGroup.tsx —— 分组容器（无 border）+ 头 + run 列表（SPEC §1.2/§6.5）。
//
// 契约：
//   - 容器无 border，靠 ``bg-[rgb(var(--surface-2)/0.3)]`` 半透底 + 左侧 3px accent 色条表达层次。
//   - 含 blocked run → 整组左条变 skipped/50（用 STATUS_BAR_HEX.blocked = #a78bfa inline）。
//   - 头：folder icon + 名 + path + 聚合（运行中/待决策/总花费/最近）+ 三态全选 checkbox。
//   - 折叠态：单行头 + （blocked>0 时）「⚠ Y 待决策」紫色 mini pill（不被埋，§5.3）。
//   - 搜索穿透：``forceOpen`` 覆盖持久折叠（§5.2）；分组头右侧显搜索命中数。
//   - 三态全选 checkbox 用 indeterminate 半选态。

import { useMemo } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  AlertTriangle,
} from "lucide-react";
import type { RunSummary } from "@/stores/run-list-store";
import { STATUS_BAR_HEX } from "@/components/layout/status-badge";
import { fmtAgo, fmtCost } from "./format-helpers";
import { RunRow } from "./RunRow";

export interface ProjectGroupData {
  /** 显示名（项目名 / workflow 名 / 状态中文 / 时间中文）。 */
  name: string;
  /**
   * 桶稳定 id（testid ``group-<bucketKey>`` 用）。
   * status dim = "running" 等；project/workflow dim = 名字本身；time dim = "today" 等。
   * 缺省回退到 ``name``（兼容旧用例）。
   */
  bucketKey?: string;
  /**
   * 路径，仅 project dim 传（取该桶首个 run 的 ``project_id``）。
   * 其它 dim 缺省 → 不渲染 path 行。
   */
  path?: string;
  runs: RunSummary[];
}

interface Props extends ProjectGroupData {
  open: boolean;
  onToggleOpen: () => void;
  /** q 非空时传入（搜索穿透：显命中数） */
  searchHitCount?: number;
  q: string;
  /** 已选 id 集合（决定三态 checkbox 状态） */
  selectedIds: Set<string>;
  /** 切换单个 run 选择（带 shiftKey） */
  onToggleRun: (id: string, shiftKey: boolean) => void;
  /** 三态全选切换 */
  onToggleSelectAll: () => void;
  /** 全选态：'all'|'none'|'partial' */
  selectAllState: "all" | "none" | "partial";
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
  /** 删除 in-flight 的 id 集合（视觉 opacity-40） */
  deletingIds: Set<string>;
  /** shift 范围选用的有序 id 列表（组内当前展示顺序） */
  orderedRunIds: string[];
}

export function ProjectGroup({
  name,
  bucketKey,
  path,
  runs,
  open,
  onToggleOpen,
  searchHitCount,
  q,
  selectedIds,
  onToggleRun,
  onToggleSelectAll,
  selectAllState,
  onOpenRun,
  onDeleteRun,
  deletingIds,
  orderedRunIds,
}: Props) {
  // 聚合统计（运行中 / 待决策 / 总花费 / 最近）。
  const agg = useMemo(() => {
    let running = 0;
    let blocked = 0;
    let cost = 0;
    let latest: number | null = null;
    for (const r of runs) {
      const s = r.status;
      if (s === "running" || s === "queued") running++;
      if (s === "blocked") blocked++;
      cost += r.cost ?? 0;
      const sa = r.started_at ?? null;
      if (sa !== null && (latest === null || sa > latest)) latest = sa;
    }
    return { running, blocked, cost, latest };
  }, [runs]);

  const hasBlocked = agg.blocked > 0;

  // 三态 checkbox indeterminate（半选）：原生 input 通过 ref 设。
  // 注：React 不直接支持 indeterminate prop，需 ref 在渲染后设。
  const setIndeterminate = (el: HTMLInputElement | null) => {
    if (el) el.indeterminate = selectAllState === "partial";
  };

  const groupTestId = bucketKey ?? name;
  const hasPath = !!path;

  return (
    <section
      data-testid={`group-${groupTestId}`}
      className="relative rounded bg-[rgb(var(--surface-2)/0.3)]"
    >
      {/* 左侧色条：默认 accent/40；含 blocked run 时整组 skipped/50（用 STATUS_BAR_HEX.blocked inline） */}
      <div
        className="absolute inset-y-0 left-0 w-[3px] rounded-l"
        style={{
          backgroundColor: hasBlocked
            ? `${STATUS_BAR_HEX.blocked}80` // hex + 0x80 alpha ≈ 50%
            : "rgb(var(--accent) / 0.4)",
        }}
      />
      <div className="py-2 pl-4 pr-2">
        {/* 折叠/展开 + 项目头（点击切折叠） */}
        <div
          data-testid="group-header"
          role="button"
          tabIndex={0}
          onClick={onToggleOpen}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onToggleOpen();
            }
          }}
          className="flex cursor-pointer items-center gap-2 outline-none"
        >
          <span data-testid="group-collapse" className="orca-text-faint shrink-0">
            {open ? (
              <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
            ) : (
              <ChevronRight size={14} strokeWidth={1.5} aria-hidden />
            )}
          </span>
          <Folder size={16} strokeWidth={1.5} aria-hidden className="orca-accent shrink-0" />
          <span
            className={`text-sm font-semibold ${
              name === "Legacy" ? "orca-text-faint" : "orca-text"
            }`}
          >
            {name}
          </span>
          {!open && (
            <span className="orca-text-muted text-xs">· {runs.length} runs</span>
          )}
          {/* 折叠态显 blocked 计数（不被埋，§5.3） */}
          {!open && hasBlocked && (
            <span className="inline-flex items-center gap-1 rounded-full bg-orca-skipped/10 px-2 py-0.5 text-xs text-orca-skipped">
              <AlertTriangle size={12} strokeWidth={1.5} aria-hidden />
              {agg.blocked} 待决策
            </span>
          )}
          {/* path：仅 project dim 传（其它 dim path 缺省）；展开态第二行，折叠态同行截断 */}
          {hasPath &&
            (open ? (
              <span
                className="block w-full truncate font-mono text-xs orca-text-muted"
                title={path}
              >
                {path}
              </span>
            ) : (
              <span
                className="truncate font-mono text-xs orca-text-faint"
                title={path}
              >
                {path}
              </span>
            ))}
          {/* 搜索命中数（搜索穿透） */}
          {q && searchHitCount !== undefined && (
            <span className="orca-text-muted ml-auto shrink-0 text-xs">
              搜索：{q} · 命中 {searchHitCount}
            </span>
          )}
          {/* 三态全选 checkbox（点击不触发扬折叠，stopPropagation） */}
          <label
            className="ml-auto flex shrink-0 cursor-pointer items-center gap-1 text-xs orca-text-muted"
            onClick={(e) => e.stopPropagation()}
          >
            <input
              type="checkbox"
              ref={setIndeterminate}
              data-testid="group-select-all"
              checked={selectAllState === "all"}
              onChange={onToggleSelectAll}
              aria-label={`全选 ${name}`}
              className="h-4 w-4"
            />
          </label>
        </div>
        {/* 展开态：聚合 + path 第二行（path 在头行已显，这里只显聚合 metric 行） */}
        {open && (
          <div className="orca-text-muted mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 pl-6 text-xs">
            <span>{runs.length} runs</span>
            {agg.running > 0 && (
              <span className="inline-flex items-center gap-1">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-orca-running" />
                {agg.running} 运行中
              </span>
            )}
            {agg.blocked > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full bg-orca-skipped/10 px-2 py-0.5 text-orca-skipped">
                <AlertTriangle size={12} strokeWidth={1.5} aria-hidden />
                {agg.blocked} 待决策
              </span>
            )}
            <span>{fmtCost(agg.cost)} 总花费</span>
            <span>最近 {fmtAgo(agg.latest)}</span>
          </div>
        )}
      </div>
      {open && (
        <div className="space-y-1.5 px-2 pb-2">
          {runs.map((r) => (
            <RunRow
              key={r.run_id}
              run={r}
              q={q}
              selected={selectedIds.has(r.run_id)}
              deleting={deletingIds.has(r.run_id)}
              onToggleSelect={(shiftKey) => onToggleRun(r.run_id, shiftKey)}
              onOpen={() => onOpenRun(r.run_id)}
              onDelete={() => onDeleteRun(r.run_id)}
              orderedRunIds={orderedRunIds}
            />
          ))}
        </div>
      )}
    </section>
  );
}
