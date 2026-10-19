// The "Funzionalità" page: everything Mnemoloci does, where in the app each thing is, and the command
// for what is still terminal-only. The list comes from the engine (/api/features), so it cannot drift
// from the command line: a test fails when a command is missing from the catalogue.
import { get } from "./api";
import { t } from "./i18n";
import type { Ctx } from "./settings-view";

type Json = Record<string, any>;

const LEVEL_LABELS: Json = {
  full: "In the app", partial: "Partly in the app", none: "Terminal only", internal: "Handled by the plugin",
};

export async function featuresView(root: HTMLElement, { h, notice }: Ctx): Promise<void> {
  const data = await get<Json>("/api/features");
  const s = data.summary as Json;
  const search = h("input", { type: "search", placeholder: t("Search a feature…"), "aria-label": t("Search a feature") }) as HTMLInputElement;
  const list = h("div", { class: "features" });

  const card = (f: Json) => {
    const cmds = (f.commands as string[]).map((c) => `mnemoloci ${c}`);
    const actions: Node[] = [];
    if (f.view) {
      const open = h("button", { class: "primary" }, t("Open")) as HTMLButtonElement;
      open.onclick = () => { location.hash = f.view; };
      actions.push(open);
    }
    const copy = h("button", {}, t("Copy the command")) as HTMLButtonElement;
    copy.onclick = () => { navigator.clipboard?.writeText(cmds[0] + " --help"); notice(t("Command copied:\n{command}", { command: cmds[0] + " --help" }), "ok"); };
    if (f.level !== "internal") actions.push(copy);
    return h("article", { class: `feature level-${f.level}`, "data-id": f.id },
      h("div", { class: "feature-head" }, h("h3", {}, t(f.title)), h("span", { class: `level level-${f.level}` }, t(LEVEL_LABELS[f.level]))),
      h("p", {}, t(f.description)),
      h("p", { class: "muted" }, ...cmds.map((c, i) => [i ? " · " : "", h("code", {}, c)]).flat()),
      h("div", { class: "actions" }, ...actions));
  };

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const groups = (data.categories as Json[]).map((c) => {
      const items = (data.features as Json[]).filter((f) => f.category === c.id).filter((f) =>
        !q || [t(f.title), t(f.description), ...f.commands].join(" ").toLowerCase().includes(q));
      return items.length ? h("section", { class: "feature-group" }, h("h2", {}, t(c.title)), ...items.map(card)) : null;
    }).filter(Boolean) as Node[];
    list.replaceChildren(...(groups.length ? groups : [h("p", { class: "muted" }, t("No feature matches."))]));
  };
  search.oninput = draw;

  root.append(h("h1", {}, t("Features")),
    h("p", { class: "muted" }, t("Everything Mnemoloci can do, and where to find it. {total} features: {full} complete in the app, {partial} partly, {none} terminal only.",
      { total: s.total, full: s.full, partial: s.partial, none: s.none })),
    search, list);
  draw();
}
