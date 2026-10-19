import ForceGraph from "force-graph";
import { ApiError, claimDevice, forgetDevice, get, hasToken, imageUrl, initialView, post, usingDeviceToken } from "./api";
import { lang, locale, setLang, t, tn, type Lang } from "./i18n";
import { renderMarkdown, splitFrontmatter } from "./markdown";
import { featuresView } from "./features-view";
import { settingsView } from "./settings-view";
import { toolsView } from "./tools-view";
import "./style.css";

type Json = Record<string, any>;
type View = (root: HTMLElement) => Promise<void>;

const main = document.getElementById("main")!;
// Label maps hold the Italian text; tr() translates at the point of use, so a language switch takes effect on re-render.
const REASONS: Json = {
  "low-confidence": "low confidence", contradicted: "contradicted", "schema-violating": "incomplete format",
  "provenance-violating": "invalid citations", "external-source": "external source", manual: "manual review",
};
const ISSUES: Json = {
  unreadable: "unreadable pages", missing_fields: "missing fields", broken_links: "broken links",
  orphans: "pages with no incoming links", stale: "pages whose sources changed", orphaned_sources: "missing sources",
  low_confidence: "low confidence", contradicted: "contradicted", alias_collisions: "conflicting aliases",
  misplaced: "in the wrong folder", uncited: "without citations", other_language: "in another language",
  embeddings_stale: "pages to re-index for semantic search",
};
const ORIGIN: Json = { web: "web page", pdf: "PDF", youtube: "YouTube", file: "file", text: "text", agent: "text from link" };
const GOAL_STATUS: Json ={ active: "active", paused: "paused", done: "done", dropped: "dropped" };
const GOAL_SECTIONS: Json = { active: "Active", paused: "Paused", done: "Done", dropped: "Dropped" };
const MEMORY_KINDS: Json = { decision: "Decisions", lesson: "Lessons", fact: "Facts", preference: "Preferences", runbook: "Runbooks", handoff: "Closed sessions" };
const QUALITY_PARTS: Json = { structure: "structure", citations: "citations", freshness: "freshness", connectivity: "links", trust: "reliability" };
const ENTITY_TYPES: Json = { person: "People", organization: "Organizations", project: "Projects", tool: "Tools",
  place: "Places", event: "Events", all: "All entities" };
const FRIENDLY: Json = {
  PRIVATE_ADDRESS: "That is a local network address: for safety the app won't download it.",
  BAD_URL: "The link must start with http:// or https://.",
  FETCH_FAILED: "Couldn't download the page (login, anti-bot block or site unreachable). Ask Claude to save it: Claude can read it.",
  NO_TRANSCRIPT: "This video has no transcript available.",
  UNSUPPORTED_TYPE: "This file type can't be saved from a link: download it and ask Claude to save it.",
  TOO_LARGE: "The file is too large (over 25 MB).",
  MISSING_EXTRA: "PDF/YouTube support is missing on the PC: python -m pip install \"mnemoloci[ingest]\".",
  INJECTION_SUSPECT: "The text seems to contain instructions for an AI: not saved.",
};
/** Translated label from one of the maps above; unknown keys show `fallback` (the raw key by default). */
const tr = (map: Json, key: string, fallback: string = key): string => (typeof map[key] === "string" ? t(map[key]) : fallback);
let slugToPath = new Map<string, string>();
let slugToTitle = new Map<string, string>();
let graphInstance: { _destructor?: () => void } | null = null;
// Redraws whatever is on screen (a view, a page, the editor, the pairing screen) after a language switch.
let rerender: () => void = () => {};

// ---------- tiny DOM helpers (text only; markdown goes through DOMPurify) ----------
function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Json = {}, ...children: (Node | string | null)[]) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, String(v));
  }
  for (const c of children) if (c !== null) el.append(c);
  return el;
}

function notice(text: string, kind: "error" | "ok" = "error") {
  document.querySelectorAll(".notice").forEach((old) => old.remove());  // they share one spot: newest wins
  const el = h("div", { class: `notice ${kind}` }, text);
  document.body.append(el);
  setTimeout(() => el.remove(), 5000);
}

function explain(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.details ? `\n${JSON.stringify(err.details, null, 1).slice(0, 400)}` : "";
    return `${err.code}: ${err.message}${detail}`;
  }
  return String(err);
}

async function loadSlugs() {
  const g = await get<Json>("/api/graph");
  slugToPath = new Map(g.nodes.map((n: Json) => [n.id, n.path]));
  slugToTitle = new Map(g.nodes.map((n: Json) => [n.id, n.title]));
  return g;
}

// ---------- unsaved-changes guard ----------
let dirty = false;
window.addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });
function leaveOk(): boolean {
  if (!dirty) return true;
  if (!confirm(t("You have unsaved changes. Leave without saving?"))) return false;
  dirty = false;
  return true;
}

const EDITABLE = /^(wiki|memory)\/.+\.md$/;
const GENERATED = new Set(["wiki/index.md", "wiki/log.md"]);
const isEditable = (path: string) => EDITABLE.test(path) && !GENERATED.has(path);
const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

// ---------- editor ----------
async function openEditor(path: string) {
  if (!leaveOk()) return;
  setActive("");
  let page: Json;
  try {
    page = await get<Json>("/api/edit", { path });
  } catch (err) {
    notice(explain(err));
    return;
  }
  rerender = () => openEditor(path);
  let base = page.sha256 as string;
  const title = h("input", { value: page.fields.title, "aria-label": t("Title") }) as HTMLInputElement;
  const summary = h("input", { value: page.fields.summary, "aria-label": t("Summary") }) as HTMLInputElement;
  const aliases = h("input", { value: page.fields.aliases.join(", "), placeholder: t("Aliases, comma-separated") }) as HTMLInputElement;
  const tags = h("input", { value: page.fields.tags.join(", "), placeholder: t("Tags, comma-separated") }) as HTMLInputElement;
  const body = h("textarea", { class: "editor-body", spellcheck: "true", "aria-label": t("Text") }) as HTMLTextAreaElement;
  body.value = page.body;
  const preview = h("article", { class: "page preview" });
  const status = h("span", { class: "muted" }, "");
  const renderPreview = () => { preview.innerHTML = renderMarkdown(body.value); };
  let timer = 0;
  const changed = () => {
    dirty = true;
    status.textContent = t("unsaved changes");
    clearTimeout(timer);
    timer = window.setTimeout(renderPreview, 150);
  };
  [title, summary, aliases, tags, body].forEach((el) => el.addEventListener("input", changed));

  const save = async () => {
    clearTimeout(timer);
    renderPreview();  // show exactly what is being saved, even mid-debounce
    try {
      const res = await post<Json>("/api/edit/save", {
        path, base_sha256: base, title: title.value, summary: summary.value, body: body.value,
        aliases: list(aliases.value), tags: list(tags.value),
      });
      base = res.sha256;
      dirty = false;
      status.textContent = t("saved");
      notice(res.warnings.length ? t("Saved, with warnings:\n{warnings}", { warnings: res.warnings.join("\n") }) : t("Page saved."), "ok");
    } catch (err) {
      if (err instanceof ApiError && err.code === "PAGE_CHANGED") {
        notice(t("The page was changed elsewhere (Obsidian or a compile) after you opened it. Your text is still here: copy it, then reload the page."));
        status.textContent = t("conflict: not saved");
      } else {
        notice(explain(err));
      }
    }
  };
  body.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {  // indent instead of leaving the field
      e.preventDefault();
      body.setRangeText("  ", body.selectionStart, body.selectionEnd, "end");
      changed();
    }
  });
  const onKey = (e: KeyboardEvent) => {
    if (!document.contains(body)) {  // editor left via the sidebar: drop the shortcut
      document.removeEventListener("keydown", onKey);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  };
  document.addEventListener("keydown", onKey);
  const close = () => {
    if (!leaveOk()) return;
    document.removeEventListener("keydown", onKey);
    openPage(path);
  };

  main.replaceChildren(
    h("div", { class: "pagebar" },
      h("button", { class: "primary", onclick: save }, t("Save (Ctrl+S)")),
      h("button", { onclick: close }, t("Close")),
      h("code", {}, path), status),
    h("div", { class: "edit-fields" },
      h("label", {}, t("Title"), title), h("label", {}, t("Summary"), summary),
      h("label", {}, t("Aliases"), aliases), h("label", {}, t("Tags"), tags)),
    h("p", { class: "muted" }, t("Sources, confidence and language stay unchanged. Cite sources with ^[key#L10-L14], link pages with [[slug]].")),
    h("div", { class: "editor" }, body, preview),
  );
  renderPreview();
  body.focus();
}

// ---------- page reader ----------
async function openPage(path: string) {
  if (!leaveOk()) return;
  location.hash = "";
  setActive("");
  rerender = () => openPage(path);
  main.replaceChildren(h("p", { class: "muted" }, t("Loading…")));
  try {
    const page = await get<Json>("/api/page", { path });
    const { meta, body } = splitFrontmatter(page.content);
    const article = h("article", { class: "page" });
    article.innerHTML = renderMarkdown(body);
    article.querySelectorAll<HTMLAnchorElement>("a.wikilink").forEach((a) => {
      const target = (a.dataset.target ?? "").toLowerCase().normalize("NFKD").replace(/[^\x00-\x7f]/g, "")
        .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      const dest = slugToPath.get(target);
      if (dest) {
        a.addEventListener("click", () => openPage(dest));
        // [[slug]] without a label: show the page title instead of the technical slug
        if (a.textContent === a.dataset.target && slugToTitle.get(target)) a.textContent = slugToTitle.get(target)!;
      } else a.classList.add("missing");
    });
    main.replaceChildren(
      h("div", { class: "pagebar" }, h("button", { onclick: () => history.back() }, t("← Back")),
        isEditable(path) ? h("button", { onclick: () => openEditor(path) }, t("Edit")) : "",
        h("code", {}, path)),
      meta ? h("details", { class: "meta" }, h("summary", {}, t("Metadata")), h("pre", {}, meta)) : "",
      article,
    );
  } catch (err) {
    main.replaceChildren(h("p", { class: "error" }, explain(err)));
  }
}

// ---------- phone access (Tailscale) ----------
async function phoneSection(root: HTMLElement) {
  const me = await get<Json>("/api/whoami");
  const data = await get<Json>("/api/remote/devices");
  const box = h("section", { class: "phone" }, h("h2", {}, t("Phone access")));
  if (me.role === "device") {
    box.append(h("p", { class: "muted" }, t("This device: {name}. Pairing and revoking are managed from the PC.", { name: me.device.name })));
  } else {
    const codeBox = h("div", { class: "pair-result" });
    const pair = async () => {
      try {
        const res = await post<Json>("/api/remote/pair", {});
        codeBox.replaceChildren(h("div", { class: "big-code" }, res.code),
          h("p", { class: "muted" }, t("Enter it on the phone within 10 minutes. It works only once.")));
      } catch (err) { notice(explain(err)); }
    };
    box.append(
      h("p", { class: "muted" }, data.allowed_hosts.length
        ? t("Reachable from: {hosts}", { hosts: data.allowed_hosts.join(", ") })
        : t("Not set up yet: see `python -P -m mnemoloci remote setup`.")),
      h("button", { class: "primary", onclick: pair }, t("Generate pairing code")), codeBox);
  }
  const revoke = async (id: string, name: string) => {
    if (!confirm(t("Revoke access for \"{name}\"?", { name }))) return;
    try { await post(`/api/remote/revoke/${encodeURIComponent(id)}`, {}); render("status"); }
    catch (err) { notice(explain(err)); }
  };
  const setScope = async (id: string, scope: string) => {
    try { await post(`/api/remote/scope/${encodeURIComponent(id)}`, { scope }); notice(t("Permissions updated."), "ok"); }
    catch (err) { notice(explain(err)); render("status"); }
  };
  const scopeCell = (d: Json) => me.role === "owner"
    ? h("select", { "aria-label": t("Permissions for {name}", { name: d.name }), onchange: (e: Event) => setScope(d.id, (e.target as HTMLSelectElement).value) },
        ...Object.entries(SCOPES).map(([v, l]) => h("option", { value: v, selected: v === (d.scope ?? "capture") }, t(l))))
    : tr(SCOPES, d.scope ?? "capture");
  const active = data.devices.filter((d: Json) => !d.revoked);
  box.append(active.length
    ? h("table", {}, h("tr", {}, ...[t("Device"), t("Permissions"), t("Paired"), t("Last seen"), ""].map((x) => h("th", {}, x))),
        ...active.map((d: Json) => h("tr", {}, h("td", {}, d.name), h("td", {}, scopeCell(d)),
          h("td", {}, (d.created ?? "").slice(0, 16).replace("T", " ")),
          h("td", {}, d.last_seen ? d.last_seen.slice(0, 16).replace("T", " ") : t("never")),
          h("td", {}, me.role === "owner" ? h("button", { onclick: () => revoke(d.id, d.name) }, t("Revoke")) : ""))))
    : h("p", { class: "muted" }, t("No paired devices.")));
  if (me.role === "owner" && active.length) {
    box.append(h("p", { class: "muted" }, t("\"Capture only\": reads everything and saves links, texts and to-dos. \"Full\": can also edit pages, reviews, goals and the profile.")));
  }
  root.append(box);
}

