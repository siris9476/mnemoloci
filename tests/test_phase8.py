"""Phase 8: todos, ChatGPT / claude.ai export import, git backup, digest and the "today" view."""

import json
import shutil
import subprocess
import zipfile
from datetime import date, timedelta

import pytest

from mnemoloci import backup, chat_import, clips, digest, hooks, memory, personal, session_index, sources
from mnemoloci import vault as vault_mod
from mnemoloci.core.errors import MnemoError, ValidationError
from mnemoloci.markdown import split_frontmatter

CWD = r"C:\Users\you\Work\Demo"
TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


# ---------------- todos ----------------

def test_todos_are_kept_scoped_and_injected(v):
    personal.todos_add(v, "Provare /cd dal telefono", project=CWD, due=YESTERDAY)
    personal.todos_add(v, "Comprare il dominio")
    personal.todos_add(v, "Altro progetto", project=r"C:\Users\you\Work\Other")
    with pytest.raises(ValidationError):
        personal.todos_add(v, "Comprare il dominio")
    with pytest.raises(ValidationError):
        personal.todos_add(v, "Data sbagliata", due="24/09/2026")
    mine = [t["text"] for t in personal.todos_list(v, project=CWD)["todos"]]
    assert mine == ["Provare /cd dal telefono", "Comprare il dominio"]  # overdue first, other project left out
    context = hooks.build_context(v, CWD)
    assert "[t1] Provare /cd dal telefono (SCADUTA" in context and "Altro progetto" not in context
    personal.todos_update(v, "t1", status="done")
    everything = personal.todos_list(v, include_closed=True)["todos"]
    assert {t["id"]: t["status"] for t in everything}["t1"] == "done"  # kept, never deleted
    assert "- [x] **[t1]**" in (v / personal.TODOS_PATH).read_text("utf-8")


# ---------------- chat exports ----------------

def _chatgpt_export():
    def node(nid, parent, role, text, t, children=(), ctype="text", hidden=False):
        msg = None if role is None else {
            "author": {"role": role}, "create_time": t, "content": {"content_type": ctype, "parts": [text]},
            "metadata": {"is_visually_hidden_from_conversation": True} if hidden else {}}
        return nid, {"id": nid, "message": msg, "parent": parent, "children": list(children)}
    mapping = dict([
        node("root", None, None, "", 0, ["sys"]),
        node("sys", "root", "system", "You are ChatGPT", 1, ["u1"], hidden=True),
        node("u1", "sys", "user", "Come organizzo le note?", 1_758_700_000, ["a1-old", "a1"]),
        node("a1-old", "u1", "assistant", "RISPOSTA SCARTATA", 1_758_700_010),
        node("a1", "u1", "assistant", "Usa uno Zettelkasten. La mia chiave è sk-proj-" + "b" * 40, 1_758_700_020, ["t1"]),
        node("t1", "a1", "tool", "risultato browsing", 1_758_700_030, ["u2"]),
        node("u2", "t1", "user", "E i tag?", 1_758_700_040, ["a2"]),
        node("a2", "u2", "assistant", "Pochi tag, molti link. Vuoi un esempio?", 1_758_700_050),
    ])
    return [{"title": "Note e Zettelkasten", "conversation_id": "c0ffee00-1111", "create_time": 1_758_700_000,
             "update_time": 1_758_700_050, "mapping": mapping, "current_node": "a2"}]


def test_chatgpt_export_follows_the_visible_branch(v, tmp_path):
    export = tmp_path / "chatgpt.zip"
    with zipfile.ZipFile(export, "w") as z:
        z.writestr("conversations.json", json.dumps(_chatgpt_export()))
    dry = chat_import.import_export(v, str(export), dry_run=True)
    assert dry["provider"] == "chatgpt" and dry["imported"] == 1 and dry["messages"] == 4
    out = chat_import.import_export(v, str(export))
    parts = list((v / "raw/chats/chatgpt").rglob("part-001.md"))
    text = parts[0].read_text("utf-8")
    assert "Zettelkasten" in text and "RISPOSTA SCARTATA" not in text and "You are ChatGPT" not in text
    assert "risultato browsing" not in text and "sk-proj-" not in text and "· ChatGPT" in text
    assert sources.load_sources(v)["sources"]["chats"]["kind"] == "sessions"
    assert chat_import.import_export(v, str(export))["unchanged"] == 1
    hit = session_index.search(v, "Zettelkasten", auto_refresh=False)["sessions"][0]
    assert hit["session_id"] == "chatgpt:c0ffee00-1111" and hit["cwd"] == "ChatGPT"
    assert session_index.show(v, "chatgpt:c0ffee")["messages"][-1]["text"].startswith("Pochi tag")
    assert out["next"].endswith("--source chats")


