---
name: mnemoloci-sessions
description: Build initial knowledge from the user's existing Claude Code session history — decisions, preferences, project context, lessons, and facts about the user. Use when the user asks to read, learn from, or import their past Claude sessions/conversations into the second brain.
---

# Learn from past Claude Code sessions

## 1. Import

Run the import. It is deterministic and needs no LLM:

```
mnemoloci sessions import [--project TEXT]… [--exclude TEXT]… [--since YYYY-MM-DD] [--dry-run]
```

**Always start with `--dry-run`** and show the user the counts:
- `imported`: sessions with content;
- `memory_files`: Claude's own memory notes, already summarised;
- `pruned_sessions_from_history`: sessions only recoverable as prompt history;
- `files_to_write`.

Ask which projects to include or exclude. Transcripts can contain private matters, and the user decides. Then run it for real.

The import writes redacted Markdown digests under `raw/sessions/<project>/<date>-<id>/part-NNN.md`:
- it keeps only what the user typed and Claude's text replies;
- it drops tool output, thinking, subagents, notifications, slash-command noise and IDE context;
- secrets are replaced by `[REDACTED:…]`;
- re-running is incremental, and a growing session only rewrites its last part.

## 2. Compile

Compile the digests with the normal loop from `mnemoloci-ingest`, using `--source claude-sessions`:

```
mnemoloci compile plan --source claude-sessions --limit 10
```

Memory files (`…/memory/*.md`) sort first because they are the densest.

## Extraction rules for session digests

These replace the generic rules.

**What to extract**
- Durable knowledge only: decisions and why they were taken, technical findings that were verified, project architecture, conventions, recurring problems and their fixes, tools and services used (entities).
- Chit-chat, transient debugging, and anything later contradicted in the same session.

**Claims and confidence**
- Claims cite the digest's line numbers, like any source.
- Something Claude said but never verified is `inferred`, with confidence ≤ 0.6.
- Something the user stated or confirmed can be `extracted` with high confidence.

## ChatGPT and claude.ai conversations

The user can download their history as an export:
- **ChatGPT:** Impostazioni → Controlli dati → Esporta dati.
- **claude.ai:** Settings → Privacy → Export data.

Both arrive by email as a zip. Import it without unpacking:

```
mnemoloci import chats "<export.zip or conversations.json>" --dry-run
mnemoloci import chats "<…>" [--include TEXT]… [--exclude TEXT]… [--since YYYY-MM-DD]
```

1. Start with `--dry-run`. Show the number of conversations and messages, and ask which titles to include or exclude. These are private conversations.
2. For ChatGPT, only the branch the user saw is imported: abandoned regenerations and edits are left out, and so are tool output and hidden system messages. Secrets are redacted.
3. The digests land in `raw/chats/<chatgpt|claude-ai>/`. They are searchable at once with `sessions search`. Compile them like sessions with `--source chats`, following the same extraction rules below.

## OpenAI Codex sessions

```
mnemoloci import codex [--since YYYY-MM-DD] [--dry-run]
```

This reads `~/.codex/sessions` (or `$CODEX_HOME`). Only what the user typed and Codex's replies are kept: the automatic `<environment_context>` blocks, reasoning and tool calls are left out. Digests land in `raw/codex/`, are searchable at once, and are compiled with `--source codex-sessions`.

## Personal information

Do **not** put facts about the user or their goals into wiki pages. Collect them separately while extracting:
- profile candidates: role, skills, tools, preferences, working style;
- goal candidates: things the user says they want to achieve.

At the end of the run, show the user the list and ask which to save. Save only confirmed ones, with `mnemoloci-remember` (`profile set`, `goals add`).

Never save identifiers such as addresses, phone numbers, IDs, bank details or health data; the engine rejects the worst ones anyway.
