"""Bounded web research with a reviewed merge.

Claude does the searching and reading (its own web tools); the engine keeps the
research honest and bounded:
- a budget: at most N rounds, M captured sources per round (default 3 x 5);
- every source goes through `ingest` (redaction, injection scan, external-source
  review) and is tagged with the research run;
- the final dossier must cite only captured sources or existing wiki pages, with
  real line ranges, and must include counter-positions (or say none were found);
- nothing reaches the wiki directly: the captured clips are compiled as usual and
  their pages wait in the review queue.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from . import briefing, clips
from .core import txn
from .core.atomic import atomic_json
from .core.errors import ConflictError, MnemoError, ValidationError
from .core.hashing import sha256_file
from .core.lock import VaultLock
from .markdown import citations, render_page, slugify
from .vault import engine, now_iso, require_vault

RESEARCH_DIR = "_meta/research"
_COUNTER = re.compile(r"^#+\s*(posizioni contrarie|controargomenti|contraddizioni|counter|obiezioni)", re.IGNORECASE | re.MULTILINE)


def _state_path(vault: Path, run: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{6,90}", run or ""):
        raise MnemoError("BAD_RUN", f"invalid research id {run!r}")
    return engine(vault) / "research" / f"{run}.json"


def _load(vault: Path, run: str) -> dict[str, Any]:
    try:
        return json.loads(_state_path(vault, run).read_text("utf-8"))
    except FileNotFoundError:
        raise MnemoError("NO_SUCH_RESEARCH", f"no research {run}") from None


def start(vault: Path, question: str, *, rounds: int = 3, per_round: int = 5) -> dict[str, Any]:
    require_vault(vault)
    question = " ".join((question or "").split())
    if not question:
        raise ValidationError("NO_QUESTION", "state the research question")
    if not 1 <= rounds <= 6 or not 1 <= per_round <= 10:
        raise ValidationError("BAD_BUDGET", "rounds 1-6, sources per round 1-10")
    run = f"{time.strftime('%Y%m%d-%H%M')}-{slugify(question)[:50]}"
    state = {"id": run, "question": question, "rounds": rounds, "per_round": per_round, "round": 1,
             "status": "open", "created": now_iso(), "sources": []}
    path = _state_path(vault, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, state)
    return {**state, "plan": ["round 1: broad survey of the main positions",
                              "round 2: fill the gaps you found, primary sources first",
                              "round 3: look for counter-positions and contradicting evidence"]}


def add_source(vault: Path, run: str, *, url: str | None = None, text: str | None = None,
               title: str | None = None) -> dict[str, Any]:
    state = _load(vault, run)
    if state["status"] != "open":
        raise ConflictError("RESEARCH_CLOSED", f"research {run} is {state['status']}")
    used = sum(1 for s in state["sources"] if s["round"] == state["round"])
    if used >= state["per_round"]:
        raise ConflictError("ROUND_BUDGET_SPENT", f"round {state['round']} already has {used} sources: "
                            "start the next round or finish")
    note = f"ricerca {run}, round {state['round']}: {state['question']}"[:300]
    if url:
        out = clips.add_url(vault, url, note=note, title=title)
    elif text and title:
        out = clips.add_text(vault, text, title=title, note=note)
    else:
        raise ValidationError("NO_SOURCE", "give --url, or --text-file with --title")
    path = out.get("path")
    with VaultLock(vault):  # re-read under the lock: two captures at once must not drop each other
        state = _load(vault, run)
        if path and not any(s["path"] == path for s in state["sources"]):
            state["sources"].append({"path": path, "round": state["round"], "title": out.get("title"), "url": url,
                                     "injection": (out.get("injection") or {}).get("verdict"), "added": now_iso()})
            atomic_json(_state_path(vault, run), state)
    return {**out, "research": run, "round": state["round"], "left_in_round": state["per_round"] - used - 1}


def next_round(vault: Path, run: str) -> dict[str, Any]:
    with VaultLock(vault):
        state = _load(vault, run)
        if state["status"] != "open":
            raise ConflictError("RESEARCH_CLOSED", f"research {run} is {state['status']}")
        if state["round"] >= state["rounds"]:
            raise ConflictError("NO_ROUNDS_LEFT", f"all {state['rounds']} rounds used: write the dossier and finish")
        state["round"] += 1
        atomic_json(_state_path(vault, run), state)
    return {"research": run, "round": state["round"], "of": state["rounds"]}


def status(vault: Path, run: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    if run:
        return _load(vault, run)
    folder = engine(vault) / "research"
    runs = [json.loads(p.read_text("utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    return {"research": [{k: r[k] for k in ("id", "question", "status", "round", "rounds")} | {"sources": len(r["sources"])}
                         for r in runs]}


def finish(vault: Path, run: str, dossier: str) -> dict[str, Any]:
    """Save the dossier after checking it cites only this research's sources (or wiki pages) at real
    lines and discusses counter-positions."""
    state = _load(vault, run)
    if state["status"] != "open":  # finishing twice would overwrite the saved dossier
        raise ConflictError("RESEARCH_CLOSED", f"research {run} is {state['status']}: its dossier is {state.get('dossier')}")
    audit = briefing.check(vault, dossier)
    captured = {s["path"].replace("raw/clips/", "clips/", 1).replace("raw/notes/", "notes/", 1) for s in state["sources"]}
    foreign = sorted({k for k, _, _ in citations(dossier) if k.startswith(("clips/", "notes/")) and k not in captured})
    problems = {k: v for k, v in (("broken_links", audit["broken_links"]), ("bad_citations", audit["bad_citations"]),
                                   ("sources_not_in_this_research", foreign)) if v}
    if not citations(dossier):
        problems["no_citations"] = ["the dossier cites nothing"]
    if not _COUNTER.search(dossier):
        problems["no_counter_positions"] = ["add a section '## Posizioni contrarie' (or say none were found there)"]
    if problems:
        raise ValidationError("DOSSIER_INVALID", "fix the dossier before finishing", problems)
    rel = f"{RESEARCH_DIR}/{run}.md"
    fm = {"title": f"Ricerca: {state['question']}"[:200], "schema": "mnemoloci.research.v1", "question": state["question"],
          "rounds_used": state["round"], "sources": [s["path"] for s in state["sources"]],
          "coverage": audit["coverage"], "finished": now_iso()}
    with VaultLock(vault) as lock:
        state = _load(vault, run)
        if state["status"] != "open":
            raise ConflictError("RESEARCH_CLOSED", f"research {run} was finished meanwhile")
        target = Path(vault) / rel
        entry = {"path": rel, "action": "create", "content": render_page(fm, dossier)}
        if target.is_file():
            entry = {**entry, "action": "replace", "expected_sha256": sha256_file(target)}
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"research-{time.time_ns()}", "operation": "research",
                  "writes": [entry]}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        state.update(status="done", finished=now_iso(), dossier=rel)
        atomic_json(_state_path(vault, run), state)
    return {"dossier": rel, "sources": len(state["sources"]), "coverage": audit["coverage"],
            "next": "compile the captured sources (`compile plan --source clips`): their pages go to review"}
