// hooks/use-show-empty.ts —— 空桶显隐 toggle + localStorage 持久（SPEC §10.9/AC-25）。
//
// 契约：
//   - 默认 ``false``（**隐藏 0-run 桶**，解决排队/待决策空列噪音）。
//   - 持久 localStorage ``orca-runlist-show-empty-v1``；损坏 try/catch 降级默认。
//   - 两视图共用（SPEC §10.9）。

import { useCallback, useState } from "react";

const STORAGE_KEY = "orca-runlist-show-empty-v1";
const DEFAULT = false;

function readStored(): boolean {
  if (typeof window === "undefined") return DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw == null) return DEFAULT;
    let parsed: unknown = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // 裸字符串 → 用 raw 本身。
    }
    if (typeof parsed === "boolean") return parsed;
    console.warn("[orca] showEmpty localStorage 内容非法，回退默认 false", parsed);
    return DEFAULT;
  } catch (e) {
    console.warn("[orca] showEmpty localStorage 读取失败，回退默认 false", e);
    return DEFAULT;
  }
}

function writeStored(v: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
  } catch (e) {
    console.warn("[orca] showEmpty localStorage 写入失败（本次不持久化）", e);
  }
}

/** 空桶显隐 hook（两视图共用）。 */
export function useShowEmpty() {
  const [showEmpty, setShowEmptyState] = useState<boolean>(() => readStored());

  const setShowEmpty = useCallback((next: boolean) => {
    setShowEmptyState(next);
    writeStored(next);
  }, []);

  return { showEmpty, setShowEmpty };
}
