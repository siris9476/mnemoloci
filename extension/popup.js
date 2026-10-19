import { FRIENDLY, applyI18n, call, config, msg, pageText } from "./common.js";

const $ = (id) => document.getElementById(id);
const status = (text, kind = "") => { $("status").textContent = text; $("status").className = kind; };

async function currentTab() {
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active && /^https?:/.test(active.url || "")) return active;  // an extension page, or a page whose address is hidden, is not the page to save
  // opened as a tab (e.g. in tests): use the most recently used web page of this window
  const tabs = (await chrome.tabs.query({ currentWindow: true })).filter((t) => /^https?:/.test(t.url || ""));
  return tabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
}

function saved(res) {
  if (res.duplicate) return status(msg("alreadySaved"), "ok");
  const flag = res.injection && res.injection.verdict !== "safe" ? msg("injectionWarning") : "";
  status(msg("savedSummary", res.title, res.chars.toLocaleString(chrome.i18n.getUILanguage()), flag), "ok");
}

async function savePage() {
  const tab = await currentTab();
  const extra = { note: $("note").value, mine: $("mine").checked };
  status(msg("saving"));
  try {
    saved(await call("/api/clips/url", { url: tab.url, ...extra }));
  } catch (err) {
    if (err.code === "FETCH_FAILED" || err.code === "PRIVATE_ADDRESS" || err.code === "UNSUPPORTED_TYPE") {
      status(FRIENDLY[err.code] || msg("savingVisibleText"));
      const page = await pageText(tab.id);  // what the user sees, also behind a login
      try { saved(await call("/api/clips/text", { title: page.title, text: page.text, url: page.url, ...extra })); }
      catch (err2) { status(FRIENDLY[err2.code] || err2.message, "error"); }
    } else status(FRIENDLY[err.code] || err.message, "error");
  }
}

async function saveSelection() {
  const tab = await currentTab();
  const page = await pageText(tab.id);
  if (!page.selection.trim()) return status(msg("selectTextFirst"), "error");
  status(msg("saving"));
  try {
    saved(await call("/api/clips/text", { title: `${page.title} (${msg("selectionSuffix")})`, text: page.selection, url: page.url,
                                           note: $("note").value, mine: $("mine").checked }));
  } catch (err) { status(FRIENDLY[err.code] || err.message, "error"); }
}

(async () => {
  applyI18n();
  const tab = await currentTab();
  $("page").textContent = tab ? tab.title : msg("noPage");
  const { token } = await config();
  if (!token) status(FRIENDLY.NOT_PAIRED, "error");
  $("save-page").addEventListener("click", savePage);
  $("save-selection").addEventListener("click", saveSelection);
})();
