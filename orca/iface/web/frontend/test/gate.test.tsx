// test/gate.test.tsx —— gate 弹窗测试（D1 验收）。
//
// 覆盖五条铁律 + SPEC §1.6 不乐观更新：
//   - tool_permission → 4 按钮 + 工具/参数
//   - agent_ask + options → radio；无 options → textarea
//   - 点批准 → POST /gate/respond body 正确
//   - 不乐观更新：答后 store.gate 仍非 null（等 resolved 事件）
//   - lastResolved → ResolvedToast 显示

import { describe, expect, vi, test, afterEach } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { GateDialog } from "@/components/gate/GateDialog";
import type { WebEvent } from "@/types/events";

let _seq = 1;
function gateRequestedEvent(
  source: string,
  data: Record<string, unknown>,
): WebEvent {
  return {
    seq: _seq++,
    type: "human_decision_requested",
    timestamp: Date.now() / 1000,
    node: "researcher",
    session_id: "researcher",
    data: { gate_id: "g1", prompt: "p?", source, ...data },
  };
}

const TOOL_PERMISSION_GATE = {
  gate_id: "g1",
  prompt: "批准 Bash 调用？",
  source: "tool_permission",
  context: { tool: "Bash", tool_input: { cmd: "ls -la" }, node: "researcher" },
};

const ASK_GATE_OPTIONS = {
  gate_id: "g2",
  prompt: "选哪个方案？",
  source: "agent_ask",
  options: ["方案A", "方案B", "方案C"],
};

const ASK_GATE_FREE = {
  gate_id: "g3",
  prompt: "请描述问题",
  source: "agent_ask",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useWorkflowStore.getState().unloadRun();
});

describe("GateDialog — source 分派", () => {
  test("null gate → 不渲染弹窗", () => {
    render(<GateDialog />);
    expect(screen.queryByTestId("gate-dialog")).not.toBeInTheDocument();
    expect(screen.queryByTestId("resolved-toast")).not.toBeInTheDocument();
  });

  test("tool_permission → PermissionGate（工具 + 参数 + 4 按钮）", () => {
    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("tool_permission", TOOL_PERMISSION_GATE));
    render(<GateDialog />);
    expect(screen.getByTestId("gate-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("permission-gate")).toBeInTheDocument();
    expect(screen.getByTestId("gate-tool").textContent).toContain("Bash");
    expect(screen.getByTestId("gate-tool-input").textContent).toContain("ls -la");
    // 4 按钮（铁律：PermissionGate 显示工具+4 按钮）
    expect(screen.getByTestId("gate-allow")).toBeInTheDocument();
    expect(screen.getByTestId("gate-deny")).toBeInTheDocument();
    expect(screen.getByTestId("gate-edit")).toBeInTheDocument();
    expect(screen.getByTestId("gate-skip")).toBeInTheDocument();
  });

  test("agent_ask + options → AskGate radio", () => {
    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("agent_ask", ASK_GATE_OPTIONS));
    render(<GateDialog />);
    expect(screen.getByTestId("ask-gate")).toBeInTheDocument();
    expect(screen.getByTestId("gate-prompt").textContent).toBe("选哪个方案？");
    expect(screen.getByTestId("gate-options")).toBeInTheDocument();
    expect(screen.getByText("方案A")).toBeInTheDocument();
    expect(screen.getByText("方案B")).toBeInTheDocument();
    expect(screen.getByText("方案C")).toBeInTheDocument();
    // radio 默认选第一项
    const radioA = screen.getAllByRole("radio")[0] as HTMLInputElement;
    expect(radioA.checked).toBe(true);
  });

  test("agent_ask 无 options → AskGate textarea", () => {
    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("agent_ask", ASK_GATE_FREE));
    render(<GateDialog />);
    expect(screen.getByTestId("ask-gate")).toBeInTheDocument();
    expect(screen.getByTestId("gate-textarea")).toBeInTheDocument();
    expect(screen.queryByTestId("gate-options")).not.toBeInTheDocument();
  });
});

describe("GateDialog — POST /gate/respond", () => {
  test("点批准 → POST /gate/respond body 正确（gate_id/answer/source=web）", async () => {
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true, gate_id: "g1" }), { status: 200 }),
    );

    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("tool_permission", TOOL_PERMISSION_GATE));
    render(<GateDialog />);
    screen.getByTestId("gate-allow").click();

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe("/gate/respond");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body));
    expect(body).toEqual({ gate_id: "g1", answer: "allow", source: "web" });
  });

  test("AskGate 提交自由文本 → POST body 含答案", async () => {
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true, gate_id: "g3" }), { status: 200 }),
    );

    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("agent_ask", ASK_GATE_FREE));
    render(<GateDialog />);
    const textarea = screen.getByTestId("gate-textarea") as HTMLTextAreaElement;
    // 用原生 setter 触发 onChange（React 受控组件需 nativeInputValueSetter）
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    setter?.call(textarea, "我的回答");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    screen.getByTestId("gate-submit").click();

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
    expect(body).toMatchObject({ gate_id: "g3", answer: "我的回答", source: "web" });
  });
});

