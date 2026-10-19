import io
import json
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mnemoloci import embed, export, hooks, index, instincts, sources, vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import MnemoError, ValidationError
from mnemoloci.core.hashing import sha256_bytes
from mnemoloci.markdown import render_page

CWD = r"C:\Users\you\Work\Demo"


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _transcript(tmp_path, turns):
    folder = tmp_path / "claude" / "projects" / "D--Work-Demo"
    folder.mkdir(parents=True, exist_ok=True)
    recs = []
    for i, (role, text) in enumerate(turns):
        rec = {"type": role, "sessionId": "sess-1", "cwd": CWD, "timestamp": f"2026-09-2{i % 10}T10:00:0{i % 10}Z",
               "message": {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}]}}
        if role == "assistant":
            rec["message"]["id"] = f"m{i}"
        recs.append(rec)
    (folder / "sess-1.jsonl").write_text("\n".join(json.dumps(r) for r in recs), "utf-8")


def _proposal(moment, **extra):
    return {"trigger": "l'utente chiede di analizzare progetti", "action": "leggi il codice senza installare nulla",
            "scope": "global", "evidence": [{"session_id": moment["session_id"], "ts": moment["ts"]}], **extra}


def _write(tmp_path, proposals):
    f = tmp_path / "proposals.json"
    f.write_text(json.dumps({"schema": instincts.PROPOSALS_SCHEMA, "proposals": proposals}), "utf-8")
    return str(f)


def test_instincts_need_approval_before_injection(v, tmp_path):
    _transcript(tmp_path, [
        ("user", "analizza questo progetto"),
        ("assistant", "Lo installo e lo provo sul sistema."),
        ("user", "No, non devi installare nulla: leggi solo il codice."),
        ("assistant", "Ok, leggo il codice."),
        ("user", "Perfetto, continua così"),
        ("assistant", "Fatto."),
        ("user", "grazie, ora il prossimo"),
    ])
    mined = instincts.mine(v)
    moments = json.loads(Path(mined["packet"]).read_text("utf-8"))["moments"]
    assert [m["kind"] for m in moments] == ["correction", "confirmation"]
    assert "installo" in moments[0]["assistant_before"]
    assert instincts.mine(v)["new_moments"] == 0  # already known

    out = instincts.propose(v, _write(tmp_path, [_proposal(moments[0])]))
    [iid] = out["created"]
    listed = instincts.list_instincts(v)["instincts"][0]
    assert listed["status"] == "proposed" and listed["confidence"] == 0.3
    assert hooks.build_context(v, CWD) is None  # proposed instincts are never injected

    instincts.set_status(v, iid, "active")
    ctx = hooks.build_context(v, CWD)
    assert "leggi il codice senza installare nulla" in ctx and "Istinti approvati" in ctx

    instincts.set_status(v, iid, "retired", "non serve più")
    assert hooks.build_context(v, CWD) is None
    assert (v / listed["path"]).is_file()  # retired, not deleted


def test_instinct_evidence_must_be_a_mined_moment(v, tmp_path):
    fake = {"session_id": "sess-x", "ts": "2026-01-01T00:00:00Z"}
    with pytest.raises(ValidationError) as info:
        instincts.propose(v, _write(tmp_path, [_proposal(fake)]))
    assert "not a mined moment" in json.dumps(info.value.details)


def test_instinct_text_is_checked_and_scope_respected(v, tmp_path):
    _transcript(tmp_path, [("assistant", "x"), ("user", "Sbagliato, usa sempre python -m")])
    m = json.loads(Path(instincts.mine(v)["packet"]).read_text("utf-8"))["moments"][0]
    with pytest.raises(ValidationError):
        instincts.propose(v, _write(tmp_path, [_proposal(m, action="usa la chiave sk-ant-abcdefghijklmnopqrstuvwxyz012345")]))
    out = instincts.propose(v, _write(tmp_path, [_proposal(m, scope=CWD, action="usa python -m mnemoloci")]))
    instincts.set_status(v, out["created"][0], "active")
    assert "python -m mnemoloci" in hooks.build_context(v, CWD)
    assert hooks.build_context(v, r"C:\Users\you\Altro") is None


