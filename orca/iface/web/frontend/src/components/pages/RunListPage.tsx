// components/pages/RunListPage.tsx —— RunListPage 重设计（薄页壳）。
//
// SPEC docs/specs/web-runlist-redesign.md v2（含 §10.8/§10.9/§10.10）已闭环 spec-reviewer。
// 职责：编排 mount refresh+轮询+WS、过滤、分组、排序、选择、bulk、dialog；不直接渲染细节，
// 组合 ``components/runlist/*`` 子组件。铁律：**不** import workflow-store（R3）。
//
// 关键时序不变量（SPEC §3.3）：
//   - 选择集 / 排序 / 折叠 三类 view-state **互不重置**（切排序/chip/groupBy dim/清搜索 → 选择全保留）。
//   - 仅「用户点取消选择」「删除完成」「页面 unmount」清空选择。
//   - 排序与分组叠加：全局排序 → 再按当前 dim 分桶（§10.8 共享 groupRuns）；桶内按用户 sort field。
//   - 折叠态按 ``"dim:key"`` 持久（§10.8），切 dim 各自独立。

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronDown, ChevronRight, AlertTriangle } from "lucide-react";

import {
  startPolling,
  stopPolling,
  useRunListStore,
} from "@/stores/run-list-store";
import { statusToRunStatus } from "@/components/layout/status-badge";
import { useCollapsedBuckets } from "@/hooks/use-collapsed-buckets";
import { useGroupBy } from "@/hooks/use-group-by";
import { useShowEmpty } from "@/hooks/use-show-empty";
import { useListSelection } from "@/hooks/use-list-selection";
import { useListSort } from "@/hooks/use-list-sort";
import { useRunListView } from "@/hooks/use-runlist-view";
import { useWsRunlist } from "@/hooks/use-ws-runlist";

import { ListTopBar } from "@/components/runlist/ListTopBar";
import { EmptyState } from "@/components/runlist/EmptyState";
import { ListSkeleton } from "@/components/runlist/ListSkeleton";
import { ErrorBanner } from "@/components/runlist/ErrorBanner";
import { ProjectGroup } from "@/components/runlist/ProjectGroup";
import { BulkActionBar } from "@/components/runlist/BulkActionBar";
import {
  DeleteConfirmDialog,
  type DeleteTarget,
} from "@/components/runlist/DeleteConfirmDialog";
import { StaleProjectsSection } from "@/components/runlist/StaleProjectsSection";
import { RunBoard } from "@/components/runlist/RunBoard";
import { sortRuns } from "@/components/runlist/sort-runs";
import { groupRuns } from "@/components/runlist/group-runs";
import type { StatusFilter } from "@/components/runlist/StatusFilterChips";

