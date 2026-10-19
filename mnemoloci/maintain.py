"""Wiki maintenance that actually changes pages, always behind the user's approval
.

- duplicates: pairs of pages that are probably the same thing (title/alias words,
  shared sources, and embeddings when computed). Claude writes the merged page;
  `propose-merge` puts it in the review queue together with a stub that replaces
  the absorbed page ("Unita in [[…]]"), so links keep working and nothing is deleted.
- links: plain-text mentions of another page's title/alias that are not linked
  yet. Applied deterministically, with a dry run and an approval hash.
- synthesis: pairs of pages often cited together but never linked to each other,
  candidates for a comparison/overview page that Claude writes and `submit-page`
  sends to review.
"""

import json
import re
import time
import unicodedata
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from . import i18n
from .core import txn
from .core.atomic import atomic_json
from .core.errors import ConflictError, MnemoError, ValidationError
from .core.hashing import canonical_hash, sha256_bytes, sha256_file
from .core.lock import VaultLock
from .index import update as index_update
from .markdown import (WIKILINK, infer_relationship_type, render_page, significant_words, slugify, split_frontmatter,
                       wikilink_targets)
from .pages import Page, alias_map, iter_pages
from .publish import build_writes, stamp_now
from .review import save_candidate
from .sources import load_sources
from .validate import LineCounter, check_page, independent_sources
from .vault import KIND_DIRS, engine, load_config, now_iso, require_vault

STOPWORDS = {"il", "lo", "la", "i", "gli", "le", "un", "una", "di", "da", "del", "della", "dei", "e", "ed", "per",
             "con", "su", "in", "a", "the", "of", "and", "for", "to", "an", "on", "with"}
MIN_NAME = 4
SEMANTIC_DUPLICATE = 0.88
# link_suggestions() scoring: an exact textual mention
# is the strongest single signal on its own but doesn't reach the top band alone -- only stacking
# with another signal (shared tags, or reaching a hub from a peripheral page) does.
MENTION_SCORE = 4
TAG_OVERLAP_MIN = 2
TAG_OVERLAP_SCORE = 2
HUB_INBOUND_MIN = 8
PERIPHERAL_OUTBOUND_MAX = 2
HUB_REACH_SCORE = 2


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _fold(text)) if w not in STOPWORDS and len(w) > 1}


def _content_pages(vault: Path) -> list[Page]:
    """Knowledge pages: no saved answers, no merge stubs, no unreadable files."""
    return [p for p in iter_pages(vault, ("wiki",))
            if not p.error and not p.merged_into and p.frontmatter.get("kind") in KIND_DIRS
            and p.frontmatter.get("kind") != "query"]


# ---------------- duplicates ----------------

DUPLICATE_BUCKET = 400  # a word shared by more pages than this says nothing about duplicates


def duplicates(vault: Path, *, limit: int = 20) -> dict[str, Any]:
    require_vault(vault)
    pages = _content_pages(vault)
    names = {p.slug: [p.title, *p.aliases] for p in pages}
    words = {p.slug: set().union(*(_words(n) for n in names[p.slug])) for p in pages}
    srcs = {p.slug: {s.get("key") for s in p.frontmatter.get("sources") or [] if isinstance(s, dict)} for p in pages}
    from . import embed

    vectors = embed.head_vectors(vault)
    by_path = {p.path: p.slug for p in pages}
    vec = {by_path[path]: v for path, v in vectors.items() if path in by_path}
    keys = {s: {slugify(n) for n in ns} for s, ns in names.items()}
    # only pairs that share a word, a name or a source can be duplicates by name or sources:
    # bucketing them avoids comparing every page with every other one
    buckets: dict[tuple[str, str], list[str]] = {}
    for s in sorted(names):
        for w in words[s] | keys[s]:
            buckets.setdefault(("w", w), []).append(s)
        for src in srcs[s]:
            buckets.setdefault(("s", str(src)), []).append(s)
    candidates = {pair for members in buckets.values() if len(members) <= DUPLICATE_BUCKET
                  for pair in combinations(members, 2)}
    if vec:
        import numpy as np

        order = sorted(vec)
        sims = np.stack([vec[s] for s in order]) @ np.stack([vec[s] for s in order]).T
        for i, j in zip(*np.nonzero(np.triu(sims >= SEMANTIC_DUPLICATE, 1))):
            candidates.add((order[i], order[j]))
    pairs = []
    for a, b in sorted(tuple(sorted(pair)) for pair in candidates):
        reasons, score = [], 0.0
        wa, wb = words[a], words[b]
        if wa and wb:
            jaccard = len(wa & wb) / len(wa | wb)
            if jaccard >= 0.5:
                reasons.append(f"nomi simili ({jaccard:.0%} parole in comune)")
                score = max(score, jaccard)
        if keys[a] & keys[b]:
            reasons.append("stesso nome o alias")
            score = max(score, 1.0)
        shared = srcs[a] & srcs[b]
        if shared and (srcs[a] <= srcs[b] or srcs[b] <= srcs[a]) and wa & wb:
            reasons.append(f"stesse fonti ({len(shared)})")
            score = max(score, 0.6)
        if a in vec and b in vec:
            cosine = float(vec[a] @ vec[b])
            if cosine >= SEMANTIC_DUPLICATE:
                reasons.append(f"significato molto vicino ({cosine:.2f})")
                score = max(score, cosine)
        if reasons:
            pairs.append({"a": a, "b": b, "titles": [names[a][0], names[b][0]], "score": round(score, 3),
                          "reasons": reasons})
    pairs.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    return {"pairs": pairs[:limit], "semantic": bool(vec),
            "next": "merge one pair: write the merged page, then `maintain propose-merge --keep A --absorb B --page FILE`"}


