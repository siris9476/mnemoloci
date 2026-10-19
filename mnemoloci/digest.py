"""Daily / weekly digest and the "today" view.

Deterministic: it gathers what happened in a period from the vault itself (log,
pages, saved answers, clips, memory notes, goals, todos) and from the session
index, with no LLM. Claude can turn it into prose (skill mnemoloci-digest); the
app shows it as the "Oggi" view. `--save` keeps it as a note in _meta/digests/.
"""

import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import clips, graph_analysis, i18n, memory, personal, session_index, skill_factory
from .core import txn
from .core.errors import ValidationError
from .core.hashing import sha256_file
from .core.lock import VaultLock
from .inbox import pending as inbox_pending
from .markdown import render_page
from .pages import iter_pages
from .review import list_candidates
from .sources import load_sources, pending_summary, scan
from .publish import regenerate
from .vault import require_vault

_LOG_ENTRY = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})[^\]]*\] (.+)$")
_LINK = re.compile(r"\[\[([^\]|#]+)")
DIGEST_ROOT = "_meta/digests"


def _period(day: str | None, week: bool) -> tuple[date, date, str]:
    try:
        anchor = date.fromisoformat(day) if day else date.today()
    except ValueError as exc:
        raise ValidationError("BAD_DATE", "day must be YYYY-MM-DD") from exc
    if week:
        start = anchor - timedelta(days=anchor.weekday())
        iso = start.isocalendar()
        return start, start + timedelta(days=7), f"{iso[0]}-W{iso[1]:02d}"
    return anchor, anchor + timedelta(days=1), anchor.isoformat()


def _utc(d: date) -> str:
    """Local midnight of d as a UTC ISO string (transcripts use UTC timestamps)."""
    return datetime(d.year, d.month, d.day).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in(value: Any, start: date, end: date) -> bool:
    text = str(value or "")[:10]
    return start.isoformat() <= text < end.isoformat()


def _log_entries(vault: Path, start: date, end: date) -> list[dict[str, Any]]:
    path = Path(vault) / "wiki/log.md"
    if not path.is_file():
        return []
    out, current = [], None
    for line in path.read_text("utf-8").splitlines():
        m = _LOG_ENTRY.match(line)
        if m:
            current = {"day": m.group(1), "text": m.group(2), "links": _LINK.findall(m.group(2))}
            if start.isoformat() <= current["day"] < end.isoformat():
                out.append(current)
            else:
                current = None
        elif current is not None:
            current["links"] += _LINK.findall(line)
    return out


