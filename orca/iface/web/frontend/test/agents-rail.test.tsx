// test/agents-rail.test.tsx —— AgentsRail（SPEC §5.2 / §0 D5 / §6 D9）。
//
// 覆盖：
//   - 单一 timer（page-root useElapsedTickActive；AgentsRail 自身不开 setInterval）
//   - per-agent elapsed：running live tick / completed snap
//   - token 小字（input/output 折叠）
//   - 选中切换中栏会话（store.selectedNode）
//   - stall：running node 静默 > 阈值 → 琥珀「思考中」
//   - DAG 按钮点击 → 浮层 lazy 挂载

import { describe, expect, test, afterEach, beforeEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { AgentsRail } from "@/components/layout/AgentsRail";
import { useWorkflowStore } from "@/stores/workflow-store";
import { useElapsedTickActive, __testReset } from "@/hooks/use-elapsed-tick";

beforeEach(() => {
  __testReset();
  useWorkflowStore.getState().unloadRun();
  _seq = 100;
});

afterEach(() => {
  cleanup();
  __testReset();
  useWorkflowStore.getState().unloadRun();
  vi.restoreAllMocks();
});

function emitWorkflowStarted() {
  useWorkflowStore.getState().processEvent({
    seq: 1,
    type: "workflow_started",
    timestamp: 100,
    node: null,
    session_id: null,
    data: {
      workflow_name: "wf",
      topology: {
        entry: "n1",
        nodes: [
          { name: "n1", kind: "agent" },
          { name: "n2", kind: "agent" },
        ],
        routes: [{ from: "n1", to: "n2" }],
        parallel: [],
      },
    },
  });
}

let _seq = 100;

function emitNodeStarted(node: string, ts = 110) {
  useWorkflowStore.getState().processEvent({
    seq: _seq++,
    type: "node_started",
    timestamp: ts,
    node,
    session_id: node,
    data: {},
  });
}

function emitNodeCompleted(node: string, elapsed: number, ts = 200) {
  useWorkflowStore.getState().processEvent({
    seq: _seq++,
    type: "node_completed",
    timestamp: ts,
    node,
    session_id: node,
    data: { elapsed },
  });
}

// 包一层模拟 RunDetailPage：单一 useElapsedTickActive 在页根
function RailRoot({ active }: { active: boolean }) {
  useElapsedTickActive(active);
  return <AgentsRail />;
}

describe("AgentsRail —— 行渲染 + 选中切换（D2）", () => {
  test("DAG topology 中节点全部展示为 agent 行", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    expect(screen.getByTestId("agent-row-n1")).toBeInTheDocument();
    expect(screen.getByTestId("agent-row-n2")).toBeInTheDocument();
  });

  test("点击 agent 行 → store.selectedNode 切换", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    fireEvent.click(screen.getByTestId("agent-row-n2"));
    expect(useWorkflowStore.getState().selectedNode).toBe("n2");
  });
});

describe("AgentsRail —— per-agent elapsed（D5 snap）", () => {
  test("running node：⏱Ns（live tick，selector 取 now - startedAt）", () => {
    emitWorkflowStarted();
    emitNodeStarted("n1", 110);
    render(<RailRoot active={true} />);
    const elapsed = screen.getByTestId("agent-elapsed-n1").textContent ?? "";
    // 显示 ⏱ + 数字 + s（live tick：now - 110）
    expect(elapsed.startsWith("⏱")).toBe(true);
    expect(elapsed).toContain("s");
  });

  test("completed node：⏱Ns snap（不再随 now 变）", () => {
    emitWorkflowStarted();
    emitNodeStarted("n1", 110);
    emitNodeCompleted("n1", 45);
    render(<RailRoot active={false} />);
    expect(screen.getByTestId("agent-elapsed-n1").textContent).toContain("45s");
  });

  test("pending node：无 elapsed（空字串）", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    const elapsed = screen.getByTestId("agent-elapsed-n1").textContent ?? "";
    // 未启动 → 不显示 elapsed
    expect(elapsed).toBe("");
  });
});

