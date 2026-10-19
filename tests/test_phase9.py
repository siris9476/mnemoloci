"""Phase 9: maintenance that writes (merge, links, synthesis), reinforced lessons, prompt hints and
periodic reflection, instincts -> skills, skill patch and curation."""

import json
import os
import time
from pathlib import Path

import pytest

from mnemoloci import hooks, index, instincts, lint, maintain, memory, review, skill_curator, skill_factory
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import ConflictError, MnemoError, ValidationError
from mnemoloci.markdown import infer_relationship_type, render_page, split_frontmatter
from mnemoloci.pages import alias_map, iter_pages

CWD = r"C:\Users\you\Work\Demo"
SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _page(title, body, *, aliases=(), sources=(("p/a.md", SHA_A),), kind="concept", tags=()):
    fm = {"title": title, "kind": kind, "summary": f"Riassunto di {title}.", "aliases": list(aliases),
          "sources": [{"key": k, "sha256": s} for k, s in sources], "confidence": 0.9, "provenanceState": "extracted",
          "contradictedBy": [], "updated": "2026-09-24", "language": "it", "tags": list(tags)}
    return render_page(fm, body)


def _seed(v, pages):
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"seed-{len(pages)}-{id(pages)}", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{slug}.md", "action": "create", "content": c} for slug, c in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


# ---------------- merge ----------------

def test_duplicates_merge_through_review_keeps_a_stub(v, tmp_path):
    _seed(v, {
        "lock-di-vault": _page("Lock di vault", "Il lock protegge il vault.", aliases=["Vault lock"]),
        "lock-del-vault": _page("Lock del vault", "Serializza le scritture.", sources=(("p/b.md", SHA_B),)),
        "journal": _page("Journal", "Vedi [[lock-del-vault]].", sources=(("p/c.md", SHA_A),)),
    })
    pairs = maintain.duplicates(v)["pairs"]
    assert (pairs[0]["a"], pairs[0]["b"]) == ("lock-del-vault", "lock-di-vault")
    merged = tmp_path / "merged.md"
    merged.write_text(_page("Lock di vault", "Il lock protegge il vault e serializza le scritture.",
                            aliases=["Vault lock"]), "utf-8")
    with pytest.raises(ValidationError) as info:  # the absorbed page's source would be lost
        maintain.propose_merge(v, keep="lock-di-vault", absorb="lock-del-vault", page_file=str(merged))
    assert info.value.code == "SOURCES_LOST"
    merged.write_text(_page("Lock di vault", "Il lock protegge il vault e serializza le scritture.",
                            aliases=["Vault lock"], sources=(("p/a.md", SHA_A), ("p/b.md", SHA_B))), "utf-8")
    out = maintain.propose_merge(v, keep="lock-di-vault", absorb="lock-del-vault", page_file=str(merged))
    shown = review.show(v, out["candidate"])
    assert shown["extra_writes"][0]["target_path"] == "wiki/concepts/lock-del-vault.md"
    approved = review.approve(v, out["candidate"], shown["content_sha256"])
    assert approved["also"] == ["wiki/concepts/lock-del-vault.md"]
    keep_fm, _ = split_frontmatter((v / "wiki/concepts/lock-di-vault.md").read_text("utf-8"))
    stub_fm, stub_body = split_frontmatter((v / "wiki/concepts/lock-del-vault.md").read_text("utf-8"))
    assert "Lock del vault" in keep_fm["aliases"] and stub_fm["mergedInto"] == "lock-di-vault"
    assert "[[lock-di-vault|Lock di vault]]" in stub_body
    pages = [p for p in iter_pages(v, ("wiki",)) if not p.error]
    assert alias_map(pages)["lock-del-vault"] == "lock-di-vault"  # old links now reach the merged page
    report = lint.lint(v)
    assert report["counts"]["broken_links"] == 0 and "lock-del-vault" not in report["issues"]["orphans"]
    assert [r["path"] for r in index.query(v, "serializza")["results"]] == ["wiki/concepts/lock-di-vault.md"]
    assert "lock-del-vault" not in (v / "wiki/index.md").read_text("utf-8")


# ---------------- links and synthesis ----------------

def test_missing_links_are_suggested_and_applied_with_approval(v, tmp_path):
    _seed(v, {
        "journal": _page("Journal", "Registro delle scritture."),
        "lock": _page("Lock", "# Journal\n\n> citazione sul journal\n\nPrima di scrivere si aggiorna il journal e poi il file."),
    })
    suggestions = maintain.link_suggestions(v)["suggestions"]
    assert suggestions == [{"page": "lock", "path": "wiki/concepts/lock.md", "target": "journal", "text": "journal",
                            "excerpt": "Prima di scrivere si aggiorna il journal e poi il file.",
                            "score": 4, "confidence": "media", "signals": ["menzione esatta"]}]
    dry = maintain.apply_links(v, suggestions)
    assert dry["dry_run"] and dry["pages"] == ["wiki/concepts/lock.md"]
    with pytest.raises(ConflictError):
        maintain.apply_links(v, suggestions, approved="0" * 64)
    maintain.apply_links(v, suggestions, approved=dry["approval_sha256"])
    body = (v / "wiki/concepts/lock.md").read_text("utf-8")
    assert "aggiorna il [[journal|journal]] e poi" in body and "# Journal" in body and "> citazione sul journal" in body
    assert maintain.link_suggestions(v)["suggestions"] == []


