import pytest

from mnemoloci import backup, reflect
from mnemoloci import vault as vault_mod
from mnemoloci.markdown import render_page

PAGE = "wiki/concepts/motore-transazionale.md"


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root)
    backup.init(root)
    return root


def _write(v, *, confidence, contradicted_by, summary, body):
    fm = {"title": "Motore transazionale", "kind": "concept", "summary": summary, "aliases": [],
         "confidence": confidence, "provenanceState": "extracted", "contradictedBy": contradicted_by}
    (v / PAGE).parent.mkdir(parents=True, exist_ok=True)
    (v / PAGE).write_text(render_page(fm, body), "utf-8")


def test_trace_walks_a_pages_history_oldest_first(v):
    before = backup.history(v, limit=1)["commits"][0]["id"]  # the init commit, predates the page

    _write(v, confidence=0.6, contradicted_by=[], summary="Prima versione.", body="# Motore\n\nUsa un lock globale.")
    backup.commit(v, "prima stesura")

    _write(v, confidence=0.9, contradicted_by=[], summary="Seconda versione.", body="# Motore\n\nUsa un lock per pagina.")
    backup.commit(v, "corretto dopo verifica")

    _write(v, confidence=0.5, contradicted_by=["lock-ottimistico"], summary="Terza versione.",
          body="# Motore\n\nForse serve un lock per pagina, ma vedi la pagina che lo contraddice.")
    backup.commit(v, "trovata una fonte in contraddizione")

    out = reflect.trace(v, PAGE)
    assert out["page"] == PAGE
    versions = out["versions"]
    assert len(versions) == 3
    assert [ver["summary"] for ver in versions] == ["Prima versione.", "Seconda versione.", "Terza versione."]
    assert [ver["confidence"] for ver in versions] == [0.6, 0.9, 0.5]
    assert versions[0]["contradictedBy"] == [] and versions[2]["contradictedBy"] == ["lock-ottimistico"]
    assert "lock globale" in versions[0]["body"] and "lock per pagina" in versions[1]["body"]
    # oldest-first, and each version's own commit message is preserved
    assert versions[0]["message"] == "prima stesura" and versions[-1]["message"] == "trovata una fonte in contraddizione"

    # content_at on a commit before the page existed returns None, not an error
    assert backup.content_at(v, PAGE, before) is None


def test_trace_respects_limit_by_keeping_the_most_recent_commits(v):
    for i in range(5):
        _write(v, confidence=0.5, contradicted_by=[], summary=f"Versione {i}.", body=f"# Motore\n\nContenuto {i}.")
        backup.commit(v, f"modifica {i}")

    out = reflect.trace(v, PAGE, limit=2)
    assert [ver["summary"] for ver in out["versions"]] == ["Versione 3.", "Versione 4."]


def test_trace_on_a_page_with_no_history_returns_no_versions(v):
    out = reflect.trace(v, "wiki/concepts/mai-esistita.md")
    assert out == {"page": "wiki/concepts/mai-esistita.md", "versions": []}