def _stub(absorbed: Page, keep: Page, vault: Path | None = None) -> str:
    fm = {"title": absorbed.title, "kind": absorbed.frontmatter.get("kind", "concept"),
          "summary": i18n.t(vault, f"Unita in {keep.title}.", f"Merged into {keep.title}."), "mergedInto": keep.slug,
          "merged": time.strftime("%Y-%m-%d"), "updated": time.strftime("%Y-%m-%d")}
    return render_page(fm, f"# {absorbed.title}\n\n"
                       + i18n.t(vault, f"Questa pagina è stata unita in [[{keep.slug}|{keep.title}]].",
                                f"This page has been merged into [[{keep.slug}|{keep.title}]].") + "\n")


def _checked(vault: Path, content: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    config = load_config(vault)
    registry = load_sources(vault)["sources"]
    hard, reasons, fm = check_page(content, config=config, line_count=LineCounter(vault, registry),
                                   source_kind=lambda k: (registry.get(k.split("/", 1)[0]) or {}).get("kind"),
                                   independence=lambda keys: independent_sources(vault, keys, registry))
    if hard:
        raise ValidationError("PAGE_INVALID", "fix the page before submitting it", {"errors": hard})
    return fm, reasons


def propose_merge(vault: Path, *, keep: str, absorb: str, page_file: str) -> dict[str, Any]:
    """Queue for review: the merged text for `keep` plus a stub replacing `absorb`."""
    require_vault(vault)
    content = Path(page_file).read_text("utf-8")
    with VaultLock(vault) as lock:
        pages = {p.slug: p for p in _content_pages(vault)}
        if keep not in pages or absorb not in pages or keep == absorb:
            raise MnemoError("NO_SUCH_PAGE", "keep and absorb must be two different existing pages")
        k, a = pages[keep], pages[absorb]
        fm, body = split_frontmatter(content)
        if not fm:
            raise ValidationError("PAGE_INVALID", "the merged page needs YAML frontmatter")
        # the absorbed page's names become aliases, so [[absorb]] links resolve to the merged page
        aliases = [str(x) for x in fm.get("aliases") or []]
        for name in [a.title, *a.aliases, *k.aliases]:
            if name not in aliases and slugify(name) != keep:
                aliases.append(name)
        fm["aliases"] = aliases
        declared = {s.get("key") for s in fm.get("sources") or [] if isinstance(s, dict)}
        lost = sorted({s.get("key") for s in (a.frontmatter.get("sources") or []) + (k.frontmatter.get("sources") or [])
                       if isinstance(s, dict)} - declared)
        if lost:
            raise ValidationError("SOURCES_LOST", "the merged page must keep the sources of both pages", {"missing": lost})
        content = render_page(fm, body)
        _, reasons = _checked(vault, content)
        reasons = [*reasons, {"code": "manual", "detail": f"unione di [[{absorb}]] in [[{keep}]]"}]
        cid = save_candidate(vault, lock, run_id="maintain", slug=keep, target_path=k.path, base_sha256=k.sha256,
                             content=content, reasons=reasons,
                             extra_writes=[{"target_path": a.path, "base_sha256": a.sha256, "content": _stub(a, k, vault)}])
    return {"candidate": cid, "keep": k.path, "absorbed": a.path,
            "note": "waiting in the review queue: approving it writes both pages in one transaction"}


def submit_page(vault: Path, page_file: str, *, replace: bool = False) -> dict[str, Any]:
    """A page Claude wrote outside compile (e.g. a synthesis): always reviewed by the user."""
    require_vault(vault)
    content = Path(page_file).read_text("utf-8")
    fm, reasons = _checked(vault, content)
    slug = slugify(str(fm["title"]))
    with VaultLock(vault) as lock:
        existing = {p.slug: p for p in iter_pages(vault, ("wiki",)) if not p.error}
        amap = alias_map(list(existing.values()))
        owner = amap.get(slug)
        if owner and not replace:
            raise ConflictError("PAGE_EXISTS", f"[[{owner}]] already covers this title: use --replace or a merge")
        target = existing[owner].path if owner else f"{KIND_DIRS[fm['kind']]}/{slug}.md"
        base = existing[owner].sha256 if owner else None
        broken = [t for t in wikilink_targets(split_frontmatter(content)[1]) if t not in amap and t != slug]
        if broken:
            raise ValidationError("BROKEN_LINKS", "these [[links]] match no page", {"broken": sorted(set(broken))})
        cid = save_candidate(vault, lock, run_id="maintain", slug=owner or slug, target_path=target, base_sha256=base,
                             content=content, reasons=[*reasons, {"code": "manual", "detail": "pagina scritta da Claude"}])
    return {"candidate": cid, "target": target}


# ---------------- missing links ----------------

def _mention_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w\[|])(" + re.escape(name) + r")(?![\w\]])", re.IGNORECASE)


