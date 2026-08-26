// test/grep-guards.test.ts —— SPEC web-board-cardgrid §8/§9 grep 守门测试。
//
// 断言意图（Rule 9）：这些测试把 SPEC §8 的精确 grep 命令编码为可执行断言，
// 防 regression——后续改动不慎重新引入被禁模式时，CI 直接红。
//
// 守门项：
//   - AC-B1：RunBoard.tsx 无 overflow-x-auto（含注释）。
//   - AC-B5：BoardCard.tsx + RunRow.tsx 无 StatusBadge（含注释）。
//   - AC-B6：RunBoard/CardGridSection/BoardCard/KpiStrip 无 var(--surface)/0. 半透明底。
//   - AC-B8：runlist scope + sort/use-list-sort 无 fmtCost / .cost。

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "../src");

function readSrc(rel: string): string {
  return readFileSync(resolve(SRC, rel), "utf-8");
}

describe("AC-B1：RunBoard.tsx 无 overflow-x-auto（含注释）", () => {
  it("grep overflow-x-auto RunBoard.tsx → 空", () => {
    const content = readSrc("components/runlist/RunBoard.tsx");
    expect(content).not.toMatch(/overflow-x-auto/);
  });
});

describe("AC-B5：BoardCard.tsx + RunRow.tsx 无 StatusBadge（含注释）", () => {
  it("grep StatusBadge BoardCard.tsx RunRow.tsx → 空", () => {
    const boardCard = readSrc("components/runlist/BoardCard.tsx");
    const runRow = readSrc("components/runlist/RunRow.tsx");
    expect(boardCard).not.toMatch(/StatusBadge/);
    expect(runRow).not.toMatch(/StatusBadge/);
  });
});

describe("AC-B6：in-scope 文件无 var(--surface)/0. 半透明底", () => {
  const pattern = /var\(--(surface|surface-2)\)\/0\./;
  it("RunBoard.tsx → 空", () => {
    expect(readSrc("components/runlist/RunBoard.tsx")).not.toMatch(pattern);
  });
  it("CardGridSection.tsx → 空", () => {
    expect(readSrc("components/runlist/CardGridSection.tsx")).not.toMatch(pattern);
  });
  it("BoardCard.tsx → 空", () => {
    expect(readSrc("components/runlist/BoardCard.tsx")).not.toMatch(pattern);
  });
  it("KpiStrip.tsx → 空", () => {
    expect(readSrc("components/runlist/KpiStrip.tsx")).not.toMatch(pattern);
  });
});

describe("AC-B8：runlist scope 无 fmtCost / .cost 显示性命中", () => {
  const files = [
    "components/runlist/BoardCard.tsx",
    "components/runlist/RunRow.tsx",
    "components/runlist/ProjectGroup.tsx",
    "components/runlist/CardGridSection.tsx",
    "components/runlist/KpiStrip.tsx",
    "components/runlist/RunBoard.tsx",
    "components/runlist/format-helpers.tsx",
    "components/runlist/sort-runs.ts",
    "components/runlist/group-runs.ts",
    "hooks/use-list-sort.ts",
  ];
  for (const f of files) {
    it(`${f} 无 fmtCost / .cost`, () => {
      const content = readSrc(f);
      // fmtCost：函数已删，任何命中都是 regression。
      expect(content).not.toMatch(/fmtCost/);
      // .cost：属性访问（类型定义中的 cost 字段不计——仅检消费性 .cost）。
      // format-helpers.tsx 注释提「cost」但不含 .cost 属性访问，不命中。
      expect(content).not.toMatch(/\.cost\b/);
    });
  }
});

describe("AC-B14 R3：run-list-store 不 import workflow-store", () => {
  it("grep workflow-store run-list-store.ts → 空", () => {
    const content = readSrc("stores/run-list-store.ts");
    expect(content).not.toMatch(/workflow-store/);
  });
});
