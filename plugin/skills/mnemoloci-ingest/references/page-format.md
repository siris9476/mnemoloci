# Page format

Pages are written in the vault's **target language** (`mnemoloci config show` → `language.target`, default `en`). When the job's `original_languages` is not empty, the page also keeps the source text in a collapsible block at the end.

```markdown
---
title: Transaction engine
kind: concept
summary: Writes to the vault with an approved plan, a journal, backup and rollback.
aliases:
- Motore transazionale
language: en
originalLanguage:
- it
sources:
- key: demo/README.md
  sha256: 3f5c…
confidence: 0.85
provenanceState: extracted
contradictedBy: []
updated: 2026-09-24
---

# Transaction engine

The engine writes to the vault only after a plan is approved ^[demo/README.md#L12-L14].
A journal allows rollback after a crash ^[demo/README.md#L20-L22].

## Links
- [[vault-lock]]

> [!quote]- Original (it)
> Il motore scrive nel vault solo dopo l'approvazione di un piano ^[demo/README.md#L12-L14].
> Un journal permette il rollback dopo un crash ^[demo/README.md#L20-L22].
```

In a vault whose target language is Italian the same page has `language: it`, the related-pages heading is
`## Collegamenti` and the callout is `> [!quote]- Originale (en)`. The engine only requires a collapsed callout
whose title ends in `(<lang>)`; follow the labels the job gives you for this vault.

## Rules

**Frontmatter**
- Start from the job's `suggested_frontmatter`. Keep `language`, `originalLanguage` and the `sources` entries exactly: key plus sha256.
- The engine checks that every contribution's source is declared with the right hash.
- Write a real `summary` of one or two sentences, at most 500 characters, in the target language.
- `confidence` may be lowered but should not be raised above what the contributions justify.

**Translation**
- The main text is a faithful translation plus synthesis of the claims in the target language.
- Keep technical terms, commands, code and proper names as they are.
- When a translation choice matters, add the original term in parentheses the first time, e.g. "lookup block (lookup)".

**Original-text block**, one per language in `original_languages`:
- It is a collapsed Obsidian callout whose first line is `> [!quote]- Original (<lang>)` (`Originale` in an Italian vault).
- Every following line starts with `> `.
- It contains the same claims in the source language, close to the source wording, with the **same citations**.
- If it is missing, the page is held for review.
- If `original_languages` is empty (the source is already in the target language), don't add it.

**Citations**
- Every factual sentence carries a citation, using the `cite` markers from the job's claims: `^[source/key#Lstart-Lend]`.
- Citations must point to declared sources and valid line ranges; otherwise the page is held for review.

**Links**
- Use `[[slug]]` or `[[slug|Label]]`. Link the job's `related_slugs`.
- A link to a page that doesn't exist yet is allowed, but `lint` reports it.
- For a brand-new page, the job may carry `cross_project_similar`: existing pages from a *different*
  project whose meaning is close to this one (found by embeddings, not by name — the engine's own
  dedup only catches concepts named the same way). It's a nudge, not a fact: read the page(s) it
  points at and decide for yourself whether they're really the same thing, a related-but-distinct
  concept worth a `[[slug]]` link, or just a coincidence to ignore. Never merge into one of them or
  copy `cross_project_similar` into the page verbatim — it's for your judgment, not the reader's.

**Body**
- Keep the page focused on one subject.
- Reference Markdown and code excerpts sparingly; the citation points to the full source.
- Don't invent facts beyond the claims. If you add your own synthesis, set `provenanceState: inferred` or lower `confidence`, and say so in the text.

**Merging with an existing page**
- Keep its valid content and citations, merge the new claims, and union the sources.
- Keep one original-text block per language, extending it with the new claims.

## When a page is held for review

The engine publishes a page directly unless one of these applies:
- `low-confidence`: confidence below 0.5 or missing;
- `contradicted`: `contradictedBy` is non-empty;
- `schema-violating`: a missing or malformed field, the wrong `language`, or a missing original-text block;
- `provenance-violating`: no citations, an undeclared source, or a bad line range;
- `external-source`: built from a screenshot or import.
