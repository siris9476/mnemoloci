import { applyI18n, config, msg, pair } from "./common.js";

const $ = (id) => document.getElementById(id);
const status = (text, kind = "") => { $("status").textContent = text; $("status").className = kind; };

(async () => {
  applyI18n();
  const { server, token } = await config();
  $("server").value = server;
  $("name").value = /Edg\//.test(navigator.userAgent) ? "Edge" : "Chrome";
  status(token ? msg("paired") : msg("notPaired"), token ? "ok" : "");
  $("pair").addEventListener("click", async () => {
    try {
      await pair($("server").value.trim(), $("code").value.trim(), $("name").value.trim() || "browser");
      status(msg("pairedNow"), "ok");
    } catch (err) {
      status(err.code === "HTTP_404" ? msg("serverNoPairing")
        : err.code === "WRONG_CODE" ? msg("wrongCode") : err.code === "NO_PAIRING_CODE" ? msg("noValidCode")
        : err.message, "error");
    }
  });
  $("forget").addEventListener("click", async () => {
    await chrome.storage.local.remove("token");
    status(msg("disconnected"), "ok");
  });
})();
