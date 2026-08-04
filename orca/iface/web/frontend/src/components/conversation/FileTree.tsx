// components/conversation/FileTree.tsx —— workflow 浏览页专用的递归文件树。
//
// plan idempotent-churning-lampson。**新写**（不复用 FileContentView.tsx——那是
// conversation read 工具结果展示，单文件纯文本；本组件是 agent 资源目录递归树）。
//
// 设计：
//   - 递归 ``<TreeNode>``，目录可折叠（chevron + name），文件 click 触发 ``onSelect``。
//   - 当前选中文件 path 高亮（accent 左 border）。
//   - 全用 ``orca-*`` utility（grep 守门测试禁 bg-slate-* / rounded-lg 等）。
//   - 缺省初始展开顶层目录（让用户立刻看到目录结构）。

import { useState } from "react";
import { ChevronDown, ChevronRight, File, Folder } from "lucide-react";
import type { TreeNode as TreeNodeData } from "@/stores/workflow-browse-store";

interface FileTreeProps {
  nodes: TreeNodeData[];
  selectedPath: string | null;
  onSelect: (path: string) => void;
}

export function FileTree({ nodes, selectedPath, onSelect }: FileTreeProps) {
  return (
    <ul
      className="orca-text orca-text-muted text-xs"
      role="tree"
      data-testid="file-tree"
    >
      {nodes.map((node) => (
        <TreeRow
          key={node.path}
          node={node}
          depth={0}
          selectedPath={selectedPath}
          onSelect={onSelect}
        />
      ))}
    </ul>
  );
}

interface TreeRowProps {
  node: TreeNodeData;
  depth: number;
  selectedPath: string | null;
  onSelect: (path: string) => void;
}

function TreeRow({ node, depth, selectedPath, onSelect }: TreeRowProps) {
  const [open, setOpen] = useState(true);
  const padLeft = 8 + depth * 12;

  if (node.is_dir) {
    return (
      <li role="treeitem" aria-expanded={open}>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="orca-text-muted hover:orca-bg-surface-2 flex w-full items-center gap-1 py-1 text-left"
          style={{ paddingLeft: padLeft }}
          data-testid={`tree-dir-${node.path}`}
        >
          {open ? (
            <ChevronDown size={12} strokeWidth={1.5} aria-hidden />
          ) : (
            <ChevronRight size={12} strokeWidth={1.5} aria-hidden />
          )}
          <Folder size={12} strokeWidth={1.5} aria-hidden />
          <span className="truncate">{node.name}</span>
        </button>
        {open && node.children && (
          <ul>
            {node.children.map((child) => (
              <TreeRow
                key={child.path}
                node={child}
                depth={depth + 1}
                selectedPath={selectedPath}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
      </li>
    );
  }

  const selected = selectedPath === node.path;
  return (
    <li role="treeitem" aria-selected={selected}>
      <button
        type="button"
        onClick={() => onSelect(node.path)}
        className={`flex w-full items-center gap-1 py-1 text-left ${
          selected
            ? "orca-bg-surface-2 orca-accent orca-border-accent border-l-2"
            : "orca-text-muted hover:orca-bg-surface-2 border-l-2 border-transparent"
        }`}
        style={{ paddingLeft: padLeft }}
        data-testid={`tree-file-${node.path}`}
      >
        <span className="inline-flex w-3" />
        <File size={12} strokeWidth={1.5} aria-hidden />
        <span className="truncate">{node.name}</span>
      </button>
    </li>
  );
}
