"""Phase 11: commit/file -> session provenance, tag taxonomy, context pack with privacy,
weekly review, refresh of stale pages only, --as-of over the whole pool."""

import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mnemoloci import (compile as compiler, context_pack, index, lint, memory, personal, session_index, sources, tags,
                       weekly)
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import ConflictError
from mnemoloci.core.hashing import sha256_file
from mnemoloci.markdown import render_page, split_frontmatter

TODAY = date.today()
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _fm(title, sources=(("p/a.md", "a" * 64),), **extra):
    return {"title": title, "kind": "concept", "summary": f"Sintesi di {title}.", "aliases": [],
            "sources": [{"key": k, "sha256": s} for k, s in sources], "confidence": 0.9,
            "provenanceState": "extracted", "contradictedBy": [], "updated": TODAY.isoformat(), "language": "it", **extra}


def _seed(v, pages):
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"seed-{id(pages)}", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{slug}.md", "action": "create", "content": render_page(fm, body)}
                    for slug, (fm, body) in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


# ---------------- code provenance ----------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _transcript(tmp_path, sid, records):
    folder = tmp_path / "claude" / "projects" / "D--repo"
    folder.mkdir(parents=True, exist_ok=True)
    base = [{"type": "user", "sessionId": sid, "cwd": "C:\\Users\\you\\repo", "timestamp": records[0][0],
             "message": {"role": "user", "content": "lavoriamo"}}]
    for ts, content, kind in records:
        base.append({"type": kind, "sessionId": sid, "cwd": "C:\\Users\\you\\repo", "timestamp": ts,
                     "message": {"role": kind, "id": f"{sid}-{ts}", "content": content}})
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in base), "utf-8")


@needs_git
def test_commit_and_line_lead_back_to_the_session(v, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "lock.py").write_text("def acquire():\n    pass\n", "utf-8")
    (repo / "journal.py").write_text("x = 1\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Aggiunge il lock")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "journal.py").write_text("x = 2\n", "utf-8")
    _git(repo, "commit", "-qam", "Journal")  # committed with -q: no "[branch sha]" in any transcript
    second = _git(repo, "rev-parse", "HEAD")
    before = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    edit = lambda f: [{"type": "tool_use", "id": "t", "name": "Edit", "input": {"file_path": str(repo / f)}}]  # noqa: E731
    _transcript(tmp_path, "sess-lock", [
        (before, edit("lock.py"), "assistant"),
        (before, [{"type": "tool_result", "tool_use_id": "t", "content": f"[master {first[:7]}] Aggiunge il lock\n 2 files"}], "user")])
    _transcript(tmp_path, "sess-journal", [(before, edit("journal.py"), "assistant")])

    direct = session_index.commit_sessions(v, first[:7], repo=str(repo))
    assert direct["sessions"][0]["session_id"] == "sess-lock" and "git output" in direct["sessions"][0]["evidence"]
    inferred = session_index.commit_sessions(v, second, repo=str(repo), auto_refresh=False)
    assert [s["session_id"] for s in inferred["sessions"]] == ["sess-journal"] and "edited 1" in inferred["sessions"][0]["evidence"]
    history = session_index.file_history(v, str(repo / "lock.py"), line=1, auto_refresh=False)
    assert history["sessions"][0]["session_id"] == "sess-lock"
    assert history["line"]["commit"] == first[:12] and history["line"]["sessions"][0]["session_id"] == "sess-lock"
    assert session_index.recent_commits(v)["commits"][0]["message"] == "Aggiunge il lock"


