"""Read-only vault health check."""

import re
import time
from pathlib import Path
from typing import Any

from . import answers, content_guard
from .compile import status as compile_status
from .core import txn
from .core.hashing import sha256_file
from .markdown import citations, citations_with_validity, malformed_validity_citations, wikilink_targets
from .pages import alias_map, iter_pages
from .review import list_candidates
from .sources import load_sources, resolve_root
from .validate import LineCounter, independent_sources
from .vault import KIND_DIRS, load_config, require_vault

REQUIRED = ("title", "kind", "summary", "sources", "confidence", "provenanceState", "updated")
MAX_PAGE_CHARS = 20_000
MIN_LINKS_SYNTHESIS = 2
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _empty_sections(body: str) -> list[str]:
    """Headings with no content before the next heading of the same or a higher level."""
    lines = body.split("\n")
    empty = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m or len(m.group(1)) == 1:
            continue
        level, has_text = len(m.group(1)), False
        for nxt in lines[i + 1:]:
            h = _HEADING.match(nxt)
            if h and len(h.group(1)) <= level:
                break
            if nxt.strip() and not h:
                has_text = True
                break
            if h:
                has_text = True  # a deeper sub-heading counts as content
                break
        if not has_text:
            empty.append(m.group(2))
    return empty


def lint(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    config = load_config(vault)
    registry = load_sources(vault)["sources"]
    count_lines = LineCounter(vault, registry)
    today = time.strftime("%Y-%m-%d")
    pages = list(iter_pages(vault, ("wiki",)))
    stubs = [p for p in pages if not p.error and p.merged_into]
    good = [p for p in pages if not p.error and not p.merged_into]
    amap = alias_map(good)
    issues: dict[str, list[Any]] = {k: [] for k in (
        "unreadable", "missing_fields", "broken_links", "orphans", "stale", "orphaned_sources",
        "low_confidence", "contradicted", "alias_collisions", "misplaced",
    )}
    for p in pages:
        if p.error:
            issues["unreadable"].append({"path": p.path, "error": p.error})

    inbound: dict[str, int] = {p.slug: 0 for p in good}
    owners: dict[str, str] = {}
    file_sha: dict[str, str | None] = {}

    def current_sha(key: str) -> str | None:
        if key not in file_sha:
            name, _, rel = key.partition("/")
            entry = registry.get(name)
            path = resolve_root(vault, entry) / rel if entry and rel else None
            file_sha[key] = sha256_file(path) if path and path.is_file() else None
        return file_sha[key]

    for p in good:
        fm = p.frontmatter
        missing = [f for f in REQUIRED if f not in fm]
        if missing:
            issues["missing_fields"].append({"path": p.path, "missing": missing})
        kind = fm.get("kind")
        if kind in KIND_DIRS and not p.path.startswith(KIND_DIRS[kind] + "/"):
            issues["misplaced"].append({"path": p.path, "kind": kind})
        for target in sorted(set(wikilink_targets(p.body))):
            resolved = amap.get(target)
            if resolved is None:
                issues["broken_links"].append({"path": p.path, "target": target})
            elif resolved != p.slug:
                inbound[resolved] += 1
        for key in sorted(p.keys):
            if key in owners and owners[key] != p.slug:
                issues["alias_collisions"].append({"key": key, "pages": [owners[key], p.slug]})
            owners.setdefault(key, p.slug)
        for src in fm.get("sources") or []:
            if not isinstance(src, dict) or "key" not in src:
                continue
            if (registry.get(str(src["key"]).split("/", 1)[0]) or {}).get("retired"):
                continue  # a retired source is frozen on purpose: never stale, never orphaned
            sha = current_sha(src["key"])
            if sha is None:
                issues["orphaned_sources"].append({"path": p.path, "source": src["key"]})
            elif sha != src.get("sha256"):
                issues["stale"].append({"path": p.path, "source": src["key"]})
        conf = fm.get("confidence")
        if isinstance(conf, (int, float)) and conf < config["review"]["low_confidence_threshold"]:
            issues["low_confidence"].append({"path": p.path, "confidence": conf})
        if fm.get("contradictedBy"):
            issues["contradicted"].append({"path": p.path, "with": fm["contradictedBy"]})
        cites = citations(p.body)
        if not cites and fm.get("kind") != "query":  # a saved answer cites the pages it is based on
            issues.setdefault("uncited", []).append(p.path)
        for key, start, end in cites:  # a line range the source no longer has (or never had)
            total = count_lines(key)
            if total is not None and not 1 <= start <= end <= total:
                issues.setdefault("bad_citations", []).append({"path": p.path, "citation": f"{key}#L{start}-L{end}",
                                                               "lines_in_source": total})
        if fm.get("critical") is True:
            keys = [s["key"] for s in fm.get("sources") or [] if isinstance(s, dict) and "key" in s]
            if independent_sources(vault, keys, registry) < 2:
                issues.setdefault("single_source", []).append({"path": p.path, "sources": keys})
        if fm.get("validUntil") and str(fm["validUntil"]) < today:
            issues.setdefault("expired", []).append({"path": p.path, "valid_until": str(fm["validUntil"])})
        if fm.get("refreshDue") and str(fm["refreshDue"]) <= today:
            issues.setdefault("refresh_due", []).append({"path": p.path, "refresh_due": str(fm["refreshDue"])})
        for c in citations_with_validity(p.body):  # same idea as expired/refresh_due, per single claim
            ref = f"{c.key}#L{c.start}-L{c.end}"
            if c.valid_until and c.valid_until < today:
                issues.setdefault("citation_expired", []).append({"path": p.path, "citation": ref, "valid_until": c.valid_until})
            if c.valid_from and c.valid_from > today:
                issues.setdefault("citation_not_yet_valid", []).append({"path": p.path, "citation": ref, "valid_from": c.valid_from})
            if c.refresh_due and c.refresh_due <= today:
                issues.setdefault("citation_refresh_due", []).append({"path": p.path, "citation": ref, "refresh_due": c.refresh_due})
        for bad in malformed_validity_citations(p.body):  # a typo'd #V/#R silently stops being a citation at all
            issues.setdefault("citation_syntax_error", []).append({"path": p.path, "text": bad})
        empty = _empty_sections(p.body)
        if empty:
            issues.setdefault("empty_sections", []).append({"path": p.path, "headings": empty})
        if len(p.body) > MAX_PAGE_CHARS:
            issues.setdefault("too_long", []).append({"path": p.path, "chars": len(p.body)})
        if kind in ("overview", "comparison") and len(set(wikilink_targets(p.body)) - {p.slug}) < MIN_LINKS_SYNTHESIS:
            issues.setdefault("few_links", []).append({"path": p.path, "kind": kind})
        if fm.get("language") != config["language"]["target"]:
            issues.setdefault("other_language", []).append({"path": p.path, "language": fm.get("language")})

    # Saved answers are leaves (nothing needs to link them); stale when a page they used changed.
    for stub in stubs:  # a merge stub must point at a page that still exists
        if amap.get(stub.merged_into) is None:
            issues["broken_links"].append({"path": stub.path, "target": stub.merged_into})
    from . import tags as tag_vocabulary

    variants = tag_vocabulary.report(vault)["variants"]
    if variants:
        issues["tag_variants"] = [{"tags": v["tags"], "suggested": v["suggested"]} for v in variants]
    answer_slugs = {p.slug for p in good if p.frontmatter.get("kind") == "query"}
    issues["orphans"] = sorted(slug for slug, n in inbound.items() if n == 0 and slug not in answer_slugs)
    issues["stale_answers"] = answers.stale(vault)
    from . import maintain

    for r in maintain.list_rejections(vault):
        if not r.get("citation"):
            issues.setdefault("unverified_rejections", []).append(
                {"page": r["page"], "target": r["target"], "reason": r["reason"]})
            continue
        parsed = maintain.parse_citation(r["citation"])
        key, start, end = parsed if parsed else (r["citation"], 0, 0)
        total = count_lines(key)
        if total is not None and not 1 <= start <= end <= total:
            issues.setdefault("stale_rejections", []).append(
                {"page": r["page"], "target": r["target"], "citation": r["citation"]})
    for p in [*good, *(m for m in iter_pages(vault, ("memory",)) if not m.error)]:
        risky = [f for f in content_guard.scan(p.body) if f["severity"] == "dangerous"]
        if risky:
            issues.setdefault("injection_suspect", []).append(
                {"path": p.path, "line": risky[0]["line"], "why": risky[0]["why"]})
    from . import embed

    if embed.has_vectors(vault):
        stale = embed.stale_count(vault)
        if stale:
            issues["embeddings_stale"] = [f"{stale} pages changed since `mnemoloci index embed`"]
    counts = {k: len(v) for k, v in issues.items()}
    return {
        "pages": len(pages),
        "ok": not any(counts[k] for k in ("unreadable", "broken_links", "stale", "orphaned_sources", "missing_fields")),
        "counts": counts,
        "issues": issues,
        "pending_review": len(list_candidates(vault)),
        "incomplete_transactions": txn.incomplete_transactions(vault),
        "compile": compile_status(vault),
    }
