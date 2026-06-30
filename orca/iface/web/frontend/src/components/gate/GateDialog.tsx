// components/gate/GateDialog.tsx —— gate 富交互弹窗（SPEC §1.2）。
//
// 铁律 1（SPEC §0.1）：**gate 状态只读 store.gate**，本组件零本地 useState 存 gate。
// gate 是否渲染、是哪种 gate 全由 store.gate 派生。被抢答（store.gate→null）→ 本组件
// 自动 return null（弹窗消失），无需手动 close。
//
// 抢答（三通道竞速，铁律 3）：store.lastResolved 由 human_decision_resolved 设置 →
// ResolvedToast 显示「已被 [source] 答」。

import { useWorkflowStore } from "@/stores/workflow-store";
import { PermissionGate } from "./PermissionGate";
import { AskGate } from "./AskGate";
import { ResolvedToast } from "./ResolvedToast";

/**
 * 按 gate.source 分派渲染（PermissionGate / AskGate）。null gate → 不渲染。
 * 同时挂载 ResolvedToast（独立于 gate：gate 关闭后 toast 仍显示 2s）。
 */
export function GateDialog() {
  const gate = useWorkflowStore((s) => s.gate);

  return (
    <>
      <ResolvedToast />
      {gate &&
        (gate.source === "tool_permission" ? (
          <PermissionGate gate={gate} />
        ) : (
          <AskGate gate={gate} />
        ))}
    </>
  );
}