def test_confidence_table():
    assert [instincts.confidence_for(n) for n in (1, 2, 3, 5, 6, 10, 11)] == [0.3, 0.3, 0.5, 0.5, 0.7, 0.7, 0.85]


def _seed_pages(v, pages):
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"seed-{len(pages)}", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{slug}.md", "action": "create",
                     "content": render_page({"title": t, "kind": "concept", "summary": s}, body)}
                    for slug, (t, s, body) in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


class FakeEmbedder:
    """Deterministic 'semantic' space: synonyms share a dimension."""
    GROUPS = [{"auto", "macchina", "veicolo", "car"}, {"cucina", "ricetta", "pasta"}, {"lock", "blocco", "serratura"}]

    def embed(self, texts, batch_size=32):
        import numpy as np

        for text in texts:
            words = {w.strip(".,:;").lower() for w in text.split()}
            yield np.array([float(bool(words & g)) for g in self.GROUPS] + [0.01], dtype=np.float32)


def test_hybrid_search_fuses_semantic_hits(v, monkeypatch):
    pytest.importorskip("numpy")
    monkeypatch.setattr(embed, "available", lambda v: True)
    monkeypatch.setattr(embed, "_model", FakeEmbedder())
    _seed_pages(v, {
        "veicolo-elettrico": ("Veicolo elettrico", "Mobilità", "Il veicolo usa una batteria."),
        "ricette": ("Ricette", "Cucina", "Pasta al pomodoro."),
    })
    assert index.query(v, "macchina")["results"] == []  # no lexical overlap at all
    assert embed.build(v)["embedded_pages"] == 2
    assert embed.build(v)["embedded_pages"] == 0  # incremental
    hybrid = index.query(v, "macchina")
    assert hybrid["mode"] == "hybrid" and hybrid["results"][0]["slug"] == "veicolo-elettrico"
    assert index.query(v, "macchina", mode="lexical")["results"] == []

    page = v / "wiki/concepts/veicolo-elettrico.md"
    old = page.read_bytes()
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "edit", "operation": "test", "writes": [
        {"path": "wiki/concepts/veicolo-elettrico.md", "action": "replace", "expected_sha256": sha256_bytes(old),
         "content": render_page({"title": "Veicolo elettrico", "kind": "concept", "summary": "Mobilità"}, "Testo nuovo.")}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)
    assert embed.stale_count(v) == 1
    assert all(r["slug"] != "veicolo-elettrico" for r in index.query(v, "macchina")["results"])  # stale vectors ignored


def test_hybrid_mode_requires_embeddings(v):
    _seed_pages(v, {"x": ("X", "x", "x")})
    with pytest.raises(MnemoError) as info:
        index.query(v, "x", mode="hybrid")
    assert info.value.code == "NO_EMBEDDINGS"


class _FakeOllamaResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_config_embeddings_switches_provider_and_validates(v):
    with pytest.raises(MnemoError) as info:
        vault_mod.set_embeddings(v, "ollama")  # no --ollama-model
    assert info.value.code == "NO_OLLAMA_MODEL"
    with pytest.raises(MnemoError):
        vault_mod.set_embeddings(v, "not-a-provider")
    out = vault_mod.set_embeddings(v, "ollama", ollama_model="nomic-embed-text")
    assert out["embeddings"] == {"provider": "ollama", "ollama_model": "nomic-embed-text", "ollama_base_url": None,
                                 "openai_model": None, "voyage_model": None}
    assert embed.model_id(v) == "ollama:nomic-embed-text"
    vault_mod.set_embeddings(v, "fastembed")
    assert embed.model_id(v) == embed.MODEL  # back to the default, unaffected by the earlier switch
    vault_mod.set_embeddings(v, "openai")  # no model given: falls back to the default at use-time
    assert embed.model_id(v) == "openai:text-embedding-3-small"
    vault_mod.set_embeddings(v, "voyage", voyage_model="voyage-3-lite")
    assert embed.model_id(v) == "voyage:voyage-3-lite"


