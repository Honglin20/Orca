// hooks/use-list-selection.ts —— 多选 hook（SPEC §3.2/§3.3/§5.5）。
//
// 契约：
//   - 选择集 = ``Set<run_id>``（D1 M7）——只存字符串 id，不存引用/index。
//   - refresh/WS 后**自动求交**：不在当前 ``runs`` 的 id 移除（防删除后残留）。
//   - **Shift+点击范围选**（同分组内 A→Shift+B）：用上次 anchor id + 当前 id 间的所有 id。
//   - 三级 API：``toggle(id, shift)`` 单行；``setMany(ids, mode)`` 分组全选；``clear()`` 全清。
//
// 时序不变量（SPEC §3.3）：切排序/chip/groupBy/清搜索 → 选择**保留**；仅「用户点取消选择」
// /「删除完成」/「页面重置」清空选择。
//
// 求交实现：useEffect 订阅 ``runIds``（来自父组件当前 runs 的 id 数组），若 selection 含 id 不
// 在 runIds 内 → 自动剔除。删空时 selection 也清。

import { useCallback, useEffect, useRef, useState } from "react";

export type SelectMode = "add" | "toggle" | "remove" | "replace";

export function useListSelection(runIds: string[]) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  // Shift 范围选 anchor：上次单点（非 Shift）的 id。null 时无 anchor，Shift 当作普通点。
  const anchorRef = useRef<string | null>(null);

  // 自动求交：runs 变化后 selection 中不在 runs 内的 id 剔除（D1 M7 / SPEC §3.3）。
  // 用 effect 而非 useMemo——选择是用户操作的 state，不是派生；effect 在 runs 变后修剪。
  useEffect(() => {
    const live = new Set(runIds);
    setSelected((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (live.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [runIds]);

  const toggle = useCallback((id: string, shiftKey: boolean, orderedIds: string[]) => {
    setSelected((prev) => {
      // Shift 范围选（SPEC §5.5）：anchor → id 之间所有 id 加入（不清既有选择）。
      if (shiftKey && anchorRef.current !== null && anchorRef.current !== id) {
        const a = orderedIds.indexOf(anchorRef.current);
        const b = orderedIds.indexOf(id);
        if (a >= 0 && b >= 0) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          const range = orderedIds.slice(lo, hi + 1);
          const next = new Set(prev);
          for (const x of range) next.add(x);
          return next;
        }
      }
      // 普通点：切；记 anchor（仅非 Shift 时更新）。
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      if (!shiftKey) anchorRef.current = id;
      return next;
    });
  }, []);

  // 分组/全选三级 checkbox：根据当前组内选中数决定 add/clear。
  // 返回 'all' | 'none' | 'partial' 供 checkbox indeterminate 用。
  const groupState = useCallback(
    (groupIds: string[]): "all" | "none" | "partial" => {
      if (groupIds.length === 0) return "none";
      let hit = 0;
      for (const id of groupIds) if (selected.has(id)) hit++;
      if (hit === 0) return "none";
      if (hit === groupIds.length) return "all";
      return "partial";
    },
    [selected],
  );

  const toggleGroup = useCallback((groupIds: string[]) => {
    setSelected((prev) => {
      const allSelected = groupIds.every((id) => prev.has(id));
      const next = new Set(prev);
      if (allSelected) {
        for (const id of groupIds) next.delete(id);
      } else {
        for (const id of groupIds) next.add(id);
      }
      return next;
    });
  }, []);

  const setMany = useCallback((ids: string[], mode: SelectMode) => {
    setSelected((prev) => {
      if (mode === "replace") return new Set(ids);
      const next = new Set(prev);
      if (mode === "add") for (const id of ids) next.add(id);
      else if (mode === "remove") for (const id of ids) next.delete(id);
      else for (const id of ids) next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setSelected(new Set());
    anchorRef.current = null;
  }, []);

  return { selected, toggle, toggleGroup, groupState, setMany, clear };
}
