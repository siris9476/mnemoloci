---
name: mnemoloci-today
description: Todos that persist across sessions, and the daily/weekly digest of the second brain. Use when the user says "what do I have to do", "add to my to-dos", "remind me to…", "mark it done", "what did I do today / this week", "summary", "how did the week go", or, in Italian, "cosa ho da fare", "aggiungi alle cose da fare", "ricordami di…", "segna come fatto", "cosa ho fatto oggi / questa settimana", "riepilogo", "com'è andata la settimana".
---

# Today: todos and digest

## Todos

```
mnemoloci todo add "<una riga>" [--project "<cwd>"] [--due YYYY-MM-DD] [--goal g2]
mnemoloci todo list [--project "<cwd>"] [--all]
mnemoloci todo done t3
mnemoloci todo drop t3            # lasciata perdere: resta registrata, non si cancella
mnemoloci todo update t3 [--text …] [--due …]
```

- Pass `--project` only when the todo belongs to the current project folder. General todos (errands, calls, ideas) have no project.
- Resolve relative dates to a real date before saving ("venerdì" → `YYYY-MM-DD`).
- Open todos are injected at every session start, together with the project's own. Overdue ones are marked `SCADUTA`.
- When the user says something is done, find it with `todo list` and close it with `todo done`. Never close something without being told.

## Digest

```
mnemoloci digest --markdown [--day YYYY-MM-DD] [--week]
mnemoloci digest --save [--day …] [--week]      # keeps it in _meta/digests/
```

The digest is deterministic. It covers:
- pages published and answers saved;
- content captured;
- decisions, lessons and handoffs;
- the Claude Code sessions of the period and the questions they left open;
- todos done, overdue and due within 7 days;
- goals, and what is waiting for the user (review, inbox, content to compile).

When the user asks "cosa ho fatto questa settimana", read the digest and answer in a few sentences:
1. What moved forward. Link the pages as `[[slug]]`.
2. What is stuck: open questions, overdue todos.
3. What deserves attention next, tied to the active goals.

Don't invent activity that is not in the digest. If the period is empty, say so.

The app shows the same data in the **Oggi** view, where todos can also be added and ticked off, from the phone too.
