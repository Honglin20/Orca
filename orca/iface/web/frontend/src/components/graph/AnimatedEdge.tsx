// components/graph/AnimatedEdge.tsx —— DAG 边（route_taken 高亮 + 回环边弧形）。
//
// - 普通 taken 边：实线 + 蓝色（route_taken 走过的）
// - 普通 untaken 边：浅灰虚线
// - 回环边（back-edge）：弧形（high curvature）+ 红色调，渲染保持原方向（SPEC §1.4）

import { memo } from "react";
import { BaseEdge, getSmoothStepPath, type EdgeProps } from "@xyflow/react";

interface AnimatedEdgeData {
  taken?: boolean;
  isBackEdge?: boolean;
  isParallel?: boolean;
}

function AnimatedEdgeBase({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps) {
  const d = (data ?? {}) as AnimatedEdgeData;
  const taken = d.taken ?? false;
  const isBack = d.isBackEdge ?? false;

  const [path] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    // 回环边圆角更大（视觉上明显是个回路）
    borderRadius: isBack ? 24 : 6,
  });

  const stroke = taken ? "#3B82F6" : isBack ? "#F59E0B" : "#CBD5E1";
  const strokeWidth = taken ? 2.5 : isBack ? 1.8 : 1;
  const strokeDasharray = taken ? undefined : "4 3";

  return (
    <BaseEdge
      id={id}
      path={path}
      style={{
        stroke,
        strokeWidth,
        strokeDasharray,
        // taken 边加流动动画（dash 流），视觉强调「正在走」
        animation: taken ? "dashflow 0.6s linear infinite" : undefined,
      }}
      data-testid={`edge-${id}`}
    />
  );
}

export const AnimatedEdge = memo(AnimatedEdgeBase);