# never touched when adding a link: existing links, citations, URLs, inline code, Markdown link targets
_PROTECTED = re.compile(r"\[\[[^\[\]\n]*\]\]|\^\[[^\[\]\n]*\]|\b[a-z][a-z0-9+.-]*://\S+|`[^`\n]*`|\]\([^)\n]*\)",
                        re.IGNORECASE)


def _blank(line: str) -> str:
    return _PROTECTED.sub(lambda m: " " * len(m.group(0)), line)


def _scannable(lines: list[str]) -> list[int]:
    """Indexes of body lines where a link may be added: no code, headings, quotes or tables."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith(("#", ">", "|")):
            continue
        out.append(i)
    return out


_WORD = re.compile(r"\w+")


def _name_index(pages: list[Page]) -> dict[str, list[tuple[str, int, str]]]:
    """First word of every title/alias -> [(name lower-cased, offset of that word, slug)], longest
    first, so "lock di vault" wins over "lock". Looking names up by word keeps the scan linear in
    the text instead of pages x names x lines."""
    index: dict[str, list[tuple[str, int, str]]] = {}
    for p in pages:
        for name in {p.title, *p.aliases}:
            low = name.lower()
            first = _WORD.search(low)
            if len(name) >= MIN_NAME and _words(name) and first and len(low) == len(name):
                index.setdefault(first.group(), []).append((low, first.start(), p.slug))
    for entries in index.values():
        entries.sort(key=lambda e: (-len(e[0]), e[0], e[2]))
    return index


def _mentions(line: str, index: dict[str, list[tuple[str, int, str]]]):
    """(start, end, slug) of each name mentioned in an already blanked line, not inside a word."""
    low = line.lower()
    if len(low) != len(line):
        return
    for m in _WORD.finditer(low):
        for name, offset, slug in index.get(m.group(), ()):
            start, end = m.start() - offset, m.start() - offset + len(name)
            if start < 0 or not low.startswith(name, start):
                continue
            before = low[start - 1] if start else " "
            after = low[end] if end < len(low) else " "
            if before.isalnum() or before in "_[|" or after.isalnum() or after in "_]":
                continue
            yield start, end, slug
            break


def _confidence_band(score: int) -> str:
    return "alta" if score >= 6 else "media" if score >= TAG_OVERLAP_SCORE + 1 else "bassa"


def link_suggestions(vault: Path, *, limit: int = 100, min_score: int = 3) -> dict[str, Any]:
    """Pages that should probably link to each other but don't yet, scored by a composite of
    signals:
    an exact textual mention, shared tags, and a peripheral page reaching a well-connected hub.
    Tag-only candidates have no natural insertion point in the body text (`text` is None), so
    `apply_links()` reports them but can't place them inline -- they're for the writing session
    to add under a "## Links" section ("## Collegamenti" in Italian vaults) by hand, if it agrees with the suggestion."""
    require_vault(vault)
    pages = _content_pages(vault)
    amap = alias_map(pages)
    index = _name_index(pages)
    outbound: dict[str, set[str]] = {}
    inbound: Counter = Counter()
    for p in pages:
        # a link through an alias ([[Lucchetto]]) already links the page
        targets = {amap.get(t, t) for t in wikilink_targets(p.body)} - {p.slug}
        outbound[p.slug] = targets
        for t in targets:
            inbound[t] += 1

    out = []
    for p in pages:
        linked = outbound[p.slug] | {p.slug}
        candidates: dict[str, dict[str, Any]] = {}
        lines = p.body.split("\n")
        for i in _scannable(lines):
            for start, end, slug in _mentions(_blank(lines[i]), index):
                if slug in linked or slug in candidates:
                    continue
                candidates[slug] = {"score": MENTION_SCORE, "signals": [i18n.t(vault, "menzione esatta", "exact mention")],
                                    "text": lines[i][start:end], "excerpt": lines[i].strip()[:160]}
        p_tags = set(p.frontmatter.get("tags") or [])
        if p_tags:
            for other in pages:
                if other.slug == p.slug or other.slug in linked:
                    continue
                shared = p_tags & set(other.frontmatter.get("tags") or [])
                if len(shared) < TAG_OVERLAP_MIN:
                    continue
                c = candidates.setdefault(other.slug, {"score": 0, "signals": [], "text": None, "excerpt": None})
                c["score"] += TAG_OVERLAP_SCORE
                c["signals"].append(i18n.t(vault, f"{len(shared)} tag condivisi", f"{len(shared)} shared tags"))
        peripheral = len(outbound[p.slug]) <= PERIPHERAL_OUTBOUND_MAX
        if peripheral:
            for slug, c in candidates.items():
                if inbound.get(slug, 0) >= HUB_INBOUND_MIN:
                    c["score"] += HUB_REACH_SCORE
                    c["signals"].append(i18n.t(vault, "da pagina periferica verso un hub", "from a peripheral page to a hub"))
        for slug, c in sorted(candidates.items(), key=lambda kv: -kv[1]["score"]):
            if c["score"] < min_score:
                continue
            out.append({"page": p.slug, "path": p.path, "target": slug, "text": c["text"],
                        "excerpt": c["excerpt"], "score": c["score"], "confidence": _confidence_band(c["score"]),
                        "signals": c["signals"]})
        if len(out) >= limit:
            break
    out.sort(key=lambda s: -s["score"])
    return {"suggestions": out[:limit],
            "next": "apply chosen ones: `maintain apply-links FILE` (dry run), then with --approved <hash>"}

def apply_links(vault: Path, chosen: list[dict[str, str]], *, approved: str | None = None) -> dict[str, Any]:
    """Add [[target|text]] at the first unlinked mention in each chosen page. Dry run unless approved."""
    require_vault(vault)
    with VaultLock(vault) as lock:
        pages = {p.slug: p for p in _content_pages(vault)}
        pages_amap = alias_map(list(pages.values()))
        new: dict[str, str] = {}
        bases: dict[str, str] = {}
        done = []
        for item in chosen:
            page, target, text = pages.get(item.get("page")), item.get("target"), item.get("text")
            if page is None or target not in pages or not text:
                continue
            if page.path not in new:
                data = (Path(vault) / page.path).read_bytes()
                bases[page.path] = sha256_bytes(data)  # the exact text the link is added to
            content = new.get(page.path) or data.decode("utf-8")
            fm, body = split_frontmatter(content)
            if target in {pages_amap.get(t, t) for t in wikilink_targets(body)}:
                continue
            lines = body.split("\n")
            pattern = _mention_pattern(text)
            for i in _scannable(lines):
                clean = _blank(lines[i])  # never inside a link, citation, URL or code
                m = pattern.search(clean)
                if m:
                    kind = infer_relationship_type(lines[i])
                    lines[i] = lines[i][: m.start(1)] + f"[[{target}|{lines[i][m.start(1):m.end(1)]}]]" + lines[i][m.end(1):]
                    relationships = [r for r in (fm.get("relationships") or []) if r.get("target") != target]
                    fm = dict(fm, relationships=[*relationships, {"target": f"[[{target}]]", "type": kind}])
                    new[page.path] = render_page(fm, "\n".join(lines))
                    done.append({"page": page.slug, "target": target, "relationship": kind})
                    break
        if not new:
            return {"changed": 0, "note": "nothing to link (already linked or mention gone)"}
        approval = canonical_hash({"pages": sorted((p, sha256_bytes(c.encode("utf-8"))) for p, c in new.items())})
        if approved is None:
            return {"dry_run": True, "links": done, "pages": sorted(new), "approval_sha256": approval}
        if approved != approval:
            raise ConflictError("PLAN_CHANGED", "pages changed since the dry run; run it again")
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"links-{time.time_ns()}", "operation": "maintain-links",
                  "writes": build_writes(vault, new, f"collegamenti aggiunti: {len(done)}", stamp_now(), bases)}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, list(new), lock)
    return {"dry_run": False, "links": done, "pages": sorted(new)}


# ---------------- recorded link judgment ----------------
# A judgment that two pages/candidates are NOT related ("false homonym", "too weak")
# otherwise exists only as chat text: the next session (or the next person) has no way
# to check it and can only inherit the verdict on trust. Recording it here, with an
# optional citation proving the two are actually different things, gives that judgment
# the same evidence trail a page's own claims already need -- and `lint` surfaces any
# rejection with no citation as `unverified_rejections`, instead of it standing forever
# as an unexamined opinion.

REJECTION_SCHEMA = "mnemoloci.link_rejection.v1"
_CITATION = re.compile(r"^([^#\n]+)#L(\d+)(?:-L(\d+))?$")


def parse_citation(citation: str) -> tuple[str, int, int] | None:
    m = _CITATION.match(citation)
    if not m:
        return None
    start = int(m.group(2))
    return m.group(1), start, int(m.group(3) or start)


def _rejections_dir(vault: Path) -> Path:
    return engine(vault) / "link_rejections"


def reject_link(vault: Path, *, page: str, target: str, reason: str, citation: str | None = None,
                run_id: str = "manual") -> dict[str, Any]:
    """Record that `page` should NOT link to `target`, and why. `citation` (when given) is
    `source/rel/path.md#L10-L14`, checked against the live source the same way a page's own
    citations are (see `lint`'s `bad_citations`/`unverified_rejections`)."""
    require_vault(vault)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("EMPTY_REASON", "reason must not be empty")
    if citation is not None and parse_citation(citation) is None:
        raise ValidationError("BAD_CITATION", "citation must look like source/rel/path.md#L10-L14")
    with VaultLock(vault):
        if page not in {p.slug for p in _content_pages(vault)}:
            raise ValidationError("NO_SUCH_PAGE", f"no page {page!r} in the vault")
        record = {"schema": REJECTION_SCHEMA, "page": page, "target": target, "reason": reason,
                   "citation": citation, "run_id": run_id, "decided": now_iso()}
        digest = sha256_bytes(f"{page}|{target}|{record['decided']}".encode())[:10]
        d = _rejections_dir(vault)
        d.mkdir(parents=True, exist_ok=True)
        atomic_json(d / f"{page}__{slugify(target)}__{digest}.json", record)
    return record


def list_rejections(vault: Path) -> list[dict[str, Any]]:
    require_vault(vault)
    d = _rejections_dir(vault)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            record = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("schema") == REJECTION_SCHEMA:
            record["_file"] = f.name
            out.append(record)
    return out


# ---------------- project hub cohesion ----------------

def _source_prefixes(fm: dict[str, Any]) -> set[str]:
    """Every path prefix (2..N-1 segments for claude-sessions, 1..N-1 for everything else)
    of every source key on a page: a slice of the project it was mined from, never the full
    key (that names a file, not a project). "claude-sessions" alone spans every session of
    every project, so depth 1 is skipped there — it is never a meaningful project boundary."""
    prefixes: set[str] = set()
    for src in fm.get("sources") or []:
        if not isinstance(src, dict) or "key" not in src:
            continue
        parts = str(src["key"]).split("/")
        start = 2 if parts and parts[0] == "claude-sessions" else 1
        for depth in range(start, len(parts)):
            prefixes.add("/".join(parts[:depth]))
    return prefixes


def _topic_words(page: Page) -> set[str]:
    """What a page is about, in words: its title, aliases and the file names of its sources. The
    file names matter because one source folder can hold several projects (a shared `memory/`
    folder), and `blurams-relay-protocol.md` says more about a page than the folder does."""
    parts = [page.title, *page.aliases]
    for src in page.frontmatter.get("sources") or []:
        if isinstance(src, dict) and "key" in src:
            parts.append(re.sub(r"[-_.]+", " ", str(src["key"]).rsplit("/", 1)[-1]))
    return significant_words(" ".join(parts))


def _pick_hub(candidates: list[Page], child: Page | None = None) -> Page | None:
    """Prefer overview pages over mere entities (an overview is meant to be the project's
    table of contents; an entity is usually one specific tool inside it, a poor stand-in for
    "the project"). Several overviews often tie for the same project (one per compile block,
    each a snapshot rather than a sub-topic): linking every child to all of them would
    multiply into thousands of duplicate bullets, so exactly one wins — the longest body,
    the closest thing to "most complete", tied-broken by slug for determinism. When no
    overview is present, None is returned rather than guessing among unrelated entities.

    With a `child`, and more than one overview to choose from, the overview about the same
    thing wins (most topic words in common, then the longest). One that shares no word with
    the child is not its hub: a source folder can mix projects, and the longest overview there
    is not the right one for a page about another. Better no link than a wrong one."""
    overviews = [h for h in candidates if h.frontmatter.get("kind") == "overview"]
    if not overviews:
        return None
    if child is None or len(overviews) == 1:
        return max(overviews, key=lambda h: (len(h.body), h.slug))
    mine = _topic_words(child)
    best = max(overviews, key=lambda h: (len(mine & _topic_words(h)), len(h.body), h.slug))
    return best if mine & _topic_words(best) else None


def link_hub_children(vault: Path, *, approved: str | None = None) -> dict[str, Any]:
    """Every page names the project it was mined from (its sources' path prefix). Give
    each content page a link from the best existing hub (an overview page) mined from the
    same project, when it does not already link to it — the deepest prefix that has ANY
    hub candidate at all wins (an entity-only match stops the search there rather than
    escalating to a broader, less specific prefix; see `_pick_hub`). Only the hub gains a
    bullet; the child page is never touched. Projects with no overview at any depth are
    left alone and reported.

    A project's winning hub only ever gives bullets, never receives one — so once it has
    at least one child, it is itself always zero inbound links away from being unreachable
    by browsing from its own project's pages (the same "collegata a un progetto ma senza
    collegamento" problem this feature exists to fix, one level up, at the hub itself).
    A hub with no inbound link anywhere gets exactly one reciprocal link back, written into
    its most substantial existing (or about-to-be-linked) child, rather than touching every
    child.

    Dry run unless approved. Not sent to review: it only adds a link to something already
    published, the same trust level as `apply-links`/`fix-links`."""
    require_vault(vault)
    with VaultLock(vault) as lock:
        pages = _content_pages(vault)
        by_slug = {p.slug: p for p in pages}
        amap = alias_map(pages)

        hub_by_prefix: dict[str, list[Page]] = {}
        for p in pages:
            if p.frontmatter.get("kind") not in ("overview", "entity"):
                continue
            for prefix in _source_prefixes(p.frontmatter):
                hub_by_prefix.setdefault(prefix, []).append(p)

        # what each hub already links to: a page one of its project's hubs links to has a hub, and is
        # left alone (re-linking it to a "better" hub would only add noise to a structure that works)
        hub_links = {h.slug: {amap.get(t, t) for t in wikilink_targets(h.body)}
                     for hubs in hub_by_prefix.values() for h in hubs}
        additions: dict[str, list[Page]] = {}
        unresolved: set[str] = set()
        for p in pages:
            if p.frontmatter.get("kind") in ("overview", "entity"):
                continue
            prefixes = sorted(_source_prefixes(p.frontmatter), key=len, reverse=True)
            if not prefixes:
                continue
            if any(p.slug in hub_links[h.slug] for prefix in prefixes for h in hub_by_prefix.get(prefix, [])
                   if h.frontmatter.get("kind") == "overview"):
                continue
            chosen = None
            for prefix in prefixes:
                candidates = [h for h in hub_by_prefix.get(prefix, []) if h.slug != p.slug]
                chosen = _pick_hub(candidates, p)
                if chosen is not None:
                    break  # the deepest prefix with an overview wins; entity-only
                # sub-levels (e.g. edildesk/infrastructure) never block the parent
                # project's own overview (edildesk) from being reached
            if chosen is None:
                unresolved.add(min(prefixes, key=len))
                continue
            linked = {amap.get(t, t) for t in wikilink_targets(chosen.body)}
            if p.slug in linked:
                continue
            additions.setdefault(chosen.slug, []).append(p)

        # sibling hubs: when several overview/entity pages share the exact same prefix,
        # only one won _pick_hub and gets linked from its children above — the others
        # (e.g. a project's "part 7" and "part 8" summaries next to its main overview)
        # would otherwise stay orphaned forever, since nothing else names them. Link
        # every runner-up into the winner's own Collegamenti, once per prefix.
        seen_prefixes: set[str] = set()
        for prefix, candidates in hub_by_prefix.items():
            if prefix in seen_prefixes or len(candidates) < 2:
                continue
            seen_prefixes.add(prefix)
            winner = _pick_hub(candidates)
            if winner is None:
                continue
            linked = {amap.get(t, t) for t in wikilink_targets(winner.body)}
            for sibling in candidates:
                if sibling.slug == winner.slug or sibling.slug in linked:
                    continue
                if not _topic_words(sibling) & _topic_words(winner):
                    continue  # same folder, other subject: not a sibling of this hub
                if sibling in additions.get(winner.slug, ()):
                    continue
                additions.setdefault(winner.slug, []).append(sibling)

        # hub reachability: a winning hub gets bullets pointing OUT to its children (above)
        # but nothing points back IN, so it stays an orphan forever once it exists — pick
        # its single most substantial child (existing, or about to be added above) and add
        # the hub as one bullet there, so the hub is reachable from at least one of its own
        # project's pages. Prefer a non-hub child (a real concept/comparison page saying
        # "see also: project overview" reads naturally); a sibling hub only if there is none.
        all_hubs = {h.slug: h for hubs in hub_by_prefix.values() for h in hubs}
        inbound_now: dict[str, int] = {p.slug: 0 for p in pages}
        for pg in pages:
            for target in set(wikilink_targets(pg.body)):
                resolved = amap.get(target)
                if resolved and resolved != pg.slug:
                    inbound_now[resolved] = inbound_now.get(resolved, 0) + 1
        for hub_slug in sorted(all_hubs):
            if inbound_now.get(hub_slug, 0) > 0:
                continue
            hub = all_hubs[hub_slug]
            existing_child_slugs = {amap.get(t, t) for t in wikilink_targets(hub.body)} - {hub_slug}
            children_by_slug = {s: by_slug[s] for s in existing_child_slugs if s in by_slug}
            children_by_slug.update({c.slug: c for c in additions.get(hub_slug, [])})
            if not children_by_slug:
                continue
            non_hub = [c for c in children_by_slug.values() if c.frontmatter.get("kind") not in ("overview", "entity")]
            pool = non_hub or list(children_by_slug.values())
            rep = max(pool, key=lambda c: (len(c.body), c.slug))
            if hub_slug in {amap.get(t, t) for t in wikilink_targets(rep.body)}:
                continue
            additions.setdefault(rep.slug, []).append(hub)

        if not additions:
            return {"changed": 0, "unresolved_projects": sorted(unresolved),
                    "note": "no hub is missing a link to one of its own pages"}

        new: dict[str, str] = {}
        bases: dict[str, str] = {}
        counts: dict[str, int] = {}
        for hub_slug, children in additions.items():
            hub = by_slug[hub_slug]
            lines = hub.body.rstrip("\n").split("\n")
            bullets = [f"- [[{c.slug}]]" for c in sorted(children, key=lambda c: c.slug)]
            heading_idx = next((i for i, line in enumerate(lines) if line.strip() in i18n.LINKS_HEADINGS), None)
            if heading_idx is None:
                lines += ["", i18n.links_heading(vault), *bullets]
            else:
                insert_at = heading_idx + 1
                while insert_at < len(lines) and lines[insert_at].strip().startswith("-"):
                    insert_at += 1
                lines[insert_at:insert_at] = bullets
            new[hub.path] = render_page(hub.frontmatter, "\n".join(lines))
            bases[hub.path] = hub.sha256
            counts[hub_slug] = len(children)

        total = sum(counts.values())
        approval = canonical_hash({"pages": sorted((p, sha256_bytes(c.encode("utf-8"))) for p, c in new.items())})
        if approved is None:
            return {"dry_run": True, "hubs": counts, "total_links": total,
                    "unresolved_projects": sorted(unresolved), "approval_sha256": approval}
        if approved != approval:
            raise ConflictError("PLAN_CHANGED", "pages changed since the dry run; run it again")
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"hublinks-{time.time_ns()}",
                  "operation": "maintain-hub-links",
                  "writes": build_writes(vault, new, f"collegamenti hub aggiunti: {total}", stamp_now(), bases)}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, list(new), lock)
    return {"dry_run": False, "hubs": counts, "total_links": total, "unresolved_projects": sorted(unresolved)}


