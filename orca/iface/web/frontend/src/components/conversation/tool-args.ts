// components/conversation/tool-args.ts —— 工具参数 smart 一行摘要 + args 规范化（SPEC §5.3）。
//
// 抄 AgentHarness previewArgs 设计（不抄 store 依赖），适配 Orca 的 WebEvent shape：
//   - bash → ``$ <cmd>`` 截断
//   - read / read_file / read_text_file → basename(path)
//   - write / write_file / edit / edit_file → basename(path)
//   - render_chart → ``<chart_type> | <title>``（如无 title → 仅 chart_type）
//   - 其它 → ``k=val, ...`` 截断 60
//
// **fail loud**：parse 异常兜底返回原始字符串截断；不静默吞错。

import { safeJson } from "./_shared";

// 重新导出 shared safeJson 让本目录组件单一 import 入口（DRY）。
export { safeJson };

const TRUNCATE_MAX = 60;

/** basename：取 path 末段（对 Linux/macOS/Windows 路径兼容）。 */
export function basename(path: string): string {
  if (!path) return "";
  const norm = path.replace(/\\/g, "/");
  const parts = norm.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function truncate(s: string, max = TRUNCATE_MAX): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
}

/** 把 args（可能为 string / object / JSON 字符串）规范化为 record（失败 → null）。 */
export function normalizeArgs(
  args: unknown
): Record<string, unknown> | null {
  if (args == null) return null;
  if (typeof args === "object" && !Array.isArray(args)) {
    return args as Record<string, unknown>;
  }
  if (typeof args === "string") {
    try {
      const p = JSON.parse(args);
      if (p && typeof p === "object" && !Array.isArray(p)) {
        return p as Record<string, unknown>;
      }
      return { _raw: args };
    } catch {
      return { _raw: args };
    }
  }
  return null;
}

function getStringArg(
  args: unknown,
  key: string
): string | undefined {
  const obj = normalizeArgs(args);
  if (!obj) return undefined;
  const v = obj[key];
  return typeof v === "string" ? v : undefined;
}

/** 文件类工具：read / write / edit 及 AH 同义别名。 */
const FILE_TOOLS = new Set([
  "read",
  "read_file",
  "read_text_file",
  "write",
  "write_file",
  "edit",
  "edit_file",
]);

/** Smart 一行摘要（SPEC §5.3 工具展开）。 */
export function previewArgs(
  toolName: string | undefined,
  args: unknown
): string {
  if (args == null) return "";
  if (typeof args === "string") return truncate(args);
  const name = toolName ?? "";

  if (name === "bash" || name === "shell" || name === "sh") {
    const cmd = getStringArg(args, "command") ?? getStringArg(args, "cmd");
    if (cmd) return "$ " + truncate(cmd, TRUNCATE_MAX - 2);
  }
  if (FILE_TOOLS.has(name)) {
    const path =
      getStringArg(args, "path") ?? getStringArg(args, "file_path");
    if (path) return truncate(basename(path));
  }
  if (name === "render_chart") {
    const norm = normalizeArgs(args);
    if (norm) {
      const ct = typeof norm.chart_type === "string" ? norm.chart_type : "chart";
      const t = typeof norm.title === "string" ? norm.title : "";
      return t ? `${ct} | ${truncate(t, 40)}` : ct;
    }
  }

  // 通用 fallback：k=val, … 截断 60
  const norm = normalizeArgs(args);
  if (!norm) return truncate(String(args));
  const entries = Object.entries(norm);
  if (entries.length === 0) return "";
  const parts = entries.map(([k, v]) => {
    const valStr = typeof v === "string" ? v : safeJson(v);
    return `${k}=${truncate(valStr ?? "", 30)}`;
  });
  return truncate(parts.join(", "));
}

/** pretty-print args block（展开视图用）。 */
export function formatArgsBlock(args: unknown): string {
  if (args == null) return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

export { FILE_TOOLS };