def test_ollama_embeddings_end_to_end(v, monkeypatch):
    pytest.importorskip("numpy")
    vault_mod.set_embeddings(v, "ollama", ollama_model="nomic-embed-text", ollama_base_url="http://box:11434")
    assert embed.available(v)  # no fastembed needed for this provider

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data)))
        n = len(json.loads(req.data)["input"])
        return _FakeOllamaResponse(json.dumps({"embeddings": [[1.0, 0.0] for _ in range(n)]}).encode("utf-8"))

    monkeypatch.setattr(embed.urllib.request, "urlopen", fake_urlopen)
    _seed_pages(v, {"pagina": ("Pagina", "Riassunto", "Corpo del testo.")})
    out = embed.build(v)
    assert out["embedded_pages"] == 1 and out["model"] == "ollama:nomic-embed-text"
    assert calls[0][0] == "http://box:11434/api/embed" and calls[0][1]["model"] == "nomic-embed-text"
    assert embed.has_vectors(v)
    results = embed.search(v, "qualsiasi query")
    assert results and results[0][0] == "wiki/concepts/pagina.md"


def test_ollama_unreachable_reports_clearly(v, monkeypatch):
    pytest.importorskip("numpy")
    vault_mod.set_embeddings(v, "ollama", ollama_model="nomic-embed-text")

    def fail(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(embed.urllib.request, "urlopen", fail)
    _seed_pages(v, {"pagina": ("Pagina", "Riassunto", "Corpo del testo.")})
    with pytest.raises(MnemoError) as info:
        embed.build(v)
    assert info.value.code == "OLLAMA_UNREACHABLE"


def test_api_key_env_var_wins_over_the_stored_file(v, monkeypatch):
    assert vault_mod.get_api_key(v, "openai") is None
    vault_mod.set_api_key(v, "openai", "sk-from-file")
    assert vault_mod.get_api_key(v, "openai") == "sk-from-file"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert vault_mod.get_api_key(v, "openai") == "sk-from-env"  # env always wins, even with a file present
    with pytest.raises(MnemoError):
        vault_mod.set_api_key(v, "openai", "")
    with pytest.raises(MnemoError):
        vault_mod.set_api_key(v, "fastembed", "x")  # not a cloud provider


def test_openai_embeddings_end_to_end_and_missing_key(v, monkeypatch):
    pytest.importorskip("numpy")
    vault_mod.set_embeddings(v, "openai")
    _seed_pages(v, {"pagina": ("Pagina", "Riassunto", "Corpo del testo.")})
    with pytest.raises(MnemoError) as info:
        embed.build(v)
    assert info.value.code == "NO_API_KEY"

    vault_mod.set_api_key(v, "openai", "sk-test")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        n = len(json.loads(req.data)["input"])
        body = {"data": [{"embedding": [1.0, 0.0], "index": i} for i in reversed(range(n))]}  # out of order on purpose
        return _FakeOllamaResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(embed.urllib.request, "urlopen", fake_urlopen)
    out = embed.build(v)
    assert out["embedded_pages"] == 1 and out["model"] == "openai:text-embedding-3-small"
    assert calls[0][0] == "https://api.openai.com/v1/embeddings"
    assert calls[0][1] == "Bearer sk-test" and calls[0][2]["model"] == "text-embedding-3-small"
    assert embed.has_vectors(v)


def test_voyage_embeddings_http_error_is_reported_clearly(v, monkeypatch):
    pytest.importorskip("numpy")
    vault_mod.set_embeddings(v, "voyage", voyage_model="voyage-3-lite")
    vault_mod.set_api_key(v, "voyage", "pa-test")
    _seed_pages(v, {"pagina": ("Pagina", "Riassunto", "Corpo del testo.")})

    def fail(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"error": "bad key"}'))

    monkeypatch.setattr(embed.urllib.request, "urlopen", fail)
    with pytest.raises(MnemoError) as info:
        embed.build(v)
    assert info.value.code == "CLOUD_EMBED_FAILED" and "401" in info.value.message


def test_graph_exports(v, tmp_path):
    _seed_pages(v, {
        "alfa": ("Alfa </script><b>", "a", "Vedi [[beta]]."),
        "beta": ("Beta", "b", "Testo."),
    })
    out = export.export_graph(v, "graphml", str(tmp_path / "g.graphml"))
    root = ET.parse(out["out"]).getroot()
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    assert len(root.findall(".//g:node", ns)) == 2 and len(root.findall(".//g:edge", ns)) == 1
    html_out = Path(export.export_graph(v, "html", str(tmp_path / "g.html"))["out"]).read_text("utf-8")
    assert "Alfa \\u003c/script>" in html_out and "</script><b>" not in html_out.split("const G =")[1].split("\n")[0]
    data = json.loads(Path(export.export_graph(v, "json", str(tmp_path / "g.json"))["out"]).read_text("utf-8"))
    assert {"source": "alfa", "target": "beta"} in data["links"]


def test_graph_exports_cypher_and_sql(v, tmp_path):
    _seed_pages(v, {
        "alfa": ("Alfa O'Brien", "a", "Vedi [[beta]]."),
        "beta": ("Beta", "b", "Testo."),
    })
    cypher = Path(export.export_graph(v, "cypher", str(tmp_path / "g.cypher"))["out"]).read_text("utf-8")
    assert "MERGE (n:Page {slug: 'alfa'}) SET n += {" in cypher
    assert "title: 'Alfa O\\'Brien'" in cypher  # a literal single quote is escaped, not left to break the script
    assert "MATCH (a:Page {slug: 'alfa'}), (b:Page {slug: 'beta'}) MERGE (a)-[:LINKS_TO]->(b);" in cypher
    sql = Path(export.export_graph(v, "sql", str(tmp_path / "g.sql"))["out"]).read_text("utf-8")
    assert "CREATE TABLE IF NOT EXISTS pages" in sql and "CREATE TABLE IF NOT EXISTS links" in sql
    assert "INSERT INTO pages (slug, path, title, kind, confidence) VALUES ('alfa'," in sql and "'Alfa O''Brien'" in sql
    assert "INSERT INTO links (source, target) VALUES ('alfa', 'beta');" in sql


def test_vault_export_reports_freshness(v, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "README.md").write_text("riga uno\nriga due\n", "utf-8")
    sources.add_source(v, str(project), "proj")
    sha = sha256_bytes((project / "README.md").read_bytes())
    fm = {"title": "Pagina", "kind": "concept", "summary": "s", "sources": [{"key": "proj/README.md", "sha256": sha}]}
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "p", "operation": "test", "writes": [
        {"path": "wiki/concepts/pagina.md", "action": "create", "content": render_page(fm, "Testo ^[proj/README.md#L1-L2].")}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    first = json.loads(Path(export.export_vault(v, str(tmp_path / "a.json"), "demo")["out"]).read_text("utf-8"))
    [page] = first["pages"]
    assert page["id"] == "demo:pagina" and page["freshnessStatus"] == "fresh"
    assert page["citations"] == [{"file": "proj/README.md", "start": 1, "end": 2}]
    (project / "README.md").write_text("cambiato\n", "utf-8")
    assert json.loads(Path(export.export_vault(v, str(tmp_path / "b.json"))["out"]).read_text("utf-8"))["pages"][0]["freshnessStatus"] == "stale"
