---
name: mnemoloci-weekly
description: Guided weekly review with the user (what moved, stuck goals, open questions, plan for next week) and turning documents or meeting notes into todos. Use when the user says "let's do the weekly review", "how did the week go and what do I do next", "let's plan the week", or shares a document/meeting notes and asks "what do I need to do from here", or, in Italian, "facciamo la revisione settimanale", "com'è andata la settimana e cosa faccio la prossima", "pianifichiamo la settimana", "cosa devo fare da qui".
---

# Weekly review

## 1. Gather

```
mnemoloci weekly review [--day YYYY-MM-DD]
```

The review returns:
- the week's digest;
- for each goal: todos done, open todos, linked pages updated;
- `stuck_goals`: active goals that did not move;
- `goals_without_next_step`: active goals with no open todo;
- `open_questions`: questions left at the end of sessions.

## 2. Talk it through, in a few short turns

1. **What moved.** Two or three sentences with page links.
2. **What is stuck.** For each stuck goal, ask what blocks it. Offer: a concrete next step (a todo with a date), pause it, or drop it. Apply only what the user confirms:
   ```
   mnemoloci goals update g2 --status paused|dropped|active
   mnemoloci todo add "<prossimo passo concreto>" --goal g2 --due YYYY-MM-DD
   ```
3. **Open questions.** Answer the questions left open, or turn them into todos.
4. **Capacity.** Ask how much time the user has next week. Propose at most 3-5 priorities, not a wish list.

## 3. Save the plan

Write the agreed plan to a file, then:

```
mnemoloci weekly save --plan-file <file>
```

The note lands in `_meta/weekly/<YYYY-Www>.md` with the digest attached. Next week's review can be compared with it.

# Todos from a document or meeting notes

When the user shares a document, contract, email or meeting notes:
1. Read it and list **only real commitments**: who, what, by when. Cite where each one comes from.
2. Keep the strength of each item distinct: "deve / must", "dovrebbe / should" and "potrebbe / may" are different. Don't upgrade a suggestion into an obligation.
3. Show the list and ask which ones to add. Then add them:
   ```
   mnemoloci todo add "<azione>" --due YYYY-MM-DD [--goal gN] [--project "<cwd>"]
   ```
4. Commitments that belong to other people are not the user's todos. Mention them as "da seguire", or add them only if the user wants a reminder to check.

If the document is worth keeping, also offer to capture it (`mnemoloci-capture`).
