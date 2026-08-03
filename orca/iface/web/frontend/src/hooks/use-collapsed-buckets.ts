// hooks/use-collapsed-buckets.ts —— 折叠态 localStorage 持久（SPEC §10.8/§6.7/AC-26）。
//
// 由 ``use-collapsed-projects`` 演进：存 ``Set<"dim:key">``，dim 切换各自独立折叠态。
// key 升级 ``orca-runlist-collapsed-v2``（v1 数据不兼容，直接弃；SPEC §10.8 末尾）。
//
// hydration 模式（沿用 v1 / commit 1f8e5cd 修复，**勿回归**）：
//   - 父级 ``known`` 在挂载时通常为空（``/api/runs`` 还没回）。若 ``useState`` 初值在此刻过滤
//     ``known``，会把持久态（如 ``["status:running"]``）过滤成空集，然后写回 effect 又把空集
//     覆盖回 localStorage → 持久态被永久清空。
//   - 因此：初值取空，等 ``known`` 首次非空时再 hydrate 一次（读 + ∩ known 惰性清理），并只在
//     hydrate 之后才允许写回。
//
// 调用方需传 ``known: Set<"dim:key">``（来自当前 groups），实现读时过滤 + 写时收口双向惰性清理。

import { useCallback, useEffect, useRef, useState } from "react";

const STORAGE_KEY = "orca-runlist-collapsed-v2";

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
 * 折叠态 hook（dim:key 泛化版，cross-dim 持久）。
 *
 * @param known 当前已知的 ``"dim:key"`` 集合（来自当前 groups；当前 dim 的惰性清理用）。
 * @returns ``{ collapsed, toggle, expandAll, collapseAll }``。
 *          - ``collapsed``：当前折叠的 ``"dim:key"`` 集合（**cross-dim**：含其它 dim 的历史折叠态）。
 *            UI 用 ``collapsed.has("${dim}:${key}")`` 检查——自然按 dim 作用域，互不干扰。
 *          - ``toggle(key)``：切换单个 ``"dim:key"``。
 *          - ``expandAll()`` / ``collapseAll()``：仅影响当前视图（基于 ``known``）。
 */
export function useCollapsedBuckets(known: Set<string>) {
  // hydration 模式（commit 1f8e5cd，勿回归）：见模块注释。初值空 → known 首次非空 hydrate → 仅 hydrate 后 write-back。
  const [collapsed, setCollapsedState] = useState<Set<string>>(() => new Set());

  // known 引用每次渲染可能变（父 useMemo 重算）。用 ref 跟最新，避免 stale 时不收口。
  const knownRef = useRef(known);
  knownRef.current = known;
  const hydratedRef = useRef(false);

  // 一次性 hydrate：known 首次非空时，从 localStorage 读 + **当前 dim** 惰性清理后 setState。
  // cross-dim 保留：其它 dim 的 entry 不在 known（仅含当前 dim）内，但**保留**——切回该 dim 时恢复。
  // 仅清当前 dim 的 stale entry（key 不在 known 内的当前 dim 项，如 backend 删了某 project）。
  useEffect(() => {
    if (hydratedRef.current || known.size === 0) return;
    hydratedRef.current = true;
    const stored = readStored();
    if (stored.size === 0) return;
    // 当前 dim 前缀（如 ``"project:"``）——known 中任一 entry 的 ``dim:`` 前缀。
    const firstKnown = [...known][0];
    const colonAt = firstKnown.indexOf(":");
    const currentDimPrefix = colonAt >= 0 ? firstKnown.slice(0, colonAt + 1) : "";
    const cleaned = new Set<string>();
    for (const n of stored) {
      if (currentDimPrefix && n.startsWith(currentDimPrefix)) {
        // 当前 dim entry：仅当仍在 known 内才保留（惰性清理 stale）。
        if (known.has(n)) cleaned.add(n);
      } else {
        // 其它 dim entry 或无前缀：保留（切回时恢复）。
        cleaned.add(n);
      }
    }
    if (cleaned.size > 0) setCollapsedState(cleaned);
  }, [known]);

  // 持久化副作用：collapsed 变 → 写 localStorage（**全集合**，cross-dim）。
  // 仅在 hydrate 之后允许写——否则会把初值（空集）覆盖回 storage，清空持久态（1f8e5cd regression）。
  // 惰性清理在 hydrate 时做（仅当前 dim）；这里不再二次过滤，避免误删其它 dim 的折叠态。
  useEffect(() => {
    if (!hydratedRef.current) return;
    writeStored(collapsed);
  }, [collapsed]);

  const toggle = useCallback((key: string) => {
    setCollapsedState((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const expandAll = useCallback(() => setCollapsedState(new Set()), []);
  const collapseAll = useCallback(
    () => setCollapsedState(new Set(knownRef.current)),
    [],
  );

  return { collapsed, toggle, expandAll, collapseAll };
}
