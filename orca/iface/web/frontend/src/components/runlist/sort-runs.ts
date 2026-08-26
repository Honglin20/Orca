// components/runlist/sort-runs.ts —— 列表/看板共享的 run comparator（SPEC §3.3 / DRY）。
//
// 提取自 RunListPage：列表组内排序与看板列内排序必须用同一 comparator，否则两视图相同 sort
// state 下顺序不一致。stable：末尾 run_id 字典序 tiebreaker（ES2019+ Array.sort 已 stable）。
//
// 调用方：``sortRuns(runs, sort)``。dir="desc" 等价于「按 cmp 反向」（reverse 在 sort 后调用，
// 保持同序项相对顺序）。

import type { RunSummary } from "@/stores/run-list-store";
import type { SortField, SortState } from "@/hooks/use-list-sort";

export function compareRuns(
  a: RunSummary,
  b: RunSummary,
  field: SortField,
): number {
  let cmp = 0;
  switch (field) {
    case "started_at": {
      const av = a.started_at ?? 0;
      const bv = b.started_at ?? 0;
      cmp = av - bv;
      break;
    }
    case "workflow_name":
      cmp = (a.workflow_name || "").localeCompare(b.workflow_name || "");
      break;
    case "status":
      cmp = (a.status || "").localeCompare(b.status || "");
      break;
    // SPEC web-board-cardgrid §4.2：cost 排序分支已删（SortField 删 cost 后 TS 强制）。
    case "elapsed":
      cmp = (a.elapsed ?? 0) - (b.elapsed ?? 0);
      break;
    case "event_count":
      cmp = (a.event_count ?? 0) - (b.event_count ?? 0);
      break;
  }
  if (cmp !== 0) return cmp;
  // tiebreaker：run_id 字典序（stable sort 保证）。
  return (a.run_id || "").localeCompare(b.run_id || "");
}

export function sortRuns(
  runs: RunSummary[],
  sort: SortState,
): RunSummary[] {
  const sorted = [...runs].sort((a, b) => compareRuns(a, b, sort.field));
  return sort.dir === "desc" ? sorted.reverse() : sorted;
  // 注：reverse 在 sort 后调用是稳定排序的正确做法（ES2019+ Array.sort 已 stable，
  // reverse 保持同序项的相对顺序，故 desc 等价于「按 cmp 反向」）。
}
