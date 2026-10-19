"""Tag taxonomy: one
vocabulary instead of "Lock", "locks" and "lock_files".

- report: every tag with its count, groups of variants of the same tag (case,
  accents, separators, a trailing plural "s"), and tags used only once;
- rename: replace a tag everywhere (wiki pages and memory notes), dry run first,
  applied only with the approval hash.
"""

import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from .core import txn
from .core.errors import ConflictError, ValidationError
from .core.hashing import canonical_hash, sha256_bytes
from .core.lock import VaultLock
from .index import update as index_update
from .markdown import render_page
from .pages import iter_pages
from .publish import build_writes, stamp_now
from .vault import require_vault

_TAG = re.compile(r"^[^\s,#][^,#]{0,79}$")


def _key(tag: str) -> str:
    """Spelling variants of one tag share a key: case, accents, separators, an English plural.
    Letters of every script are kept (a Cyrillic or CJK tag is not an empty key), and short
    words keep their final s ("news" is not "new")."""
    decomposed = unicodedata.normalize("NFKD", tag.casefold())
    folded = re.sub(r"[\s_\-/]+", "", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))
    return folded[:-1] if len(folded) > 4 and folded.endswith("s") and not folded.endswith("ss") else folded


def _tagged(vault: Path):
    for page in iter_pages(vault, ("wiki", "memory")):
        tags = page.frontmatter.get("tags") if not page.error else None
        if isinstance(tags, list) and tags:
            yield page, [str(t) for t in tags]


def report(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    counts: dict[str, int] = {}
    for _, tags in _tagged(vault):
        for t in set(tags):
            counts[t] = counts.get(t, 0) + 1
    groups: dict[str, list[str]] = {}
    for t in counts:
        groups.setdefault(_key(t), []).append(t)
    variants = [{"tags": sorted(g, key=lambda t: (-counts[t], t)), "suggested": max(g, key=lambda t: (counts[t], -len(t)))}
                for g in groups.values() if len(g) > 1]
    return {"tags": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))),
            "variants": sorted(variants, key=lambda v: v["suggested"].casefold()),
            "used_once": sorted((t for t, n in counts.items() if n == 1), key=str.casefold),
            "next": "unify a group: `mnemoloci tags rename <old> <new>` (dry run), then with --approved <hash>"}


def rename(vault: Path, old: str, new: str, *, approved: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    new = (new or "").strip()
    if not _TAG.match(new):
        raise ValidationError("BAD_TAG", "a tag is one short label without commas or #")
    with VaultLock(vault) as lock:
        changed: dict[str, str] = {}
        bases: dict[str, str] = {}
        for page, tags in _tagged(vault):
            if old not in tags:
                continue
            merged: list[str] = []
            for t in tags:
                t = new if t == old else t
                if t not in merged:
                    merged.append(t)
            fm = dict(page.frontmatter, tags=merged)
            changed[page.path] = render_page(fm, page.body)
            bases[page.path] = page.sha256
        if not changed:
            return {"changed": 0, "note": f"no page has the tag {old!r}"}
        approval = canonical_hash({"rename": [old, new],
                                   "pages": sorted((p, sha256_bytes(c.encode("utf-8"))) for p, c in changed.items())})
        if approved is None:
            return {"dry_run": True, "pages": sorted(changed), "approval_sha256": approval}
        if approved != approval:
            raise ConflictError("PLAN_CHANGED", "pages changed since the dry run; run it again")
        wiki = {p: c for p, c in changed.items() if p.startswith("wiki/")}
        # wiki pages go through build_writes (index + log); memory notes are plain replaces, not wiki index entries
        writes = build_writes(vault, wiki, f"tag rinominato: {old} → {new}", stamp_now(), bases) if wiki else []
        writes += [{"path": p, "action": "replace", "content": c, "expected_sha256": bases[p]}
                   for p, c in changed.items() if p not in wiki]
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"tags-{time.time_ns()}", "operation": "tags-rename",
                  "writes": writes}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, list(changed), lock)
    return {"dry_run": False, "renamed": [old, new], "pages": sorted(changed)}
