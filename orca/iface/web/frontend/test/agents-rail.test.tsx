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
import { selectAgentGroups } from "@/selectors";
import { NODE_STATUS_HEX } from "@/components/graph/constants";
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

// ── P3：selectAgentGroups 阶段分组（SPEC §P3 方案 4 + P2-3 算法）────────────────────
// 纯函数 oracle：构造最小 state（不经过 store，避免事件 fold 噪声），直接断言分组。

/** 构造 selectAgentGroups 测试用最小 state（partial cast，跳过 store fold）。 */
function makeGroupsState(
  nodes: { name: string; kind: "agent" }[],
  routes: { from: string; to: string }[],
  nodesIndex: Record<
    string,
    { sessions: string[]; sessionEventCounts: Record<string, number>; sessionFirstTs: Record<string, number> }
  > = {}
) {
  return {
    workflowDef: {
      entry: nodes[0]?.name ?? "",
      nodes,
      routes,
      parallel: [],
    },
    nodes: {},
    events: [],
    nodesIndex,
  } as unknown as Parameters<typeof selectAgentGroups>[0];
}

describe("selectAgentGroups —— 阶段分组（P3 方案 4 + P2-3 算法）", () => {
  test("e3b8ad 拓扑（viz_round→hypothesizer back-route）→ Setup/Loop/Finalize 三组", () => {
    const state = makeGroupsState(
      [
        { name: "family_detect", kind: "agent" },
        { name: "baseline_measure", kind: "agent" },
        { name: "hypothesizer", kind: "agent" },
        { name: "engineer", kind: "agent" },
        { name: "structure_gate", kind: "agent" },
        { name: "evaluator", kind: "agent" },
        { name: "analyst", kind: "agent" },
        { name: "curator", kind: "agent" },
        { name: "viz_round", kind: "agent" },
        { name: "finalize", kind: "agent" },
        { name: "viz_finalize", kind: "agent" },
      ],
      [
        { from: "family_detect", to: "baseline_measure" },
        { from: "baseline_measure", to: "hypothesizer" },
        { from: "hypothesizer", to: "engineer" },
        { from: "engineer", to: "structure_gate" },
        { from: "structure_gate", to: "evaluator" },
        { from: "evaluator", to: "analyst" },
        { from: "analyst", to: "curator" },
        { from: "curator", to: "viz_round" },
        { from: "viz_round", to: "hypothesizer" }, // back-route（to 先于 from 声明）
        { from: "viz_round", to: "finalize" },
        { from: "finalize", to: "viz_finalize" },
        { from: "viz_finalize", to: "$end" },
      ]
    );
    const groups = selectAgentGroups(state);
    expect(groups.map((g) => g.group)).toEqual(["Setup", "Loop", "Finalize"]);
    expect(groups[0].agents.map((a) => a.node)).toEqual([
      "family_detect",
      "baseline_measure",
    ]);
    expect(groups[1].agents.map((a) => a.node)).toEqual([
      "hypothesizer",
      "engineer",
      "structure_gate",
      "evaluator",
      "analyst",
      "curator",
      "viz_round",
    ]);
    expect(groups[2].agents.map((a) => a.node)).toEqual([
      "finalize",
      "viz_finalize",
    ]);
  });

  test("无 back-route（线性拓扑）→ 单组 'Agents' 平铺 fallback", () => {
    const state = makeGroupsState(
      [
        { name: "n1", kind: "agent" },
        { name: "n2", kind: "agent" },
      ],
      [{ from: "n1", to: "n2" }]
    );
    const groups = selectAgentGroups(state);
    expect(groups.map((g) => g.group)).toEqual(["Agents"]);
    expect(groups[0].agents.map((a) => a.node)).toEqual(["n1", "n2"]);
  });

  test("$end route 不被当 back-route（to 不在 declIdx → 跳过）", () => {
    const state = makeGroupsState(
      [
        { name: "a", kind: "agent" },
        { name: "b", kind: "agent" },
      ],
      [
        { from: "a", to: "b" },
        { from: "b", to: "$end" },
      ]
    );
    const groups = selectAgentGroups(state);
    // b→$end 不是 back-route（$end 未声明）→ 单组平铺
    expect(groups.map((g) => g.group)).toEqual(["Agents"]);
  });

  test("Loop 组 agent 派生 iteration = sessionCount（P3 方案 5）", () => {
    const state = makeGroupsState(
      [
        { name: "entry", kind: "agent" },
        { name: "loopA", kind: "agent" },
        { name: "loopB", kind: "agent" },
        { name: "exit", kind: "agent" },
      ],
      [
        { from: "entry", to: "loopA" },
        { from: "loopA", to: "loopB" },
        { from: "loopB", to: "loopA" }, // back-route
        { from: "loopB", to: "exit" },
      ],
      {
        loopA: {
          // main + 3 sub → sessionCount = 3
          sessions: ["main", "s1", "s2", "s3"],
          sessionEventCounts: { main: 1, s1: 2, s2: 2, s3: 2 },
          sessionFirstTs: { main: 1, s1: 2, s2: 3, s3: 4 },
        },
      }
    );
    const groups = selectAgentGroups(state);
    const loop = groups.find((g) => g.group === "Loop");
    expect(loop).toBeDefined();
    const loopA = loop!.agents.find((a) => a.node === "loopA");
    expect(loopA?.sessionCount).toBe(3);
    expect(loopA?.iteration).toBe(3);
    // entry 在 Setup 组，不设 iteration
    const setup = groups.find((g) => g.group === "Setup");
    const entry = setup!.agents.find((a) => a.node === "entry");
    expect(entry?.iteration).toBeUndefined();
  });
});

