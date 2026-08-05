// components/pages/WorkflowBrowsePage.tsx —— workflow 浏览三栏页（plan idempotent-churning-lampson）。
//
// 照 RunDetailPage.tsx 用 ``useParams`` + ``PanelGroup``。三栏：
//   - 左：workflow 元信息 + 引用 agents 高亮 + 全量 agents 折叠区（missing:true 灰显）
//   - 中：FileTree（递归文件树）
//   - 右：.md/SKILL.md → <MarkdownText>；其它 → <CodeViewer>
//
// **m5**：切 workflow 时 store 同步清空 activeAgent/fileTree/activeFile（在 store action 内做）。

import { useEffect, lazy, Suspense } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Loader2, ArrowLeft } from "lucide-react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import {
  useWorkflowBrowseStore,
  type AgentSummary,
} from "@/stores/workflow-browse-store";
import { FileTree } from "@/components/conversation/FileTree";
import { CodeViewer } from "@/components/conversation/CodeViewer";

export function WorkflowBrowsePage() {
  const { name } = useParams<{ name: string }>();
  const navigate = useNavigate();
  const {
    activeWorkflow,
    workflowLoading,
    workflowError,
    activeAgent,
    fileTree,
    treeLoading,
    treeError,
    activeFile,
    fileLoading,
    fileError,
    openWorkflow,
    openAgent,
    openFile,
    reset,
  } = useWorkflowBrowseStore();

  // 只显示 workflow 引用的 agent（agents_referenced），按声明顺序。description/is_folder/
  // missing 从全量 resolve（all_agents）查表；referenced 名单中未命中 resolve 的仍显示（兜底空 meta）。
  // 不再把全量 unreferenced agent 拼进列表（用户诉求：浏览某 workflow 时只看它引用的 agent）。
  const allAgents = activeWorkflow?.all_agents ?? [];
  const referencedAgents: AgentSummary[] = (
    activeWorkflow?.agents_referenced ?? []
  ).map((n) => {
    const found = allAgents.find((a) => a.name === n);
    return found ?? { name: n, is_folder: false, description: "", missing: false };
  });

  useEffect(() => {
    if (!name) return;
    void openWorkflow(name);
    return () => {
      reset();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  if (!name) {
    return <p className="orca-text-muted p-4 text-sm">缺少 workflow name</p>;
  }

  return (
    <div className="orca-bg-app orca-text flex h-full flex-col">
      {/* 顶栏：返回 + workflow 名 + description */}
      <header className="orca-bg-surface orca-border flex h-12 items-center gap-3 border-b px-4">
        <button
          type="button"
          onClick={() => navigate("/workflows")}
          className="orca-text-muted hover:orca-text inline-flex items-center gap-1 text-xs"
          data-testid="back-btn"
        >
          <ArrowLeft size={14} strokeWidth={1.5} aria-hidden />
          返回
        </button>
        <h1 className="orca-text text-sm font-semibold">
          {workflowLoading && !activeWorkflow ? "加载中…" : (activeWorkflow?.meta.name ?? name)}
        </h1>
        {activeWorkflow?.meta.description && (
          <span className="orca-text-faint truncate text-xs">
            {activeWorkflow.meta.description}
          </span>
        )}
      </header>

      {workflowError && !activeWorkflow && (
        <div
          className="orca-border text-orca-failed border-b px-4 py-2 text-sm"
          data-testid="workflow-error"
        >
          加载失败：{workflowError}
        </div>
      )}

      <PanelGroup direction="horizontal" className="flex-1">
        {/* 左栏：workflow 元信息 + agents 列表 */}
        <Panel defaultSize={22} minSize={15} maxSize={35}>
          <div className="orca-bg-surface orca-border flex h-full flex-col border-r">
            <div className="orca-bg-surface-2 orca-text-muted orca-border border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide">
              Workflow
            </div>
            <div className="flex-1 overflow-y-auto">
              {workflowLoading && !activeWorkflow ? (
                <div
                  className="orca-text-faint flex items-center justify-center gap-2 py-8 text-xs"
                  data-testid="workflow-loading"
                >
                  <Loader2 size={12} strokeWidth={1.5} className="animate-spin" aria-hidden />
                  <span>加载 workflow…</span>
                </div>
              ) : activeWorkflow ? (
                <WorkflowMetaSection
                  name={activeWorkflow.meta.name}
                  entry={activeWorkflow.meta.entry}
                  inputsSchema={activeWorkflow.meta.inputs_schema}
                />
              ) : null}

              {activeWorkflow && (
                <div className="orca-border border-t">
                  <div className="orca-bg-surface-2 orca-text-muted orca-border border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide">
                    Agents（{referencedAgents.length}）
                  </div>
                  <ul data-testid="agent-list">
                    {referencedAgents.length === 0 && (
                      <li
                        className="orca-text-faint px-3 py-3 text-xs"
                        data-testid="agent-list-empty"
                      >
                        该 workflow 未引用任何 agent
                      </li>
                    )}
                    {referencedAgents.map((agent) => {
                      const isActive = activeAgent === agent.name;
                      return (
                        <li key={agent.name}>
                          <button
                            type="button"
                            onClick={() => void openAgent(agent.name)}
                            className={`flex w-full flex-col items-start px-3 py-1.5 text-left text-xs ${
                              isActive
                                ? "orca-bg-surface-2 orca-accent orca-border-accent border-l-2"
                                : "orca-text-muted hover:orca-bg-surface-2 border-l-2 border-transparent"
                            } ${agent.missing ? "opacity-50" : ""}`}
                            data-testid={`agent-row-${agent.name}`}
                          >
                            <span className="flex w-full items-center gap-1">
                              <span className="truncate font-medium">
                                {agent.name}
                              </span>
                              {agent.missing && (
                                <span
                                  className="text-orca-failed ml-auto text-xs"
                                  title="agent 解析失败（坏 frontmatter 或 TOCTOU）"
                                >
                                  missing
                                </span>
                              )}
                            </span>
                            {agent.description && (
                              <span className="orca-text-faint mt-0.5 truncate text-xs">
                                {agent.description}
                              </span>
                            )}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </div>
          </div>
        </Panel>

        <PanelResizeHandle className="group relative w-2 cursor-col-resize">
          <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-[rgb(var(--surface-2))] transition-colors group-hover:bg-[rgb(var(--accent))]" />
        </PanelResizeHandle>

        {/* 中栏：文件树 */}
        <Panel defaultSize={28} minSize={15}>
          <div className="orca-bg-surface orca-border flex h-full flex-col border-r">
            <div className="orca-bg-surface-2 orca-text-muted orca-border border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide">
              {activeAgent ? `Files · ${activeAgent}` : "Files"}
            </div>
            <div className="orca-bg-surface flex-1 overflow-y-auto">
              {!activeAgent && (
                <p
                  className="orca-text-faint px-3 py-4 text-xs"
                  data-testid="tree-empty"
                >
                  选择左侧 agent 查看其资源目录
                </p>
              )}
              {activeAgent && treeLoading && (
                <div
                  className="orca-text-faint flex items-center gap-2 px-3 py-4 text-xs"
                  data-testid="tree-loading"
                >
                  <Loader2 size={12} strokeWidth={1.5} className="animate-spin" aria-hidden />
                  加载文件树…
                </div>
              )}
              {treeError && (
                <p
                  className="text-orca-failed px-3 py-3 text-xs"
                  data-testid="tree-error"
                >
                  {treeError}
                </p>
              )}
              {activeAgent && fileTree && !treeLoading && (
                <FileTree
                  nodes={fileTree}
                  selectedPath={activeFile?.path ?? null}
                  onSelect={(p) => void openFile(p)}
                />
              )}
            </div>
          </div>
        </Panel>

        <PanelResizeHandle className="group relative w-2 cursor-col-resize">
          <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-[rgb(var(--surface-2))] transition-colors group-hover:bg-[rgb(var(--accent))]" />
        </PanelResizeHandle>

        {/* 右栏：文件内容 */}
        <Panel defaultSize={50} minSize={25}>
          <div className="orca-bg-surface flex h-full flex-col">
            <div className="orca-bg-surface-2 orca-text-muted orca-border border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide truncate">
              {activeFile?.path ?? "Preview"}
            </div>
            <div className="flex-1 overflow-hidden">
              {!activeAgent && (
                <p
                  className="orca-text-faint px-3 py-4 text-xs"
                  data-testid="preview-empty"
                >
                  选择文件查看内容
                </p>
              )}
              {fileError && (
                <p
                  className="text-orca-failed px-3 py-3 text-xs"
                  data-testid="file-error"
                >
                  {fileError}
                </p>
              )}
              {activeAgent && fileLoading && (
                <div
                  className="orca-text-faint flex items-center gap-2 px-3 py-4 text-xs"
                  data-testid="file-loading"
                >
                  <Loader2 size={12} strokeWidth={1.5} className="animate-spin" aria-hidden />
                  加载文件…
                </div>
              )}
              {activeFile && !fileLoading && (
                <FilePreview file={activeFile} />
              )}
            </div>
          </div>
        </Panel>
      </PanelGroup>
    </div>
  );
}

function WorkflowMetaSection({
  name,
  entry,
  inputsSchema,
}: {
  name: string;
  entry: string;
  inputsSchema: Record<
    string,
    { type: string; required: boolean; description?: string; default?: unknown }
  >;
}) {
  const inputKeys = Object.keys(inputsSchema);
  return (
    <div className="orca-text-muted px-3 py-2 text-xs" data-testid="workflow-meta">
      <div className="orca-text font-medium">{name}</div>
      <div className="orca-text-faint mt-1">
        entry: <code className="orca-accent">{entry}</code>
      </div>
      <div className="orca-text-faint mt-2">
        inputs（{inputKeys.length}）
      </div>
      {inputKeys.length > 0 && (
        <ul className="orca-text-faint mt-1 space-y-0.5">
          {inputKeys.map((k) => {
            const def = inputsSchema[k];
            return (
              <li key={k} className="truncate">
                <code className="orca-accent">{k}</code>
                <span className="orca-text-faint">
                  : {def.type}
                  {def.required ? " *" : ""}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// MarkdownText 重依赖（react-markdown + remark-math + rehype-katex + rehype-prism-plus
// 数 MB）——lazy 拆 chunk，仅在用户实际打开 .md 文件时加载（review 闭环 §五 可选优化 4）。
const MarkdownText = lazy(() =>
  import("@/components/conversation/MarkdownText").then((m) => ({
    default: m.MarkdownText,
  })),
);

function FilePreview({
  file,
}: {
  file: { path: string; text: string; ext: string; size: number };
}) {
  // .md → markdown 渲染；其它 → code viewer。SKILL.md / agent.md 等扩展名同为 .md，
  // 第一子句 ``ext === "md"`` 已覆盖，无需额外 ``endsWith("SKILL.md")`` 判断。
  if (file.ext === "md") {
    return (
      <div
        className="orca-text orca-text-muted px-4 py-3 overflow-y-auto h-full text-sm"
        data-testid="file-markdown"
      >
        <Suspense fallback={<div className="orca-text-faint text-xs">渲染 markdown…</div>}>
          <MarkdownText>{file.text}</MarkdownText>
        </Suspense>
      </div>
    );
  }
  return <CodeViewer text={file.text} ext={file.ext} filename={file.path} />;
}

export default WorkflowBrowsePage;
