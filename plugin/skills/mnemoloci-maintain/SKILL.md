---
name: mnemoloci-maintain
description: Improve the Mnemoloci wiki itself - merge duplicate pages, add missing links between pages, write synthesis pages for topics that belong together. Use when the user says "tidy up the wiki", "merge the duplicates", "link the pages", "maintain the second brain", or, in Italian, "sistema il wiki", "unisci i doppioni", "collega le pagine", "manutenzione del second brain", or when lint shows alias collisions or many orphans.
---

# Wiki maintenance

Every change here goes through the user: merges and new pages go through the review queue, and links need an approval hash. Never edit wiki files by hand.

## 1. Duplicates → merge

```
mnemoloci maintain duplicates [--limit 20]
```

Pairs come with their reasons: similar names, same sources, or, when `index embed` was run, very close meaning. For each pair the user agrees to merge:
1. Read both pages.
2. Write one merged page for the page to keep:
   - keep every fact and every `^[source#Lx-Ly]` citation from both;
   - list **all** sources of both pages in `sources`, or the engine refuses with `SOURCES_LOST`;
   - keep the vault language and the original-text block.
3. Queue it:

   ```
   mnemoloci maintain propose-merge --keep <slug> --absorb <slug> --page <file>
   ```

   The engine adds the absorbed page's names as aliases. It queues the merged page together with a stub that replaces the absorbed page ("Unita in [[…]]"), so old links keep working and nothing is deleted. The user approves both writes as one, from `mnemoloci-review` or the app.

## 2. Missing links

```
mnemoloci maintain links [--limit 100] [--min-score 3]
```

Each suggestion is scored from three signals — an exact textual mention of another page's title/alias (strongest alone), shared tags (2+), and a peripheral page (few outbound links) reaching a well-connected hub — with a `confidence` label (`bassa`/`media`/`alta`). A mention-based suggestion has `text` set and can be applied inline; a tags-only suggestion has `text: null` — `apply-links` can't place it in the body (no natural mention to wrap), so add it under a `## Collegamenti` section by hand if you agree with it. Show the suggestions and let the user pick. Put the chosen ones in a JSON file, then:

```
mnemoloci maintain apply-links <file>                    # dry run: pages and approval_sha256
mnemoloci maintain apply-links <file> --approved <sha>    # applies, one transaction
```

Applying a mention-based suggestion also infers a relationship type from the sentence it's in (`uses`, `extends`, `implements`, `contradicts`, `derived_from`, `replaces`, or the default `related_to`) and records it in the page's `relationships:` frontmatter — deterministic pattern matching, not a judgment call you need to make.

Only the first unlinked mention in each page is linked. Headings, quotes, tables and code are never touched.

## 2b. Broken links

```
mnemoloci maintain fix-links                 # dry run: fixes and unresolved ones
mnemoloci maintain fix-links --approved <sha>
```

This repairs a broken `[[short name]]` only when exactly one page can be meant, for example a longer title starting with it, and keeps the visible text. A more specific link is never folded into a general page. Ambiguous links are listed for the user to fix.

## 2c. Project hub cohesion

```
mnemoloci maintain hub-links                 # dry run: hubs and how many bullets each gains
mnemoloci maintain hub-links --approved <sha>
```

Links every page to the most specific existing overview (or entity) mined from the same project, and gives a hub with zero inbound links a reciprocal link back from its single most substantial child, so browsing from any page in a project always reaches that project's own overview. Run this after every compile block, not just once — new pages need it every time.

## 2d. Recording a "no"

When you (or the user) look at a candidate link — from `maintain links`, `maintain synthesis`, or free-form investigation like "are these two pages actually the same thing?" — and decide it does **not** belong, don't just drop it silently: that judgment is exactly the kind of thing a future session has no way to check and can only inherit on trust. Record it:

```
mnemoloci maintain reject-link --page <slug> --target <slug-or-label> --reason "..." [--citation "source/rel/path.md#L10-L14"]
mnemoloci maintain rejections                # list what's recorded
```

Always include `--citation` when the source material actually proves the two are different (a line that names a different company, a different repo, a different person) — `lint` flags a rejection with no citation as `unverified_rejections`, and one whose citation no longer matches the live source as `stale_rejections`, so an unexamined or rotted "no" surfaces for a second look instead of standing forever. Before restating a rejection you find already recorded (or inherited from a prior session's summary) as settled, re-read the citation yourself; don't just repeat the verdict.

## 3. Synthesis

```
mnemoloci maintain synthesis [--limit 15]
```

These are pairs of pages that other pages cite together, or that share sources, but that never link each other. When a pair is worth it:
1. Write a `comparison` or `overview` page that links both and cites the sources.
2. Send it to review:

   ```
   mnemoloci maintain submit-page <file>
   ```

   Broken `[[links]]` are refused. A title that already exists needs `--replace`, or a merge.

## After approval

Run `mnemoloci lint` to confirm that no broken links or alias collisions are left, and check `unverified_rejections`/`stale_rejections` too — a "no" that was never backed by a citation, or whose citation rotted, needs a second look before it gets trusted again.

## Routine after every compile block

Do not let this accumulate across blocks: run `fix-links` → `links`/`apply-links` → `hub-links` → close the review queue, every block, in this order — not once every several blocks. `mnemoloci doctor` now checks this on its own (the `maintenance` check warns when compile blocks have landed since the last of the three ran) — but don't wait for the warning, run the routine as a matter of course.
