import json
from pathlib import Path

import pytest

from mnemoloci import compile as compiler
from mnemoloci import index, lint, review, sources, vault as vault_mod
from mnemoloci.core.errors import MnemoError
from mnemoloci.markdown import render_page

README = """# Progetto Demo

Il motore transazionale scrive nel vault in modo sicuro.
Ogni scrittura ha un hash di approvazione.
Il lock di vault serializza i processi.
"""
ARCH = """# Architettura

Transaction engine: journal, backup e rollback.
Recover ripristina il vault dopo un crash.
"""


@pytest.fixture
def world(tmp_path):
    project = tmp_path / "demo"
    (project / "docs").mkdir(parents=True)
    (project / "node_modules").mkdir()
    (project / "README.md").write_text(README, "utf-8")
    (project / "docs" / "arch.md").write_text(ARCH, "utf-8")
    (project / "config.py").write_text('API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz123456"\nDEBUG = True\n', "utf-8")
    (project / ".env").write_text("SECRET=1\n", "utf-8")
    (project / "node_modules" / "x.js").write_text("x", "utf-8")
    (project / "logo.png").write_bytes(b"\x89PNG\0\0")
    v = tmp_path / "vault"
    vault_mod.init(v, "it")
    sources.add_source(v, str(project), "demo")
    return v, project


def fake_extract(packet_path: str, concepts: list[dict], *, extracted_by: str | None = None) -> None:
    packet = json.loads(Path(packet_path).read_text("utf-8"))
    data = {"schema": "mnemoloci.extract.v1", "source_key": packet["source_key"],
            "sha256": packet["sha256"], "concepts": concepts}
    if extracted_by:
        data["extracted_by"] = extracted_by
    Path(packet["extract_path"]).write_text(json.dumps(data), "utf-8")


def concept(title, lines, *, aliases=(), confidence=0.9, kind="concept", related=(), text=None):
    """`text` defaults to a synthetic placeholder; pass the real source substring for a claim
    whose page is expected to reach `submit()` without a hold (the verbatim citation check
    requires the claim text to actually appear in the cited source lines)."""
    return {"title": title, "kind": kind, "aliases": list(aliases), "summary": f"Sintesi di {title}",
            "claims": [{"text": text or f"affermazione su {title}", "lines": lines}], "confidence": confidence,
            "provenance": "extracted", "related": list(related), "contradicts": []}


def fake_write_pages(merge_result):
    for job_ref in merge_result["jobs"]:
        job = json.loads(Path(job_ref["job"]).read_text("utf-8"))
        fm = dict(job["suggested_frontmatter"], summary=f"Pagina su {job['title']}")
        body = f"# {job['title']}\n\n" + "\n".join(
            f"- {claim['text']} {claim['cite']}" for c in job["contributions"] for claim in c["claims"]
        )
        body += "".join(f"\n\nVedi anche [[{s}]]." for s in job["related_slugs"])
        Path(job["page_file"]).write_text(render_page(fm, body), "utf-8")


def packet_for(plan, key):
    return next(f["packet"] for f in plan["files"] if f["key"] == key)


