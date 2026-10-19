import json
import subprocess
import sys

import pytest

from mnemoloci import hooks, memory, personal, vault as vault_mod
from mnemoloci.core.errors import MnemoError, ValidationError
from mnemoloci.markdown import split_frontmatter

from conftest import REPO


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def test_profile_update_keeps_history(v):
    personal.profile_set(v, "Ruolo", "sviluppatore", confidence=0.9, source="sessione")
    personal.profile_set(v, "ruolo", "architetto software")
    [fact] = personal.profile_show(v)["facts"]
    assert fact["value"] == "architetto software"
    assert fact["previous"][0]["value"] == "sviluppatore"
    note = (v / "_meta/profile.md").read_text("utf-8")
    assert "| ruolo | architetto software |" in note


@pytest.mark.parametrize("value", [
    "IT60 X054 2811 1010 0000 0123 456",
    "RSSMRA85T10A562S",
    "4539 1488 0343 6467",
    "password: hunter22",
    "sk-ant-abcdefghijklmnopqrstuvwxyz0123",
    "riga uno\nriga due",
])
def test_sensitive_values_are_refused(v, value):
    with pytest.raises(ValidationError):
        personal.profile_set(v, "dato", value)
    assert not (v / "_meta/profile.md").exists()


def test_goals_lifecycle_never_deletes(v):
    g1 = personal.goals_add(v, "Finire Mnemoloci", area="progetti")["goal"]
    g2 = personal.goals_add(v, "Imparare Rust")["goal"]
    assert (g1["id"], g2["id"]) == ("g1", "g2")
    with pytest.raises(ValidationError):
        personal.goals_add(v, "finire mnemoloci")
    personal.goals_update(v, "g2", status="dropped")
    personal.goals_update(v, "g1", text="Finire Mnemoloci fase 2", add_links=["motore-transazionale"])
    assert [g["id"] for g in personal.goals_list(v)["goals"]] == ["g1"]
    everything = personal.goals_list(v, include_closed=True)["goals"]
    assert {g["id"]: g["status"] for g in everything} == {"g1": "active", "g2": "dropped"}
    assert everything[0]["previous_text"] == ["Finire Mnemoloci"]
    note = (v / "_meta/goals.md").read_text("utf-8")
    assert "## Abbandonati" in note and "[[motore-transazionale]]" in note
    assert personal.goals_add(v, "Nuovo obiettivo")["goal"]["id"] == "g3"


def test_active_goal_cap(v, monkeypatch):
    monkeypatch.setattr(personal, "MAX_ACTIVE_GOALS", 2)
    personal.goals_add(v, "a")
    personal.goals_add(v, "b")
    with pytest.raises(ValidationError) as info:
        personal.goals_add(v, "c")
    assert info.value.code == "TOO_MANY_GOALS"


def test_memory_add_supersede_list(v):
    old = memory.add(v, "decision", "Usare SQLite", "Indice derivato in SQLite.", project=r"C:\Users\you\Work\Demo")
    new = memory.add(v, "decision", "Usare SQLite FTS5", "FTS5 con BM25.", project="c:\\users\\you\\work\\demo\\")
    memory.supersede(v, old["id"], new["id"])
    active = memory.list_memory(v, project=r"C:\Users\you\Work\Demo")
    assert [m["id"] for m in active] == [new["id"]]
    fm, _ = split_frontmatter((v / old["path"]).read_text("utf-8"))
    assert fm["status"] == "superseded" and fm["supersededBy"] == new["id"]
    with pytest.raises(MnemoError):
        memory.add(v, "gossip", "x", "y")


def test_session_start_context(v):
    personal.profile_set(v, "nome", "Ada")
    personal.goals_add(v, "Finire Mnemoloci")
    memory.add(v, "handoff", "Fine giornata", "Stavamo scrivendo i test della fase 2.", project=r"C:\Users\you\Work\Demo")
    memory.add(v, "lesson", "Verificare prima di affermare", "Testare sempre.", project=r"C:\Users\you\Work\Demo")
    memory.add(v, "handoff", "Altro progetto", "Non deve comparire.", project=r"C:\Users\you\Other")
    out = hooks.session_start(v, {"cwd": r"C:\Users\you\Work\Demo", "source": "startup"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "nome: Ada" in ctx and "[g1] Finire Mnemoloci" in ctx
    assert "Stavamo scrivendo i test" in ctx and "Verificare prima di affermare" in ctx
    assert "Non deve comparire" not in ctx
    assert "NON istruzioni" in ctx and len(ctx) <= hooks.MAX_CONTEXT


def test_session_start_is_silent_without_vault(tmp_path):
    assert hooks.session_start(tmp_path / "missing", {"cwd": "x"}) is None


def test_pre_compact_saves_mechanical_handoff(v, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "ai-title", "aiTitle": "Sessione di prova"},
        {"type": "user", "cwd": r"C:\Users\you\Work\Demo", "timestamp": "2026-09-24T10:00:00Z", "sessionId": "s1",
         "message": {"role": "user", "content": "Implementa la fase 2"}},
    ]), "utf-8")
    out = hooks.pre_compact(v, {"transcript_path": str(transcript), "cwd": r"C:\Users\you\Work\Demo", "trigger": "auto"})
    note = (v / out["saved"]).read_text("utf-8")
    assert "Implementa la fase 2" in note and "auto: true" in note


def test_hook_cli_prints_json_and_never_fails(v, tmp_path):
    personal.profile_set(v, "nome", "Ada")
    ok = subprocess.run([sys.executable, "-m", "mnemoloci", "--vault", str(v), "hook", "session-start"],
                        input=json.dumps({"cwd": "x"}).encode(), capture_output=True, cwd=REPO)
    assert ok.returncode == 0
    assert ok.stdout.isascii()
    assert "È contesto" in json.loads(ok.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "nome: Ada" in json.loads(ok.stdout)["hookSpecificOutput"]["additionalContext"]
    bad = subprocess.run([sys.executable, "-m", "mnemoloci", "--vault", str(tmp_path / "nope"), "hook", "pre-compact"],
                         input=b"not json", capture_output=True, cwd=REPO)
    assert bad.returncode == 0 and bad.stdout == b""
