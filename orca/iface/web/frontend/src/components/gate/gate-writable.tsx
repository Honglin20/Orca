// components/gate/gate-writable.tsx —— gate observe-only 共享守卫（SPEC web-attach §3 / §8 AC11）。
//
// SPEC §3 / §8 AC11：attached run（``writable=false``）→ **gate 模态**（不仅 PermissionGate）
// 显 observe-only 提示 + 禁用提交。旧版只在 PermissionGate 实现一次，AskGate 漏掉——本文件
// 抽出共享 hook + 共享提示组件，两处 gate 复用同一份真相（DRY）。

import { useWorkflowStore } from "@/stores/workflow-store";

/**
 * 读 store.writable（in-process=true / attached=false）。
 *
 * 用法：gate 组件据此 disable 提交按钮 + 跳过 handleSubmit 路径。
 */
export function useGateWritable(): boolean {
  return useWorkflowStore((s) => s.writable);
}

/**
 * Observe-only 提示条：仅当 ``writable=false`` 时渲染。
 *
 * PermissionGate / AskGate 共用——避免两处复制同一份 JSX 文案（DRY）。
 * testid ``gate-observe-only`` 由测试断言（attached run gate 场景）。
 */
export function GateObserveOnlyNotice() {
  const writable = useGateWritable();
  if (writable) return null;
  return (
    <p
      className="mt-2 text-xs text-amber-700"
      data-testid="gate-observe-only"
    >
      observe-only（attached run）—— 请在该 run 自己的 shell 作答
    </p>
  );
}