def test_sharded_chatgpt_export_tool_commands_and_voice(v, tmp_path):
    first = _chatgpt_export()[0]
    second = json.loads(json.dumps(first))
    second.update(conversation_id="beef0000-2222", title="Seconda")
    # a canvas command (recipient != all) and a voice message transcription
    second["mapping"]["a2"]["message"]["recipient"] = "canmore.create_textdoc"
    second["mapping"]["u2"]["message"]["content"] = {"content_type": "multimodal_text", "parts": [
        {"content_type": "audio_transcription", "text": "Trascrizione del vocale"}]}
    second["current_node"] = "a2"
    export = tmp_path / "big.zip"
    with zipfile.ZipFile(export, "w") as z:
        z.writestr("export/conversations-000.json", json.dumps([first]))
        z.writestr("export/conversations-001.json", json.dumps([second]))
        z.writestr("export/shared_conversations.json", json.dumps([{"id": "x", "title": "stub"}]))
    out = chat_import.import_export(v, str(export))
    assert out["conversations"] == 2 and out["imported"] == 2
    text = next(p for p in (v / "raw/chats/chatgpt").rglob("part-001.md") if "seconda" in str(p)).read_text("utf-8")
    assert "Trascrizione del vocale" in text and "Pochi tag" not in text


def test_claude_ai_new_format_reads_content_blocks_and_attachments(v, tmp_path):
    conv = {"uuid": "ffff0000-9999", "name": "Contratto", "created_at": "2026-09-21T08:00:00Z", "chat_messages": [
        {"sender": "human", "text": "", "created_at": "2026-09-21T08:00:00Z",
         "content": [{"type": "text", "text": "Riassumi il contratto"}],
         "attachments": [{"file_name": "contratto.txt", "extracted_content": "Durata: 12 mesi. Recesso: 30 giorni."}]},
        {"sender": "assistant", "text": "This block is not supported on your current device yet.",
         "created_at": "2026-09-21T08:01:00Z",
         "content": [{"type": "thinking", "thinking": "ragionamento interno"}, {"type": "text", "text": "Dura un anno."}]}]}
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([conv]), "utf-8")
    chat_import.import_export(v, str(export))
    text = next((v / "raw/chats/claude-ai").rglob("part-001.md")).read_text("utf-8")
    assert "Riassumi il contratto" in text and "Recesso: 30 giorni" in text and "Dura un anno." in text
    assert "not supported" not in text and "ragionamento interno" not in text


def test_claude_ai_export_and_incremental_update(v, tmp_path):
    conv = {"uuid": "abcd1234-0000", "name": "Viaggio a Lisbona", "created_at": "2026-09-20T08:00:00Z",
            "updated_at": "2026-09-20T08:05:00Z", "chat_messages": [
                {"sender": "human", "text": "Cosa vedere a Lisbona?", "created_at": "2026-09-20T08:00:00Z",
                 "attachments": [{"file_name": "itinerario.pdf"}]},
                {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "L'Alfama e Belém."}],
                 "created_at": "2026-09-20T08:01:00Z"}]}
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([conv]), "utf-8")
    assert chat_import.import_export(v, str(export))["provider"] == "claude-ai"
    folder = next((v / "raw/chats/claude-ai").iterdir())
    assert "[allegato: itinerario.pdf]" in (folder / "part-001.md").read_text("utf-8")
    conv["chat_messages"].append({"sender": "human", "text": "E per mangiare?", "created_at": "2026-09-20T09:00:00Z"})
    export.write_text(json.dumps([conv]), "utf-8")
    again = chat_import.import_export(v, str(export))
    assert again["imported"] == 1 and next((v / "raw/chats/claude-ai").iterdir()) == folder  # same folder, updated
    assert "E per mangiare?" in (folder / "part-001.md").read_text("utf-8")
    assert chat_import.import_export(v, str(export), exclude=["lisbona"])["filtered"] == 1
    bad = tmp_path / "x.json"
    bad.write_text(json.dumps([{"foo": 1}]), "utf-8")
    with pytest.raises(ValidationError):
        chat_import.import_export(v, str(bad))


# ---------------- backup ----------------

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


