// components/profopt/ProfOptDocsPanel.tsx —— prof-opt「文档」页签面板（web SPEC §3）。
//
// 数据流（铁律 1+5：selectors 是唯一 view 输入）：
//   chart socket → store.events → selectDocRowsWithContent（selectors 层合并跨推送
//   content，R-1）→ 本面板按组分区渲染**单行清单**（左栏）→ 点行 → 右栏正文预览。
//
// 布局（2026-09-09 用户拍板）：**左右分栏**——左栏固定宽清单（按 基线/变体/轮次/规则
// 分区，独立滚动），右栏渲染选中文档正文（占满剩余高，独立滚动）。清单项为单行：
// 13px 线性类型图标 + 纯文件名（truncate）+ 短时间戳（可选）+ 状态点；完整 path 进
// hover title（button 与文件名 span 双落点——截断的长文件名恰是 hover 高频位）。
//
// 正文三态（B4/C3）：
//   1. 行 content 非空（本推自带或历史合并）→ 直接渲染，**零请求**（异机可见——
//      与 workflows md 同等体验）；
//   2. content 空 + content_omitted === "true" → 显式提示「内容过大或本批未推送，
//      仅清单可读」，**不发起 fetch**（异机必 404，fail loud 不假试）；
//   3. content 空且未标注（legacy run）→ 回退既有 artifacts fetch（同机可用，
//      404/413 提示保留）。
//
// 清单 payload 契约（v6 §10.4 + C3；P4-T3 push_curves 推送方）：字段名 dict 行
// vid/doc/status/path/updated_at（+ 可选 content/content_omitted）。解析/分组
// （parseDocManifest/docGroupOf）在 selectors.ts；中文组名（GROUP_ORDER）留展示层。
//
// 分组（web §3.1：基线 / 变体按轮序 / 轮次按 Round N 子区 / 规则）从行内容确定性
// 派生；变体组内按 vid 归子组（docs-variant-card-<vid>，子组头带状态文本），轮次组
// 归「Round N」可折叠子区（数字升序）。
//
// 只读与白名单（web §5）：面板只消费清单内相对 path，唯一网络请求是 GET artifacts
// 端点（且仅 legacy 三态走它），无任何写入口。markdown 内相对图片改写为 artifacts
// 端点前缀（doc 目录相对解析）；http(s)/data/blob/file:// 不改写——交由
// MarkdownText 现有 assets 改写约定（file:// 落 assets 端点 = 已知破图降级）。
//
// 失败路径（web §5：降级提示不崩）：404 → 「不存在」提示；413 → 「超 1MB」提示；
// 其余非 2xx / 网络错误 → 显式错误行；清单行缺 path → schema warning（fail loud
// 计数，与 ChartRenderer chart-schema-warning 同模式），合法行照常渲染。

import { useEffect, useMemo, useState } from "react";
import {
  Braces,
  ChevronDown,
  ChevronRight,
  FileText,
  Loader2,
} from "lucide-react";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  selectDocRowsWithContent,
  docGroupOf,
  docRoundNoOf,
  type DocGroupKey,
  type DocRow,
} from "@/selectors";
import { MarkdownText } from "@/components/conversation/MarkdownText";
import { FileContentView } from "@/components/conversation/FileContentView";

/** 面板分组渲染顺序（中文组名属展示层，留组件；键与 selectors.DocGroupKey 对齐）。 */
const GROUP_ORDER: { key: DocGroupKey; label: string }[] = [
  { key: "baseline", label: "基线" },
  { key: "variants", label: "变体" },
  { key: "rounds", label: "轮次" },
  { key: "rules", label: "规则" },
];

/** 分组 / 子组共用的小节头（DRY：基线、变体 vid 子组、规则四处同款）。 */
function GroupLabel({ text }: { text: string }) {
  return (
    <p className="orca-text-faint mb-1 px-1.5 text-[11px] font-medium">{text}</p>
  );
}

/**
 * 文件类型线性图标（单行清单项前缀）。未知扩展 FileText 兜底（清单白名单只有
 * .md/.json，防御 payload 漂移时不崩样式）。
 */
