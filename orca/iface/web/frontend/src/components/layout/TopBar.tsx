// components/layout/TopBar.tsx —— 顶栏：Logo + 导航链接（骨架，9c/9d 完善）。
//
// 纯渲染（铁律 6）：不含编排逻辑，只 navigate。

import { Link } from "react-router-dom";

export function TopBar() {
  return (
    <header className="flex h-12 items-center gap-6 border-b border-slate-200 bg-white px-4">
      <Link to="/" className="text-lg font-semibold text-slate-900">
        Orca
      </Link>
      <nav className="flex gap-4 text-sm text-slate-600">
        <Link to="/" className="hover:text-slate-900">
          Runs
        </Link>
        <Link to="/runs/new" className="hover:text-slate-900">
          New Run
        </Link>
      </nav>
    </header>
  );
}
