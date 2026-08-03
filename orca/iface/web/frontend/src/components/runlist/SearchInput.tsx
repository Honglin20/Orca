// components/runlist/SearchInput.tsx —— 搜索框（SPEC §4/§5.2/§6.2）。
//
// 契约：
//   - 范围含 workflow_name / run_id / project_name（D3 m1）。
//   - debounce ~250ms 上抛 onChange（父组件控制 filtering；本组件内部 state 即时反映输入）。
//   - 清空按钮（X）+ aria-label + search icon。
//   - 高亮匹配子串的渲染在 RunRow 内做（本组件只负责输入）。

import { useEffect, useState } from "react";
import { Search, X } from "lucide-react";

interface Props {
  value: string;
  onChange: (v: string) => void;
  debounceMs?: number;
}

export function SearchInput({ value, onChange, debounceMs = 250 }: Props) {
  // 本地 state 即时反映输入；debounce 后才上抛，避免每键触发过滤重算。
  const [local, setLocal] = useState(value);

  // 父控 value 变（如外部 clear）→ 同步本地。
  useEffect(() => {
    setLocal(value);
  }, [value]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (local !== value) onChange(local);
    }, debounceMs);
    return () => clearTimeout(t);
  }, [local, debounceMs, onChange, value]);

  return (
    <span
      data-testid="search-input-wrap"
      className="orca-border inline-flex flex-1 max-w-md items-center gap-1.5 rounded border px-2 py-1"
    >
      <Search size={14} strokeWidth={1.5} aria-hidden className="orca-text-faint shrink-0" />
      <input
        type="text"
        data-testid="search-input"
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        placeholder="搜索 workflow / run_id / 项目…"
        aria-label="搜索"
        className="orca-text w-full bg-transparent text-sm outline-none placeholder:orca-text-faint"
      />
      {local && (
        <button
          type="button"
          data-testid="search-clear"
          onClick={() => {
            setLocal("");
            onChange("");
          }}
          aria-label="清空搜索"
          className="orca-text-faint hover:orca-text shrink-0"
        >
          <X size={12} strokeWidth={1.5} aria-hidden />
        </button>
      )}
    </span>
  );
}
