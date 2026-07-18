/** @type {import('tailwindcss').Config} */
// Tailwind v3 (CSS-first v4 was deemed less stable with React 19 + vite 6 in mid-2026;
// SPEC §1 + plan explicitly permit pinning v3 with documented rationale — see release note).
//
// P5b：``orca`` palette 对齐 design token（与 index.css --accent 等同步）。
// 状态语义保留（pending 中性 / running 钢蓝品牌 / done 绿 / failed 红 / skipped 紫），
// 但 ``running`` 从 blue-500 #3b82f6 收敛到 ``--accent`` = PALETTE[0] #5B8DB8（品牌强调色
// 与图表第一色一致），完成状态保留 emerald 语义、失败保留 red 语义——避免视觉歧义。
// 注：该 palette 已启用（P0 token 收口后，组件经 text-orca-* / bg-orca-* / border-orca-*
// 暴露入口消费），是 status → 视觉色 DRY 真相源（与 ``NODE_STATUS_HEX`` 互补：前者 utility、
// 后者 hex inline style）。
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // status palette aligned to RunState/Status semantics + design tokens
        orca: {
          pending: "#94a3b8", // slate-400 = --text-faint（占位/未启动）
          running: "#5b8db8", // 钢蓝 = --accent / PALETTE[0]（品牌强调色）
          done: "#10b981", // emerald-500（语义：成功）
          completed: "#10b981",
          failed: "#ef4444", // red-500（语义：失败）
          skipped: "#a78bfa", // violet-400（语义：跳过）
          accent: "#5b8db8", // 显式 brand 入口（= PALETTE[0] = --accent）
        },
      },
      animation: {
        // 用于 tool pending icon（⟳）的慢速旋转——avoid Tailwind `animate-spin`（太快）
        "spin-slow": "spin 2s linear infinite",
      },
    },
  },
  plugins: [],
};