def test_full_compile_cycle(world):
    v, project = world
    report = sources.scan(v)["demo"]
    keys = [c["key"] for c in report["changed"]]
    assert keys == ["demo/README.md", "demo/docs/arch.md", "demo/config.py"]
    assert report["skipped"]["secret-file"] == 1
    assert "excluded-dir" in report["skipped"] or "unsupported-type" in report["skipped"]

    plan = compiler.plan(v)
    config_packet = json.loads(Path(packet_for(plan, "demo/config.py")).read_text("utf-8"))
    assert "sk-ant-" not in json.dumps(config_packet)
    assert config_packet["redactions"] == 1

    with pytest.raises(MnemoError) as info:
        compiler.merge(v)
    assert info.value.code == "EXTRACT_INCOMPLETE"

    fake_extract(packet_for(plan, "demo/README.md"), [
        concept("Motore transazionale", [3, 4], aliases=["Transaction engine"], related=["Lock di vault"],
                text="Il motore transazionale scrive nel vault in modo sicuro."),
        concept("Lock di vault", [5, 5], confidence=0.3),
    ])
    fake_extract(packet_for(plan, "demo/docs/arch.md"),
                [concept("Transaction Engine", [3, 4], text="Transaction engine: journal, backup e rollback.")])
    fake_extract(packet_for(plan, "demo/config.py"), [])

    merged = compiler.merge(v)
    jobs = {j["slug"]: j for j in merged["jobs"]}
    assert set(jobs) == {"motore-transazionale", "lock-di-vault"}
    engine_job = json.loads(Path(jobs["motore-transazionale"]["job"]).read_text("utf-8"))
    assert {c["source_key"] for c in engine_job["contributions"]} == {"demo/README.md", "demo/docs/arch.md"}
    assert engine_job["suggested_frontmatter"]["provenanceState"] == "merged"

    fake_write_pages(merged)
    dry = compiler.submit(v)
    assert [p["path"] for p in dry["live"]] == ["wiki/concepts/motore-transazionale.md"]
    assert dry["held"][0]["slug"] == "lock-di-vault"
    assert dry["held"][0]["reasons"][0]["code"] == "low-confidence"
    # advisory only: the citations are counted next to the dry run and never change what is held or approved
    assert dry["weak_citations"]["citations"] >= 1 and isinstance(dry["weak_citations"]["weak"], int)

    with pytest.raises(MnemoError) as info:
        compiler.submit(v, approved="0" * 64)
    assert info.value.code == "PLAN_CHANGED"

    done = compiler.submit(v, approved=dry["approval_sha256"])
    page = (v / "wiki/concepts/motore-transazionale.md").read_text("utf-8")
    assert "^[demo/README.md#L3-L4]" in page
    assert "[[motore-transazionale|Motore transazionale]]" in (v / "wiki/index.md").read_text("utf-8")
    assert "compile" in (v / "wiki/log.md").read_text("utf-8")
    assert compiler.plan(v)["run_id"] is None

    hits = index.query(v, "transazionale")["results"]
    assert hits and hits[0]["slug"] == "motore-transazionale"

    [cand] = review.list_candidates(v)
    assert cand["id"] == done["candidates"][0]
    shown = review.show(v, cand["id"])
    review.approve(v, cand["id"], shown["content_sha256"])
    assert (v / "wiki/concepts/lock-di-vault.md").is_file()
    assert review.list_candidates(v) == []
    related = index.query(v, "hash approvazione")["related"]
    assert any(r["slug"] == "lock-di-vault" for r in related) or index.query(v, "lock")["results"]

    report = lint.lint(v)
    assert report["ok"], report["issues"]
    assert report["counts"]["broken_links"] == 0

    (project / "README.md").write_text(README + "Nuova riga.\n", "utf-8")
    assert [c["key"] for c in sources.scan(v)["demo"]["changed"]] == ["demo/README.md"]
    stale = lint.lint(v)
    assert not stale["ok"] and stale["counts"]["stale"] == 2


def test_a_local_extraction_contribution_always_forces_review(world):
    v, _ = world
    plan = compiler.plan(v)
    fake_extract(packet_for(plan, "demo/README.md"),
                [concept("Motore transazionale", [3, 4], aliases=["Transaction engine"], confidence=0.95)])
    # a local model's contribution to the SAME page, merged with an otherwise-clean Claude one
    fake_extract(packet_for(plan, "demo/docs/arch.md"), [concept("Transaction Engine", [3, 4], confidence=0.95)],
                extracted_by="ollama:llama3.1")
    fake_extract(packet_for(plan, "demo/config.py"), [])
    merged = compiler.merge(v)
    job = json.loads(Path(next(j["job"] for j in merged["jobs"] if j["slug"] == "motore-transazionale")).read_text("utf-8"))
    assert {c["source_key"]: c["extracted_by"] for c in job["contributions"]} == {
        "demo/README.md": None, "demo/docs/arch.md": "ollama:llama3.1"}
    fake_write_pages(merged)
    dry = compiler.submit(v)
    assert dry["live"] == []  # high confidence, no other problems -- still never published on its own
    [held] = dry["held"]
    assert held["slug"] == "motore-transazionale"
    assert {"code": "local-extraction", "detail": "estratto da: ollama:llama3.1"} in held["reasons"]

    compiler.submit(v, approved=dry["approval_sha256"])
    [cand] = review.list_candidates(v)
    assert cand["id"].startswith("motore-transazionale")  # reached review, not the live wiki
    assert not (v / "wiki/concepts/motore-transazionale.md").is_file()


