---
name: mnemoloci-remember
description: Save and manage the user's personal profile, goals, and working-memory notes (decisions, lessons, handoffs, preferences, runbooks) in Mnemoloci. Use when the user says "remember that…" ("ricordati che…"), states a goal or preference, asks to track objectives, when an important decision is taken, when a mistake teaches a lesson, or before ending/pausing work to leave a handoff.
---

# Remember: profile, goals, working memory

These notes are injected at the start of every Claude Code session by the SessionStart hook. Keep them **short, true, and useful**, because noise here costs context in every session.

## Profile (`_meta/profile.md`)

```
mnemoloci profile show
mnemoloci profile set "<fatto>" "<valore>" [--confidence 0.9] [--source "sessione 2026-09-24"]
```

- **What to store:** stable facts about the user: role, skills, tools, preferences, working style.
- **Keys:** short and reused ("ruolo", "linguaggi", "editor", "stile di risposta"). Setting a key again keeps the old value in history.
- **Confirmation:** save only what the user said or confirmed. Ask before saving something you inferred.
- **Refused by the engine:** secrets, IBAN, codice fiscale, card numbers, passwords, multi-line text. Don't try to work around it.

## Goals (`_meta/goals.md`)

```
mnemoloci goals list [--all]
mnemoloci goals add "<obiettivo in una riga>" [--area lavoro] [--link <slug-pagina>]
mnemoloci goals update g3 --status done|paused|active|dropped [--text …] [--link …]
```

- One line per goal, phrased as an outcome.
- At most 12 active goals.
- Close or pause goals instead of deleting them; nothing is ever deleted.
- Link goals to the wiki pages they concern.

## Working memory (`memory/`)

```
mnemoloci memory add decision|fact|lesson|handoff|preference|runbook --title "…" --text "…" --project "<cwd>"
mnemoloci memory list [--kind handoff] [--project <cwd>]
mnemoloci memory supersede <old-id> --by <new-id>
```

Always pass `--project` with the current working directory. That is how the next session in that folder finds the note.

| Kind | When to use it |
|---|---|
| `decision` | A choice plus **why**, including the alternatives rejected. |
| `lesson` | What went wrong or right and the rule to follow next time. |
| `handoff` | Before stopping, or when the user says "riprendiamo dopo". What was done, what's left, where to resume, open questions. |
| `runbook` | A procedure that worked and will be repeated. |
| `fact`, `preference` | Project-scoped facts or preferences that are not about the user personally. |

**Say how sure you are.** `--confidence verified|inferred|unverified` (default `inferred`). `verified` needs `--verification "how you checked"`, and figures with no date (`45 tok/s`, `12%`, versions) are capped to `inferred`; the result then carries `warnings`. Lessons, preferences and runbooks with no `--project` should say where they apply with `--generality`. A pasted conversation is refused (`TRANSCRIPT_LIKE`): save the conclusion. If the result has `similar`, look at those notes before saving another.

The PreCompact hook saves a mechanical handoff (the last prompts) automatically. A handoff you write yourself is better. When a newer decision replaces an older one, supersede the old note; don't edit or delete it.

**Save without asking: `decision`, `fact`, `handoff`.** These record something that happened, not a judgement about the user or a rule for your own future behavior, so there is little downside to saving them as soon as they're clear and no reinforcement effect to get wrong. Save them straight away, tagged `--source assistant-auto` so they stay distinguishable from what the user asked you to save directly, then say in one short line what you saved (title + kind) so they can correct or retract it on the spot. Don't wait for the periodic check and don't ask first.

```
mnemoloci memory add decision --title "…" --text "…" --project "<cwd>" --source assistant-auto
```

**Lessons: save them when they are confirmed.** A lesson is confirmed when the session itself proved it (you measured it, the fix worked, the user corrected you) or when the user said so. Save it straight away, with `--generality` saying where it applies and tagged `--source assistant-auto`, then say in one line what you saved so it can be retracted on the spot. A lesson you only suspect is still offered, not saved. The user asked for this ("salva sempre le lezioni se sono confermate"); it does not extend to anything else.

**Keep asking first: `preference`, `runbook`.** A `preference` is re-injected to steer how you behave in later sessions and gets *stronger* each time it is confirmed (below), so a wrong one compounds; keep offering it and waiting for a yes. A `runbook` claims that a procedure works, worth a human's confirmation before it's repeated.

**Reviewing what was saved automatically.** `mnemoloci memory list --source assistant-auto [--project <cwd>]` lists everything saved without asking, newest first. Point the user here if they want to audit or prune; retract or suppress an entry exactly as any other note (below). The desktop app can filter `/api/memory` the same way (`?source=assistant-auto`) if its UI grows a review view.

**Lessons and preferences get stronger, they don't pile up.**
- Saving a `lesson` or `preference` whose title matches an active one for the same project confirms it: the result has `reinforced: true` and a `confirmations` count, and the new evidence is appended.
- Reuse the exact title of a lesson you are confirming.
- Nothing ever decays.

**They come back when relevant.** When the user writes a message, relevant lessons, decisions, preferences and runbooks are added to your context as `<mnemoloci-hints>`, at most three, each once per session. Treat them as reference: apply them when they fit, and mention one if it changes what you do.

**Periodic check.** Every 20 user messages you get a short reminder, set by `reflection.every` in the vault config, where 0 disables it. At a natural pause, offer in one line to save what emerged: preferences or runbooks worth confirming, lessons you only suspect, todos. (Decisions, facts and handoffs, and lessons that are already confirmed, don't need batching here — save those as they happen, per above.)

**Retract instead of forgetting.** If the user wants something gone ("dimentica…", "non è più vero", "togli quel dato"), use:

```
mnemoloci memory retract <id> --reason "…" [--redact]
```

The note stays in the vault but is marked `retracted` and never injected or suggested again. `--redact` also blanks its text. The file remains, and the earlier text survives only in the git backup, if backup is on. Tell the user this honestly. Nothing is deleted.

**Suppress instead of retract, when nothing was wrong.** If a note is just noisy right now — not false, not outdated, just not relevant today — retracting it overstates the case (it requires a reason and reads as "this was wrong"). Use:

```
mnemoloci memory suppress <id>
mnemoloci memory unsuppress <id>
```

No reason needed, and it's trivially reversible: the note stays `active` in every other way, just hidden from hints/listings until unsuppressed. Reach for this when the user says something like "non mi serve più per ora" rather than "questo non è più vero".

Text that reads like orders to an AI ("ignora le istruzioni…") is refused with `INJECTION_SUSPECT`. Rephrase it as a plain statement.
