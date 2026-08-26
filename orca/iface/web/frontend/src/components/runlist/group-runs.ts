// components/runlist/group-runs.ts —— 共享分桶（SPEC §10.8/AC-24，DRY 单出口）。
//
// 看板列与列表段**共用同一分桶逻辑**：``groupRuns(sortedRuns, dim) → RunBucket[]``。
// 这是 ``sort-runs.ts`` 的同层姊妹模块——排序先于分桶（调用方先 ``sortRuns`` 全局 stable 排序，
// 再 groupRuns 保 stable 顺序）。
//
// 桶顺序（SPEC web-board-cardgrid §4.1 修订；原 §10.8 status 顺序被 supersede）：
//   - ``status``：运行中 → 排队 → 待决策 → 失败 → 已完成（监控关注项置前，失败提前到完成前避免淹没；
//     运行中·待决策强调；限显统一由 ``CardGridSection.SECTION_LIMIT`` 接管）。
//   - ``project``：``project_name``（``source==="legacy"``→"Legacy"，空→"其它"）；alpha 排序 + Legacy/其它垫底。
//   - ``workflow``：``workflow_name``（空→"其它"）；alpha + 其它垫底。
//   - ``time``：今天 / 昨天 / 本周 / 更早 / 未知；按 ``started_at`` 逆序；无→未知。
//   - ``none``：单桶「全部」。
//
// 桶 ``status`` 字段仅 ``status`` dim 存在（用于 CardGridSection emphasize/ring）。其它 dim 的
// blocked 穿透提示（紫条）由调用方调 ``bucketHasBlocked(bucket)`` 判定。

import type { RunSummary } from "@/stores/run-list-store";
import {
  statusToRunStatus,
  type RunStatus,
} from "@/components/layout/status-badge";
import type { GroupBy } from "@/hooks/use-group-by";

export interface RunBucket {
  /** 桶内稳定 id（testid / 折叠 ``dim:key`` / React key 用）。各 dim 取值见模块注释。 */
  key: string;
  /** 显示名（中文）。status/time 用中文，project/workflow 用原名。 */
  label: string;
  /** 桶内已排序的 runs（顺序 = 调用方 ``sortRuns`` 的全局顺序）。 */
  runs: RunSummary[];
  /** 仅 ``status`` dim：桶对应的 RunStatus（emphasize/ring/dot 用）。 */
  status?: RunStatus;
}

/** status dim 的桶定义（SPEC web-board-cardgrid §4.1 顺序：运行中→排队→待决策→失败→已完成
 *  + 多 status 归桶，例如 cancelled → 失败桶；live-pending → 排队桶）。 */
interface StatusBucketDef {
  status: RunStatus;
  key: string;
  label: string;
  accept: ReadonlySet<RunStatus>;
}

const STATUS_BUCKETS: StatusBucketDef[] = [
  {
    status: "running",
    key: "running",
    label: "运行中",
    accept: new Set<RunStatus>(["running"]),
  },
  {
    status: "queued",
    key: "queued",
    label: "排队",
    accept: new Set<RunStatus>(["queued", "live-pending"]),
  },
  {
    status: "blocked",
    key: "blocked",
    label: "待决策",
    accept: new Set<RunStatus>(["blocked"]),
  },
  {
    status: "failed",
    key: "failed",
    label: "失败",
    accept: new Set<RunStatus>(["failed", "cancelled"]),
  },
  {
    status: "completed",
    key: "completed",
    label: "已完成",
    accept: new Set<RunStatus>(["completed"]),
  },
];

/** 列强调（色条 + 列头加粗 + 计数状态色）：运行中 / 待决策（SPEC §10.2，保留）。 */
export const EMPHASIS_STATUSES: ReadonlySet<RunStatus> = new Set([
  "running",
  "blocked",
]);
/** 非空时整列 ring：待决策（SPEC §10.2/§10.9，保留）。
 *  注：§2.3 I9 将 ring 触发条件泛化到 ``bucketHasBlocked``（任意 dim 含 blocked run），
 *  此常量仅作 status dim 的语义标记保留，CardGridSection 实际用 ``bucketHasBlocked`` 判定。 */
export const RING_STATUSES: ReadonlySet<RunStatus> = new Set(["blocked"]);
// SPEC web-board-cardgrid §4.1：``LIMITED_STATUSES`` / ``COMPLETED_LIMIT`` 已删除——
// 限显统一由 ``CardGridSection.SECTION_LIMIT=6`` 接管所有 section（不再区分 completed/failed）。

const MS_PER_DAY = 86400 * 1000;

