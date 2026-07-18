// hooks/use-theme.ts —— 三态主题开关（P3，light / dark / system）。
//
// **SPEC §7 双触发机制**（web-shell-v2-spec.md）：暗色由 ``<html>.dark`` class 控制，
// index.css 的 ``:root.dark`` 作用域定义在与 ``@media (prefers-color-scheme: dark)``
// **之后**（同 specificity 后者胜），从而用户显式选择覆盖系统偏好。
//
// 持久化 localStorage("orca-theme")；无存储 / 无 window（SSR 防御）→ fallback system。
// system 态：移除 .dark class，跟随 ``@media prefers-color-scheme``（保留默认行为）。

export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "orca-theme";

function readStored(): Theme {
  if (typeof window === "undefined") return "system";
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch (err) {
    // fail loud：localStorage 不可用（隐私模式 / 禁用）→ fallback system，不静默吞。
    console.error("[orca] theme localStorage 读取失败，回退 system", err);
  }
  return "system";
}

function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  // 三态：dark → .dark class；light → .light class；system → 无 class（走 @media）。
  // index.css 的 :root.dark / :root.light specificity (0,2,0) > @media :root (0,1,0)，
  // 故用户显式选择覆盖系统偏好（无需依赖规则顺序）。
  root.classList.remove("dark", "light");
  if (theme === "dark") root.classList.add("dark");
  else if (theme === "light") root.classList.add("light");
}

/** 初始化：模块加载时读 localStorage 并 apply（RunDetailPage / App 根调用一次）。 */
export function initTheme(): void {
  applyTheme(readStored());
}

/** 切主题：持久化 + apply + 返回新值。localStorage 写失败 → console.error，仍 apply（不阻断 UX）。 */
export function setTheme(theme: Theme): void {
  applyTheme(theme);
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, theme);
  } catch (err) {
    console.error("[orca] theme localStorage 写入失败（本次切换不持久化）", err);
  }
}

export function currentTheme(): Theme {
  return readStored();
}

/** 下一个主题（toggle 循环 system → dark → light → system）。 */
export function nextTheme(t: Theme): Theme {
  if (t === "system") return "dark";
  if (t === "dark") return "light";
  return "system";
}
