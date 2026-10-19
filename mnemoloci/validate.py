import re
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import urlsplit

from .core.errors import ValidationError
from .markdown import CITATION, citations, claim_line_for_citation, normalize_for_compare, split_frontmatter
from .sources import EXTERNAL_KINDS, resolve_root
from .vault import KIND_DIRS

PROVENANCE_STATES = {"extracted", "merged", "inferred", "ambiguous"}
ENTITY_TYPES = {"person", "organization", "place", "tool", "project", "event"}
DATE_FIELDS = ("validFrom", "validUntil", "refreshDue")  # optional validity in time (YYYY-MM-DD)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ORIGINAL_BLOCK = r"^> \[!\w+\][-+]?[^\n]*\(%s\)\s*$"
_SHA = re.compile(r"^[0-9a-f]{64}$")


class LineCounter:
    """Resolves 'source/rel/path' keys to line counts of the current source file."""

    def __init__(self, vault: Path, registry: dict[str, Any]) -> None:
        self.vault = vault
        self.registry = registry
        self._cache: dict[str, int | None] = {}

    def __call__(self, key: str) -> int | None:
        if key not in self._cache:
            self._cache[key] = self._count(key)
        return self._cache[key]

    def _count(self, key: str) -> int | None:
        name, _, rel = key.partition("/")
        entry = self.registry.get(name)
        if entry is None or not rel:
            return None
        path = resolve_root(self.vault, entry) / rel
        try:
            return len(path.read_text("utf-8", errors="replace").splitlines())
        except OSError:
            return None


class ChunkTextReader:
    """Resolves 'source/rel/path'#Lstart-Lend to the literal source text, for the verbatim
    citation check: same root-resolution as LineCounter, caching a file's lines once per key
    instead of once per citation."""

    def __init__(self, vault: Path, registry: dict[str, Any]) -> None:
        self.vault = vault
        self.registry = registry
        self._cache: dict[str, list[str] | None] = {}

    def __call__(self, key: str, start: int, end: int) -> str | None:
        if key not in self._cache:
            self._cache[key] = self._lines(key)
        lines = self._cache[key]
        return None if lines is None else "\n".join(lines[max(0, start - 1) : end])

    def _lines(self, key: str) -> list[str] | None:
        name, _, rel = key.partition("/")
        entry = self.registry.get(name)
        if entry is None or not rel:
            return None
        path = resolve_root(self.vault, entry) / rel
        try:
            return path.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return None


class CitationProblem(NamedTuple):
    key: str
    kind: str  # "undeclared" | "unreadable" | "out_of_range" | "text_mismatch"
    start: int
    end: int
    total: int | None


def citation_problems(
    body: str, declared: dict[str, str] | set[str], *,
    line_count: Callable[[str], int | None],
    chunk_text: Callable[[str, int, int], str | None] | None = None,
) -> list[CitationProblem]:
    """Every problem with the page's citations -- shared by check_page (compile/maintain, turns
    each into a hold reason) and editor.save (manual edits, turns each into a warning) so the two
    write paths can't silently drift apart on what counts as a bad citation. Does not check whether
    the body has any citations at all; that page-level requirement is compile-pipeline-specific.
    chunk_text=None skips the verbatim check (still checks declared/range)."""
    problems: list[CitationProblem] = []
    for key, start, end in citations(body):
        if key not in declared:
            problems.append(CitationProblem(key, "undeclared", start, end, None))
            continue
        total = line_count(key)
        if total is None:
            problems.append(CitationProblem(key, "unreadable", start, end, None))
        elif not 1 <= start <= end <= total:
            problems.append(CitationProblem(key, "out_of_range", start, end, total))
    if chunk_text is not None:
        for m in CITATION.finditer(body):
            key, start = m.group(1).strip(), int(m.group(2))
            end = int(m.group(3) or start)
            total = line_count(key)
            if key not in declared or total is None or not 1 <= start <= end <= total:
                continue  # already reported above
            source_text = chunk_text(key, start, end)
            claim = claim_line_for_citation(body, m)
            if source_text and claim and normalize_for_compare(claim) not in normalize_for_compare(source_text):
                problems.append(CitationProblem(key, "text_mismatch", start, end, total))
    return problems


def independent_sources(vault: Path, keys: list[str], registry: dict[str, Any]) -> int:
    """How many independent voices back a page: one project
    is one voice however many files it has; captured documents count one each, web clips one per site."""
    units = set()
    for key in keys:
        name, _, rel = key.partition("/")
        entry = registry.get(name) or {}
        if entry.get("kind") == "project":
            units.add(name)
            continue
        unit = f"{name}:{rel}"
        if name == "clips":
            try:
                fm, _ = split_frontmatter((resolve_root(vault, entry) / rel).read_text("utf-8"))
                site = urlsplit(str((fm or {}).get("final_url") or (fm or {}).get("url") or "")).hostname
                unit = f"site:{site.removeprefix('www.')}" if site else unit
            except (OSError, ValidationError):
                pass
        units.add(unit)
    return len(units)


