"""Briefings, plain explanations and progressive lessons on a topic: Claude writes them from a context pack; the engine
audits the citations and links, and saves the good ones in _meta/briefings/.

The audit is what makes a briefing trustworthy: every [[link]] must resolve,
every ^[source#Lx-Ly] must point at real lines, and paragraphs without any
citation or link are listed so the writer can back them or mark them as opinion.
"""

import re
import time
from pathlib import Path
from typing import Any

from . import content_guard
from .core import txn
from .core.errors import ValidationError
from .core.lock import VaultLock
from .markdown import citations, render_page, slugify, split_frontmatter, wikilink_targets
from .pages import alias_map, iter_pages
from .secrets import redact
from .sources import load_sources
from .validate import LineCounter
from .vault import require_vault

BRIEF_DIR = "_meta/briefings"
MODES = {"briefing": "Briefing", "spiegazione": "Spiegazione semplice", "lezione": "Lezione progressiva"}


def check(vault: Path, text: str) -> dict[str, Any]:
    require_vault(vault)
    _, body = split_frontmatter(text)
    amap = alias_map([p for p in iter_pages(vault, ("wiki",)) if not p.error])
    counter = LineCounter(vault, load_sources(vault)["sources"])
    broken = sorted({t for t in wikilink_targets(body) if t not in amap})
    bad = [f"{k}#L{s}-L{e}" for k, s, e in citations(body) if not (counter(k) and 1 <= s <= e <= counter(k))]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    unsupported = [p[:120] for p in paragraphs
                   if not p.startswith(("#", ">", "|", "- ", "* ", "```")) and len(p) > 80
                   and not citations(p) and not wikilink_targets(p) and "(opinione)" not in p.casefold()]
    cited = len(paragraphs) - len(unsupported)
    return {"ok": not broken and not bad, "broken_links": broken, "bad_citations": bad,
            "unsupported_paragraphs": unsupported, "paragraphs": len(paragraphs),
            "coverage": round(cited / len(paragraphs), 2) if paragraphs else 0.0}


def save(vault: Path, text: str, *, title: str, topic: str, mode: str = "briefing") -> dict[str, Any]:
    if mode not in MODES:
        raise ValidationError("BAD_MODE", f"mode must be one of {sorted(MODES)}")
    title = " ".join((title or "").split())[:200]
    if not title:
        raise ValidationError("BAD_TITLE", "a title is required")
    body, redactions = redact((text or "").strip())
    content_guard.require_safe(body, "this briefing")
    audit = check(vault, body)
    if not audit["ok"]:
        raise ValidationError("BRIEFING_INVALID", "fix the broken links and citations first", audit)
    rel = f"{BRIEF_DIR}/{time.strftime('%Y-%m-%d')}-{slugify(title)[:60]}.md"
    fm = {"title": title, "schema": "mnemoloci.briefing.v1", "mode": mode, "topic": topic,
          "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "coverage": audit["coverage"],
          "unsupported_paragraphs": len(audit["unsupported_paragraphs"]), "redactions": redactions}
    with VaultLock(vault) as lock:
        n, base = 2, rel
        while (Path(vault) / rel).exists():
            rel, n = base.replace(".md", f"-{n}.md"), n + 1
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"brief-{time.time_ns()}", "operation": "briefing",
                  "writes": [{"path": rel, "action": "create", "content": render_page(fm, body)}]}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    return {"path": rel, "mode": MODES[mode], "coverage": audit["coverage"],
            "unsupported_paragraphs": audit["unsupported_paragraphs"]}
