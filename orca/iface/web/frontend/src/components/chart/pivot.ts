// components/chart/pivot.ts —— hue 长格式 → 宽格式 pivot 共享 helper（DRY，迁移自 AgentHarness hue 分组逻辑）。
//
// line/bar/scatter 的 hue 分组都用「按 hue 值分系列 → 每个 x 一行、各 hue 值作列」的 pivot。
// 抽出共享避免三处重复（SPEC §代码质量底线 6 禁三处以上重复）。

/**
 * 長格式 record array → 按 hue 分系列的宽格式 pivot。
 *
 * @param data 原始扁平 record（长格式：每行一个 (x, hue, y) 元组）
 * @param xKey x 轴列名
 * @param hueKey 分系列列名（每个唯一值 → 一列/一条线/一组柱）
 * @param yKey y 轴值列名
 * @returns { pivoted: 宽格式 rows；hueValues: 有序 hue 值（首次出现序） }
 */
export function pivotByHue(
  data: Record<string, unknown>[],
  xKey: string,
  hueKey: string,
  yKey: string,
): { pivoted: Record<string, unknown>[]; hueValues: string[] } {
  const hueValues = Array.from(new Set(data.map((d) => String(d[hueKey]))));
  const xMap = new Map<string, Record<string, unknown>>();
  data.forEach((d) => {
    const xv = String(d[xKey]);
    if (!xMap.has(xv)) xMap.set(xv, { [xKey]: d[xKey] });
    const row = xMap.get(xv)!;
    row[String(d[hueKey])] = d[yKey];
  });
  return { pivoted: Array.from(xMap.values()), hueValues };
}
