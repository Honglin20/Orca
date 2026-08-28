// test/markdown-text.test.tsx —— D3/D5 MarkdownText 组件渲染契约 + img rewrite 集成。
//
// SPEC §10 acceptance 1：markdown 渲染节点必须存在。本测试断言：
//   - code inline vs block 分支（hasLang / 多行 position）
//   - pre / table / ul / ol / a / blockquote / img 各 component 映射
//   - img src 经 rewriteImageSrc 改写（runId 来自 store.activeRunId）
//
// 不重复 rewriteImageSrc 纯函数断言（见 image-rewrite.test.ts）；本测试只验证「组件树正确
// 把 markdown 解析后用 components.* 渲染 + img renderer 调用 rewriteImageSrc」。

import { describe, expect, test, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { MarkdownText } from "@/components/conversation/MarkdownText";
import { useWorkflowStore } from "@/stores/workflow-store";

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
  // SPEC audit-c: 测试 setup 默认进入 loaded 让 processEvent 可驱动（INV-7 不拦）
  useWorkflowStore.setState({ loadStatus: "loaded" });
});

describe("MarkdownText —— 组件映射 + img rewrite 集成", () => {
  test("段落 / 行内 code / 代码块 / 列表 / 表格 / 链接 / 引用 全渲染", () => {
    const md = [
      "hello **world** with `inline_code`",
      "",
      "- a",
      "- b",
      "",
      "1. one",
      "2. two",
      "",
      "```ts",
      "const x: number = 1;",
      "```",
      "",
      "| h1 | h2 |",
      "|----|----|",
      "| 1  | 2  |",
      "",
      "[link](https://x.com)",
      "",
      "> quoted",
    ].join("\n");
    const { container } = render(
      <div>
        <MarkdownText>{md}</MarkdownText>
      </div>
    );
    // 段落
    expect(container.querySelectorAll("p").length).toBeGreaterThan(0);
    // 行内 code + 块 code（语言围栏 ```` ts）
    const codes = container.querySelectorAll("code");
    expect(codes.length).toBeGreaterThanOrEqual(2);
    // 块 code（含 language-ts class）存在
    expect(container.querySelector("code.language-ts")).not.toBeNull();
    // pre 包裹块 code
    expect(container.querySelectorAll("pre").length).toBeGreaterThanOrEqual(1);
    // 无序列表 / 有序列表
    expect(container.querySelector("ul")).not.toBeNull();
    expect(container.querySelector("ol")).not.toBeNull();
    // 表格
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelectorAll("th").length).toBeGreaterThanOrEqual(2);
    // 链接
    const link = container.querySelector<HTMLAnchorElement>("a[href]");
    expect(link).not.toBeNull();
    expect(link!.href).toBe("https://x.com/");
    // 引用
    expect(container.querySelector("blockquote")).not.toBeNull();
  });

  // SPEC 2026-08-28 C2.4：token 级断言——仅断言 code.language-* class 存在在
  // ignoreMissing 下 0 门语言也绿（refractor 空手高亮仍回填 class），不足为凭。
  test("common 收录语言产出 .token.* 级元素（python / ts）", () => {
    const md = [
      "```python",
      "import os",
      "```",
      "",
      "```ts",
      "const x: number = 1;",
      "```",
    ].join("\n");
    const { container } = render(<MarkdownText>{md}</MarkdownText>);
    // python：import 关键字被高亮为 token.keyword（common 36 门收录 python）
    const py = container.querySelector("code.language-python");
    expect(py).not.toBeNull();
    expect(py!.querySelector(".token.keyword")).not.toBeNull();
    // ts：const 关键字（common 36 门收录 typescript）
    const ts = container.querySelector("code.language-ts");
    expect(ts).not.toBeNull();
    expect(ts!.querySelector(".token.keyword")).not.toBeNull();
  });

  test("未收录语言（tsx 不在 common 36 门）→ 原样渲染不报错（C2.1 fail-soft）", () => {
    const md = ["```tsx", "const X = () => <div />;", "```"].join("\n");
    const { container } = render(<MarkdownText>{md}</MarkdownText>);
    const code = container.querySelector("code.language-tsx");
    expect(code).not.toBeNull();
    // ignoreMissing：不高亮 → 无 token 元素，但文本原样保留
    expect(code!.querySelector(".token")).toBeNull();
    expect(code!.textContent).toContain("const X = () => <div />;");
  });

  test("img 相对路径 → /api/runs/<runId>/assets/<encoded>（D3 集成）", () => {
    useWorkflowStore.setState({ activeRunId: "run_xyz" });
    const md = "![alt](diagram.png)";
    const { container } = render(<MarkdownText>{md}</MarkdownText>);
    const img = container.querySelector<HTMLImageElement>("img");
    expect(img).not.toBeNull();
    expect(img!.src).toContain("/api/runs/run_xyz/assets/diagram.png");
    // lazy loading 属性（性能）
    expect(img!.getAttribute("loading")).toBe("lazy");
  });

  test("img 绝对 https URL → 直通（D3 不二次改写）", () => {
    useWorkflowStore.setState({ activeRunId: "run_xyz" });
    const md = "![alt](https://cdn.example.com/x.png)";
    const { container } = render(<MarkdownText>{md}</MarkdownText>);
    const img = container.querySelector<HTMLImageElement>("img");
    expect(img).not.toBeNull();
    expect(img!.src).toBe("https://cdn.example.com/x.png");
  });

  test("无 runId（store 未 loadRun）→ 相对路径直通（fail loud，不崩渲染）", () => {
    useWorkflowStore.setState({ activeRunId: null });
    const md = "![alt](local.png)";
    const { container } = render(<MarkdownText>{md}</MarkdownText>);
    const img = container.querySelector<HTMLImageElement>("img");
    expect(img).not.toBeNull();
    // 未改写：src 保留原值（浏览器会 404，但不崩）
    expect(img!.getAttribute("src")).toBe("local.png");
  });
});
