"""Phase 12: Codex import, Obsidian vault adoption, retired sources, pending-source awareness,
editorial instructions (SOUL.md), skill stocktake, briefings."""

import json
from datetime import date
from pathlib import Path

import pytest

from mnemoloci import (briefing, codex_import, compile as compiler, hooks, index, lint, memory, session_index,
                       skill_curator, sources)
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import MnemoError, ValidationError
from mnemoloci.core.hashing import sha256_file
from mnemoloci.markdown import render_page

TODAY = date.today().isoformat()


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _rollout(tmp_path, sid="019e-aaaa-bbbb"):
    folder = tmp_path / "codex" / "sessions" / "2026" / "09" / "20"
    folder.mkdir(parents=True, exist_ok=True)
    msg = lambda role, kind, text: {"type": "response_item", "timestamp": "2026-09-20T10:00:00Z",  # noqa: E731
                                    "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}}
    recs = [{"type": "session_meta", "timestamp": "2026-09-20T10:00:00Z", "payload": {"id": sid, "cwd": r"C:\Work\Api"}},
            msg("developer", "input_text", "<permissions instructions>sandbox</permissions instructions>"),
            msg("user", "input_text", "<environment_context>\n  <cwd>C:\\Work\\Api</cwd>\n</environment_context>"),
            msg("user", "input_text", "Progetta un motore di raccomandazione dei percorsi"),
            {"type": "response_item", "timestamp": "2026-09-20T10:00:05Z", "payload": {"type": "reasoning", "summary": []}},
            msg("assistant", "output_text", "Ecco il design: grafo pesato e Dijkstra. token = \"ghp_" + "z" * 36 + "\"")]
    path = folder / f"rollout-2026-09-20T10-00-00-{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recs), "utf-8")
    return path


def test_codex_sessions_are_imported_searchable_and_incremental(v, tmp_path):
    _rollout(tmp_path)
    dry = codex_import.import_codex(v, dry_run=True)
    assert dry["scanned"] == 1 and dry["imported"] == 1
    out = codex_import.import_codex(v)
    text = next((v / "raw/codex").rglob("part-001.md")).read_text("utf-8")
    assert "Progetta un motore" in text and "environment_context" not in text and "permissions" not in text
    assert "· Codex" in text and "ghp_" not in text and "source: codex" in text
    assert sources.load_sources(v)["sources"]["codex-sessions"]["kind"] == "sessions"
    assert codex_import.import_codex(v)["unchanged"] == 1
    hit = session_index.search(v, "raccomandazione", auto_refresh=False)["sessions"][0]
    assert hit["session_id"] == "codex:019e-aaaa-bbbb" and hit["title"].startswith("Progetta un motore")
    assert out["next"].endswith("--source codex-sessions")


def test_adopt_obsidian_vault_and_retire_a_source(v, tmp_path):
    obs = tmp_path / "MyVault"
    (obs / ".obsidian").mkdir(parents=True)
    (obs / "Idee.md").write_text("# Idee\n\nUn second brain.\n", "utf-8")
    (obs / ".obsidian" / "app.json").write_text("{}", "utf-8")
    with pytest.raises(MnemoError):
        sources.add_obsidian(v, str(tmp_path), "nope")
    out = sources.add_obsidian(v, str(obs), "obsidian")
    assert out["kind"] == "notes" and out["notes"] == 1
    assert [c["rel"] for c in sources.scan(v, "obsidian")["obsidian"]["changed"]] == ["Idee.md"]
    fm = {"title": "Idee", "kind": "concept", "summary": "s", "aliases": [], "confidence": 0.9,
          "sources": [{"key": "obsidian/Idee.md", "sha256": "0" * 64}], "provenanceState": "extracted",
          "contradictedBy": [], "updated": TODAY, "language": "it"}
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed-o", "operation": "test",
         "writes": [{"path": "wiki/concepts/idee.md", "action": "create", "content": render_page(fm, "x ^[obsidian/Idee.md#L1-L1]")}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    assert lint.lint(v)["counts"]["stale"] == 1
    retired = sources.retire_source(v, "obsidian", reason="non lo uso più")
    assert retired["pages_only_from_it"] == ["wiki/concepts/idee.md"]
    assert lint.lint(v)["counts"]["stale"] == 0 and "obsidian" not in sources.scan(v)  # a full scan skips it
    assert sources.scan(v, "obsidian")["obsidian"]["retired"]
    sources.retire_source(v, "obsidian", reason="", retired=False)
    assert lint.lint(v)["counts"]["stale"] == 1


def test_changed_sources_are_announced_at_session_start(v, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("# Progetto\n", "utf-8")
    sources.add_source(v, str(proj), "proj")
    sources.scan(v)  # a full scan leaves the summary behind
    context = hooks.build_context(v, str(proj))
    assert "1 file cambiati da compilare nelle fonti: proj (1)" in context


def test_editorial_instructions_reach_packets_and_jobs(v, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("# Progetto\n\nUsa il lock.\n", "utf-8")
    sources.add_source(v, str(proj), "proj")
    compiler.set_instructions(v, "Scrivi in modo asciutto. Metti sempre un esempio concreto.")
    run = compiler.plan(v)
    packet = json.loads(Path(run["files"][0]["packet"]).read_text("utf-8"))
    assert packet["editorial_instructions"].startswith("Scrivi in modo asciutto")
    Path(packet["extract_path"]).write_text(json.dumps({
        "schema": compiler.EXTRACT_SCHEMA, "source_key": packet["source_key"], "sha256": packet["sha256"],
        "source_language": "it", "concepts": [{"title": "Lock", "kind": "concept", "summary": "s", "confidence": 0.9,
                                               "provenance": "extracted", "claims": [{"text": "t", "lines": [3, 3]}]}]}), "utf-8")
    job = json.loads(Path(compiler.merge(v)["jobs"][0]["job"]).read_text("utf-8"))
    assert "esempio concreto" in job["editorial_instructions"]


def test_skill_stocktake_finds_quality_problems(v, tmp_path):
    skills = tmp_path / "claude" / "skills"
    (skills / "buona").mkdir(parents=True)
    (skills / "buona" / "SKILL.md").write_text(
        "---\nname: buona\ndescription: Prepara il rilascio del progetto con test e tag. Use when the user says "
        "\"rilascia\" or asks to publish a version.\n---\n\n# Rilascio\n\nVedi `references/checklist.md`.\n", "utf-8")
    (skills / "buona" / "references").mkdir()
    (skills / "buona" / "references" / "checklist.md").write_text("- test\n", "utf-8")
    (skills / "scarsa").mkdir()
    (skills / "scarsa" / "SKILL.md").write_text(
        "---\nname: altra\ndescription: Fa cose.\n---\n\nVedi `references/manca.md`.\n", "utf-8")
    (skills / "scarsa" / "script.py").write_text("print(1)\n", "utf-8")
    report = skill_curator.stocktake(v)
    by = {r["name"]: r for r in report["results"]}
    assert by["buona"]["issues"] == [] and by["buona"]["score"] == 100
    issues = " | ".join(by["altra"]["issues"])
    assert "does not match" in issues and "too short" in issues and "when to use it" in issues
    assert "references/manca.md" in issues and "never mentioned" in issues
    assert report["with_issues"] == 1


def test_skill_eval_readiness_reports_coverage_and_malformed_tests_yaml(v, tmp_path):
    skills = tmp_path / "claude" / "skills"
    (skills / "coperta").mkdir(parents=True)
    (skills / "coperta" / "SKILL.md").write_text(
        "---\nname: coperta\ndescription: Fa una cosa. Use when the user asks for it.\n---\n\n# Coperta\n", "utf-8")
    (skills / "coperta" / "tests.yaml").write_text(
        "- input: \"ricordati che mi piace il caffe\"\n"
        "  expect_cli: \"mnemoloci memory add preference ...\"\n"
        "  expect_assertion: \"una nota preference viene creata\"\n", "utf-8")
    (skills / "scoperta").mkdir()
    (skills / "scoperta" / "SKILL.md").write_text(
        "---\nname: scoperta\ndescription: Fa un'altra cosa. Use when the user asks for it.\n---\n\n# Scoperta\n", "utf-8")
    (skills / "rotta").mkdir()
    (skills / "rotta" / "SKILL.md").write_text(
        "---\nname: rotta\ndescription: Fa una terza cosa. Use when the user asks for it.\n---\n\n# Rotta\n", "utf-8")
    (skills / "rotta" / "tests.yaml").write_text("questo non e' una lista\n", "utf-8")

    report = skill_curator.eval_readiness(v)
    by = {r["name"]: r for r in report["results"]}
    assert by["coperta"]["covered"] and by["coperta"]["scenarios"] == 1
    assert not by["scoperta"]["covered"] and by["scoperta"]["scenarios"] == 0 and by["scoperta"]["error"] is None
    assert by["rotta"]["error"] and not by["rotta"]["covered"]
    assert report["covered"] == 1
    assert "scoperta" in report["no_coverage"]
    assert "rotta" in report["malformed"]


def test_skill_run_tests_executes_scenarios_on_a_scratch_vault(v, tmp_path):
    skills = tmp_path / "claude" / "skills"
    (skills / "esegue").mkdir(parents=True)
    (skills / "esegue" / "SKILL.md").write_text(
        "---\nname: esegue\ndescription: Fa una cosa. Use when the user asks for it.\n---\n\n# Esegue\n", "utf-8")
    (skills / "esegue" / "tests.yaml").write_text(
        "- input: \"ricordati che mi piace il caffe\"\n"
        "  expect_cli: \"mnemoloci memory add preference --title Caffe --text 'Mi piace il caffe'\"\n"
        "  expect_assertion: \"una nota preference viene creata\"\n"
        "- input: \"comando scritto male\"\n"
        "  expect_cli: \"mnemoloci memory add\"\n"  # missing required --title/--text: argparse rejects it
        "  expect_assertion: \"irrilevante\"\n"
        "- input: \"comando sintatticamente valido ma che fallisce a runtime\"\n"
        "  expect_cli: \"mnemoloci source show non-esiste\"\n"
        "  expect_assertion: \"irrilevante\"\n"
        f"- input: \"prova a scrivere sul vault vero, deve essere ignorato\"\n"
        f"  expect_cli: \"mnemoloci --vault {v.as_posix()} memory add preference --title X --text Y\"\n"
        "  expect_assertion: \"non deve toccare il vault vero\"\n", "utf-8")
    with pytest.raises(MnemoError) as info:
        skill_curator.run_tests(v, "non-installata")
    assert info.value.code == "NO_SUCH_SKILL"

    report = skill_curator.run_tests(v, "esegue")
    assert report["scenarios"] == 4
    by_input = {r["input"]: r for r in report["results"]}
    ok = by_input["ricordati che mi piace il caffe"]
    assert ok["status"] == "ran" and "preference" in ok["result"]["path"] and "caffe" in ok["result"]["path"]
    bad = by_input["comando scritto male"]
    assert bad["status"] == "invalid_command"
    fails = by_input["comando sintatticamente valido ma che fallisce a runtime"]
    assert fails["status"] == "command_failed" and "NO_SUCH_SOURCE" in fails["error"]
    stripped = by_input["prova a scrivere sul vault vero, deve essere ignorato"]
    assert stripped["status"] == "ran"
    assert memory.list_memory(v, kind="preference") == []  # the --vault it tried to specify was never honoured
    assert report["ran"] == 2 and report["invalid_command"] == 1 and report["command_failed"] == 1


def test_skill_export_evals_bridges_tests_yaml_to_skill_creator_format(v, tmp_path):
    skills = tmp_path / "claude" / "skills"
    (skills / "esegue").mkdir(parents=True)
    (skills / "esegue" / "SKILL.md").write_text(
        "---\nname: esegue\ndescription: Fa una cosa. Use when the user asks for it.\n---\n\n# Esegue\n", "utf-8")
    (skills / "esegue" / "tests.yaml").write_text(
        "- input: \"ricordati che mi piace il caffe\"\n"
        "  expect_cli: \"mnemoloci memory add preference --title Caffe --text 'Mi piace il caffe'\"\n"
        "  expect_assertion: \"una nota preference viene creata\"\n"
        "- input: \"comando scritto male\"\n"
        "  expect_cli: \"mnemoloci memory add\"\n"  # missing required --title/--text: argparse rejects it
        "  expect_assertion: \"irrilevante\"\n", "utf-8")

    with pytest.raises(MnemoError) as info:
        skill_curator.export_evals(v, "non-installata")
    assert info.value.code == "NO_SUCH_SKILL"

    out = skill_curator.export_evals(v, "esegue")
    assert out["exported"] == 1
    assert [s["input"] for s in out["skipped"]] == ["comando scritto male"]
    assert out["skipped"][0]["status"] == "invalid_command"
    out_path = skills / "esegue" / "evals" / "evals.json"
    assert Path(out["path"]) == out_path

    payload = json.loads(out_path.read_text("utf-8"))
    assert payload["skill_name"] == "esegue"
    [ev] = payload["evals"]
    assert ev["id"] == 1
    assert ev["prompt"] == "ricordati che mi piace il caffe"
    assert ev["expected_output"] == "una nota preference viene creata"
    assert ev["files"] == []
    assert ev["expectations"][0] == ("Runs a mnemoloci command equivalent to: mnemoloci memory add preference "
                                     "--title Caffe --text 'Mi piace il caffe'")
    assert ev["expectations"][1] == "una nota preference viene creata"


def test_briefing_audit_and_save(v, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.md").write_text("uno\ndue\n", "utf-8")
    sources.add_source(v, str(proj), "p")
    fm = {"title": "Lock", "kind": "concept", "summary": "s", "aliases": [], "confidence": 0.9,
          "sources": [{"key": "p/a.md", "sha256": sha256_file(proj / "a.md")}], "provenanceState": "extracted",
          "contradictedBy": [], "updated": TODAY, "language": "it"}
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed-b", "operation": "test",
         "writes": [{"path": "wiki/concepts/lock.md", "action": "create", "content": render_page(fm, "x")}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    bad = "Il [[lock]] serializza ^[p/a.md#L1-L9].\n\nVedi [[inesistente]]."
    audit = briefing.check(v, bad)
    assert not audit["ok"] and audit["broken_links"] == ["inesistente"] and audit["bad_citations"] == ["p/a.md#L1-L9"]
    with pytest.raises(ValidationError):
        briefing.save(v, bad, title="Lock", topic="lock")
    good = ("Il [[lock]] serializza le scritture ^[p/a.md#L1-L2].\n\n"
            "Questo paragrafo lungo non ha citazioni né collegamenti e quindi viene segnalato come non supportato dal vault.")
    out = briefing.save(v, good, title="Il lock in breve", topic="lock", mode="spiegazione")
    assert out["path"].startswith("_meta/briefings/") and out["coverage"] == 0.5 and len(out["unsupported_paragraphs"]) == 1
    assert briefing.save(v, good, title="Il lock in breve", topic="lock")["path"].endswith("-2.md")
