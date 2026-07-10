// test/status-line.test.tsx —— D7 StatusLine 折叠修正（SPEC §5.3）。
//
// Chunk B 把 StatusLine 做成单行不可折叠（YAGNI 偏离）；SPEC §5.3 折叠规则明确这些状态行
// 默认折叠。本测试断言修正后行为：
//   - 默认折叠（aria-expanded=false），detail 不在 DOM
//   - 点击 toggle 展开（aria-expanded=true），detail 可见
//   - validator_failed 例外：默认展开（错误高敏感）

import { describe, expect, test, afterEach } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { StatusLine } from "@/components/conversation/StatusLine";
import type { WebEvent } from "@/types/events";

afterEach(() => cleanup());

function mkEvent(
  type: WebEvent["type"],
  data: Record<string, unknown>,
): WebEvent {
  return {
    seq: 1,
    type,
    timestamp: 0,
    node: null,
    session_id: null,
    data,
  };
}

describe("StatusLine —— D7 SPEC §5.3 折叠修正", () => {
  test("retry_started 默认折叠（aria-expanded=false，无 detail）", () => {
    render(
      <StatusLine
        event={mkEvent("retry_started", {
          attempt: 1,
          max_attempts: 3,
          kind: "transient",
        })}
      />
    );
    const toggle = screen.getByTestId("status-line-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("status-line-detail")).not.toBeInTheDocument();
  });

  test("点击 toggle → 展开（aria-expanded=true，detail 可见，含 data JSON）", () => {
    render(
      <StatusLine
        event={mkEvent("retry_started", {
          attempt: 1,
          max_attempts: 3,
          kind: "transient",
        })}
      />
    );
    fireEvent.click(screen.getByTestId("status-line-toggle"));
    const toggle = screen.getByTestId("status-line-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const detail = screen.getByTestId("status-line-detail");
    expect(detail.textContent).toContain("attempt");
    expect(detail.textContent).toContain("transient");
  });

  test("再次点击 → 折叠回去（detail 消失）", () => {
    render(
      <StatusLine
        event={mkEvent("wait_started", { duration_seconds: 5, reason: "x" })}
      />
    );
    const toggle = screen.getByTestId("status-line-toggle");
    fireEvent.click(toggle);
    expect(screen.getByTestId("status-line-detail")).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.queryByTestId("status-line-detail")).not.toBeInTheDocument();
  });

  test("validator_failed 默认展开（错误信息高敏感，SPEC §5.3 闭 review #29）", () => {
    render(
      <StatusLine
        event={mkEvent("validator_failed", { message: "boom" })}
      />
    );
    const toggle = screen.getByTestId("status-line-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("status-line-detail")).toBeInTheDocument();
  });

  test("无 data 的事件 → 无 toggle（无展开价值，保持单行）", () => {
    render(<StatusLine event={mkEvent("validator_passed", {})} />);
    expect(screen.queryByTestId("status-line-toggle")).not.toBeInTheDocument();
    expect(screen.queryByTestId("status-line-detail")).not.toBeInTheDocument();
    // 单行摘要仍可见
    expect(screen.getByTestId("status-line").textContent).toContain(
      "validator passed"
    );
  });
});
