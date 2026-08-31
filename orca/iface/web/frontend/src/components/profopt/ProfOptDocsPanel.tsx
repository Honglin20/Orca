// components/profopt/ProfOptDocsPanel.tsx —— prof-opt「分析文档」面板（web SPEC §3）。
//
// 数据流（web SPEC §3.2，铁律 1+5：selectors 是唯一 view 输入）：
//   chart socket → store.events → selectCharts → label `prof-opt/docs` 的 table 清单
//   → 本面板只渲染名称 + 状态徽标 + 更新时间（**不渲染正文**，web §2.3）
//   → 点选条目 → GET /api/runs/<id>/artifacts/file?path=<相对 path>（W-P1 端点）
//   → `.md` 复用 MarkdownText；其余（json 等）复用 FileContentView。
//
// 清单 payload 契约（v6 §10.4 + web §2.3；P4-T3 push_curves 推送方）：
//   chart_type="table"、label="prof-opt/docs"、行字段（列名即 canonical 契约）：
//     vid（baseline / r<R>-NN / round / rules）、doc（文档名）、status、
//     path（相对 run artifacts 根）、updated_at（可选——缺席则不显示更新时间，不造假值）。
//
// 分组（web §3.1：基线 / 变体按轮序 / 轮次 / 规则）从行内容**确定性派生**，不依赖
// 推送方额外字段：
//   - path === "base/accuracy_rules_snapshot.json"      → 规则（S-9：规则组 path 源）
//   - path 以 "rounds/" 开头                             → 轮次
//   - vid === "baseline" 或 path 在 base//baseline/ 下   → 基线
//   - 其余（variants/<vid>/...）                         → 变体（按 vid 自然序 = 轮序）
//
// 只读与白名单（web §5）：面板只消费清单内相对 path，唯一网络请求是 GET artifacts
// 端点，无任何写入口。markdown 内相对图片改写为 artifacts 端点前缀（doc 目录相对
// 解析）；http(s)/data/blob/file:// 不改写——交由 MarkdownText 现有 assets 改写约定
// （file:// 落 assets 端点 = 已知破图降级，fail-soft 不崩渲染）。
//
// 变体卡片实现拍板偏离说明（W2-T1）：「复用 chart table payload」落在**数据源**层
// ——本面板直接消费清单行，不另造数据/表格派生逻辑；**渲染层**自绘卡片，因
// DataTableWidget 无点选/分组/状态徽标能力，复用它必须改 widget，与「chart widgets
// 零改」（web §7）直接冲突——二选一取零改优先。已上报编排方回写 plan 记录。
//
// 失败路径（web §5：降级提示不崩）：404 → 「不存在」提示；413 → 「超 1MB」提示；
// 其余非 2xx / 网络错误 → 显式错误行；清单行缺 path → schema warning（fail loud 计数，
// 与 ChartRenderer chart-schema-warning 同模式），合法行照常渲染。

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectCharts } from "@/selectors";
import { MarkdownText } from "@/components/conversation/MarkdownText";
import { FileContentView } from "@/components/conversation/FileContentView";

/** 文档清单 chart 的 label（v6 §10.4 契约字面量）。 */
const DOCS_LABEL = "prof-opt/docs";

/** 规则组唯一合法 path（S-9 拍板：规则面板数据源 = run 内只读快照）。 */
const RULES_SNAPSHOT_PATH = "base/accuracy_rules_snapshot.json";

/** 面板分组键（web §3.1 四组，渲染顺序同此）。 */
type DocGroupKey = "baseline" | "variants" | "rounds" | "rules";

const GROUP_ORDER: { key: DocGroupKey; label: string }[] = [
  { key: "baseline", label: "基线" },
  { key: "variants", label: "变体" },
  { key: "rounds", label: "轮次" },
  { key: "rules", label: "规则" },
];

/** 清单行（列名 = P4-T3 推送方 canonical 契约；updated_at 可选）。 */
export interface DocRow {
  vid: string;
  doc: string;
  status: string;
  path: string;
  updated_at?: string;
}

/** 行 → 分组（确定性派生，见文件头注释）。 */
export function docGroupOf(row: DocRow): DocGroupKey {
  if (row.path === RULES_SNAPSHOT_PATH) return "rules";
  if (row.path.startsWith("rounds/")) return "rounds";
  if (
    row.vid === "baseline" ||
    row.path.startsWith("baseline/") ||
    row.path.startsWith("base/")
  ) {
    return "baseline";
  }
  return "variants";
}

