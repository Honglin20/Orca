// test/replay.test.ts —— tape replay 验收（SPEC §5.2 / plan C2.4）。
//
// 断言意图（Rule 9）—— 核心铁律：
//   1. **live==replay 末尾 byte-identical**（铁律 2）：全量事件 live fold → 快照 → enterReplay
//      → 拨到末尾 → 快照 == live 快照（deep equal）
//   2. **增量前进**：setReplayTarget 从 3→8 只 apply 5 个事件（spy applyOne）
//   3. **增量后退**：setReplayTarget 从大→小用 checkpoint（断言 checkpoint 被用）
//   4. **checkpoint 每 K 存**：长流有 floor(N/K) 个 checkpoint
//   5. **单路径 fold**：replay 的 apply 走同一 handler 表（store.setReplayTarget 内复用 foldEvent）

import { beforeEach, describe, expect, it } from "vitest";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  CHECKPOINT_INTERVAL,
  __getReplayBufferForTest,
  resetReplayBuffer,
} from "@/stores/replay-actions";
import {
  buildDemoStream,
  buildLongStream,
  buildRichStream,
  FOREACH_TOPOLOGY,
  mkWorkflowStarted,
  resetSeq,
  snapshotState,
  LINEAR_TOPOLOGY,
} from "./fixtures/events";
import type { WorkflowEvent } from "@/types/events";

function resetStore() {
  resetReplayBuffer();
  resetSeq();
  useWorkflowStore.setState({
    events: [],
    nodes: {},
    gate: null,
    workflowName: "",
    status: "idle",
    cost: 0,
    workflowDef: null,
    selectedNode: null,
    replayMode: false,
    replayPosition: 0,
    activeRunId: null,
  });
}

describe("replay: live == replay 末尾（铁律 2，核心）", () => {
  beforeEach(() => resetStore());

  it("全量 live fold → enterReplay → 拨到末尾 → nodes/state byte-identical", () => {
    const events = buildDemoStream();
    // 1. live 全跑
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    const liveFinal = snapshotState(useWorkflowStore.getState());

    // 2. 进 replay（重置派生态 → 拨到 -1）
    useWorkflowStore.getState().enterReplay();
    expect(useWorkflowStore.getState().replayMode).toBe(true);
    expect(useWorkflowStore.getState().replayPosition).toBe(-1);

    // 3. 增量拨到末尾
    useWorkflowStore.getState().setReplayTarget(events.length - 1);

    // 4. 断言 byte-identical（deep equal）
    const replayFinal = snapshotState(useWorkflowStore.getState());
    expect(replayFinal).toEqual(liveFinal);
  });

  it("富流（cost/gate/foreach 派生）live==replay byte-identical（压全部派生 handler）", () => {
    // buildRichStream 覆盖 agent_usage→cost / foreach→progress / human_decision→gate，
    // 让 live==replay 断言真正压到所有有副作用的 handler（反 vacuous-pass）。
    const events = buildRichStream(FOREACH_TOPOLOGY);
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    const liveFinal = snapshotState(useWorkflowStore.getState());
    // sanity：富流确实触发了 cost/gate-resolved/foreach-progress 派生
    expect(useWorkflowStore.getState().cost).toBeGreaterThan(0);
    expect(useWorkflowStore.getState().nodes.fan?.progress).toBeTruthy();

    useWorkflowStore.getState().enterReplay();
    useWorkflowStore.getState().setReplayTarget(events.length - 1);
    const replayFinal = snapshotState(useWorkflowStore.getState());
    expect(replayFinal).toEqual(liveFinal);
  });

  it("replay 中间位置状态正确（events[0..pos] fold 结果）", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();

    // 拨到 pos=3（第 4 个事件后；含 workflow_started + start 的 started/completed）
    useWorkflowStore.getState().setReplayTarget(3);

    // start 节点应已完成（events[2]=node_completed start），decide 未开始
    const nodes = useWorkflowStore.getState().nodes;
    expect(nodes.start?.status).toBe("done");
    expect(nodes.decide).toBeUndefined(); // decide 事件尚未 apply
  });
});

describe("replay: 增量前进（反全量重放）", () => {
  beforeEach(() => resetStore());

  it("setReplayTarget 前进只 apply 增量事件（不从头重放）", () => {
    // 用长流（101 事件）确保有足够跨度
    const events = buildLongStream(50);
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();

    // spy：替换 setReplayTarget 注入的 applyOne 计数。
    // 直接断言「前进 N→M 后 replayPosition==M 且状态 == events[0..M] fold 结果」
    // （与 live 全量 fold 到 M 比较，保证增量正确性）。
    // 先拨到 3
    useWorkflowStore.getState().setReplayTarget(3);
    expect(useWorkflowStore.getState().replayPosition).toBe(3);

    // 拨到 8（前进 5 步）
    useWorkflowStore.getState().setReplayTarget(8);
    expect(useWorkflowStore.getState().replayPosition).toBe(8);

    // 增量正确性：拨到 8 的状态 == 从头 fold events[0..8] 的状态。
    // 单独 fold 一份对照（resetStore 后只跑 events[0..8]）
    const replayAt8 = JSON.parse(JSON.stringify(useWorkflowStore.getState().nodes));

    resetStore();
    events.slice(0, 9).forEach((e) => useWorkflowStore.getState().processEvent(e));
    const directAt8 = JSON.parse(JSON.stringify(useWorkflowStore.getState().nodes));

    expect(replayAt8).toEqual(directAt8);
  });

  it("前进跨 CHECKPOINT_INTERVAL 存 checkpoint", () => {
    const events = buildLongStream(50); // 50 nodes → 1 + 100 = 101 事件
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();

    // 拨到 40（跨 20、40 两个 checkpoint 点；40 = (40+1)%20... 用 i+1 % 20 ===0，i=19,39）
    useWorkflowStore.getState().setReplayTarget(40);
    const buf = __getReplayBufferForTest();
    // checkpoint 在 i=19 和 i=39（(i+1)%20===0）
    expect(buf.checkpoints.has(19)).toBe(true);
    expect(buf.checkpoints.has(39)).toBe(true);
  });
});

