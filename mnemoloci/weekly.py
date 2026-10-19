"""Weekly review: what moved, which goals are stuck, what was left open, then a plan for
the coming week that Claude writes with the user and saves as a note.

The engine only gathers facts (deterministic); the conversation and the plan are
Claude's job (skill mnemoloci-weekly). Nothing changes without the user's yes.
"""

import time
from pathlib import Path
from typing import Any

from . import digest, i18n, personal
from .core import txn
from .core.errors import ValidationError
from .core.hashing import sha256_file
from .core.lock import VaultLock
from .markdown import render_page
from .pages import iter_pages
from .publish import regenerate
from .vault import require_vault

WEEKLY_DIR = "_meta/weekly"


def review(vault: Path, *, day: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    d = digest.build(vault, day=day, week=True)
    start, end = d["from"], d["to"]
    todos = personal.todos_list(vault, include_closed=True)["todos"]
    updated = {p.slug for p in iter_pages(vault, ("wiki",))
               if not p.error and start <= str(p.frontmatter.get("updated", ""))[:10] <= end}
    goals = []
    for g in personal.goals_list(vault)["goals"]:
        mine = [t for t in todos if t.get("goal") == g["id"]]
        done = [t["text"] for t in mine if t["status"] == "done" and start <= str(t.get("closed") or "")[:10] <= end]
        still_open = [t["text"] for t in mine if t["status"] == "open"]
        pages = sorted(set(g.get("links") or []) & updated)
        goals.append({"id": g["id"], "text": g["text"], "status": g["status"], "area": g.get("area"),
                      "done_this_week": done, "open_todos": still_open, "pages_updated": pages,
                      "moved": bool(done or pages), "no_next_step": g["status"] == "active" and not still_open})
    return {
        "week": d["period"], "from": start, "to": end, "digest": d,
        "goals": goals,
        "stuck_goals": [g["id"] for g in goals if g["status"] == "active" and not g["moved"]],
        "goals_without_next_step": [g["id"] for g in goals if g["no_next_step"]],
        "open_questions": [{"session": s["title"], "question": s["open_question"]} for s in d["sessions"] if s["open_question"]],
        "next": "discuss with the user, update goals/todos they agree on, then `mnemoloci weekly save --plan-file FILE`",
    }


def save(vault: Path, plan: str, *, day: str | None = None) -> dict[str, Any]:
    """Keep the week's review and the plan agreed with the user as _meta/weekly/<YYYY-Www>.md."""
    plan = (plan or "").strip()
    if not plan:
        raise ValidationError("EMPTY_TEXT", "the plan is empty")
    data = review(vault, day=day)
    def T(it: str, en: str) -> str:
        return i18n.t(vault, it, en)

    summary = T("Riepilogo", "Summary")
    body = (T(f"# Revisione settimanale {data['week']}\n\n## Piano per la prossima settimana\n\n{plan}\n\n",
              f"# Weekly review {data['week']}\n\n## Plan for next week\n\n{plan}\n\n")
            + digest.render(data["digest"], vault).replace(f"# {summary}", f"## {summary}", 1))
    stuck = ", ".join(data["stuck_goals"]) or T("nessuno", "none")
    fm = {"title": T(f"Revisione {data['week']}", f"Review {data['week']}"), "schema": "mnemoloci.weekly.v1", "week": data["week"],
          "stuckGoals": data["stuck_goals"], "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}
    rel = f"{WEEKLY_DIR}/{data['week']}.md"
    with VaultLock(vault) as lock:
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"weekly-{time.time_ns()}", "operation": "weekly",
                  "writes": regenerate(vault, rel, fm, body + T(f"\n_Obiettivi fermi questa settimana: {stuck}._\n", f"\n_Goals stuck this week: {stuck}._\n"))}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    return {"path": rel, "week": data["week"]}
