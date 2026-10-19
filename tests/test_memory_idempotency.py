import pytest

from mnemoloci import hooks, memory, vault as vault_mod
from mnemoloci.core.lock import VaultLock

P = r"C:\Users\you\Work\Demo"
TITLE = "Handoff automatico: passa alla fase 1"
BODY = "Ultime richieste: compilare la fonte X; poi eseguire la manutenzione."


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def _handoffs(v):
    return memory.list_memory(v, kind="handoff", status=None)


def test_the_same_automatic_handoff_saved_twice_writes_one_note(v):
    first = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    again = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    assert again["duplicate"] is True and again["id"] == first["id"] and again["path"] == first["path"]
    assert "duplicate" not in first
    assert len(_handoffs(v)) == 1


def test_spacing_and_case_do_not_make_a_new_note(v):
    first = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    again = memory.add(v, "handoff", TITLE.upper(), "  " + BODY.replace(" ", "   ") + "\n", project=P, auto=True)
    assert again["duplicate"] is True and again["id"] == first["id"]


def test_different_text_project_or_kind_is_a_new_note(v):
    first = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    other_text = memory.add(v, "handoff", TITLE, BODY + " Aggiunta.", project=P, auto=True)
    other_project = memory.add(v, "handoff", TITLE, BODY, project=r"C:\Users\you\Work\Other", auto=True)
    assert "duplicate" not in other_text and "duplicate" not in other_project
    assert len({first["id"], other_text["id"], other_project["id"]}) == 3
    assert len(_handoffs(v)) == 3


def test_outside_the_retry_window_it_is_a_new_note(v, monkeypatch):
    first = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    monkeypatch.setattr(memory, "RETRY_WINDOW", -1)
    again = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    assert "duplicate" not in again and again["id"] != first["id"]


def test_a_retracted_note_does_not_block_saving_the_same_text_again(v):
    first = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    memory.retract(v, first["id"], reason="prova")
    again = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    assert "duplicate" not in again and again["id"] != first["id"]


def test_the_key_is_stored_in_the_note(v):
    note = memory.add(v, "handoff", TITLE, BODY, project=P, auto=True)
    shown = memory.read(v, note["id"])["frontmatter"]
    assert shown["contentKey"] == memory.content_key("handoff", P, TITLE, BODY)


def test_a_manual_lesson_saved_again_is_still_confirmed_not_ignored(v):
    first = memory.add(v, "lesson", "Risposte brevi", "Niente riassunti finali.", project=P)
    again = memory.add(v, "lesson", "Risposte brevi", "Niente riassunti finali.", project=P)
    assert again["reinforced"] is True and again["id"] == first["id"] and again["confirmations"] == 2
    assert "duplicate" not in again


def _item(title, body, supersede=True):
    return {"title": title, "body": body, "project": P, "source": "hook", "tag": "auto-handoff", "supersede": supersede}


def test_replaying_an_old_handoff_does_not_reshuffle_the_chain(v):
    old_item, new_item = _item("Handoff A", "Primo testo."), _item("Handoff B", "Secondo testo.")
    def states():
        return {n["id"]: memory.read(v, n["id"])["frontmatter"]["status"] for n in _handoffs(v)}

    with VaultLock(v) as lock:
        hooks._save_handoff(v, lock, old_item)
        hooks._save_handoff(v, lock, new_item)
        before = states()
        hooks._save_handoff(v, lock, old_item)  # the queue replays the first one
        after = states()
    assert before == after and len(after) == 2
    assert sorted(after.values()) == ["active", "superseded"]
