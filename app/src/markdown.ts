import DOMPurify from "dompurify";
import { marked } from "marked";
import { t } from "./i18n";

export interface Frontmatter {
  [key: string]: unknown;
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

export function splitFrontmatter(text: string): { meta: string; body: string } {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  return m ? { meta: m[1], body: text.slice(m[0].length) } : { meta: "", body: text };
}

// Obsidian callouts: "> [!quote]- Title" + "> ..." lines -> <details>, collapsed when "-".
function callouts(md: string): string {
  const lines = md.split(/\r?\n/);
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const head = /^> \[!(\w+)\]([-+]?)\s*(.*)$/.exec(lines[i]);
    if (!head) {
      out.push(lines[i]);
      continue;
    }
    const inner: string[] = [];
    while (i + 1 < lines.length && lines[i + 1].startsWith(">")) inner.push(lines[++i].replace(/^> ?/, ""));
    const open = head[2] === "-" ? "" : " open";
    out.push(`<details class="callout callout-${esc(head[1])}"${open}><summary>${esc(head[3] || head[1])}</summary>\n\n${inner.join("\n")}\n\n</details>`);
  }
  return out.join("\n");
}

export function renderMarkdown(body: string): string {
  let n = 0;
  const pre = callouts(body)
    .replace(/\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|([^\]\n]*))?\]\]/g, (_m, target: string, label?: string) =>
      `<a class="wikilink" data-target="${esc(target.trim())}">${esc((label ?? target).trim())}</a>`)
    .replace(/\^\[([^\]\n#]+)#L(\d+)(?:-L(\d+))?\]/g, (_m, key: string, a: string, b?: string) =>
      `<sup class="cite" title="${esc(t("{key} lines {lines}", { key, lines: `${a}${b && b !== a ? `-${b}` : ""}` }))}">${++n}</sup>`)
    .replace(/!\[\[([^\]\n]+)\]\]/g, (_m, file: string) => `<span class="embed">${esc(t("[image: {file}]", { file }))}</span>`);
  const html = marked.parse(pre, { async: false, gfm: true }) as string;
  // vault text can come from web clips: no forms or styling that could fake the app's own UI
  // (task-list checkboxes stay; the CSP also blocks form submission and remote images)
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ["data-target", "open"], ADD_TAGS: ["details", "summary"],
    FORBID_TAGS: ["style", "form", "button", "textarea", "select", "option", "iframe", "object", "embed", "base"],
    FORBID_ATTR: ["style", "action", "formaction"],
  });
}
