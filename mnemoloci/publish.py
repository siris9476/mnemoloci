"""Builds the transaction writes that publish pages plus the generated index and log."""

import time
from pathlib import Path
from typing import Any

from . import i18n
from .core.hashing import sha256_bytes
from .markdown import split_frontmatter
from .pages import iter_pages

KIND_LABELS = [
    ("concept", "Concetti"),
    ("entity", "Entità"),
    ("comparison", "Confronti"),
    ("overview", "Panoramiche"),
    ("query", "Risposte salvate"),
]
KIND_LABELS_EN = [
    ("concept", "Concepts"),
    ("entity", "Entities"),
    ("comparison", "Comparisons"),
    ("overview", "Overviews"),
    ("query", "Saved answers"),
]


def _current(vault: Path, rel: str) -> tuple[str | None, bytes | None]:
    path = Path(vault) / rel
    if not path.is_file():
        return None, None
    data = path.read_bytes()
    return sha256_bytes(data), data


_UNSET = object()


def _write(vault: Path, rel: str, content: str, base: Any = _UNSET) -> dict[str, Any]:
    """base: the sha256 of the version the new content was derived from (None: it did not exist).
    With it, the transaction refuses if the file changed since (an edit in Obsidian is never
    overwritten); without it, the current file is taken as the base."""
    sha = _current(vault, rel)[0] if base is _UNSET else base
    entry = {"path": rel, "content": content, "action": "create" if sha is None else "replace"}
    if sha is not None:
        entry["expected_sha256"] = sha
    return entry


def render_index(vault: Path, new_pages: dict[str, str], stamp: str) -> str:
    entries: dict[str, dict[str, Any]] = {}
    for page in iter_pages(vault, ("wiki",)):
        if not page.error:
            entries[page.path] = {"slug": page.slug, **page.frontmatter}
    for rel, content in new_pages.items():
        fm, _ = split_frontmatter(content)
        entries[rel] = {"slug": Path(rel).stem, **(fm or {})}
    entries = {rel: e for rel, e in entries.items() if not e.get("mergedInto")}  # stubs stay out of the index
    it = i18n.lang(vault) == "it"
    lines = (["# Indice", "", f"_{len(entries)} pagine · aggiornato {stamp}_", ""] if it
             else ["# Index", "", f"_{len(entries)} pages · updated {stamp}_", ""])
    kinds = KIND_LABELS if it else KIND_LABELS_EN
    labels = dict(kinds)
    for kind, label in [*kinds, ("", "Altro" if it else "Other")]:
        group = sorted(
            (e for e in entries.values() if (e.get("kind") if e.get("kind") in labels else "") == kind),
            key=lambda e: str(e.get("title", e["slug"])).casefold(),
        )
        if not group:
            continue
        lines += [f"## {label}", ""]
        for e in group:
            summary = str(e.get("summary", "")).strip().replace("\n", " ")
            lines.append(f"- [[{e['slug']}|{e.get('title', e['slug'])}]]" + (f" — {summary}" if summary else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def stamp_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


GENERATED_BODY = "generatedBody"  # sha256 of the body as the engine wrote it


def regenerate(vault: Path, rel: str, fm: dict[str, Any], body: str) -> list[dict[str, Any]]:
    """Writes for a note the engine (re)generates: digest, weekly review, folded log, filing maps.
    When the existing note was edited by hand since it was generated, a copy of it is saved next
    to it (modifiche/) in the same transaction: a refresh never throws away the user's words."""
    from .markdown import render_page

    content = render_page({**fm, GENERATED_BODY: sha256_bytes(body.strip().encode("utf-8"))}, body)
    target = Path(vault) / rel
    if not target.is_file():
        return [{"path": rel, "action": "create", "content": content}]
    data = target.read_bytes()
    writes = [{"path": rel, "action": "replace", "content": content, "expected_sha256": sha256_bytes(data)}]
    old_fm, old_body = split_frontmatter(data.decode("utf-8", errors="replace"))
    if (old_fm or {}).get(GENERATED_BODY) != sha256_bytes(old_body.strip().encode("utf-8")):
        stem = Path(rel).stem
        writes.append({"path": f"{Path(rel).parent.as_posix()}/modifiche/{stem}-{time.strftime('%Y%m%d-%H%M%S')}.md",
                       "action": "create", "content": data.decode("utf-8", errors="replace")})
    return writes


def build_writes(vault: Path, pages: dict[str, str], log_entry: str, stamp: str,
                 bases: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
    """pages: vault-relative path -> full Markdown content. stamp is fixed by the
    caller so a dry run and its approved apply produce byte-identical writes.
    bases: path -> sha256 of the text each new page was computed from (see _write); callers that
    edit an existing page pass it so a concurrent edit makes the write fail instead of vanishing."""
    bases = bases or {}
    writes = [_write(vault, rel, content, bases.get(rel, _UNSET)) for rel, content in sorted(pages.items())]
    if pages:
        index_sha, _ = _current(vault, "wiki/index.md")
        writes.append(_write(vault, "wiki/index.md", render_index(vault, pages, stamp), index_sha))
    log_sha, log = _current(vault, "wiki/log.md")  # hash of the very bytes the new log extends
    previous = log.decode("utf-8") if log else "# Log\n"
    writes.append(_write(vault, "wiki/log.md", previous.rstrip("\n") + f"\n\n## [{stamp}] {log_entry.strip()}\n", log_sha))
    return writes
