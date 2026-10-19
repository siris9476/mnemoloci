import { FRIENDLY, call, msg } from "./common.js";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "save-selection", title: msg("menuSaveSelection"), contexts: ["selection"] });
  chrome.contextMenus.create({ id: "save-page", title: msg("menuSavePage"), contexts: ["page"] });
});

function badge(text, color) {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === "save-selection" && info.selectionText) {
      await call("/api/clips/text", { title: `${tab.title} (${msg("selectionSuffix")})`, text: info.selectionText, url: tab.url });
    } else {
      await call("/api/clips/url", { url: tab.url });
    }
    badge("OK", "#15803d");
  } catch (err) {
    console.warn("Mnemoloci:", FRIENDLY[err.code] || err.message);
    badge("!", "#c2410c");
  }
});