def test_local_extraction_hold_is_not_configurable_away(world):
    """Every other hold reason is gated on config["review"]["hold"] -- and a vault whose config.json
    predates this hold-mode (saved once at init, never migrated) would simply never have it listed,
    the same staleness bug just fixed for .gitignore. This one is intentionally unconditional."""
    v, _ = world
    cfg_path = v / ".mnemoloci" / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    cfg["review"]["hold"] = [h for h in cfg["review"]["hold"] if h != "local-extraction"]
    assert "local-extraction" not in cfg["review"]["hold"]
    cfg_path.write_text(json.dumps(cfg), "utf-8")

    plan = compiler.plan(v)
    fake_extract(packet_for(plan, "demo/README.md"),
                [concept("Motore transazionale", [3, 4], confidence=0.99,
                         text="Il motore transazionale scrive nel vault in modo sicuro.")],
                extracted_by="ollama:llama3.1")
    fake_extract(packet_for(plan, "demo/docs/arch.md"), [])
    fake_extract(packet_for(plan, "demo/config.py"), [])
    merged = compiler.merge(v)
    fake_write_pages(merged)
    dry = compiler.submit(v)
    assert dry["live"] == [] and dry["held"][0]["reasons"][0]["code"] == "local-extraction"


def test_invalid_extract_is_rejected_with_details(world):
    v, _ = world
    plan = compiler.plan(v)
    for f in plan["files"]:
        fake_extract(f["packet"], [concept("Fuori", [1, 999])])
    with pytest.raises(MnemoError) as info:
        compiler.merge(v)
    assert "outside" in json.dumps(info.value.details)


def test_page_edited_in_obsidian_blocks_submit(world):
    v, _ = world
    plan = compiler.plan(v)
    for f in plan["files"]:
        fake_extract(f["packet"], [concept("Motore transazionale", [1, 2])] if f["key"] == "demo/README.md" else [])
    merged = compiler.merge(v)
    fake_write_pages(merged)
    (v / "wiki/concepts").mkdir(parents=True, exist_ok=True)
    (v / "wiki/concepts/motore-transazionale.md").write_text("scritto a mano", "utf-8")
    with pytest.raises(MnemoError) as info:
        compiler.submit(v)
    assert info.value.code == "PAGES_INVALID"


def test_one_run_at_a_time_and_abandon(world):
    v, _ = world
    compiler.plan(v, limit=1)
    with pytest.raises(MnemoError) as info:
        compiler.plan(v)
    assert info.value.code == "RUN_IN_PROGRESS"
    compiler.abandon(v)
    assert compiler.plan(v)["run_id"]


def test_bilingual_pages_translate_and_keep_original(world):
    v, _ = world
    plan = compiler.plan(v)
    for f in plan["files"]:
        concepts = []
        if f["key"] == "demo/docs/arch.md":
            concepts = [dict(concept("Transaction Engine", [3, 4]), translated_title="Motore transazionale")]
        packet = json.loads(Path(f["packet"]).read_text("utf-8"))
        Path(packet["extract_path"]).write_text(json.dumps({
            "schema": "mnemoloci.extract.v1", "source_key": packet["source_key"], "sha256": packet["sha256"],
            "source_language": "en", "concepts": concepts}), "utf-8")
    merged = compiler.merge(v)
    [job_ref] = merged["jobs"]
    job = json.loads(Path(job_ref["job"]).read_text("utf-8"))
    assert job["slug"] == "motore-transazionale" and job["title"] == "Motore transazionale"
    assert "Transaction Engine" in job["suggested_frontmatter"]["aliases"]
    assert job["original_languages"] == ["en"] and job["suggested_frontmatter"]["language"] == "it"

    fm = dict(job["suggested_frontmatter"], summary="Scrive nel vault con journal e rollback.")
    cite = job["contributions"][0]["claims"][0]["cite"]
    italian = f"# Motore transazionale\n\nJournal, backup e rollback {cite}."
    Path(job["page_file"]).write_text(render_page(fm, italian), "utf-8")
    held = compiler.submit(v)["held"]
    assert held and "Originale (en)" in held[0]["reasons"][0]["detail"]

    Path(job["page_file"]).write_text(render_page(fm, italian + f"\n\n> [!quote]- Originale (en)\n> Transaction engine: journal, backup e rollback {cite}."), "utf-8")
    dry = compiler.submit(v)
    assert dry["held"] == [] and len(dry["live"]) == 1


