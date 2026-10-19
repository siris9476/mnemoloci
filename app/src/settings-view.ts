// The "Impostazioni" page: every setting of the vault in one place. Each card reads its section from
// /api/settings and saves it on its own, so a mistake in one never blocks another.
import { get, post } from "./api";
import { t } from "./i18n";

type Json = Record<string, any>;
type H = (tag: any, attrs?: Json, ...children: (Node | string | null)[]) => HTMLElement;
export type Ctx = { h: H; notice: (text: string, kind?: "error" | "ok") => void; explain: (err: unknown) => string };

// Italian labels; t() translates them where they are shown.
const HOLD_LABELS: Json = {
  "low-confidence": "Low confidence", contradicted: "Contradicts other pages", "schema-violating": "Incomplete format",
  "provenance-violating": "Invalid citations", "external-source": "External source (screenshot or import)",
  "single-source": "Critical page with a single source",
};
const STEP_LABELS: Json = {
  sources: "Sources", connectors: "Connectors", sessions: "Sessions", skills_usage: "Skill usage", digest: "Digest",
  quality: "Quality", backup: "Backup", verify: "Verification",
};

export async function settingsView(root: HTMLElement, { h, notice, explain }: Ctx): Promise<void> {
  const data = await get<Json>("/api/settings");

  /** A card with a save button: `collect` returns the section's payload, `after` redraws on success. */
  const card = (title: string, help: string, section: string, fields: Node[], collect: () => Json,
                extra: Node[] = []) => {
    const save = h("button", { class: "primary" }, t("Save")) as HTMLButtonElement;
    save.onclick = async () => {
      save.disabled = true;
      save.textContent = t("Saving…");
      try {
        await post<Json>(`/api/settings/${section}`, collect());
        notice(t("Settings saved."), "ok");
      } catch (err) {
        notice(explain(err));
      } finally {
        save.disabled = false;
        save.textContent = t("Save");
      }
    };
    const box = h("section", { class: "settings-card", "data-section": section }, h("h2", {}, t(title)),
      h("p", { class: "muted" }, t(help)), ...fields, h("div", { class: "actions" }, save, ...extra));
    root.append(box);
    return box;
  };
  const field = (label: string, input: Node, hint = "") =>
    h("label", { class: "settings-field" }, h("span", {}, t(label)), input, hint ? h("span", { class: "muted" }, t(hint)) : null);
  const check = (checked: boolean, disabled = false) =>
    h("input", { type: "checkbox", ...(checked ? { checked: "" } : {}), ...(disabled ? { disabled: "" } : {}) }) as HTMLInputElement;
  const num = (value: number, min: number, max: number, step: number) =>
    h("input", { type: "number", value: String(value), min: String(min), max: String(max), step: String(step) }) as HTMLInputElement;

  root.append(h("h1", {}, t("Settings")),
    h("p", { class: "muted" }, t("Everything you can adjust in Mnemoloci. Each card saves on its own; the same settings are available from the command line.")));

  // ---- language ----
  const lg = data.language;
  const target = h("input", { value: lg.target, maxlength: "5", list: "lang-codes", "aria-label": t("Language of the pages") }) as HTMLInputElement;
  const keep = check(lg.keep_original);
  card("Language of the pages", "The language the wiki is written in. Sources in another language are translated; the original can stay in a collapsible block at the bottom of the page.",
    "language", [
      h("datalist", { id: "lang-codes" }, ...["it", "en", "es", "fr", "de", "pt"].map((c) => h("option", { value: c }))),
      field("Language code (e.g. it, en, es)", target),
      h("label", { class: "settings-field inline" }, keep, h("span", {}, t("Keep the original text below the translation")))],
    () => ({ target: target.value.trim().toLowerCase(), keep_original: keep.checked }));

  // ---- writing guidelines ----
  const soul = h("textarea", { rows: "7", "aria-label": t("Writing guidelines") }) as HTMLTextAreaElement;
  soul.value = data.instructions.text;
  const counter = h("span", { class: "muted" }, "");
  const count = () => {
    const n = soul.value.length, limit = data.instructions.used_chars;
    counter.textContent = n > limit ? t("{n} characters: anything beyond {limit} is not passed to Claude.", { n, limit })
                                    : t("{n} / {limit} characters", { n, limit });
  };
  soul.oninput = count;
  count();
  card("Writing guidelines", "Your style rules for the wiki: tone, what to emphasise, what to leave out. Claude reads them on every compile. They do not replace the citation rules.",
    "instructions", [soul, counter], () => ({ text: soul.value }));

  // ---- review rules ----
  const rv = data.review;
  const holds = (rv.choices as string[]).map((k) => ({ k, box: check((rv.hold as string[]).includes(k)) }));
  const threshold = num(rv.low_confidence_threshold, 0, 1, 0.05);
  const missing = h("select", {}, h("option", { value: "hold" }, t("Hold for review")),
    h("option", { value: "ok" }, t("Publish anyway"))) as HTMLSelectElement;
  missing.value = rv.treat_missing_confidence_as;
  card("When a page goes to review", "Compiled pages that match one of these cases wait for your approval instead of being published.",
    "review", [
      ...holds.map(({ k, box }) => h("label", { class: "settings-field inline" }, box, h("span", {}, t(HOLD_LABELS[k] ?? k)))),
      h("p", { class: "muted" }, t("Always held, not configurable: {cases}.", { cases: Object.values(rv.always as Json).map((x) => t(String(x))).join("; ") })),
      field("Low-confidence threshold (0 – 1)", threshold, "Below this value, and if “Low confidence” is on, the page goes to review."),
      field("Page with no confidence stated", missing)],
    () => ({ hold: holds.filter((x) => x.box.checked).map((x) => x.k), low_confidence_threshold: Number(threshold.value),
             treat_missing_confidence_as: missing.value }));

  // ---- reading sources ----
  const kb = num(Math.round(data.ingest.max_file_bytes / 1024), Math.ceil(data.ingest.min / 1024), Math.floor(data.ingest.max / 1024), 1);
  card("Reading sources", "Files larger than this limit are not read when sources are compiled.",
    "ingest", [field("Maximum size of a file (KB)", kb)], () => ({ max_file_bytes: Math.round(Number(kb.value) * 1024) }));

  // ---- backup ----
  const bk = data.backup;
  const commit = check(bk.auto_commit, !bk.enabled), push = check(bk.auto_push, !bk.enabled || !bk.remote);
  const last = bk.last_commit ? t("Last backup: {at}", { at: String(bk.last_commit.at).replace("T", " ").slice(0, 16) }) : t("No backup yet.");
  const now = h("button", {}, t("Back up now")) as HTMLButtonElement;
  now.onclick = async () => {
    now.disabled = true;
    try { await post<Json>("/api/backup/now", {}); notice(t("Backup done."), "ok"); } catch (err) { notice(explain(err)); }
    now.disabled = false;
  };
  card("Backup", bk.enabled ? "The vault is versioned with git: every version of every page stays recoverable." : "Backup is not active yet. Start it from the terminal with `mnemoloci backup init`, then come back here.",
    "backup", [
      h("label", { class: "settings-field inline" }, commit, h("span", {}, t("Save a version at the end of a session"))),
      h("label", { class: "settings-field inline" }, push, h("span", {}, t("Push automatically to the remote repository"))),
      h("p", { class: "muted" }, bk.enabled ? `${last} · ${t("{n} unsaved changes", { n: bk.uncommitted_changes ?? 0 })}${bk.remote ? ` · ${t("remoto")}: ${bk.remote}` : ` · ${t("no remote")}`}` : "")],
    () => ({ auto_commit: commit.checked, auto_push: push.checked }), bk.enabled ? [now] : []);

  // ---- reminder ----
  const every = num(data.reflection.every, 0, data.reflection.max, 1);
  card("Reminder to save", "Every so many of your messages Claude reminds you to save decisions, lessons and todos. 0 turns it off.",
    "reflection", [field("Every how many messages (0 = never)", every)], () => ({ every: Number(every.value) }));

  // ---- ask the vault (reason) ----
  const budget = num(data.reason.maxBudgetUsd, 0.01, data.reason.max, 0.05);
  card("Questions to the vault (`reason`)", "The `mnemoloci reason` command has Claude answer from the vault, read-only. It spends tokens: this is the spending cap for each question, in dollars.",
    "reason", [field("Spending cap per question (USD)", budget)], () => ({ maxBudgetUsd: Number(budget.value) }));

  // ---- nightly job (asks the operating system: loaded after the rest is on screen) ----
  const sched = h("section", { class: "settings-card", "data-section": "schedule" }, h("h2", {}, t("Nightly job")),
    h("p", { class: "muted" }, t("Checking…")));
  root.append(sched);
  try {
    const s = await get<Json>("/api/settings/schedule");
    const on = check(!!s.enabled);
    const at = h("input", { type: "time", value: "21:30", "aria-label": t("Time") }) as HTMLInputElement;
    const save = h("button", { class: "primary" }, t("Save")) as HTMLButtonElement;
    save.onclick = async () => {
      save.disabled = true;
      try { await post<Json>("/api/settings/schedule", { enabled: on.checked, time: at.value }); notice(t("Settings saved."), "ok"); }
      catch (err) { notice(explain(err)); }
      save.disabled = false;
    };
    const steps = (s.last_run?.steps ?? {}) as Json;
    const rows = Object.entries(steps).map(([name, res]) => {
      const r = res as Json;
      const text = r.error ? `${t("errore")}: ${r.error}` : name === "verify"
        ? `${(r.issues ?? []).length ? (r.issues as string[]).join("; ") : t("all fine")}${r.weak_citations !== undefined ? ` · ${t("{n} weak citations", { n: r.weak_citations })}${r.weak_citations_change ? ` (${r.weak_citations_change > 0 ? "+" : ""}${r.weak_citations_change})` : ""}` : ""}`
        : r.skipped ? String(r.skipped) : "ok";
      return h("tr", {}, h("td", {}, t(STEP_LABELS[name] ?? name)), h("td", { class: r.error ? "error" : "" }, text));
    });
    sched.replaceChildren(h("h2", {}, t("Nightly job")),
      h("p", { class: "muted" }, t("Every day at the chosen time: session index, digest, quality, backup and verification. It never calls a model and never edits pages.")),
      h("label", { class: "settings-field inline" }, on, h("span", {}, t("On"))),
      field("Time", at),
      h("div", { class: "actions" }, save),
      s.last_run ? h("div", {}, h("h3", {}, t("Last run: {at}", { at: String(s.last_run.at).replace("T", " ").slice(0, 16) })), h("table", {}, ...rows))
                 : h("p", { class: "muted" }, t("It has not run yet.")));
  } catch (err) {
    sched.replaceChildren(h("h2", {}, t("Nightly job")), h("p", { class: "error" }, explain(err)));
  }
}
