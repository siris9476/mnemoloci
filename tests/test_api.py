import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient

from mnemoloci import api, inbox, memory, personal, review, vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.lock import VaultLock
from mnemoloci.markdown import render_page

TOKEN = "t0ken"
PORT = 8765
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40


@pytest.fixture
def client(tmp_path):
    v = tmp_path / "vault"
    vault_mod.init(v)
    page = render_page({"title": "Alfa", "kind": "concept", "summary": "Pagina alfa"}, "# Alfa\n\nVedi [[beta]].")
    beta = render_page({"title": "Beta", "kind": "concept", "summary": "Pagina beta"}, "# Beta\n\nTesto beta.")
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed", "operation": "test", "writes": [
        {"path": "wiki/concepts/alfa.md", "action": "create", "content": page},
        {"path": "wiki/concepts/beta.md", "action": "create", "content": beta}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    with VaultLock(v) as lock:
        review.save_candidate(v, lock, run_id="r1", slug="gamma", target_path="wiki/concepts/gamma.md",
                              base_sha256=None, reasons=[{"code": "low-confidence", "detail": "0.3"}],
                              content=render_page({"title": "Gamma", "kind": "concept"}, "# Gamma\n\nIncerto."))
    shot = tmp_path / "s.png"
    shot.write_bytes(PNG)
    inbox.add_files(v, [str(shot)])
    app = api.create_app(v, TOKEN, {"port": PORT})
    c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c, v


