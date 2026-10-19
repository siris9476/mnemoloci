// Two kinds of credentials:
// - on the PC, a per-launch token arrives in the URL fragment (never sent to servers)
//   and lives only in this tab (sessionStorage);
// - a paired phone keeps its long-lived device token in localStorage.
import { t } from "./i18n";

const TOKEN_KEY ="mnemoloci-token";
const DEVICE_KEY = "mnemoloci-device-token";
const fragment = new URLSearchParams(location.hash.slice(1));
const fromHash = fragment.get("token");
export const initialView = fromHash ? fragment.get("view") ?? "" : location.hash.slice(1);
const store = (area: Storage, key: string, value?: string | null) => {
  try {
    if (value === undefined) return area.getItem(key);
    if (value === null) area.removeItem(key);
    else area.setItem(key, value);
  } catch { /* storage blocked: the app still works for this page load */ }
  return null;
};
if (fromHash) {
  store(sessionStorage, TOKEN_KEY, fromHash);
  history.replaceState(null, "", location.pathname + (initialView ? `#${initialView}` : ""));
}
const sessionToken = store(sessionStorage, TOKEN_KEY) ?? "";
const deviceToken = store(localStorage, DEVICE_KEY) ?? "";
const token = sessionToken || deviceToken;
export const usingDeviceToken = () => !sessionToken && !!deviceToken;
export const forgetDevice = () => store(localStorage, DEVICE_KEY, null);

export async function claimDevice(code: string, name: string): Promise<void> {
  const res = await fetch("/api/remote/claim", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, name }),
  });
  const data = await res.json().catch(() => ({ ok: false, code: `HTTP_${res.status}` }));
  if (!data.ok) throw new ApiError(data.code, data.message ?? data.code);
  store(localStorage, DEVICE_KEY, data.token);
}

export class ApiError extends Error {
  constructor(public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, ...(init.body ? { "Content-Type": "application/json" } : {}) },
  });
  const data = await res.json().catch(() => ({ ok: false, code: `HTTP_${res.status}`, message: res.statusText }));
  if (!data.ok) throw new ApiError(data.code, data.message ?? data.code, data.details);
  return data as T;
}

export const get = <T>(path: string, params: Record<string, string | number> = {}) => {
  const qs = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
  return call<T>(qs.toString() ? `${path}?${qs}` : path);
};

export const post = <T>(path: string, body: unknown) => call<T>(path, { method: "POST", body: JSON.stringify(body) });

export async function imageUrl(id: string): Promise<string> {
  const res = await fetch(`/api/inbox/${encodeURIComponent(id)}/image`, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new ApiError(`HTTP_${res.status}`, t("image not available"));
  return URL.createObjectURL(await res.blob());
}

export const hasToken = () => token.length > 0;
