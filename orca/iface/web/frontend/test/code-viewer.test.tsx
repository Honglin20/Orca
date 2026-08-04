// test/code-viewer.test.tsx —— CodeViewer 组件测试（plan §前端测试 M7/M8 闭环）。
//
// 断言意图（Rule 9）：
//   - prism 生效：``<CodeViewer ext="py">{"def f(): pass"}</CodeViewer>`` → 输出含
//     ``class="token"`` span（prism 高亮 token 标记）。
//   - M8：超 50KB 文件回退 plain（无 ``class="token"``，含「回退纯文本」提示）。
//   - 无扩展名 / 未知扩展名 → 仍渲染（plain ``<code>``）。

import { afterEach, describe, expect, it } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { CodeViewer } from "@/components/conversation/CodeViewer";

describe("CodeViewer", () => {
  afterEach(() => cleanup());

  it("prism 生效：python 代码 → 输出含 class=\"token\" span", () => {
    const { container } = render(
      <CodeViewer text="def hello():\n    return 'world'" ext="py" />,
    );
    expect(container.querySelector('[data-testid="code-viewer"]')).toBeTruthy();
    // prism 高亮后产生的 span class 形如 "token keyword" / "token def" / "token string"
    const tokenSpans = container.querySelectorAll('code span[class^="token"]');
    expect(tokenSpans.length, "应至少有一个 prism token span").toBeGreaterThan(0);
  });

  it("M8：超 50KB 文件回退 plain（无 token span + 提示）", () => {
    // 构造 60KB 文本（> 50_000 阈值）。
    const big = "x = 1\n".repeat(15_000); // ~75_000 chars
    expect(big.length).toBeGreaterThan(50_000);
    const { container } = render(<CodeViewer text={big} ext="py" />);
    // 1. 无 prism token span（已回退 plain）。
    const tokenSpans = container.querySelectorAll('code span[class^="token"]');
    expect(tokenSpans.length, "超限不应产生 prism token").toBe(0);
    // 2. 含回退提示文案。
    const viewer = container.querySelector('[data-testid="code-viewer"]');
    expect(viewer?.textContent).toMatch(/回退纯文本/);
  });

  it("无扩展名（ext 为空）→ 渲染 plain code（无 token span，不崩）", () => {
    const { container } = render(<CodeViewer text="just plain text" ext="" />);
    expect(container.querySelector('[data-testid="code-viewer"]')).toBeTruthy();
    const tokenSpans = container.querySelectorAll('code span[class^="token"]');
    expect(tokenSpans.length).toBe(0);
  });

  it("未知扩展名（如 .log）→ 渲染 plain code（无 token span）", () => {
    const { container } = render(
      <CodeViewer text="INFO start\nERROR boom" ext="log" />,
    );
    const tokenSpans = container.querySelectorAll('code span[class^="token"]');
    expect(tokenSpans.length).toBe(0);
  });

  it("filename 渲染在 header；行号从 1 开始", () => {
    const { container } = render(
      <CodeViewer text="line1\nline2" ext="py" filename="foo.py" />,
    );
    const filename = container.querySelector('[data-testid="code-filename"]');
    expect(filename?.textContent).toBe("foo.py");
    // 第一行行号 = 1。
    const firstRowNum = container.querySelector("tbody tr td");
    expect(firstRowNum?.textContent).toBe("1");
  });
});
