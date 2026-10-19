"""Monthly log folds: wiki/log.md only grows; a fold
is a deterministic monthly summary of it in _meta/folds/<YYYY-MM>.md - operations by
type and the pages touched, each with the date of its log entry. log.md itself is
never changed; the same entries always produce the same fold.
"""

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import i18n
from .core import txn
from .core.hashing import sha256_file
from .core.lock import VaultLock
from .markdown import render_page
from .publish import regenerate
from .vault import require_vault

_ENTRY = re.compile(r"^## \[(\d{4}-\d{2})-(\d{2})[^\]]*\] (.+)$")
_LINK = re.compile(r"\[\[([^\]|#]+)")


def _entries(vault: Path) -> list[dict[str, Any]]:
    path = Path(vault) / "wiki/log.md"
    if not path.is_file():
        return []
    out, current = [], None
    for line in path.read_text("utf-8").splitlines():
        m = _ENTRY.match(line)
        if m:
            current = {"month": m.group(1), "day": f"{m.group(1)}-{m.group(2)}", "text": m.group(3),
                       "links": _LINK.findall(m.group(3))}
            out.append(current)
        elif current is not None:
            current["links"] += _LINK.findall(line)
    return out


def _kind(text: str) -> str:
    head = text.split(":", 1)[0].split(" — ")[0].strip()
    return re.sub(r"\s+r?\d{8}-\d{6}x*", "", head).strip() or "altro"


def fold(vault: Path, *, month: str | None = None) -> dict[str, Any]:
    """Fold one month (YYYY-MM), or every month before the current one."""
    require_vault(vault)
    entries = _entries(vault)
    current = time.strftime("%Y-%m")
    months = sorted({e["month"] for e in entries if (e["month"] == month if month else e["month"] < current)})
    writes = []
    with VaultLock(vault) as lock:
        for m in months:
            chosen = [e for e in entries if e["month"] == m]
            kinds = Counter(_kind(e["text"]) for e in chosen)
            touched: dict[str, str] = {}
            for e in chosen:
                for slug in e["links"]:
                    touched.setdefault(slug.strip(), e["day"])
            body = [i18n.t(vault, f"# Log di {m}", f"# Log of {m}"), "",
                    i18n.t(vault, f"{len(chosen)} operazioni registrate in `wiki/log.md`.",
                           f"{len(chosen)} operations recorded in `wiki/log.md`."), "",
                    i18n.t(vault, "## Per tipo", "## By type"), ""]
            body += [f"- {k}: {n}" for k, n in kinds.most_common()]
            if touched:
                body += ["", i18n.t(vault, "## Pagine toccate (prima volta nel mese)", "## Pages touched (first time in the month)"), ""]
                body += [f"- [[{s}]] — {d}" for s, d in sorted(touched.items(), key=lambda kv: (kv[1], kv[0]))]
            rel = f"_meta/folds/{m}.md"
            planned = regenerate(vault, rel, {"title": f"Log {m}", "schema": "mnemoloci.fold.v1", "month": m,
                                              "entries": len(chosen)}, "\n".join(body))
            target = Path(vault) / rel
            if target.is_file() and target.read_text("utf-8") == planned[0]["content"]:
                continue
            writes += planned
        if writes:
            bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"fold-{time.time_ns()}", "operation": "fold",
                      "writes": writes}
            txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    return {"months": months, "written": [w["path"] for w in writes if w["path"].count("/modifiche/") == 0],
            "hand_edits_kept_in": [w["path"] for w in writes if "/modifiche/" in w["path"]], "note": "wiki/log.md is unchanged"}