# ---------------- deterministic link repair ----------------

def _repair_target(target: str, keys: dict[str, str]) -> str | None:
    """The single page a broken [[target]] can only mean: a longer name that starts with it, or one
    whose words contain all of its words. A more specific target (e.g. journal-delle-scritture) is
    never folded into a general page (journal). None when zero or several pages qualify."""
    words = set(target.split("-"))
    found = {slug for key, slug in keys.items()
             if key.startswith(target + "-") or (len(words) > 1 and words <= set(key.split("-")))}
    return found.pop() if len(found) == 1 else None


def fix_links(vault: Path, *, approved: str | None = None) -> dict[str, Any]:
    """Repair broken links whose target is unambiguous, keeping the visible text. Dry run unless approved; ambiguous ones are only reported."""
    require_vault(vault)
    with VaultLock(vault) as lock:
        pages = [p for p in iter_pages(vault, ("wiki",)) if not p.error]
        keys = alias_map(pages)
        new, fixes, ambiguous = {}, [], []
        for page in pages:
            if page.merged_into:
                continue

            def repl(m: re.Match) -> str:
                raw = m.group(1).strip()
                target = slugify(raw.split("/")[-1])
                if target in keys:
                    return m.group(0)
                dest = _repair_target(target, keys)
                if dest is None:
                    ambiguous.append({"page": page.slug, "link": raw})
                    return m.group(0)
                label = m.group(0)[2:-2].split("|", 1)[1] if "|" in m.group(0) else raw
                fixes.append({"page": page.slug, "from": raw, "to": dest})
                return f"[[{dest}|{label}]]"

            body = WIKILINK.sub(repl, page.body)
            if body != page.body:
                new[page.path] = render_page(page.frontmatter, body)
        if not new:
            return {"changed": 0, "unresolved": ambiguous, "note": "no broken link has a single obvious target"}
        approval = canonical_hash({"fix": sorted((p, sha256_bytes(c.encode("utf-8"))) for p, c in new.items())})
        if approved is None:
            return {"dry_run": True, "fixes": fixes, "unresolved": ambiguous, "approval_sha256": approval}
        if approved != approval:
            raise ConflictError("PLAN_CHANGED", "pages changed since the dry run; run it again")
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"fixlinks-{time.time_ns()}", "operation": "maintain-fix-links",
                  "writes": build_writes(vault, new, f"link riparati: {len(fixes)}", stamp_now(),
                                              {p.path: p.sha256 for p in pages if p.path in new})}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, list(new), lock)
    return {"dry_run": False, "fixes": fixes, "unresolved": ambiguous}


