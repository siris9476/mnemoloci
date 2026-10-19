"""Phase 10: people/entities, graph analysis, validity in time, extra lint + quality score,
Obsidian canvas/bases/graph colours, daily schedule, retract, llms.txt export."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from mnemoloci import (cli, compile as compiler, export, graph_analysis, index, lint, maintain, memory, obsidian,
                       quality, schedule, sources)
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import MnemoError, ValidationError
from mnemoloci.markdown import render_page, split_frontmatter
from mnemoloci.validate import LineCounter, check_page

TODAY = date.today()
SHA = "a" * 64


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _fm(title, kind="concept", **extra):
    return {"title": title, "kind": kind, "summary": f"{title}.", "aliases": [], "sources": [{"key": "p/a.md", "sha256": SHA}],
            "confidence": 0.9, "provenanceState": "extracted", "contradictedBy": [], "updated": TODAY.isoformat(),
            "language": "it", **extra}


def _seed(v, pages):
    dirs = {"concept": "concepts", "entity": "entities", "comparison": "comparisons", "overview": "overviews"}
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"seed-{id(pages)}", "operation": "test",
         "writes": [{"path": f"wiki/{dirs[fm['kind']]}/{slug}.md", "action": "create", "content": render_page(fm, body)}
                    for slug, (fm, body) in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


# ---------------- entities ----------------

def test_entity_type_flows_from_extract_to_page_and_is_validated(v, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("# Team\n\nMarco Rossi guida il progetto.\n", "utf-8")
    sources.add_source(v, str(proj), "proj")
    run = compiler.plan(v)
    packet = json.loads(Path(run["files"][0]["packet"]).read_text("utf-8"))
    extract = {"schema": compiler.EXTRACT_SCHEMA, "source_key": packet["source_key"], "sha256": packet["sha256"],
               "source_language": "it", "concepts": [{"title": "Marco Rossi", "kind": "entity", "entity_type": "person",
                                                      "summary": "Guida il progetto.", "confidence": 0.9,
                                                      "provenance": "extracted",
                                                      "claims": [{"text": "Guida il progetto.", "lines": [3, 3]}]}]}
    bad = dict(extract, concepts=[dict(extract["concepts"][0], entity_type="alien")])
    Path(packet["extract_path"]).write_text(json.dumps(bad), "utf-8")
    with pytest.raises(MnemoError):
        compiler.merge(v)
    Path(packet["extract_path"]).write_text(json.dumps(extract), "utf-8")
    job = json.loads(Path(compiler.merge(v)["jobs"][0]["job"]).read_text("utf-8"))
    assert job["suggested_frontmatter"]["entityType"] == "person"
    config = vault_mod.load_config(v)
    _, holds, _ = check_page(render_page(_fm("X", "entity", entityType="alien", validUntil="ieri"), "x ^[p/a.md#L1-L1]"),
                             config=config, line_count=lambda k: 5, source_kind=lambda k: "project")
    assert {h["code"] for h in holds} == {"schema-violating"} and len(holds) == 2


def test_people_ranked_by_mentions_sources_and_recency(v):
    old = (TODAY - timedelta(days=720)).isoformat()
    _seed(v, {
        "marco": (_fm("Marco", "entity", entityType="person"), "Collega."),
        "giulia": (_fm("Giulia", "entity", entityType="person", updated=old), "Ex collega."),
        "acme": (_fm("Acme", "entity", entityType="organization"), "Azienda."),
        "progetto": (_fm("Progetto"), "Con [[marco]] e [[giulia]] per [[acme]]."),
        "riunione": (_fm("Riunione"), "Presenti [[marco]] e [[giulia]]."),
    })
    people = graph_analysis.entities(v)["entities"]
    assert [p["title"] for p in people] == ["Marco", "Giulia"] and people[0]["mentioned_by"] == 2
    assert [e["title"] for e in graph_analysis.entities(v, entity_type="organization")["entities"]] == ["Acme"]
    assert graph_analysis.entities(v, entity_type=None)["total"] == 3


# ---------------- graph analysis ----------------

def test_graph_hubs_bridges_communities_and_isolated(v):
    pages = {}
    for group, names in (("a", ["a1", "a2", "a3"]), ("b", ["b1", "b2", "b3"])):
        for n in names:
            others = " ".join(f"[[{m}]]" for m in names if m != n)
            pages[n] = (_fm(n.upper()), f"Gruppo {group}: {others}")
    pages["ponte"] = (_fm("Ponte"), "Unisce [[a1]] e [[b1]].")
    pages["solo"] = (_fm("Solo"), "Nessun collegamento.")
    _seed(v, pages)
    g = graph_analysis.analyze(v)
    assert g["bridges"][0]["title"] in ("Ponte", "A1", "B1") and "Solo" in g["isolated"]
    assert len([c for c in g["communities"] if c["size"] >= 3]) == 2
    assert g["hubs"][0]["links"] == 3 and g["links"] == 8


def test_backfill_finds_graph_and_time_close_pages_not_text_similar_ones(v):
    near = (TODAY - timedelta(days=3)).isoformat()
    far = (TODAY - timedelta(days=60)).isoformat()
    deep = (TODAY - timedelta(days=5)).isoformat()
    _seed(v, {
        "bug": (_fm("Bug nel motore", updated=TODAY.isoformat()), "Riferisce a [[helper]] e [[old-neighbor]]."),
        "helper": (_fm("Modulo di supporto", updated=near), "Collegato a [[bug]] e [[deep-page]]."),
        "old-neighbor": (_fm("Vicino vecchio", updated=far), "Collegato a [[bug]]."),
        "deep-page": (_fm("Pagina profonda", updated=deep), "Collegato a [[helper]]."),
        "unrelated": (_fm("Bug nel motore simile", updated=TODAY.isoformat()), "Nessun collegamento a nulla."),
    })
    out = graph_analysis.backfill(v, "wiki/concepts/bug.md", window_days=14)
    slugs = {r["slug"] for r in out["results"]}
    assert slugs == {"helper", "deep-page"}  # graph AND time close
    assert "old-neighbor" not in slugs  # graph-close but time-far
    assert "unrelated" not in slugs  # text-similar title but no graph edge at all
    one_hop = graph_analysis.backfill(v, "wiki/concepts/bug.md", window_days=14, hops=1)
    assert {r["slug"] for r in one_hop["results"]} == {"helper"}
    with pytest.raises(MnemoError):
        graph_analysis.backfill(v, "wiki/concepts/nope.md")


# ---------------- validity in time ----------------

def test_validity_expiry_refresh_and_as_of_search(v):
    _seed(v, {"tariffa-2025": (_fm("Tariffa 2025"), "La tariffa vale 10 euro."),
              "tariffa-2026": (_fm("Tariffa 2026"), "La tariffa vale 12 euro.")})
    maintain.set_validity(v, "tariffa-2025", valid_from="2025-01-01", superseded_by="tariffa-2026")
    maintain.set_validity(v, "tariffa-2026", valid_from=TODAY.isoformat(), refresh_due=TODAY.isoformat())
    with pytest.raises(ValidationError):
        maintain.set_validity(v, "tariffa-2026", valid_until="2020-01-01")  # before validFrom
    fm, _ = split_frontmatter((v / "wiki/concepts/tariffa-2025.md").read_text("utf-8"))
    assert fm["supersededBy"] == "tariffa-2026" and fm["validUntil"] == TODAY.isoformat()
    report = lint.lint(v)
    assert report["issues"]["refresh_due"][0]["path"] == "wiki/concepts/tariffa-2026.md"
    past = (TODAY - timedelta(days=30)).isoformat()
    assert [r["path"] for r in index.query(v, "tariffa", as_of=past)["results"]] == ["wiki/concepts/tariffa-2025.md"]
    maintain.set_validity(v, "tariffa-2025", valid_until=past)
    default = {r["path"] for r in index.query(v, "tariffa")["results"]}
    assert "wiki/concepts/tariffa-2025.md" not in default  # expired pages excluded by default now
    flagged = {r["path"]: r for r in index.query(v, "tariffa", include_expired=True)["results"]}
    assert flagged["wiki/concepts/tariffa-2025.md"]["expired"] == past
    assert flagged["wiki/concepts/tariffa-2025.md"]["superseded_by"] == "tariffa-2026"
    assert lint.lint(v)["issues"]["expired"][0]["path"] == "wiki/concepts/tariffa-2025.md"


# ---------------- quality ----------------

def test_extra_lint_checks_and_quality_score_with_gate(v, tmp_path, capsys):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.md").write_text("una\ndue\n", "utf-8")
    sources.add_source(v, str(proj), "p")
    real = {"key": "p/a.md", "sha256": SHA}
    _seed(v, {
        "buona": (_fm("Buona", sources=[real]), "Testo ^[p/a.md#L1-L2]. Vedi [[vuota]]."),
        "vuota": (_fm("Vuota", sources=[real]), "## Sezione vuota\n\n## Piena\n\nTesto ^[p/a.md#L9-L12]."),
        "panorama": (_fm("Panorama", "overview", sources=[real]), "Solo [[buona]] ^[p/a.md#L1-L1]."),
        "lunga": (_fm("Lunga", sources=[real]), ("parola " * 3000) + "^[p/a.md#L1-L1] [[buona]]"),
    })
    issues = lint.lint(v)["issues"]
    assert issues["bad_citations"][0]["citation"] == "p/a.md#L9-L12"
    assert issues["empty_sections"][0]["headings"] == ["Sezione vuota"]
    assert issues["few_links"][0]["path"] == "wiki/overviews/panorama.md"
    assert issues["too_long"][0]["path"] == "wiki/concepts/lunga.md"
    first = quality.run(v, record=True)
    assert 0 <= first["score"] <= 100 and set(first["components"]) == set(quality.WEIGHTS) and first["recorded"]
    assert quality.run(v)["delta"] == 0 and len(quality.history(v)) == 1
    assert cli.main(["--vault", str(v), "quality", "--min-score", "101"]) == 1
    assert cli.main(["--vault", str(v), "quality", "--min-score", "0"]) == 0
    capsys.readouterr()


def test_per_citation_validity_is_flagged_without_touching_the_rest_of_the_page(v):
    past = (TODAY - timedelta(days=1)).isoformat()
    future = (TODAY + timedelta(days=1)).isoformat()
    body = (f"Piano base 10 euro, sempre valido ^[p/a.md#L1-L1].\n"
            f"Piano pro 29 euro, scaduto ^[p/a.md#L2-L2#V..{past}].\n"
            f"Offerta non ancora attiva ^[p/a.md#L3-L3#V{future}..].\n"
            f"Da riverificare ^[p/a.md#L4-L4#R{past}].\n"
            f"Citazione scritta male ^[p/a.md#L5-L5#Vnotadate].\n")
    _seed(v, {"prezzi": (_fm("Prezzi"), body)})
    issues = lint.lint(v)["issues"]
    assert [i["citation"] for i in issues["citation_expired"]] == ["p/a.md#L2-L2"]
    assert [i["citation"] for i in issues["citation_not_yet_valid"]] == ["p/a.md#L3-L3"]
    assert [i["citation"] for i in issues["citation_refresh_due"]] == ["p/a.md#L4-L4"]
    assert issues["citation_syntax_error"] == [{"path": "wiki/concepts/prezzi.md", "text": "^[p/a.md#L5-L5#Vnotadate]"}]
    assert "citation_expired" not in issues or len(issues["citation_expired"]) == 1  # L1 (no validity) never flagged
    page = (v / "wiki/concepts/prezzi.md").read_text("utf-8")
    assert "^[p/a.md#L5-L5#Vnotadate]" in page  # nothing is ever rewritten or dropped, only reported


# ---------------- obsidian ----------------

def test_canvas_bases_and_graph_colours(v):
    _seed(v, {"centro": (_fm("Centro"), "Vedi [[uno]] e [[due]]."), "uno": (_fm("Uno"), "Vedi [[tre]]."),
              "due": (_fm("Due"), "x"), "tre": (_fm("Tre"), "y")})
    out = obsidian.canvas(v, "centro", depth=2)
    doc = json.loads((v / out["path"]).read_text("utf-8"))
    assert {n["id"] for n in doc["nodes"]} == {"centro", "uno", "due", "tre"} and len(doc["edges"]) == 3
    assert obsidian.canvas(v, "centro")["action"] == "replace"
    (v / "_meta/bases").mkdir(parents=True)
    (v / "_meta/bases/Persone.base").write_text("views: []\n", "utf-8")  # the user's own file
    result = obsidian.bases(v)
    assert len(result["bases"]) == 4 and result["skipped"][0]["path"].endswith("Persone.base")
    assert (v / "_meta/bases/Persone.base").read_text("utf-8") == "views: []\n"
    assert "file.inFolder" in (v / "_meta/bases/Da rivedere.base").read_text("utf-8")
    assert obsidian.graph_colors(v)["changed"] is True
    assert obsidian.graph_colors(v)["changed"] is False  # never overwrites existing colour groups


# ---------------- schedule, retract, llms.txt ----------------

def test_schedule_uses_task_scheduler_and_the_run_logs_each_step(v, monkeypatch):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    with pytest.raises(ValidationError):
        schedule.enable(v, at="25:00")
    if os.name == "nt":  # Linux (cron / systemd timer) is covered by test_linux.py
        monkeypatch.setattr(schedule, "_schtasks", lambda *args: calls.append(args) or Done())
        out = schedule.enable(v, at="21:15")
        assert calls[0][:2] == ("/Create", "/SC") and "21:15" in calls[0] and "scheduled-run" in out["command"]
        assert schedule.disable()["removed"]
    record = schedule.run(v)
    assert set(record["steps"]) == {"sources", "connectors", "sessions", "skills_usage", "digest", "quality",
                                    "duplicates", "backup", "verify"}
    assert record["steps"]["backup"] == {"skipped": "backup not enabled"}
    assert record["steps"]["connectors"] == {}  # nothing connected yet, never calls an LLM either way
    assert record["steps"]["verify"] == {"issues": [], "citations": 0, "weak_citations": 0}  # digest exists, no backup/quality claim to recheck, no citations yet
    assert (v / f"_meta/digests/{TODAY.isoformat()}.md").is_file()
    assert schedule.status(v)["last_run"]["steps"]["digest"]["period"] == TODAY.isoformat()


def test_schedule_verify_catches_a_digest_file_that_never_landed(v, monkeypatch):
    from mnemoloci import digest as digest_mod

    monkeypatch.setattr(digest_mod, "save", lambda vault: {"path": "_meta/digests/nope.md", "period": "2020-01-01"})
    record = schedule.run(v)
    assert record["steps"]["verify"]["issues"] == ["digest: _meta/digests/nope.md missing or empty"]


def test_schedule_verify_catches_a_backup_commit_that_is_not_head(v, monkeypatch):
    from mnemoloci import backup as backup_mod

    backup_mod.init(v)
    monkeypatch.setattr(backup_mod, "status", lambda vault: {"enabled": True, "auto_push": False,
                                                              "last_commit": {"id": "0000000"}})
    monkeypatch.setattr(backup_mod, "settings", lambda vault: {"auto_push": False})
    monkeypatch.setattr(backup_mod, "now", lambda vault, **kw: {"committed": True, "commit": "deadbee", "files": 1})
    record = schedule.run(v)
    assert record["steps"]["verify"]["issues"] == ["backup: commit deadbee is not HEAD (0000000)"]


def test_schedule_run_survives_a_broken_connectors_step(v, monkeypatch):
    """connectors.sync_all() already isolates one provider failing from the others; this locks in
    the outer guarantee too -- even if connectors blows up entirely, every other daily step (and
    especially backup, which captures whatever the day's steps produced) still runs."""
    from mnemoloci import connectors

    monkeypatch.setattr(connectors, "sync_all", lambda vault: (_ for _ in ()).throw(RuntimeError("boom")))
    record = schedule.run(v)
    assert "RuntimeError" in record["steps"]["connectors"]["error"]
    assert "digest" in record["steps"] and "period" in record["steps"]["digest"]


def test_retract_keeps_the_note_and_llms_export(v, tmp_path):
    note = memory.add(v, "fact", "Numero di telefono di Anna", "Anna: chiamare dopo le 18.")
    out = memory.retract(v, note["id"], reason="non serve più", redact_text=True)
    body = memory.read(v, note["id"])["body"]
    assert out["redacted"] and "chiamare" not in body and "Testo ritirato" in body
    assert memory.list_memory(v, kind="fact") == [] and memory.list_memory(v, kind="fact", status="retracted")
    with pytest.raises(ValidationError):
        memory.retract(v, note["id"], reason=" ")
    _seed(v, {"lock": (_fm("Lock"), "Il lock."), "vecchia": (_fm("Vecchia", mergedInto="lock"), "Unita.")})
    result = export.export_llms(v, str(tmp_path / "llms"), title="Il mio wiki")
    text = (tmp_path / "llms/llms.txt").read_text("utf-8")
    assert result["pages"] == 1 and "- [Lock](wiki/concepts/lock.md): Lock." in text and "Vecchia" not in text
    assert "Il lock." in (tmp_path / "llms/llms-full.txt").read_text("utf-8")


def test_suppress_hides_a_note_without_a_reason_and_is_reversible(v):
    note = memory.add(v, "fact", "Numero di telefono di Anna", "Anna: chiamare dopo le 18.")
    out = memory.suppress(v, note["id"])
    assert out["suppressed"] == note["id"]
    assert memory.list_memory(v, kind="fact") == []
    assert memory.list_memory(v, kind="fact", status="suppressed")[0]["id"] == note["id"]
    body = memory.read(v, note["id"])["body"]
    assert "chiamare" in body  # unlike retract --redact, nothing is blanked
    with pytest.raises(MnemoError):
        memory.suppress(v, note["id"])  # already suppressed
    back = memory.unsuppress(v, note["id"])
    assert back["unsuppressed"] == note["id"]
    assert memory.list_memory(v, kind="fact")[0]["id"] == note["id"]
    assert "suppressed" not in memory.read(v, note["id"])["frontmatter"]
    with pytest.raises(MnemoError):
        memory.unsuppress(v, note["id"])  # already active