def test_missing_translation_is_rejected(world):
    v, _ = world
    plan = compiler.plan(v)
    for f in plan["files"]:
        packet = json.loads(Path(f["packet"]).read_text("utf-8"))
        Path(packet["extract_path"]).write_text(json.dumps({
            "schema": "mnemoloci.extract.v1", "source_key": packet["source_key"], "sha256": packet["sha256"],
            "source_language": "en", "concepts": [concept("Transaction Engine", [1, 1])]}), "utf-8")
    with pytest.raises(MnemoError) as info:
        compiler.merge(v)
    assert "translated_title" in json.dumps(info.value.details)


def test_language_setting(world):
    v, _ = world
    assert vault_mod.load_config(v)["language"] == {"target": "it", "keep_original": True}
    vault_mod.set_language(v, "en", False)
    assert vault_mod.load_config(v)["language"] == {"target": "en", "keep_original": False}
    with pytest.raises(MnemoError):
        vault_mod.set_language(v, "italiano", True)


def test_corrupt_config_fails_closed(world):
    v, _ = world
    cfg = v / ".mnemoloci/config.json"
    data = json.loads(cfg.read_text())
    data["review"]["hold"].append("typo-mode")
    cfg.write_text(json.dumps(data))
    with pytest.raises(MnemoError) as info:
        compiler.submit(v)
    assert info.value.code == "CONFIG_INVALID"


def test_ast_boundaries_are_none_without_the_extra_and_plan_falls_back(world):
    v, _ = world
    assert compiler._ast_boundaries(["x = 1", "y = 2"], ".py") is None  # tree-sitter-language-pack not installed here
    assert compiler._ast_boundaries(["x = 1"], ".md") is None  # no grammar mapped for this suffix at all
    plan = compiler.plan(v)
    config_packet = json.loads(Path(packet_for(plan, "demo/config.py")).read_text("utf-8"))
    # unchanged behaviour without the extra: one plain chunk covering the whole (short) file, same shape as before
    [chunk] = config_packet["chunks"]
    assert chunk["start_line"] == 1 and chunk["end_line"] == 2 and "DEBUG = True" in chunk["text"]
    assert chunk["text"].splitlines()[1].startswith("    2| ")


def test_chunks_ast_only_cuts_on_a_boundary(monkeypatch):
    monkeypatch.setattr(compiler, "CHUNK_LINES", 5)
    monkeypatch.setattr(compiler, "CHUNK_CHARS", 10**9)  # isolate the line-count threshold
    lines = [f"line {n}" for n in range(1, 21)]
    chunks = compiler._chunks_ast(lines, {3, 7, 12})
    # boundary 3: buf has 3 lines, not "big enough" (< CHUNK_LINES=5) yet -> folds into the next
    # boundary 7: buf has 7 lines (1..7) -> big enough -> cuts there
    # boundary 12: new buf has 5 lines (8..12) -> big enough -> cuts there
    # nothing left but 13..20 (8 lines): never big enough again before EOF -> flushed as the tail, same as _chunks
    assert [c["start_line"] for c in chunks] == [1, 8, 13]
    assert [c["end_line"] for c in chunks] == [7, 12, 20]
    assert chunks[0]["text"].splitlines()[0] == "    1| line 1" and chunks[-1]["text"].splitlines()[-1] == "   20| line 20"


def test_chunks_ast_has_a_hard_ceiling_when_no_boundary_ever_comes(monkeypatch):
    monkeypatch.setattr(compiler, "CHUNK_LINES", 5)
    monkeypatch.setattr(compiler, "CHUNK_CHARS", 10**9)
    huge = [f"line {n}" for n in range(1, 41)]  # one "function" spanning the whole file: no boundary until the end
    forced = compiler._chunks_ast(huge, set())
    assert len(forced) > 1  # the ceiling (CHUNK_LINES*4 = 20) still forces cuts, never one giant chunk
    assert all(c["end_line"] - c["start_line"] + 1 <= 20 for c in forced[:-1])  # every cut but the trailing flush respects it


# ---------- cross-project similarity (semantic, not name-based -- the "connect the sources" feature) ----------

def _page(path, slug, title, sources):
    from mnemoloci.pages import Page
    return Page(path, slug, {"title": title, "sources": sources}, "corpo", "sha")


def test_cross_project_similar_returns_nothing_without_embeddings(world, monkeypatch):
    from mnemoloci import embed
    v, _ = world
    monkeypatch.setattr(embed, "available", lambda vault: False)
    assert compiler._cross_project_similar(v, {}, [], "Titolo") == []