describe("AgentsRail —— 单一 timer（SPEC §5.2）", () => {
  test("AgentsRail 不直接 setInterval（页根控制 active）", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    emitWorkflowStarted();
    // 不启 useElapsedTickActive —— AgentsRail 渲染也不应自启 timer
    render(<AgentsRail />);
    expect(setIntervalSpy).not.toHaveBeenCalled();
  });

  test("多 agent 行渲染只通过页根 active 控制 timer", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    emitWorkflowStarted();
    render(<RailRoot active={true} />);
    // 仅一个 setInterval（来自 useElapsedTickActive singleton，不来自 AgentsRail）
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
  });
});

describe("AgentsRail —— token 小字（agent_usage fold）", () => {
  test("agent_usage 累计到对应 node 的 input/output tokens", () => {
    emitWorkflowStarted();
    emitNodeStarted("n1");
    useWorkflowStore.getState().processEvent({
      seq: 300,
      type: "agent_usage",
      timestamp: 120,
      node: "n1",
      session_id: "n1",
      data: {
        cost_usd: 0.001,
        input_tokens: 1500,
        output_tokens: 800,
        reasoning_tokens: 50,
      },
    });
    render(<RailRoot active={false} />);
    const row = screen.getByTestId("agent-row-n1").textContent ?? "";
    // 1500 → 1.5k；800 → 800（< 1000 不缩写）
    expect(row).toContain("1.5k");
    expect(row).toContain("800");
    expect(row).toContain("🔤");
  });
});

describe("AgentsRail —— DAG 浮层 lazy 挂载（§5.7 / D2 lazy）", () => {
  test("默认不渲染 dag-overlay；点 DAG 按钮后挂载", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    expect(screen.queryByTestId("dag-overlay")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("dag-toggle"));
    expect(screen.getByTestId("dag-overlay")).toBeInTheDocument();
  });

  test("浮层背景点击 → 关闭", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    fireEvent.click(screen.getByTestId("dag-toggle"));
    const overlay = screen.getByTestId("dag-overlay");
    // 点背景（overlay 本身，非内部 stop-propagation 容器）
    fireEvent.click(overlay);
    expect(screen.queryByTestId("dag-overlay")).not.toBeInTheDocument();
  });

  test("D2 lazy：点 DAG 按钮 → 内部 WorkflowGraph 经 React.lazy 渲染（lazy chunk 解析后 workflow-graph testid 出现）", async () => {
    // workflow_started 已 emit → store.workflowDef 非 null → WorkflowGraph 渲染 ReactFlow。
    // React.lazy 在 vitest 下异步 resolve（dynamic import），用 findByTestId 等 lazy chunk
    // 解析完成。若 lazy import 路径错（如 module specifier 拼错）→ 永久 fallback，timeout fail。
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    fireEvent.click(screen.getByTestId("dag-toggle"));
    expect(screen.getByTestId("dag-overlay")).toBeInTheDocument();
    // Suspense fallback 先出现（验证 lazy 包装确实生效）
    expect(screen.getByTestId("dag-fallback")).toBeInTheDocument();
    // 等 lazy 解析完成 → workflow-graph testid 出现
    const graph = await screen.findByTestId("workflow-graph", undefined, {
      timeout: 1000,
    });
    expect(graph).toBeInTheDocument();
  });

  test("D2 lazy：拓扑未知（无 workflow_started）时浮层仍挂（lazy 解析后占位文本可见，不崩）", async () => {
    // 不 emit workflow_started → WorkflowGraph 渲染「等待 workflow_started 事件以获取拓扑…」占位。
    render(<RailRoot active={false} />);
    fireEvent.click(screen.getByTestId("dag-toggle"));
    expect(screen.getByTestId("dag-overlay")).toBeInTheDocument();
    // 等 lazy 解析后 WorkflowGraph 内部渲染占位文本
    const placeholder = await screen.findByText(/等待 workflow_started/, undefined, {
      timeout: 1000,
    });
    expect(placeholder).toBeInTheDocument();
  });
});
