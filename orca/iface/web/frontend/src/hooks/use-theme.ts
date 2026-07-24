// hooks/use-theme.ts —— 三态主题开关（P3，light / dark / system）+ matchMedia 跟随。
//
// **SPEC §7 双触发机制**（web-shell-v2-spec.md + amendment）：暗色由 ``<html>.dark`` class
// 控制；tailwind.config ``darkMode: "class"`` 让 ``dark:`` 变体也看该 class（与 CSS 变量
// 同源）。``<html>.light`` 强制亮（覆盖系统）。``index.css`` 的 ``:root.dark/.light``
// specificity (0,2,0) > ``@media :root`` (0,1,0)，显式 class 总胜。
//
// **system 态**：读 ``matchMedia("(prefers-color-scheme: dark)")`` 决定加 ``.dark`` 还是
// ``.light``，并注册 change listener 跟随系统切换（让 dark: 变体与 CSS @media 同步）。
//
// 持久化 localStorage("orca-theme")；无 window/document（SSR 防御）→ fallback system。
//
// **消费者契约**（code-reviewer Y5）：当前 theme React state 仅 TopBar 单消费者持有
// （useState(currentTheme())）。若未来新增消费者，应改为 useSyncExternalStore 订阅
// module-level getter，避免多组件 state 漂移。

export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "orca-theme";
const MEDIA_DARK = "(prefers-color-scheme: dark)";

function systemPrefersDark(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(MEDIA_DARK).matches;
}

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
  // darkMode: "class"：dark 态加 .dark；light 态加 .light；system 态读 matchMedia
  // （让 dark: 变体与 CSS @media 一致）。.dark / .light 互斥（toggle 二选一）。
  const dark = theme === "dark" || (theme === "system" && systemPrefersDark());
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  root.classList.toggle("light", !dark);
}

let initialized = false;

/** 初始化：apply 持久化主题 + 注册 system 态 matchMedia listener（幂等，App 根调用）。 */
export function initTheme(): void {
  if (initialized) return;
  initialized = true;
  applyTheme(readStored());
  if (typeof window === "undefined" || !window.matchMedia) return;
  const mq = window.matchMedia(MEDIA_DARK);
  mq.addEventListener("change", () => {
    // 仅 system 态跟随系统偏好（显式 dark/light 不被系统覆盖）。
    if (readStored() === "system") applyTheme("system");
  });
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