def test_cross_project_similar_filters_by_threshold_and_shared_project(world, monkeypatch):
    from mnemoloci import embed
    v, _ = world
    other_project = _page("wiki/concepts/lock-ottimistico.md", "lock-ottimistico", "Lock ottimistico",
                          [{"key": "proj-a/doc.md", "sha256": "x"}])
    same_project = _page("wiki/concepts/altro.md", "altro", "Altro concetto",
                         [{"key": "proj-b/doc.md", "sha256": "x"}])
    by_path = {other_project.path: other_project, same_project.path: same_project}
    contributions = [{"source_key": "proj-b/readme.md", "summary": "Un motore che scrive con un lock."}]

    monkeypatch.setattr(embed, "available", lambda vault: True)
    monkeypatch.setattr(embed, "has_vectors", lambda vault: True)
    monkeypatch.setattr(embed, "search", lambda vault, query, limit=30: [
        (other_project.path, 0.9),   # different project, above threshold -> kept
        (same_project.path, 0.95),   # same project as the job -> filtered out despite the high score
        ("wiki/concepts/nomatch.md", 0.99),  # not in by_path (e.g. a query/answer page) -> skipped
        (other_project.path, 0.5),   # below threshold, would be filtered anyway
    ])

    hits = compiler._cross_project_similar(v, by_path, contributions, "Motore transazionale")
    assert [h["slug"] for h in hits] == ["lock-ottimistico"]
    assert hits[0]["projects"] == ["proj-a"] and hits[0]["score"] == 0.9


def test_merge_attaches_cross_project_similar_only_to_brand_new_pages(world, monkeypatch):
    from mnemoloci import embed

    v, project = world
    existing_fm = {"title": "Lock ottimistico", "kind": "concept", "summary": "Un lock ottimistico.",
                  "aliases": [], "sources": [{"key": "otherproj/doc.md", "sha256": "x" * 64}],
                  "confidence": 0.9, "provenanceState": "extracted", "contradictedBy": [], "language": "it"}
    (v / "wiki/concepts").mkdir(parents=True, exist_ok=True)
    (v / "wiki/concepts/lock-ottimistico.md").write_text(render_page(existing_fm, "Corpo."), "utf-8")

    monkeypatch.setattr(embed, "available", lambda vault: True)
    monkeypatch.setattr(embed, "has_vectors", lambda vault: True)
    monkeypatch.setattr(embed, "search", lambda vault, query, limit=30: [("wiki/concepts/lock-ottimistico.md", 0.9)])

    plan = compiler.plan(v)
    fake_extract(packet_for(plan, "demo/README.md"),
                [concept("Motore transazionale", [3, 4], aliases=["Transaction engine"])])
    fake_extract(packet_for(plan, "demo/docs/arch.md"), [])
    fake_extract(packet_for(plan, "demo/config.py"), [])
    merged = compiler.merge(v)

    job = json.loads(Path(next(j["job"] for j in merged["jobs"] if j["slug"] == "motore-transazionale")).read_text("utf-8"))
    assert job["existing"] is None  # genuinely new page
    assert [h["slug"] for h in job["cross_project_similar"]] == ["lock-ottimistico"]
    assert job["cross_project_similar"][0]["projects"] == ["otherproj"]


def test_merge_flags_claims_the_existing_page_already_cites(world):
    v, project = world
    plan = compiler.plan(v)
    key = "demo/README.md"
    existing_fm = {"title": "Motore transazionale", "kind": "concept", "summary": "Un motore.",
                  "aliases": [], "sources": [{"key": key, "sha256": "x" * 64}],
                  "confidence": 0.9, "provenanceState": "extracted", "contradictedBy": [], "language": "it"}
    (v / "wiki/concepts").mkdir(parents=True, exist_ok=True)
    (v / "wiki/concepts/motore-transazionale.md").write_text(
        render_page(existing_fm, f"- Gia' detto. ^[{key}#L3-L4]"), "utf-8")
    fake_extract(packet_for(plan, key), [
        {**concept("Motore transazionale", [3, 3]), "claims": [
            {"text": "Stesso fatto", "lines": [3, 3]}, {"text": "Fatto nuovo", "lines": [1, 1]}]}])
    fake_extract(packet_for(plan, "demo/docs/arch.md"), [])
    fake_extract(packet_for(plan, "demo/config.py"), [])
    merged = compiler.merge(v)
    job = json.loads(Path(next(j["job"] for j in merged["jobs"] if j["slug"] == "motore-transazionale")).read_text("utf-8"))
    claims = job["contributions"][0]["claims"]
    assert [c.get("already_cited", False) for c in claims] == [True, False]