def test_requires_token_and_local_host(client):
    c, _ = client
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Host": f"evil.example:{PORT}"}).status_code == 403
    assert c.get("/api/status", headers={"Host": "127.0.0.1:1"}).status_code == 403
    assert c.post("/api/goals/add", content="text=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415


def test_read_endpoints(client):
    c, v = client
    status = c.get("/api/status").json()
    assert status["ok"] and status["pending_review"] == 1 and status["inbox_pending"] == 1
    graph = c.get("/api/graph").json()
    assert {n["id"] for n in graph["nodes"]} >= {"alfa", "beta"}
    assert {"source": "alfa", "target": "beta"} in graph["links"]
    hits = c.get("/api/search", params={"q": "alfa"}).json()
    assert hits["results"][0]["slug"] == "alfa"
    page = c.get("/api/page", params={"path": "wiki/concepts/alfa.md"}).json()
    assert "Vedi [[beta]]" in page["content"]
    assert c.get("/api/page", params={"path": "../outside.md"}).json()["ok"] is False
    assert c.get("/api/page", params={"path": ".mnemoloci/config.json"}).json()["ok"] is False
    [item] = c.get("/api/inbox").json()["items"]
    image = c.get(f"/api/inbox/{item['id']}/image")
    assert image.content == PNG and image.headers["content-type"] == "image/png"
    assert c.get("/api/inbox/..%2F..%2Fx/image").status_code in (400, 404)


def test_review_approve_is_pinned_to_content(client):
    c, v = client
    [cand] = c.get("/api/review").json()["candidates"]
    shown = c.get(f"/api/review/{cand['id']}").json()
    bad = c.post(f"/api/review/{cand['id']}/approve", json={"content_sha256": "0" * 64}).json()
    assert bad["code"] == "CONTENT_CHANGED"
    good = c.post(f"/api/review/{cand['id']}/approve", json={"content_sha256": shown["content_sha256"]}).json()
    assert good["ok"] and (v / "wiki/concepts/gamma.md").is_file()


def test_memory_retract_and_suppress_via_api(client):
    c, v = client
    saved = memory.add(v, "decision", "Prova auto-save", "Testo di prova.", source="assistant-auto")
    manual = memory.add(v, "decision", "Prova manuale", "Testo di prova.")
    listed = c.get("/api/memory", params={"source": "assistant-auto"}).json()["memories"]
    assert [m["id"] for m in listed] == [saved["id"]]  # the manually-sourced one is filtered out

    no_reason = c.post(f"/api/memory/{saved['id']}/retract", json={}).json()
    assert no_reason["ok"] is False and no_reason["code"] == "NO_REASON"
    retracted = c.post(f"/api/memory/{saved['id']}/retract", json={"reason": "sbagliata"}).json()
    assert retracted["ok"]
    assert c.get("/api/memory", params={"source": "assistant-auto"}).json()["memories"] == []
    [gone] = c.get("/api/memory", params={"source": "assistant-auto", "status": "retracted"}).json()["memories"]
    assert gone["id"] == saved["id"] and gone["retractedReason"] == "sbagliata"

    suppressed = c.post(f"/api/memory/{manual['id']}/suppress", json={}).json()
    assert suppressed["ok"]
    assert c.get("/api/memory").json()["memories"] == []
    [hidden] = c.get("/api/memory", params={"status": "suppressed"}).json()["memories"]
    assert hidden["id"] == manual["id"]
    reactivated = c.post(f"/api/memory/{manual['id']}/unsuppress", json={}).json()
    assert reactivated["ok"]
    assert [m["id"] for m in c.get("/api/memory").json()["memories"]] == [manual["id"]]
    again = c.post(f"/api/memory/{manual['id']}/unsuppress", json={}).json()
    assert again["ok"] is False and again["code"] == "NOT_SUPPRESSED"


def test_config_embeddings_from_app(client):
    c, v = client
    first = c.get("/api/config/embeddings").json()
    assert first["embeddings"] == {"provider": "fastembed", "ollama_model": None, "ollama_base_url": None,
                                   "openai_model": None, "voyage_model": None}
    assert first["keys_set"] == {"openai": False, "voyage": False}
    missing_model = c.post("/api/config/embeddings", json={"provider": "ollama"}).json()
    assert missing_model["ok"] is False and missing_model["code"] == "NO_OLLAMA_MODEL"
    out = c.post("/api/config/embeddings", json={"provider": "ollama", "ollama_model": "nomic-embed-text",
                                                  "ollama_base_url": "http://box:11434"}).json()
    assert out["ok"] and out["embeddings"]["ollama_model"] == "nomic-embed-text"
    assert c.get("/api/config/embeddings").json()["embeddings"]["provider"] == "ollama"
    assert vault_mod.load_config(v)["embeddings"]["ollama_model"] == "nomic-embed-text"


def test_config_embeddings_key_from_app_never_echoes_the_key_back(client):
    c, v = client
    empty = c.post("/api/config/embeddings/key", json={"provider": "openai", "api_key": ""}).json()
    assert empty["ok"] is False and empty["code"] == "EMPTY_KEY"
    out = c.post("/api/config/embeddings/key", json={"provider": "openai", "api_key": "sk-secret"}).json()
    assert out["ok"] and out["key_set"] is True
    status = c.get("/api/config/embeddings").json()
    assert status["keys_set"] == {"openai": True, "voyage": False}
    assert "sk-secret" not in c.get("/api/config/embeddings").text  # never echoed back
    assert vault_mod.get_api_key(v, "openai") == "sk-secret"


def test_goals_and_profile_from_app(client):
    c, v = client
    g = c.post("/api/goals/add", json={"text": "Usare l'app desktop", "area": "progetti"}).json()
    assert g["goal"]["id"] == "g1"
    assert c.post("/api/goals/g1", json={"status": "done"}).json()["goal"]["status"] == "done"
    assert c.post("/api/profile/set", json={"key": "editor", "value": "VS Code"}).json()["ok"]
    refused = c.post("/api/profile/set", json={"key": "iban", "value": "IT60 X054 2811 1010 0000 0123 456"}).json()
    assert refused["ok"] is False and refused["code"] == "SENSITIVE_DATA"
    assert personal.profile_show(v)["facts"][0]["value"] == "VS Code"
