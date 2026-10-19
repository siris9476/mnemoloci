import json

import pytest

from mnemoloci import cli, memory, vault as vault_mod
from mnemoloci.core.errors import ValidationError
from mnemoloci.markdown import split_frontmatter

P = r"C:\Users\you\Work\Demo"


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def _fm(v, res):
    return split_frontmatter((v / res["path"]).read_text("utf-8"))[0]


def test_a_pasted_transcript_is_refused(v):
    transcript = "utente: fai questo\nassistant: fatto\nutente: ora quello\nassistant: ok\n"
    with pytest.raises(ValidationError) as exc:
        memory.add(v, "fact", "Chat incollata", transcript, project=P)
    assert exc.value.code == "TRANSCRIPT_LIKE"


def test_default_confidence_is_inferred_and_automatic_notes_are_unverified(v):
    manual = memory.add(v, "fact", "Un fatto", "Corpo breve.", project=P)
    auto = memory.add(v, "fact", "Un altro fatto", "Corpo breve.", project=P, auto=True, source="assistant-auto")
    assert manual["confidence"] == "inferred" and _fm(v, manual)["confidence"] == "inferred"
    assert _fm(v, auto)["confidence"] == "unverified"


def test_verified_without_a_verification_is_capped_and_warned(v):
    res = memory.add(v, "fact", "Porta del server", "Il server ascolta sulla porta 8080.", project=P, confidence="verified")
    fm = _fm(v, res)
    assert res["confidence"] == "inferred" and fm["claimedConfidence"] == "verified"
    assert res["warnings"]


def test_verified_with_a_verification_is_kept(v):
    res = memory.add(v, "fact", "Porta del server", "Il server ascolta sulla porta 8080.", project=P,
                     confidence="verified", verification="letto in config/server.yaml")
    fm = _fm(v, res)
    assert res["confidence"] == "verified" and fm["verification"] == "letto in config/server.yaml"
    assert "claimedConfidence" not in fm and "warnings" not in res


def test_undated_volatile_figures_cap_verified(v):
    res = memory.add(v, "fact", "Velocità", "Il modello genera 45 tok/s sulla macchina.", project=P,
                     confidence="verified", verification="misurato con il benchmark interno")
    assert res["confidence"] == "inferred" and res["warnings"]


def test_dated_figures_are_not_capped(v):
    res = memory.add(v, "fact", "Velocità", "Il 2026-09-01 il modello generava 45 tok/s sulla macchina.", project=P,
                     confidence="verified", verification="misurato con il benchmark interno")
    assert res["confidence"] == "verified"


def test_a_global_lesson_without_generality_warns_but_is_saved(v):
    res = memory.add(v, "lesson", "Regola generale", "Rileggere sempre il file prima di modificarlo.")
    assert res["warnings"] and memory.list_memory(v, kind="lesson")[0]["confidence"] == "inferred"
    ok = memory.add(v, "lesson", "Regola generale due", "Rileggere il file prima di modificarlo.", generality="ogni progetto")
    assert "warnings" not in ok


def test_a_long_body_warns(v):
    res = memory.add(v, "fact", "Lungo", "parola " * 1500, project=P)
    assert res["warnings"]


def test_invalid_confidence_is_rejected(v):
    with pytest.raises(ValidationError) as exc:
        memory.add(v, "fact", "X", "Y", project=P, confidence="certo")
    assert exc.value.code == "BAD_CONFIDENCE"


def test_cli_flags(v, capsys):
    assert cli.main(["--vault", str(v), "memory", "add", "fact", "--title", "Porta", "--text", "Porta 8080.",
                     "--project", P, "--confidence", "verified", "--verification", "letto nella configurazione"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["confidence"] == "verified"
