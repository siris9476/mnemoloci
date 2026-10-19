import pytest

from mnemoloci import memory, vault as vault_mod

P = r"C:\Users\you\Work\Demo"
TITLE = "Eseguire sempre la routine di manutenzione dopo ogni blocco di compilazione"
BODY = "Dopo ogni blocco di compilazione bisogna eseguire fix-links, links, hub-links e chiudere la coda di revisione."


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def test_the_same_lesson_in_other_words_is_confirmed_not_duplicated(v):
    first = memory.add(v, "lesson", TITLE, BODY, project=P)
    again = memory.add(v, "lesson", "Eseguire la routine di manutenzione dopo ogni blocco di compilazione",
                       "Dopo ogni blocco di compilazione eseguire fix-links, links, hub-links e chiudere la coda di revisione.",
                       project=P)
    assert again["reinforced"] is True and again["id"] == first["id"] and again["confirmations"] == 2
    assert again["matched"] == "similar" and again["score"] >= memory.DUPLICATE_SCORE
    assert len(memory.list_memory(v, kind="lesson")) == 1
    text = (v / first["path"]).read_text("utf-8")
    assert "Taken up again under the title «Eseguire la routine" in text and "Confirmed on" in text


def test_the_same_title_is_still_a_certain_match(v):
    first = memory.add(v, "preference", "Risposte brevi", "Niente riassunti finali.", project=P)
    again = memory.add(v, "preference", "risposte brevi", "Testo completamente diverso qui.", project=P)
    assert again["reinforced"] and again["id"] == first["id"] and again["matched"] == "title" and again["score"] == 1.0


def test_a_related_note_is_reported_but_never_merged(v):
    memory.add(v, "lesson", TITLE, BODY, project=P)
    other = memory.add(v, "lesson", "Controllare i link rotti dopo ogni blocco di compilazione",
                       "Dopo ogni blocco di compilazione eseguire fix-links e controllare i link rotti rimasti.", project=P)
    assert "reinforced" not in other
    [near] = other["similar"]
    assert memory.RELATED_SCORE <= near["score"] < memory.DUPLICATE_SCORE
    assert len(memory.list_memory(v, kind="lesson")) == 2


def test_unrelated_notes_report_nothing(v):
    memory.add(v, "lesson", TITLE, BODY, project=P)
    other = memory.add(v, "lesson", "Estensione Chrome per firmare i file", "Il manifest richiede permessi sugli host.", project=P)
    assert "similar" not in other and "reinforced" not in other


def test_only_the_same_kind_and_project_count(v):
    memory.add(v, "lesson", TITLE, BODY, project=P)
    other_project = memory.add(v, "lesson", TITLE + " ", BODY, project=r"C:\Users\you\Work\Altro")
    other_kind = memory.add(v, "decision", TITLE, BODY, project=P)
    assert "reinforced" not in other_project and "similar" not in other_project
    assert "similar" not in other_kind


def test_a_near_duplicate_decision_is_reported_not_merged(v):
    memory.add(v, "decision", "Usare SQLite per l'indice derivato", "L'indice derivato sta in SQLite con FTS5 e BM25.", project=P)
    second = memory.add(v, "decision", "Usare SQLite per l'indice derivato del vault", "L'indice derivato sta in SQLite con FTS5 e BM25 nel vault.", project=P)
    assert "reinforced" not in second and second["similar"][0]["score"] >= memory.RELATED_SCORE
    assert len(memory.list_memory(v, kind="decision")) == 2


def test_automatic_notes_skip_the_comparison(v):
    memory.add(v, "lesson", TITLE, BODY, project=P)
    auto = memory.add(v, "lesson", TITLE, BODY, project=P, auto=True, source="hook:x")
    assert "reinforced" not in auto and "similar" not in auto
