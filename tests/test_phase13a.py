"""Phase 13 block A: doctor, leftover-work warning at session start, link repair, instinct
promotion/export/import, retrieval eval, bounded research."""

import json
from pathlib import Path

import pytest

from mnemoloci import answers, compile as compiler, doctor, hooks, index, instincts, maintain, quality, research, sources
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import ConflictError, ValidationError
from mnemoloci.markdown import render_page

CWD = r"C:\Users\you\Work\Demo"


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    (tmp_path / "claude").mkdir()
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _fm(title, **extra):
    return {"title": title, "kind": "concept", "summary": f"{title}.", "aliases": [],
            "sources": [{"key": "p/a.md", "sha256": "a" * 64}], "confidence": 0.9, "provenanceState": "extracted",
            "contradictedBy": [], "updated": "2026-09-24", "language": "it", **extra}


def _seed(v, pages):
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"seed-{id(pages)}", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{s}.md", "action": "create", "content": render_page(fm, body)}
                    for s, (fm, body) in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


def test_doctor_reports_each_check_with_a_fix(v):
    report = doctor.run(v)
    by = {c["check"]: c for c in report["checks"]}
    assert by["vault"]["status"] == "ok" and by["transactions"]["status"] == "ok" and by["lock"]["status"] == "ok"
    assert by["plugin"]["status"] == "fail" and "claude plugin" in by["plugin"]["fix"]
    assert report["healthy"] is False and report["counts"]["fail"] >= 1
    assert doctor.run(v.parent / "nessun-vault")["checks"][-1]["fix"].endswith("init")


def test_session_start_warns_about_work_left_half_done(v, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("# P\n", "utf-8")
    sources.add_source(v, str(proj), "proj")
    run = compiler.plan(v)
    assert f"Compilazione {run['run_id']} lasciata a metà" in hooks.build_context(v, CWD)


def test_broken_links_with_a_single_target_are_repaired(v):
    _seed(v, {"lock-di-vault": (_fm("Lock di vault"), "Il lock."),
              "journal": (_fm("Journal"), "Prima prendi il [[lock]], poi scrivi [[journal-delle-scritture|il registro]]."),
              "altro": (_fm("Altro"), "Link a [[vault]].")})
    dry = maintain.fix_links(v)
    assert dry["fixes"] == [{"page": "journal", "from": "lock", "to": "lock-di-vault"}]
    assert {"page": "journal", "link": "journal-delle-scritture"} in dry["unresolved"]
    with pytest.raises(ConflictError):
        maintain.fix_links(v, approved="0" * 64)
    maintain.fix_links(v, approved=dry["approval_sha256"])
    assert "Prima prendi il [[lock-di-vault|lock]]" in (v / "wiki/concepts/journal.md").read_text("utf-8")


def _instinct(v, iid, trigger, action, scope):
    fm = {"title": f"{trigger} → {action}", "schema": instincts.SCHEMA, "id": iid, "trigger": trigger, "action": action,
          "scope": scope, "status": "active", "confidence": 0.5, "created": "2026-09-24", "updated": "2026-09-24",
          "evidence": [{"session_id": "s", "ts": "2026-09-24T10:00:00Z", "project": scope, "quote": "no, così"}]}
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"ins-{iid}", "operation": "test",
         "writes": [{"path": f"{instincts.FOLDER}/{iid[4:]}.md", "action": "create", "content": instincts._render(fm)}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)


def test_instincts_promotion_export_and_import(v, tmp_path):
    _instinct(v, "ins_a", "scrivere commit", "usare messaggi in italiano descrittivi", r"c:\users\you\work\uno")
    _instinct(v, "ins_b", "scrivere commit", "usare messaggi descrittivi in italiano", r"c:\users\you\work\due")
    _instinct(v, "ins_c", "analizzare progetti", "leggere il codice", r"c:\users\you\work\uno")
    group = instincts.promotion_candidates(v)["candidates"]
    assert len(group) == 1 and group[0]["projects"] == [r"c:\users\you\work\due", r"c:\users\you\work\uno"]
    assert instincts.promote(v, "ins_a")["scope"] == "global"
    assert instincts.promote(v, "ins_a")["note"] == "already global"
    out = tmp_path / "rules.json"
    assert instincts.export_instincts(v, str(out))["instincts"] == 3
    other = tmp_path / "other"
    vault_mod.init(other)
    imported = instincts.import_instincts(other, str(out))
    assert len(imported["created"]) == 3
    assert {i["status"] for i in instincts.list_instincts(other)["instincts"]} == {"proposed"}
    assert instincts.import_instincts(other, str(out))["created"] == []
    bad = tmp_path / "bad.json"
    bad.write_text("{}", "utf-8")
    with pytest.raises(ValidationError):
        instincts.import_instincts(other, str(bad))


def test_retrieval_eval_uses_saved_answers_and_extra_questions(v):
    _seed(v, {"lock": (_fm("Lock di vault"), "Il lock di vault serializza le scritture concorrenti."),
              "journal": (_fm("Journal"), "Il journal registra le scritture per il recupero dopo un crash.")})
    assert quality.retrieval_eval(v)["questions"] == 0
    answers.save(v, title="Come si evitano scritture concorrenti", question="come evitare scritture concorrenti nel vault",
                 text="Con il [[lock]].")
    (v / "_meta/eval").mkdir(parents=True)
    (v / "_meta/eval/queries.jsonl").write_text(json.dumps({"query": "ricetta della pasta", "expected": ["journal"]}) + "\n", "utf-8")
    report = quality.retrieval_eval(v, k=3, record=True)
    lexical = report["modes"]["lexical"]
    assert report["questions"] == 2 and lexical["recall_at_k"] == 0.5 and lexical["mrr"] == 0.5
    assert lexical["worst"][0]["missed"] == ["journal"] and report["recorded"]


def test_research_budget_dossier_checks(v, tmp_path):
    run = research.start(v, "Come funzionano i lock su Windows?", rounds=2, per_round=1)["id"]
    first = research.add_source(v, run, text="I lock msvcrt si liberano quando il processo muore.\nSono per byte.", title="Lock msvcrt")
    with pytest.raises(ConflictError):
        research.add_source(v, run, text="altro", title="Troppo")
    research.next_round(v, run)
    research.add_source(v, run, text="Alcuni sostengono che i lock su file siano inaffidabili in rete.", title="Critiche")
    with pytest.raises(ConflictError):
        research.next_round(v, run)
    key = first["path"].replace("raw/clips/", "clips/")
    lines = (v / first["path"]).read_text("utf-8").splitlines()
    n = next(i for i, l in enumerate(lines, 1) if "si liberano" in l)
    no_counter = f"# Sintesi\n\nI lock si liberano da soli ^[{key}#L{n}-L{n}]."
    with pytest.raises(ValidationError) as info:
        research.finish(v, run, no_counter)
    assert "no_counter_positions" in info.value.details
    foreign = no_counter + "\n\n## Posizioni contrarie\n\nNessuna ^[clips/altro.md#L1-L1]."
    with pytest.raises(ValidationError):
        research.finish(v, run, foreign)
    good = no_counter + "\n\n## Posizioni contrarie\n\nSu disco di rete sono considerati poco affidabili."
    out = research.finish(v, run, good)
    assert out["dossier"] == f"_meta/research/{run}.md" and out["sources"] == 2
    assert research.status(v, run)["status"] == "done" and research.status(v)["research"][0]["sources"] == 2
