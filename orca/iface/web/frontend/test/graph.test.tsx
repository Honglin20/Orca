// test/graph.test.tsx —— DAG 可视化验收（SPEC §5.1 / plan C1.4）。
//
// 断言意图（Rule 9）：
//   1. **节点数 == wf.nodes + parallel 组**（applyDagreLayout 覆盖全部节点）
//   2. **回环边** findBackEdges 识别 decide→decide，布局后回环节点 y 坐标合理（不乱排）
//   3. **增量更新** mergeNodeStatus 只改变化节点的 data（未变节点保持原引用）
//   4. **状态颜色** NODE_STATUS_HEX 5 色映射
//   5. **parallel 组渲染** applyDagreLayout 含 parallel 组节点 + branches 边

import { describe, expect, it } from "vitest";
import {
  applyDagreLayout,
  findBackEdges,
  markTakenEdges,
  mergeNodeStatus,
} from "@/components/graph/graph-layout";
import { NODE_STATUS_HEX } from "@/components/graph/constants";
import { LINEAR_TOPOLOGY, PARALLEL_TOPOLOGY, FOR_EACH_TOPOLOGY } from "./fixtures/events";

describe("graph-layout: findBackEdges", () => {
  it("识别自环边（decide→decide）为回环边", () => {
    // decide→decide 是自环，应被识别为 back-edge
    const back = findBackEdges(LINEAR_TOPOLOGY);
    expect(back.has("decide->decide")).toBe(true);
    expect(back.has("start->decide")).toBe(false); // 普通前向边
  });

  it("普通 DAG 无回环边", () => {
    const back = findBackEdges(PARALLEL_TOPOLOGY);
    expect(back.size).toBe(0);
  });

  it("识别 A→B→C→A 三角回环", () => {
    const topo = {
      entry: "a",
      nodes: [
        { name: "a", kind: "set" as const },
        { name: "b", kind: "set" as const },
        { name: "c", kind: "set" as const },
      ],
      routes: [
        { from: "a", to: "b" },
        { from: "b", to: "c" },
        { from: "c", to: "a" }, // 回到祖先 a → back-edge
      ],
      parallel: [],
    };
    const back = findBackEdges(topo);
    expect(back.has("c->a")).toBe(true);
    expect(back.size).toBe(1);
  });
});

describe("graph-layout: applyDagreLayout", () => {
  it("生成全部节点（含 $end 哨兵）", () => {
    const { nodes } = applyDagreLayout(LINEAR_TOPOLOGY);
    const ids = nodes.map((n) => n.id);
    expect(ids).toContain("start");
    expect(ids).toContain("decide");
    expect(ids).toContain("$end"); // route.to=$end → 注册为节点
    expect(nodes.length).toBe(3);
  });

  it("回环边布局：节点 y 坐标递增（TB 布局不乱翻）", () => {
    // TB（top→bottom）：上游节点 y < 下游节点 y。start→decide，故 start.y < decide.y。
    // 回环边 decide→decide 不应导致节点被排到祖先上方。
    const { nodes } = applyDagreLayout(LINEAR_TOPOLOGY);
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const startY = byId.get("start")!.position.y;
    const decideY = byId.get("decide")!.position.y;
    expect(startY).toBeLessThan(decideY); // start 在 decide 上方
  });

  it("回环边标记为 animated-back 类型（渲染保持原方向）", () => {
    const { edges } = applyDagreLayout(LINEAR_TOPOLOGY);
    const back = edges.find((e) => e.source === "decide" && e.target === "decide");
    expect(back).toBeDefined();
    expect(back!.type).toBe("animated-back");
    expect((back!.data as { isBackEdge: boolean }).isBackEdge).toBe(true);
  });

  it("parallel 组渲染：含组节点 + 组→branches 边", () => {
    const { nodes, edges } = applyDagreLayout(PARALLEL_TOPOLOGY);
    const ids = nodes.map((n) => n.id);
    expect(ids).toContain("grp"); // parallel 组作为节点
    expect(ids).toContain("a");
    expect(ids).toContain("b");
    // 组→branches 边
    const grpToA = edges.find((e) => e.source === "grp" && e.target === "a");
    expect(grpToA).toBeDefined();
    // fan→grp 边（route.to=grp）
    const fanToGrp = edges.find((e) => e.source === "fan" && e.target === "grp");
    expect(fanToGrp).toBeDefined();
  });

  it("节点初始 status=pending", () => {
    const { nodes } = applyDagreLayout(LINEAR_TOPOLOGY);
    for (const n of nodes) {
      expect((n.data as { status: string }).status).toBe("pending");
    }
  });
});

