/** @type {import('tailwindcss').Config} */
// Tailwind v3 (CSS-first v4 was deemed less stable with React 19 + vite 6 in mid-2026;
// SPEC §1 + plan explicitly permit pinning v3 with documented rationale — see release note).
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // status palette aligned to RunState/Status semantics
        orca: {
          pending: "#94a3b8", // slate-400
          running: "#3b82f6", // blue-500
          done: "#10b981", // emerald-500
          completed: "#10b981",
          failed: "#ef4444", // red-500
          skipped: "#a78bfa", // violet-400
        },
      },
    },
  },
  plugins: [],
};