const SCOPES: Record<string, string> = { capture: "Capture only", full: "Full" };

// ---------- connectors (periodic OAuth sync from external services, owner-only) ----------
async function connectorsSection(root: HTMLElement) {
  const box = h("section", { class: "connectors" }, h("h2", {}, t("Connectors")),
    h("p", { class: "muted" }, t("Periodically syncs an external folder (no call to a language model: it only downloads files, which then land like any other external source in the review queue).")));
  root.append(box);
  let data: Json;
  try { data = await get<Json>("/api/connectors"); }
  catch (err) { box.append(h("p", { class: "error" }, explain(err))); return; }
  const list = (data.connectors as Json[]) ?? [];

  for (const c of list) {
    if (!c.implemented) {
      box.append(h("div", { class: "row connector-row muted" }, h("strong", {}, c.display_name as string),
        t("not available yet")));
      continue;
    }
    box.append(connectorCard(c));
  }
}

function connectorCard(c: Json): HTMLElement {
  const provider = c.provider as string;
  const status = h("span", { class: "muted" }, connectorStatus(c));
  const authRow = c.auth_kind === "token" ? tokenAuthFields(provider, status) : oauthAuthFields(c, provider, status);

  const folderId = h("input", { placeholder: t("Folder ID"), value: (c.folder_id as string) ?? "" }) as HTMLInputElement;
  const folderName = h("input", { placeholder: t("Name (optional)"), value: (c.folder_name as string) ?? "" }) as HTMLInputElement;
  const saveFolder = h("button", {}, t("Save folder")) as HTMLButtonElement;
  saveFolder.onclick = async () => {
    if (!folderId.value.trim()) { notice(t("The folder ID is required.")); return; }
    saveFolder.disabled = true;
    try {
      await post<Json>(`/api/connectors/${provider}/folder`, { folder_id: folderId.value.trim(), folder_name: folderName.value.trim() || null });
      notice(t("Folder saved."), "ok");
      render("status");
    } catch (err) { notice(explain(err)); }
    saveFolder.disabled = false;
  };

  const sync = h("button", {}, t("Sync now")) as HTMLButtonElement;
  sync.onclick = async () => {
    sync.disabled = true;
    status.textContent = t("Syncing…");
    try {
      const res = await post<Json>(`/api/connectors/${provider}/sync`, {});
      const imported = ((res.imported as string[]) ?? []).length;
      const skipped = ((res.skipped as string[]) ?? []).length;
      notice(t("{imported} files imported, {skipped} skipped.", { imported, skipped }), "ok");
      render("status");
    } catch (err) { notice(explain(err)); }
    status.textContent = "";
    sync.disabled = false;
  };

  const disable = h("button", {}, t("Disconnect")) as HTMLButtonElement;
  disable.onclick = async () => {
    if (!confirm(t("Disconnect {name}? Files already imported stay.", { name: c.display_name as string }))) return;
    try { await post<Json>(`/api/connectors/${provider}/disable`, {}); render("status"); }
    catch (err) { notice(explain(err)); }
  };

  return h("div", { class: "connector-card" },
    h("h3", {}, c.display_name as string, " ", status),
    authRow,
    c.connected ? h("div", { class: "row" }, folderId, folderName, saveFolder, sync, disable) : "");
}

function oauthAuthFields(c: Json, provider: string, status: HTMLElement): HTMLElement {
  const clientId = h("input", { placeholder: t("Client ID") }) as HTMLInputElement;
  const clientSecret = h("input", { type: "password", placeholder: t("Client secret") }) as HTMLInputElement;
  const saveCreds = h("button", {}, t("Save credentials")) as HTMLButtonElement;
  saveCreds.onclick = async () => {
    if (!clientId.value.trim() || !clientSecret.value.trim()) { notice(t("Both the client ID and client secret are required.")); return; }
    saveCreds.disabled = true;
    try {
      await post<Json>(`/api/connectors/${provider}/credentials`, { client_id: clientId.value.trim(), client_secret: clientSecret.value.trim() });
      notice(t("Credentials saved."), "ok");
      clientId.value = ""; clientSecret.value = "";
      render("status");
    } catch (err) { notice(explain(err)); }
    saveCreds.disabled = false;
  };

  const connect = h("button", { class: "primary" }, t("Connect")) as HTMLButtonElement;
  connect.onclick = async () => {
    if (!c.has_credentials) { notice(t("Save the OAuth credentials first.")); return; }
    connect.disabled = true;
    status.textContent = t("Waiting for consent in the browser…");
    try {
      await post<Json>(`/api/connectors/${provider}/connect`, {});
      notice(t("Connected."), "ok");
      render("status");
    } catch (err) { notice(explain(err)); }
    connect.disabled = false;
  };

  return h("div", { class: "row" }, clientId, clientSecret, saveCreds, connect);
}

function tokenAuthFields(provider: string, status: HTMLElement): HTMLElement {
  const token = h("input", { type: "password", placeholder: t("Token") }) as HTMLInputElement;
  const save = h("button", { class: "primary" }, t("Save token")) as HTMLButtonElement;
  save.onclick = async () => {
    if (!token.value.trim()) { notice(t("A token is required.")); return; }
    save.disabled = true;
    status.textContent = t("Connecting…");
    try {
      await post<Json>(`/api/connectors/${provider}/token`, { token: token.value.trim() });
      notice(t("Connected."), "ok");
      token.value = "";
      render("status");
    } catch (err) { notice(explain(err)); }
    save.disabled = false;
  };
  return h("div", { class: "row" }, token, save);
}

function connectorStatus(c: Json): string {
  if (!c.connected) return t("not connected");
  const folder = c.folder_name || c.folder_id;
  if (!folder) return t("connected, no folder set");
  return c.last_sync ? t("{folder} — last synced {when}", { folder: folder as string, when: (c.last_sync as string).slice(0, 16).replace("T", " ") })
    : t("{folder} — never synced", { folder: folder as string });
}

// ---------- system health (doctor, with one-click repairs) ----------
const CHECK_NAMES: Record<string, string> = {
  "python": "Python", "engine": "Engine", "launch": "Engine launch", "plugin-python": "Plugin Python",
  "vault": "Vault", "lock": "Vault lock", "transactions": "Interrupted writes", "compile": "Compile",
  "claude-config": "Claude configuration", "plugin": "Plugin", "hooks": "Hooks", "git": "Git", "backup": "Backup",
  "git-lock": "Git lock", "handoffs": "Queued handoffs", "search-index": "Search index", "embeddings": "Semantic search",
  "schedule": "Daily job",
};
const REPAIR_LABELS: Record<string, string> = {
  "python": "Use this Python", "transactions": "Recover", "git-lock": "Move the lock", "handoffs": "Save now",
  "search-index": "Rebuild", "embeddings": "Compute",
};

async function healthSection(root: HTMLElement) {
  const box = h("section", { class: "health" }, h("h2", {}, t("System health")), h("p", { class: "muted" }, t("Checking…")));
  root.append(box);
  const show = async (): Promise<void> => {
    let data: Json;
    try { data = await get<Json>("/api/doctor", { lang }); } catch (err) { box.replaceChildren(h("h2", {}, t("System health")), h("p", { class: "error" }, explain(err))); return; }
    const bad = (c: Json) => c.status === "fail" || c.status === "warn";
    const problems = (data.checks as Json[]).filter(bad);
    const fine = (data.checks as Json[]).filter((c) => !bad(c));
    const repair = async (name: string | null, button: HTMLButtonElement) => {
      button.disabled = true;
      button.textContent = t("Working…");
      try {
        const res = await post<Json>("/api/doctor/fix", name ? { repair: name, lang } : { lang });
        const failed = (res.repairs as Json[]).filter((r) => !r.ok);
        notice(failed.length ? failed.map((r) => `${tr(REPAIR_LABELS, r.repair)}: ${r.error}`).join("\n")
          : res.nothing_to_repair ? t("Nothing to repair.") : t("Repaired."), failed.length ? "error" : "ok");
      } catch (err) { notice(explain(err)); }
      await show();
    };
    const row = (c: Json) => {
      const actions: HTMLElement[] = [];
      if (c.repair) {
        const b = h("button", { class: "primary" }, tr(REPAIR_LABELS, c.repair, t("Fix"))) as HTMLButtonElement;
        b.onclick = () => repair(c.repair, b);
        actions.push(b);
      } else if (c.fix) {
        actions.push(h("button", { onclick: () => { navigator.clipboard?.writeText(c.fix); notice(t("Command copied:\n{command}", { command: c.fix }), "ok"); } },
          t("Copy the command")));
      }
      return h("tr", { class: `health-${c.status}` },
        h("td", {}, c.status === "fail" ? "✖" : c.status === "warn" ? "!" : "✓"),
        h("td", {}, tr(CHECK_NAMES, c.check, c.check.startsWith("extra:") ? t("Optional component") : c.check)),
        h("td", {}, c.detail, c.fix && !c.repair ? h("div", { class: "muted" }, h("code", {}, c.fix)) : ""),
        h("td", {}, ...actions));
    };
    const all = h("button", {}, t("Fix all")) as HTMLButtonElement;
    all.onclick = () => repair(null, all);
    box.replaceChildren(h("h2", {}, t("System health")),
      problems.length
        ? h("div", {}, h("table", {}, ...problems.map(row)), (data.repairable as string[]).filter((r) => r !== "embeddings").length > 1 ? all : "")
        : h("p", { class: "muted" }, t("All good.")),
      h("details", {}, h("summary", {}, t("{n} checks passed", { n: fine.length })), h("table", {}, ...fine.map(row))),
      h("p", { class: "muted" }, t("Repairs never delete anything. From the terminal: {command}", { command: "python -P -m mnemoloci doctor --fix" })));
  };
  show();  // not awaited: the checks take a few seconds and must not hold up the rest of the page
}

// ---------- semantic search provider (fastembed, a local Ollama server, or a cloud API) ----------
const CLOUD_PROVIDER_NAMES: Record<string, string> = { openai: "OpenAI", voyage: "Voyage AI" };

async function embeddingsSettings(root: HTMLElement) {
  const box = h("section", { class: "embeddings-settings" }, h("h2", {}, t("Semantic search")),
    h("p", { class: "muted" }, t("How embeddings are computed: locally with fastembed (included, no server) or Ollama (local server, free), or with a paid cloud API (OpenAI or Voyage AI — Claude has no embeddings API of its own).")));
  root.append(box);
  let data: Json;
  try { data = await get<Json>("/api/config/embeddings"); }
  catch (err) { box.append(h("p", { class: "error" }, explain(err))); return; }
  const cfg = data.embeddings as Json;
  const keysSet = data.keys_set as Json;

  const providerSelect = h("select", {},
    h("option", { value: "fastembed" }, t("fastembed (local)")),
    h("option", { value: "ollama" }, t("Ollama (local server)")),
    h("option", { value: "openai" }, t("OpenAI (cloud, paid)")),
    h("option", { value: "voyage" }, t("Voyage AI (cloud, paid)"))) as HTMLSelectElement;
  providerSelect.value = cfg.provider as string;

  const ollamaModel = h("input", { placeholder: t("Ollama model, e.g. nomic-embed-text"),
    value: (cfg.ollama_model as string) ?? "" }) as HTMLInputElement;
  const ollamaUrl = h("input", { placeholder: "http://localhost:11434",
    value: (cfg.ollama_base_url as string) ?? "" }) as HTMLInputElement;
  const ollamaFields = h("span", { class: "provider-fields" }, ollamaModel, ollamaUrl);

  const cloudModel = h("input", { placeholder: t("model (blank = default)") }) as HTMLInputElement;
  const cloudKey = h("input", { type: "password", placeholder: t("API key (leave blank to keep it unchanged)") }) as HTMLInputElement;
  const cloudStatus = h("span", { class: "muted" }, "");
  const cloudFields = h("span", { class: "provider-fields" }, cloudModel, cloudKey, cloudStatus);

  const refreshVisibility = () => {
    const provider = providerSelect.value;
    ollamaFields.style.display = provider === "ollama" ? "" : "none";
    cloudFields.style.display = provider === "openai" || provider === "voyage" ? "" : "none";
    if (provider === "openai" || provider === "voyage") {
      cloudModel.value = (cfg[`${provider}_model`] as string) ?? "";
      cloudModel.placeholder = t("model (blank = default)");
      cloudKey.value = "";
      cloudStatus.textContent = keysSet[provider]
        ? t("Key set for {name}.", { name: CLOUD_PROVIDER_NAMES[provider] })
        : t("No key set for {name}.", { name: CLOUD_PROVIDER_NAMES[provider] });
    }
  };
  refreshVisibility();
  providerSelect.onchange = refreshVisibility;

  const save = h("button", { class: "primary" }, t("Save")) as HTMLButtonElement;
  save.onclick = async () => {
    save.disabled = true;
    save.textContent = t("Saving…");
    try {
      const provider = providerSelect.value;
      await post<Json>("/api/config/embeddings", {
        provider, ollama_model: ollamaModel.value.trim() || null, ollama_base_url: ollamaUrl.value.trim() || null,
        openai_model: provider === "openai" ? (cloudModel.value.trim() || null) : null,
        voyage_model: provider === "voyage" ? (cloudModel.value.trim() || null) : null,
      });
      if ((provider === "openai" || provider === "voyage") && cloudKey.value.trim()) {
        await post<Json>("/api/config/embeddings/key", { provider, api_key: cloudKey.value.trim() });
      }
      notice(t("Provider saved. Run `mnemoloci index embed` to compute embeddings with the new provider."), "ok");
      cfg.provider = provider;
      keysSet[provider] = keysSet[provider] || !!cloudKey.value.trim();
      refreshVisibility();
    } catch (err) { notice(explain(err)); }
    save.disabled = false;
    save.textContent = t("Save");
  };
  box.append(h("div", { class: "row" }, providerSelect, ollamaFields, cloudFields, save));
}

