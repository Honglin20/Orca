// route-edge.ts —— route_taken 边 key 统一 helper（SPEC 2026-08-28 C3.5）。
//
// 放独立 src 根模块：workflow-store（takenEdgeKeys 派生）与 graph 层（WorkflowGraph/
// graph-layout 边匹配）都要用，若放任一侧会造成 store↔组件 反向依赖。双方都只依赖本模块。

/** route 边唯一 key：`${from}->${to}`（markTakenEdges / findBackEdges / takenEdgeKeys 同源）。 */
export function routeEdgeKey(from: string, to: string): string {
  return `${from}->${to}`;
}