/** 当日 0 点的 ms（运行时 ``Date.now()`` 可用，SPEC §10.8 time 维度）。 */
function startOfTodayMs(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function projectKey(r: RunSummary): string {
  const n = r.project_name;
  if (n && n.length > 0) return n;
  if (r.source === "legacy") return "Legacy";
  return "其它";
}

function workflowKey(r: RunSummary): string {
  const n = r.workflow_name;
  return n && n.length > 0 ? n : "其它";
}

type TimeKey = "today" | "yesterday" | "week" | "earlier" | "unknown";

function timeKey(r: RunSummary): TimeKey {
  const ts = r.started_at;
  // null / undefined / 非正数 → 未知（SPEC §10.8）。
  if (!ts || ts <= 0) return "unknown";
  const tsMs = ts * 1000; // started_at 是秒级 epoch
  const todayStart = startOfTodayMs();
  const yesterdayStart = todayStart - MS_PER_DAY;
  // 本周 = 近 7 天（不含今天/昨天），day-aligned：[todayStart - 7d, yesterdayStart)。
  const weekStart = todayStart - 7 * MS_PER_DAY;
  if (tsMs >= todayStart) return "today";
  if (tsMs >= yesterdayStart) return "yesterday";
  if (tsMs >= weekStart) return "week";
  return "earlier";
}

const TIME_LABELS: Record<TimeKey, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "本周",
  earlier: "更早",
  unknown: "未知",
};

/** time 桶顺序（逆序：最新在最前，未知沉底）。 */
const TIME_ORDER: TimeKey[] = ["today", "yesterday", "week", "earlier", "unknown"];

/** 纯 codepoint 比较（确定性，消除 localeCompare 跨环境 / CJK locale 差异）。 */
function compareCodepoint(a: string, b: string): number {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

/** project/workflow 桶间排序：alpha + 兜底桶（"Legacy" / "其它"）沉底。 */
function compareNamedBuckets(
  a: string,
  b: string,
  fallbackLabels: ReadonlySet<string>,
): number {
  const aFallback = fallbackLabels.has(a);
  const bFallback = fallbackLabels.has(b);
  if (aFallback && !bFallback) return 1;
  if (!aFallback && bFallback) return -1;
  if (aFallback && bFallback) {
    // 都在兜底集合内：确定性排序——「其它」最沉（无信息量），「Legacy」次之。
    if (a === "其它") return 1;
    if (b === "其它") return -1;
    return compareCodepoint(a, b);
  }
  return compareCodepoint(a, b);
}

/**
 * 按维度分桶。调用方应**先**用 ``sortRuns`` 做全局 stable 排序，再传进来——
 * 桶内顺序 = 输入顺序（stable），桶间顺序按 §10.8 各 dim 定义。
 */
export function groupRuns(runs: RunSummary[], dim: GroupBy): RunBucket[] {
  if (dim === "none") {
    return [{ key: "all", label: "全部", runs }];
  }

  if (dim === "status") {
    return STATUS_BUCKETS.map((b) => ({
      key: b.key,
      label: b.label,
      status: b.status,
      runs: runs.filter((r) => b.accept.has(statusToRunStatus(r.status))),
    }));
  }

  if (dim === "project") {
    const m = new Map<string, RunSummary[]>();
    for (const r of runs) {
      const k = projectKey(r);
      const arr = m.get(k);
      if (arr) arr.push(r);
      else m.set(k, [r]);
    }
    const PROJECT_FALLBACKS = new Set(["Legacy", "其它"]);
    const keys = [...m.keys()].sort((a, b) =>
      compareNamedBuckets(a, b, PROJECT_FALLBACKS),
    );
    return keys.map((k) => ({ key: k, label: k, runs: m.get(k)! }));
  }

  if (dim === "workflow") {
    const m = new Map<string, RunSummary[]>();
    for (const r of runs) {
      const k = workflowKey(r);
      const arr = m.get(k);
      if (arr) arr.push(r);
      else m.set(k, [r]);
    }
    const WF_FALLBACKS = new Set(["其它"]);
    const keys = [...m.keys()].sort((a, b) =>
      compareNamedBuckets(a, b, WF_FALLBACKS),
    );
    return keys.map((k) => ({ key: k, label: k, runs: m.get(k)! }));
  }

  // dim === "time"
  const m = new Map<TimeKey, RunSummary[]>();
  for (const r of runs) {
    const k = timeKey(r);
    const arr = m.get(k);
    if (arr) arr.push(r);
    else m.set(k, [r]);
  }
  return TIME_ORDER.filter((k) => m.has(k)).map((k) => ({
    key: k,
    label: TIME_LABELS[k],
    runs: m.get(k)!,
  }));
}

/** 桶是否含 blocked run（紫条穿透提示用，SPEC §10.8 末尾「不限 status 维度」）。 */
export function bucketHasBlocked(bucket: RunBucket): boolean {
  for (const r of bucket.runs) {
    if (statusToRunStatus(r.status) === "blocked") return true;
  }
  return false;
}
