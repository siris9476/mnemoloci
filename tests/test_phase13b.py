"""Phase 13 block B: two independent sources, session topics, filing modes, log folds, voice
samples, JSON-LD/Marp export, skill trigger compliance."""

import json
import os
from pathlib import Path

import pytest

from mnemoloci import (clips, export, filing, folds, index, lint, personal, session_index, skill_curator, sources, voice)
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.markdown import render_page, split_frontmatter
from mnemoloci.validate import check_page, independent_sources

CWD = r"C:\Users\you\Work\Demo"


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _fm(title, keys=("p/a.md",), **extra):
    return {"title": title, "kind": "concept", "summary": f"{title}.", "aliases": [],
            "sources": [{"key": k, "sha256": "a" * 64} for k in keys], "confidence": 0.9, "provenanceState": "extracted",
            "contradictedBy": [], "updated": "2026-09-24", "language": "it", **extra}


def _seed(v, pages):
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"seed-{id(pages)}", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{s}.md", "action": "create", "content": render_page(fm, body)}
                    for s, (fm, body) in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


def _session(tmp_path, sid, prompts, skill_after=None):
    folder = tmp_path / "claude" / "projects" / "D--Work-Demo"
    folder.mkdir(parents=True, exist_ok=True)
    recs = []
    for i, text in enumerate(prompts):
        recs.append({"type": "user", "sessionId": sid, "cwd": CWD, "timestamp": f"2026-09-2{i}T10:00:00Z",
                     "message": {"role": "user", "content": text}})
        if skill_after is not None and i in skill_after:
            recs.append({"type": "assistant", "sessionId": sid, "cwd": CWD, "timestamp": f"2026-09-2{i}T10:00:05Z",
                         "message": {"role": "assistant", "id": f"{sid}{i}", "content": [
                             {"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": "salvataggio"}}]}})
        recs.append({"type": "assistant", "sessionId": sid, "cwd": CWD, "timestamp": f"2026-09-2{i}T10:00:09Z",
                     "message": {"role": "assistant", "id": f"{sid}r{i}", "content": [{"type": "text", "text": "ok"}]}})
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs), "utf-8")


# ---------------- two independent sources ----------------

