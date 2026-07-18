// components/chart/LazyChartWidget.tsx —— IntersectionObserver 懒挂（SPEC §5.4）。
//
// 300px skeleton 直到进入视口，永久挂载（进入视口一次后 disconnect，防反复挂载抖动）。
// Recharts ResponsiveContainer 在不可见时 measure 不到尺寸 → 懒挂也避免视口外 chart 计算。
//
// 单一职责：本组件只做「在视口内？」的判定 + skeleton 占位；chart 渲染委托
// ChartWidget（OCP：扩展 chart 类型不改本组件）。
//
// 注：测试环境 happy-dom 提供了 IntersectionObserver 构造器但不触发 callback，
// ``test/setup.ts`` 注入 IOStub 让所有元素立即 intersecting（同步可见）。下面
// ``typeof IntersectionObserver === "undefined"`` 分支只在无 IO 的旧浏览器触发。

import { useEffect, useRef, useState } from "react";
import type { ChartPayload } from "./types";
import { ChartWidget } from "./ChartWidget";

interface LazyChartWidgetProps {
  payload: ChartPayload;
}

export function LazyChartWidget({ payload }: LazyChartWidgetProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      // 旧浏览器无 IO → 直接可见（fail open：渲染优于懒挂）。
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setVisible(true);
            io.disconnect(); // 永久挂载：进入视口一次后不再卸载（防反复 measure 抖动）
          }
        }
      },
      { rootMargin: "300px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  if (!visible) {
    return (
      <div
        ref={ref}
        className="aspect-[4/3] w-full animate-pulse rounded orca-bg-surface-2"
        data-testid="chart-skeleton"
      />
    );
  }
  return (
    <div ref={ref}>
      <ChartWidget payload={payload} />
    </div>
  );
}

