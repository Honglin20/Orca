// test/image-rewrite.test.ts —— D3 markdown 图片 URL rewrite（SPEC §0 D10）。
//
// 断言意图（Rule 9）：markdown 内的相对 / file:// / 裸文件名路径必须改写到
// ``/api/runs/<runId>/assets/<encoded>``；绝对 http(s) / data: / blob: / 已是 /api/ 的不改写。

import { describe, expect, it } from "vitest";
import { rewriteImageSrc } from "@/components/conversation/MarkdownText";

describe("rewriteImageSrc（D10 image URL rewrite）", () => {
  const RUN_ID = "run_abc123";

  it("相对路径 → /api/runs/<id>/assets/<encoded>", () => {
    expect(rewriteImageSrc("diagram.png", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/diagram.png`
    );
    expect(rewriteImageSrc("./sub/dir/img.png", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/sub%2Fdir%2Fimg.png`
    );
  });

  it("裸文件名（无目录）→ endpoint", () => {
    expect(rewriteImageSrc("cover.jpg", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/cover.jpg`
    );
  });

  it("file:// URL → 剥前缀 + 取相对资源", () => {
    expect(rewriteImageSrc("file://diagram.png", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/diagram.png`
    );
    // file:// + host（如 localhost）剥前缀 host 段
    expect(rewriteImageSrc("file://localhost/x.png", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/x.png`
    );
    // 绝对路径 ``file:///tmp/foo.png``：剥前缀 + 首段（``tmp`` 视作 host 段，与
    // RFC 8089 略有偏差但对 agent 产出场景够用——agent 应将文件写入 run 私有 assets
    // 目录后引用相对路径）。
    expect(rewriteImageSrc("file:///tmp/foo.png", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/foo.png`
    );
  });

  it("绝对 http(s) URL → 直通不改写", () => {
    expect(rewriteImageSrc("https://cdn.example.com/x.png", RUN_ID)).toBe(
      "https://cdn.example.com/x.png"
    );
    expect(rewriteImageSrc("http://foo.com/y.gif", RUN_ID)).toBe(
      "http://foo.com/y.gif"
    );
  });

  it("data: / blob: URL → 直通", () => {
    expect(rewriteImageSrc("data:image/png;base64,iVBOR...", RUN_ID)).toBe(
      "data:image/png;base64,iVBOR..."
    );
    expect(rewriteImageSrc("blob:https://foo/uuid", RUN_ID)).toBe(
      "blob:https://foo/uuid"
    );
  });

  it("已是 /api/ 路径 → 不二次改写", () => {
    const path = `/api/runs/${RUN_ID}/assets/x.png`;
    expect(rewriteImageSrc(path, RUN_ID)).toBe(path);
  });

  it("无 runId（store 未 loadRun）→ 直通 fail loud（图片大概率 404 但不崩）", () => {
    expect(rewriteImageSrc("rel.png", null)).toBe("rel.png");
    expect(rewriteImageSrc("file://x.png", null)).toBe("file://x.png");
  });

  it("空 src → 直通", () => {
    expect(rewriteImageSrc("", RUN_ID)).toBe("");
  });

  it("URL 特殊字符（中文 / 空格）正确编码", () => {
    expect(rewriteImageSrc("图 1.png", RUN_ID)).toBe(
      `/api/runs/${RUN_ID}/assets/${encodeURIComponent("图 1.png")}`
    );
  });
});