def build(vault: Path, *, day: str | None = None, week: bool = False) -> dict[str, Any]:
    require_vault(vault)
    start, end, label = _period(day, week)
    pages = [p for p in iter_pages(vault, ("wiki",)) if not p.error]
    titles = {p.slug: p.title for p in pages}
    published = sorted({s for e in _log_entries(vault, start, end) for s in e["links"]})
    new_answers = [{"title": p.title, "path": p.path, "question": p.frontmatter.get("question")}
                   for p in pages if p.frontmatter.get("kind") == "query" and _in(p.frontmatter.get("updated"), start, end)]
    captured = [c for c in clips.list_clips(vault, 500)["clips"] if _in(c["captured"], start, end)]
    # active notes only: a superseded one (e.g. an older handoff of the same session) would repeat itself
    notes = [m for m in memory.list_memory(vault, limit=500) if _in(m["created"], start, end)]
    todos_all = personal.todos_list(vault, include_closed=True)["todos"]
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=7)).isoformat()
    open_todos = [t for t in todos_all if t["status"] == "open"]
    goals = personal.goals_list(vault, include_closed=True)["goals"]
    try:
        sessions = session_index.between(vault, _utc(start), _utc(end))
    except Exception:  # noqa: BLE001 - the digest must work even when the session index cannot refresh
        sessions = []
    published_set = set(published)
    try:
        # graph_analysis/suggest_clusters already compute this vault-wide on every call (CLI
        # `graph-analyze` / `skills suggest-clusters`) but nobody sees it unless they think to ask;
        # surfacing here is the whole point -- filtered to what a page published *this period*
        # touches, so the same standing connection isn't repeated in every digest forever
        graph = graph_analysis.analyze(vault, top=50, betweenness=False)  # only surprising_links is used here
        new_links = [e for e in graph["surprising_links"] if e["a_slug"] in published_set or e["b_slug"] in published_set]
        clusters = skill_factory.suggest_clusters(vault, limit=10)["clusters"]
        new_clusters = [c for c in clusters if any(p["slug"] in published_set for p in c["mature_pages"])]
    except Exception:  # noqa: BLE001 - the digest must work even when graph analysis cannot run
        new_links, new_clusters = [], []
    registry = load_sources(vault)["sources"]
    to_compile = sum(len(scan(vault, n)[n].get("changed", [])) for n in ("clips", "notes", "chats") if n in registry)
    return {
        "period": label, "from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(), "week": week,
        "pages_published": [{"slug": s, "title": titles.get(s, s)} for s in published if s in titles],
        "answers_saved": new_answers,
        "captured": [{"title": c["title"], "origin": c["origin"], "path": c["path"], "url": c.get("url")} for c in captured],
        "memory": {kind: [{"title": m["title"], "path": m["path"]} for m in notes if m["kind"] == kind]
                   for kind in ("decision", "lesson", "fact", "preference", "runbook", "handoff")
                   if any(m["kind"] == kind for m in notes)},
        "sessions": [{"session_id": s["session_id"], "title": s["title"], "project": s["cwd"], "requests": s["n_user"],
                      "open_question": s["open_question"]} for s in sessions],
        "todos": {"done": [t for t in todos_all if t["status"] == "done" and _in(t.get("closed"), start, end)],
                  "overdue": [t for t in open_todos if t.get("due") and t["due"] < today],
                  "due_soon": [t for t in open_todos if t.get("due") and today <= t["due"] <= soon],
                  "open": len(open_todos)},
        "goals": {"active": [g for g in goals if g["status"] == "active"],
                  "changed": [g for g in goals if _in(g.get("updated"), start, end) and g["status"] != "active"]},
        "waiting": {"review": len(list_candidates(vault)), "inbox": len(inbox_pending(vault)["pending"]),
                    "to_compile": to_compile,
                    # project sources: as of the last full scan (plan, or the daily scheduled run)
                    "sources_changed": {n: c for n, c in ((pending_summary(vault) or {}).get("changed") or {}).items()
                                        if n not in ("clips", "notes", "chats")}},
        "connections": {
            "surprising": [{"a": e["a"], "b": e["b"], "between": e["between"]} for e in new_links],
            "clusters": [{"name": c["suggested_name"], "hub": c["hub_page"]["title"], "pages": len(c["mature_pages"])}
                        for c in new_clusters],
        },
    }


