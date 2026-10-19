---
name: mnemoloci
description: Personal second brain in an Obsidian-compatible Markdown vault (default ~/Mnemoloci). Use when the user wants to build knowledge from their projects, search what they already know, review pending pages, or check vault health — and when the user says "new project X" / "create a project" (new project folder under the projects root, e.g. from the phone), "save this link/PDF/video", "where did we leave off", "what do I have to do", "summary of the week", "turn on the backup" (or their Italian equivalents "nuovo progetto X", "crea un progetto", "salva questo link/PDF/video", "dove eravamo rimasti", "cosa ho da fare", "riepilogo della settimana", "attiva il backup"), or wants to import ChatGPT/claude.ai chats. Routes to mnemoloci-ingest, mnemoloci-capture, mnemoloci-resume, mnemoloci-query, mnemoloci-review, mnemoloci-lint, mnemoloci-new-project.
---

# Mnemoloci

Mnemoloci splits the work in two:
- **You (the agent)** do everything that needs understanding: reading sources, extracting concepts, writing pages.
- **The `mnemoloci` engine** does everything that must be exact: hashing, locking, validation, journaled writes, indexing.

**Never write into the vault's `wiki/` or `.mnemoloci/` by hand.** The engine publishes pages. The only exceptions are the files the engine tells you to write inside `.mnemoloci/work/<run>/`: extracts and page drafts.

## Running the engine

Every command prints JSON. `"ok": false` comes with a stable `code` and often a `details` object; read it and act on it.

```
python -P -m mnemoloci [--vault PATH] <command>
```

The skills write commands as `mnemoloci <command>` for brevity. Always run them as `python -P -m mnemoloci <command>`, which works even when the `mnemoloci` launcher is not on PATH. Keep the `-P`: without it Python looks in the current folder first, and from a folder that contains a `mnemoloci` folder it picks up the source checkout folder `mnemoloci\` instead of the engine, which fails with `cannot import name '__version__'`.

The vault is chosen in this order:
1. `--vault`, if given;
2. otherwise `$MNEMOLOCI_VAULT`;
3. otherwise the vault recorded in `~/.mnemoloci/vault.txt` (`mnemoloci vault set`, or the first `init`);
4. otherwise `~/Mnemoloci`.

## Which skill to use

| The user wants to… | Skill |
|---|---|
| Start a new project in its own folder ("nuovo progetto X"), also from the phone | `mnemoloci-new-project` |
| Save a link, article, PDF, YouTube video, file or pasted text ("salva questo") | `mnemoloci-capture` |
| Resume past work, find an old session, save a handoff ("dove eravamo rimasti") | `mnemoloci-resume` |
| Todos ("ricordami di…", "cosa ho da fare"), daily/weekly digest ("cosa ho fatto questa settimana") | `mnemoloci-today` |
| Back up the vault, restore an earlier version ("attiva il backup") | `mnemoloci-backup` |
| Merge duplicate pages, add missing links, write synthesis pages ("sistema il wiki") | `mnemoloci-maintain` |
| People, how topics connect, quality score, Obsidian maps/dashboards, llms.txt, daily schedule, context pack, tags | `mnemoloci-explore` |
| Weekly review and plan, todos from a document or meeting notes | `mnemoloci-weekly` |
| Briefing, plain explanation or lesson on a topic, with audited citations | `mnemoloci-brief` |
| Web research on a question, with a cited dossier and reviewed merge ("fai una ricerca su X") | `mnemoloci-research` |
| Write a post/email/note in the user's own voice | `mnemoloci-write` |

**Something not working?** `mnemoloci doctor` checks Python, the engine, the vault, the lock, interrupted writes, the plugin version and hooks, optional extras, git, backup and indexes, and prints the fix for each problem. Run it first when a command fails in an unexpected way.
| Which session wrote this file/line/commit ("perché abbiamo fatto così?") | `mnemoloci-resume` |
| Import ChatGPT or claude.ai conversation exports | `mnemoloci-sessions` |
| Learn from a project folder, docs, notes ("rileggi il progetto X") | `mnemoloci-ingest` |
| Learn from past Claude Code sessions ("leggi le mie sessioni") | `mnemoloci-sessions` |
| Remember a fact about them, a goal, a decision, a lesson; leave a handoff | `mnemoloci-remember` |
| Find an existing skill, create a new one, audit or install a skill | `mnemoloci-skills` |
| Save a screenshot/photo (also sent from the phone), process the inbox | `mnemoloci-inbox` |
| Learn rules from their past corrections ("impara dalle mie correzioni") | `mnemoloci-instincts` |
| Ask what they know about something ("cosa so di X?"), save a good answer | `mnemoloci-query` |
| See or decide on pages held for review | `mnemoloci-review` |
| Check vault health, stale pages, broken links | `mnemoloci-lint` |

## First-time setup

1. Run `mnemoloci init` (only if `lint` reports `NOT_INITIALIZED`).
2. Run `mnemoloci source add <folder> --name <short-name>` for each project the user wants learned.
3. Continue with `mnemoloci-ingest`.

## Extra commands

- **Semantic search:** `mnemoloci index embed` computes local, free, multilingual embeddings, incrementally. After that, `query` also finds pages by meaning, not only by words; results found by meaning carry a `semantic` score. Re-run it when `lint` reports `embeddings_stale`.
- **Exports:**
  - `mnemoloci export graph --format html|graphml|json --out FILE`. The HTML file is self-contained and works offline.
  - `mnemoloci export vault --out FILE [--project-id ID]` exports every page with its metadata, citations and freshness.
- **Open Knowledge Format v0.2** (Google Cloud spec):
  - `mnemoloci export okf --out DIR [--human-id ID] [--include-sources]` writes a conformant bundle.
    - Pages the user approved in review are marked `verified: human:<id>`; all others stay unverified.
    - `--include-sources` copies the redacted source files into the bundle. Ask the user first, because the sources may be private.
  - `mnemoloci import okf DIR [--dry-run]` stages another tool's bundle in the review queue as external knowledge. Unknown fields are kept under `x-okf`.

## Rules

- Knowledge in the vault must be traceable. Every claim you write cites a source line range.
- Nothing is ever deleted automatically. If something looks wrong, report it; do not remove it.
- If the engine returns `LOCK_TIMEOUT`, another process is writing. Wait and retry once, then tell the user.
- If it returns `INCOMPLETE_TRANSACTION`, run `mnemoloci txn recover` and report the result.