describe("graph-layout: mergeNodeStatus（增量更新，铁律 5）", () => {
  it("只更新变化节点的 data，未变节点保持原对象引用", () => {
    const { nodes: initial } = applyDagreLayout(LINEAR_TOPOLOGY);
    // 只有 decide 状态变了
    const status = { decide: { status: "done" } };
    const next = mergeNodeStatus(initial, status);
    // decide 引用变了（data 更新）
    const decideBefore = initial.find((n) => n.id === "decide");
    const decideAfter = next.find((n) => n.id === "decide");
    expect(decideAfter).not.toBe(decideBefore);
    expect((decideAfter!.data as { status: string }).status).toBe("done");
    // start 未变 → 保持原引用（增量更新核心，反全量 rebuild）
    const startBefore = initial.find((n) => n.id === "start");
    const startAfter = next.find((n) => n.id === "start");
    expect(startAfter).toBe(startBefore);
  });

  it("全部未变 → 返回原数组（零重渲染）", () => {
    const { nodes: initial } = applyDagreLayout(LINEAR_TOPOLOGY);
    // 空状态 map → 全未变
    const next = mergeNodeStatus(initial, {});
    expect(next).toBe(initial);
  });

  it("透传 progress（foreach/parallel widget 进度计数，SPEC §1.2 验收）", () => {
    // 反 M5 回归：mergeNodeStatus 必须把 NodeState.progress 透传到 WorkflowNodeData.progress，
    // 否则 foreach widget 永远显示不出 "3/7" 进度。
    const { nodes: initial } = applyDagreLayout(FOR_EACH_TOPOLOGY);
    const fan = initial.find((n) => n.id === "fan")!;
    expect(fan).toBeDefined();
    // 注入 progress（模拟 foreach_item_completed 派生）
    const next = mergeNodeStatus(initial, {
      fan: { status: "running", progress: "3/7" },
    });
    const fanAfter = next.find((n) => n.id === "fan")!;
    expect((fanAfter.data as { progress: string }).progress).toBe("3/7");
    expect((fanAfter.data as { status: string }).status).toBe("running");
  });

  it("progress 变化但 status 不变 → 仍触发更新", () => {
    const { nodes: initial } = applyDagreLayout(FOR_EACH_TOPOLOGY);
    // 先把 fan 设为 running + 1/7
    let next = mergeNodeStatus(initial, {
      fan: { status: "running", progress: "1/7" },
    });
    // 再只改 progress（status 仍 running）
    next = mergeNodeStatus(next, {
      fan: { status: "running", progress: "2/7" },
    });
    const fan = next.find((n) => n.id === "fan")!;
    expect((fan.data as { progress: string }).progress).toBe("2/7");
  });
});

describe("graph-layout: markTakenEdges（route_taken 高亮）", () => {
  it("标记走过的边 taken=true，未走过保持 false", () => {
    const { edges } = applyDagreLayout(LINEAR_TOPOLOGY);
    const taken = new Set<string>(["start->decide"]);
    const next = markTakenEdges(edges, taken);
    const e1 = next.find((e) => e.source === "start" && e.target === "decide")!;
    expect((e1.data as { taken: boolean }).taken).toBe(true);
    const e2 = next.find((e) => e.source === "decide" && e.target === "$end")!;
    expect((e2.data as { taken: boolean }).taken).toBe(false);
  });

  it("全未变 → 返回原数组", () => {
    const { edges } = applyDagreLayout(LINEAR_TOPOLOGY);
    const next = markTakenEdges(edges, new Set());
    expect(next).toBe(edges);
  });
});

describe("constants: NODE_STATUS_HEX（5 色，SPEC §1.5）", () => {
  it("5 色映射齐全（Y1：与 orca.* palette 同源 hex）", () => {
    expect(NODE_STATUS_HEX.pending).toMatch(/^#/)
    expect(NODE_STATUS_HEX.running).toBe("#5b8db8"); // 钢蓝 = orca.running / --accent
    expect(NODE_STATUS_HEX.done).toBe("#10b981"); // emerald = orca.done
    expect(NODE_STATUS_HEX.failed).toBe("#ef4444"); // red = orca.failed
    expect(NODE_STATUS_HEX.blocked).toBe("#a78bfa"); // violet = orca.skipped（与 statusColor 同源）
    expect(NODE_STATUS_HEX.pending).toBe("#94a3b8"); // slate-400 = orca.pending
  });
});
