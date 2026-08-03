// hooks/use-group-by.ts —— 分组维度 state + localStorage 持久（SPEC §10.8/AC-24）。
//
// 契约：
//   - ``GroupBy = "none" | "status" | "project" | "workflow" | "time"``。
//   - 默认 ``"status"``（看板自然轴；替换旧 groupBy on/off toggle，§10.8）。
//   - 持久 localStorage ``orca-runlist-groupby-v1``；损坏 try/catch 降级默认。
//   - 两视图（看板/列表）共用同一 dim（SPEC §10.8 末尾）。

import { useCallback, useState } from "react";

export type GroupBy = "none" | "status" | "project" | "workflow" | "time";

export const GROUP_BY_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: "none", label: "不分组" },
  { value: "status", label: "状态" },
  { value: "project", label: "项目" },
  { value: "workflow", label: "workflow" },
  { value: "time", label: "时间" },
];

const STORAGE_KEY = "orca-runlist-groupby-v1";
const DEFAULT: GroupBy = "status";

function isGroupBy(v: unknown): v is GroupBy {
  return (
    v === "none" ||
    v === "status" ||
    v === "project" ||
    v === "workflow" ||
    v === "time"
  );
}

function readStored(): GroupBy {
  if (typeof window === "undefined") return DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw == null) return DEFAULT;
    // 兼容 JSON 字符串 ``"\"status\""`` 与裸字符串 ``status`` 两种（与 use-runlist-view 同模式）。
    let parsed: unknown = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // 裸字符串 → 用 raw 本身。
    }
    if (isGroupBy(parsed)) return parsed;
    console.warn("[orca] groupBy localStorage 内容非法，回退默认 status", parsed);
    return DEFAULT;
  } catch (e) {
    console.warn("[orca] groupBy localStorage 读取失败，回退默认 status", e);
    return DEFAULT;
  }
}

function writeStored(v: GroupBy): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
  } catch (e) {
    console.warn("[orca] groupBy localStorage 写入失败（本次不持久化）", e);
  }
}

/** 分组维度 hook（两视图共用）。 */
export function useGroupBy() {
  const [groupBy, setGroupByState] = useState<GroupBy>(() => readStored());

  const setGroupBy = useCallback((next: GroupBy) => {
    setGroupByState(next);
    writeStored(next);
  }, []);

  return { groupBy, setGroupBy };
}