# ---------------- validity in time ----------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def set_validity(vault: Path, slug: str, *, valid_from: str | None = None, valid_until: str | None = None,
                 refresh_due: str | None = None, superseded_by: str | None = None) -> dict[str, Any]:
    """Record when a page's content holds. Nothing is removed: an expired page stays, marked."""
    require_vault(vault)
    changes = {"validFrom": valid_from, "validUntil": valid_until, "refreshDue": refresh_due}
    for field, value in changes.items():
        if value and not _DATE.match(value):
            raise ValidationError("BAD_DATE", f"{field} must be YYYY-MM-DD")
    if not any(changes.values()) and not superseded_by:
        raise ValidationError("NOTHING_TO_SET", "give --from, --until, --refresh-due or --superseded-by")
    with VaultLock(vault) as lock:
        pages = {p.slug: p for p in iter_pages(vault, ("wiki",)) if not p.error}
        page = pages.get(slug)
        if page is None:
            raise MnemoError("NO_SUCH_PAGE", f"no wiki page {slug}")
        if superseded_by and superseded_by not in pages:
            raise MnemoError("NO_SUCH_PAGE", f"no wiki page {superseded_by}")
        fm = dict(page.frontmatter)
        fm.update({k: v for k, v in changes.items() if v})
        if superseded_by:
            fm["supersededBy"] = superseded_by
            fm.setdefault("validUntil", time.strftime("%Y-%m-%d"))
        if fm.get("validFrom") and fm.get("validUntil") and str(fm["validUntil"]) < str(fm["validFrom"]):
            raise ValidationError("BAD_DATE", "validUntil is before validFrom")
        content = render_page(fm, page.body)
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"validity-{time.time_ns()}", "operation": "validity",
                  "writes": build_writes(vault, {page.path: content}, f"validità aggiornata: [[{slug}]]", stamp_now(),
                                              {page.path: page.sha256})}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, [page.path], lock)
    return {"path": page.path, **{k: fm.get(k) for k in ("validFrom", "validUntil", "refreshDue", "supersededBy")}}


