// components/gate/YoloToggle.tsx —— yolo 全局开关（SPEC in-session-permission-hook §3.3）。
//
// 工具栏切换：on → 所有 approval 立即 allow（不等、不超时）；仅保留 web 可见性（仍广播
// approval_requested + approval_resolved(yolo)）。SPEC §3.3 范围：broker **全局**开关
// （跨所有活跃 run）；多 run 并行时慎用——本期不做 per-run yolo（YAGNI）。
//
// 持久化在后端 broker（~/.orca/approval-yolo.json），前端只镜像；点击 → POST /approval/yolo。

import { useState } from "react";
import { useApprovalStore } from "@/stores/approval-store";

/**
 * YoloToggle：渲染按钮，点击调 POST /approval/yolo；本地镜像通过 store.yolo（broker 广播
 * yolo_changed 时同步）。
 */
export function YoloToggle() {
  const yolo = useApprovalStore((s) => s.yolo);
  const setYolo = useApprovalStore((s) => s.setYolo);
  const [pending, setPending] = useState(false);

  async function toggle() {
    if (pending) return;
    const next = !yolo;
    setPending(true);
    // 乐观镜像：让 UI 即时反映（broker yolo_changed 也会确认 / 纠正）。
    setYolo(next);
    try {
      const resp = await fetch("/approval/yolo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yolo: next }),
      });
      if (!resp.ok) {
        // 回滚乐观镜像。
        setYolo(!next);
        console.error(`[orca] POST /approval/yolo HTTP ${resp.status}`);
      }
    } catch (err) {
      setYolo(!next);
      console.error("[orca] POST /approval/yolo 网络错误", err);
    } finally {
      setPending(false);
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={pending}
      className={
        "rounded px-2.5 py-1 text-xs font-medium border orca-border disabled:opacity-50 " +
        (yolo
          ? "bg-orca-failed/20 text-orca-failed"
          : "orca-bg-surface-2 orca-text-muted hover:orca-bg-surface")
      }
      title={
        yolo
          ? "Yolo ON：所有权限请求立即批准（仅 web 可见）。点击关闭。"
          : "Yolo OFF：每个权限请求都要应答。点击开启后所有请求立即放行。"
      }
      data-testid="yolo-toggle"
    >
      {yolo ? "Yolo ON" : "Yolo"}
    </button>
  );
}
