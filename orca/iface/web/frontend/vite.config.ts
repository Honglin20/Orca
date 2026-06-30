import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// Vite config: build outputs to ../static (served by FastAPI at `/`).
// Vitest uses happy-dom for hook/component DOM (faster than jsdom; both supported).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../static",
    // outDir 在 vite root（frontend/）之外。**必须 false**：true 会清空 ../static，
    // 物理删除其中被 git 跟踪的 .gitignore/.gitkeep（构建产物靠 static/.gitignore 的 `*`
    // 规则忽略）。false 下 vite 不清空，仅写入新产物（旧 hash 资产由 .gitignore 忽略，
    // 不影响服务——index.html 永远指向当前 hash；需彻底清理时 rm -rf static/assets）。
    emptyOutDir: false,
  },
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.{test,spec}.{ts,tsx}"],
    css: false,
  },
});
