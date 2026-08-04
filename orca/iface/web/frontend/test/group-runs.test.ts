// test/group-runs.test.ts —— groupRuns 纯逻辑单测（SPEC web-board-cardgrid §9，7 条断言）。
//
// 断言意图（Rule 9）：
//   1. status dim 桶顺序 = [running, queued, blocked, failed, completed]（§4.1 修订）。
//   2. accept 集合：cancelled → failed；live-pending → queued。
//   3. project dim alpha + Legacy/其它垫底。
//   4. workflow dim alpha + 其它垫底。
//   5. time dim 5 桶逆序 + unknown 沉底。
//   6. dim=none 单桶「全部」。
//   7. use-list-sort 读 field==="cost" 回退 started_at（AC-B9 持久化回退）。

import { describe, expect, it } from "vitest";
import { groupRuns, bucketHasBlocked } from "@/components/runlist/group-runs";
import type { RunSummary } from "@/stores/run-list-store";
import { SORT_FIELDS } from "@/hooks/use-list-sort";

function mkRun(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: overrides.run_id ?? "r1",
    workflow_name: overrides.workflow_name ?? "demo",
    status: overrides.status ?? "completed",
    cost: overrides.cost ?? 0,
    elapsed: overrides.elapsed ?? 10,
    started_at: overrides.started_at ?? 1700000000,
    event_count: overrides.event_count ?? 5,
    project_name: overrides.project_name ?? "demo",
    project_id: overrides.project_id ?? "/tmp/demo",
    source: overrides.source ?? "in-process",
    ...overrides,
  };
}

describe("groupRuns（SPEC web-board-cardgrid §4.1/§9）", () => {
  // ① status dim 桶顺序 = [running, queued, blocked, failed, completed]
  it("status dim 桶顺序：running → queued → blocked → failed → completed", () => {
    const buckets = groupRuns([], "status");
    expect(buckets.map((b) => b.key)).toEqual([
      "running",
      "queued",
      "blocked",
      "failed",
      "completed",
    ]);
  });

  // ② accept 集合：cancelled → failed；live-pending → queued
  it("accept：cancelled 归 failed 桶；live-pending 归 queued 桶", () => {
    const buckets = groupRuns(
      [
        mkRun({ run_id: "rlp", status: "live-pending" }),
        mkRun({ run_id: "rca", status: "cancelled" }),
      ],
      "status",
    );
    const queued = buckets.find((b) => b.key === "queued")!;
    expect(queued.runs.length).toBe(1);
    expect(queued.runs[0].run_id).toBe("rlp");
    const failed = buckets.find((b) => b.key === "failed")!;
    expect(failed.runs.length).toBe(1);
    expect(failed.runs[0].run_id).toBe("rca");
  });

  // ③ project dim alpha + Legacy/其它垫底
  it("project dim：alpha 排序 + Legacy/其它垫底", () => {
    const buckets = groupRuns(
      [
        mkRun({ run_id: "rd", project_name: "demo" }),
        mkRun({ run_id: "ra", project_name: "alpha" }),
        mkRun({ run_id: "rl", source: "legacy", project_name: undefined }),
        mkRun({ run_id: "ro", project_name: undefined, source: "in-process" }),
      ],
      "project",
    );
    expect(buckets.map((b) => b.key)).toEqual(["alpha", "demo", "Legacy", "其它"]);
  });

  // ④ workflow dim alpha + 其它垫底
  it("workflow dim：alpha + 其它垫底", () => {
    const buckets = groupRuns(
      [
        mkRun({ run_id: "rb", workflow_name: "wf-b" }),
        mkRun({ run_id: "ra", workflow_name: "wf-a" }),
        mkRun({ run_id: "ro", workflow_name: undefined }),
      ],
      "workflow",
    );
    expect(buckets.map((b) => b.key)).toEqual(["wf-a", "wf-b", "其它"]);
  });

  // ⑤ time dim 5 桶逆序 + unknown 沉底
  it("time dim：今天/昨天/本周/更早/未知 逆序；无 started_at → 未知", () => {
    const MS_PER_DAY = 86400 * 1000;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const todayStartMs = today.getTime();
    const sec = (ms: number) => Math.floor(ms / 1000);

    const buckets = groupRuns(
      [
        mkRun({ run_id: "rt", started_at: sec(todayStartMs + 12 * 3600 * 1000) }),
        mkRun({ run_id: "ry", started_at: sec(todayStartMs - 12 * 3600 * 1000) }),
        mkRun({ run_id: "rw", started_at: sec(todayStartMs - 3 * MS_PER_DAY) }),
        mkRun({ run_id: "re", started_at: sec(todayStartMs - 30 * MS_PER_DAY) }),
        mkRun({ run_id: "ru", started_at: undefined }),
      ],
      "time",
    );
    expect(buckets.map((b) => b.key)).toEqual([
      "today",
      "yesterday",
      "week",
      "earlier",
      "unknown",
    ]);
  });

  // ⑥ dim=none 单桶「全部」
  it("none dim：单桶「全部」含所有 run", () => {
    const buckets = groupRuns(
      [
        mkRun({ run_id: "r1", status: "completed" }),
        mkRun({ run_id: "r2", status: "failed" }),
      ],
      "none",
    );
    expect(buckets.length).toBe(1);
    expect(buckets[0].key).toBe("all");
    expect(buckets[0].runs.length).toBe(2);
  });
});

describe("SORT_FIELDS 持久化回退（SPEC web-board-cardgrid §4.2/AC-B9）", () => {
  // ⑦ use-list-sort 读 field==="cost" 回退 started_at（SORT_FIELDS 不含 cost → 校验拒绝）
  it("SORT_FIELDS 不含 cost：readStored 校验自动拒绝旧 cost 持久化值", () => {
    // SORT_FIELDS 是持久化回退的校验数据源（use-list-sort readStored 的 .some 校验）。
    // 删 cost 项后，旧 localStorage {field:"cost"} 不在 SORT_FIELDS 内 → 校验拒绝 → 回退默认。
    // 注：TS 已不让 SortField 与 "cost" 直接比较（删后无 overlap）——说明编译期已强制；
    // 此处转 string 做运行时验证（防有人绕过类型、动态塞回 cost）。
    const hasCost = SORT_FIELDS.some((f) => (f.field as string) === "cost");
    expect(hasCost).toBe(false);
    // 确保回退目标字段（started_at）仍在 SORT_FIELDS 内。
    const hasStartedAt = SORT_FIELDS.some((f) => f.field === "started_at");
    expect(hasStartedAt).toBe(true);
  });
});

describe("bucketHasBlocked（紫条穿透提示）", () => {
  it("含 blocked run 的桶 → true；不含 → false", () => {
    const buckets = groupRuns(
      [
        mkRun({ run_id: "rb", status: "blocked", project_name: "demo" }),
        mkRun({ run_id: "rc", status: "completed", project_name: "demo" }),
      ],
      "project",
    );
    expect(bucketHasBlocked(buckets[0])).toBe(true);
    const emptyBuckets = groupRuns(
      [mkRun({ run_id: "rc", status: "completed", project_name: "demo" })],
      "project",
    );
    expect(bucketHasBlocked(emptyBuckets[0])).toBe(false);
  });
});