# ---------------- synthesis ----------------

def synthesis(vault: Path, *, limit: int = 15) -> dict[str, Any]:
    """Pairs cited together by other pages (or built from the same sources) that never link each other."""
    require_vault(vault)
    pages = _content_pages(vault)
    amap = alias_map(pages)
    slugs = {p.slug for p in pages}
    links = {p.slug: {amap[t] for t in wikilink_targets(p.body) if t in amap} & slugs for p in pages}
    srcs = {p.slug: {s.get("key") for s in p.frontmatter.get("sources") or [] if isinstance(s, dict)} for p in pages}
    titles = {p.slug: p.title for p in pages}
    together: dict[tuple[str, str], list[str]] = {}
    for p in pages:
        for a, b in combinations(sorted(links[p.slug] - {p.slug}), 2):
            together.setdefault((a, b), []).append(p.slug)
    out = []
    for a, b in combinations(sorted(slugs), 2):
        if b in links[a] or a in links[b]:
            continue
        cited = together.get((a, b), [])
        shared = srcs[a] & srcs[b]
        score = 2 * len(cited) + len(shared)
        if score >= 2:
            out.append({"a": a, "b": b, "titles": [titles[a], titles[b]], "score": score,
                        "cited_together_in": cited, "shared_sources": sorted(shared)[:5]})
    out.sort(key=lambda x: (-x["score"], x["a"], x["b"]))
    return {"pairs": out[:limit],
            "next": "write a comparison/overview page linking both, then `maintain submit-page FILE` (goes to review)"}
