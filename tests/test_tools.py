import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient

from mnemoloci import api, reason, tools, vault as vault_mod
from mnemoloci.core.errors import MnemoError, ValidationError

TOKEN = "t0ken"
PORT = 8767


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


@pytest.fixture
def client(v):
    c = TestClient(api.create_app(v, TOKEN, {"port": PORT}), base_url=f"http://127.0.0.1:{PORT}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c, v


def test_every_spec_has_a_runner_and_a_group_and_well_formed_fields():
    groups = {g["id"] for g in tools.catalogue()["groups"]}
    assert {s["id"] for s in tools.SPECS} == set(tools._RUN) and len({s["id"] for s in tools.SPECS}) == len(tools.SPECS)
    for s in tools.SPECS:
        assert s["group"] in groups and s["title"] and len(s["help"]) > 15
        for f in s["fields"]:
            assert f["type"] in ("text", "textarea", "int", "float", "bool", "select", "abspath") and f["label"]
            assert f["type"] != "select" or f["choices"]


def test_a_tool_that_spends_needs_confirmation_and_says_how_much(v, monkeypatch):
    seen = []
    monkeypatch.setattr(reason, "ask", lambda vault, q, **kw: seen.append((q, kw)) or {"answer": "Sì", "cost_usd": 0.01})
    with pytest.raises(ValidationError) as exc:
        tools.run(v, "reason", {"question": "Che cosa ho deciso?", "budget": 1.5})
    assert exc.value.code == "CONFIRM_COST" and exc.value.details == {"cap_usd": 1.5} and not seen
    with pytest.raises(ValidationError) as exc:
        tools.run(v, "reason", {"question": "Che cosa ho deciso?"})
    assert exc.value.details == {"cap_usd": 0.5}  # the saved cap
    out = tools.run(v, "reason", {"question": "Che cosa ho deciso?"}, confirm_cost=True)
    assert out["result"]["answer"] == "Sì" and seen == [("Che cosa ho deciso?", {"budget": None, "model": None, "roots": None})]


def test_parameters_are_validated(v):
    for tool, params in (("reason", {}), ("reason", {"question": "x" * 3000}), ("reason-history", {"limit": 0}),
                         ("reason-history", {"limit": "molti"}), ("export-jsonld", {"out": "relativo/cartella"}),
                         ("export-graph", {"format": "pdf", "out": r"C:\Users\you\x"}), ("quality-eval", {"record": "sì"}),
                         ("nope", {}), ("reason-history", [])):
        with pytest.raises(ValidationError):
            tools.run(v, tool, params, confirm_cost=True)


def test_defaults_are_applied_and_imports_default_to_a_preview(v, tmp_path, monkeypatch):
    from mnemoloci import okf

    seen = {}
    monkeypatch.setattr(okf, "import_okf", lambda vault, bundle, dry_run: seen.update(bundle=bundle, dry_run=dry_run) or {"pages": 0})
    tools.run(v, "import-okf", {"bundle": str(tmp_path)})
    assert seen == {"bundle": str(tmp_path), "dry_run": True}
    tools.run(v, "import-okf", {"bundle": str(tmp_path), "dry_run": False})
    assert seen["dry_run"] is False


def test_read_only_tools_run_for_real(v):
    assert tools.run(v, "reason-history", {})["result"]["runs"] == []
    assert tools.run(v, "privacy-audit", {})["result"]
    assert tools.run(v, "check-citations", {})["result"]["citations"] == 0
    assert tools.run(v, "context-cost", {})["result"]["injected"] is False
    assert tools.run(v, "quality-history", {})["result"]["history"] == []


def test_an_export_writes_where_it_was_told(v, tmp_path):
    out = tmp_path / "grafo.html"
    done = tools.run(v, "export-graph", {"format": "html", "out": str(out)})
    assert out.is_file() and done["tool"] == "export-graph"


# ---------- HTTP ----------

def test_api_lists_and_runs_tools(client):
    c, _ = client
    listing = c.get("/api/tools").json()
    assert listing["ok"] and any(t["id"] == "reason" and t["spends"] for t in listing["tools"])
    ran = c.post("/api/tools/reason-history", json={"params": {}}).json()
    assert ran["ok"] and ran["tool"] == "reason-history"
    pay = c.post("/api/tools/reason", json={"params": {"question": "Domanda?"}})
    assert pay.status_code == 400 and pay.json()["code"] == "CONFIRM_COST" and pay.json()["details"]["cap_usd"] == 0.5
    assert c.post("/api/tools/nope", json={"params": {}}).json()["code"] == "NO_SUCH_TOOL"


def test_api_only_the_owner_can_use_tools(client):
    c, _ = client
    c.headers.pop("Authorization")
    assert c.get("/api/tools").status_code == 401
    assert c.post("/api/tools/reason-history", json={"params": {}}).status_code == 401


def test_every_tool_text_has_an_italian_translation():
    import json
    from pathlib import Path

    known = (Path(__file__).resolve().parent.parent / "app" / "src" / "i18n-it.ts")
    if not known.is_file():
        pytest.skip("web app sources not present")
    text = known.read_text("utf-8")
    cat = tools.catalogue()
    texts = [g["title"] for g in cat["groups"]] + [x for s in cat["tools"] for x in (s["title"], s["help"], *(f["label"] for f in s["fields"]), *(f["hint"] for f in s["fields"] if f["hint"]))]
    missing = [t for t in dict.fromkeys(texts) if json.dumps(t, ensure_ascii=False) not in text]
    assert not missing, f"add to app/src/i18n-it.ts: {missing}"


def test_the_history_of_a_page_needs_the_backup_and_says_so(v):
    with pytest.raises(ValidationError) as exc:
        tools.run(v, "reflect-trace", {"path": "wiki/concepts/x.md"})
    assert exc.value.code == "NO_BACKUP"
