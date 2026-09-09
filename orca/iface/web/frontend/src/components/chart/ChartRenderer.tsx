// components/chart/ChartRenderer.tsx —— 主入口：用 selectCharts 选择 custom(chart) 事件
// → 按 group 分组渲染（SPEC §5.4 / §0 D3 / D7）。
//
// 铁律 4（SPEC §0.1）：chart 是事件不是图片——从 store.events filter type==="custom" &&
// data.kind==="chart"（D7 seq 升序 fold，序无关）。**不单独存 chart store/通道**。
//
// **去重真相出口 = selectCharts**（identity=title||chart_type+seq，upsert）。ChartGroup
// 不再二次去重（铁律 1：selectors 是唯一 view 输入）。
//
// SPEC audit-c §4.2：partition 在 selectCharts 与 ChartGroup 之间——内联 cast + reject
// 分区（C5），不再 silent filter（INV-5 schema 漂移显形）；无 title chart dev warn-once-
// per-identity（MINOR-5，huge→full identity 变化允许 remount）；ChartGroup key=identity（E3）。

import { useMemo } from "react";
import { Loader2 } from "lucide-react";
import { useWorkflowStore, untitledChartWarned } from "@/stores/workflow-store";
import { selectCharts, DOCS_LABEL, type ChartEntry } from "@/selectors";
import { ChartGroup } from "./ChartGroup";
import type { ChartPayload } from "./types";

// B2（2026-09-07 #2）：prof-opt 文档清单表不进图表渲染。它是文档面板
// （ProfOptDocsPanel / selectDocRowsWithContent）的数据源——**推送侧不停**
// （事件照常进 store，面板从事件流读清单），仅不作为 chart 呈现。
// DOCS_LABEL 契约字面量单源在 selectors（DRY：漂移 = 过滤静默失效）。

interface ChartRendererProps {
  /** 限定到某节点；undefined = 全部节点（ChartsView 用）。 */
  nodeId?: string;
}

/** partition 输出：valid（cast 后）+ placeholders（huge 目录占位）+ rejected（shape 异常）。 */
interface PartitionOutput {
  valid: { identity: string; payload: ChartPayload }[];
  placeholders: { identity: string; payload: ChartPayload }[];
  rejected: { seq: number; group: string }[];
}

/**
 * SPEC audit-c §4.2 E10：partitionCharts——内联 cast + reject 分区。
 *
 * - **不 dedup**（B3 round-5）：信任 selectCharts 的 byIdentity Map（selectors.ts:525）。
 * - **不扩 ChartPayload 加 seq**（C5：污染 chart 契约）。
 * - partition 内联 ``chart_type`` / ``data-is-array`` 校验，valid 直接 cast，rejected 收集。
 * - **huge 模式目录占位（placeholder）不 reject**（SPEC web-attach §3 M3）：serverOverview
 *   charts 清单只有 label/title/chart_type，无 data——不是 INV-5 schema 漂移，是服务端
 *   fold 的目录。单独 placeholders 桶渲染占位卡，后台自动 load-full 到位后替换（根治
 *   「773 个 chart 数据格式异常」误报）。
 * - **无 title chart dev warn-once-per-identity**（MINOR-5）：huge 模式 identity=
 *   ``chart_type#index`` vs full 模式 ``chart_type#seq``，跨 huge→full identity 变化 →
 *   React remount（允许）+ dev warn。模块级 ``untitledChartWarned: Set<identity>`` 防 spam。
 */
function partitionCharts(
  groups: { group: string; entries: ChartEntry[] }[]
): { group: string; partitioned: PartitionOutput }[] {
  return groups.map(({ group, entries }) => {
    const valid: { identity: string; payload: ChartPayload }[] = [];
    const placeholders: { identity: string; payload: ChartPayload }[] = [];
    const rejected: { seq: number; group: string }[] = [];
    for (const entry of entries) {
      const p = entry.payload as Record<string, unknown>;
      if (entry.placeholder) {
        placeholders.push({
          identity: entry.identity,
          payload: p as unknown as ChartPayload,
        });
        continue;
      }
      if (!p || !p.chart_type || !Array.isArray(p.data)) {
        rejected.push({ seq: entry.seq, group });
        continue;
      }
      // dev warn-once-per-identity：无 title chart 跨 huge→full identity 变化 → remount
      if (import.meta.env.DEV) {
        const hasTitle =
          typeof (p as { title?: unknown }).title === "string" &&
          (p as { title?: string }).title;
        if (!hasTitle && !untitledChartWarned.has(entry.identity)) {
          untitledChartWarned.add(entry.identity);
          console.warn(
            `[orca] chart 缺 title，huge→full 将 remount (identity=${entry.identity})`
          );
        }
      }
      valid.push({
        identity: entry.identity,
        payload: p as unknown as ChartPayload,
      });
    }
    return { group, partitioned: { valid, placeholders, rejected } };
  });
}

