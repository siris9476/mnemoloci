---
name: mnemoloci-lint
description: Read-only health check of the Mnemoloci vault — stale pages whose sources changed, orphaned sources, broken wikilinks, missing frontmatter, low-confidence and contradicted pages, pending reviews, interrupted transactions. Use when the user asks about vault health or before/after large ingests.
---

# Vault health

Run `mnemoloci lint`. It never changes anything. Report to the user by priority:

1. **Must act**
   - `incomplete_transactions`: run `mnemoloci txn recover`.
   - `unreadable`: pages whose frontmatter is broken.
2. **Knowledge is out of date**
   - `stale`: a page's source changed.
   - `orphaned_sources`: a page's source file is gone.
   - Fix with `mnemoloci-ingest`; a new scan picks up modified files automatically.
3. **Quality**
   - `broken_links`, `missing_fields`, `uncited`, `misplaced`, `alias_collisions`.
   - `low_confidence`, `contradicted`: offer to re-check the sources.
4. **Informational**
   - `orphans`: pages nobody links to.
   - `pending_review`: point to `mnemoloci-review`.
   - An active compile run.

Never delete pages to "clean up". Orphaned or stale pages are the user's call.