/** artifacts 只读端点 URL（web SPEC §2.1；唯一被本面板消费的端点）。 */
export function artifactFileUrl(runId: string, path: string): string {
  return `/api/runs/${runId}/artifacts/file?path=${encodeURIComponent(path)}`;
}

/** 词法拼接 doc 相对目录 + 相对 src（处理 `.` / `..` 段；不触 fs）。 */
function resolveRel(docDir: string, rel: string): string {
  const stack = docDir ? docDir.split("/") : [];
  for (const part of rel.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") stack.pop();
    else stack.push(part);
  }
  return stack.join("/");
}

/**
 * 把 markdown 内的**相对**图片 src 改写为 artifacts 端点前缀（web §3.1）。
 *
 * 预改写成 `/api/runs/...` 前缀后，MarkdownText 的 rewriteImageSrc 按 `/api/`
 * 规则直通（零改复用）。http(s) / data: / blob: / file:// 与既有 `/api/` 前缀
 * 一律不动——file:// 交由 MarkdownText 现有 assets 改写约定处理。
 * **围栏代码块不改写**（``` / ~~~ 围栏内的图片语法是展示文本，不是真图）。
 */
export function rewriteDocImages(
  md: string,
  docPath: string,
  runId: string
): string {
  const docDir = docPath.includes("/")
    ? docPath.slice(0, docPath.lastIndexOf("/"))
    : "";
  const rewriteSegment = (segment: string): string =>
    segment.replace(
      /(!\[[^\]]*\]\()([^)\s]+)((?:\s+"[^"]*")?\s*\))/g,
      (match, pre: string, src: string, post: string) => {
        if (/^(https?:|data:|blob:|\/api\/|file:\/\/)/.test(src)) return match;
        const resolved = resolveRel(docDir, src.replace(/^\.\//, ""));
        if (!resolved) return match;
        return `${pre}${artifactFileUrl(runId, resolved)}${post}`;
      }
    );
  // 按围栏代码块切分（``` 与 ~~~ 两种围栏；捕获组保留围栏原文），只改写非代码段。
  return md
    .split(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$))/g)
    .map((part, i) => (i % 2 === 1 ? part : rewriteSegment(part)))
    .join("");
}

/** 清单 payload 解析：合法行 + 坏行计数 + payload 形状漂移标记（fail loud 披露）。 */
export function parseDocManifest(payload: unknown): {
  rows: DocRow[];
  invalid: number;
  /** payload 在场但缺 data 数组 → 整体形状漂移（INV-5 同口径：显形不静默空态）。 */
  malformed: boolean;
} {
  const p = payload as { data?: unknown } | null;
  if (!p) return { rows: [], invalid: 0, malformed: false };
  if (!Array.isArray(p.data)) return { rows: [], invalid: 0, malformed: true };
  const rows: DocRow[] = [];
  let invalid = 0;
  for (const raw of p.data) {
    const r = raw as Record<string, unknown> | null;
    if (!r || typeof r.path !== "string" || r.path.length === 0) {
      invalid++;
      continue;
    }
    const vid = typeof r.vid === "string" ? r.vid : "";
    // 变体行必须有 vid（无 vid 渲染不出卡片归属，归坏行；基线/轮次/规则按 path 判组不依赖 vid）。
    if (!vid && r.path.startsWith("variants/")) {
      invalid++;
      continue;
    }
    rows.push({
      path: r.path,
      vid,
      doc: typeof r.doc === "string" ? r.doc : r.path,
      status: typeof r.status === "string" ? r.status : "",
      updated_at: typeof r.updated_at === "string" ? r.updated_at : undefined,
    });
  }
  return { rows, invalid, malformed: false };
}

/** 状态徽标配色：success 绿、含 fail/insufficient 红、其余中性。 */
function statusClass(status: string): string {
  if (status === "success") return "text-emerald-600";
  if (status.includes("fail") || status.includes("insufficient")) {
    return "orca-text-failed";
  }
  return "orca-text-muted";
}

interface Selection {
  path: string;
  name: string;
}

