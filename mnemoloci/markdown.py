import re
import unicodedata
from typing import Any, NamedTuple

import yaml

from .core.errors import ValidationError

# "[" is excluded inside the brackets: a link text never contains it, and it keeps the match linear
# on hostile input such as "[[a" repeated thousands of times
WIKILINK = re.compile(r"\[\[([^\[\]\|#\n]+)(?:#[^\[\]\|\n]*)?(?:\|[^\[\]\n]*)?\]\]")
# Obsidian inline footnote used as a citation: ^[source/key#L10-L14]. #V.. / #R are an optional
# per-citation validity window (validFrom..validUntil) and refresh-due date, e.g.
# ^[source/key#L10-L14#V2025-01-01..2026-06-30#R2026-03-01] -- each letter disambiguates its own
# branch (like #L already did), so a plain citation without either still matches unchanged and
# the match stays linear (no branch can be confused with another).
CITATION = re.compile(
    r"\^\[([^\[\]\n#]+)#L(\d+)(?:-L(\d+))?"
    r"(?:#V(\d{4}-\d{2}-\d{2})?\.\.(\d{4}-\d{2}-\d{2})?)?"
    r"(?:#R(\d{4}-\d{2}-\d{2}))?\]"
)
MAX_SLUG = 80
_BASE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # libyaml is ~10x faster when present


class _Loader(_BASE_LOADER):
    """Keep timestamps as the text the author wrote (YAML 1.2 core behaviour).

    PyYAML (YAML 1.1) turns `2026-06-30T14:00:00Z` into a datetime: re-rendering
    then rewrites the author's value and JSON serialisation (API, MCP) fails.
    Same fix as Google's OKF reference agent (bundle/document.py).
    """


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in _BASE_LOADER.yaml_implicit_resolvers.items()
}
_LOADER = _Loader


# letters that carry no accent to strip, so NFKD alone would not map them to Latin
_LATIN_EXTRA = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "ħ": "h", "ı": "i", "æ": "ae", "œ": "oe", "þ": "th", "ð": "d"})


def slugify(title: str) -> str:
    """Lower-case, accents removed, words joined by "-". Letters of every script are kept
    (Москва -> москва, 東京 -> 東京): only titles with no letter at all become "untitled".

    A slug over MAX_SLUG is cut at the last whole word, never mid-word: two related concepts
    whose titles agree up to that word (but diverge after) would otherwise truncate to two
    different byte offsets and silently stop matching each other's [[wikilinks]]."""
    decomposed = unicodedata.normalize("NFKD", title.casefold().translate(_LATIN_EXTRA))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub(r"[\W_]+", "-", unicodedata.normalize("NFC", plain)).strip("-")
    if len(slug) > MAX_SLUG:
        cut = slug[:MAX_SLUG]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug.rstrip("-") or "untitled"


_STOPWORDS = {"il", "lo", "la", "i", "gli", "le", "un", "una", "di", "da", "del", "della", "e", "per", "con", "su", "in",
              "a", "che", "non", "come", "cosa", "questo", "questa", "fai", "puoi", "the", "of", "and", "to", "is", "it"}


