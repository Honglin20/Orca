// clean-assets.mjs —— build 前清空 ../static/assets（SPEC 2026-08-28 C1.5）。
//
// vite `emptyOutDir: false`（C1.6：保 static/.gitignore 与 .gitkeep 存活），旧 hash
// 产物会无限累积（调查时已 5.5MB）。本脚本只删 `assets/` 子目录，vite build 随后重建。
// 执行位置：`tsc --noEmit` 之后、`vite build` 之前（tsc 拦截大部分失败前置，降低
// 「clean 后 build 失败 → assets 空目录」窗口，SPEC §4 失败路径）。
import { rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const staticAssets = resolve(dirname(fileURLToPath(import.meta.url)), "../../static/assets");
rmSync(staticAssets, { recursive: true, force: true });
console.log(`[clean-assets] removed ${staticAssets}`);
