// hooks/use-collapsed-projects.ts —— 折叠态 localStorage 持久化（SPEC §3.2/§6.7）。
//
// 契约：
//   - 持久化 key ``orca-runlist-collapsed-v1``（版本后缀，未来 schema 变可升 v2）。
//   - 读：try JSON.parse catch → console.warn + 默认空集（fail-loud + 降级，不崩 UI）。
//   - 写：try localStorage.setItem catch → 静默降级内存态（隐私模式 / 配额满）。
//   - 惰性清理：读到的 project 名不在当前 ``known`` → 忽略（避免历史脏数据堆积）。
//
// 调用方需传 ``known: Set<projectName>``（来自当前 groups），hook 在写时也只在 known 内持久化，
// 实现「读时过滤 + 写时收口」双向惰性清理。

import { useCallback, useEffect, useRef, useState } from "react";

const STORAGE_KEY = "orca-runlist-collapsed-v1";

function readStored(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((x): x is string => typeof x === "string"));
  } catch (e) {
    // fail-loud：localStorage 损坏 / 隐私模式 → console.warn + 降级空集，不阻断渲染。
    console.warn("[orca] collapsed localStorage 读取失败，回退空集", e);
    return new Set();
  }
}

function writeStored(set: Set<string>): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...set]));
    return true;
  } catch (e) {
    // 静默降级：写失败（隐私模式 / 配额满）→ 内存态继续工作，仅不持久化。
    console.warn("[orca] collapsed localStorage 写入失败（本次不持久化）", e);
    return false;
  }
}

/**
 * 折叠态 hook。
 *
 * @param known 当前已知的项目名集合（惰性清理用：读时过滤未知、写时只持久化已知）。
 * @returns `{ collapsed, toggle, setCollapsed, expandAll, collapseAll }`。
 *          - ``collapsed``：当前折叠的项目名集合（已过滤 unknown）。
 *          - ``toggle(name)``：切换单个。
 *          - ``setCollapsed(next)``：整体替换（用于「全部展开/折叠」）。
 */
export function useCollapsedProjects(known: Set<string>) {
  const [collapsed, setCollapsedState] = useState<Set<string>>(() => {
    const raw = readStored();
    // 读时惰性清理：保留 ∩ known。
    const cleaned = new Set<string>();
    for (const n of raw) if (known.has(n)) cleaned.add(n);
    return cleaned;
  });

  // known 引用每次渲染可能变（父 useMemo 重算）。用 ref 跟最新，避免 stale 写时不收口。
  const knownRef = useRef(known);
  knownRef.current = known;

  // 持久化副作用：collapsed 变 → 写 localStorage（仅 known 子集）。
  useEffect(() => {
    const knownNow = knownRef.current;
    const subset = new Set<string>();
    for (const n of collapsed) if (knownNow.has(n)) subset.add(n);
    writeStored(subset);
  }, [collapsed]);

  const toggle = useCallback((name: string) => {
    setCollapsedState((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const setCollapsed = useCallback((next: Set<string>) => {
    setCollapsedState(next);
  }, []);

  const expandAll = useCallback(() => setCollapsedState(new Set()), []);
  const collapseAll = useCallback(
    () => setCollapsedState(new Set(knownRef.current)),
    [],
  );

  return { collapsed, toggle, setCollapsed, expandAll, collapseAll };
}