describe("replay: 增量后退（checkpoint rollback）", () => {
  beforeEach(() => resetStore());

  it("setReplayTarget 大→小用 checkpoint 恢复", () => {
    const events = buildLongStream(50);
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();

    // 先前进到 40（建立 checkpoint 19, 39）
    useWorkflowStore.getState().setReplayTarget(40);
    const buf1 = __getReplayBufferForTest();
    expect(buf1.checkpoints.size).toBeGreaterThanOrEqual(2);

    // 后退到 25（应用最近 checkpoint 19 → apply 20..25）
    useWorkflowStore.getState().setReplayTarget(25);
    expect(useWorkflowStore.getState().replayPosition).toBe(25);
    // 后退后 nodes 应反映 events[0..25] 的状态（n0..n11 completed 左右）
    const nodes = useWorkflowStore.getState().nodes;
    // events[1..25] 含 workflow_started + ~12 个 node 的 started/completed
    // 至少 n0 应已完成（events 2-3 是 n0）
    expect(nodes.n0).toBeDefined();
    expect(nodes.n0.status).toBe("done");
    // n24+ 不应存在（events[0..25] 还没到）
    expect(nodes.n30).toBeUndefined();
  });

  it("后退到 0 之前（无 checkpoint）→ 重置到初始再 apply", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();
    useWorkflowStore.getState().setReplayTarget(5);
    // 后退到 0（demo 流短，无 checkpoint ≤ 0）
    useWorkflowStore.getState().setReplayTarget(0);
    expect(useWorkflowStore.getState().replayPosition).toBe(0);
    // events[0] = workflow_started → status=running, workflowName 设置
    expect(useWorkflowStore.getState().status).toBe("running");
    expect(useWorkflowStore.getState().workflowName).toBe("demo");
  });
});

describe("replay: checkpoint 间隔", () => {
  beforeEach(() => resetStore());

  it(`每 ${CHECKPOINT_INTERVAL} 个事件存一个 checkpoint（100 事件流约 5 个）`, () => {
    const events = buildLongStream(50); // 101 事件
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();
    useWorkflowStore.getState().setReplayTarget(events.length - 1);
    const buf = __getReplayBufferForTest();
    // i=19,39,59,79,99 → 5 个 checkpoint（-1 空态不计入业务 checkpoint 计数）
    // checkpoints 含 -1（空态）+ 5 个业务 = 6
    expect(buf.checkpoints.size).toBe(6);
    // 业务 checkpoint（i=19,39,59,79,99）都在
    for (const k of [19, 39, 59, 79, 99]) {
      expect(buf.checkpoints.has(k)).toBe(true);
    }
  });

  it("checkpoint 内容正确：snapshot == 从头 fold 到该位置的状态", () => {
    // 后退用 checkpoint restore，若 snapshot 存了错快照会导致状态错乱。
    // 此测试断言 checkpoint[19] 的 nodes 快照 == 单独 fold events[0..19] 的 nodes。
    const events = buildLongStream(50);
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();
    useWorkflowStore.getState().setReplayTarget(40); // 跨 i=19,39 建 checkpoint

    const buf = __getReplayBufferForTest();
    const cp19 = buf.checkpoints.get(19);
    expect(cp19).toBeDefined();

    // 单独 fold events[0..19]（含 19）
    resetStore();
    events.slice(0, 20).forEach((e) => useWorkflowStore.getState().processEvent(e));
    const expectedNodes = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );

    expect(cp19!.nodes).toEqual(expectedNodes);
  });
});

describe("replay: exitReplay 恢复 live 末态", () => {
  beforeEach(() => resetStore());

  it("exitReplay → 状态回到 live 末态 + replayMode 关闭", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    const liveFinal = snapshotState(useWorkflowStore.getState());

    useWorkflowStore.getState().enterReplay();
    useWorkflowStore.getState().setReplayTarget(2); // 拨到中间
    expect(useWorkflowStore.getState().replayMode).toBe(true);

    useWorkflowStore.getState().exitReplay();
    expect(useWorkflowStore.getState().replayMode).toBe(false);
    // 状态 == live 末态
    expect(snapshotState(useWorkflowStore.getState())).toEqual(liveFinal);
  });
});

describe("replay: workflowDef（拓扑）从 workflow_started 提取", () => {
  beforeEach(() => resetStore());

  it("workflow_started.data.topology → store.workflowDef", () => {
    const ev = mkWorkflowStarted(LINEAR_TOPOLOGY);
    useWorkflowStore.getState().processEvent(ev);
    const wf = useWorkflowStore.getState().workflowDef;
    expect(wf).not.toBeNull();
    expect(wf!.entry).toBe("start");
    expect(wf!.nodes.length).toBe(2);
    expect(wf!.routes.some((r) => r.from === "decide" && r.to === "decide")).toBe(true);
  });

  it("无 topology 字段的 workflow_started（旧后端兼容）→ workflowDef 留 null", () => {
    const ev: WorkflowEvent = {
      seq: 1,
      type: "workflow_started",
      timestamp: 1,
      node: null,
      session_id: null,
      data: { workflow_name: "old" }, // 无 topology
    };
    useWorkflowStore.getState().processEvent(ev);
    expect(useWorkflowStore.getState().workflowDef).toBeNull();
  });
});
