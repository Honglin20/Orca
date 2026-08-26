// components/gate/ResolvedToast.tsx —— 抢答广播提示（SPEC §1.5）。
//
// 三通道竞速（铁律 3）：别的壳先答 → backend emit human_decision_resolved →
// store.gate=null + store.lastResolved={by, answer}。本组件读 lastResolved 显示
// 「已被 [source] 答：[answer]」，2 秒后自动清（local timer 清 lastResolved 视觉态）。
//
// 注意：lastResolved 是「最近一次已解决」快照，非真相源（真相在 tape）。本组件只做
// 短暂 toast 显示 —— 2s 后通过 setTimeout 清掉本地可见态（不写回 store，避免污染派生）。
// 实现方式：对 lastResolved 引用做 effect，2s 后置 local hidden 集合（覆盖已显示过的）。

import { useEffect, useState } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";

export function ResolvedToast() {
  const lastResolved = useWorkflowStore((s) => s.lastResolved);
  // hidden 记录已显示过的 lastResolved 快照（按 by+answer 去重），2s 后加入 → 隐藏。
  // 用快照字符串作 key 避免重复弹（同一 resolved 事件 re-render 不重置 timer）。
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!lastResolved) return;
    const key = `${lastResolved.by}|${lastResolved.answer}`;
    if (hidden.has(key)) return; // 已显示过（timer 跑着或已结束）
    const timer = window.setTimeout(() => {
      setHidden((prev) => new Set(prev).add(key));
    }, 2000);
    // 清理：组件卸载或 lastResolved 变 → 取消 timer（防 leak，SPEC 铁律）
    return () => window.clearTimeout(timer);
  }, [lastResolved, hidden]);

  if (!lastResolved) return null;
  const key = `${lastResolved.by}|${lastResolved.answer}`;
  if (hidden.has(key)) return null;

  return (
    <div
      // P0b 白名单（intentional inverse）：``bg-slate-900`` 是 white-on-dark toast 强对比
      // 浮层（同 LogStream live badge / AgentsRail DAG overlay），不属于 surface scale，
      // 不替换为 ``orca-*`` token。``text-amber-300`` 是 dark 背景上的高亮 emphasis，
      // 同离 surface scale（与 LogStream LEVEL_TEXT_COLOR.warning 同语义独立真相源）。
      // P3 暗色机制收口时统一复核。
      className="fixed bottom-4 left-1/2 z-[60] -translate-x-1/2 rounded-lg bg-slate-900 px-4 py-2 text-sm text-white shadow-lg"
      data-testid="resolved-toast"
      role="status"
    >
      已被 <span className="font-medium text-amber-300">{lastResolved.by}</span> 答：
      <span className="ml-1 font-mono">{lastResolved.answer}</span>
    </div>
  );
}
