// stores/approval-store.ts —— in-session 权限审批 store（SPEC in-session-permission-hook §4.3）。
//
// **铁律（SPEC §4.3）**：
//   - 独立 store，**不复用** useWorkflowStore.gate（避免两真相源：审批 ≠ workflow gate，
//     后者从 tape fold 派生；前者是 broker 直接 push 的 CC 工具决策，非 workflow 事件）。
//   - snapshot 权威（N13）：WS ``approval_snapshot`` 帧是权威 pending 集——不在 snapshot 里的
//     本地卡清掉（防 broker 重启后 stale）。
//   - yolo 是 broker 全局开关，存此 store（跨 run 统一）。
//
// WS 帧（broker → 前端，kind:"approval"）：
//   - approval_requested {approval_id, run_id, tool, tool_input, created_at}
//   - approval_resolved  {approval_id, behavior, resolved_by}
//   - approval_resolved_late {approval_id, answer, ...}  — 仅审计，UI 不翻盘（N2）
//   - yolo_changed       {yolo}
//   - approval_snapshot  {approvals:[...], yolo}         — 权威 pending 集（B-5/N5/N13）
//
// WS 出站消息（前端 → broker）：
//   - request_approval_snapshot（connect / 重连后）
//   - approval_respond {approval_id, answer}
//   - approval_yolo    {yolo}

import { create } from "zustand";

export interface ApprovalEntry {
  approval_id: string;
  run_id: string;
  tool: string;
  /** 已 redact（broker 侧），直接展示。 */
  tool_input: Record<string, unknown> | unknown[];
  created_at: number;
}

export interface ApprovalState {
  /** 当前 pending 审批（按 approval_id 索引，便于 O(1) resolve/late-respond 查找）。 */
  pending: Record<string, ApprovalEntry>;
  /** broker 全局 yolo 开关（SPEC §3.3）。 */
  yolo: boolean;
  /** 最近一次 resolve 提示（驱动 toast：已被 [source] 答）。null = 无。 */
  lastResolved: { approval_id: string; behavior: string; resolved_by: string } | null;

  // === actions ===
  /** WS ``approval_*`` 帧统一入口（kind === "approval"）。 */
  ingestFrame: (frame: Record<string, unknown>) => void;
  /** 设 yolo（仅本地镜像；持久化由 broker 后端经 set_yolo 完成）。 */
  setYolo: (yolo: boolean) => void;
  /** 清空（unmount / 切 session 时）。 */
  reset: () => void;
}

/**
 * 单例 store。WS hook 在 onmessage 里判 ``frame.kind === "approval"`` 调
 * ``ingestFrame``；工具栏 / 审批弹窗读 ``pending`` 渲染。
 */
export const useApprovalStore = create<ApprovalState>((set) => ({
  pending: {},
  yolo: false,
  lastResolved: null,

  ingestFrame: (frame) => {
    const type = String(frame.type ?? "");
    if (type === "approval_snapshot") {
      // SPEC §4.3 N13：snapshot 权威——不在 snapshot 里的本地卡清掉。
      const approvals = (frame.approvals as ApprovalEntry[] | undefined) ?? [];
      const next: Record<string, ApprovalEntry> = {};
      for (const a of approvals) {
        if (a && typeof a.approval_id === "string") {
          next[a.approval_id] = a;
        }
      }
      set({
        pending: next,
        yolo: Boolean(frame.yolo),
      });
      return;
    }
    if (type === "approval_requested") {
      const id = String(frame.approval_id ?? "");
      if (!id) return;
      const entry: ApprovalEntry = {
        approval_id: id,
        run_id: String(frame.run_id ?? ""),
        tool: String(frame.tool ?? "<unknown>"),
        tool_input: (frame.tool_input as Record<string, unknown> | unknown[]) ?? {},
        created_at: Number(frame.created_at ?? Date.now() / 1000),
      };
      set((s) => ({ pending: { ...s.pending, [id]: entry } }));
      return;
    }
    if (type === "approval_resolved") {
      const id = String(frame.approval_id ?? "");
      if (!id) return;
      set((s) => {
        const next = { ...s.pending };
        delete next[id];
        return {
          pending: next,
          lastResolved: {
            approval_id: id,
            behavior: String(frame.behavior ?? ""),
            resolved_by: String(frame.resolved_by ?? ""),
          },
        };
      });
      return;
    }
    if (type === "approval_resolved_late") {
      // N2：仅审计可见，不翻盘 UI（pending 已在 approval_resolved 时清掉）。
      // 这里仅 console.warn（可观测），不改 store。
      // eslint-disable-next-line no-console
      console.warn(
        "[orca] approval_resolved_late（不翻盘，仅审计）:",
        frame,
      );
      return;
    }
    if (type === "yolo_changed") {
      set({ yolo: Boolean(frame.yolo) });
      return;
    }
    // 未知 approval 帧：fail loud 记 warning，不崩。
    // eslint-disable-next-line no-console
    console.warn("[orca] 未知 approval 帧类型:", frame.type, frame);
  },

  setYolo: (yolo) => set({ yolo }),

  reset: () => set({ pending: {}, yolo: false, lastResolved: null }),
}));

/**
 * 选当前 run 的 pending approvals（按 created_at 升序，UI 列表稳定顺序）。
 * 调用方传 runId；不在该 run 的卡 skip（broker 已 run-scoped 投递，这是双保险）。
 */
export function selectPendingForRun(
  pending: Record<string, ApprovalEntry>,
  runId: string | null | undefined,
): ApprovalEntry[] {
  const list = Object.values(pending);
  const filtered = runId
    ? list.filter((a) => a.run_id === runId)
    : list;
  return filtered.sort((a, b) => a.created_at - b.created_at);
}