// ── P3：视觉重做组件测试（色条 / 底色 / 分组 / 折叠 / 子 session 联动）──────────────

describe("AgentsRail —— P3 视觉重做（色条 / 底色 / 分组 / 折叠）", () => {
  test("底色统一：aside orca-bg-surface-2（非 orca-bg-surface）；无 w-56 GAP；w-full h-full", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    const rail = screen.getByTestId("agents-rail");
    // P0 token 收口：原 bg-slate-50 → orca-bg-surface-2（视觉相同，token 替换）。
    // ``toMatch`` 用负向 lookahead 避免子串匹配 ``orca-bg-surface-2``。
    expect(rail.className).toContain("orca-bg-surface-2");
    expect(rail.className).not.toMatch(/\borca-bg-surface\b(?!-)/);
    expect(rail.className).not.toContain("w-56");
    expect(rail.className).toContain("w-full");
    expect(rail.className).toContain("h-full");
  });

  test("状态色条：running 节点左竖条 = NODE_STATUS_HEX.running（DRY，无文字 icon）", () => {
    emitWorkflowStarted();
    emitNodeStarted("n1");
    render(<RailRoot active={false} />);
    const bar = screen.getByTestId("agent-bar-n1");
    expect(bar.getAttribute("data-status")).toBe("running");
    // NODE_STATUS_HEX.running = #3B82F6 → jsdom 规范化为 rgb(59, 130, 246)
    const hex = NODE_STATUS_HEX.running.toLowerCase();
    expect(bar.style.backgroundColor.toLowerCase()).toMatch(
      new RegExp(hex.replace("#", "") + "|rgb\\(59,\\s*130,\\s*246\\)")
    );
  });

  test("状态色条：completed 节点色 = NODE_STATUS_HEX.done", () => {
    emitWorkflowStarted();
    emitNodeStarted("n1");
    emitNodeCompleted("n1", 5);
    render(<RailRoot active={false} />);
    const bar = screen.getByTestId("agent-bar-n1");
    expect(bar.getAttribute("data-status")).toBe("done");
    const hex = NODE_STATUS_HEX.done.toLowerCase();
    expect(bar.style.backgroundColor.toLowerCase()).toMatch(
      new RegExp(hex.replace("#", "") + "|rgb\\(34,\\s*197,\\s*94\\)")
    );
  });

  test("无 back-route 拓扑 → 单组 'Agents'（fallback，data-testid=agent-group-Agents）", () => {
    emitWorkflowStarted();
    render(<RailRoot active={false} />);
    expect(screen.getByTestId("agent-group-Agents")).toBeInTheDocument();
  });

  test("sessionCount > 1 → 显示 ▸ N subs 折叠；展开后点子 session 联动 setSelectedSession", () => {
    emitWorkflowStarted(); // n1, n2
    emitNodeStarted("n1");
    // 给 n1 注入 2 个子 session（sessionCount = 2 > 1 触发折叠）
    useWorkflowStore.setState((s) => ({
      nodesIndex: {
        ...s.nodesIndex,
        n1: {
          sessions: ["main", "ses_aaa", "ses_bbb"],
          sessionEventCounts: { main: 1, ses_aaa: 2, ses_bbb: 3 },
          sessionFirstTs: { main: 1, ses_aaa: 2, ses_bbb: 3 },
        },
      },
    }));
    render(<RailRoot active={false} />);
    // 折叠按钮存在 + 文案含 "2 subs"（main 不计）
    const fold = screen.getByTestId("agent-fold-n1");
    expect(fold.textContent).toContain("2 subs");
    // 默认未展开 → 无子 session 列表
    expect(screen.queryByTestId("agent-subs-n1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-sub-n1-ses_aaa")).not.toBeInTheDocument();
    // 点击展开
    fireEvent.click(fold);
    expect(screen.getByTestId("agent-subs-n1")).toBeInTheDocument();
    expect(screen.getByTestId("agent-sub-n1-ses_aaa")).toBeInTheDocument();
    expect(screen.getByTestId("agent-sub-n1-ses_bbb")).toBeInTheDocument();
    // 点击子 session → selectedNode + selectedSession 联动（setSelectedNode 先把
    // selectedSession 设为第一个 sub ses_aaa，setSelectedSession 覆盖为 ses_bbb）
    fireEvent.click(screen.getByTestId("agent-sub-n1-ses_bbb"));
    expect(useWorkflowStore.getState().selectedNode).toBe("n1");
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_bbb");
  });

  test("sessionCount <= 1 → 不显折叠（既有用例零回归：n1 只 1 session 不出 fold）", () => {
    emitWorkflowStarted();
    emitNodeStarted("n1"); // session_id="n1" → 1 sub session
    render(<RailRoot active={false} />);
    expect(screen.queryByTestId("agent-fold-n1")).not.toBeInTheDocument();
    // 但 agent-row-n1 仍可点击选节点（既有交互不变）
    fireEvent.click(screen.getByTestId("agent-row-n1"));
    expect(useWorkflowStore.getState().selectedNode).toBe("n1");
  });

  test("Loop 组循环节点显示 R{iteration} 徽章；Setup/Finalize 不显（P3 方案 5 组件层）", () => {
    // 构造 back-route 拓扑：entry → loopA → loopB →(back) loopA / loopB → exit
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: {
        workflow_name: "wf-loop",
        topology: {
          entry: "entry",
          nodes: [
            { name: "entry", kind: "agent" },
            { name: "loopA", kind: "agent" },
            { name: "loopB", kind: "agent" },
            { name: "exit", kind: "agent" },
          ],
          routes: [
            { from: "entry", to: "loopA" },
            { from: "loopA", to: "loopB" },
            { from: "loopB", to: "loopA" }, // back-route
            { from: "loopB", to: "exit" },
          ],
          parallel: [],
        },
      },
    });
    // loopA 跑了 3 轮（main + 3 sub session → sessionCount = 3 → iteration = 3）
    useWorkflowStore.setState((s) => ({
      nodesIndex: {
        ...s.nodesIndex,
        loopA: {
          sessions: ["main", "s1", "s2", "s3"],
          sessionEventCounts: { main: 1, s1: 2, s2: 2, s3: 2 },
          sessionFirstTs: { main: 1, s1: 2, s2: 3, s3: 4 },
        },
      },
    }));
    render(<RailRoot active={false} />);
    // Loop 组 section + 徽章
    expect(screen.getByTestId("agent-group-Loop")).toBeInTheDocument();
    expect(screen.getByTestId("agent-group-Setup")).toBeInTheDocument();
    expect(screen.getByTestId("agent-group-Finalize")).toBeInTheDocument();
    const iter = screen.getByTestId("agent-iter-loopA");
    expect(iter.textContent).toBe("R3");
    // Setup / Finalize 节点无徽章
    expect(screen.queryByTestId("agent-iter-entry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-iter-exit")).not.toBeInTheDocument();
    // loopB 无子 session → 无徽章（sessionCount=0 → iteration=0 → showIter=false）
    expect(screen.queryByTestId("agent-iter-loopB")).not.toBeInTheDocument();
  });
});
