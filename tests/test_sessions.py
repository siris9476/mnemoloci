import json
from pathlib import Path

import pytest

from mnemoloci import compile as compiler
from mnemoloci import sessions, sources, vault as vault_mod

CWD = r"C:\Work\Demo"


def rec(kind, **fields):
    base = {"type": kind, "sessionId": "sess-1111-aaaa", "cwd": CWD, "gitBranch": "main",
            "timestamp": fields.pop("ts", "2026-09-20T10:00:00Z"), "isSidechain": False}
    base.update(fields)
    return base


def user(content, **extra):
    return rec("user", message={"role": "user", "content": content}, **extra)


def assistant(mid, blocks, **extra):
    return rec("assistant", message={"id": mid, "role": "assistant", "content": blocks}, **extra)


TRANSCRIPT = [
    {"type": "ai-title", "aiTitle": "Refactoring del motore", "sessionId": "sess-1111-aaaa"},
    user("Primo prompt come stringa", origin={"kind": "human"}),
    user([{"type": "text", "text": "<ide_selection>codice selezionato</ide_selection>Secondo prompt in forma lista"}],
         origin={"kind": "human"}, promptSource="sdk"),
    assistant("m1", [{"type": "thinking", "thinking": "privato"}]),
    assistant("m1", [{"type": "text", "text": "Risposta parte A."}]),
    assistant("m1", [{"type": "tool_use", "name": "Bash", "input": {}}]),
    assistant("m1", [{"type": "text", "text": "Risposta parte B, chiave sk-ant-abcdefghijklmnopqrstuvwxyz0123"}]),
    user([{"type": "tool_result", "tool_use_id": "t", "content": "output enorme"}]),
    user("<task-notification><task-id>x</task-id></task-notification>"),
    user("<command-name>/model</command-name><command-args></command-args>"),
    user("Contesto iniettato", isMeta=True),
    user("Riassunto di compattazione", isCompactSummary=True),
    user("Messaggio di un subagente", isSidechain=True),
    {"type": "attachment", "attachment": {"type": "total_tokens_reminder"}},
]


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", "utf-8")


@pytest.fixture
def claude(tmp_path):
    base = tmp_path / "claude"
    proj = base / "projects" / "D--Work-Demo"
    write_jsonl(proj / "sess-1111-aaaa.jsonl", TRANSCRIPT)
    write_jsonl(proj / "sess-1111-aaaa" / "subagents" / "agent-x.jsonl", [user("dentro un subagente")])
    (proj / "memory").mkdir()
    (proj / "memory" / "user_role.md").write_text("---\nname: ruolo\n---\nL'utente è uno sviluppatore.\n", "utf-8")
    other = base / "projects" / "D--Private-Diary"
    write_jsonl(other / "sess-2222-bbbb.jsonl", [rec("user", cwd=r"C:\Private\Diary", sessionId="sess-2222-bbbb",
                                                     message={"role": "user", "content": "segreto personale"})])
    (base / "history.jsonl").write_text(json.dumps({
        "display": "prompt di una sessione cancellata", "timestamp": 1758000000000,
        "project": r"C:\Work\Demo", "sessionId": "sess-9999-gone"}) + "\n", "utf-8")
    return base


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def digest_text(v) -> str:
    return "\n".join(p.read_text("utf-8") for p in sorted((v / "raw/sessions").rglob("part-*.md")))


def test_parser_keeps_real_prompts_and_drops_noise(claude):
    info = sessions.parse_transcript(claude / "projects/D--Work-Demo/sess-1111-aaaa.jsonl")
    assert info["title"] == "Refactoring del motore"
    assert [(m["role"], m["text"]) for m in info["messages"]] == [
        ("user", "Primo prompt come stringa"),
        ("user", "Secondo prompt in forma lista"),
        ("assistant", "Risposta parte A.\n\nRisposta parte B, chiave sk-ant-abcdefghijklmnopqrstuvwxyz0123"),
    ]


def test_two_claude_dirs_with_the_same_project_folder_name_keep_separate_memory_files(v, tmp_path):
    # a project folder can exist under two Claude config dirs with the same name (e.g. after moving
    # CLAUDE_CONFIG_DIR): one has no sessions of its own (so no cwd of its own is ever recorded), the
    # other has an old session whose cwd predates a rename. Their memory files must not collide just
    # because the folder name is shared and one dir's cached cwd leaks into the other's resolution.
    old_dir = tmp_path / "old_claude"
    new_dir = tmp_path / "new_claude"
    old_proj = old_dir / "projects" / "c--Work-progx"
    write_jsonl(old_proj / "sess-old-1111.jsonl", [rec("user", cwd=r"C:\progx", sessionId="sess-old-1111",
                                                        message={"role": "user", "content": "vecchia sessione"})])
    (old_proj / "memory").mkdir(parents=True)
    (old_proj / "memory" / "nota.md").write_text("Nota dalla cartella vecchia.\n", "utf-8")
    new_proj = new_dir / "projects" / "c--Work-progx"
    (new_proj / "memory").mkdir(parents=True)  # no .jsonl here: this dir never recorded its own cwd
    (new_proj / "memory" / "nota.md").write_text("Nota dalla cartella nuova.\n", "utf-8")
    report = sessions.import_sessions(v, claude_dir=[str(old_dir), str(new_dir)])
    assert report["ok"] if "ok" in report else True
    assert report["memory_files"] == 2
    old_note = (v / "raw/sessions/c-progx/memory/nota.md").read_text("utf-8")
    new_note = (v / "raw/sessions/c-work-progx/memory/nota.md").read_text("utf-8")
    assert "vecchia" in old_note and "nuova" in new_note


