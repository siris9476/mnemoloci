"""Does the cited source text actually support the sentence that cites it?

The compile pipeline checks that a citation resolves to a real line range, and once checked that the
sentence appeared verbatim in it; that second check was dropped because it rejected faithful
paraphrase and translation. This one never blocks anything: it only reports the citations where the
cited lines share too little with the sentence, so a person can look at those few instead of all.

What counts as support:
- numbers, versions, identifiers and `code` in the sentence must appear in the cited lines: they are
  the facts a paraphrase or translation cannot change, so this works across languages;
- for a page written in the source's own language, the sentence's other words are also compared.
A sentence with nothing to compare is reported as `unchecked`, not as a pass."""

import re
from pathlib import Path
from typing import Any

from .markdown import CITATION, significant_words, split_frontmatter
from .pages import iter_pages
from .sources import load_sources
from .validate import ChunkTextReader
from .vault import require_vault

WORD_SUPPORT = 0.5  # share of a sentence's words found in the cited lines, same-language pages only
_MARKER = re.compile(r"\^\[[^\[\]\n]*\]")
_LINK = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")  # [[slug|label]] reads as label: the slug is not in the prose
_CODE = re.compile(r"`([^`\n]+)`")
_ANCHOR = re.compile(r"\d[\d.,:/-]*\d|\d|[A-Za-z]+(?:[_.:-][A-Za-z0-9]+)+|\b[a-z]+[A-Z][A-Za-z0-9]*")


def _anchors(sentence: str) -> set[str]:
    found = {m.strip(".,:;").casefold() for m in _CODE.findall(sentence)}
    plain = _CODE.sub(" ", sentence)
    found |= {m.group(0).strip(".,:;-").casefold() for m in _ANCHOR.finditer(plain)}
    return {a for a in found if a}


def judge(sentence: str, chunk: str, *, same_language: bool) -> dict[str, Any]:
    """support in 0..1, or None when the sentence has nothing to compare; `missing` lists what was not found."""
    if not chunk.strip():
        return {"support": 0.0, "missing": ["(the cited lines are empty)"]}
    folded = chunk.casefold()
    anchors = _anchors(sentence)
    missing = sorted(a for a in anchors if a not in folded)
    parts: list[float] = []
    if anchors:
        parts.append(1 - len(missing) / len(anchors))
    if same_language:
        words = significant_words(sentence)
        if words:
            have = significant_words(chunk)
            absent = sorted(w for w in words if w not in have)
            parts.append(1 - len(absent) / len(words))
            if len(absent) / len(words) > 1 - WORD_SUPPORT:
                missing += absent
    if not parts:
        return {"support": None, "missing": []}
    return {"support": round(min(parts), 2), "missing": missing}


FLAG_BELOW = 0.3
# Calibrated on the real vault: moving each citation to other lines of the same file, 97% of those
# fall under 0.3 while 10% of the real ones do (the few hundred worth a look, many of them genuine:
# cited lines that are blank or only a session heading).


def page_weak(reader: ChunkTextReader, path: str, body: str, *, translated: bool, source: str | None = None,
              below: float = FLAG_BELOW) -> tuple[int, int, list[dict[str, Any]]]:
    """(citations, unchecked, weak ones) of one page's text."""
    total = unchecked = 0
    weak: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.lstrip().startswith(">"):  # the original-language block repeats the same citations
            continue
        sentence = _LINK.sub(lambda m: m.group(2) or m.group(1), _MARKER.sub("", line)).strip(" -*\t")
        cited = [(m.group(1).strip(), int(m.group(2)), int(m.group(3) or m.group(2))) for m in CITATION.finditer(line)]
        texts = [reader(*c) for c in cited]
        # one sentence often rests on several ranges: it is judged against all of them together
        union = "\n".join(t for t in texts if t)
        for (key, start, end), chunk in zip(cited, texts):
            if source and key.partition("/")[0] != source:
                continue
            if chunk is None:
                continue  # a missing file or range is lint's job
            total += 1
            verdict = judge(sentence, union, same_language=not translated)
            if verdict["support"] is None:
                unchecked += 1
            elif verdict["support"] < below:
                weak.append({"page": path, "citation": f"{key}#L{start}-L{end}", "support": verdict["support"],
                             "not_found": verdict["missing"][:8], "sentence": sentence[:200]})
    return total, unchecked, weak


def weak_in_texts(vault: Path, pages: dict[str, str], *, below: float = FLAG_BELOW, limit: int = 50) -> dict[str, Any]:
    """The same check on page texts that are not in the vault yet (a compile run's pages)."""
    reader = ChunkTextReader(vault, load_sources(vault)["sources"])
    total = unchecked = 0
    weak: list[dict[str, Any]] = []
    for path, content in sorted(pages.items()):
        fm, body = split_frontmatter(content)
        t, u, w = page_weak(reader, path, body, translated=bool((fm or {}).get("originalLanguage")), below=below)
        total, unchecked, weak = total + t, unchecked + u, weak + w
    weak.sort(key=lambda w: (w["support"], w["page"]))
    return {"citations": total, "unchecked": unchecked, "weak": len(weak), "items": weak[:limit]}


def check(vault: Path, *, source: str | None = None, below: float = FLAG_BELOW, limit: int = 50) -> dict[str, Any]:
    """Every citation in the wiki, weakest first. `below` keeps only those under that support."""
    require_vault(vault)
    reader = ChunkTextReader(vault, load_sources(vault)["sources"])
    total = unchecked = 0
    weak: list[dict[str, Any]] = []
    for page in iter_pages(vault, ("wiki",)):
        if page.error:
            continue
        t, u, w = page_weak(reader, page.path, page.body, translated=bool(page.frontmatter.get("originalLanguage")),
                            source=source, below=below)
        total, unchecked, weak = total + t, unchecked + u, weak + w
    weak.sort(key=lambda w: (w["support"], w["page"]))
    return {"citations": total, "unchecked": unchecked, "weak": len(weak), "items": weak[:limit]}