def significant_words(text: str) -> set[str]:
    """Accent-free, lower-case words of 3+ letters/digits without the most common Italian/English
    function words: the unit for "do these two texts talk about the same thing" checks."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return {w for w in re.findall(r"[a-z0-9]{3,}", folded) if w not in _STOPWORDS}


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    text = text.lstrip("﻿")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None, text
    normalized = text.replace("\r\n", "\n")
    end = normalized.find("\n---\n", 4)
    if end == -1:
        if normalized.rstrip().endswith("\n---"):
            end = normalized.rstrip().rfind("\n---")
        else:
            raise ValidationError("BAD_FRONTMATTER", "frontmatter is not closed")
    raw = normalized[4:end]
    try:
        data = yaml.load(raw, Loader=_LOADER) or {}
    except yaml.YAMLError as exc:
        raise ValidationError("BAD_FRONTMATTER", f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("BAD_FRONTMATTER", "frontmatter must be a mapping")
    return _plain(data), normalized[end + 5 :]


def _plain(data: dict[str, Any]) -> dict[str, Any]:
    """Only JSON types leave here. Explicit tags (!!timestamp, !!binary, !!set) become text, and
    YAML aliases are refused: a few hundred bytes of nested aliases expand to gigabytes once the
    frontmatter is serialised for the API or MCP."""
    seen: set[int] = set()
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            raise ValidationError("BAD_FRONTMATTER", "YAML anchors/aliases (&name, *name) are not supported")
        seen.add(id(node))
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in list(items):
            if isinstance(value, (dict, list)):
                stack.append(value)
            elif not (value is None or isinstance(value, (str, int, float, bool))):
                if isinstance(value, (set, tuple, frozenset)):
                    node[key] = sorted(map(str, value))
                else:
                    node[key] = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        if isinstance(node, dict) and any(not isinstance(k, str) for k in node):
            for k in [k for k in node if not isinstance(k, str)]:
                node[str(k)] = node.pop(k)
    return data


FIELD_ORDER = ("title", "kind", "summary", "aliases", "sources", "confidence", "provenanceState", "contradictedBy", "updated")


def render_page(frontmatter: dict[str, Any], body: str) -> str:
    ordered = {k: frontmatter[k] for k in FIELD_ORDER if k in frontmatter}
    ordered.update((k, v) for k, v in frontmatter.items() if k not in ordered)
    dumped = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{dumped}---\n\n{body.strip()}\n"


def wikilink_targets(body: str) -> list[str]:
    return [slugify(m.group(1).strip().split("/")[-1]) for m in WIKILINK.finditer(body)]


# Ordered so a more specific cue is tried before "uses", the broadest verb group. Each pattern has an
# English and an Italian cue list, so the type is found whatever language the page is written in.
# First match wins.
_RELATIONSHIP_PATTERNS = [
    ("extends", re.compile(r"(?i)\b(extends|builds on|generali[sz]es|expands|estende|si basa su|generalizza|amplia)\b")),
    ("implements", re.compile(r"(?i)\b(implements|is an implementation of|implementa|è un'implementazione di|e un'implementazione di)\b")),
    ("contradicts", re.compile(r"(?i)\b(contradicts|conflicts with|is at odds with|refutes|disproves|contraddice|è in contrasto con|e in contrasto con|confuta|smentisce)\b")),
    ("derived_from", re.compile(r"(?i)\b(derives from|is derived from|adapted from|taken from|deriva da|adattato da|ricavato da)\b")),
    ("replaces", re.compile(r"(?i)\b(replaces|supersedes|deprecated by|sostituisce|rimpiazza|deprecat[oa] da|supersed[ea])\b")),
    ("uses", re.compile(r"(?i)\b(uses|utili[sz]es|relies on|depends on|requires|usa|utilizza|si appoggia a|dipende da|richiede)\b")),
]


def infer_relationship_type(sentence: str) -> str:
    """The semantic relationship a sentence implies between two linked pages, from the verb
    cueing it -- deterministic pattern matching, never an LLM call. Falls back to the untyped
    default when nothing more specific matches (shared tags, no directional verb in the text)."""
    for kind, pattern in _RELATIONSHIP_PATTERNS:
        if pattern.search(sentence):
            return kind
    return "related_to"


def claim_line_for_citation(body: str, match: re.Match) -> str:
    """The Markdown line the citation marker sits in, stripped of any leading list bullet or
    blockquote marker (`-`, `*`, `>`) and the marker itself -- pages render one claim per
    line/bullet, including inside a collapsible "> [!quote]- Originale" block, so the containing
    line is the claim the citation is meant to support. Used for the verbatim citation check: the
    extracted claim must actually appear in the cited source chunk, not just resolve to a valid
    line range."""
    start = body.rfind("\n", 0, match.start()) + 1
    end = body.find("\n", match.end())
    if end == -1:
        end = len(body)
    line = body[start:end]
    line = line[: match.start() - start] + line[match.end() - start :]  # drop the marker itself
    return re.sub(r"^\s*[-*>]+\s*", "", line).strip()


_MD_SYNTAX = re.compile(r"[*_`]+|\[([^\]]*)\]\([^)]*\)")
_WS = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?])")


def normalize_for_compare(text: str) -> str:
    """Fold case/accents (same normalization the search index uses) and strip Markdown emphasis/
    link syntax, so a citation's claim text can be compared against plain source prose without
    formatting differences alone causing a false mismatch. Also drops the space a removed citation
    marker leaves behind before trailing punctuation (`rollback ^[cite].` -> `rollback .` ->
    `rollback.`), since that space was never part of the claim itself."""
    text = _MD_SYNTAX.sub(lambda m: m.group(1) or "", text)
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    folded = _WS.sub(" ", folded).strip()
    return _SPACE_BEFORE_PUNCT.sub(r"\1", folded)


def citations(body: str) -> list[tuple[str, int, int]]:
    found = []
    for m in CITATION.finditer(body):
        start = int(m.group(2))
        found.append((m.group(1).strip(), start, int(m.group(3) or start)))
    return found


class Citation(NamedTuple):
    key: str
    start: int
    end: int
    valid_from: str | None
    valid_until: str | None
    refresh_due: str | None


def citations_with_validity(body: str) -> list[Citation]:
    """Same citations as `citations()`, plus the optional per-citation #V/#R fields -- kept
    separate (rather than changing `citations()` itself) because several call sites already
    unpack it as a plain 3-tuple; only the validity lint check needs the extra fields."""
    found = []
    for m in CITATION.finditer(body):
        start = int(m.group(2))
        found.append(Citation(m.group(1).strip(), start, int(m.group(3) or start), m.group(4), m.group(5), m.group(6)))
    return found


_MALFORMED_VALIDITY = re.compile(r"\^\[[^\[\]\n]*#[VR][^\]\n]*\]")


def malformed_validity_citations(body: str) -> list[str]:
    """A `#V`/`#R` that doesn't match CITATION's strict date format -- e.g. a typo'd date -- would
    otherwise just silently stop being a citation at all (the whole `^[...]` fails to match and
    is left as plain text, same as any other broken citation). Never touches the text; only used
    to surface a warning instead of it disappearing unnoticed."""
    return [m.group(0) for m in _MALFORMED_VALIDITY.finditer(body) if not CITATION.fullmatch(m.group(0))]
