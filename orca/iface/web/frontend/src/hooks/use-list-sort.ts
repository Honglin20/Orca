// hooks/use-list-sort.ts —— 排序 state + localStorage 持久（SPEC §3.2/§5.4）。
//
// 契约：
//   - ``SortField = "started_at"|"workflow_name"|"status"|"cost"|"elapsed"|"event_count"``。
//   - 默认 ``{field:"started_at", dir:"desc"}``。
//   - 持久 localStorage ``orca-runlist-sort-v1``；损坏 try/catch 降级默认。
//   - 切换语义：点字段名 → 切到该字段（默认 desc）；**同字段二次点击反转方向**；不循环回「无排序」。
//   - 排序必须 stable（ES2019+ Array.sort 已 stable；调用方在 comparator 末尾加 run_id tiebreaker 兜底）。

import { useCallback, useState } from "react";

export type SortField =
  | "started_at"
  | "workflow_name"
  | "status"
  | "cost"
  | "elapsed"
  | "event_count";

export type SortDir = "asc" | "desc";

export interface SortState {
  field: SortField;
  dir: SortDir;
}

export const SORT_FIELDS: { field: SortField; label: string }[] = [
  { field: "started_at", label: "开始时间" },
  { field: "workflow_name", label: "workflow 名称" },
  { field: "status", label: "状态" },
  { field: "cost", label: "花费" },
  { field: "elapsed", label: "耗时" },
  { field: "event_count", label: "事件数" },
];

const STORAGE_KEY = "orca-runlist-sort-v1";
const DEFAULT: SortState = { field: "started_at", dir: "desc" };

function readStored(): SortState {
  if (typeof window === "undefined") return DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      typeof (parsed as SortState).field === "string" &&
      ((parsed as SortState).dir === "asc" || (parsed as SortState).dir === "desc") &&
      SORT_FIELDS.some((f) => f.field === (parsed as SortState).field)
    ) {
      return parsed as SortState;
    }
    console.warn("[orca] sort localStorage 内容非法，回退默认", parsed);
    return DEFAULT;
  } catch (e) {
    console.warn("[orca] sort localStorage 读取失败，回退默认", e);
    return DEFAULT;
  }
}

function writeStored(s: SortState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch (e) {
    console.warn("[orca] sort localStorage 写入失败（本次不持久化）", e);
  }
}

export function useListSort() {
  const [sort, setSortState] = useState<SortState>(() => readStored());

  // 点字段名语义（SPEC §5.4）：切到该字段（默认 desc）；同字段二次点反转方向；不循环回「无排序」。
  const selectField = useCallback(
    (field: SortField) => {
      setSortState((prev) => {
        const next: SortState =
          prev.field === field
            ? { field, dir: prev.dir === "asc" ? "desc" : "asc" }
            : { field, dir: "desc" };
        writeStored(next);
        return next;
      });
    },
    [],
  );

  return { sort, selectField };
}