export function RunListPage() {
  const navigate = useNavigate();
  const {
    runs,
    staleProjects,
    loading,
    error,
    refresh,
    deleteRun,
    deleteRuns,
    onRunChanged,
    reset,
  } = useRunListStore();

  // ── view-state ────────────────────────────────────────────────────────────
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const { groupBy, setGroupBy } = useGroupBy();
  const { showEmpty, setShowEmpty } = useShowEmpty();
  const { sort, selectField } = useListSort();
  const { view, setView } = useRunListView();

  // ── 删除对话框态 ────────────────────────────────────────────────────────────
  // null = 关闭；{mode:"single",...} = 单条；{mode:"bulk", targets:[...]} = 批量。
  const [dialog, setDialog] = useState<
    | null
    | { mode: "single"; target: DeleteTarget }
    | { mode: "bulk"; targets: DeleteTarget[] }
  >(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  // DELETE in-flight 的 id 集合（视觉 opacity-40）。
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

  // ── WS（控制帧 + 重连） ─────────────────────────────────────────────────────
  const wsUrl = useMemo(() => {
    if (typeof window === "undefined") return "";
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws`;
  }, []);
  const wsState = useWsRunlist(
    wsUrl,
    onRunChanged,
    // 重连成功 → 清 lastFetch 节流后 refresh（SPEC §5.8「重连成功淡出 + refresh」）。
    () => {
      useRunListStore.setState({ lastFetch: 0 });
      void refresh();
    },
  );

  // ── mount：refresh + 启轮询。unmount：reset + 停轮询。 ──────────────────────
  useEffect(() => {
    void refresh();
    startPolling();
    return () => {
      stopPolling();
      reset();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 过滤（q + status chip） ─────────────────────────────────────────────────
  const filtered = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return runs.filter((r) => {
      if (
        ql &&
        !(r.workflow_name || "").toLowerCase().includes(ql) &&
        !r.run_id.toLowerCase().includes(ql) &&
        !(r.project_name || "").toLowerCase().includes(ql)
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

  // ── 全局排序（分组前）：稳定排序 + tiebreaker ───────────────────────────────
  const sorted = useMemo(() => sortRuns(filtered, sort), [filtered, sort]);

  // ── 分组（共享 groupRuns DRY 单出口，§10.8） ───────────────────────────────
  const buckets = useMemo(() => groupRuns(sorted, groupBy), [sorted, groupBy]);
  // 空桶隐藏（§10.9/AC-25）：showEmpty=false → 0-run 桶不渲染。
  // none 维度单桶永不为空（有 run 时），filter 不影响。
  const visibleBuckets = useMemo(
    () => buckets.filter((b) => showEmpty || b.runs.length > 0),
    [buckets, showEmpty],
  );

  // ── 折叠持久（known = 当前所有 "dim:key"；惰性清理用） ──────────────────────
  const knownKeys = useMemo(() => {
    const s = new Set<string>();
    for (const b of buckets) s.add(`${groupBy}:${b.key}`);
    return s;
  }, [buckets, groupBy]);
  const { collapsed, toggle: toggleCollapse, expandAll, collapseAll } =
    useCollapsedBuckets(knownKeys);

  // ── 选择（runs 变化自动求交，§3.3） ────────────────────────────────────────
  // 求交数据源用**未过滤** runs（review M-1 / SPEC §3.3）：切 chip/搜索隐藏已选 run 时，
  // 选择**保留**——只有 run 真正从 runs 消失（删除/WS）才被求交剔除。若用 sorted（含 q/status
  // 过滤），切 chip 隐藏已选 run 后会被误剔，永久丢选择。
  const runIds = useMemo(() => runs.map((r) => r.run_id), [runs]);
  const { selected, toggle, toggleGroup, groupState, setMany, clear } =
    useListSelection(runIds);

  // ── 搜索穿透：q 非空 → 含匹配 run 的桶强制展开（覆盖持久折叠） ─────────────
  const searching = q.trim().length > 0;
  const isBucketOpen = (bucketKey: string): boolean =>
    searching
      ? (buckets.find((b) => b.key === bucketKey)?.runs.length ?? 0) > 0
      : !collapsed.has(`${groupBy}:${bucketKey}`);

  // ── handlers ──────────────────────────────────────────────────────────────
  const handleOpen = (id: string) => navigate(`/runs/${id}`);

  const handleDeleteOne = (target: DeleteTarget) => {
    setDialog({ mode: "single", target });
  };

  const handleConfirm = async () => {
    if (!dialog) return;
    setDeleteBusy(true);
    // 用 inflight id 集合标记视觉「删除中」。
    const inflight = new Set<string>(
      dialog.mode === "single"
        ? [dialog.target.runId]
        : dialog.targets.map((t) => t.runId),
    );
    setDeletingIds(inflight);
    // 全部成功才清选择 + 关对话框（SPEC §3.3：仅「删除完成」清空选择；失败保留以便重试）。
    let allOk = true;
    try {
      if (dialog.mode === "single") {
        try {
          await deleteRun(dialog.target.runId);
          // M-3：单删成功 toast（SPEC §5.6「成功 → 右下 toast 已删除 <name>」）。
          toast(`已删除 ${dialog.target.workflowName}`, "ok");
        } catch (e) {
          allOk = false;
          // fail-loud（§ AC-15）：toast 错误原因，**不**用 alert。
          toast(
            `删除失败：${e instanceof Error ? e.message : String(e)}`,
            "failed",
          );
        }
      } else {
        const ids = dialog.targets.map((t) => t.runId);
        const result = await deleteRuns(ids);
        if (result.failed.length === 0) {
          toast(`已删除 ${result.deleted.length} 项`, "ok");
        } else if (result.deleted.length === 0) {
          allOk = false;
          toast(
            `删除失败：${result.failed.map((f) => f.id.slice(0, 8)).join(", ")}`,
            "failed",
          );
        } else {
          allOk = false;
          toast(
            `已删除 ${result.deleted.length} 项，${result.failed.length} 项失败：` +
              result.failed.map((f) => `${f.id.slice(0, 8)}(${f.reason})`).join("; "),
            "partial",
          );
        }
      }
      // 仅全部成功才关对话框；清空选择**仅 bulk 全成功**（review M-5）——单删成功不清选择集
      // （被删 id 由 M-1 的求交自动剔，其它已选保留；旧实现无条件 clear 会误清整个选择集）。
      if (allOk) {
        if (dialog.mode === "bulk") clear();
        setDialog(null);
      }
    } finally {
      setDeleteBusy(false);
      setDeletingIds(new Set());
    }
  };

  const handleBulkDelete = () => {
    const targets: DeleteTarget[] = sorted
      .filter((r) => selected.has(r.run_id))
      .map((r) => ({ runId: r.run_id, workflowName: r.workflow_name }));
    if (targets.length === 0) return;
    setDialog({ mode: "bulk", targets });
  };

  // ── 三态加载判定（§5.1） ────────────────────────────────────────────────────
  // 注：``showEmptyState`` 命名避让 §10.9 的 ``showEmpty``（空桶显隐 toggle）。
  const showSkeleton = loading && runs.length === 0;
  const showError = !loading && !!error && runs.length === 0;
  const showEmptyState = !loading && !error && runs.length === 0;
  const showFilteredEmpty = runs.length > 0 && sorted.length === 0;

  return (
    <div
      className="orca-bg-app orca-text flex h-full flex-col"
      // 模态打开时设 inert，让背后 UI 对键盘/AT 不可达（React 19 inert 原生支持）。
      {...(dialog ? { inert: true } : {})}
    >
      <ListTopBar
        q={q}
        onQ={setQ}
        status={status}
        onStatus={setStatus}
        groupBy={groupBy}
        onGroupBy={setGroupBy}
        showEmpty={showEmpty}
        onShowEmpty={setShowEmpty}
        refreshing={loading}
        onRefresh={() => void refresh()}
        sort={sort}
        onSelectSortField={selectField}
        view={view}
        onView={setView}
      />

      <main className="orca-bg-app flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl px-6 py-5">
          {/* bulk bar：选中非空时作为 main 首子 sticky 渲染（SPEC §1.1/§4） */}
          {selected.size > 0 && (
            <BulkActionBar
              selectedCount={selected.size}
              totalCount={sorted.length}
              onBulkDelete={handleBulkDelete}
              onClearSelection={clear}
              onSelectAll={() => setMany(sorted.map((r) => r.run_id), "replace")}
            />
          )}

          {showSkeleton && <ListSkeleton />}
          {showError && (
            <ErrorBanner message={error ?? ""} onRetry={() => void refresh()} />
          )}
          {showEmptyState && <EmptyState mode="empty" />}
          {showFilteredEmpty && <EmptyState mode="filtered" />}

          {/* WS 状态：断线显非阻塞提示（§5.8） */}
          {!wsState.connected && wsState.reconnects > 0 && (
            <div
              data-testid="ws-status"
              className="orca-border orca-bg-surface mb-3 flex items-center gap-2 rounded border px-3 py-2 text-xs"
            >
              <AlertTriangle
                size={12}
                strokeWidth={1.5}
                aria-hidden
                className="text-orca-failed shrink-0"
              />
              <span className="text-orca-failed">
                实时连接已断开（轮询兜底），重连 {wsState.reconnects} 次
              </span>
              {wsState.giveUp && (
                <button
                  type="button"
                  onClick={wsState.reconnect}
                  className="orca-text-muted hover:orca-text ml-auto rounded border orca-border px-2 py-0.5"
                >
                  重试连接
                </button>
              )}
            </div>
          )}

          {/* 列表（仅 list 视图渲染；board 视图见下方 RunBoard） */}
          {view === "list" &&
            !showSkeleton &&
            !showError &&
            !showEmptyState &&
            sorted.length > 0 && (
              <div className={`space-y-3 ${loading ? "opacity-60" : ""}`}>
                {visibleBuckets.map((b) => {
                  const open = isBucketOpen(b.key);
                  const groupIds = b.runs.map((r) => r.run_id);
                  return (
                    <ProjectGroup
                      key={`${groupBy}:${b.key}`}
                      name={b.label}
                      bucketKey={b.key}
                      runs={b.runs}
                      open={open}
                      onToggleOpen={() => toggleCollapse(`${groupBy}:${b.key}`)}
                      searchHitCount={searching ? b.runs.length : undefined}
                      q={q}
                      selectedIds={selected}
                      // M-4：Shift 范围选 orderedIds 收窄到**当前桶**（SPEC §5.5「同分组内」），
                      // anchor 跨桶时 toggle 内 indexOf 自然返 -1 → 退化为普通点，不越组。
                      onToggleRun={(id, shiftKey) =>
                        toggle(id, shiftKey, groupIds)
                      }
                      onToggleSelectAll={() => toggleGroup(groupIds)}
                      selectAllState={groupState(groupIds)}
                      onOpenRun={handleOpen}
                      onDeleteRun={(id) =>
                        handleDeleteOne({
                          runId: id,
                          workflowName:
                            runs.find((r) => r.run_id === id)?.workflow_name ??
                            id,
                        })
                      }
                      deletingIds={deletingIds}
                    />
                  );
                })}
              </div>
            )}

          {/* 看板（仅 board 视图渲染；与列表共享 store/selection/sort/dim/showEmpty/etc.） */}
          {view === "board" &&
            !showSkeleton &&
            !showError &&
            !showEmptyState &&
            sorted.length > 0 && (
              <div className={loading ? "opacity-60" : ""}>
                <RunBoard
                  runs={sorted}
                  dim={groupBy}
                  showEmpty={showEmpty}
                  selectedIds={selected}
                  deletingIds={deletingIds}
                  // M-4：RunBoard 内部按桶收窄 orderedIds（同列表 ProjectGroup 一致）。
                  onToggleRun={toggle}
                  onOpenRun={handleOpen}
                  onDeleteRun={(id) =>
                    handleDeleteOne({
                      runId: id,
                      workflowName:
                        runs.find((r) => r.run_id === id)?.workflow_name ?? id,
                    })
                  }
                />
              </div>
            )}

          {/* 0 命中（有数据但被筛光）行内提示（§5.2：不跳全屏空态） */}
          {runs.length > 0 && sorted.length === 0 && searching && (
            <p className="orca-text-muted mt-4 text-center text-xs">
              未匹配任何 run
            </p>
          )}

          <StaleProjectsSection items={staleProjects} />
        </div>
      </main>

      {/* footer：显示数 + 选中数 + 全部展开/折叠（分组 ≥3 时） */}
      <footer className="orca-bg-surface orca-border orca-text-muted flex h-10 items-center justify-between border-t px-6 text-xs">
        <span>
          显示 <span className="orca-text tabular-nums">{sorted.length}</span> / 共{" "}
          <span className="orca-text tabular-nums">{runs.length}</span>
          {selected.size > 0 && (
            <>
              {" · 已选 "}
              <span className="orca-accent tabular-nums">{selected.size}</span>
            </>
          )}
        </span>
        {view === "list" && visibleBuckets.length >= 3 && (
          <span className="flex items-center gap-2">
            <button
              type="button"
              data-testid="expand-all"
              onClick={expandAll}
              className="orca-text-muted hover:orca-text inline-flex items-center gap-1"
            >
              <ChevronDown size={12} strokeWidth={1.5} aria-hidden />
              全部展开
            </button>
            <button
              type="button"
              data-testid="collapse-all"
              onClick={collapseAll}
              className="orca-text-muted hover:orca-text inline-flex items-center gap-1"
            >
              <ChevronRight size={12} strokeWidth={1.5} aria-hidden />
              全部折叠
            </button>
          </span>
        )}
      </footer>

      {dialog && (
        <DeleteConfirmDialog
          targets={
            dialog.mode === "single"
              ? { single: dialog.target }
              : dialog.targets
          }
          onCancel={() => setDialog(null)}
          onConfirm={() => void handleConfirm()}
          busy={deleteBusy}
        />
      )}
    </div>
  );
}

// ── toast（极简，右下角；SPEC §5.6 要求 toast 不用 alert） ────────────────────
// 注：本轮 SPEC 范围未要求 toast 库；用最轻量的 dom 操作实现 fail-loud 反馈。
// 三态：ok（accent）/ partial（skipped）/ failed（failed）。
function toast(message: string, kind: "ok" | "partial" | "failed"): void {
  if (typeof document === "undefined") return;
  const el = document.createElement("div");
  el.setAttribute("data-testid", "runlist-toast");
  // 失败/部分 → role=alert（assertive，AT 立即播报）；成功 → role=status（polite）。
  el.setAttribute("role", kind === "ok" ? "status" : "alert");
  el.className =
    "orca-bg-surface orca-border fixed bottom-4 right-4 z-[60] rounded border px-4 py-2 text-sm shadow-md " +
    (kind === "failed"
      ? "text-orca-failed"
      : kind === "partial"
        ? "text-orca-skipped"
        : "orca-text");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => {
    el.remove();
  }, 3000);
}

export default RunListPage;
