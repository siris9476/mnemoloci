---
name: mnemoloci-query
description: Answer questions from the user's Mnemoloci second brain with citations, and save good answers as wiki pages. Use when the user asks what they know, decided, or wrote about a topic ("what do I know about…", "how does… work", "what had we decided about…", in Italian "cosa so di…", "come funziona…", "cosa avevamo deciso su…"), or says "save this answer" ("salva questa risposta").
---

# Query the vault

1. Run `mnemoloci query <words…> [--limit 10]`. It uses full-text BM25 ranking (accents ignored, weighted towards title and aliases). It also returns `related` pages reached through wikilinks.
2. Read the top pages from the vault: `path` is relative to the vault root. Follow a wikilink when it clearly helps.
3. Answer from the pages only:
   - Cite each page as a link, e.g. `[[slug]]`.
   - If a key point matters, also give the underlying source citation (`^[source#Lx-Ly]`) found in the page.
4. If the vault doesn't cover the question, say so plainly. You may add general knowledge, but label it as not from the vault.
5. Point out when a relevant page is flagged in its frontmatter as low confidence, contradicted, or `inferred`.

If the query finds nothing, try synonyms or the other language (Italian/English), since the index matches words, not meaning. Then check whether the topic's source was ever ingested (`mnemoloci scan`).

## Save a good answer

When an answer synthesised several pages and would be useful again, offer to keep it. Save it when the user says "salva questa risposta" or accepts the offer. Write the answer to a file, then run:

```
mnemoloci answer save --title "<titolo breve>" --question "<la domanda dell'utente>" --text-file <file> [--summary "<una riga>"]
```

- The text must link every page it is based on as `[[slug]]`, and every link must resolve. Otherwise the engine refuses with `BROKEN_LINKS` or `NO_BASIS`.
- Keep the `^[source#Lx-Ly]` citations you took from the pages; the engine checks the line ranges.
- Write it in the vault language and state what the vault does not cover. Don't include general knowledge in a saved answer.
- The page lands in `wiki/queries/risposta-<slug>.md`. It records which version of each page it used, and `lint` reports `stale_answers` when those pages change. Update it with `--replace`.
