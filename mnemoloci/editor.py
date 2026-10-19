"""Manual page editing (desktop app editor).

Edits go through the same journaled transaction as every engine write, pinned
to the sha256 of the version the user opened: if the page changed meanwhile
(Obsidian, a compile run, another window) the save is refused, never merged
blindly. Only wiki pages and memory notes are editable; generated files are not.

Citations are checked with the same logic compile/maintain use (undeclared
source, out-of-range locator, claim text not literally in the source) but
report as `warnings` on the save result, never a block: this is the user's
own direct edit, not automated output waiting for review.
"""

import time
from pathlib import Path, PurePosixPath
from typing import Any

from .core import txn
from .core.errors import ConflictError, MnemoError, ValidationError
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .core.paths import resolve_in_vault, validate_relpath
from .index import update as index_update
from .markdown import render_page, split_frontmatter
from .pages import GENERATED as GENERATED_PATHS
from .publish import build_writes, stamp_now
from .secrets import redact
from .sources import load_sources
from .validate import ChunkTextReader, LineCounter, citation_problems
from .vault import require_vault

EDITABLE_ROOTS = ("wiki", "memory")
FIELDS = ("title", "summary", "aliases", "tags")
MAX_BODY = 200_000


def _check_path(path: str) -> PurePosixPath:
    rel = validate_relpath(path)
    if rel.parts[0] not in EDITABLE_ROOTS or rel.suffix.lower() != ".md" or str(rel) in GENERATED_PATHS:
        raise ValidationError("NOT_EDITABLE", f"{path} cannot be edited (only wiki pages and memory notes)")
    return rel


def load(vault: Path, path: str) -> dict[str, Any]:
    require_vault(vault)
    rel = _check_path(path)
    target = resolve_in_vault(vault, rel)
    if not target.is_file():
        raise MnemoError("NO_SUCH_PAGE", f"{path} does not exist")
    data = target.read_bytes()
    fm, body = split_frontmatter(data.decode("utf-8"))
    fm = fm or {}
    return {
        "path": str(rel), "sha256": sha256_bytes(data),
        "fields": {"title": fm.get("title", ""), "summary": fm.get("summary", ""),
                   "aliases": fm.get("aliases") or [], "tags": fm.get("tags") or []},
        "body": body.strip("\n"),
        "readonly": {k: v for k, v in fm.items() if k not in FIELDS},
    }


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError("BAD_FIELD", f"{name} must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def save(vault: Path, path: str, *, base_sha256: str, title: str, summary: str, body: str,
         aliases: list[str] | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    require_vault(vault)
    rel = _check_path(path)
    title, summary, body = (title or "").strip(), (summary or "").strip(), (body or "").strip()
    if not title or len(title) > 200 or "\n" in title:
        raise ValidationError("BAD_TITLE", "title must be one line, max 200 characters")
    if len(summary) > 500:
        raise ValidationError("BAD_SUMMARY", "summary is longer than 500 characters")
    if not body:
        raise ValidationError("EMPTY_BODY", "the page body is empty")
    if len(body) > MAX_BODY:
        raise ValidationError("BODY_TOO_LONG", f"the page body is over {MAX_BODY} characters")
    aliases, tags = _str_list(aliases, "aliases"), _str_list(tags, "tags")
    _, secrets_found = redact("\n".join([title, summary, body, *aliases, *tags]))
    if secrets_found:
        raise ValidationError("SECRET_IN_TEXT", "the page contains something that looks like a secret (API key, token, password)")

    with VaultLock(vault) as lock:
        target = resolve_in_vault(vault, rel)
        if not target.is_file():
            raise MnemoError("NO_SUCH_PAGE", f"{path} does not exist")
        current = target.read_bytes()
        if sha256_bytes(current) != base_sha256:
            raise ConflictError("PAGE_CHANGED", "the page changed after you opened it (Obsidian, a compile or another window)")
        fm, _ = split_frontmatter(current.decode("utf-8"))
        fm = dict(fm or {})
        fm.update(title=title, summary=summary, aliases=aliases)
        if tags or "tags" in fm:
            fm["tags"] = tags
        fm["updated"] = time.strftime("%Y-%m-%d")
        fm["lastEditedBy"] = "human"
        fm["lastEdited"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        content = render_page(fm, body)
        declared = {s.get("key") for s in fm.get("sources") or [] if isinstance(s, dict)}
        registry = load_sources(vault)["sources"]
        problems = citation_problems(body, declared, line_count=LineCounter(vault, registry),
                                     chunk_text=ChunkTextReader(vault, registry))
        warning_text = {
            "undeclared": lambda p: f"citation to undeclared source {p.key}",
            "unreadable": lambda p: f"source file for {p.key} is not readable",
            "out_of_range": lambda p: f"citation {p.key}#L{p.start}-L{p.end} is outside the source's line range (1..{p.total})",
            "text_mismatch": lambda p: f"citation {p.key}#L{p.start}-L{p.end} does not literally match its cited source text",
        }
        warnings = sorted({warning_text[p.kind](p) for p in problems})
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"edit-{time.time_ns()}", "operation": "manual-edit",
                  "writes": build_writes(vault, {str(rel): content}, f"modifica manuale [[{rel.stem}]]", stamp_now(),
                                              {str(rel): base_sha256})}
        result = txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        index_update(vault, [str(rel)], lock)
    return {"saved": str(rel), "sha256": sha256_bytes(content.encode("utf-8")), "warnings": warnings,
            "transaction": result["operation_id"]}
