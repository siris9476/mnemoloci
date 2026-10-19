// The "Strumenti" page: everything you run on demand. The forms are built from the specs the engine
// returns (/api/tools), so a new tool appears here without new interface code.
import { ApiError, get, post } from "./api";
import { t } from "./i18n";
import { renderMarkdown } from "./markdown";
import type { Ctx } from "./settings-view";

type Json = Record<string, any>;
const MAX_SHOWN = 12_000;

export async function toolsView(root: HTMLElement, { h, notice, explain }: Ctx): Promise<void> {
  const data = await get<Json>("/api/tools");
  root.append(h("h1", {}, t("Tools")),
    h("p", { class: "muted" }, t("What you run when needed: questions to the vault, checks, exports and imports. Actions that write files or spend tokens say so first.")));

  const table = (head: string[], rows: (string | number)[][]) =>
    h("table", {}, h("tr", {}, ...head.map((x) => h("th", {}, t(x)))), ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, String(c))))));

  /** Readable results for the tools whose output has a clear shape; everything else shows its data. */
  const show = (tool: string, r: Json): HTMLElement => {
    if (tool === "reason") {
      const answer = h("div", { class: "answer markdown" });
      answer.innerHTML = renderMarkdown(String(r.answer ?? ""));  // sanitised by renderMarkdown
      return h("div", {}, answer,
        h("p", { class: "muted" }, t("Cost: {cost} USD · {turns} turns · {seconds} s", { cost: r.cost_usd ?? "?", turns: r.turns ?? "?", seconds: r.seconds ?? "?" })));
    }
    if (tool === "check-citations") {
      return h("div", {}, h("p", {}, t("Weak citations: {weak} out of {total} checked ({unchecked} not verifiable).", { weak: r.weak, total: r.citations, unchecked: r.unchecked })),
        (r.items as Json[]).length ? table(["Page", "Citation", "Support", "Not found"],
          (r.items as Json[]).map((i) => [i.page, i.citation, i.support, (i.not_found as string[]).join(", ")])) : h("p", { class: "muted" }, t("No weak citations.")));
    }
    if (tool === "reason-history") {
      return (r.runs as Json[]).length ? h("div", {}, table(["When", "Question", "Cost (USD)", "Outcome"],
        (r.runs as Json[]).map((x) => [String(x.at).replace("T", " ").slice(0, 16), x.question, x.cost_usd ?? "", x.ok ? "ok" : (x.error ?? "errore")])),
        h("p", { class: "muted" }, t("Total spent: {total} USD", { total: r.total_cost_usd })))
        : h("p", { class: "muted" }, t("No questions yet."));
    }
    if (tool === "context-cost") {
      return r.injected ? h("div", {}, h("p", {}, t("{used} / {limit} characters injected.", { used: r.used, limit: r.limit })),
        table(["Section", "Characters", "Status"], (r.sections as Json[]).map((x) => [x.name, x.chars, x.degraded ? t("reduced to a pointer") : x.truncated ? t("shortened") : "ok"])))
        : h("p", { class: "muted" }, t("Nothing to inject for this folder."));
    }
    if (tool === "context") {
      const box = h("div", { class: "answer markdown" });
      box.innerHTML = renderMarkdown(String(r.markdown ?? "").slice(0, MAX_SHOWN));
      return box;
    }
    const text = JSON.stringify(r, null, 2);
    return h("pre", {}, text.length > MAX_SHOWN ? `${text.slice(0, MAX_SHOWN)}\n… ${t("(result shortened)")}` : text);
  };

  const card = (spec: Json) => {
    const inputs: { field: Json; el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement }[] = [];
    const rows = (spec.fields as Json[]).map((f) => {
      let el: any;
      if (f.type === "textarea") el = h("textarea", { rows: "4" });
      else if (f.type === "select") el = h("select", {}, ...(f.choices as string[]).map((c) => h("option", { value: c }, c)));
      else if (f.type === "bool") el = h("input", { type: "checkbox", ...(f.default ? { checked: "" } : {}) });
      else if (f.type === "int" || f.type === "float")
        el = h("input", { type: "number", ...(f.min != null ? { min: String(f.min) } : {}), ...(f.max != null ? { max: String(f.max) } : {}), step: f.type === "float" ? "0.05" : "1" });
      else el = h("input", { type: "text", ...(f.type === "abspath" ? { placeholder: "C:\\Users\\you\\" } : {}) });
      if (f.type !== "bool" && f.default != null) el.value = String(f.default);
      inputs.push({ field: f, el });
      return h("label", { class: f.type === "bool" ? "settings-field inline" : "settings-field" },
        ...(f.type === "bool" ? [el, h("span", {}, t(f.label))] : [h("span", {}, t(f.label) + (f.required ? " *" : "")), el]));
    });
    const confirm = spec.spends ? h("input", { type: "checkbox" }) as HTMLInputElement : null;
    const result = h("div", { class: "tool-result" });
    const run = h("button", { class: "primary" }, t("Run")) as HTMLButtonElement;
    run.onclick = async () => {
      const params: Json = {};
      for (const { field, el } of inputs) {
        const v = field.type === "bool" ? (el as HTMLInputElement).checked : el.value.trim();
        if (v !== "") params[field.name] = field.type === "int" || field.type === "float" ? Number(v) : v;
      }
      run.disabled = true;
      result.replaceChildren(h("p", { class: "muted" }, t("Running…")));
      try {
        const out = await post<Json>(`/api/tools/${spec.id}`, { params, confirm_cost: !!confirm?.checked });
        result.replaceChildren(show(spec.id, out.result as Json));
        if (spec.writes && !params.dry_run) notice(t("Done."), "ok");
      } catch (err) {
        result.replaceChildren();
        notice(err instanceof ApiError && err.code === "CONFIRM_COST" ? t("Tick the spending confirmation to run.") + "\n" + explain(err) : explain(err));
      } finally {
        run.disabled = false;
      }
    };
    const flags = [spec.spends ? h("span", { class: "level level-none" }, t("Spends tokens")) : null,
                   spec.writes ? h("span", { class: "level level-partial" }, t("Writes files")) : null].filter(Boolean) as Node[];
    return h("details", { class: "tool", "data-id": spec.id },
      h("summary", {}, h("strong", {}, t(spec.title)), ...flags, h("span", { class: "muted" }, " — " + t(spec.help))),
      ...rows,
      confirm ? h("label", { class: "settings-field inline" }, confirm, h("span", {}, t("I confirm this action spends tokens."))) : null,
      h("div", { class: "actions" }, run), result);
  };

  for (const g of data.groups as Json[]) {
    root.append(h("h2", {}, t(g.title)), ...(data.tools as Json[]).filter((x) => x.group === g.id).map(card));
  }
}