export function ChartRenderer({ nodeId }: ChartRendererProps) {
  // 订阅收窄（SPEC 2026-08-28 C4.4，取消全 store 订阅）。字段清单：events/
  // serverOverview/hugeFullyLoaded 是 selectCharts 全部输入。
  // （2026-09-09 自动后台全量：窗口态不再有 load-full 按钮——loadRunWithMeta 提交
  // 窗口后自动 loadFull(background)，占位目录在真实数据到位前先行展示。）
  const events = useWorkflowStore((s) => s.events);
  const serverOverview = useWorkflowStore((s) => s.serverOverview);
  const hugeFullyLoaded = useWorkflowStore((s) => s.hugeFullyLoaded);
  const { groups } = useMemo(
    () => selectCharts._from(events, serverOverview, hugeFullyLoaded),
    [events, serverOverview, hugeFullyLoaded]
  );

  // B2：整组滤除 docs 清单组——valid 与 huge placeholder 两分支同免（组级剔除，
  // 不留 0 项组壳）；空态判定在过滤**之后**（docs-only run 显示诚实 chart-empty，
  // 非过滤副作用的空壳）。selectCharts 输出不变（文档面板仍从事件读同一清单）。
  const chartGroups = useMemo(
    () => groups.filter((g) => g.group !== DOCS_LABEL),
    [groups]
  );

  // nodeId filter（可选）：限定到某节点
  const filtered = useMemo(() => {
    if (nodeId === undefined) return chartGroups;
    return chartGroups
      .map((g) => ({
        ...g,
        entries: g.entries.filter((e) => e.node === nodeId),
      }))
      .filter((g) => g.entries.length > 0);
  }, [chartGroups, nodeId]);

  // partition：cast + reject 分区（替代旧 silent filter）
  const partitioned = useMemo(() => partitionCharts(filtered), [filtered]);
  const totalRejected = partitioned.reduce(
    (sum, g) => sum + g.partitioned.rejected.length,
    0
  );
  const totalPlaceholders = partitioned.reduce(
    (sum, g) => sum + g.partitioned.placeholders.length,
    0
  );

  if (filtered.length === 0) {
    return (
      <p className="p-4 text-xs orca-text-faint" data-testid="chart-empty">
        暂无图表
      </p>
    );
  }

  // 窗口态（serverOverview 目录占位在）→ 渲染目录 + 后台全量提示
  // （web-perf P3：非 huge 的截断 run 同样走此通道，huge 不再是前提；
  // 2026-09-09 起后台自动拉全量——到位后 serverOverview 清，本条自然消失）
  const isWindowed = !hugeFullyLoaded && serverOverview !== null;

  return (
    <div className="space-y-4 p-3" data-testid="chart-renderer">
      {totalRejected > 0 && (
        <div
          className="border orca-border orca-bg-surface rounded p-2 text-xs orca-text-failed"
          data-testid="chart-schema-warning"
        >
          ⚠️ {totalRejected} 个 chart 数据格式异常（后端 schema 漂移？）
          <details className="orca-text-faint mt-1">
            <summary className="cursor-pointer">查看详情</summary>
            <ul className="ml-4">
              {partitioned.flatMap((g) =>
                g.partitioned.rejected.map((r, i) => (
                  <li key={`${g.group}-${i}`}>
                    group={r.group} seq={r.seq}
                  </li>
                ))
              )}
            </ul>
          </details>
        </div>
      )}
      {isWindowed && totalPlaceholders > 0 && (
        <div
          className="border orca-border orca-bg-surface flex items-center gap-2 rounded p-2 text-xs orca-text-muted"
          data-testid="huge-charts-placeholder"
        >
          <Loader2
            size={12}
            strokeWidth={1.5}
            className="animate-spin shrink-0"
            aria-hidden
          />
          <p>
            首屏按窗口加载：图表先显示目录（{totalPlaceholders} 张），
            完整事件后台拉取中，到位后自动替换。
          </p>
        </div>
      )}
      {partitioned.map(({ group, partitioned: p }) =>
        p.valid.length === 0 && p.placeholders.length === 0 ? null : (
          <ChartGroup
            key={group}
            label={group}
            charts={[
              ...p.placeholders.map((x) => ({ ...x, placeholder: true })),
              ...p.valid,
            ]}
          />
        )
      )}
    </div>
  );
}