def test_import_writes_redacted_digests_memory_and_history(v, claude):
    report = sessions.import_sessions(v, claude_dir=[str(claude)], exclude=["private"])
    assert report["imported"] == 1 and report["filtered"] == 1
    assert report["memory_files"] == 1 and report["pruned_sessions_from_history"] == 1
    text = digest_text(v)
    assert "Secondo prompt in forma lista" in text
    assert "sk-ant-" not in text and "[REDACTED:anthropic-key]" in text
    assert "codice selezionato" not in text and "privato" not in text
    assert "subagente" not in text and "segreto personale" not in text
    assert "prompt di una sessione cancellata" in text
    assert list((v / "raw/sessions/c-work-demo/memory").glob("*.md"))
    assert "claude-sessions" in sources.load_sources(v)["sources"]


def test_reimport_is_idempotent_and_growth_rewrites_only_last_part(v, claude, monkeypatch):
    monkeypatch.setattr(sessions, "PART_CHARS", 200)
    sessions.import_sessions(v, claude_dir=[str(claude)])
    folder = next(p.parent for p in (v / "raw/sessions").rglob("part-001.md") if "cronologia" not in str(p) and "c-work-demo" in str(p))
    before = {p.name: p.read_bytes() for p in folder.glob("part-*.md")}
    assert len(before) >= 2

    again = sessions.import_sessions(v, claude_dir=[str(claude)])
    assert again["files_to_write"] == 0 and again["unchanged"] == 2

    path = claude / "projects/D--Work-Demo/sess-1111-aaaa.jsonl"
    write_jsonl(path, TRANSCRIPT + [user("Nuova domanda alla fine", ts="2026-09-20T11:00:00Z")])
    sessions.import_sessions(v, claude_dir=[str(claude)])
    after = {p.name: p.read_bytes() for p in folder.glob("part-*.md")}
    last = sorted(before)[-1]
    assert all(after[name] == data for name, data in before.items() if name != last)
    assert b"Nuova domanda alla fine" in b"".join(after.values())


def test_session_digests_flow_into_compile(v, claude):
    sessions.import_sessions(v, claude_dir=[str(claude)])
    plan = compiler.plan(v, source="claude-sessions")
    keys = [f["key"] for f in plan["files"]]
    assert any(k.endswith("part-001.md") for k in keys)
    assert any("/memory/" in k for k in keys)


def test_import_only_mirrors_a_single_transcript_without_the_full_sweep(v, claude):
    # a hook (SessionEnd/PreCompact) cannot afford import_sessions' full directory scan: `only`
    # must do just enough to make that one session's digest ready for compile/search, and nothing
    # that would misfire from seeing a single transcript in isolation (no memory files, no
    # "every other session looks deleted" pruned-history sweep).
    transcript = claude / "projects/D--Work-Demo/sess-1111-aaaa.jsonl"
    report = sessions.import_sessions(v, claude_dir=[str(claude)], only=str(transcript))
    assert report["imported"] == 1
    assert report["memory_files"] == 0 and report["pruned_sessions_from_history"] == 0
    text = digest_text(v)
    assert "Secondo prompt in forma lista" in text and "[REDACTED:anthropic-key]" in text
    assert not list((v / "raw/sessions/c-work-demo/memory").glob("*.md"))  # skipped in `only` mode
    # re-running `only` on the same unchanged transcript is a no-op, like a full import would be
    again = sessions.import_sessions(v, claude_dir=[str(claude)], only=str(transcript))
    assert again["imported"] == 0 and again["unchanged"] == 1


def test_import_only_with_a_transcript_outside_any_claude_dir_is_a_safe_noop(v, tmp_path):
    stray = tmp_path / "elsewhere" / "projects" / "X" / "sess.jsonl"
    stray.parent.mkdir(parents=True)
    stray.write_text("{}", "utf-8")
    report = sessions.import_sessions(v, claude_dir=[str(tmp_path / "real_claude")], only=str(stray))
    assert report["skipped"] and report["imported"] == 0 and report["files_to_write"] == 0
