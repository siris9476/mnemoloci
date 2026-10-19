"""Saved answers: good answers become wiki pages instead of being lost with the
chat.

An answer is only saved when it is grounded: it must link the wiki pages it is
based on, every link must resolve, and every ^[source#Lx-Ly] citation must point
at real lines. The page records the exact version of each page it used
(`basedOn`), inherits their sources (so it goes stale with them) and takes the
lowest of their confidences.
"""

import time
from pathlib import Path
from typing import Any

from . import content_guard
from .core import txn
from .core.errors import ConflictError, ValidationError
from .core.lock import VaultLock
from .index import update as index_update
from .markdown import citations, render_page, slugify, wikilink_targets
from .pages import alias_map, iter_pages
from .publish import build_writes, stamp_now
from .secrets import redact
from .sources import load_sources
from .validate import LineCounter
from .vault import KIND_DIRS, load_config, require_vault

PREFIX = "risposta-"
MAX_ANSWER = 30_000


def save(vault: Path, *, title: str, question: str, text: str, summary: str | None = None,
         replace: bool = False) -> dict[str, Any]:
    require_vault(vault)
    title = " ".join((title or "").split())
    question = " ".join((question or "").split())
    if not title or len(title) > 200:
        raise ValidationError("BAD_TITLE", "title must be 1-200 characters")
    if not question:
        raise ValidationError("NO_QUESTION", "record the question this answers")
    body, redactions = redact((text or "").strip())
    if not body:
        raise ValidationError("EMPTY_TEXT", "the answer is empty")
    if len(body) > MAX_ANSWER:
        raise ValidationError("TEXT_TOO_LONG", f"answer over {MAX_ANSWER} chars: split it or summarise")
    content_guard.require_safe(body, "this answer")
    config = load_config(vault)

    with VaultLock(vault) as lock:
        pages = [p for p in iter_pages(vault, ("wiki",)) if not p.error]
        amap = alias_map(pages)
        by_slug = {p.slug: p for p in pages}
        targets = sorted(set(wikilink_targets(body)))
        broken = [t for t in targets if t not in amap]
        if broken:
            raise ValidationError("BROKEN_LINKS", "these [[links]] match no wiki page; fix or remove them",
                                  {"broken": broken})
        rel = f"{KIND_DIRS['query']}/{PREFIX}{slugify(title)[:70]}.md"
        own_slug = Path(rel).stem
        basis = sorted({amap[t] for t in targets} - {own_slug})
        if not basis:
            raise ValidationError("NO_BASIS", "an answer must link the wiki pages it is based on, e.g. [[slug]]")
        counter = LineCounter(vault, load_sources(vault)["sources"])
        bad = [f"{k}#L{s}-L{e}" for k, s, e in citations(body) if not (counter(k) and 1 <= s <= e <= counter(k))]
        if bad:
            raise ValidationError("BAD_CITATIONS", "citations must point at existing source lines", {"bad": bad})

        sources: dict[str, str] = {}
        confidences = []
        for slug in basis:
            fm = by_slug[slug].frontmatter
            for src in fm.get("sources") or []:
                if isinstance(src, dict) and isinstance(src.get("key"), str) and src.get("sha256"):
                    sources.setdefault(src["key"], str(src["sha256"]))
            if isinstance(fm.get("confidence"), (int, float)) and not isinstance(fm.get("confidence"), bool):
                confidences.append(float(fm["confidence"]))
        fm = {
            "title": title, "kind": "query",
            "summary": (summary or question)[:500],
            "aliases": [],
            "sources": [{"key": k, "sha256": v} for k, v in sorted(sources.items())],
            "confidence": min(confidences) if confidences else 0.5,
            "provenanceState": "inferred", "contradictedBy": [],
            "updated": time.strftime("%Y-%m-%d"),
            "language": config["language"]["target"],
            "question": question,
            "basedOn": [{"slug": s, "sha256": by_slug[s].sha256} for s in basis],
            "redactions": redactions,
        }
        target = Path(vault) / rel
        existed = target.exists()
        if existed and not replace:
            raise ConflictError("ANSWER_EXISTS", f"{rel} exists: use --replace to update it (the old text is kept "
                                                 "as a backup under .mnemoloci/transactions/)")
        content = render_page(fm, f"# {title}\n\n> **Domanda:** {question}\n\n{body}")
        stamp = stamp_now()
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"answer-{time.time_ns()}", "operation": "answer-save",
                  "writes": build_writes(vault, {rel: content}, f"risposta salvata: [[{own_slug}]]", stamp)}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, [rel], lock)
    return {"path": rel, "slug": own_slug, "based_on": basis, "sources": len(sources),
            "confidence": fm["confidence"], "replaced": existed}


def stale(vault: Path) -> list[dict[str, Any]]:
    """Answers whose basis pages changed (or vanished) since they were saved."""
    pages = [p for p in iter_pages(vault, ("wiki",)) if not p.error]
    by_slug = {p.slug: p for p in pages}
    out = []
    for p in pages:
        if p.frontmatter.get("kind") != "query":
            continue
        changed = [b.get("slug") for b in p.frontmatter.get("basedOn") or []
                   if isinstance(b, dict) and (b.get("slug") not in by_slug or by_slug[b["slug"]].sha256 != b.get("sha256"))]
        if changed:
            out.append({"path": p.path, "changed": changed})
    return out