def test_apply_links_infers_and_records_relationship_type(v):
    _seed(v, {
        "journal": _page("Journal", "Registro delle scritture."),
        "lock": _page("Lock", "Il lock usa journal per registrare ogni scrittura."),
    })
    suggestions = maintain.link_suggestions(v)["suggestions"]
    dry = maintain.apply_links(v, suggestions)
    maintain.apply_links(v, suggestions, approved=dry["approval_sha256"])
    fm, body = split_frontmatter((v / "wiki/concepts/lock.md").read_text("utf-8"))
    assert fm["relationships"] == [{"target": "[[journal]]", "type": "uses"}]
    assert "[[journal|journal]]" in body


@pytest.mark.parametrize("sentence,expected", [
    ("Questo approccio estende il motore transazionale.", "extends"),
    ("La skill implementa il pattern descritto altrove.", "implements"),
    ("Questa misura contraddice la stima precedente.", "contradicts"),
    ("Il valore deriva da un calcolo indipendente.", "derived_from"),
    ("La versione 2 sostituisce il vecchio formato.", "replaces"),
    ("Il modulo dipende da una libreria esterna.", "uses"),
    ("Nessun verbo di relazione qui, solo un fatto.", "related_to"),
])
def test_infer_relationship_type_from_sentence(sentence, expected):
    assert infer_relationship_type(sentence) == expected


def test_link_suggestions_score_shared_tags_and_peripheral_to_hub(v):
    pages = {"hub": _page("Hub", "Pagina centrale.", tags=["ricerca", "importante"])}
    for i in range(8):
        pages[f"satellite-{i}"] = _page(f"Satellite {i}", "Riferimento a [[hub]].", tags=["varie"])
    pages["periferica"] = _page("Periferica", "Nessuna menzione testuale di nessuno qui.",
                                tags=["ricerca", "importante"])
    _seed(v, pages)
    suggestions = maintain.link_suggestions(v)["suggestions"]
    found = next(s for s in suggestions if s["page"] == "periferica" and s["target"] == "hub")
    assert found["text"] is None  # no textual mention: apply_links can report it but not place it inline
    assert found["score"] == maintain.TAG_OVERLAP_SCORE + maintain.HUB_REACH_SCORE
    assert found["confidence"] == "media"


def test_synthesis_candidates_and_submit_page(v, tmp_path):
    _seed(v, {
        "lock": _page("Lock", "Il lock."),
        "journal": _page("Journal", "Il journal."),
        "scritture": _page("Scritture sicure", "Usano [[lock]] e [[journal]]."),
        "recupero": _page("Recupero", "Dopo un crash servono [[lock]] e [[journal]]."),
    })
    pair = maintain.synthesis(v)["pairs"][0]
    assert (pair["a"], pair["b"]) == ("journal", "lock") and pair["cited_together_in"] == ["recupero", "scritture"]
    page = tmp_path / "confronto.md"
    page.write_text(_page("Lock e journal", "Il [[lock]] serializza, il [[journal]] rende recuperabile.",
                          kind="comparison"), "utf-8")
    out = maintain.submit_page(v, str(page))
    assert out["target"] == "wiki/comparisons/lock-e-journal.md"
    assert "manual" in [c for c in review.list_candidates(v) if c["id"] == out["candidate"]][0]["reasons"]
    page.write_text(_page("Altro", "Vedi [[inesistente]].", kind="overview"), "utf-8")
    with pytest.raises(ValidationError):
        maintain.submit_page(v, str(page))
    page.write_text(_page("Lock", "Doppione.", kind="concept"), "utf-8")
    with pytest.raises(ConflictError):
        maintain.submit_page(v, str(page))


# ---------------- lessons, hints, reflection ----------------

def test_lessons_are_reinforced_not_duplicated(v):
    first = memory.add(v, "lesson", "Su Windows usare msvcrt per il lock", "Il file lock si libera da solo.")
    again = memory.add(v, "lesson", "Su Windows usare msvcrt per il lock", "Confermato anche dopo un crash.")
    assert again["reinforced"] and again["confirmations"] == 2 and again["path"] == first["path"]
    assert len(memory.list_memory(v, kind="lesson")) == 1
    body = memory.read(v, first["id"])["body"]
    assert "Confermato anche dopo un crash." in body and "Confermata il" in body


