// Shared by popup, options and background. The device token is stored only in
// chrome.storage.local (this browser profile); it can be revoked from the Mnemoloci app.
export const DEFAULT_SERVER = "http://127.0.0.1:8790";
export const MAX_TEXT = 200000;

/** A string from _locales (English by default, Italian when the browser is Italian). */
export const msg = (key, ...subs) => chrome.i18n.getMessage(key, subs) || key;

/** Fill every [data-i18n], [data-i18n-placeholder] and [data-i18n-aria] element of a page. */
export function applyI18n() {
  document.documentElement.lang = chrome.i18n.getUILanguage().slice(0, 2);
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = msg(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = msg(el.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", msg(el.dataset.i18nAria)); });
  const title = document.querySelector("title[data-i18n]");
  if (title) document.title = msg(title.dataset.i18n);
}

export async function config() {
  const { server = DEFAULT_SERVER, token = "" } = await chrome.storage.local.get(["server", "token"]);
  return { server: server.replace(/\/+$/, ""), token };
}

export async function call(path, body) {
  const { server, token } = await config();
  if (!token) throw Object.assign(new Error(msg("errNotPairedShort")), { code: "NOT_PAIRED" });
  const res = await fetch(server + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ ok: false, code: `HTTP_${res.status}` }));
  if (!data.ok) throw Object.assign(new Error(data.message || data.code), { code: data.code });
  return data;
}

export async function pair(server, code, name) {
  const res = await fetch(server.replace(/\/+$/, "") + "/api/remote/claim", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, name }),
  });
  const data = await res.json().catch(() => ({ ok: false, code: `HTTP_${res.status}` }));
  if (!data.ok) throw Object.assign(new Error(data.message || data.code), { code: data.code });
  await chrome.storage.local.set({ server: server.replace(/\/+$/, ""), token: data.token });
  return data;
}

// What the user is looking at: the page's readable text, as rendered (works behind logins).
export async function pageText(tabId) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (max) => {
      const main = document.querySelector("article, main") || document.body;
      return { title: document.title, text: (main.innerText || "").slice(0, max), url: location.href,
               selection: String(getSelection() || "").slice(0, max) };
    },
    args: [MAX_TEXT],
  });
  return result;
}

export const FRIENDLY = {
  get NOT_PAIRED() { return msg("errNotPaired"); },
  get UNAUTHORIZED() { return msg("errUnauthorized"); },
  get PRIVATE_ADDRESS() { return msg("errPrivateAddress"); },
  get FETCH_FAILED() { return msg("errFetchFailed"); },
  get INJECTION_SUSPECT() { return msg("errInjection"); },
};
