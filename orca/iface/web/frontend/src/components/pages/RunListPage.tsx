// components/pages/RunListPage.tsx
//
// 多 Run 列表页（SPEC §13 §6.1-6.3 / D9-D11 / mockup 复用）。
//
// 设计与 RunListPageMockup 同视觉，但接 store + API + WS 控制帧：
//   - mount → ``refresh()`` + 启 ~4s 轮询（store 内节流 2s）。
//   - unmount → ``reset()``（清 runs=[] + 停轮询 + 无 orphan task）。
//   - WS 控制帧 ``run_changed`` → store.onRunChanged（不进 reducer，§13 M-8）。
//   - 行点击 → navigate ``/runs/:runId``（详情页零改）。
//   - 删除二次确认 → store.deleteRun（乐观移除 + 失败回滚）。
//
// 铁律：本页**不** import workflow-store（R3）。

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  RefreshCw,
  Search,
  X,
  Trash2,
  ExternalLink,
  ChevronDown,
  ChevronRight,
  Inbox,
  Loader2,
  Sun,
  Moon,
  Monitor,
  List as ListIcon,
  Group,
  Coins,
  Timer,
  Activity,
} from "lucide-react";

import {
  startPolling,
  stopPolling,
  useRunListStore,
  type RunSummary,
} from "@/stores/run-list-store";
import {
  StatusBadge,
  STATUS_BAR_HEX,
  statusToRunStatus,
} from "@/components/layout/status-badge";

type StatusFilter = "all" | "running" | "blocked" | "completed" | "failed";

const CHIPS: { key: StatusFilter; label: string; dot?: string }[] = [
  { key: "all", label: "全部" },
  { key: "running", label: "运行中", dot: "bg-orca-running" },
  { key: "blocked", label: "待决策", dot: "bg-orca-skipped" },
  { key: "completed", label: "已完成", dot: "bg-orca-done" },
  { key: "failed", label: "失败", dot: "bg-orca-failed" },
];

