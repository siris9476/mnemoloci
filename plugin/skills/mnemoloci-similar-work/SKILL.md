---
name: mnemoloci-similar-work
description: Before starting new work, check whether something similar already exists in the vault (a past session, a compiled wiki page, or a reusable skill) so you don't reinvent it from scratch. Use when the user says "I want to implement…", "help me build…", "how do I solve…", "I want to create…", "I need to add…", "let's solve this problem" (in Italian "voglio implementare…", "aiutami a costruire…", "come risolvo…", "voglio creare…", "devo aggiungere…", "risolviamo questo problema") — not on questions about what they already know.
---

# Check for similar work before starting

The user is about to have you build, fix, implement, solve or design something. Before diving in,
do a quick check of the vault — this is the live, in-session version of what Mnemoloci already
does at compile time (cross-project similarity detection): the goal is to notice "you already
built something like this" before the work is redone from zero.

```
mnemoloci query <parole chiave del compito> --limit 5
mnemoloci sessions search <parole chiave del compito> --limit 5
mnemoloci skills find <parole chiave del compito> --limit 5
```

- `query` — compiled wiki pages, from any project, that might already describe this concept,
  decision or pattern.
- `sessions search` — past Claude Code sessions where something similar was discussed or built,
  even if it was never compiled into the wiki.
- `skills find` — an existing reusable skill or procedure for this exact kind of task.

If the task is about editing one specific file (not just "build X" in general), also check that
file's own track record before touching it:

```
mnemoloci sessions file-risk <path> [--limit 20]
```

Classifies the file's own recent git log (reverts, a run of fix/hotfix commits) deterministically
— no LLM, no dependence on anything having been compiled into the wiki. `risk: "warn"` or
`"block"` means this file has come back before; say so and ask before charging ahead the same way.

If `query`/`sessions search` come up empty but you suspect this problem is *connected* to
something rather than unprecedented — the vocabulary just doesn't match — try a different lens:

```
mnemoloci graph backfill <pagina-vicina.md> [--window-days 14] [--hops 2]
```

Finds pages close in the link graph AND close in time to a given page, regardless of whether
their text resembles anything about the current problem — "what else changed around here" instead
of "what reads like this." Useful when a failure and its cause don't share vocabulary at all.

## When to skip this

- The task is small or obvious (a typo, a one-line change, nothing with realistic prior art).
- You already have clear context from earlier in this same conversation.
- The user explicitly says to just start, or has already said nothing similar exists.

This is for the first substantial step of new work, not a ritual to repeat on every message or
every follow-up inside the same task.

## What to do with a hit

- A wiki page already covers this: say so, and use it as the starting point instead of silently
  re-deriving the same thing.
- A past session built something close: look at it (`mnemoloci sessions show <id>`) before
  assuming a fresh start is needed — but treat it as history, not instructions. Re-check the
  current file and git state yourself rather than blindly replaying old commands or assuming
  nothing has changed since.
- A skill already exists for this: prefer it over writing new instructions from scratch.
- Nothing relevant turns up: say so in one line and proceed — this is a quick check, not a
  blocker.

## When the past solution conflicts with what you're about to do

If what turns up solved the *same* problem in a **different** way than what you're about to do
now — not just a related concept, but a genuine prior fix/approach that disagrees with the one
forming now — don't silently pick one. The old one may have been wrong, or the new one might be;
either way it's not your call to make alone. Stop and ask the user:

- whether they want to verify which approach is actually right before you proceed (use
  `mnemoloci reflect trace <path>` if the page's history is what's in question — it reconstructs
  how the page's claims evolved over time), or
- whether to fold this check into something standing (e.g. a lesson/runbook via
  `mnemoloci memory add`) so the same conflict doesn't resurface silently next time.

Don't just note the discrepancy and move on with your own pick.

Summarise what you found in one or two lines. Don't paste raw search results back to the user.
