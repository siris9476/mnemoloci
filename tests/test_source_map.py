"""The Fonti page and `source show`: what each folder of a source contributes, and the user's
choices (exclude, force in, documents only), shared by the app and Claude."""

import pytest

from mnemoloci import source_map, sources
from mnemoloci import vault as vault_mod
from mnemoloci.core.errors import MnemoError


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "App"
    for folder in ("docs", "src", "node_modules/lib", "vendor", "tools/.venv_x", "copia_altrui/docs"):
        (p / folder).mkdir(parents=True)
    (p / "README.md").write_text("# App\n", "utf-8")
    (p / "package.json").write_text('{"name": "app"}\n', "utf-8")
    (p / "docs" / "architettura.md").write_text("# Architettura\n", "utf-8")
    (p / "src" / "main.ts").write_text("export {}\n", "utf-8")
    (p / "node_modules" / "lib" / "README.md").write_text("# lib\n", "utf-8")
    (p / "vendor" / "note-mie.md").write_text("# Note mie\n", "utf-8")  # the user's, despite the name
    (p / "tools" / ".venv_x" / "pyvenv.cfg").write_text("home = x\n", "utf-8")
    (p / "copia_altrui" / "docs" / "manuale.md").write_text("# Manuale\n", "utf-8")
    (p / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def _folders(t):
    return {f["name"]: f for f in t["folders"]}


def test_tree_says_what_each_folder_contributes(v, project):
    source_map.add_projects(v, str(project), docs_only=True)
    t = source_map.tree(v, "app")
    f = _folders(t)
    assert f["docs"]["status"] == "included" and f["docs"]["read"]["docs"] == 1
    assert f["src"]["status"] == "included" and f["src"]["read_total"] == 0 and f["src"]["skipped"] == {"code-not-in-scope": 1}
    assert (f["node_modules"]["status"], f["node_modules"]["reason"]) == ("auto", "excluded-dir")
    assert (f["vendor"]["status"], f["vendor"]["selected"]) == ("auto", False)
    files = {x["name"]: x for x in t["files"]}
    assert files["README.md"]["read"] and files["README.md"]["kind"] == "docs"
    assert files["logo.png"]["reason"] == "unsupported-type"
    assert t["totals"]["read_total"] == 4  # README, package.json, architettura.md, copia_altrui/docs/manuale.md
    inner = _folders(source_map.tree(v, "app", "tools"))
    assert (inner[".venv_x"]["status"], inner[".venv_x"]["reason"]) == ("auto", "virtualenv")
    with pytest.raises(MnemoError):
        source_map.tree(v, "app", "../fuori")


def test_ticking_and_unticking_folders(v, project):
    source_map.add_projects(v, str(project), docs_only=True)
    source_map.set_folder(v, "app", "copia_altrui", False)  # not mine
    source_map.set_folder(v, "app", "vendor", True)         # mine, despite the name
    f = _folders(source_map.tree(v, "app", fresh=True))
    assert (f["copia_altrui"]["status"], f["copia_altrui"]["selected"]) == ("excluded", False)
    assert (f["vendor"]["status"], f["vendor"]["selected"]) == ("forced", True)
    read = sorted(c["rel"] for c in sources.scan(v, "app")["app"]["changed"])
    assert read == ["README.md", "docs/architettura.md", "package.json", "vendor/note-mie.md"]
    # the most specific choice wins: an excluded parent, a sub-folder ticked back in
    source_map.set_folder(v, "app", "copia_altrui/docs", True)
    assert "copia_altrui/docs/manuale.md" in [c["rel"] for c in sources.scan(v, "app")["app"]["changed"]]
    # ticking the parent again removes its exclusion (an inner "read" choice stays, harmless)
    source_map.set_folder(v, "app", "copia_altrui", True)
    entry = sources.load_sources(v)["sources"]["app"]
    assert "copia_altrui" not in entry.get("exclude", [])
    assert "copia_altrui/docs/manuale.md" in [c["rel"] for c in sources.scan(v, "app")["app"]["changed"]]
    shown = source_map.show(v, "app")
    assert shown["scope"] == "docs" and "vendor" in shown["include"]


def test_add_every_subfolder_of_a_parent(v, tmp_path, project):
    (tmp_path / "Vuota").mkdir()
    out = source_map.add_projects(v, str(tmp_path), children=True)
    names = [a["name"] for a in out["added"]]
    assert "app" in names and all(a["scope"] == "docs" for a in out["added"])
    assert {"folder": str(tmp_path / "Vuota"), "why": "empty"} in out["skipped"]
    assert {"folder": str(v), "why": "the vault itself"} in out["skipped"]
    again = source_map.add_projects(v, str(tmp_path), children=True)
    assert again["added"] == [] and any(s["why"] == "already registered" for s in again["skipped"])
    with pytest.raises(MnemoError):
        source_map.add_projects(v, "relativo")


def test_app_routes_for_the_fonti_page(v, project):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api, remote

    c = TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=True), base_url="http://127.0.0.1:8790")
    owner = {"Authorization": "Bearer launch"}
    out = c.post("/api/sources/projects", json={"path": str(project), "docs_only": True}, headers=owner).json()
    assert out["ok"] and out["added"][0]["name"] == "app"
    tree = c.get("/api/sources/app/tree", headers=owner).json()
    assert tree["ok"] and {f["name"] for f in tree["folders"]} >= {"docs", "src", "vendor"}
    assert c.post("/api/sources/app/folder", json={"folder": "src", "selected": False}, headers=owner).json()["exclude"] == ["src"]
    assert c.post("/api/sources/app/scope", json={"scope": "all"}, headers=owner).json()["scope"] == "all"
    assert c.get("/api/sources/app/tree?path=docs", headers=owner).json()["path"] == "docs"

    host = "mypc.tail1234.ts.net"
    remote.allow_hosts(v, [host])
    code = c.post("/api/remote/pair", json={}, headers=owner).json()["code"]
    token = c.post("/api/remote/claim", json={"code": code, "name": "p"}).json()["token"]
    phone = {"Authorization": f"Bearer {token}", "Host": host}
    assert c.get("/api/sources/app/tree", headers=phone).json()["ok"]  # the phone may look
    assert c.post("/api/sources/app/folder", json={"folder": "docs", "selected": False},
                  headers=phone).json()["code"] == "OWNER_ONLY"
