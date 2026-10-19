"""Belief tracing: gathers a page's whole recorded history -- confidence, provenance,
contradictions, summary, body, at each commit -- as the raw material for narrating how a position
on a topic evolved.

This module deterministically GATHERS the trail; it never narrates it. Turning a sequence of page
snapshots into "here's how your view changed and why" is exactly the kind of synthesis an LLM does
well and a fixed algorithm does badly, so that part is the `mnemoloci-changed-my-mind` skill's job
(a live Claude Code session reading this function's output), not this module's -- same split as
everywhere else in the engine: deterministic plumbing here, understanding in the session."""

from pathlib import Path
from typing import Any

from . import backup
from .core.errors import ValidationError
from .markdown import split_frontmatter
from .vault import require_vault


def trace(vault: Path, page_path: str, limit: int = 20) -> dict[str, Any]:
    """Every version of `page_path` on record, oldest first. `limit` bounds how many of the most
    recent commits to look at (a page edited hundreds of times only needs its recent arc, not its
    entire history, for a "how did this change" narrative)."""
    require_vault(vault)
    commits = backup.history(vault, page_path, limit=limit)["commits"]
    versions = []
    for c in reversed(commits):  # oldest first: the order a narrative would walk them in
        content = backup.content_at(vault, page_path, c["id"])
        if content is None:
            continue  # this commit predates the page (a rename, or it touched a sibling file)
        try:
            fm, body = split_frontmatter(content)
        except ValidationError:
            fm, body = None, content
        fm = fm or {}
        versions.append({
            "commit": c["id"], "at": c["at"], "message": c["message"],
            "confidence": fm.get("confidence"), "provenanceState": fm.get("provenanceState"),
            "contradictedBy": fm.get("contradictedBy") or [], "summary": fm.get("summary"),
            "body": body.strip(),
        })
    return {"page": page_path, "versions": versions}