def test_prompt_hints_once_per_session_and_periodic_reflection(v):
    memory.add(v, "lesson", "Su Windows usare msvcrt per il lock", "Il lock msvcrt si rilascia se il processo muore.")
    memory.add(v, "lesson", "Lock del progetto Altro", "Solo per l'altro progetto.", project=r"C:\Users\you\Work\Other")
    cfg = v / ".mnemoloci/config.json"
    data = json.loads(cfg.read_text("utf-8"))
    data["reflection"] = {"every": 3}
    cfg.write_text(json.dumps(data), "utf-8")
    event = {"session_id": "s1", "cwd": CWD, "prompt": "come gestisco il lock su windows dopo un crash?"}
    first = hooks.user_prompt(v, event)["hookSpecificOutput"]["additionalContext"]
    assert "Lezione: Su Windows usare msvcrt per il lock" in first and "altro progetto" not in first.lower()
    assert "Promemoria" not in first
    assert hooks.user_prompt(v, event) is None  # already shown in this session, not the 3rd prompt yet
    third = hooks.user_prompt(v, event)["hookSpecificOutput"]["additionalContext"]
    assert "Promemoria Mnemoloci (3 messaggi" in third and "Lezione" not in third
    assert hooks.user_prompt(v, {"session_id": "s2", "cwd": CWD, "prompt": "ciao"}) is None


# ---------------- instincts -> skills ----------------

def _instinct(v, iid, trigger, action):
    fm = {"title": f"{trigger} → {action}", "schema": instincts.SCHEMA, "id": iid, "trigger": trigger,
          "action": action, "scope": "global", "status": "active", "confidence": 0.5,
          "evidence": [{"session_id": "s", "ts": "2026-09-24T10:00:00Z", "project": CWD, "quote": "x"}],
          "created": "2026-09-24", "updated": "2026-09-24"}
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"ins-{iid}", "operation": "test",
         "writes": [{"path": f"{instincts.FOLDER}/{iid[4:]}.md", "action": "create", "content": instincts._render(fm)}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)


def test_instincts_evolve_into_skill_material(v):
    _instinct(v, "ins_a", "analizzare progetti esterni", "leggere codice senza installare nulla")
    _instinct(v, "ins_b", "valutare progetti esterni", "leggere documentazione e codice, mai installare")
    _instinct(v, "ins_c", "scrivere commit", "usare messaggi in inglese")
    clusters = instincts.evolve(v)["clusters"]
    assert len(clusters) == 1 and [i["id"] for i in clusters[0]["instincts"]] == ["ins_a", "ins_b"]
    assert "instincts" in skill_factory.ORIGINS


# ---------------- skills: patch and curation ----------------

def _generated(v, tmp_path, name="rilascio"):
    draft = tmp_path / "drafts" / name
    draft.mkdir(parents=True)
    (draft / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Procedura di rilascio.\n---\n\n"
                                    "# Rilascio\n\n1. Esegui i test.\n2. Crea il tag.\n", "utf-8")
    skill_factory.submit(v, str(draft), origin="manual")


def test_patch_generated_skill_makes_a_new_version(v, tmp_path):
    _generated(v, tmp_path)
    with pytest.raises(ValidationError) as info:
        skill_factory.patch(v, "rilascio", old="inesistente", new="x")
    assert info.value.code == "PATCH_NOT_UNIQUE"
    out = skill_factory.patch(v, "rilascio", old="2. Crea il tag.", new="2. Crea il tag firmato.")
    assert out["patched"] == "SKILL.md" and out["guard_verdict"] == "safe"
    assert "tag firmato" in (v / "_generated-skills/rilascio/SKILL.md").read_text("utf-8")
    assert list((v / ".mnemoloci/skill-versions/rilascio").iterdir())  # previous version kept
    with pytest.raises(MnemoError):
        skill_factory.patch(v, "non-esiste", old="a", new="b")


def test_skill_usage_curate_archive_restore_pin(v, tmp_path):
    claude = tmp_path / "claude"
    for name in ("alpha", "beta"):
        (claude / "skills" / name).mkdir(parents=True)
        (claude / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nbody\n", "utf-8")
        old = time.time() - 400 * 86400  # installed long ago: a skill installed inside the window is never "unused"
        os.utime(claude / "skills" / name / "SKILL.md", (old, old))
    folder = claude / "projects" / "D--Work-Demo"
    folder.mkdir(parents=True)
    recs = [{"type": "assistant", "sessionId": "s", "timestamp": "2099-01-01T10:00:00Z", "message": {"role": "assistant",
             "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "alpha"}}]}},
            {"type": "user", "sessionId": "s", "timestamp": "2025-01-01T10:00:00Z",
             "message": {"role": "user", "content": "<command-name>/beta</command-name>"}}]
    (folder / "s.jsonl").write_text("\n".join(json.dumps(r) for r in recs), "utf-8")
    used = skill_curator.usage(v)
    assert used["alpha"]["count"] == 1 and used["beta"]["last_used"].startswith("2025")
    report = skill_curator.curate(v, unused_days=60)
    assert [u["name"] for u in report["unused"]] == ["beta"] and report["unused"][0]["archivable"]
    skill_curator.pin(v, "beta")
    assert skill_curator.curate(v)["unused"] == []
    with pytest.raises(ConflictError):
        skill_curator.archive(v, "beta")
    skill_curator.pin(v, "beta", False)
    moved = skill_curator.archive(v, "beta")["archived"][0]
    assert not (claude / "skills" / "beta").exists() and Path(moved["to"]).is_dir()
    skill_curator.restore(v, "beta")
    assert (claude / "skills" / "beta" / "SKILL.md").is_file()