function fmtElapsed(sec: number | undefined): string {
  if (!sec || sec < 0) return "—";
  const s = Math.floor(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h < 24) return mm ? `${h}h${mm}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const hh = h % 24;
  return hh ? `${d}d${hh}h` : `${d}d`;
}

function fmtCost(c: number | undefined): string {
  if (!c) return "$0.00";
  return `$${c.toFixed(2)}`;
}

function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const diff = (Date.now() / 1000 - ts);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

// ── 子组件 ─────────────────────────────────────────────────────────────────

interface ProjectGroup {
  name: string;
  path: string;
  runs: RunSummary[];
  defaultOpen: boolean;
}

function RunRowItem({
  run,
  onOpen,
  onDelete,
}: {
  run: RunSummary;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const rs = statusToRunStatus(run.status);
  const isBlocked = rs === "blocked";
  return (
    <div
      className={`group relative overflow-hidden rounded border orca-border orca-bg-surface shadow-sm transition-colors hover:orca-bg-surface-2 ${
        isBlocked ? "ring-1 ring-inset ring-orca-skipped/30" : ""
      }`}
    >
      <div
        className={`absolute inset-y-0 left-0 w-0.5 ${isBlocked ? "w-1" : ""}`}
        style={{ backgroundColor: STATUS_BAR_HEX[rs] }}
      />
      <button
        type="button"
        onClick={() => onOpen(run.run_id)}
        className="flex w-full items-center gap-4 px-4 py-3 pl-5 text-left"
      >
        <span className="w-24 shrink-0">
          <StatusBadge status={rs} />
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-medium orca-text">
              {run.workflow_name}
            </span>
            {isBlocked && (
              <span className="rounded orca-bg-surface-2 px-1.5 py-0.5 text-[10px] orca-text-muted">
                待决策
              </span>
            )}
          </span>
          <span className="flex items-center gap-2 font-mono text-[11px] orca-text-faint">
            <span>{run.run_id.slice(0, 18)}…</span>
            <span>·</span>
            <span>{fmtAgo(run.started_at)}</span>
          </span>
        </span>
        <span className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1 text-xs md:flex-nowrap">
          <Metric icon={Activity} value={run.progress ?? "?"} label="进度" />
          <Metric icon={Coins} value={fmtCost(run.cost)} label="花费" />
          <Metric icon={Timer} value={fmtElapsed(run.elapsed)} label="耗时" />
          <Metric
            icon={Activity}
            value={String(run.event_count ?? 0)}
            label="事件数"
          />
        </span>
      </button>
      <span className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onOpen(run.run_id);
          }}
          title="打开"
          className="orca-text-faint hover:orca-accent rounded p-1.5"
          aria-label="打开 run"
        >
          <ExternalLink size={14} strokeWidth={1.5} aria-hidden />
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete(run.run_id);
          }}
          title="删除"
          className="orca-text-faint hover:text-orca-failed rounded p-1.5 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
          aria-label="删除 run"
        >
          <Trash2 size={14} strokeWidth={1.5} aria-hidden />
        </button>
      </span>
    </div>
  );
}

function Metric({
  icon: Icon,
  value,
  label,
}: {
  icon: typeof Coins;
  value: string;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1 orca-text-faint" title={label}>
      <Icon size={12} strokeWidth={1.5} aria-hidden />
      <span className="tabular-nums">{value}</span>
    </span>
  );
}

function ProjectGroupView({
  name,
  path,
  runs,
  defaultOpen,
  onOpen,
  onDelete,
}: ProjectGroup & {
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const isLegacy = name === "Legacy";
  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-1 pb-1 text-left"
      >
        {open ? (
          <ChevronDown size={14} strokeWidth={1.5} aria-hidden className="orca-text-faint" />
        ) : (
          <ChevronRight size={14} strokeWidth={1.5} aria-hidden className="orca-text-faint" />
        )}
        <span className={`text-sm font-semibold ${isLegacy ? "orca-text-faint" : "orca-text"}`}>
          {name}
        </span>
        <span className="truncate font-mono text-[11px] orca-text-faint">{path}</span>
        <span className="orca-text-faint text-[11px]">· {runs.length} runs</span>
      </button>
      {open && (
        <div className="space-y-1.5 pt-1">
          {runs.map((r) => (
            <RunRowItem
              key={r.run_id}
              run={r}
              onOpen={onOpen}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function DeleteConfirmDialog({
  runId,
  onCancel,
  onConfirm,
}: {
  runId: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-labelledby="del-title"
    >
      <div
        className="orca-bg-surface orca-border w-full max-w-sm rounded-lg border p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="del-title" className="orca-text text-base font-semibold">
          删除该 run？
        </h3>
        <p className="orca-text-muted mt-2 text-sm">
          将永久删除 tape 与产物目录，不可恢复。
        </p>
        <p className="orca-text-faint mt-2 font-mono text-xs">{runId}</p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="orca-text-muted hover:orca-text rounded border orca-border px-3 py-1.5 text-sm"
            autoFocus
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded bg-orca-failed px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
          >
            删除
          </button>
        </div>
      </div>
    </div>
  );
}

function StatusFilterChips({
  active,
  onChange,
}: {
  active: StatusFilter;
  onChange: (s: StatusFilter) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      {CHIPS.map((c) => {
        const isActive = active === c.key;
        return (
          <button
            key={c.key}
            type="button"
            onClick={() => onChange(c.key)}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs ${
              isActive
                ? "border-transparent bg-orca-accent text-white"
                : "orca-border orca-text-muted orca-bg-surface hover:orca-bg-surface-2"
            }`}
          >
            {c.dot && (
              <span
                className={`h-1.5 w-1.5 rounded-full ${isActive ? "bg-white/80" : c.dot}`}
              />
            )}
            {c.label}
          </button>
        );
      })}
    </div>
  );
}

// ── 顶栏 ───────────────────────────────────────────────────────────────────

type Theme = "system" | "dark" | "light";
const THEME_ICON: Record<Theme, typeof Sun> = {
  system: Monitor,
  dark: Moon,
  light: Sun,
};

