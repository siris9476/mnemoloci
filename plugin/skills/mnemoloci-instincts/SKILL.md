---
name: mnemoloci-instincts
description: Learn small durable rules ("when X, do Y") from the user's corrections and confirmations in past Claude Code sessions, and propose them for approval. Use when the user asks to learn from their feedback, to stop repeating mistakes, "learn from my corrections" (in Italian "impara dalle mie correzioni"), or periodically after many sessions.
---

# Instincts: learn from corrections, with the user's approval

An instinct is a one-line rule the user taught through feedback. Approved instincts are injected into every future session, in its folder or globally. **Nothing is used until the user approves it.**

## 1. Mine the moments

The engine does this step deterministically:

```
mnemoloci instincts mine [--project TEXT] [--since YYYY-MM-DD]
```

It reads the transcripts and finds:
- **corrections**: "no, …", "wrong", "actually…", "don't…", "stop", "I said…", "not what I asked", "from now on…"
  (and their Italian equivalents: "in realtà…", "non devi…", "ti avevo chiesto…", "ricordati che…");
- **confirmations**: "perfect", "exactly" (Italian: "perfetto", "esatto").

Each moment comes with the assistant reply that came right before it. The engine writes a `packet` JSON file; already-seen moments are skipped.

## 2. Write proposals

Read the packet. For each **recurring or clearly durable** lesson, write one proposal to a JSON file:

```json
{"schema": "mnemoloci.instinct-proposals.v1", "proposals": [
  {"trigger": "the user asks to analyse a project",
   "action": "read the code and the documentation; do not install or run anything without asking",
   "scope": "global",
   "evidence": [{"session_id": "…", "ts": "…", "quote": "Actually I never asked you to run the tests…"}]}
]}
```

**What makes a good instinct**
- The **trigger** is a situation, not a topic. The **action** is concrete behaviour. Each is one line, at most 300 characters, in the user's language.
- Generalise from the correction to the rule behind it. Don't copy the sentence.
- Keep confirmations too, when they validate a non-obvious choice. They prevent drifting away from what worked.

**Scope and evidence**
- Set `scope` to the project path if the lesson only applies there; otherwise use `global`.
- Evidence must be `session_id` + `ts` pairs from the packet. The engine rejects anything else. Several moments can support one instinct, and more distinct sessions means higher confidence (0.3 → 0.85).

**Skip**
- one-off task corrections: a typo, a wrong file;
- anything already in the user's profile or `CLAUDE.md`;
- anything sensitive.

## 3. Submit and ask for approval

```
mnemoloci instincts propose <file>
mnemoloci instincts list --status proposed
```

Show the user each proposal as "Quando … → …", with its evidence. They decide in chat or in the app's Revisione view:

```
mnemoloci instincts approve <id>
mnemoloci instincts reject <id> --reason "…"
mnemoloci instincts retire <id> --reason "…"    # an active one that no longer applies
```

Rejected and retired instincts are kept for audit. Re-proposing the same rule adds evidence to the existing note instead of duplicating it.

## Promote, export, import

```
mnemoloci instincts promote-candidates   # project rules that recur, similar, in 2+ projects
mnemoloci instincts promote <id>         # make one global (only when the user agrees)
mnemoloci instincts export --out rules.json
mnemoloci instincts import rules.json    # e.g. on another PC: they arrive as proposed, approve them again
```

After promoting a rule, offer to retire its project copies with `instincts retire`.

## From instincts to a skill

Several approved instincts about the same subject are really one procedure:

```
mnemoloci instincts evolve [--min-size 2]
```

It returns groups of approved instincts with the words they share. Offer the user to turn a group into a skill. If they agree, follow `mnemoloci-skills` step 3 with `--origin instincts` and the instinct paths as `--source`. The instincts stay active: the skill does not replace them.