describe("GateDialog — 不乐观更新（SPEC §1.6 铁律）", () => {
  test("答后 store.gate 仍非 null（等 resolved 事件才清）", async () => {
    // fetch mock 一个永不 resolve 的 promise（模拟 backend 在处理中），
    // 确保「fetch 返回后也不清 gate」可被断言。用 immediately-resolved 即可——
    // 关键是即便 fetch 成功，前端也不主动清 gate。
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true, gate_id: "g1" }), { status: 200 }),
    );

    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("tool_permission", TOOL_PERMISSION_GATE));
    render(<GateDialog />);
    screen.getByTestId("gate-allow").click();

    // 等 fetch 调用（说明点击已触发 POST）
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    // ── 关键断言：答后 store.gate 仍非 null（SPEC §1.6 不乐观更新）──
    expect(useWorkflowStore.getState().gate).not.toBeNull();
    // 弹窗仍可见
    expect(screen.getByTestId("gate-dialog")).toBeInTheDocument();
    // 按钮变 disabled（submitting UX 反馈）
    expect(screen.getByTestId("gate-allow")).toBeDisabled();
  });

  test("resolved 事件到达 → store.gate=null + 弹窗关 + ResolvedToast 显示", async () => {
    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("tool_permission", TOOL_PERMISSION_GATE));
    render(<GateDialog />);
    expect(screen.getByTestId("gate-dialog")).toBeInTheDocument();

    // 模拟别壳先答（三通道竞速广播）：emit human_decision_resolved（act 包裹让 React 同步 flush）
    act(() => {
      useWorkflowStore.getState().processEvent({
        seq: _seq++,
        type: "human_decision_resolved",
        timestamp: Date.now() / 1000,
        node: null,
        session_id: null,
        data: { gate_id: "g1", answer: "allow", resolved_by: "cli" },
      });
    });

    // 弹窗自动消失（store.gate→null 驱动）
    await waitFor(() =>
      expect(screen.queryByTestId("gate-dialog")).not.toBeInTheDocument(),
    );
    // lastResolved 设置（驱动 toast）
    expect(useWorkflowStore.getState().lastResolved).toEqual({
      by: "cli",
      answer: "allow",
    });
    // ResolvedToast 显示「已被 cli 答」
    const toast = screen.getByTestId("resolved-toast");
    expect(toast.textContent).toContain("cli");
    expect(toast.textContent).toContain("allow");
  });

  test("gate 组件零本地 useState 存 gate（铁律 1）", async () => {
    // 间接验证：连续两个不同 gate_id 的 requested 事件应反映在 UI（而非被旧 useState 卡住）
    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("tool_permission", TOOL_PERMISSION_GATE));
    const { unmount } = render(<GateDialog />);
    expect(screen.getByTestId("gate-tool").textContent).toContain("Bash");

    // 切到新 gate（不 unmount，模拟 store 派生变化）
    act(() => {
      useWorkflowStore.getState().processEvent({
        seq: _seq++,
        type: "human_decision_resolved",
        timestamp: Date.now() / 1000,
        node: null,
        session_id: null,
        data: { gate_id: "g1", answer: "allow", resolved_by: "web" },
      });
      useWorkflowStore.getState().processEvent(
        gateRequestedEvent("tool_permission", {
          gate_id: "g4",
          prompt: "p?",
          source: "tool_permission",
          context: { tool: "Write", tool_input: { path: "/a" }, node: "writer" },
        }),
      );
    });
    // 新 gate 的工具应显示（未被旧 useState 缓存）
    await waitFor(() =>
      expect(screen.getByTestId("gate-tool").textContent).toContain("Write"),
    );
    unmount();
  });

  test("AskGate gate 切换 → selected 重置到新 options 首项", async () => {
    // 验证 AskGate 的 useEffect([gate.gate_id]) 同步：直接换 gate（不经 null，模拟 store 派生变化）
    useWorkflowStore
      .getState()
      .processEvent(gateRequestedEvent("agent_ask", ASK_GATE_OPTIONS));
    const { unmount } = render(<GateDialog />);
    // 首项默认选中（方案A）
    const radioA = screen.getAllByRole("radio")[0] as HTMLInputElement;
    expect(radioA.checked).toBe(true);

    // 直接换 gate（新 gate_id + 新 options），selected 应重置到新 options 首项
    act(() => {
      useWorkflowStore.getState().processEvent(
        gateRequestedEvent("agent_ask", {
          gate_id: "g5",
          prompt: "p?",
          source: "agent_ask",
          options: ["X1", "X2", "X3"],
        }),
      );
    });
    // 新首项 X1 应被选中（旧 方案A 不残留）
    await waitFor(() => {
      const checked = screen.getAllByRole("radio").find(
        (r) => (r as HTMLInputElement).checked,
      ) as HTMLInputElement | undefined;
      expect(checked?.value).toBe("X1");
    });
    unmount();
  });
});
