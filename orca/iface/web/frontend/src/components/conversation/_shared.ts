// components/conversation/_shared.ts —— 共享工具（DRY：四处重复 safeJson 抽出）。

/** JSON.stringify 失败时回退到 String——任何渲染 raw payload 处都用此（不抛）。 */
export function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}
