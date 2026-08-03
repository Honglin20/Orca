// hooks/use-runlist-view.ts —— 看板/列表视图切换 + localStorage 持久（SPEC §10.1/§10.6 AC-19）。
//
// 契约：
//   - 持久 localStorage ``orca-runlist-view-v1`` ∈ ``"board"|"list"``；默认 ``"board"``。
//   - 读：try JSON.parse/string 校验，非法值 → console.warn + 默认 board。
//   - 写：try localStorage.setItem catch → 静默降级内存态（隐私模式 / 配额满）。
//   - SPEC §10.1：默认视图 = 看板（用户反馈「列表不像看板」，要一眼看清运行中/待决策）。

import { useCallback, useState } from "react";

export type RunListView = "board" | "list";

const STORAGE_KEY = "orca-runlist-view-v1";
const DEFAULT: RunListView = "board";

function isView(v: unknown): v is RunListView {
  return v === "board" || v === "list";
}

function readStored(): RunListView {
  if (typeof window === "undefined") return DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw == null) return DEFAULT;
    // 兼容历史存了 JSON 字符串 ``"\"board\""`` 与裸字符串 ``board`` 两种。
    let parsed: unknown = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // 裸字符串 → 用 raw 本身。
    }
    if (isView(parsed)) return parsed;
    console.warn("[orca] view localStorage 内容非法，回退默认 board", parsed);
    return DEFAULT;
  } catch (e) {
    console.warn("[orca] view localStorage 读取失败，回退默认 board", e);
    return DEFAULT;
  }
}

function writeStored(v: RunListView): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
  } catch (e) {
    console.warn("[orca] view localStorage 写入失败（本次不持久化）", e);
  }
}

export function useRunListView() {
  const [view, setViewState] = useState<RunListView>(() => readStored());

  const setView = useCallback((next: RunListView) => {
    setViewState(next);
    writeStored(next);
  }, []);

  return { view, setView };
}