function DocTypeIcon({ path }: { path: string }) {
  const ext = path.toLowerCase().split(".").pop() ?? "";
  const Icon = ext === "json" ? Braces : FileText;
  return (
    <Icon
      size={13}
      strokeWidth={1.5}
      className="orca-text-faint shrink-0"
      aria-hidden
    />
  );
}

/** ISO 时间戳压成 ``YYYY-MM-DD HH:MM``（完整 ISO 进该 span 的 title）。 */
function shortTime(iso: string): string {
  return iso.replace("T", " ").slice(0, 16);
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

/** 状态徽标配色：success 绿、含 fail/insufficient 红、其余中性（状态点/状态文本沿用）。 */
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
  // 订阅收窄：selectDocRowsWithContent 全部输入（与 ChartRenderer 同面）。
  const events = useWorkflowStore((s) => s.events);
  const serverOverview = useWorkflowStore((s) => s.serverOverview);
  const hugeFullyLoaded = useWorkflowStore((s) => s.hugeFullyLoaded);

  const { rows, invalid, malformed, placeholder, empty } = useMemo(
    () => selectDocRowsWithContent._from(events, serverOverview, hugeFullyLoaded),
    [events, serverOverview, hugeFullyLoaded]
  );

  const rowByPath = useMemo(
    () => new Map(rows.map((r) => [r.path, r])),
    [rows]
  );

  // 分组 + 变体按 vid 归子组（自然序 = 轮序：r1-01 < r2-01 < r10-01）
  // + 轮次按 docRoundNoOf 归「Round N」子区（数字升序；不匹配 rounds/<N>/ 形状
  // 的行落入 roundMisc 照常渲染——不静默隐藏任何清单行）。
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
    const byRound = new Map<number, DocRow[]>();
    const roundMisc: DocRow[] = [];
    for (const row of byGroup.get("rounds") ?? []) {
      const no = docRoundNoOf(row);
      if (no === null) {
        roundMisc.push(row);
        continue;
      }
      const arr = byRound.get(no);
      if (arr) arr.push(row);
      else byRound.set(no, [row]);
    }
    const roundNos = Array.from(byRound.keys()).sort((a, b) => a - b);
    return { byGroup, byVid, vids, byRound, roundNos, roundMisc };
  }, [rows]);

  const [selection, setSelection] = useState<Selection | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // 三态解析（渲染期派生，非 state）：事件 content（含历史合并）→ 直渲零请求；
  // omitted → 提示不 fetch；legacy → fetch 回退。
  const mergedRow = selection ? rowByPath.get(selection.path) : undefined;
  const inline =
    mergedRow && (mergedRow.content ?? "") !== "" ? mergedRow.content! : null;
  const omitted = !inline && mergedRow?.content_omitted === "true";

  // legacy 回退 fetch（web §2.3：点开后才拉，AbortController 防过期竞态）。
  useEffect(() => {
    if (!selection) {
      setContent(null);
      setError(null);
      setLoading(false);
      return;
    }
    if (inline !== null || omitted) {
      // 事件通道已有正文（或显式 omitted）——网络层零请求。
      setContent(null);
      setError(null);
      setLoading(false);
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
          setError(`文档不存在或路径不可访问（404）：${selection.path}`);
        } else if (res.status === 413) {
          setError(`文档超过 1MB 上限，无法预览（413）：${selection.path}`);
        } else {
          setError(`加载失败（HTTP ${res.status}）：${selection.path}`);
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
  }, [selection, runId, inline, omitted]);

  const body = inline ?? content;
  const isMarkdown = selection?.path.toLowerCase().endsWith(".md") ?? false;

  return (
    <section
      className="orca-bg-app flex h-full min-h-0 w-full"
      data-testid="profopt-docs-panel"
    >
      {/* 左：清单（固定宽，独立滚动） */}
      <aside className="orca-border orca-bg-surface flex w-72 shrink-0 flex-col overflow-y-auto border-r">
        <div className="orca-border orca-text-muted shrink-0 border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide">
          分析文档
        </div>
        <nav className="px-1.5 py-2">
          {placeholder ? (
            <p
              className="orca-text-faint flex items-center gap-1.5 px-1.5 text-xs"
              data-testid="docs-huge-hint"
            >
              <Loader2
                size={12}
                strokeWidth={1.5}
                className="animate-spin shrink-0"
                aria-hidden
              />
              完整清单后台拉取中…
            </p>
          ) : empty ? (
            <p className="orca-text-faint px-1.5 text-xs" data-testid="docs-empty">
              暂无分析文档清单（prof-opt run 推送后显示）。
            </p>
          ) : (
            <>
              {malformed && (
                <p
                  className="orca-text-failed mb-1 px-1.5 text-xs"
                  data-testid="docs-schema-warning"
                >
                  ⚠️ 清单 payload 缺 data 数组（后端 schema 漂移？）
                </p>
              )}
              {invalid > 0 && (
                <p
                  className="orca-text-failed mb-1 px-1.5 text-xs"
                  data-testid="docs-schema-warning"
                >
                  ⚠️ {invalid} 个清单行缺 path 或 vid（后端 schema 漂移？）
                </p>
              )}
              {GROUP_ORDER.map(({ key, label }) => {
                if (key === "variants") {
                  if (grouped.vids.length === 0) return null;
                  return (
                    <div
                      key={key}
                      className="mb-3"
                      data-testid={`docs-group-${key}`}
                    >
                      <GroupLabel text={label} />
                      {grouped.vids.map((vid) => {
                        const vidRows = grouped.byVid.get(vid) ?? [];
                        // 子组头状态取该 vid 首行 status（假设同 vid 文档状态一致，
                        // 契约未保证；行级状态点始终各自显示、为准）。
                        const st = vidRows[0]?.status ?? "";
                        return (
                          <div
                            key={vid}
                            className="mb-2"
                            data-testid={`docs-variant-card-${vid}`}
                          >
                            <p className="orca-text-faint mb-0.5 flex items-center gap-1.5 px-1.5 text-[11px]">
                              <span className="orca-text-muted font-medium">
                                {vid}
                              </span>
                              <span
                                className={`text-[10px] ${statusClass(st)}`}
                                title={st}
                              >
                                ● {st}
                              </span>
                            </p>
                            <DocList rows={vidRows} selection={selection} onSelect={setSelection} />
                          </div>
                        );
                      })}
                    </div>
                  );
                }
                if (key === "rounds") {
                  if (
                    grouped.roundNos.length === 0 &&
                    grouped.roundMisc.length === 0
                  )
                    return null;
                  return (
                    <div
                      key={key}
                      className="mb-3"
                      data-testid="docs-group-rounds"
                    >
                      <GroupLabel text={label} />
                      {grouped.roundNos.map((no) => (
                        <RoundSection
                          key={no}
                          no={no}
                          rows={grouped.byRound.get(no) ?? []}
                          selection={selection}
                          onSelect={setSelection}
                        />
                      ))}
                      {grouped.roundMisc.length > 0 && (
                        // 非 rounds/<N>/ 形状的轮次组行：照常渲染，testid 锚定
                        // 「不静默丢行」契约（docs-round-misc）。
                        <DocList
                          className="mt-1"
                          testid="docs-round-misc"
                          rows={grouped.roundMisc}
                          selection={selection}
                          onSelect={setSelection}
                        />
                      )}
                    </div>
                  );
                }
                const groupRows = grouped.byGroup.get(key) ?? [];
                if (groupRows.length === 0) return null;
                return (
                  <div
                    key={key}
                    className="mb-3"
                    data-testid={`docs-group-${key}`}
                  >
                    <GroupLabel text={label} />
                    <DocList rows={groupRows} selection={selection} onSelect={setSelection} />
                  </div>
                );
              })}
            </>
          )}
        </nav>
      </aside>
      {/* 右：正文（占满剩余高，独立滚动） */}
      <div className="flex min-w-0 flex-1 flex-col">
        {(selection || loading || error) ? (
          <>
            <div
              className="orca-border orca-text-muted shrink-0 truncate border-b px-3 py-2 text-xs font-medium"
              title={selection?.path}
              data-testid="doc-selected-name"
            >
              {selection?.name}
            </div>
            <div className="flex-1 overflow-auto p-4">
              {loading ? (
                <p
                  className="orca-text-faint flex items-center gap-1 text-xs"
                  data-testid="doc-loading"
                >
                  <Loader2
                    size={12}
                    strokeWidth={1.5}
                    className="animate-spin"
                    aria-hidden
                  />
                  拉取文档…
                </p>
              ) : body != null && selection ? (
                isMarkdown ? (
                  <MarkdownText>
                    {rewriteDocImages(body, selection.path, runId)}
                  </MarkdownText>
                ) : (
                  <FileContentView content={body} filePath={selection.path} />
                )
              ) : omitted ? (
                <p
                  className="orca-text-faint text-xs"
                  data-testid="doc-omitted"
                >
                  内容过大或本批未推送，仅清单可读。
                </p>
              ) : error ? (
                <p className="orca-text-failed text-xs" data-testid="doc-fetch-error">
                  {error}
                </p>
              ) : null}
            </div>
          </>
        ) : (
          <div className="orca-text-faint flex h-full items-center justify-center px-4 text-center text-xs">
            左侧选择文档查看内容
          </div>
        )}
      </div>
    </section>
  );
}

