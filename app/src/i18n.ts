// Interface language. English is the source text; Italian comes from IT (i18n-it.ts).
// t("Page saved in {path}", { path }) -> the translation with {placeholders} filled in.
// A sentence missing from IT falls back to English: the app never shows a raw key.
// The choice is per device (localStorage); the first time the browser language decides.
import { IT } from "./i18n-it";

export type Lang = "it" | "en";
const KEY = "mnemoloci-lang";

function stored(): string | null {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

export let lang: Lang = (() => {
  const saved = stored();
  if (saved === "it" || saved === "en") return saved;
  return (navigator.language || "en").toLowerCase().startsWith("it") ? "it" : "en";
})();
document.documentElement.lang = lang;

export function setLang(next: Lang): void {
  lang = next;
  document.documentElement.lang = next;
  try { localStorage.setItem(KEY, next); } catch { /* private mode: this session only */ }
}

const missing = new Set<string>();

export function t(text: string, vars: Record<string, unknown> = {}): string {
  let out = text;
  if (lang === "it") {
    const found = IT[text];
    if (found === undefined) {
      if (!missing.has(text)) { missing.add(text); console.debug("i18n: missing Italian for", JSON.stringify(text)); }
    } else {
      out = found;
    }
  }
  return out.replace(/\{(\w+)\}/g, (m, name: string) => (name in vars ? String(vars[name]) : m));
}

/** Plural helper: tn(n, "1 page", "{n} pages"). */
export function tn(n: number, one: string, many: string, vars: Record<string, unknown> = {}): string {
  return t(n === 1 ? one : many, { n, ...vars });
}

/** Dates and times in the interface language. */
export function locale(): string {
  return lang === "en" ? "en-GB" : "it-IT";
}