def render(d: dict[str, Any], vault: Path | None = None) -> str:
    def T(it: str, en: str) -> str:
        return i18n.t(vault, it, en)

    head = (T(f"# Riepilogo {'della settimana ' + d['period'] if d['week'] else 'del ' + d['from']}",
              f"# Summary {'of the week ' + d['period'] if d['week'] else 'of ' + d['from']}"))
    out = [head, ""]

    def section(title: str, lines: list[str]) -> None:
        lines = [line for line in lines if line]
        if lines:
            out.extend([f"## {title}", "", *lines, ""])

    section(T("Pagine pubblicate", "Published pages"), [f"- [[{p['slug']}|{p['title']}]]" for p in d["pages_published"]])
    section(T("Risposte salvate", "Saved answers"), [f"- [[{Path(a['path']).stem}|{a['title']}]]" + (f" — {a['question']}" if a["question"] else "")
                                 for a in d["answers_saved"]])
    section(T("Contenuti salvati", "Saved content"), [f"- {c['title']} ({c['origin']})" + (f" — {c['url']}" if c.get("url") else "")
                                  for c in d["captured"]])
    labels = {"decision": T("Decisioni", "Decisions"), "lesson": T("Lezioni", "Lessons"), "fact": T("Fatti", "Facts"),
              "preference": T("Preferenze", "Preferences"), "runbook": T("Procedure", "Runbooks"), "handoff": "Handoff"}
    for kind, items in d["memory"].items():
        section(labels[kind], [f"- {m['title']}" for m in items])
    section(T("Sessioni di lavoro", "Work sessions"),
            [f"- {s['title'] or T('sessione', 'session')} · {s['project'] or ''} · "
             + T(f"{s['requests']} richieste", f"{s['requests']} requests")
             + (T(f"\n  - domanda rimasta aperta: {s['open_question'][:200]}",
                  f"\n  - question left open: {s['open_question'][:200]}") if s["open_question"] else "")
             for s in d["sessions"]])
    conn = d["connections"]
    section(T("Collegamenti emersi", "Connections that emerged"), [
        *(f"- **{e['a']}** ↔ **{e['b']}** — " + T(f"collega {e['between'][0]} e {e['between'][1]}",
                                                   f"links {e['between'][0]} and {e['between'][1]}") for e in conn["surprising"]),
        *(T(f"- {c['pages']} pagine mature intorno a **{c['hub']}** potrebbero diventare una skill ",
            f"- {c['pages']} mature pages around **{c['hub']}** could become a skill ")
          + "(`mnemoloci skills suggest-clusters`)" for c in conn["clusters"]),
    ])
    todos = d["todos"]
    section(T("Cose da fare", "To-dos"), [*(f"- [x] {t['text']}" for t in todos["done"]),
                             *(f"- [ ] **{T('SCADUTA', 'OVERDUE')} {t['due']}** {t['text']}" for t in todos["overdue"]),
                             *(f"- [ ] {T('entro', 'by')} {t['due']}: {t['text']}" for t in todos["due_soon"]),
                             T(f"- {todos['open']} aperte in totale", f"- {todos['open']} open in total") if todos["open"] else ""])
    section(T("Obiettivi", "Goals"), [*(f"- [{g['id']}] {g['text']}" for g in d["goals"]["active"]),
                          *(f"- [{g['id']}] {g['text']} → {g['status']}" for g in d["goals"]["changed"])])
    wait = d["waiting"]
    section(T("In attesa di te", "Waiting for you"), [line for line in (
        T(f"- {wait['review']} pagine da revisionare", f"- {wait['review']} pages to review") if wait["review"] else "",
        T(f"- {wait['inbox']} screenshot da trascrivere", f"- {wait['inbox']} screenshots to transcribe") if wait["inbox"] else "",
        T(f"- {wait['to_compile']} contenuti da compilare", f"- {wait['to_compile']} items to compile") if wait["to_compile"] else "",
        T(f"- {sum(wait['sources_changed'].values())} file cambiati nelle fonti ({', '.join(wait['sources_changed'])})",
          f"- {sum(wait['sources_changed'].values())} changed files in the sources ({', '.join(wait['sources_changed'])})")
        if wait.get("sources_changed") else "") if line])
    if len(out) == 2:
        out.append(T("_Nessuna attività registrata in questo periodo._", "_No activity recorded in this period._"))
    return "\n".join(out).rstrip() + "\n"


def save(vault: Path, *, day: str | None = None, week: bool = False) -> dict[str, Any]:
    d = build(vault, day=day, week=week)
    rel = f"{DIGEST_ROOT}/{d['period']}.md"
    fm = {"title": f"Riepilogo {d['period']}", "schema": "mnemoloci.digest.v1", "period": d["period"],
          "generated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with VaultLock(vault) as lock:
        # the same period saved again is refreshed; notes added by hand are kept in a copy (modifiche/)
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"digest-{time.time_ns()}", "operation": "digest",
                  "writes": regenerate(vault, rel, fm, render(d, vault))}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    return {"path": rel, "period": d["period"]}