// ---------- unattended extraction with a local Ollama model, no Claude Code session needed ----------
interface EngineControls {
  providerSelect: HTMLSelectElement;
  ollamaModel: HTMLInputElement; ollamaUrl: HTMLInputElement;
  anthropicModel: HTMLInputElement; claudeCodeModel: HTMLInputElement;
  row: HTMLElement;
  refreshVisibility: () => void;
}

/** One phase's engine picker (provider + that provider's model/base-url fields), reused for both
 * the extraction phase and the optional, separately-configured page-writing phase. */
function engineControls(phaseCfg: Json): EngineControls {
  const providerSelect = h("select", {},
    h("option", { value: "ollama" }, t("Ollama (local, free)")),
    h("option", { value: "anthropic" }, t("Claude's API (paid)")),
    h("option", { value: "claude_code" }, t("claude -p (with your subscription)"))) as HTMLSelectElement;
  providerSelect.value = (phaseCfg.provider as string) || "ollama";

  const ollamaModel = h("input", { placeholder: t("Ollama model, e.g. llama3.1"),
    value: (phaseCfg.ollama_model as string) ?? "" }) as HTMLInputElement;
  const ollamaUrl = h("input", { placeholder: "http://localhost:11434",
    value: (phaseCfg.ollama_base_url as string) ?? "" }) as HTMLInputElement;
  const ollamaFields = h("span", { class: "provider-fields" }, ollamaModel, ollamaUrl);

  const anthropicModel = h("input", { placeholder: t("Claude model, e.g. claude-haiku-4-5-20251001"),
    value: (phaseCfg.anthropic_model as string) ?? "" }) as HTMLInputElement;
  const anthropicFields = h("span", { class: "provider-fields" }, anthropicModel);

  const claudeCodeModel = h("input", { placeholder: t("model, e.g. sonnet or claude-sonnet-5"),
    value: (phaseCfg.claude_code_model as string) ?? "" }) as HTMLInputElement;
  const claudeCodeFields = h("span", { class: "provider-fields" }, claudeCodeModel);

  const refreshVisibility = () => {
    const provider = providerSelect.value;
    ollamaFields.style.display = provider === "ollama" ? "" : "none";
    anthropicFields.style.display = provider === "anthropic" ? "" : "none";
    claudeCodeFields.style.display = provider === "claude_code" ? "" : "none";
  };
  refreshVisibility();
  providerSelect.onchange = refreshVisibility;

  return { providerSelect, ollamaModel, ollamaUrl, anthropicModel, claudeCodeModel,
    row: h("span", { class: "engine-fields" }, providerSelect, ollamaFields, anthropicFields, claudeCodeFields),
    refreshVisibility };
}

function enginePayload(engine: EngineControls, prefix: "" | "write_") {
  return {
    [`${prefix}provider`]: engine.providerSelect.value,
    [`${prefix}ollama_model`]: engine.ollamaModel.value.trim() || null,
    [`${prefix}ollama_base_url`]: engine.ollamaUrl.value.trim() || null,
    [`${prefix}anthropic_model`]: engine.anthropicModel.value.trim() || null,
    [`${prefix}claude_code_model`]: engine.claudeCodeModel.value.trim() || null,
  };
}

async function localExtractionSettings(root: HTMLElement) {
  const box = h("section", { class: "local-extraction-settings" }, h("h2", {}, t("Unattended extraction")),
    h("p", { class: "muted" }, t("Extract from a source with no active Claude Code session: with Ollama (local, free), Claude's own API (paid, with your own key), or \"claude -p\" (uses your subscription, no key). It always goes to the review queue: no page is ever published directly, and this never starts on its own — only with \"Run now\" below or from the command line.")));
  root.append(box);
  let data: Json;
  try { data = await get<Json>("/api/local-extraction"); }
  catch (err) { box.append(h("p", { class: "error" }, explain(err))); return; }
  const cfg = data.local_extraction as Json;
  const srcList = (data.sources as Json[]) ?? [];
  let anthropicKeySet = !!data.anthropic_key_set;

  const sourceSelect = h("select", {},
    ...srcList.map((s) => h("option", { value: s.name }, `${s.name} (${s.to_compile ?? 0})`))) as HTMLSelectElement;
  const modeSelect = h("select", {},
    h("option", { value: "extract_only" }, t("Extraction only (pages left to write)")),
    h("option", { value: "extract_and_write" }, t("Extraction and page-writing"))) as HTMLSelectElement;
  modeSelect.value = (cfg.mode as string) || "extract_only";

  const extractEngine = engineControls(cfg.extract as Json);
  const writeEngine = engineControls(cfg.write as Json);
  const differentWrite = h("input", { type: "checkbox" }) as HTMLInputElement;
  differentWrite.checked = JSON.stringify(cfg.extract) !== JSON.stringify(cfg.write);
  const writeToggleLabel = h("label", {}, differentWrite, t(" different engine to write the page"));

  const anthropicKey = h("input", { type: "password", placeholder: t("API key (leave blank to keep it unchanged)") }) as HTMLInputElement;
  const anthropicStatus = h("span", { class: "muted" },
    anthropicKeySet ? t("Key set.") : t("No key set."));
  const anthropicKeyRow = h("div", { class: "row" }, t("Claude API key:"), anthropicKey, anthropicStatus);

  const status = h("span", { class: "muted" }, "");

  const refreshVisibility = () => {
    const writePhaseUsed = modeSelect.value === "extract_and_write";
    writeToggleLabel.style.display = writePhaseUsed ? "" : "none";
    writeEngine.row.style.display = writePhaseUsed && differentWrite.checked ? "" : "none";
    const usesAnthropic = extractEngine.providerSelect.value === "anthropic"
      || (writePhaseUsed && differentWrite.checked && writeEngine.providerSelect.value === "anthropic");
    anthropicKeyRow.style.display = usesAnthropic ? "" : "none";
  };
  refreshVisibility();
  modeSelect.onchange = refreshVisibility;
  differentWrite.onchange = refreshVisibility;
  const wrap = (fn: () => void) => () => { fn(); refreshVisibility(); };
  extractEngine.providerSelect.onchange = wrap(extractEngine.refreshVisibility);
  writeEngine.providerSelect.onchange = wrap(writeEngine.refreshVisibility);

  const save = h("button", {}, t("Save")) as HTMLButtonElement;
  save.onclick = async () => {
    save.disabled = true;
    try {
      const payload: Json = { mode: modeSelect.value, ...enginePayload(extractEngine, "") };
      if (differentWrite.checked) Object.assign(payload, enginePayload(writeEngine, "write_"));
      await post<Json>("/api/local-extraction/config", payload);
      if (anthropicKey.value.trim()) {
        await post<Json>("/api/local-extraction/key", { provider: "anthropic", api_key: anthropicKey.value.trim() });
        anthropicKeySet = true;
        anthropicKey.value = "";
        anthropicStatus.textContent = t("Key set.");
      }
      notice(t("Configuration saved."), "ok");
    } catch (err) { notice(explain(err)); }
    save.disabled = false;
  };

  const run = h("button", { class: "primary" }, t("Run now")) as HTMLButtonElement;
  run.onclick = async () => {
    if (!srcList.length) { notice(t("No sources registered.")); return; }
    run.disabled = true;
    status.textContent = t("Extracting… this can take a few minutes.");
    try {
      const payload: Json = { source: sourceSelect.value, mode: modeSelect.value, ...enginePayload(extractEngine, "") };
      if (differentWrite.checked) Object.assign(payload, enginePayload(writeEngine, "write_"));
      const res = await post<Json>("/api/local-extraction/run", payload);
      const warnings = (res.warnings as string[]) ?? [];
      const summary = res.state === "writing"
        ? t("Extraction complete: {n} pages left to write (resume with a Claude Code session: compile status).", { n: res.jobs })
        : t("{n} pages in the review queue.", { n: res.held ?? 0 });
      notice(warnings.length ? `${summary}\n${warnings.slice(0, 5).join("\n")}` : summary, "ok");
    } catch (err) { notice(explain(err)); }
    status.textContent = "";
    run.disabled = false;
  };

  box.append(
    h("div", { class: "row" }, sourceSelect, modeSelect, extractEngine.row, save, run, status),
    h("div", { class: "row" }, writeToggleLabel, writeEngine.row),
    anthropicKeyRow,
  );
}

// ---------- sources (projects, Obsidian vaults, clips...): register here, compile in Claude Code ----------
const SOURCE_KINDS: Record<string, string> = {
  project: "Project", notes: "Notes / Obsidian vault", import: "Saved content", inbox: "Screenshots", sessions: "Sessions",
};

function compileRequest(name: string): string {
  return t("Compile the Mnemoloci source \"{name}\"", { name });
}

async function sourcesSection(root: HTMLElement, owner: boolean) {
  const data = await get<Json>("/api/sources");
  const box = h("section", { class: "sources" }, h("h2", {}, t("Sources")),
    h("p", { class: "muted" }, t("Folders Mnemoloci learns from. You register them here; Claude turns them into wiki pages in a Claude Code session (from the phone too): copy the request and paste it there.")));
  const copy = (name: string) => {
    navigator.clipboard?.writeText(compileRequest(name));
    notice(t("Request copied, paste it into Claude Code:\n{request}", { request: compileRequest(name) }), "ok");
  };
  const check = async (name: string, button: HTMLButtonElement) => {
    button.disabled = true;
    button.textContent = t("Checking…");
    try {
      const res = await post<Json>(`/api/sources/${encodeURIComponent(name)}/check`, {});
      notice(res.to_compile ? t("{name}: {n} new or changed files to compile (of {total}).", { name, n: res.to_compile, total: res.files })
        : t("{name}: nothing new ({total} files).", { name, total: res.files }), "ok");
    } catch (err) { notice(explain(err)); }
    render("status");
  };
  const rows = (data.sources as Json[]).map((s) => {
    const actions: HTMLElement[] = [];
    if (owner && !s.retired) {
      const b = h("button", {}, t("Check")) as HTMLButtonElement;
      b.onclick = () => check(s.name, b);
      actions.push(b);
    }
    if (!s.retired && (s.to_compile || !s.checked)) {
      actions.push(h("button", { class: "primary", onclick: () => copy(s.name) }, t("Copy request for Claude")));
    }
    const state = s.retired ? t("retired") : !s.exists ? t("folder not found")
      : s.to_compile ? t("{n} to compile", { n: s.to_compile }) : s.checked ? t("up to date") : t("never checked");
    return h("tr", {}, h("td", {}, h("b", {}, s.name)), h("td", {}, tr(SOURCE_KINDS, s.kind)),
      h("td", {}, h("code", {}, s.root)), h("td", { class: s.to_compile || !s.exists ? "warn" : "" }, state),
      h("td", {}, ...actions));
  });
  box.append(rows.length
    ? h("table", {}, h("tr", {}, ...[t("Name"), t("Type"), t("Folder"), t("Status"), ""].map((x) => h("th", {}, x))), ...rows)
    : h("p", { class: "muted" }, t("No sources registered.")));
  if (owner) {
    const path = h("input", { placeholder: t("Vault folder, e.g. C:\\Users\\you\\Notes\\MyVault"), "aria-label": t("Obsidian vault folder"),
      spellcheck: "false" }) as HTMLInputElement;
    const name = h("input", { placeholder: t("Name (optional)"), class: "short", "aria-label": t("Source name"),
      spellcheck: "false" }) as HTMLInputElement;
    const add = async () => {
      if (!path.value.trim()) return;
      try {
        const res = await post<Json>("/api/sources/obsidian", { path: path.value, name: name.value });
        notice(t("Vault \"{name}\" added: {notes} notes. It stays read-only.\nTo compile them, paste into Claude Code: {request}",
          { name: res.name, notes: res.notes, request: compileRequest(res.name) }), "ok");
        navigator.clipboard?.writeText(compileRequest(res.name));
        render("status");
      } catch (err) { notice(explain(err)); }
    };
    path.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    box.append(h("h3", {}, t("Add an Obsidian vault")),
      h("div", { class: "row" }, path, name, h("button", { class: "primary", onclick: add }, t("Add"))),
      h("p", { class: "muted" }, t("The vault is never modified; notes that change later are flagged and recompiled.")));
  }
  box.append(h("p", {}, h("button", { class: "link", onclick: () => { sourceNav.name = ""; location.hash = "sources"; } },
    t("Choose what to read in each source, folder by folder →"))));
  root.append(box);
}