@needs_git
def test_file_risk_flags_reverts_and_fix_runs(v, tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init", "-q")
    target = repo / "flaky.py"
    target.write_text("x = 1\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Add flaky.py")
    assert session_index.file_risk(v, str(target))["risk"] == "none"

    for i in range(3):
        target.write_text(f"x = {i}\n", "utf-8")
        _git(repo, "commit", "-qam", f"fix: adjust flaky value {i}")
    warned = session_index.file_risk(v, str(target))
    assert warned["risk"] == "warn" and len(warned["fixes"]) == 3 and not warned["reverts"]

    for i in range(2):
        target.write_text(f"y = {i}\n", "utf-8")
        _git(repo, "commit", "-qam", f'Revert "fix: adjust flaky value {i}"')
    blocked = session_index.file_risk(v, str(target))
    assert blocked["risk"] == "block" and len(blocked["reverts"]) == 2


# ---------------- tags ----------------

def test_tag_variants_and_rename_across_wiki_and_memory(v):
    _seed(v, {"a": (_fm("A", tags=["Lock", "windows"]), "a"), "b": (_fm("B", tags=["locks"]), "b"),
              "c": (_fm("C", tags=["lock"]), "c")})
    memory.add(v, "lesson", "Lezione", "testo", tags=["locks"])
    rep = tags.report(v)
    assert rep["variants"] == [{"tags": ["locks", "Lock", "lock"], "suggested": "locks"}]
    assert "windows" in rep["used_once"] and lint.lint(v)["counts"]["tag_variants"] == 1
    dry = tags.rename(v, "locks", "lock")
    assert dry["dry_run"] and len(dry["pages"]) == 2
    with pytest.raises(ConflictError):
        tags.rename(v, "locks", "lock", approved="0" * 64)
    tags.rename(v, "locks", "lock", approved=dry["approval_sha256"])
    tags.rename(v, "Lock", "lock", approved=tags.rename(v, "Lock", "lock")["approval_sha256"])
    assert tags.report(v)["variants"] == [] and tags.report(v)["tags"]["lock"] == 4
    assert "lezione" not in (v / "wiki/index.md").read_text("utf-8").casefold()


# ---------------- context pack ----------------

def test_context_pack_respects_budget_privacy_and_freshness(v, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.md").write_text("uno\ndue\ntre\n", "utf-8")
    sources.add_source(v, str(proj), "p")
    real = (("p/a.md", sha256_file(proj / "a.md")),)
    _seed(v, {
        "lock": (_fm("Lock", sources=real), "# Lock\n\nIl lock serializza le scritture ^[p/a.md#L1-L2].\n\n"
                                            "> [!quote]- Originale (en)\n> The lock serialises writes."),
        "journal": (_fm("Journal del lock", sources=real), "Il journal registra ogni scrittura del lock." * 20),
        "segreto": (_fm("Lock di casa", visibility="private"), "Il codice del lock di casa."),
    })
    pack = context_pack.build(v, "lock", budget=4000)
    paths = [p["path"] for p in pack["pages"]]
    assert "wiki/concepts/segreto.md" not in paths and pack["private_excluded"] == 1
    assert "^[p/a.md#L1-L2]" in pack["markdown"] and "serialises" not in pack["markdown"]
    assert all(p["freshness"] == "fresh" for p in pack["pages"])
    small = context_pack.build(v, "lock", budget=200)
    assert small["used_tokens"] <= 220  # the budget holds: pages are cut or left out, never overflow
    assert small["omitted_for_budget"] or any(p["included"] != "full" for p in small["pages"])
    assert all(p["included"] == "summary" for p in context_pack.build(v, "lock", summary_only=True)["pages"])
    (proj / "a.md").write_text("cambiato\n", "utf-8")
    assert context_pack.build(v, "serializza")["pages"][0]["freshness"] == "stale"


# ---------------- weekly review ----------------

def test_weekly_review_finds_stuck_goals_and_saves_the_plan(v):
    personal.goals_add(v, "Finire Mnemoloci")
    personal.goals_add(v, "Imparare il portoghese")
    personal.todos_add(v, "Scrivere i test", goal="g1")
    personal.todos_update(v, "t1", status="done")
    personal.todos_add(v, "Rilasciare la 1.0", goal="g1")
    data = weekly.review(v)
    by_id = {g["id"]: g for g in data["goals"]}
    assert by_id["g1"]["moved"] and by_id["g1"]["done_this_week"] == ["Scrivere i test"]
    assert data["stuck_goals"] == ["g2"] and data["goals_without_next_step"] == ["g2"]
    out = weekly.save(v, "- g2: 20 minuti al giorno di Duolingo\n- g1: rilascio venerdì")
    fm, body = split_frontmatter((v / out["path"]).read_text("utf-8"))
    assert fm["stuckGoals"] == ["g2"] and "Duolingo" in body and "## Riepilogo" in body


# ---------------- refresh stale, as-of ----------------

def test_plan_stale_only_refreshes_cited_sources(v, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.md").write_text("vecchio\n", "utf-8")
    sources.add_source(v, str(proj), "p")
    _seed(v, {"a": (_fm("A", sources=(("p/a.md", sha256_file(proj / "a.md")),)), "x ^[p/a.md#L1-L1]")})
    (proj / "a.md").write_text("nuovo\n", "utf-8")
    (proj / "b.md").write_text("materiale nuovo\n", "utf-8")
    plan = compiler.plan(v, stale_only=True)
    assert [f["key"] for f in plan["files"]] == ["p/a.md"]


def test_as_of_filters_the_whole_pool_before_the_limit(v):
    old = (TODAY - timedelta(days=400)).isoformat()
    _seed(v, {f"tariffa-{i}": (_fm(f"Tariffa {i}", validUntil=old if i < 3 else None) if i < 3 else _fm(f"Tariffa {i}"),
                               "La tariffa del parcheggio.") for i in range(4)})
    hits = index.query(v, "tariffa parcheggio", limit=1, as_of=TODAY.isoformat())["results"]
    assert [h["path"] for h in hits] == ["wiki/concepts/tariffa-3.md"]