@needs_git
def test_backup_commits_versions_and_ignores_derived_data(v, tmp_path):
    with pytest.raises(MnemoError):
        backup.commit(v)  # not set up yet
    with pytest.raises(MnemoError):
        backup.init(v, auto_push=True)  # auto push without a remote
    out = backup.init(v)
    assert out["created"] and out["first_commit"]["committed"] and out["auto_commit"] is True
    (v / ".mnemoloci/index.sqlite").write_bytes(b"derived")
    tracked = subprocess.run(["git", "-C", str(v), "ls-files"], capture_output=True, text=True).stdout
    assert "wiki/log.md" in tracked and "index.sqlite" not in tracked and ".mnemoloci/config.json" in tracked
    assert backup.commit(v)["committed"] is False  # only ignored files changed
    personal.goals_add(v, "Imparare il portoghese")
    second = backup.commit(v, "obiettivo")
    assert second["committed"] and second["files"] == 1
    assert [c["message"] for c in backup.history(v, "_meta/goals.md")["commits"]] == ["obiettivo"]
    assert backup.status(v)["uncommitted_changes"] == 0


@needs_git
def test_backup_self_heals_gitignore_for_a_vault_set_up_before_a_new_exclusion_existed(v):
    backup.init(v)
    ignore = v / ".gitignore"
    # simulate a vault whose .gitignore predates embedding_keys.json becoming a sensitive path
    old_text = ignore.read_text("utf-8").replace(".mnemoloci/embedding_keys.json\n", "")
    assert ".mnemoloci/embedding_keys.json" not in old_text and "# Mnemoloci: derived or machine-local data" in old_text
    ignore.write_text(old_text, "utf-8")
    (v / ".mnemoloci/embedding_keys.json").write_text('{"openai": "sk-secret"}', "utf-8")
    out = backup.commit(v, "after the fact")
    assert out["committed"]  # the .gitignore fix itself is a real change worth a commit
    assert ".mnemoloci/embedding_keys.json" in ignore.read_text("utf-8")
    tracked = subprocess.run(["git", "-C", str(v), "ls-files"], capture_output=True, text=True).stdout
    assert "embedding_keys.json" not in tracked  # never staged, thanks to the self-heal before `git add`


@needs_git
def test_backup_push_to_a_remote_and_on_session_end(v, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    backup.init(v, remote=str(remote))
    pushed = backup.now(v, push_after=True)  # nothing new to commit, but the first backup goes out
    assert pushed["committed"] is False and pushed["push"]["pushed"] is True
    assert backup.status(v)["unpushed_commits"] == 0
    assert subprocess.run(["git", "-C", str(remote), "log", "--oneline"], capture_output=True, text=True).stdout.strip()
    # the end-of-session hook commits whatever the session changed
    clips.add_text(v, "Nota della sessione", title="Nota")
    folder = tmp_path / "claude" / "projects" / "D--Work-Demo"
    folder.mkdir(parents=True)
    recs = [{"type": "user", "sessionId": "s1", "cwd": CWD, "timestamp": "2026-09-24T10:00:00Z",
             "message": {"role": "user", "content": "ciao"}}]
    (folder / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in recs), "utf-8")
    result = hooks.session_end(v, {"transcript_path": str(folder / "s1.jsonl"), "cwd": CWD})
    assert result["backup"]["committed"] and "fine sessione" in backup.history(v, limit=1)["commits"][0]["message"]


# ---------------- digest ----------------

def test_digest_gathers_the_day_and_saves_a_note(v, tmp_path):
    personal.todos_add(v, "Scaduta", due=YESTERDAY)
    personal.todos_add(v, "Da chiudere")
    personal.todos_update(v, "t2", status="done")
    personal.goals_add(v, "Finire Mnemoloci")
    clips.add_text(v, "Articolo interessante.", title="Articolo")
    memory.add(v, "decision", "Usare SQLite per l'indice", "Perché è derivato e ricostruibile.", project=CWD)
    folder = tmp_path / "claude" / "projects" / "D--Work-Demo"
    folder.mkdir(parents=True)
    now = date.today().isoformat()
    recs = [{"type": "user", "sessionId": "s9", "cwd": CWD, "timestamp": f"{now}T11:00:00Z",
             "message": {"role": "user", "content": "lavoro di oggi"}},
            {"type": "assistant", "sessionId": "s9", "cwd": CWD, "timestamp": f"{now}T11:01:00Z",
             "message": {"role": "assistant", "id": "m", "content": [{"type": "text", "text": "Fatto. Procedo?"}]}}]
    (folder / "s9.jsonl").write_text("\n".join(json.dumps(r) for r in recs), "utf-8")
    d = digest.build(v)
    assert d["period"] == TODAY and d["captured"][0]["title"] == "Articolo"
    assert d["memory"]["decision"][0]["title"] == "Usare SQLite per l'indice"
    assert [t["text"] for t in d["todos"]["overdue"]] == ["Scaduta"] and d["todos"]["done"][0]["text"] == "Da chiudere"
    assert d["sessions"][0]["open_question"] == "Fatto. Procedo?" and d["waiting"]["to_compile"] == 1
    text = digest.render(d, v)
    assert "## Contenuti salvati" in text and "SCADUTA" in text and "Finire Mnemoloci" in text
    saved = digest.save(v)
    fm, body = split_frontmatter((v / saved["path"]).read_text("utf-8"))
    assert saved["path"] == f"_meta/digests/{TODAY}.md" and fm["period"] == TODAY and "Riepilogo del" in body
    assert digest.save(v)["path"] == saved["path"]  # saving again refreshes the same note
    week = digest.build(v, week=True)
    assert week["period"].count("-W") == 1 and week["captured"]
    with pytest.raises(ValidationError):
        digest.build(v, day="ieri")


