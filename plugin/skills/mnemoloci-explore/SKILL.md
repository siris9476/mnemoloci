---
name: mnemoloci-explore
description: Explore what the second brain knows as a whole - people and organizations, how topics connect (hub pages, bridges, communities, surprising links, isolated pages), the wiki's quality score and trend, Obsidian maps and dashboards, llms.txt export. Use when the user asks "who are the people I work with", "how are my topics connected", "how healthy is the wiki", "make me a map of X in Obsidian", "export for another agent", or, in Italian, "chi sono le persone con cui lavoro", "come sono collegati i miei argomenti", "quanto è in salute il wiki", "fammi una mappa di X in Obsidian", "esporta per un altro agente".
---

# Explore the second brain

## People and other entities

```
mnemoloci people [--type person|organization|place|tool|project|event|all] [--limit 50]
```

Entity pages are ranked by how much they matter now: pages citing them × sources × recency. The ranking is only an order; nothing fades. The app shows them in **Persone**.

## How topics connect

```
mnemoloci graph analyze [--top 10]
```

The analysis returns:
- `hubs`: the most connected pages;
- `bridges`: pages that connect otherwise separate areas, the best candidates for overview pages;
- `communities`: groups of pages that link each other, named after their most connected page;
- `surprising_links`: links between different communities, often the most interesting ideas;
- `isolated`: pages nothing links to.

Offer the next steps: `mnemoloci-maintain` for synthesis and missing links, or an overview page for a large community. The app's **Grafo** view shows the same analysis and can colour pages by community.

## Quality

```
mnemoloci quality [--record] [--min-score 70]
```

- The score runs from 0 to 100 and is built from five parts: structure, citations, freshness, connectivity and trust.
- It shows the change since the last recorded score, and `top_issues` says what to fix first.
- `--record` adds the score to the history; the daily scheduled job does this automatically.
- `--min-score` makes the command exit with code 1 below the threshold, useful as a gate.

## Time

```
mnemoloci query <parole> --as-of YYYY-MM-DD        # only pages valid on that date
mnemoloci maintain validity <slug> [--from D] [--until D] [--refresh-due D] [--superseded-by <slug>]
```

In normal search results, expired pages stay visible but carry `expired`, plus `superseded_by` when set. `lint` lists the `expired` and `refresh_due` pages.

## Obsidian views

```
mnemoloci obsidian canvas <slug> [--depth 1|2]   # wiki/canvases/<slug>.canvas
mnemoloci obsidian bases                         # _meta/bases/*.base dashboards
mnemoloci obsidian graph-colors                  # colour the graph by page kind
```

- Only files Mnemoloci generated are replaced; the user's own `.base` files are skipped.
- Graph colours are set only if the user has no colour groups, and the previous `graph.json` is saved first.

## Context pack for another agent or a chat

```
mnemoloci context <domanda> [--budget 4000] [--summary-only] [--no-memory] [--markdown]
```

The best pages on the question, within a token budget. Each page carries its summary, confidence, freshness, validity and citations. Pages are cut or left out rather than overflowing the budget. The MCP tool `context_pack` does the same.

**Private pages** are never included in context packs or the `llms.txt` export. A page is private when it has `visibility: private` in its frontmatter, or a tag `privato`, `private` or `visibility/private`. Set this with the app editor or when writing the page.

## Tags

```
mnemoloci tags report                       # counts, variants of the same tag, tags used once
mnemoloci tags rename <old> <new>           # dry run, then --approved <hash>
```

`lint` reports `tag_variants`, for example `Lock`, `lock` and `locks`. Propose the most used spelling, and rename only with the user's approval.

## Search quality on real questions

```
mnemoloci quality-eval [--k 5] [--record]
```

- The questions are every saved answer (its question → the pages it used), plus any line `{"query": …, "expected": [slug…]}` in `_meta/eval/queries.jsonl`.
- It reports recall@k and MRR for lexical search, and for hybrid search when embeddings exist, with the worst questions.
- A drop after changes means search got worse.

## Filing methods, as navigation

```
mnemoloci filing para|lyt|zettelkasten
```

This writes index notes in `_meta/filing/<mode>/`; pages never move.
- **PARA:** Progetti, Aree, Risorse, Archivio.
- **LYT:** one Map of Content per group of linked pages, plus Home.
- **Zettelkasten:** a numbered index, plus notes without links.

Regenerate any time.

## Log summaries

`mnemoloci log-fold [--month YYYY-MM]` writes monthly summaries of `wiki/log.md` in `_meta/folds/`. The log itself is never changed.

## Critical pages

Mark a page `critical: true` in its frontmatter when it must be trustworthy, such as health, money or legal matters. `lint` then reports `single_source` if it rests on fewer than **2 independent sources**. A project counts as one source; web clips count one per site. If the vault's review policy includes `single-source`, such pages wait in review.

## Export for other agents

```
mnemoloci export llms --out <folder outside the vault> [--title "…"]
```

This writes `llms.txt` (one line per page, grouped by kind) and `llms-full.txt` (all the text).

```
mnemoloci export jsonld --out wiki.jsonld              # schema.org graph (Person, DefinedTerm, Article, Question…)
mnemoloci export marp <pagina o briefing> --out deck.md  # Marp slides, one per ## section, sources on the last slide
```

Private pages are left out of every export.

## Daily maintenance (opt-in)

```
mnemoloci schedule enable [--time 21:30] | disable | status
```

This creates one Windows scheduled task, and only when the user asks. Once a day it:
1. refreshes the session search index and skill usage;
2. saves the day's digest;
3. records the quality score;
4. backs up the vault, if backup is on.

Each step is logged in `.mnemoloci/scheduled.log`.
