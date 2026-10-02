/**
 * Light / dark / system appearance. The choice lives in localStorage (per browser) and is applied as
 * `data-theme` on <html>; "system" removes the attribute so globals.css follows prefers-color-scheme.
 * THEME_INIT_SCRIPT runs in <head> before first paint, so a stored choice never flashes the other theme.
 */
export type ThemePreference = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "iv-theme";
export const THEME_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

function isPreference(v: unknown): v is ThemePreference {
  return v === "system" || v === "light" || v === "dark";
}

export function readThemePreference(): ThemePreference {
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isPreference(v) ? v : "system";
  } catch {
    return "system"; // storage blocked (private mode, sandboxed preview)
  }
}

export function applyThemePreference(pref: ThemePreference): void {
  const root = document.documentElement;
  if (pref === "system") delete root.dataset.theme;
  else root.dataset.theme = pref;
  try {
    if (pref === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    /* the choice still applies for this page view */
  }
}

export const THEME_INIT_SCRIPT = `try{var t=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;