def test_digest_surfaces_a_cross_community_link_only_once_it_is_published(v):
    """graph_analysis already computes surprising_links on every call; the digest's own job is
    just to filter that down to links touching a page published in THIS period, so the same
    standing connection isn't repeated in every digest forever."""
    from mnemoloci.core import txn as txn_mod
    from mnemoloci.markdown import render_page as render

    sha = "a" * 64
    fm = lambda title: {"title": title, "kind": "concept", "summary": f"{title}.", "aliases": [],
                        "sources": [{"key": "p/a.md", "sha256": sha}], "confidence": 0.9,
                        "provenanceState": "extracted", "contradictedBy": [], "language": "it"}
    pages = {}
    for group, names in (("a", ["a1", "a2", "a3"]), ("b", ["b1", "b2", "b3"])):
        for n in names:
            others = " ".join(f"[[{m}]]" for m in names if m != n)
            extra = " [[b1]]" if n == "a3" else ""  # the one edge that crosses communities
            pages[n] = f"wiki/concepts/{n}.md", render(fm(n.upper()), f"Gruppo {group}: {others}{extra}")
    bundle = {"schema": txn_mod.BUNDLE_SCHEMA, "operation_id": "seed-cross-community", "operation": "test",
             "writes": [{"path": p, "action": "create", "content": c} for p, c in pages.values()]}
    txn_mod.apply(v, bundle, txn_mod.prepare(v, bundle).approval_sha256)

    # not published yet today: nothing new to surface, even though the cross-community edge exists
    d = digest.build(v)
    assert d["connections"]["surprising"] == []
    assert "Collegamenti emersi" not in digest.render(d, v)

    # a3 gets published today (same log.md shape publish.py writes)
    stamp = date.today().isoformat() + " 12:00"
    (v / "wiki/log.md").write_text(f"# Registro\n\n## [{stamp}] pubblicate 1\n- [[a3]]\n", "utf-8")

    d = digest.build(v)
    assert len(d["connections"]["surprising"]) == 1
    link = d["connections"]["surprising"][0]
    assert {link["a"], link["b"]} == {"A3", "B1"}
    text = digest.render(d, v)
    assert "Collegamenti emersi" in text and "A3" in text and "B1" in text


def test_today_api_todos_and_owner_only_backup(v):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api, remote

    c = TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=True), base_url="http://127.0.0.1:8790")
    owner = {"Authorization": "Bearer launch"}
    assert c.post("/api/todos/add", json={"text": "Chiamare il commercialista", "due": YESTERDAY}, headers=owner).json()["ok"]
    today = c.get("/api/today", headers=owner).json()
    assert today["todos"][0]["text"] == "Chiamare il commercialista" and today["digest"]["todos"]["overdue"]
    assert today["backup"]["enabled"] is False
    assert c.get("/api/status", headers=owner).json()["todos_overdue"] == 1
    assert c.post("/api/todos/t1", json={"status": "done"}, headers=owner).json()["todo"]["status"] == "done"
    assert c.post("/api/digest/save", json={}, headers=owner).json()["path"].startswith("_meta/digests/")
    host = "mypc.tail1234.ts.net"
    remote.allow_hosts(v, [host])
    phone = {"Authorization": f"Bearer {remote.claim(v, remote.create_pairing_code(v)['code'], 'iPhone')['token']}", "Host": host}
    assert c.post("/api/todos/add", json={"text": "Dal telefono"}, headers=phone).json()["ok"]
    assert c.post("/api/backup/now", json={}, headers=phone).json()["code"] == "OWNER_ONLY"
