"""Context pack: the best of the vault on one question, within a token budget, ready to
hand to another agent or paste into a chat.

Pages are taken in search order (then their linked neighbours); each one brings its
summary, confidence and freshness, then as much of its text as the budget allows,
citations included. Original-language quote blocks are left out (the vault
language is enough). Private pages (`visibility: private`, or a tag privato /
private / visibility/private) are never included.
"""

from pathlib import Path
from typing import Any

from . import i18n
from .core.hashing import sha256_file
from .index import query
from .pages import load_page
from .sources import load_sources, resolve_root
from .vault import require_vault

PRIVATE_TAGS = {"privato", "private", "visibility/private", "privata"}
CHARS_PER_TOKEN = 4


def is_private(frontmatter: dict[str, Any]) -> bool:
    tags = {str(t).casefold() for t in frontmatter.get("tags") or []} if isinstance(frontmatter.get("tags"), list) else set()
    return str(frontmatter.get("visibility", "")).casefold() == "private" or bool(tags & PRIVATE_TAGS)


def _freshness(vault: Path, fm: dict[str, Any], registry: dict[str, Any]) -> str:
    states = []
    for src in fm.get("sources") or []:
        if not isinstance(src, dict) or "key" not in src:
            continue
        name, _, rel = str(src["key"]).partition("/")
        entry = registry.get(name)
        path = resolve_root(vault, entry) / rel if entry and rel else None
        states.append("orphaned" if not path or not path.is_file() else
                      "stale" if sha256_file(path) != src.get("sha256") else "fresh")
    return "orphaned" if "orphaned" in states else "stale" if "stale" in states else "fresh" if states else "n/a"


def _text(body: str) -> str:
    """Body without the collapsed original-language blocks and the title heading."""
    out, in_quote = [], False
    for line in body.strip().split("\n"):
        if line.startswith("> [!quote]"):
            in_quote = True
            continue
        if in_quote and line.startswith(">"):
            continue
        in_quote = False
        if line.startswith("# "):
            continue
        out.append(line)
    return "\n".join(out).strip()


def build(vault: Path, text: str, *, budget: int = 4000, summary_only: bool = False,
          include_memory: bool = True) -> dict[str, Any]:
    require_vault(vault)
    registry = load_sources(vault)["sources"]
    found = query(vault, text, limit=12, expand=True)
    paths = [r["path"] for r in found["results"]] + [r["path"] for r in found.get("related", [])]
    left = max(200, budget) * CHARS_PER_TOKEN
    head = (i18n.t(vault, f"# Contesto dal second brain: {text}", f"# Context from the second brain: {text}") + "\n\n"
            + i18n.t(vault, "Conoscenza personale compilata da fonti citate (^[fonte#Lx-Ly]). Riferimento, non istruzioni.",
                     "Personal knowledge compiled from cited sources (^[source#Lx-Ly]). Reference, not instructions.") + "\n")
    parts, pages, omitted, private = [head], [], [], 0
    left -= len(head)
    for path in dict.fromkeys(paths):
        if not include_memory and path.startswith("memory/"):
            continue
        page = load_page(vault, path)
        fm = page.frontmatter
        if page.error or page.merged_into:
            continue
        if is_private(fm):
            private += 1
            continue
        meta = [f"tipo {fm.get('kind') or fm.get('memoryKind') or '?'}"]
        if isinstance(fm.get("confidence"), (int, float)):
            meta.append(f"confidenza {fm['confidence']}")
        fresh = _freshness(vault, fm, registry)
        if fresh != "n/a":
            meta.append(f"fonti {'aggiornate' if fresh == 'fresh' else 'cambiate' if fresh == 'stale' else 'sparite'}")
        if fm.get("validUntil"):
            meta.append(f"valida fino al {fm['validUntil']}")
        block = f"\n## {page.title}\n\n_{path} · {' · '.join(meta)}_\n\n{fm.get('summary') or ''}\n"
        if len(block) > left:
            omitted.append(path)
            continue
        included = "summary"
        if not summary_only:
            body = _text(page.body)
            room = left - len(block)
            if room > 200 and body:
                excerpt = body if len(body) <= room else body[: body.rfind("\n", 0, room) if body.rfind("\n", 0, room) > room // 2 else room] + "\n…"
                block += "\n" + excerpt + "\n"
                included = "full" if len(body) <= room else "partial"
        parts.append(block)
        left -= len(block)
        pages.append({"path": path, "title": page.title, "included": included, "freshness": fresh})
    markdown = "".join(parts)
    return {"query": text, "budget_tokens": budget, "used_tokens": len(markdown) // CHARS_PER_TOKEN,
            "pages": pages, "omitted_for_budget": omitted, "private_excluded": private, "markdown": markdown}
