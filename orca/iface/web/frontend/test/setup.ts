import "@testing-library/jest-dom/vitest";

// vitest global setup. happy-dom provides window/document. fetch and WebSocket are mocked per-test
// (vi.stubGlobal / vi.spyOn) so tests stay hermetic.
//
// ── recharts ResponsiveContainer：happy-dom 不计算布局，ResponsiveContainer 读父元素 clientWidth/clientHeight，
// 为 0 时 recharts 不渲染子组件（line/bar/scatter 都不产出 SVG path）。phase 9d chart 测试需要
// 非非零尺寸 → 这里给所有 Element 打桩 clientWidth/clientHeight/getBoundingClientRect（足够让
// recharts 渲 ResponsiveContainer 在 happy-dom 下产出 SVG）。仅在 test 环境生效，不影响 prod。
const STUB_W = 600;
const STUB_H = 400;
Object.defineProperties(Element.prototype, {
  clientWidth: { configurable: true, get: () => STUB_W },
  clientHeight: { configurable: true, get: () => STUB_H },
  offsetWidth: { configurable: true, get: () => STUB_W },
  offsetHeight: { configurable: true, get: () => STUB_H },
  scrollWidth: { configurable: true, get: () => STUB_W },
  scrollHeight: { configurable: true, get: () => STUB_H },
});
Element.prototype.getBoundingClientRect = function () {
  return {
    width: STUB_W,
    height: STUB_H,
    top: 0,
    left: 0,
    right: STUB_W,
    bottom: STUB_H,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
};

// ── IntersectionObserver stub（happy-dom 提供 IO 构造器但不触发 callback）
// LazyChartWidget 用 IO 做懒挂；测试环境下让所有元素立即「intersecting」→ 同步可见。
class IOStub {
  callback: (entries: { isIntersecting: boolean; target: Element }[]) => void;
  elements: Set<Element> = new Set();
  constructor(cb: (entries: { isIntersecting: boolean; target: Element }[]) => void) {
    this.callback = cb;
  }
  observe(target: Element) {
    this.elements.add(target);
    // 立即触发 intersecting=true（测试环境：所有 chart 默认可见）
    this.callback([{ isIntersecting: true, target }]);
  }
  unobserve(target: Element) {
    this.elements.delete(target);
  }
  disconnect() {
    this.elements.clear();
  }
  takeRecords() {
    return [];
  }
}
Object.defineProperty(globalThis, "IntersectionObserver", {
  configurable: true,
  writable: true,
  value: IOStub,
});

