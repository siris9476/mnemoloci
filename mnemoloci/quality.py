"""A health score for the vault with history: one number from 0 to 100 built from the lint
report, and its trend over time, so you can tell whether the wiki is getting
better. `--min-score` turns it into a gate (exit code 1 below the threshold).
"""

import json
import time
from pathlib import Path
from typing import Any

from . import lint
from .core.errors import MnemoError
from .vault import engine, require_vault

WEIGHTS = {"structure": 0.30, "citations": 0.25, "freshness": 0.15, "connectivity": 0.15, "trust": 0.15}
HISTORY = "quality_history.jsonl"


def _pages_with(issues: dict[str, list], *keys: str) -> set[str]:
    out: set[str] = set()
    for key in keys:
        for item in issues.get(key, []):
            out.add(item["path"] if isinstance(item, dict) and "path" in item else str(item))
    return out


def score(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    report = lint.lint(vault)
    issues = report["issues"]
    total = max(report["pages"], 1)
    if not report["pages"]:
        return {"score": None, "pages": 0, "note": "no pages yet"}
    orphans = len(issues.get("orphans", []))
    parts = {
        "structure": 1 - len(_pages_with(issues, "unreadable", "missing_fields", "broken_links", "bad_citations",
                                         "misplaced", "empty_sections")) / total,
        "citations": 1 - len(_pages_with(issues, "uncited")) / total,
        "freshness": 1 - len(_pages_with(issues, "stale", "orphaned_sources", "expired", "refresh_due")) / total,
        "connectivity": 1 - orphans / total,
        "trust": 1 - len(_pages_with(issues, "low_confidence", "contradicted", "injection_suspect")) / total,
    }
    parts = {k: round(max(0.0, v), 3) for k, v in parts.items()}
    value = round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()))
    worst = sorted(((k, v) for k, v in report["counts"].items() if v), key=lambda kv: -kv[1])[:5]
    return {"score": value, "components": parts, "pages": report["pages"],
            "top_issues": [{"issue": k, "count": v} for k, v in worst]}


EVAL_FILE = "_meta/eval/queries.jsonl"
EVAL_HISTORY = "eval_history.jsonl"


def gold_set(vault: Path) -> list[dict[str, Any]]:
    """Questions with known right pages: every saved answer (its question -> the pages it used),
    plus any line {"query": "...", "expected": ["slug", ...]} in _meta/eval/queries.jsonl."""
    from .pages import iter_pages

    items = []
    for p in iter_pages(vault, ("wiki",)):
        fm = p.frontmatter
        if not p.error and fm.get("kind") == "query" and fm.get("question"):
            expected = [b.get("slug") for b in fm.get("basedOn") or [] if isinstance(b, dict) and b.get("slug")]
            if expected:
                items.append({"query": str(fm["question"]), "expected": expected, "from": p.path})
    extra = Path(vault) / EVAL_FILE
    if extra.is_file():
        for n, line in enumerate(extra.read_text("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise MnemoError("BAD_EVAL_FILE", f"{EVAL_FILE}:{n} is not valid JSON") from None
            expected = row.get("expected") if isinstance(row, dict) else None
            if not isinstance(expected, list) or not expected or not row.get("query"):
                raise MnemoError("BAD_EVAL_FILE", f"{EVAL_FILE}:{n} needs a query and a non-empty expected list")
            items.append({"query": str(row["query"]), "expected": [str(e) for e in expected], "from": f"{EVAL_FILE}:{n}"})
    return items


def retrieval_eval(vault: Path, *, k: int = 5, record: bool = False) -> dict[str, Any]:
    """recall@k and MRR of search on the gold set, for the lexical
    ranking and, when embeddings exist, the hybrid one. Saved answers never count as hits."""
    require_vault(vault)
    from . import embed, index

    items = gold_set(vault)
    if not items:
        return {"questions": 0, "note": "no gold questions yet: save a few answers (`answer save`) or add "
                                        f"{EVAL_FILE}"}
    modes = ["lexical"] + (["hybrid"] if embed.available(vault) and embed.has_vectors(vault) else [])
    report: dict[str, Any] = {"questions": len(items), "k": k, "modes": {}}
    for mode in modes:
        rows = []
        for item in items:
            hits = [r for r in index.query(vault, item["query"], limit=k * 3, expand=False, mode=mode)["results"]
                    if r.get("kind") != "query"][:k]
            slugs = [Path(r["path"]).stem for r in hits]
            found = [s for s in item["expected"] if s in slugs]
            first = min((slugs.index(s) for s in found), default=None)
            rows.append({"query": item["query"], "recall": round(len(found) / len(item["expected"]), 3),
                         "rr": round(1 / (first + 1), 3) if first is not None else 0.0,
                         "missed": [s for s in item["expected"] if s not in slugs]})
        report["modes"][mode] = {"recall_at_k": round(sum(r["recall"] for r in rows) / len(rows), 3),
                                 "mrr": round(sum(r["rr"] for r in rows) / len(rows), 3),
                                 "worst": sorted(rows, key=lambda r: (r["recall"], r["rr"]))[:5]}
    if record:
        with open(engine(vault) / EVAL_HISTORY, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "k": k, "questions": len(items),
                                     **{m: {x: v[x] for x in ("recall_at_k", "mrr")} for m, v in report["modes"].items()}}) + "\n")
        report["recorded"] = True
    return report


def history(vault: Path, limit: int = 30) -> list[dict[str, Any]]:
    path = engine(vault) / HISTORY
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    return rows[-limit:]


def run(vault: Path, *, record: bool = False, min_score: int | None = None) -> dict[str, Any]:
    result = score(vault)
    previous = history(vault, 1)
    if previous and result["score"] is not None and previous[-1].get("score") is not None:
        result["previous"] = previous[-1]
        result["delta"] = result["score"] - previous[-1]["score"]
    if record and result["score"] is not None:
        path = engine(vault) / HISTORY
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "score": result["score"],
                                     "components": result["components"], "pages": result["pages"]}) + "\n")
        result["recorded"] = True
    if min_score is not None:
        result["gate"] = {"min_score": min_score, "passed": result["score"] is not None and result["score"] >= min_score}
    return result
