// components/chart/ChartCaption.tsx —— 图下小字说明（render_chart ``caption`` 字段渲染）。
//
// 解「图表看不懂」根因 C（render_chart 无轴标签）：``caption`` 让调用方能解释数据来源/单位/
// ★ 含义等，前端统一渲染样式（小字、淡色）。空串 = 不渲染（向后兼容旧 tape）。
//
// 单一职责：只渲染传入的 ``text``。组件自带空守卫（defense-in-depth）；调用方约定
// ``{caption && <ChartCaption ... />}`` 跳过空串以避免无意义 mount。

interface ChartCaptionProps {
  /** caption 文案。空串则组件自身也返回 null（与调用方 guard 等效，defense-in-depth）。 */
  text: string;
}

export function ChartCaption({ text }: ChartCaptionProps) {
  if (!text) return null;
  return (
    <p
      className="mt-1 text-[10px] orca-text-faint"
      data-testid="chart-caption"
    >
      {text}
    </p>
  );
}
