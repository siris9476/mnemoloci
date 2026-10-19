---
name: mnemoloci-privacy
description: Scan the vault for credential- or PII-shaped strings that slipped past capture-time redaction, and answer what the vault knows about a person or account. Use when the user says "check the vault for secrets", "did I write a password somewhere by mistake?", "run a privacy audit", "what do you know about me/about X", or, in Italian, "controlla se ci sono segreti nel vault", "ho scritto per sbaglio una password da qualche parte?", "fai un audit privacy", "cosa sai di me/di X", or before sharing/exporting part of the vault with someone else.
---

# Privacy audit

Two separate checks live here: a scan for leaked secrets, and a look at what personal data a page
actually holds. Neither one changes anything — both are report-only, the user decides what to do
with what's found.

## 1. Scan for leaked secrets

```
mnemoloci privacy audit
```

Scans `wiki/`, `memory/` and `_meta/` (not `raw/`, which is already redacted once at capture time
regardless of provenance) for credential-shaped strings: API keys, private keys, tokens, and
similar patterns. It exists because two things can slip past the normal redaction pipeline:

- a manual edit made directly in Obsidian, which bypasses `clips`/ingest entirely;
- a redaction pattern added to the engine *after* a page was already written — patterns are never
  applied retroactively, so older pages don't get rescanned automatically.

The report is `{scanned, pages_with_findings, findings: [{path, findings: [{kind, line}]}]}`. It
never echoes the matched secret itself, only its kind (e.g. `api-key`, `private-key`) and line
number — read the actual line yourself before deciding what to do, since the report is
deliberately silent on the value.

**What to do with a hit:** tell the user exactly which page(s) and line(s), in plain terms (not
just the raw JSON). Don't paste the secret value into the conversation even after opening the
file to look at it. Ask before editing anything — this command never modifies pages itself, and
neither should you without confirmation, since redacting a real credential is different from
removing a false positive (e.g. a `sha256:` hash in frontmatter, or an example key in
documentation).

## 2. What does the vault know about someone

For "cosa sai di me" / "cosa sai su X" style questions where the concern is exposure rather than
recall, this overlaps with `mnemoloci-query` — use `mnemoloci query <name>` to gather what's
written, but frame the answer around what's disclosed (contact info, addresses, relationships,
opinions attributed to them) rather than just facts. If the user is about to share, export or
publish something built from the vault, do a quick `mnemoloci privacy audit` first on anything
that will leave the vault, and flag personal data about third parties (not just the user) that a
wiki page might carry — Mnemoloci's redaction targets credentials, not names, so a person's name
or personal details showing up in a page you're about to share is your call to catch, not the
engine's.

## 3. Tracing how a claim about someone changed

If the concern is "did I write something about X that's no longer true / that I retracted", use
`mnemoloci reflect trace <path>` on the page — it reconstructs every recorded version's
confidence, provenance, contradictions and body, oldest first, so you can show the user the arc
rather than just the current state. See the `mnemoloci-changed-my-mind` skill for narrating that
arc conversationally.

## When to skip this

- A routine question about the user's own preferences/profile — that's `mnemoloci-remember`, not
  this.
- Don't run a full audit on every message; it's for an explicit ask, before sharing content
  outside the vault, or a periodic check the user requests.