function ListTopBar({
  q,
  onQ,
  status,
  onStatus,
  groupBy,
  onToggleGroup,
  refreshing,
  onRefresh,
}: {
  q: string;
  onQ: (v: string) => void;
  status: StatusFilter;
  onStatus: (s: StatusFilter) => void;
  groupBy: boolean;
  onToggleGroup: () => void;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const [theme, setTheme] = useState<Theme>("system");
  const ThemeIcon = THEME_ICON[theme];
  const cycleTheme = () =>
    setTheme((t) => (t === "system" ? "dark" : t === "dark" ? "light" : "system"));
  return (
    <header className="orca-bg-surface orca-border orca-text flex h-14 items-center gap-3 border-b px-4">
      <span className="orca-accent text-lg font-semibold tracking-wider">TARS</span>
      <span className="orca-text-faint text-sm">/ Orca Runs</span>
      <span className="ml-auto flex items-center gap-3">
        <button
          type="button"
          onClick={onRefresh}
          title="刷新"
          className="orca-text-faint hover:orca-text inline-flex items-center rounded border orca-border px-2 py-1"
        >
          {refreshing ? (
            <Loader2 size={14} strokeWidth={1.5} className="animate-spin" aria-hidden />
          ) : (
            <RefreshCw size={14} strokeWidth={1.5} aria-hidden />
          )}
        </button>
        <span className="orca-border inline-flex items-center gap-1.5 rounded border px-2 py-1">
          <Search size={14} strokeWidth={1.5} aria-hidden className="orca-text-faint" />
          <input
            value={q}
            onChange={(e) => onQ(e.target.value)}
            placeholder="搜索 workflow / run_id"
            className="orca-text w-48 bg-transparent text-sm outline-none placeholder:orca-text-faint"
          />
          {q && (
            <button
              type="button"
              onClick={() => onQ("")}
              className="orca-text-faint hover:orca-text"
              aria-label="清空"
            >
              <X size={12} strokeWidth={1.5} aria-hidden />
            </button>
          )}
        </span>
        <StatusFilterChips active={status} onChange={onStatus} />
        <button
          type="button"
          onClick={onToggleGroup}
          title={groupBy ? "取消分组" : "按项目分组"}
          className={`inline-flex items-center rounded border orca-border px-2 py-1 ${
            groupBy ? "orca-accent" : "orca-text-faint hover:orca-text"
          }`}
        >
          {groupBy ? (
            <Group size={14} strokeWidth={1.5} aria-hidden />
          ) : (
            <ListIcon size={14} strokeWidth={1.5} aria-hidden />
          )}
        </button>
        <button
          type="button"
          onClick={cycleTheme}
          title={`主题：${theme}`}
          className="orca-text-faint hover:orca-text inline-flex items-center"
          aria-label="切换主题"
        >
          <ThemeIcon size={15} strokeWidth={1.5} aria-hidden />
        </button>
      </span>
    </header>
  );
}

// ── 页根 ───────────────────────────────────────────────────────────────────

export function RunListPage() {
  const navigate = useNavigate();
  const { runs, loading, refresh, deleteRun, onRunChanged, reset } = useRunListStore();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [groupBy, setGroupBy] = useState(true);
  const [pendingDelete, setPendingDelete] = useState<string | null>( null);
  const wsRef = useRef<WebSocket | null>(null);

  // mount：refresh + 启轮询 + 接 WS 控制帧。unmount：reset + 关 WS（无 orphan task）。
  useEffect(() => {
    void refresh();
    startPolling();
    // 控制帧 WS（列表页不订阅任何 run，仅收 run_changed，§13 M-14）。
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        // §13 M-8：``kind==="control"`` → 列表层处理；其它事件一律拒（不进 reducer）。
        if (msg?.kind === "control" && msg?.type === "run_changed") {
          onRunChanged({ run_id: msg.run_id, action: msg.action });
        }
      } catch {
        // ignore non-json / partial
      }
    };
    return () => {
      ws.close();
      wsRef.current = null;
      stopPolling();
      reset();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return runs.filter((r) => {
      if (
        ql &&
        !(r.workflow_name || "").toLowerCase().includes(ql) &&
        !r.run_id.toLowerCase().includes(ql)
      ) {
        return false;
      }
      const rs = statusToRunStatus(r.status);
      if (status === "running") return rs === "running" || rs === "queued";
      if (status === "blocked") return rs === "blocked";
      if (status === "completed") return rs === "completed";
      if (status === "failed") return rs === "failed";
      return true;
    });
  }, [q, status, runs]);

  const groups = useMemo<ProjectGroup[]>(() => {
    if (!groupBy) {
      return [{ name: "全部", path: "", runs: filtered, defaultOpen: true }];
    }
    const buckets: Record<string, RunSummary[]> = {};
    const pathById: Record<string, string> = {};
    for (const r of filtered) {
      const key = r.project_name || (r.source === "legacy" ? "Legacy" : "其它");
      (buckets[key] ??= []).push(r);
      pathById[key] = r.project_id ?? "";
    }
    const names = Object.keys(buckets).sort((a, b) => {
      if (a === "Legacy") return 1;
      if (b === "Legacy") return -1;
      return a.localeCompare(b);
    });
    return names.map((n) => ({
      name: n,
      path: pathById[n] ?? "",
      runs: buckets[n],
      defaultOpen: n !== "Legacy",
    }));
  }, [groupBy, filtered]);

  const handleOpen = (id: string) => {
    navigate(`/runs/${id}`);
  };

  const handleConfirmDelete = async () => {
    if (!pendingDelete) return;
    const target = pendingDelete;
    setPendingDelete(null);
    try {
      await deleteRun(target);
    } catch (e) {
      // 失败回滚已由 store 处理；这里提示用户。
      console.error("[RunListPage] deleteRun failed", e);
      alert(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const isEmpty = runs.length === 0;
  const isFilteredEmpty = !isEmpty && filtered.length === 0;

  return (
    <div className="orca-bg-app orca-text flex h-full flex-col">
      <ListTopBar
        q={q}
        onQ={setQ}
        status={status}
        onStatus={setStatus}
        groupBy={groupBy}
        onToggleGroup={() => setGroupBy((v) => !v)}
        refreshing={loading}
        onRefresh={() => void refresh()}
      />

      <main className="orca-bg-app flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl px-6 py-5">
          {isEmpty && (
            <div className="flex h-80 flex-col items-center justify-center gap-2">
              <Inbox size={48} strokeWidth={1} aria-hidden className="orca-text-faint" />
              <p className="orca-text text-base font-medium">暂无 run</p>
              <p className="orca-text-faint text-sm">
                在项目里运行 <code className="font-mono">orca run &lt;workflow&gt;</code> 即可在此看到。
              </p>
            </div>
          )}

          {isFilteredEmpty && (
            <div className="flex h-80 flex-col items-center justify-center gap-2">
              <Search size={36} strokeWidth={1} aria-hidden className="orca-text-faint" />
              <p className="orca-text text-sm font-medium">没有匹配的 run</p>
              <p className="orca-text-faint text-sm">试试调整搜索或过滤条件。</p>
            </div>
          )}

          {!isEmpty && !isFilteredEmpty && (
            <div className={`space-y-4 ${loading ? "opacity-60" : ""}`}>
              {groups.map((g) => (
                <ProjectGroupView
                  key={g.name}
                  {...g}
                  onOpen={handleOpen}
                  onDelete={setPendingDelete}
                />
              ))}
            </div>
          )}
        </div>
      </main>

      <footer className="orca-bg-surface orca-border orca-text-muted flex h-10 items-center justify-between border-t px-6 text-xs">
        <span>
          显示 <span className="orca-text tabular-nums">{filtered.length}</span> / 共{" "}
          <span className="orca-text tabular-nums">{runs.length}</span>
        </span>
      </footer>

      {pendingDelete && (
        <DeleteConfirmDialog
          runId={pendingDelete}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void handleConfirmDelete()}
        />
      )}
    </div>
  );
}

export default RunListPage;
