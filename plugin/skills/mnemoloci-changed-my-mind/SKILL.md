---
name: mnemoloci-changed-my-mind
description: Narrate how the user's recorded position on a topic changed over time in Mnemoloci — confidence shifts, contradictions found later, revised claims. Use when the user asks things like "how did I change my mind about…", "what did I think before…", "do I still hold the same opinion about…", "why did I change my mind about…", or, in Italian, "come ho cambiato idea su…", "cosa pensavo prima di…", "sono ancora della stessa opinione su…", "perché ho cambiato idea su…".
---

# Trace how a position changed

1. Find the page. If the user gave a topic, not a path, run `mnemoloci query <words…>` first and pick the best-matching page's `path`. Confirm with the user if more than one page could be meant.
2. Run `mnemoloci reflect trace <path> [--limit 20]`. It returns every recorded version of that page, **oldest first**, each with its `confidence`, `provenanceState`, `contradictedBy`, `summary` and `body` as of that commit. It gathers the trail; it does not interpret it — that's this step.
3. If it returns 0 or 1 versions, say so plainly: there isn't enough history to trace a change of mind. Don't invent a narrative from a single snapshot.
4. Read the versions in order and look for what actually moved:
   - **Confidence**: did it rise (verified) or fall (a doubt appeared)?
   - **`contradictedBy`**: did a contradiction appear, get added to, or get cleared? A cleared contradiction usually means it was resolved or the conflicting page was corrected — check that other page (`get_page` on its slug) if it still exists, for the other half of the story.
   - **`body`/`summary`**: what claim actually changed wording, not just tone — a genuine revision, not a rephrase.
   - **`provenanceState`**: moving from `inferred`/`ambiguous` to `extracted` usually means a real source was found later; the reverse means a claim got weaker.
5. Tell the story chronologically, in the vault's language: what was believed first, what changed, when (use the commit dates), and — if visible in the versions — why (a new citation, a contradiction, a correction). Quote the actual old and new wording when it clarifies the shift; don't just say "it changed."
6. If nothing meaningful changed across the versions (just formatting, or the same claim restated), say that plainly instead of manufacturing a narrative arc.

This is read-only: it never writes anything back to the vault. If the conversation surfaces something worth recording (e.g. "I should note why I changed my mind"), that's a normal page edit — go through the review queue like any other change, not a special path from this skill.