// ---------- Fonti page: what each source contributes, folder by folder ----------
// The same settings Claude reads (`source show`) and updates when asked in the conversation.
const sourceNav: { name: string; path: string } = { name: "", path: "" };
const SKIP_REASONS: Record<string, string> = {
  "excluded-by-user": "excluded by you",
  "excluded-dir": "skipped automatically (dependencies, build or tools)",
  "virtualenv": "skipped automatically (Python virtual environment)",
  "gitignored": "ignored by .gitignore",
  "hidden": "hidden folder",
  "secret-file": "possible secrets file",
  "unsupported-type": "type not read (images, binaries…)",
  "code-not-in-scope": "code, outside documents-only mode",
  "too-large": "too large",
  "binary-or-not-utf8": "binary or not UTF-8",
  "empty": "empty",
  "unreadable": "unreadable",
  "not-regular": "link or special file",
};
const READ_KINDS: Record<string, string> = { docs: "documents", project: "project files", config: "configuration files", code: "code files" };
const READ_KIND_ONE: Record<string, string> = { docs: "document", project: "project files", config: "configuration file", code: "code files" };
const SCOPE_LABELS: Record<string, string> = { docs: "Documents only", all: "Documents and code" };

function readSummary(read: Json): string {
  const parts = Object.entries(read).filter(([, n]) => (n as number) > 0).map(([k, n]) => `${n} ${tr((n as number) === 1 ? READ_KIND_ONE : READ_KINDS, k)}`);
  return parts.length ? parts.join(", ") : t("nothing");
}

function skippedSummary(skipped: Json): string {
  return Object.entries(skipped).sort((a, b) => (b[1] as number) - (a[1] as number))
    .map(([k, n]) => `${n} ${tr(SKIP_REASONS, k)}`).join(" · ");
}

async function sourcesView(root: HTMLElement) {
  const me = await get<Json>("/api/whoami");
  const owner = me.role === "owner";
  if (sourceNav.name) return sourceDetail(root, owner);
  const data = await get<Json>("/api/sources");
  root.append(h("h1", {}, t("Sources")),
    h("p", { class: "muted" }, t("The folders Mnemoloci learns from and, for each one, what is read. Claude reads these choices before compiling and updates them when you ask in the chat: changes made in a conversation show up here.")));
  const rows = (data.sources as Json[]).map((s) => h("tr", {},
    h("td", {}, h("button", { class: "link", onclick: () => { sourceNav.name = s.name; sourceNav.path = ""; render("sources"); } }, s.name)),
    h("td", {}, tr(SOURCE_KINDS, s.kind)), h("td", {}, tr(SCOPE_LABELS, s.scope ?? "all")),
    h("td", {}, h("code", {}, s.root)),
    h("td", {}, (s.exclude ?? []).length ? t("{n} excluded", { n: s.exclude.length }) : "—"),
    h("td", { class: s.to_compile || !s.exists ? "warn" : "" },
      s.retired ? t("retired") : !s.exists ? t("folder not found") : s.to_compile ? t("{n} to compile", { n: s.to_compile })
        : s.checked ? t("up to date") : t("never checked"))));
  root.append(rows.length
    ? h("table", { class: "sources-list" }, h("tr", {}, ...[t("Name"), t("Type"), t("Mode"), t("Folder"), t("Exclusions"), t("Status")]
        .map((x) => h("th", {}, x))), ...rows)
    : h("p", { class: "muted" }, t("No sources registered.")));
  if (!owner) return;
  const path = h("input", { placeholder: t("Folder, e.g. C:\\Users\\you\\Projects\\MyApp or C:\\Users\\you\\Projects"), "aria-label": t("Folder to add"),
    spellcheck: "false" }) as HTMLInputElement;
  const docsOnly = h("input", { type: "checkbox", checked: true, "aria-label": t("Documents only") }) as HTMLInputElement;
  const children = h("input", { type: "checkbox", "aria-label": t("One source per subfolder") }) as HTMLInputElement;
  const add = async () => {
    if (!path.value.trim()) return;
    try {
      const res = await post<Json>("/api/sources/projects", { path: path.value, docs_only: docsOnly.checked, children: children.checked });
      const skipped = (res.skipped as Json[]).map((s) => `${s.folder}: ${tr(ADD_SKIPPED, s.why)}`).join("\n");
      notice(t("Added {n} sources: {names}", { n: res.added.length, names: (res.added as Json[]).map((a) => a.name).join(", ") || "—" })
        + (skipped ? `\n${t("Not added:")}\n${skipped}` : ""), res.added.length ? "ok" : "error");
      render("sources");
    } catch (err) { notice(explain(err)); }
  };
  path.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  root.append(h("h2", {}, t("Add a project")),
    h("div", { class: "row" }, path, h("button", { class: "primary", onclick: add }, t("Add"))),
    h("label", { class: "check" }, docsOnly, " ", t("Documents only (README, notes, project files): the code can be added later")),
    h("label", { class: "check" }, children, " ", t("One source per subfolder (e.g. every project in C:\\Users\\you\\Projects)")),
    h("p", { class: "muted" }, t("The folders stay read-only. Once added, open them above to choose what to read.")));
}

const ADD_SKIPPED: Record<string, string> = { "already registered": "already registered", "empty": "empty", "the vault itself": "it is the vault itself" };

async function sourceDetail(root: HTMLElement, owner: boolean) {
  const name = sourceNav.name;
  const data = await get<Json>(`/api/sources/${encodeURIComponent(name)}/tree`, { path: sourceNav.path, fresh: 1 });
  const go = (path: string) => { sourceNav.path = path; render("sources"); };
  const back = h("button", { onclick: () => { sourceNav.name = ""; sourceNav.path = ""; render("sources"); } }, t("← All sources"));
  const scope = owner
    ? h("select", { "aria-label": t("Mode"), onchange: async (e: Event) => {
        try { await post(`/api/sources/${encodeURIComponent(name)}/scope`, { scope: (e.target as HTMLSelectElement).value }); }
        catch (err) { notice(explain(err)); }
        render("sources");
      } }, ...Object.entries(SCOPE_LABELS).map(([v, l]) => h("option", { value: v, selected: v === data.scope }, t(l))))
    : h("b", {}, tr(SCOPE_LABELS, data.scope));
  const crumbs: HTMLElement[] = [h("button", { class: "link", onclick: () => go("") }, name)];
  const parts = data.path ? String(data.path).split("/") : [];
  parts.forEach((p, i) => {
    crumbs.push(h("span", { class: "muted" }, " / "));
    crumbs.push(h("button", { class: "link", onclick: () => go(parts.slice(0, i + 1).join("/")) }, p));
  });
  root.append(h("div", { class: "pagebar" }, back, h("code", {}, data.root)),
    h("h1", {}, name),
    h("p", {}, t("Mode:"), " ", scope, " ",
      h("button", { onclick: () => { navigator.clipboard?.writeText(compileRequest(name));
        notice(t("Request copied, paste it into Claude Code:\n{request}", { request: compileRequest(name) }), "ok"); } },
        t("Copy request for Claude"))),
    h("p", {}, h("b", {}, t("Read in total:")), " ", readSummary(data.totals.read)),
    Object.keys(data.totals.skipped).length ? h("p", { class: "muted" }, t("Skipped:"), " ", skippedSummary(data.totals.skipped)) : "",
    data.exclude.length || data.include.length
      ? h("p", { class: "muted" },
          data.exclude.length ? `${t("Excluded by you:")} ${data.exclude.join(", ")}. ` : "",
          data.include.length ? `${t("Included by your choice:")} ${data.include.join(", ")}.` : "")
      : "",
    h("div", { class: "crumbs" }, ...crumbs));
  const toggle = async (folder: string, selected: boolean) => {
    try { await post(`/api/sources/${encodeURIComponent(name)}/folder`, { folder, selected }); }
    catch (err) { notice(explain(err)); }
    render("sources");
  };
  const STATUS: Record<string, string> = { included: "included", forced: "included (your choice)", excluded: "excluded by you", empty: "no readable files" };
  const folderRows = (data.folders as Json[]).map((f) => h("tr", { class: `src-${f.status}` },
    h("td", {}, h("input", { type: "checkbox", checked: f.selected, disabled: !owner, "aria-label": t("Read {folder}", { folder: f.rel }),
      onchange: (e: Event) => toggle(f.rel, (e.target as HTMLInputElement).checked) })),
    h("td", {}, h("button", { class: "link", onclick: () => go(f.rel) }, `${f.name}/`)),
    h("td", {}, f.status === "auto" ? tr(SKIP_REASONS, f.reason) : tr(STATUS, f.status),
      (f.status === "included" || f.status === "forced") && !f.read_total && Object.keys(f.skipped).length
        ? h("div", { class: "muted" }, t("nothing to read for now")) : ""),
    h("td", {}, f.read_total ? readSummary(f.read) : "—",
      Object.keys(f.skipped).length ? h("div", { class: "muted" }, t("skipped:"), " ", skippedSummary(f.skipped)) : "")));
  root.append(folderRows.length
    ? h("table", { class: "src-tree" }, h("tr", {}, ...["", t("Folder"), t("Status"), t("What is read")].map((x) => h("th", {}, x))), ...folderRows)
    : h("p", { class: "muted" }, t("No subfolders.")));
  const files = data.files as Json[];
  if (files.length) {
    const read = files.filter((f) => f.read).length;
    root.append(h("details", { open: files.length <= 30 },
      h("summary", {}, t("{n} files in this folder, {read} read", { n: files.length, read })),
      h("table", {}, ...files.map((f) => h("tr", { class: f.read ? "" : "muted" }, h("td", {}, f.name),
        h("td", {}, f.read ? `${t("read")} (${tr(READ_KINDS, f.kind)})` : tr(SKIP_REASONS, f.reason)))))));
  }
  root.append(h("p", { class: "muted" }, owner
    ? t("Untick a folder to exclude it; tick it to read it even when an automatic rule would skip it. It applies from the next compile; pages already created stay.")
    : t("Choices are changed from the PC.")));
}

// ---------- vault location (owner-only: hooks, MCP and the app all follow it) ----------
const VAULT_SOURCES: Record<string, string> = {
  argument: "chosen on the command line",
  environment: "set by the MNEMOLOCI_VAULT variable",
  registered: "registered in ~/.mnemoloci/vault.txt",
  default: "default location",
};

