import pytest

from mnemoloci import editor, index, sources, vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import ConflictError, MnemoError, ValidationError
from mnemoloci.markdown import render_page, split_frontmatter

PAGE = "wiki/concepts/motore.md"
FM = {"title": "Motore", "kind": "concept", "summary": "Scrive nel vault.", "aliases": ["Engine"], "language": "it",
      "sources": [{"key": "proj/README.md", "sha256": "a" * 64}], "confidence": 0.9, "provenanceState": "extracted",
      "contradictedBy": [], "updated": "2026-09-20"}
# lines 1-2 of the registered "proj/README.md" source -- matched verbatim by the citation
# checks against the body used by test_save_keeps_provenance_and_updates_index_log_search
SOURCE_L1 = "Testo riscritto a mano: il journal garantisce il rollback."
SOURCE_L2 = "Seconda riga di prova."


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text(f"{SOURCE_L1}\n{SOURCE_L2}\n", "utf-8")
    sources.add_source(root, str(proj), "proj")
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed", "operation": "test", "writes": [
        {"path": PAGE, "action": "create", "content": render_page(FM, "Testo originale ^[proj/README.md#L1-L2].")}]}
    txn.apply(root, b, txn.prepare(root, b).approval_sha256)
    index.rebuild(root)
    return root


@pytest.mark.parametrize("path", ["wiki/index.md", "wiki/log.md", "raw/inbox/x.md", "_meta/profile.md",
                                  ".mnemoloci/config.json", "../fuori.md", "MNEMOLOCI.md"])
def test_only_pages_and_notes_are_editable(v, path):
    with pytest.raises(MnemoError):
        editor.load(v, path)


def test_load_splits_editable_fields_from_the_rest(v):
    page = editor.load(v, PAGE)
    assert page["fields"] == {"title": "Motore", "summary": "Scrive nel vault.", "aliases": ["Engine"], "tags": []}
    assert page["body"].startswith("Testo originale") and page["readonly"]["confidence"] == 0.9


def test_save_keeps_provenance_and_updates_index_log_search(v):
    base = editor.load(v, PAGE)["sha256"]
    out = editor.save(v, PAGE, base_sha256=base, title="Motore transazionale", summary="Scrive con journal.",
                      body="Testo riscritto a mano: il journal garantisce il rollback ^[proj/README.md#L1-L2].",
                      aliases=["Engine", "Transaction engine"], tags=["core"])
    assert out["warnings"] == []
    fm, body = split_frontmatter((v / PAGE).read_text("utf-8"))
    assert fm["title"] == "Motore transazionale" and fm["tags"] == ["core"] and fm["lastEditedBy"] == "human"
    assert fm["sources"] == FM["sources"] and fm["confidence"] == 0.9 and fm["kind"] == "concept"
    assert "riscritto a mano" in body
    assert "[[motore|Motore transazionale]]" in (v / "wiki/index.md").read_text("utf-8")
    assert "modifica manuale [[motore]]" in (v / "wiki/log.md").read_text("utf-8")
    assert index.query(v, "rollback", mode="lexical")["results"][0]["slug"] == "motore"
    again = editor.load(v, PAGE)
    assert again["sha256"] == out["sha256"]


def test_external_change_is_never_overwritten(v):
    base = editor.load(v, PAGE)["sha256"]
    (v / PAGE).write_text(render_page(FM, "Modificato in Obsidian."), "utf-8")
    with pytest.raises(ConflictError) as info:
        editor.save(v, PAGE, base_sha256=base, title="X", summary="", body="Il mio testo.")
    assert info.value.code == "PAGE_CHANGED"
    assert "Modificato in Obsidian." in (v / PAGE).read_text("utf-8")


def test_secrets_and_empty_pages_are_refused(v):
    base = editor.load(v, PAGE)["sha256"]
    with pytest.raises(ValidationError) as info:
        editor.save(v, PAGE, base_sha256=base, title="X", summary="", body='token = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"')
    assert info.value.code == "SECRET_IN_TEXT"
    with pytest.raises(ValidationError):
        editor.save(v, PAGE, base_sha256=base, title="X", summary="", body="   ")
    assert "Testo originale" in (v / PAGE).read_text("utf-8")


def test_undeclared_citation_is_a_warning_not_a_block(v):
    base = editor.load(v, PAGE)["sha256"]
    out = editor.save(v, PAGE, base_sha256=base, title="Motore", summary="s", body="Vedi ^[altro/file.md#L1-L1].")
    assert out["warnings"] == ["citation to undeclared source altro/file.md"]


def test_out_of_range_citation_is_a_warning_not_a_block(v):
    base = editor.load(v, PAGE)["sha256"]
    out = editor.save(v, PAGE, base_sha256=base, title="Motore", summary="s",
                      body="Testo a caso ^[proj/README.md#L9-L9].")  # the source has only 2 lines
    assert out["warnings"] == ["citation proj/README.md#L9-L9 is outside the source's line range (1..2)"]
    assert "Testo a caso" in (v / PAGE).read_text("utf-8")  # saved anyway


def test_citation_text_mismatch_is_a_warning_not_a_block(v):
    base = editor.load(v, PAGE)["sha256"]
    out = editor.save(v, PAGE, base_sha256=base, title="Motore", summary="s",
                      body="Il motore vola sulla luna ^[proj/README.md#L1-L1].")  # not in the source
    assert out["warnings"] == ["citation proj/README.md#L1-L1 does not literally match its cited source text"]
    assert "vola sulla luna" in (v / PAGE).read_text("utf-8")  # saved anyway


def test_matching_citation_has_no_warnings(v):
    base = editor.load(v, PAGE)["sha256"]
    # SOURCE_L1 already ends in ".": no extra trailing punctuation after the marker here
    out = editor.save(v, PAGE, base_sha256=base, title="Motore", summary="s", body=f"{SOURCE_L1} ^[proj/README.md#L1-L1]")
    assert out["warnings"] == []


def test_api_edit_roundtrip(v):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api

    c = TestClient(api.create_app(v, "t", {"port": 9}), base_url="http://127.0.0.1:9")
    c.headers["Authorization"] = "Bearer t"
    page = c.get("/api/edit", params={"path": PAGE}).json()
    ok = c.post("/api/edit/save", json={"path": PAGE, "base_sha256": page["sha256"], "title": "Nuovo",
                                       "summary": "s", "body": "Corpo.", "aliases": [], "tags": []}).json()
    assert ok["ok"]
    stale = c.post("/api/edit/save", json={"path": PAGE, "base_sha256": page["sha256"], "title": "Altro",
                                          "summary": "s", "body": "Corpo 2.", "aliases": [], "tags": []}).json()
    assert stale["code"] == "PAGE_CHANGED"
    assert c.get("/api/edit", params={"path": "wiki/index.md"}).json()["code"] == "NOT_EDITABLE"
