"""The user's own voice: samples of text the user
actually wrote, so Claude can draft posts, emails or notes that sound like them.

Samples come from the user's own notes (raw/notes, captured with --mine, and an
adopted Obsidian vault) and from the longer messages they typed in past sessions.
Pasted material (code, logs, quoted text) is skipped; everything is redacted.
"""

import re
from pathlib import Path
from typing import Any

from .markdown import split_frontmatter
from .secrets import redact
from .sources import load_sources, resolve_root
from .vault import require_vault

MIN_CHARS = 280
MAX_SAMPLE = 1_500


def _prose(text: str) -> bool:
    """Written prose, not pasted code/logs/tables."""
    if text.count("```") or text.count("\t") > 3:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in text)
    lines = [l for l in text.splitlines() if l.strip()]
    symbolic = sum(1 for l in lines if re.match(r"^\s*([{}\[\]<>|#$>]|\w+\(|\d+[:.]\d+)", l))
    return letters / max(len(text), 1) > 0.85 and symbolic <= len(lines) // 4


def samples(vault: Path, *, limit: int = 8) -> dict[str, Any]:
    require_vault(vault)
    found: list[dict[str, Any]] = []
    registry = load_sources(vault)["sources"]
    for name, entry in registry.items():
        if entry.get("kind") != "notes" or entry.get("retired"):
            continue
        for path in sorted(resolve_root(vault, entry).rglob("*.md"), key=lambda p: -p.stat().st_mtime)[:40]:
            if ".obsidian" in path.parts:
                continue
            try:
                _, body = split_frontmatter(path.read_text("utf-8"))
            except Exception:  # noqa: BLE001 - an unreadable note is just not a sample
                continue
            body = re.sub(r"^# .*\n", "", body.strip())
            if len(body) >= MIN_CHARS and _prose(body):
                found.append({"from": f"{name}/{path.name}", "text": redact(body[:MAX_SAMPLE])[0]})
    from contextlib import closing

    from .session_index import _connect

    with closing(_connect(vault)) as conn:
        rows = conn.execute("SELECT session_id, text FROM msgs WHERE role='user' AND length(text) >= ? "
                            "ORDER BY ts DESC LIMIT 400", (MIN_CHARS,)).fetchall()
    for sid, text in rows:
        if _prose(text):
            found.append({"from": f"sessione {sid[:8]}", "text": redact(text[:MAX_SAMPLE])[0]})
    picked = found[:limit]
    words = [w for s in picked for w in re.findall(r"\w+", s["text"])]
    sentences = [x for s in picked for x in re.split(r"[.!?]+\s", s["text"]) if x.strip()]
    return {"samples": picked, "available": len(found),
            "style": {"avg_sentence_words": round(len(words) / max(len(sentences), 1), 1),
                      "uses_first_person": sum(1 for s in picked if re.search(r"\b(io|mi|mio|mia|ho|sono)\b", s["text"], re.I))},
            "note": "imitate tone, rhythm and vocabulary; never copy personal details from the samples into new text"}