async function vaultSection(root: HTMLElement) {
  const me = await get<Json>("/api/whoami");
  if (me.role !== "owner") return;
  const data = await get<Json>("/api/vault");
  const box = h("section", { class: "vault" }, h("h2", {}, t("Vault")),
    h("p", {}, h("code", {}, data.path), " ", h("span", { class: "muted" }, `(${tr(VAULT_SOURCES, data.source)})`)));
  if (data.overridden) {
    box.append(h("p", { class: "warn" }, t("The MNEMOLOCI_VAULT variable or the command line decides which vault is used, so a change made here will not take effect until it is removed.")));
  }
  const input = h("input", { value: data.path, placeholder: t("Full path of an existing vault, e.g. C:\\Users\\you\\Notes\\MyVault"),
    "aria-label": t("Vault"), spellcheck: "false" }) as HTMLInputElement;
  const save = async () => {
    const next = input.value.trim();
    if (next === data.path) return notice(t("That is already the vault."), "ok");
    if (!confirm(t("Use \"{new}\" as the vault?\nHooks, the MCP server and the app follow it the next time they start: restart this app and your Claude Code sessions.", { new: next }))) return;
    try {
      const res = await post<Json>("/api/vault", { path: next });
      notice(t("Vault set to {path}. Restart this app and your Claude Code sessions to use it.", { path: res.path }), "ok");
      render("status");
    } catch (err) { notice(explain(err)); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  box.append(h("div", { class: "row" }, input, h("button", { class: "primary", onclick: save }, t("Save"))),
    h("p", { class: "muted" }, t("The vault must already exist. To create a new one, run `mnemoloci --vault <folder> init`.")));
  root.append(box);
}

// ---------- projects root (Remote Control sessions: "nuovo progetto X") ----------
async function projectsSection(root: HTMLElement) {
  const me = await get<Json>("/api/whoami");
  const data = await get<Json>("/api/projects");
  const box = h("section", { class: "projects" }, h("h2", {}, t("Projects folder")),
    h("p", { class: "muted" }, t("Claude creates new projects here when you ask for \"new project X\", from the phone too. Run `claude remote-control` in this folder.")));
  if (me.role === "owner") {
    const input = h("input", { value: data.root ?? "", placeholder: t("Full path, e.g. C:\\Users\\you\\Projects"),
      "aria-label": t("Projects folder"), spellcheck: "false" }) as HTMLInputElement;
    const save = async () => {
      if (input.value.trim() === (data.root ?? "")) return notice(t("That is already the folder."), "ok");
      if (data.root && !confirm(t("Use \"{new}\" instead of \"{old}\" for new projects?\nExisting folders are not moved.",
        { new: input.value.trim(), old: data.root }))) return;
      try {
        const res = await post<Json>("/api/projects/root", { root: input.value });
        notice(t("Projects folder: {root}", { root: res.root }) +
          (res.autostart_updated ? "\n" + t("Autostart updated.") : "") +
          (res.inside_git_repo ? "" : "\n" + t("The first time, run `claude` there and accept the trust prompt: it covers all subfolders.")), "ok");
        render("status");
      } catch (err) { notice(explain(err)); }
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
    box.append(h("div", { class: "row" }, input, h("button", { class: "primary", onclick: save }, t("Save"))));
  } else {
    box.append(h("p", {}, data.root ? h("code", {}, data.root) : t("Not set."),
      h("span", { class: "muted" }, " " + t("(changed from the PC)"))));
  }
  if (data.root) {
    box.append(data.projects.length
      ? h("details", {}, h("summary", {}, t("{n} folders", { n: data.projects.length })),
          h("table", {}, h("tr", {}, h("th", {}, t("Project")), h("th", {}, t("In the second brain"))),
            ...data.projects.map((p: Json) => h("tr", {}, h("td", {}, p.name), h("td", {}, p.source ? t("yes ({source})", { source: p.source }) : t("no"))))))
      : h("p", { class: "muted" }, t("No folders.")));
  }
  root.append(box);
}

// ---------- "Oggi": day/week digest, todos, backup ----------
const todayState = { day: "", week: false };
const isoDay = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function shiftDay(day: string, days: number): string {
  const d = day ? new Date(`${day}T12:00:00`) : new Date();
  d.setDate(d.getDate() + days);
  return isoDay(d);
}

async function todayView(root: HTMLElement) {
  const me = await get<Json>("/api/whoami");
  const data = await get<Json>("/api/today", { ...(todayState.day ? { day: todayState.day } : {}), ...(todayState.week ? { week: 1 } : {}) });
  const d = data.digest;
  const todayIso = isoDay(new Date());
  const isToday = !todayState.week && (!todayState.day || todayState.day === todayIso);
  const title = todayState.week ? t("Week {period} ({from} → {to})", { period: d.period, from: d.from, to: d.to })
    : isToday ? t("Today, {date}", { date: new Date().toLocaleDateString(locale(), { weekday: "long", day: "numeric", month: "long" }) }) : d.from;
  const go = (days: number) => { todayState.day = shiftDay(todayState.day || todayIso, todayState.week ? days * 7 : days); render("today"); };
  const bar = h("div", { class: "actions" },
    h("button", { onclick: () => go(-1), "aria-label": t("Previous") }, "←"),
    h("button", { onclick: () => { todayState.day = ""; render("today"); } }, t("Today")),
    h("button", { onclick: () => go(1), "aria-label": t("Next") }, "→"),
    h("button", { class: todayState.week ? "primary" : "", onclick: () => { todayState.week = !todayState.week; render("today"); } },
      todayState.week ? t("Day view") : t("Week view")),
    h("button", {
      onclick: async () => {
        try { notice(t("Summary saved to {path}", { path: (await post<Json>("/api/digest/save", { day: todayState.day, week: todayState.week })).path }), "ok"); }
        catch (err) { notice(explain(err)); }
      },
    }, t("Save summary")));

  // todos: quick add, check to close
  const text = h("input", { placeholder: t("New to-do"), "aria-label": t("New to-do") }) as HTMLInputElement;
  const due = h("input", { type: "date", class: "short", "aria-label": t("Due date") }) as HTMLInputElement;
  const addTodo = async () => {
    if (!text.value.trim()) return;
    try { await post("/api/todos/add", { text: text.value, due: due.value }); render("today"); } catch (err) { notice(explain(err)); }
  };
  text.addEventListener("keydown", (e) => { if (e.key === "Enter") addTodo(); });
  const close = async (id: string, status: string) => {
    try { await post(`/api/todos/${encodeURIComponent(id)}`, { status }); render("today"); } catch (err) { notice(explain(err)); }
  };
  const todoRow = (todo: Json) => {
    const box = h("input", { type: "checkbox", "aria-label": t("Done: {text}", { text: todo.text }), onchange: () => close(todo.id, "done") });
    const late = todo.due && todo.due < todayIso;
    return h("div", { class: `todo${late ? " late" : ""}` }, box, h("span", {}, todo.text),
      todo.due ? h("span", { class: `kind ${late ? "warn" : ""}` }, late ? t("overdue since {date}", { date: todo.due }) : t("due {date}", { date: todo.due })) : "",
      todo.project ? h("span", { class: "kind", title: todo.project }, todo.project.split(/[\\/]/).pop()) : "",
      h("button", { class: "link", title: t("Drop it"), onclick: () => close(todo.id, "dropped") }, "×"));
  };

  const card = (label: string, value: number, view?: string) =>
    h("div", { class: `card${value ? " warn" : ""}${view ? " clickable" : ""}`, onclick: view ? () => { location.hash = view; } : null },
      h("div", { class: "num" }, String(value)), h("div", {}, label));
  const list = (items: string[]) => h("ul", {}, ...items.map((i) => h("li", {}, i)));
  const block = (heading: string, content: Node | null) => content ? h("section", {}, h("h2", {}, heading), content) : null;

  const backupLine = data.backup.enabled
    ? h("p", { class: "muted" }, t("Backup: last {when}", { when: data.backup.last_commit ? data.backup.last_commit.at.slice(0, 16).replace("T", " ") : t("never") })
        + (data.backup.uncommitted_changes ? " · " + t("{n} unsaved changes", { n: data.backup.uncommitted_changes }) : " · " + t("everything saved"))
        + (data.backup.remote ? " · " + t("remote copy {state}", { state: data.backup.unpushed_commits ? t("({n} to push)", { n: data.backup.unpushed_commits }) : t("up to date") }) : ""),
        me.role === "owner" && data.backup.uncommitted_changes ? h("button", {
          class: "link", onclick: async () => {
            try { const r = await post<Json>("/api/backup/now", {}); notice(r.committed ? t("Backup done ({n} files).", { n: r.files }) : t("Nothing to save."), "ok"); render("today"); }
            catch (err) { notice(explain(err)); }
          },
        }, " " + t("Back up now")) : "")
    : h("p", { class: "muted" }, t("Vault backup is off: ask Claude to \"turn on the second brain backup\"."));

  root.append(
    h("h1", {}, title), bar,
    h("div", { class: "cards" },
      card(t("open to-dos"), d.todos.open), card(t("overdue"), d.todos.overdue.length),
      card(t("to review"), d.waiting.review, "review"), card(t("to compile"), d.waiting.to_compile, "clips"),
      card(t("screenshots"), d.waiting.inbox, "inbox")),
    h("section", {}, h("h2", {}, t("To-dos")),
      h("div", { class: "form" }, text, due, h("button", { class: "primary", onclick: addTodo }, t("Add"))),
      data.todos.length ? h("div", {}, ...data.todos.map(todoRow)) : h("p", { class: "muted" }, t("Nothing to do.")),
      d.todos.done.length ? h("p", { class: "muted" }, t("Completed in this period: {items}", { items: d.todos.done.map((x: Json) => x.text).join(" · ") })) : ""),
    ...[
      block(t("Work sessions"), d.sessions.length ? h("div", {}, ...d.sessions.map((s: Json) => h("div", { class: "result", onclick: () => { location.hash = "sessions"; } },
        h("div", { class: "title" }, s.title || t("Session"), h("span", { class: "kind" }, t("{n} requests", { n: s.requests }))),
        h("div", { class: "muted" }, s.project ?? ""),
        s.open_question ? h("div", { class: "snippet" }, t("Open question: {question}", { question: s.open_question })) : ""))) : null),
      block(t("Published pages"), d.pages_published.length ? h("ul", {}, ...d.pages_published.map((p: Json) =>
        h("li", {}, h("a", { class: "wikilink", onclick: () => openPage(slugToPath.get(p.slug) ?? "") }, p.title)))) : null),
      block(t("Saved answers"), d.answers_saved.length ? h("ul", {}, ...d.answers_saved.map((a: Json) =>
        h("li", {}, h("a", { class: "wikilink", onclick: () => openPage(a.path) }, a.title)))) : null),
      block(t("Saved content"), d.captured.length ? list(d.captured.map((c: Json) => `${c.title} (${tr(ORIGIN, c.origin)})`)) : null),
      ...Object.entries(d.memory).map(([kind, items]) => block(tr(MEMORY_KINDS, kind), list((items as Json[]).map((m) => m.title)))),
      block(t("Active goals"), d.goals.active.length ? list(d.goals.active.map((g: Json) => `[${g.id}] ${g.text}`)) : null),
    ].filter(Boolean) as HTMLElement[],
    backupLine);
  await loadSlugs();
}

// ---------- views ----------
const views: Record<string, View> = {
  today: todayView,
  async tools(root) {
    await toolsView(root, { h: h as any, notice, explain });
  },
  async features(root) {
    await featuresView(root, { h: h as any, notice, explain });
  },
  async settings(root) {
    await settingsView(root, { h: h as any, notice, explain });
    await embeddingsSettings(root);
    await localExtractionSettings(root);
  },
  async status(root) {
    const s = await get<Json>("/api/status");
    const card = (label: string, value: unknown, warn = false) =>
      h("div", { class: `card${warn ? " warn" : ""}` }, h("div", { class: "num" }, String(value)), h("div", {}, label));
    const issues = Object.entries(s.counts as Json).filter(([, n]) => (n as number) > 0);
    root.append(
      h("h1", {}, t("Vault status")),
      h("p", { class: "muted" }, s.vault),
      h("div", { class: "cards" },
        card(t("pages"), s.pages),
        card(t("in review"), s.pending_review, s.pending_review > 0),
        card(t("screenshots to transcribe"), s.inbox_pending, s.inbox_pending > 0),
        card(t("interrupted transactions"), s.incomplete_transactions.length, s.incomplete_transactions.length > 0),
        card(s.healthy ? t("vault healthy") : t("needs a look"), s.healthy ? "OK" : "!", !s.healthy)),
      h("h2", {}, t("Issues")),
      issues.length
        ? h("ul", {}, ...issues.map(([k, n]) => h("li", {}, `${tr(ISSUES, k)}: ${n}`)))
        : h("p", { class: "muted" }, t("No issues.")),
      s.compile?.active ? h("p", {}, t("Compile in progress: {name} ({state})", { name: s.compile.active, state: s.compile.state })) : "",
    );
    const qual = await get<Json>("/api/quality");
    if (qual.score !== null && qual.score !== undefined) {
      const trend = (qual.history as Json[]).map((x) => x.score).slice(-12);
      root.append(h("section", { class: "quality" }, h("h2", {}, t("Wiki quality")),
        h("div", { class: "cards" },
          h("div", { class: `card${qual.score < 70 ? " warn" : ""}` }, h("div", { class: "num" }, `${qual.score}/100`),
            h("div", {}, qual.delta !== undefined ? t("{delta} since the last measurement", { delta: `${qual.delta >= 0 ? "+" : ""}${qual.delta}` }) : t("overall score"))),
          ...Object.entries(qual.components).map(([k, v]) => h("div", { class: "card" },
            h("div", { class: "num" }, `${Math.round((v as number) * 100)}%`), h("div", {}, tr(QUALITY_PARTS, k))))),
        trend.length > 1 ? h("p", { class: "muted" }, t("Trend: {trend}", { trend: trend.join(" → ") })) : ""));
    }
    const me = await get<Json>("/api/whoami");
    if (me.role === "owner") await healthSection(root);
    await sourcesSection(root, me.role === "owner");
    await vaultSection(root);
    await projectsSection(root);
    await phoneSection(root);
    if (me.role === "owner") await connectorsSection(root);
  },

  async search(root) {
    const input = h("input", { type: "search", placeholder: t("Search your second brain…"), autofocus: true }) as HTMLInputElement;
    const results = h("div", { class: "results" });
    const run = async () => {
      if (!input.value.trim()) return results.replaceChildren();
      try {
        const r = await get<Json>("/api/search", { q: input.value, limit: 25 });
        const item = (x: Json, extra = "") =>
          h("div", { class: "result", onclick: () => openPage(x.path) },
            h("div", { class: "title" }, x.title, h("span", { class: `kind kind-${x.kind || "memory"}` }, x.kind || t("memory")),
              x.semantic !== undefined ? h("span", { class: "kind ok", title: t("similarity {score}", { score: x.semantic }) }, t("semantic")) : ""),
            h("div", { class: "muted" }, extra || x.summary || ""),
            x.snippet ? h("div", { class: "snippet" }, x.snippet.replace(/[[\]]/g, "")) : null);
        results.replaceChildren(
          ...(r.results.length ? r.results.map((x: Json) => item(x)) : [h("p", { class: "muted" }, t("No results."))]),
          ...(r.related?.length ? [h("h3", {}, t("Related")), ...r.related.map((x: Json) => item(x, t("linked to {page}", { page: x.via })))] : []));
      } catch (err) {
        results.replaceChildren(h("p", { class: "error" }, explain(err)));
      }
    };
    let timer = 0;
    input.addEventListener("input", () => { clearTimeout(timer); timer = window.setTimeout(run, 200); });
    root.append(h("h1", {}, t("Search")), input, results);
    await loadSlugs();
  },

  async graph(root) {
    const [g, analysis] = await Promise.all([loadSlugs(), get<Json>("/api/graph/analysis", { top: 8 })]);
    const box = h("div", { class: "graph" });
    let byCommunity = false;
    const palette = ["#4f7cff", "#e0873a", "#2fb37a", "#b05bd6", "#d4a017", "#e05a8a", "#1fa5b8", "#8a6d3b"];
    const communityColor = new Map<string, string>(
      (analysis.communities as Json[]).map((c, i) => [c.label as string, palette[i % palette.length]]));
    const toggle = h("button", {
      onclick: () => {
        byCommunity = !byCommunity;
        toggle.textContent = byCommunity ? t("Color by type") : t("Color by community");
        fg.nodeColor(fg.nodeColor());  // re-apply the accessor
      },
    }, t("Color by community"));
    root.append(h("h1", {}, t("Graph")),
      h("div", { class: "pagebar" }, h("span", { class: "muted" }, t("{pages} pages, {links} links. Click a node to open it.", { pages: g.nodes.length, links: g.links.length })), toggle),
      box);
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const colors: Json = { concept: "#4f7cff", entity: "#e0873a", comparison: "#2fb37a", overview: "#b05bd6", query: "#d4a017", memory: "#8a8f98" };
    const fg = new ForceGraph(box)
      .width(box.clientWidth).height(Math.max(420, innerHeight - 190))
      .backgroundColor(dark ? "#15171c" : "#ffffff")
      .graphData({ nodes: g.nodes, links: g.links })
      .nodeLabel((n: any) => n.title)
      .nodeColor((n: any) => byCommunity
        ? communityColor.get(analysis.community_of?.[n.id]) ?? "#999"
        : colors[n.kind] ?? "#888")
      .nodeRelSize(5)
      .linkColor(() => (dark ? "#3a3f4a" : "#cfd3da"))
      .onNodeClick((n: any) => openPage(n.path))
      .nodeCanvasObjectMode(() => "after")
      .nodeCanvasObject((n: any, ctx, scale) => {
        if (scale < 1.2) return;
        ctx.font = `${11 / scale}px system-ui`;
        ctx.fillStyle = dark ? "#d7dae0" : "#333";
        ctx.fillText(n.title, n.x + 6 / scale, n.y + 3 / scale);
      });
    graphInstance = fg as any;
    const pageLink = (slug: string, title: string) =>
      h("a", { class: "wikilink", onclick: () => slugToPath.get(slug) && openPage(slugToPath.get(slug)!) }, title);
    const col = (heading: string, items: Node[]) => items.length
      ? h("section", {}, h("h2", {}, heading), h("ul", {}, ...items.map((i) => h("li", {}, i)))) : null;
    root.append(h("div", { class: "analysis" }, ...[
      col(t("Hub pages"), analysis.hubs.map((x: Json) => h("span", {}, pageLink(x.slug, x.title), " · " + t("{n} links", { n: x.links })))),
      col(t("Bridge pages"), analysis.bridges.map((x: Json) => h("span", {}, pageLink(x.slug, x.title),
        h("span", { class: "muted" }, " " + t("connect different topics"))))),
      col(t("Communities"), analysis.communities.map((c: Json) => h("span", {},
        h("span", { class: "dot", style: `background:${communityColor.get(c.label)}` }), ` ${c.label} (${c.size}): `,
        h("span", { class: "muted" }, c.pages.slice(0, 6).join(", "))))),
      col(t("Unexpected links"), analysis.surprising_links.map((e: Json) => h("span", {}, `${e.a} ↔ ${e.b}`,
        h("span", { class: "muted" }, ` (${t("between {a} and {b}", { a: e.between[0], b: e.between[1] })})`)))),
      col(t("Isolated pages"), analysis.isolated.slice(0, 20).map((x: string) => h("span", {}, x))),
    ].filter(Boolean) as HTMLElement[]));
  },

  async people(root) {
    const kind = (location.hash.match(/type=(\w+)/)?.[1]) ?? "person";
    const data = await get<Json>("/api/people", { type: kind });
    await loadSlugs();
    root.append(h("h1", {}, tr(ENTITY_TYPES, kind, t("People"))),
      h("div", { class: "actions" }, ...Object.keys(ENTITY_TYPES).map((type) =>
        h("button", { class: type === kind ? "primary" : "", onclick: () => { history.replaceState(null, "", `#people?type=${type}`); render("people"); } }, tr(ENTITY_TYPES, type)))),
      h("p", { class: "muted" }, t("Sorted by how much they matter now: how many pages mention them, how many sources they come from, how recently they were updated.")),
      data.entities.length
        ? h("div", { class: "results" }, ...data.entities.map((e: Json) => h("div", { class: "result", onclick: () => openPage(e.path) },
            h("div", { class: "title" }, e.title, e.entity_type && kind === "all" ? h("span", { class: "kind" }, e.entity_type) : "",
              h("span", { class: "kind" }, t("mentioned by {n}", { n: e.mentioned_by })), h("span", { class: "kind" }, t("{n} sources", { n: e.sources }))),
            h("div", { class: "muted" }, e.summary ?? ""),
            e.related.length ? h("div", { class: "snippet" }, t("Related to: {items}", { items: e.related.join(", ") })) : "")))
        : h("p", { class: "muted" }, t("Nothing here yet: entities appear when Claude compiles sources that mention them.")));
  },

  async review(root) {
    const list = await get<Json>("/api/review");
    const detail = h("div", { class: "detail" });
    const open = async (id: string) => {
      const c = await get<Json>(`/api/review/${encodeURIComponent(id)}`);
      const article = h("article", { class: "page" });
      article.innerHTML = renderMarkdown(splitFrontmatter(c.content).body);
      const approve = async () => {
        try {
          await post(`/api/review/${encodeURIComponent(id)}/approve`, { content_sha256: c.content_sha256 });
          notice(t("Page published."), "ok");
          render("review");
        } catch (err) { notice(explain(err)); }
      };
      const reject = async () => {
        const reason = prompt(t("Why are you rejecting it?"));
        if (!reason) return;
        try {
          await post(`/api/review/${encodeURIComponent(id)}/reject`, { reason });
          notice(t("Rejected and archived."), "ok");
          render("review");
        } catch (err) { notice(explain(err)); }
      };
      detail.replaceChildren(
        h("h2", {}, c.slug),
        h("p", { class: "muted" }, t("destination: {path}", { path: c.target_path })),
        h("ul", { class: "reasons" }, ...c.reasons.map((r: Json) => h("li", {}, h("b", {}, tr(REASONS, r.code)), ` — ${r.detail}`))),
        h("div", { class: "actions" }, h("button", { class: "primary", onclick: approve }, t("Approve and publish")),
          h("button", { onclick: reject }, t("Reject"))),
        ...(c.extra_writes ?? []).map((e: Json) => h("details", { class: "meta" },
          h("summary", {}, t("Also changes: {path}", { path: e.target_path })), h("pre", {}, e.content))),
        article);
    };
    root.append(h("h1", {}, t("Review")),
      list.candidates.length
        ? h("div", { class: "split" },
            h("div", { class: "list" }, ...list.candidates.map((c: Json) =>
              h("div", { class: "result", "data-id": c.id, onclick: () => open(c.id) }, h("div", { class: "title" }, c.slug),
                h("div", { class: "muted" }, c.reasons.map((r: string) => tr(REASONS, r)).join(", "))))),
            detail)
        : h("p", { class: "muted" }, t("Nothing to review.")));
    if (list.candidates.length) await open(list.candidates[0].id);

    const proposed = (await get<Json>("/api/instincts", { status: "proposed" })).instincts;
    const decide = async (id: string, action: "approve" | "reject") => {
      const reason = action === "reject" ? prompt(t("Why are you rejecting it?")) : "";
      if (reason === null) return;
      try {
        await post(`/api/instincts/${encodeURIComponent(id)}/${action}`, { reason });
        notice(action === "approve" ? t("Instinct activated: upcoming sessions will use it.") : t("Instinct rejected."), "ok");
        render("review");
      } catch (err) { notice(explain(err)); }
    };
    root.append(h("h2", {}, t("Proposed instincts")),
      h("p", { class: "muted" }, t("Rules drawn from your corrections. None is used until you approve it.")),
      proposed.length
        ? h("div", {}, ...proposed.map((i: Json) => h("div", { class: "instinct" },
            h("div", {}, h("b", {}, t("When") + " "), i.trigger, h("b", {}, " → "), i.action),
            h("div", { class: "muted" }, `${tn(i.evidence, "{n} piece of evidence", "{n} pieces of evidence")} · ${t("confidence {pct}%", { pct: Math.round(i.confidence * 100) })} · ${i.scope === "global" ? t("global") : i.scope}`),
            h("div", { class: "actions" },
              h("button", { class: "primary", onclick: () => decide(i.id, "approve") }, t("Approve")),
              h("button", { onclick: () => decide(i.id, "reject") }, t("Reject")),
              h("button", { onclick: () => openPage(i.path) }, t("Evidence"))))))
        : h("p", { class: "muted" }, t("No pending instincts.")));
  },

  async memory(root) {
    let status: "active" | "retracted" | "suppressed" = "active";
    const tabs = h("div", { class: "actions" });
    const list = h("div", {});
    const tabLabel = (s: string) => (s === "active" ? t("Pending") : s === "retracted" ? t("Retracted") : t("Suppressed"));
    const emptyText: Json = { active: t("No pending notes right now."), retracted: t("No retracted notes."),
                              suppressed: t("No suppressed notes.") };
    const retract = async (id: string) => {
      const reason = prompt(t("Why are you retracting it?"));
      if (!reason) return;
      try {
        await post(`/api/memory/${encodeURIComponent(id)}/retract`, { reason });
        notice(t("Note retracted."), "ok");
        draw();
      } catch (err) { notice(explain(err)); }
    };
    const suppress = async (id: string) => {
      try {
        await post(`/api/memory/${encodeURIComponent(id)}/suppress`, {});
        notice(t("Note hidden."), "ok");
        draw();
      } catch (err) { notice(explain(err)); }
    };
    const unsuppress = async (id: string) => {
      try {
        await post(`/api/memory/${encodeURIComponent(id)}/unsuppress`, {});
        notice(t("Note reactivated."), "ok");
        draw();
      } catch (err) { notice(explain(err)); }
    };
    const draw = async () => {
      const data = await get<Json>("/api/memory", { source: "assistant-auto", status });
      const items = data.memories as Json[];
      tabs.replaceChildren(...(["active", "retracted", "suppressed"] as const).map((s) =>
        h("button", { class: s === status ? "primary" : "", onclick: () => { status = s; draw(); } }, tabLabel(s))));
      list.replaceChildren(
        items.length
          ? h("div", {}, ...items.map((m) => h("div", { class: "instinct" },
              h("div", {}, h("span", { class: "kind" }, tr(MEMORY_KINDS, m.kind)), " ", h("b", {}, m.title)),
              h("div", { class: "muted" }, [m.project, (m.created ?? "").slice(0, 10)].filter(Boolean).join(" · ")),
              m.retractedReason ? h("div", { class: "muted" }, t("reason: {reason}", { reason: m.retractedReason })) : "",
              h("div", { class: "actions" },
                status === "active" ? h("button", { onclick: () => retract(m.id) }, t("Retract")) : "",
                status === "active" ? h("button", { onclick: () => suppress(m.id) }, t("Suppress")) : "",
                status === "suppressed" ? h("button", { onclick: () => unsuppress(m.id) }, t("Reactivate")) : "",
                h("button", { onclick: () => openPage(m.path) }, t("Open"))))))
          : h("p", { class: "muted" }, emptyText[status]));
    };
    root.append(h("h1", {}, t("Automatically saved memory")),
      h("p", { class: "muted" }, t("Decisions, facts and session handoffs the assistant saved without asking. Retract the wrong ones, suppress the merely unhelpful ones.")),
      tabs, list);
    await draw();
  },

  async goals(root) {
    const data = await get<Json>("/api/goals", { all: 1 });
    const text = h("input", { placeholder: t("New goal (one line)") }) as HTMLInputElement;
    const area = h("input", { placeholder: t("Area (optional)"), class: "short" }) as HTMLInputElement;
    const add = async () => {
      try {
        await post("/api/goals/add", { text: text.value, area: area.value });
        render("goals");
      } catch (err) { notice(explain(err)); }
    };
    const sections = Object.keys(GOAL_SECTIONS).map((status) => {
      const goals = data.goals.filter((g: Json) => g.status === status);
      if (!goals.length) return null;
      return h("section", {}, h("h2", {}, tr(GOAL_SECTIONS, status)), ...goals.map((g: Json) => {
        const select = h("select", {
          onchange: async (e: Event) => {
            try {
              await post(`/api/goals/${g.id}`, { status: (e.target as HTMLSelectElement).value });
              render("goals");
            } catch (err) { notice(explain(err)); }
          },
        }, ...Object.keys(GOAL_STATUS).map((v) => h("option", { value: v, selected: v === g.status }, tr(GOAL_STATUS, v))));
        return h("div", { class: "goal" }, h("b", {}, g.id), h("span", {}, g.text),
          g.area ? h("span", { class: "kind" }, g.area) : null, select);
      }));
    });
    root.append(h("h1", {}, t("Goals")), h("div", { class: "form" }, text, area, h("button", { class: "primary", onclick: add }, t("Add"))),
      ...sections.filter(Boolean) as HTMLElement[]);
  },

  async profile(root) {
    const data = await get<Json>("/api/profile");
    const key = h("input", { placeholder: t("Fact (e.g. role)"), class: "short" }) as HTMLInputElement;
    const value = h("input", { placeholder: t("Value") }) as HTMLInputElement;
    const save = async () => {
      try {
        await post("/api/profile/set", { key: key.value, value: value.value, source: "app" });
        render("profile");
      } catch (err) { notice(explain(err)); }
    };
    root.append(h("h1", {}, t("Profile")),
      h("p", { class: "muted" }, t("Facts about you, injected into every new Claude Code session. Sensitive data (IBAN, tax code, passwords) is refused.")),
      h("div", { class: "form" }, key, value, h("button", { class: "primary", onclick: save }, t("Save"))),
      h("table", {}, h("tr", {}, ...[t("Fact"), t("Value"), t("Confidence"), t("Source"), t("Updated")].map((x) => h("th", {}, x))),
        ...data.facts.map((f: Json) => h("tr", {}, ...[f.key, f.value, f.confidence, f.source, f.updated].map((v) => h("td", {}, String(v ?? "")))))));
  },

  async skills(root) {
    const input = h("input", { type: "search", placeholder: t("Search existing skills…") }) as HTMLInputElement;
    const results = h("div", { class: "results" });
    const run = async () => {
      if (!input.value.trim()) return results.replaceChildren();
      try {
        const r = await get<Json>("/api/skills", { q: input.value, limit: 25 });
        results.replaceChildren(...r.results.map((s: Json) => h("div", { class: "result" },
          h("div", { class: "title" }, s.name, h("span", { class: `kind ${s.installed ? "ok" : ""}` }, s.installed ? t("installed") : t("available"))),
          h("div", { class: "muted" }, s.description),
          h("div", { class: "locs" }, ...s.locations.map((l: Json) => h("span", { class: "kind", title: l.path }, l.plugin ? `${l.label}: ${l.plugin}` : l.label))))));
      } catch (err) { results.replaceChildren(h("p", { class: "error" }, explain(err))); }
    };
    let timer = 0;
    input.addEventListener("input", () => { clearTimeout(timer); timer = window.setTimeout(run, 250); });
    const gen = await get<Json>("/api/skills/generated");
    const quality = h("div", {}, h("p", { class: "muted" }, t("Analysing quality and usage (the first time it reads every session)…")));
    root.append(h("h1", {}, t("Skills")), input, results,
      h("h2", {}, t("Generated, in review")),
      gen.generated.length
        ? h("div", {}, ...gen.generated.map((s: Json) => h("div", { class: "result" },
            h("div", { class: "title" }, s.name, h("span", { class: `kind verdict-${s.guard_verdict}` }, s.guard_verdict ?? "?")),
            h("div", { class: "muted" }, s.description),
            h("div", { class: "muted" }, s.installed_at?.length ? t("installed in {places}", { places: s.installed_at.join(", ") }) : t("not installed")))))
        : h("p", { class: "muted" }, t("No generated skills.")),
      quality);
    get<Json>("/api/skills/quality").then((q) => {
      const st = q.stocktake, cu = q.curate;
      const archive = async (name: string) => {
        if (!confirm(t("Move \"{name}\" to the archive? Restore it with: {command}", { name, command: `mnemoloci skills restore ${name}` }))) return;
        try { await post("/api/skills/archive", { name }); notice(t("{name} archived.", { name }), "ok"); render("skills"); }
        catch (err) { notice(explain(err)); }
      };
      quality.replaceChildren(
        h("h2", {}, t("Quality")),
        h("p", { class: "muted" }, t("{n} of your skills, {issues} with issues, average score {score}.", { n: st.skills, issues: st.with_issues, score: st.average_score ?? "-" })),
        h("div", {}, ...st.results.filter((r: Json) => r.issues.length).slice(0, 15).map((r: Json) => h("div", { class: "result" },
          h("div", { class: "title" }, r.name, h("span", { class: `kind ${r.score < 60 ? "warn" : ""}` }, `${r.score}/100`)),
          h("ul", { class: "muted" }, ...r.issues.map((i: string) => h("li", {}, i)))))),
        h("h2", {}, t("Cleanup")),
        h("p", { class: "muted" }, t("{installed} installed · {unused} unused in the last {days} days · {twice} installed twice. Nothing is archived without your command.",
          { installed: cu.installed, unused: cu.unused.length, days: cu.window_days, twice: cu.installed_twice.length })),
        h("div", {}, ...cu.unused.filter((u: Json) => u.archivable).slice(0, 20).map((u: Json) => h("div", { class: "goal" },
          h("span", {}, u.name), h("span", { class: "kind" }, u.last_used ? t("last used {date}", { date: u.last_used }) : t("never used")),
          h("button", { onclick: () => archive(u.name) }, t("Archive"))))));
    }).catch((err) => quality.replaceChildren(h("p", { class: "error" }, explain(err))));
  },

  async clips(root) {
    const data = await get<Json>("/api/clips");
    const url = h("input", { type: "url", placeholder: t("Link to a web page, PDF or YouTube video"), "aria-label": t("Link") }) as HTMLInputElement;
    const note = h("input", { placeholder: t("Why it matters (optional)"), "aria-label": t("Note") }) as HTMLInputElement;
    const mine = h("input", { type: "checkbox" }) as HTMLInputElement;
    const title = h("input", { placeholder: t("Title"), "aria-label": t("Text title") }) as HTMLInputElement;
    const text = h("textarea", { class: "clip-text", placeholder: t("Or paste a text to remember here"), "aria-label": t("Text") }) as HTMLTextAreaElement;
    const busy = h("span", { class: "muted" }, "");
    const saved = (res: Json) => {
      if (res.duplicate) return notice(t("Already saved: {path}", { path: res.path }), "ok");
      const flag = res.injection?.verdict && res.injection.verdict !== "safe"
        ? "\n" + t("Warning: it contains sentences aimed at an AI ({n}).", { n: res.injection.count }) : "";
      notice(t("Saved: {title} ({chars} characters{truncated}).{flag}", {
        title: res.title, chars: res.chars.toLocaleString(locale()), truncated: res.truncated ? t(", truncated") : "", flag,
      }), "ok");
      render("clips");
    };
    const run = async (path: string, payload: Json) => {
      busy.textContent = t("saving…");
      try { saved(await post<Json>(path, payload)); }
      catch (err) { notice(err instanceof ApiError && FRIENDLY[err.code] ? tr(FRIENDLY, err.code) : explain(err)); }
      finally { busy.textContent = ""; }
    };
    const saveUrl = () => url.value.trim() && run("/api/clips/url", { url: url.value.trim(), note: note.value, mine: mine.checked });
    const saveText = () => run("/api/clips/text", { title: title.value, text: text.value, note: note.value, mine: mine.checked });
    url.addEventListener("keydown", (e) => { if (e.key === "Enter") saveUrl(); });
    const pending = Object.values(data.to_compile ?? {}).reduce((a: number, b) => a + (b as number), 0) as number;
    root.append(
      h("h1", {}, t("Saved")),
      h("p", { class: "muted" }, t("Links, PDFs, videos and texts to remember. They become pages when Claude compiles them (\"compile the saved items\"); pages from external sources go through Review.")),
      h("div", { class: "form" }, url, h("button", { class: "primary", onclick: saveUrl }, t("Save link")), busy),
      h("div", { class: "form" }, note, h("label", { class: "check" }, mine, t("I wrote this"))),
      h("details", { class: "paste" }, h("summary", {}, t("Paste a text")),
        h("div", { class: "form" }, title), text, h("button", { onclick: saveText }, t("Save text"))),
      pending ? h("p", { class: "warn-text" }, t("{n} to compile: ask Claude to \"compile the saved items\".", { n: pending })) : "",
      data.clips.length
        ? h("div", { class: "results" }, ...data.clips.map((c: Json) => h("div", { class: "result", onclick: () => openPage(c.path) },
            h("div", { class: "title" }, c.title, h("span", { class: "kind" }, tr(ORIGIN, c.origin)),
              c.mine ? h("span", { class: "kind ok" }, t("mine")) : "",
              c.injection && c.injection !== "safe" ? h("span", { class: "kind warn", title: t("contains sentences aimed at an AI") }, t("warning")) : ""),
            h("div", { class: "muted" }, [(c.captured ?? "").slice(0, 16).replace("T", " "), c.url ?? ""].filter(Boolean).join(" · ")))))
        : h("p", { class: "muted" }, t("Nothing saved yet.")));
    url.focus();
  },

  async answers(root) {
    const data = await get<Json>("/api/answers");
    await loadSlugs();
    root.append(h("h1", {}, t("Saved answers")),
      h("p", { class: "muted" }, t("Claude's answers saved as pages (\"save this answer\"). They flag themselves when the pages they rely on change.")),
      data.answers.length
        ? h("div", { class: "results" }, ...data.answers.map((a: Json) => h("div", { class: "result", onclick: () => openPage(a.path) },
            h("div", { class: "title" }, a.title, a.stale.length
              ? h("span", { class: "kind warn", title: t("changed: {pages}", { pages: a.stale.join(", ") }) }, t("needs update")) : h("span", { class: "kind ok" }, t("up to date"))),
            h("div", {}, a.question ?? ""),
            h("div", { class: "muted" }, `${a.updated ?? ""} · ${t("based on {pages}", { pages: a.based_on.map((s: string) => slugToTitle.get(s) ?? s).join(", ") })}`))))
        : h("p", { class: "muted" }, t("No saved answers.")));
    const briefs = (await get<Json>("/api/briefings")).items;
    root.append(h("h2", {}, t("Briefings and research")),
      briefs.length
        ? h("div", { class: "results" }, ...briefs.map((b: Json) => h("div", { class: "result", onclick: () => openPage(b.path) },
            h("div", { class: "title" }, b.title, h("span", { class: "kind" }, b.kind === "research" ? t("research") : (b.mode ?? t("briefing"))),
              b.coverage !== undefined && b.coverage !== null ? h("span", { class: `kind ${b.coverage >= 0.8 ? "ok" : "warn"}` }, t("sources {pct}%", { pct: Math.round(b.coverage * 100) })) : ""),
            h("div", { class: "muted" }, b.date))))
        : h("p", { class: "muted" }, t("None yet: ask Claude to \"brief me on X\" or \"research X\".")));
  },

  async sessions(root) {
    const input = h("input", { type: "search", placeholder: t("Search what was said in past sessions…") }) as HTMLInputElement;
    const project = h("input", { placeholder: t("Filter by folder (optional)"), class: "short", "aria-label": t("Folder") }) as HTMLInputElement;
    const list = h("div", { class: "list" }, h("p", { class: "muted" }, t("Loading… (the first time it indexes every session: this can take a minute)")));
    const detail = h("div", { class: "detail" });
    const row = (s: Json) => h("div", { class: "result", onclick: () => show(s.session_id, s.matches?.[0]?.idx) },
      h("div", { class: "title" }, s.title || t("Untitled session"), h("span", { class: "kind" }, t("{n} requests", { n: s.n_user }))),
      h("div", { class: "muted" }, `${s.started} → ${s.ended} · ${s.cwd ?? ""}`),
      ...(s.matches ?? []).map((m: Json) => h("div", { class: "snippet" }, `#${m.idx} ${m.role === "user" ? t("you") : "Claude"}: ${m.snippet.replace(/[[\]]/g, "")}`)),
      s.open_question && !s.matches ? h("div", { class: "snippet" }, t("Open question: {question}", { question: s.open_question })) : "");
    const show = async (id: string, around?: number) => {
      detail.replaceChildren(h("p", { class: "muted" }, t("Loading…")));
      try {
        const s = await get<Json>("/api/sessions/show", around === undefined ? { id } : { id, around, context: 6 });
        detail.replaceChildren(
          h("h2", {}, s.title || t("Session")),
          h("p", { class: "muted" }, t("{started} → {ended} · {n} messages · {id}", { started: s.started, ended: s.ended, n: s.total_messages, id: s.session_id })),
          h("p", { class: "muted" }, t("To resume it in Claude Code: \"resume session {id}\".", { id: s.session_id.slice(0, 8) })),
          ...s.messages.map((m: Json) => {
            if (m.note) return h("p", { class: "muted" }, m.note);
            const article = h("article", { class: `page msg msg-${m.role}` });
            article.innerHTML = renderMarkdown(m.text);
            return h("div", {}, h("div", { class: "muted" }, `#${m.idx} · ${m.role === "user" ? t("you") : "Claude"} · ${m.at}`), article);
          }));
      } catch (err) { detail.replaceChildren(h("p", { class: "error" }, explain(err))); }
    };
    const load = async () => {
      try {
        const q = input.value.trim();
        const r = q ? await get<Json>("/api/sessions/search", { q, project: project.value.trim(), limit: 15 })
          : await get<Json>("/api/sessions/recent", { project: project.value.trim(), limit: 20 });
        list.replaceChildren(...(r.sessions.length ? r.sessions.map(row) : [h("p", { class: "muted" }, t("No sessions found."))]));
      } catch (err) { list.replaceChildren(h("p", { class: "error" }, explain(err))); }
    };
    let timer = 0;
    [input, project].forEach((el) => el.addEventListener("input", () => { clearTimeout(timer); timer = window.setTimeout(load, 350); }));
    root.append(h("h1", {}, t("Sessions")), h("div", { class: "form" }, input, project), h("div", { class: "split" }, list, detail));
    await load();
  },

  async maintain(root) {
    const d = await get<Json>("/api/maintain");
    await loadSlugs();
    const title = (slug: string) => slugToTitle.get(slug) ?? slug;
    const approveFlow = async (path: string, payload: Json, what: string) => {
      try {
        const dry = await post<Json>(path, payload);
        if (dry.dry_run === false || !dry.approval_sha256) {
          notice(t("Nothing to change."), "ok");
          if (location.hash.slice(1) === "maintain") render("maintain");
          return;
        }
        if (!confirm(t("{what}\n\nPages affected: {n}. Go ahead?", { what, n: (dry.pages ?? dry.fixes?.map((f: Json) => f.page) ?? []).length }))) return;
        await post(path, { ...payload, approved: dry.approval_sha256 });
        notice(t("Done."), "ok");
        if (location.hash.slice(1) === "maintain") render("maintain");  // never pull the user back from another view
      } catch (err) { notice(explain(err)); }
    };
    const fixes = d.fix_links.fixes ?? [];
    const boxes: [HTMLInputElement, Json][] = d.links.map((s: Json) => [h("input", { type: "checkbox" }) as HTMLInputElement, s]);
    root.append(h("h1", {}, t("Wiki maintenance")),
      h("p", { class: "muted" }, t("Every change shows what it will do first and asks for confirmation. Merges and synthesis pages are written by Claude and go through Review.")),
      h("section", {}, h("h2", {}, t("Links to fix ({n})", { n: fixes.length })),
        fixes.length ? h("div", {}, h("ul", {}, ...fixes.map((f: Json) => h("li", {}, `${title(f.page)}: [[${f.from}]] → ${title(f.to)}`))),
          h("button", { class: "primary", onclick: () => approveFlow("/api/maintain/fix-links", {}, t("Fixing links that have only one possible target.")) }, t("Fix")))
          : h("p", { class: "muted" }, t("No broken links that can be fixed automatically.")),
        d.fix_links.unresolved?.length ? h("p", { class: "muted" }, t("Ambiguous, fix by hand: {links}", { links: d.fix_links.unresolved.map((u: Json) => u.link).join(", ") })) : ""),
      h("section", {}, h("h2", {}, t("Missing links ({n})", { n: d.links.length })),
        d.links.length ? h("div", {}, ...boxes.map(([box, s]) => h("label", { class: "check line" }, box,
            h("span", {}, `${title(s.page)} → ${title(s.target)}: `, h("span", { class: "muted" }, s.excerpt)))),
          h("button", { class: "primary", onclick: () => {
            const chosen = boxes.filter(([b]) => b.checked).map(([, s]) => s);
            if (!chosen.length) return notice(t("Select at least one link."));
            approveFlow("/api/maintain/apply-links", { chosen }, t("Adding {n} links.", { n: chosen.length }));
          } }, t("Link the selected")))
          : h("p", { class: "muted" }, t("No missing links found."))),
      h("section", {}, h("h2", {}, t("Tags to merge ({n})", { n: d.tags.length })),
        d.tags.length ? h("ul", {}, ...d.tags.map((g: Json) => h("li", {}, `${g.tags.join(", ")} → `,
          h("button", { onclick: async () => {
            for (const old of g.tags.filter((tag: string) => tag !== g.suggested)) {
              await approveFlow("/api/tags/rename", { old, new: g.suggested }, t("Renaming tag \"{old}\" to \"{new}\".", { old, new: g.suggested }));
            }
          } }, t("Merge into \"{tag}\"", { tag: g.suggested })))))
          : h("p", { class: "muted" }, t("No variants of the same tag."))),
      h("section", {}, h("h2", {}, t("Possible duplicates ({n})", { n: d.duplicates.length })),
        d.duplicates.length ? h("ul", {}, ...d.duplicates.map((p: Json) => h("li", {}, `${p.titles[0]} ≈ ${p.titles[1]} `,
          h("span", { class: "muted" }, `(${p.reasons.join("; ")})`)))) : h("p", { class: "muted" }, t("No likely duplicates.")),
        d.duplicates.length ? h("p", { class: "muted" }, t("To merge them, ask Claude to \"merge X and Y\" (mnemoloci-maintain skill).")) : ""),
      h("section", {}, h("h2", {}, t("Possible syntheses ({n})", { n: d.synthesis.length })),
        d.synthesis.length ? h("ul", {}, ...d.synthesis.map((p: Json) => h("li", {}, `${p.titles[0]} + ${p.titles[1]} `,
          h("span", { class: "muted" }, t("(cited together in {n} pages)", { n: p.cited_together_in.length }))))) : h("p", { class: "muted" }, t("None.")),
        d.synthesis.length ? h("p", { class: "muted" }, t("For a comparison page, ask Claude to \"write a synthesis of X and Y\".")) : ""));
  },

  async weekly(root) {
    const w = await get<Json>("/api/weekly");
    const plan = h("textarea", { class: "clip-text", placeholder: t("The plan for next week (3-5 priorities)"), "aria-label": t("Plan") }) as HTMLTextAreaElement;
    const save = async () => {
      try { notice(t("Plan saved to {path}", { path: (await post<Json>("/api/weekly/save", { plan: plan.value })).path }), "ok"); }
      catch (err) { notice(explain(err)); }
    };
    root.append(h("h1", {}, t("Week {week}", { week: w.week })), h("p", { class: "muted" }, `${w.from} → ${w.to}`),
      h("section", {}, h("h2", {}, t("Goals")), h("div", {}, ...w.goals.map((g: Json) => h("div", { class: `todo${g.moved ? "" : " late"}` },
        h("b", {}, g.id), h("span", {}, g.text),
        h("span", { class: `kind ${g.moved ? "ok" : "warn"}` }, g.moved ? t("moved forward") : t("stalled")),
        g.no_next_step ? h("span", { class: "kind warn" }, t("no next step")) : "")))),
      w.open_questions.length ? h("section", {}, h("h2", {}, t("Open questions")),
        h("ul", {}, ...w.open_questions.map((q: Json) => h("li", {}, h("b", {}, `${q.session}: `), q.question)))) : "",
      h("section", {}, h("h2", {}, t("Plan")),
        h("p", { class: "muted" }, t("For a guided review, ask Claude: \"let's do the weekly review\". Or write the plan here.")),
        plan, h("button", { class: "primary", onclick: save }, t("Save the plan"))));
  },

  async inbox(root) {
    const data = await get<Json>("/api/inbox");
    const grid = h("div", { class: "grid" });
    root.append(h("h1", {}, t("Inbox")), h("p", { class: "muted" }, t("Screenshots from the phone and images. Claude transcribes them (mnemoloci-inbox skill).")), grid);
    for (const item of data.items) {
      const img = h("img", { alt: item.id }) as HTMLImageElement;
      imageUrl(item.id).then((u) => (img.src = u)).catch(() => img.replaceWith(h("div", { class: "muted" }, t("image not available"))));
      grid.append(h("div", { class: "tile", onclick: () => openPage(item.sidecar) }, img,
        h("div", {}, h("span", { class: `kind ${item.status === "transcribed" ? "ok" : "warn"}` },
          item.status === "transcribed" ? t("transcribed") : t("to transcribe"))),
        h("div", { class: "muted" }, (item.received ?? "").slice(0, 16).replace("T", " "))));
    }
    if (!data.items.length) grid.append(h("p", { class: "muted" }, t("No images.")));
    await loadSlugs();
  },
};

views.sources = sourcesView;

function setActive(view: string) {
  document.querySelectorAll<HTMLAnchorElement>("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
}

async function refreshBadges() {
  try {
    const s = await get<Json>("/api/status");
    const toReview = s.pending_review + (s.instincts_proposed ?? 0);
    document.getElementById("badge-review")!.textContent = toReview ? String(toReview) : "";
    document.getElementById("badge-memory")!.textContent = s.memory_auto_pending ? String(s.memory_auto_pending) : "";
    document.getElementById("badge-inbox")!.textContent = s.inbox_pending ? String(s.inbox_pending) : "";
    document.getElementById("badge-today")!.textContent = s.todos_overdue ? String(s.todos_overdue) : "";
    const upkeep = (s.counts?.broken_links ?? 0) + (s.counts?.tag_variants ?? 0);
    document.getElementById("badge-maintain")!.textContent = upkeep ? String(upkeep) : "";
    document.getElementById("badge-clips")!.textContent = s.clips_to_compile ? String(s.clips_to_compile) : "";
    document.getElementById("badge-answers")!.textContent = s.stale_answers ? String(s.stale_answers) : "";
  } catch { /* shown by the active view */ }
}

async function render(view: string) {
  if (!leaveOk()) return;
  graphInstance?._destructor?.();
  graphInstance = null;
  setActive(view);
  rerender = () => render(view);
  const root = h("div", { class: "view" });
  const loading = h("p", { class: "muted loading" }, t("Loading…"));
  root.append(loading);  // some views gather data for a second or two: never show a blank page
  main.replaceChildren(root);
  try {
    await views[view](root);
    loading.remove();
  } catch (err) {
    loading.remove();
    if (err instanceof ApiError && err.code === "UNAUTHORIZED" && usingDeviceToken()) {
      forgetDevice();
      pairingScreen(true);
      return;
    }
    root.append(h("p", { class: "error" }, explain(err)));
  }
  refreshBadges();
}

// ---------- interface language ----------
// The menu labels live in index.html as Italian text followed (sometimes) by a badge: only the text node changes.
function translateNav() {
  document.querySelectorAll<HTMLAnchorElement>("#nav a").forEach((a) => {
    const node = Array.from(a.childNodes).find((n) => n.nodeType === Node.TEXT_NODE && (n.nodeValue ?? "").trim());
    if (!node) return;
    if (!a.dataset.label) a.dataset.label = node.nodeValue!.trim();
    node.nodeValue = t(a.dataset.label) + (/\s$/.test(node.nodeValue!) ? " " : "");
  });
  document.querySelectorAll<HTMLSelectElement>("select.lang-select, #lang-select").forEach((s) => {
    s.setAttribute("aria-label", t("Language"));
    s.value = lang;
  });
}

function changeLang(next: string) {
  if (next !== "it" && next !== "en") return;
  setLang(next as Lang);
  translateNav();
  if (!dirty) rerender();  // with unsaved edits the editor stays as it is; the new language applies on the next screen
}

function langSelect(): HTMLSelectElement {
  const s = h("select", { class: "lang-select", "aria-label": t("Language"), onchange: (e: Event) => changeLang((e.target as HTMLSelectElement).value) },
    h("option", { value: "it", lang: "it" }, "Italiano"), h("option", { value: "en", lang: "en" }, "English")) as HTMLSelectElement;
  s.value = lang;
  return s;
}

const navLang = document.getElementById("lang-select") as HTMLSelectElement | null;
navLang?.addEventListener("change", () => changeLang(navLang.value));
translateNav();

document.querySelectorAll<HTMLAnchorElement>("#nav a").forEach((a) =>
  a.addEventListener("click", () => { location.hash = a.dataset.view!; }));
let lastHash = location.hash;
window.addEventListener("hashchange", () => {
  const raw = location.hash.slice(1);
  const v = raw.includes("=") ? new URLSearchParams(raw).get("view") ?? "" : raw;
  if (!leaveOk()) {
    // Put the previous hash back, otherwise clicking the same menu item again changes nothing.
    history.replaceState(null, "", location.pathname + lastHash);
    return;
  }
  if (raw.includes("token=")) history.replaceState(null, "", location.pathname + (v ? `#${v}` : ""));
  lastHash = location.hash;
  if (views[v]) render(v);
});

function pairingScreen(revoked = false) {
  rerender = () => pairingScreen(revoked);
  document.getElementById("nav")!.style.display = "none";
  const code = h("input", { placeholder: t("Code (e.g. ABCD-EF23)"), autocapitalize: "characters", autocomplete: "off",
    spellcheck: "false", class: "pair-code", "aria-label": t("Pairing code") }) as HTMLInputElement;
  const name = h("input", { value: /iPhone/.test(navigator.userAgent) ? "iPhone" : t("phone"), "aria-label": t("Device name") }) as HTMLInputElement;
  const error = h("p", { class: "error" }, revoked ? t("This device is no longer authorized (revoked from the PC). Pair it again.") : "");
  const submit = async () => {
    try {
      await claimDevice(code.value, name.value);
      location.reload();
    } catch (err) {
      error.textContent = err instanceof ApiError && err.code === "HTTP_404"
        ? t("This server doesn't accept pairing: on the PC run `python -P -m mnemoloci remote serve`.")
        : err instanceof ApiError && err.code === "WRONG_CODE" ? t("Wrong code. After 5 mistakes the code is cancelled.")
        : err instanceof ApiError && err.code === "NO_PAIRING_CODE" ? t("No valid code: generate a new one on the PC.")
        : explain(err);
    }
  };
  code.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  main.replaceChildren(h("div", { class: "pairing" },
    h("h1", {}, "Mnemoloci"),
    h("p", {}, t("Pair this device with your second brain.")),
    h("ol", {},
      h("li", {}, t("On the PC: Mnemoloci app → Status → \"Generate pairing code\", or python -P -m mnemoloci remote pair")),
      h("li", {}, t("Enter the code here (valid for 10 minutes, once only)."))),
    h("label", {}, t("Code"), code), h("label", {}, t("Device name"), name),
    h("button", { class: "primary", onclick: submit }, t("Pair")), error,
    h("div", { class: "lang-switch" }, langSelect())));
  code.focus();
}

if (!hasToken()) {
  pairingScreen();
} else {
  render(views[initialView] ? initialView : "today");
}