export function ProfOptDocsPanel({ runId }: { runId: string }) {
  // 订阅收窄（与 ChartRenderer 同面）：selectCharts 全部输入。
  const events = useWorkflowStore((s) => s.events);
  const huge = useWorkflowStore((s) => s.huge);
  const serverOverview = useWorkflowStore((s) => s.serverOverview);
  const hugeFullyLoaded = useWorkflowStore((s) => s.hugeFullyLoaded);

  const manifest = useMemo(() => {
    const { groups } = selectCharts._from(
      events,
      huge,
      serverOverview,
      hugeFullyLoaded
    );
    const g = groups.find((x) => x.group === DOCS_LABEL);
    if (!g) return null;
    // 同 label 多 title 防御：取 seq 最大（最新一次幂等替换语义，web §2.3）。
    let latest = g.entries[0];
    for (const e of g.entries) if (e.seq > latest.seq) latest = e;
    return latest;
  }, [events, huge, serverOverview, hugeFullyLoaded]);

  const { rows, invalid, malformed } = useMemo(
    () => parseDocManifest(manifest?.payload),
    [manifest]
  );

  // 分组 + 变体按 vid 归卡（自然序 = 轮序：r1-01 < r2-01 < r10-01）。
  const grouped = useMemo(() => {
    const byGroup = new Map<DocGroupKey, DocRow[]>();
    for (const row of rows) {
      const key = docGroupOf(row);
      const arr = byGroup.get(key);
      if (arr) arr.push(row);
      else byGroup.set(key, [row]);
    }
    const variantRows = byGroup.get("variants") ?? [];
    const byVid = new Map<string, DocRow[]>();
    for (const row of variantRows) {
      const arr = byVid.get(row.vid);
      if (arr) arr.push(row);
      else byVid.set(row.vid, [row]);
    }
    const vids = Array.from(byVid.keys()).sort((a, b) =>
      a.localeCompare(b, undefined, { numeric: true })
    );
    return { byGroup, byVid, vids };
  }, [rows]);

  const [open, setOpen] = useState(true);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // 点选 → 拉正文（web §2.3：点开后才拉，AbortController 防过期竞态）。
  useEffect(() => {
    if (!selection) {
      setContent(null);
      setError(null);
      return;
    }
    const ctrl = new AbortController();
    let cancelled = false;
    setLoading(true);
    setContent(null);
    setError(null);
    fetch(artifactFileUrl(runId, selection.path), {
      method: "GET",
      signal: ctrl.signal,
    })
      .then(async (res) => {
        const text = await res.text();
        if (cancelled) return;
        if (res.ok) {
          setContent(text);
        } else if (res.status === 404) {
          setError(`文档不存在或路径不可访问（404）：${selection.name}`);
        } else if (res.status === 413) {
          setError(`文档超过 1MB 上限，无法预览（413）：${selection.name}`);
        } else {
          setError(`加载失败（HTTP ${res.status}）：${selection.name}`);
        }
      })
      .catch((e: unknown) => {
        if (cancelled || (e instanceof Error && e.name === "AbortError")) {
          return;
        }
        setError(`加载失败：${String(e)}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [selection, runId]);

  const isMarkdown = selection?.path.toLowerCase().endsWith(".md") ?? false;

  return (
    <section
      className="orca-border border-b"
      data-testid="profopt-docs-panel"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="orca-text-muted hover:orca-text flex w-full items-center gap-1 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide"
        data-testid="profopt-docs-toggle"
      >
        {open ? (
          <ChevronDown size={12} strokeWidth={1.5} aria-hidden />
        ) : (
          <ChevronRight size={12} strokeWidth={1.5} aria-hidden />
        )}
        分析文档（prof-opt）
      </button>
      {open && (
        <div className="max-h-64 overflow-auto px-3 pb-2">
          {manifest?.placeholder ? (
            <p className="text-xs orca-text-faint" data-testid="docs-huge-hint">
              超大 run：需「加载全部」后才能查看文档清单。
            </p>
          ) : !manifest ? (
            <p className="text-xs orca-text-faint" data-testid="docs-empty">
              暂无分析文档清单（prof-opt run 推送后显示）。
            </p>
          ) : (
            <>
              {malformed && (
                <p
                  className="mb-1 text-xs orca-text-failed"
                  data-testid="docs-schema-warning"
                >
                  ⚠️ 清单 payload 缺 data 数组（后端 schema 漂移？）
                </p>
              )}
              {invalid > 0 && (
                <p
                  className="mb-1 text-xs orca-text-failed"
                  data-testid="docs-schema-warning"
                >
                  ⚠️ {invalid} 个清单行缺 path 或 vid（后端 schema 漂移？）
                </p>
              )}
              {GROUP_ORDER.map(({ key, label }) => {
                if (key === "variants") {
                  if (grouped.vids.length === 0) return null;
                  return (
                    <div key={key} className="mb-2" data-testid={`docs-group-${key}`}>
                      <p className="orca-text-faint mb-1 text-[11px] font-medium">
                        {label}
                      </p>
                      {grouped.vids.map((vid) => {
                        const vidRows = grouped.byVid.get(vid) ?? [];
                        // 卡片级徽标取该 vid 首行 status（假设同 vid 文档状态一致，
                        // 契约未保证；行级徽标始终各自显示、为准）。
                        const st = vidRows[0]?.status ?? "";
                        return (
                          <div
                            key={vid}
                            className="orca-border mb-1 rounded p-1.5"
                            data-testid={`docs-variant-card-${vid}`}
                          >
                            <p className="flex items-center gap-2 text-xs">
                              <span className="font-medium">{vid}</span>
                              <span className={`text-[10px] ${statusClass(st)}`}>
                                {st}
                              </span>
                            </p>
                            {vidRows.map((row) => (
                              <DocItemButton
                                key={row.path}
                                row={row}
                                active={selection?.path === row.path}
                                onSelect={setSelection}
                              />
                            ))}
                          </div>
                        );
                      })}
                    </div>
                  );
                }
                const groupRows = grouped.byGroup.get(key) ?? [];
                if (groupRows.length === 0) return null;
                return (
                  <div key={key} className="mb-2" data-testid={`docs-group-${key}`}>
                    <p className="orca-text-faint mb-1 text-[11px] font-medium">
                      {label}
                    </p>
                    {groupRows.map((row) => (
                      <DocItemButton
                        key={row.path}
                        row={row}
                        active={selection?.path === row.path}
                        onSelect={setSelection}
                      />
                    ))}
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}
      {(selection || loading || error) && (
        <div className="orca-border border-t px-3 py-2">
          <p
            className="orca-text-muted mb-1 truncate text-[11px] font-medium"
            title={selection?.path}
            data-testid="doc-selected-name"
          >
            {selection?.name}
          </p>
          <div className="max-h-96 overflow-auto">
            {loading ? (
              <p
                className="orca-text-faint flex items-center gap-1 text-xs"
                data-testid="doc-loading"
              >
                <Loader2 size={12} strokeWidth={1.5} className="animate-spin" aria-hidden />
                拉取文档…
              </p>
            ) : error ? (
              <p className="text-xs orca-text-failed" data-testid="doc-fetch-error">
                {error}
              </p>
            ) : content != null && selection ? (
              isMarkdown ? (
                <MarkdownText>
                  {rewriteDocImages(content, selection.path, runId)}
                </MarkdownText>
              ) : (
                <FileContentView content={content} filePath={selection.path} />
              )
            ) : null}
          </div>
        </div>
      )}
    </section>
  );
}

/** 单个文档条目：名称 + 状态徽标 + 更新时间（web §3.1；不渲染正文）。 */
function DocItemButton({
  row,
  active,
  onSelect,
}: {
  row: DocRow;
  active: boolean;
  onSelect: (sel: Selection) => void;
}) {
  // 轮次组 doc 名同为 analysis.md → 用完整相对 path 作显示名消歧。
  const name = docGroupOf(row) === "rounds" ? row.path : row.doc;
  return (
    <button
      type="button"
      onClick={() => onSelect({ path: row.path, name })}
      title={row.path}
      data-testid="doc-item"
      className={`flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-xs hover:orca-bg-surface-2 ${
        active ? "orca-bg-surface-2 orca-accent" : ""
      }`}
    >
      <span className="min-w-0 flex-1 truncate">{name}</span>
      <span className={`shrink-0 text-[10px] ${statusClass(row.status)}`}>
        {row.status}
      </span>
      {row.updated_at ? (
        <span className="orca-text-faint shrink-0 text-[10px]">
          {row.updated_at}
        </span>
      ) : null}
    </button>
  );
}