def check_page(
    content: str,
    *,
    config: dict[str, Any],
    line_count: Callable[[str], int | None],
    source_kind: Callable[[str], str | None],
    required_sources: dict[str, str] | None = None,
    original_languages: list[str] | None = None,
    independence: Callable[[list[str]], int] | None = None,
    chunk_text: Callable[[str, int, int], str | None] | None = None,
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    """Return (hard_errors, hold_reasons, frontmatter).

    Hard errors block submission (the agent must fix the page). Hold reasons
    route the page to the review queue when their mode is enabled in config.
    """
    hard: list[str] = []
    reasons: list[dict[str, str]] = []

    def hold(code: str, detail: str) -> None:
        reasons.append({"code": code, "detail": detail})

    try:
        fm, body = split_frontmatter(content)
    except ValidationError as exc:
        return [exc.message], [], {}
    if fm is None:
        return ["page has no YAML frontmatter"], [], {}
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        hard.append("title must be a non-empty string (max 200 chars)")
    if fm.get("kind") not in KIND_DIRS:
        hard.append(f"kind must be one of {sorted(KIND_DIRS)}")
    if not body.strip():
        hard.append("page body is empty")
    if hard:
        return hard, [], fm

    summary = fm.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        hold("schema-violating", "summary missing or longer than 500 chars")
    aliases = fm.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        hold("schema-violating", "aliases must be a list of strings")
    if fm.get("provenanceState") not in PROVENANCE_STATES:
        hold("schema-violating", f"provenanceState must be one of {sorted(PROVENANCE_STATES)}")
    if "updated" not in fm:
        hold("schema-violating", "updated is missing")
    if "entityType" in fm and fm["entityType"] not in ENTITY_TYPES:
        hold("schema-violating", f"entityType must be one of {sorted(ENTITY_TYPES)}")
    for field in DATE_FIELDS:
        if field in fm and not _DATE.match(str(fm[field])):
            hold("schema-violating", f"{field} must be a date YYYY-MM-DD")
    if fm.get("validFrom") and fm.get("validUntil") and str(fm["validUntil"]) < str(fm["validFrom"]):
        hold("schema-violating", "validUntil is before validFrom")
    contradicted = fm.get("contradictedBy", [])
    if not isinstance(contradicted, list):
        hold("schema-violating", "contradictedBy must be a list")
    elif contradicted:
        hold("contradicted", f"in tension with: {', '.join(map(str, contradicted))}")

    review = config["review"]
    confidence = fm.get("confidence")
    if confidence is None:
        if review["treat_missing_confidence_as"] == "hold":
            hold("low-confidence", "confidence missing")
    elif isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        hold("schema-violating", "confidence must be a number within 0..1")
    elif confidence < review["low_confidence_threshold"]:
        hold("low-confidence", f"confidence {confidence} < {review['low_confidence_threshold']}")

    sources = fm.get("sources")
    declared: dict[str, str] = {}
    if not isinstance(sources, list) or not sources:
        hold("provenance-violating", "sources must be a non-empty list of {key, sha256}")
    else:
        for item in sources:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not _SHA.match(str(item.get("sha256", ""))):
                hold("provenance-violating", f"malformed source entry {item!r}")
                continue
            declared[item["key"]] = item["sha256"]
    for key, sha in (required_sources or {}).items():
        if declared.get(key) != sha:
            hold("provenance-violating", f"source {key} (sha {sha[:12]}…) not declared in sources")

    if not citations(body):
        hold("provenance-violating", "no citations ^[source#Lx-Ly] in body")
    for p in citation_problems(body, declared, line_count=line_count, chunk_text=chunk_text):
        if p.kind == "undeclared":
            hold("provenance-violating", f"citation {p.key} is not a declared source")
        elif p.kind == "unreadable":
            hold("provenance-violating", f"source file for {p.key} is not readable")
        elif p.kind == "out_of_range":
            hold("provenance-violating", f"citation {p.key}#L{p.start}-L{p.end} outside 1..{p.total}")
        elif p.kind == "text_mismatch":
            hold("provenance-violating", f"citation {p.key}#L{p.start}-L{p.end} text does not match source")

    target = config.get("language", {}).get("target")
    if target and fm.get("language") != target:
        hold("schema-violating", f"language must be '{target}' (the vault's target language)")
    for lang in original_languages or []:
        if not re.search(ORIGINAL_BLOCK % re.escape(lang), body, re.MULTILINE):
            label = "Originale" if target == "it" else "Original"
            hold("schema-violating", f"missing collapsible original-text block '> [!quote]- {label} ({lang})'")

    if fm.get("critical") is True and independence is not None and independence(list(declared)) < 2:
        hold("single-source", "marked critical but backed by fewer than 2 independent sources")
    if any(source_kind(k) in EXTERNAL_KINDS for k in declared):
        hold("external-source", "built from an external source (web clip, screenshot or import)")

    enabled = set(review["hold"])
    return [], [r for r in reasons if r["code"] in enabled], fm
