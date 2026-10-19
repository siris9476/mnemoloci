# Changelog

## 0.16.0 — first public release

- A Markdown vault (Obsidian-compatible) compiled by Claude Code into cited wiki pages, with a deterministic,
  crash-safe Python engine: lock, atomic writes, plan approval hashing, transaction journal and recovery.
- Capture of web pages, PDFs, YouTube transcripts, files, screenshots and chat exports; review queue for
  anything from an external source.
- Working memory (decisions, lessons, preferences, handoffs), goals, to-dos, instincts and skills, injected at
  the start of each Claude Code session within a character budget.
- Desktop and web app (Tauri/Vite) and MCP server. Phone access over Tailscale and the Chrome/Edge extension are optional and still in progress.
- Search (lexical and local semantic), graph analysis, validity over time, quality score, OKF import/export.
- English by default, with Italian: a vault has a language (`language.target`), the app and the browser extension
  follow the browser language.
- The vault location is one setting for the CLI, hooks, MCP server and app (`mnemoloci vault show|set`, or the
  Vault section of the app's Status view).