def test_critical_pages_need_two_independent_sources(v, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    sources.add_source(v, str(proj), "proj")
    a = clips.add_text(v, "Fonte A sul lock.", title="A", url="https://www.example.org/a")
    b = clips.add_text(v, "Fonte B sul lock.", title="B", url="https://example.org/b")
    c = clips.add_text(v, "Fonte C sul lock.", title="C", url="https://other.net/c")
    reg = sources.load_sources(v)["sources"]
    key = lambda out: out["path"].replace("raw/clips/", "clips/")  # noqa: E731
    assert independent_sources(v, ["proj/a.md", "proj/b.md"], reg) == 1          # one project, one voice
    assert independent_sources(v, [key(a), key(b)], reg) == 1                     # same site
    assert independent_sources(v, [key(a), key(c)], reg) == 2
    _seed(v, {"x": (_fm("X", keys=("proj/a.md", "proj/b.md"), critical=True), "x"),
              "y": (_fm("Y", keys=(key(a), key(c)), critical=True), "y")})
    assert [i["path"] for i in lint.lint(v)["issues"]["single_source"]] == ["wiki/concepts/x.md"]
    content = render_page(_fm("Z", keys=("proj/a.md",), critical=True), "z ^[proj/a.md#L1-L1]")
    _, holds, _ = check_page(content, config=vault_mod.load_config(v), line_count=lambda k: 3,
                             source_kind=lambda k: "project", independence=lambda keys: independent_sources(v, keys, reg))
    assert "single-source" in {h["code"] for h in holds}


# ---------------- topics ----------------

def test_sessions_are_grouped_by_topic(v, tmp_path):
    _session(tmp_path, "s-lock-1", ["il lock del vault su windows si blocca", "come libero il lock msvcrt del vault"])
    _session(tmp_path, "s-lock-2", ["errore lock vault windows dopo crash", "il lock msvcrt resta preso"])
    _session(tmp_path, "s-pasta-1", ["ricetta della pasta al pomodoro", "quanto sale nella pasta"])
    _session(tmp_path, "s-pasta-2", ["pasta al pomodoro fresco ricetta", "tempo di cottura della pasta"])
    out = session_index.topics(v)
    groups = sorted(sorted(s["session_id"] for s in t["sessions"]) for t in out["topics"])
    assert groups == [["s-lock-1", "s-lock-2"], ["s-pasta-1", "s-pasta-2"]]
    assert any("pasta" in t["topic"] for t in out["topics"])


# ---------------- filing, folds ----------------

def test_filing_modes_write_navigation_only(v, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    sources.add_source(v, str(proj), "proj")
    _seed(v, {"lock": (_fm("Lock", keys=("proj/a.md",)), "Vedi [[journal]]."),
              "journal": (_fm("Journal", keys=("clips/x.md",)), "Vedi [[lock]]."),
              "solo": (_fm("Solo", keys=("proj/b.md",), validUntil="2020-01-01"), "nessun link")})
    personal.goals_add(v, "Capire i lock", area="lavoro", links=["lock"])
    para = filing.generate(v, "para")
    progetti = (v / "_meta/filing/para/Progetti.md").read_text("utf-8")
    assert "## proj" in progetti and "[[lock|Lock]]" in progetti and "Solo" not in progetti
    assert "[[solo|Solo]]" in (v / "_meta/filing/para/Archivio.md").read_text("utf-8")
    assert "## lavoro" in (v / "_meta/filing/para/Aree.md").read_text("utf-8")
    assert "[[journal|Journal]]" in (v / "_meta/filing/para/Risorse.md").read_text("utf-8")
    assert len(para["notes"]) == 4
    lyt = filing.generate(v, "lyt")
    assert "_meta/filing/lyt/home.md" in lyt["notes"] and any("moc" in n for n in lyt["notes"])
    (v / "_meta/filing/zettelkasten").mkdir(parents=True)
    (v / "_meta/filing/zettelkasten/indice.md").write_text("# mio\n", "utf-8")
    assert filing.generate(v, "zettelkasten")["skipped_not_ours"] == ["_meta/filing/zettelkasten/indice.md"]


def test_log_fold_summarises_past_months_without_touching_the_log(v):
    log = v / "wiki/log.md"
    old = log.read_text("utf-8")
    log.write_text("# Log\n\n## [2026-08-03 10:00] compile r20260803-100000 — pubblicate 2, in revisione 0\n- [[lock]]\n- [[journal]]\n"
                   "\n## [2026-08-09 11:00] risposta salvata: [[risposta-x]]\n" + old.split("\n", 2)[-1], "utf-8")
    before = log.read_text("utf-8")
    out = folds.fold(v)
    fold = (v / "_meta/folds/2026-08.md").read_text("utf-8")
    assert out["months"] == ["2026-08"] and "- compile: 1" in fold and "[[lock]] — 2026-08-03" in fold
    assert log.read_text("utf-8") == before and folds.fold(v)["written"] == []


# ---------------- voice ----------------

def test_voice_samples_are_prose_the_user_wrote(v, tmp_path):
    clips.add_text(v, "Io penso che un second brain debba restare semplice. " * 8, title="Mio pensiero", mine=True)
    clips.add_text(v, "Articolo di qualcun altro sul second brain. " * 10, title="Altrui")
    _session(tmp_path, "s-v", ["Vorrei che l'app fosse davvero comoda da usare dal telefono, senza passaggi inutili. " * 5,
                                "```\nTraceback (most recent call last):\n  File x\n```\n" * 10])
    session_index.refresh(v)
    out = voice.samples(v)
    froms = [s["from"] for s in out["samples"]]
    assert any(f.startswith("notes/") for f in froms) and any(f.startswith("sessione") for f in froms)
    assert not any("Traceback" in s["text"] for s in out["samples"]) and not any("Altrui" in f for f in froms)


# ---------------- exports ----------------

def test_jsonld_and_marp_exports(v, tmp_path):
    _seed(v, {"marco": ({**_fm("Marco", kind="entity"), "entityType": "person"}, "Lavora con [[lock]]."),
              "lock": (_fm("Lock", aliases=["Vault lock"]), "# Lock\n\nIntro ^[p/a.md#L1-L2].\n\n## Come funziona\n\n"
                                                              "Serializza [[marco|con Marco]] ^[p/a.md#L3-L3].\n\n"
                                                              "> [!quote]- Originale (en)\n> Serialises."),
              "segreto": (_fm("Segreto", visibility="private"), "x")})
    graph = json.loads(Path(export.export_jsonld(v, str(tmp_path / "wiki.jsonld"))["out"]).read_text("utf-8"))["@graph"]
    by = {n["@id"]: n for n in graph}
    assert by["urn:mnemoloci:marco"]["@type"] == "Person" and by["urn:mnemoloci:lock"]["@type"] == "DefinedTerm"
    assert by["urn:mnemoloci:lock"]["alternateName"] == ["Vault lock"] and "urn:mnemoloci:segreto" not in by
    assert {"@id": "urn:mnemoloci:lock"} in by["urn:mnemoloci:marco"]["mentions"]
    deck_out = export.export_marp(v, "wiki/concepts/lock.md", str(tmp_path / "deck.md"))
    deck = (tmp_path / "deck.md").read_text("utf-8")
    assert deck.startswith("---\nmarp: true") and deck_out["slides"] == 4 and deck_out["sources"] == 2
    assert "con Marco" in deck and "[[" not in deck and "Serialises" not in deck and "## Fonti" in deck


# ---------------- trigger compliance ----------------

def test_skill_trigger_compliance_is_measured(v, tmp_path):
    skill = tmp_path / "claude" / "skills" / "salvataggio"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: salvataggio\ndescription: Salva la sessione. Use when the user says "
                                    "\"salva la sessione\" or \"fai un handoff\".\n---\n\nbody\n", "utf-8")
    os.utime(skill / "SKILL.md", (1_700_000_000, 1_700_000_000))  # installed (2023) before the messages
    _session(tmp_path, "s-t", ["per favore salva la sessione", "altro lavoro", "ok fai un handoff adesso"], skill_after={0})
    out = skill_curator.trigger_audit(v)
    row = out["results"][0]
    assert row["skill"] == "salvataggio" and row["expected"] == 2 and row["fired"] == 1 and row["rate"] == 0.5
    assert "fai un handoff" in row["missed_examples"][0]["message"]
    os.utime(skill / "SKILL.md", None)  # updated just now: older messages no longer count
    assert skill_curator.trigger_audit(v)["measured"] == 0