/** 轮次子区（Round N，可折叠；默认展开）：N 为数字升序，组内单行清单。 */
function RoundSection({
  no,
  rows,
  selection,
  onSelect,
}: {
  no: number;
  rows: DocRow[];
  selection: Selection | null;
  onSelect: (sel: Selection) => void;
}) {
  // 折叠仅 UI 交互态（非业务真相，铁律 2），与 ChartGroup 同模式。
  const [open, setOpen] = useState(true);
  return (
    <div className="mb-2" data-testid={`docs-round-${no}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="orca-text-muted hover:orca-text flex w-full items-center gap-1 px-1.5 py-0.5 text-left text-xs font-medium"
        data-testid={`docs-round-toggle-${no}`}
      >
        {open ? (
          <ChevronDown size={12} strokeWidth={1.5} aria-hidden />
        ) : (
          <ChevronRight size={12} strokeWidth={1.5} aria-hidden />
        )}
        Round {no}
        <span className="orca-text-faint text-[10px]">{rows.length} 份</span>
      </button>
      {open && (
        <DocList
          className="mt-0.5"
          rows={rows}
          selection={selection}
          onSelect={onSelect}
        />
      )}
    </div>
  );
}

/** 清单列表（DRY：基线/变体内层/轮次子区/roundMisc 四处共用同一接线）。 */
function DocList({
  rows,
  selection,
  onSelect,
  className,
  testid,
}: {
  rows: DocRow[];
  selection: Selection | null;
  onSelect: (sel: Selection) => void;
  className?: string;
  testid?: string;
}) {
  return (
    <div className={className} data-testid={testid}>
      {rows.map((row) => (
        <DocItemRow
          key={row.path}
          row={row}
          active={selection?.path === row.path}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

/**
 * 单个清单行（单行行卡）：类型图标 + 纯文件名（truncate）+ 短时间戳（可选）+
 * 状态点。显示名一律 ``row.doc``；完整相对 path 进 hover title（button 与文件名
 * span 双落点——截断的长文件名恰是 hover 高频位），完整 ISO 时间进时间戳 title。
 */
function DocItemRow({
  row,
  active,
  onSelect,
}: {
  row: DocRow;
  active: boolean;
  onSelect: (sel: Selection) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect({ path: row.path, name: row.doc })}
      title={row.path}
      data-testid="doc-item"
      className={`orca-text-muted hover:orca-bg-surface-2 hover:orca-text flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-xs ${
        active ? "orca-bg-surface-2 orca-accent font-medium" : ""
      }`}
    >
      <DocTypeIcon path={row.path} />
      <span className="min-w-0 flex-1 truncate" title={row.path}>
        {row.doc}
      </span>
      {row.updated_at ? (
        <span
          className="orca-text-faint shrink-0 text-[9px]"
          title={row.updated_at}
        >
          {shortTime(row.updated_at)}
        </span>
      ) : null}
      <span
        className={`shrink-0 text-[10px] ${statusClass(row.status)}`}
        title={row.status}
      >
        ●
      </span>
    </button>
  );
}
