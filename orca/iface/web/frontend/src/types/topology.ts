// types/topology.ts —— 静态 DAG 拓扑（phase 9c）。
//
// 拓扑来源决策（SPEC §0.1 铁律「tape 是唯一真相」）：后端 ``make_workflow_started`` 把
// 紧凑拓扑摘要放进 ``workflow_started.data.topology``（见 orca/run/lifecycle.py），前端
// store 的 ``workflow_started`` handler 提取到 ``workflowDef``。这样：
//   - live：第一个事件即拿到拓扑，DAG 立刻布局（无需等 route_taken 增量拼边）。
//   - 历史 run replay：同样从事件拿，单一数据源，无额外 endpoint。
// 摘要只含 name+kind / routes(from→to) / parallel(name+branches) —— **非完整 yaml**，保持
// 事件 payload 小（foreach body 不展开：动态并行，运行时按 foreach_started 渲染分支）。

/** 节点 kind（对齐 orca/schema/workflow.py 顶层 AnnotatedNode 判别联合）。 */
export type NodeKind = "agent" | "script" | "set" | "foreach";

/** topology 摘要里的单个节点（name+kind，无 prompt/command 等执行细节）。 */
export interface TopologyNode {
  name: string;
  kind: NodeKind;
}

/** topology 摘要里的边（from→to，when 缺省=兜底 catch-all）。$end 为终止哨兵。 */
export interface TopologyRoute {
  from: string;
  to: string;
  when?: string;
}

/** topology 摘要里的静态并行组（branches 为已知 node 名）。 */
export interface TopologyParallel {
  name: string;
  branches: string[];
}

/** workflow_started.data.topology 的形状（对齐 orca/run/lifecycle._topology_summary）。 */
export interface WorkflowTopology {
  entry: string;
  nodes: TopologyNode[];
  routes: TopologyRoute[];
  parallel: TopologyParallel[];
}
