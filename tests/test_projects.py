import os
import subprocess
import sys
from pathlib import Path

import pytest

from mnemoloci import hooks, projects, sources, vault as vault_mod
from mnemoloci.core.errors import ConflictError, MnemoError, ValidationError


@pytest.fixture(autouse=True)
def private_appdata(tmp_path, monkeypatch):
    # set_root rewrites the Startup launcher it created: never let a test reach the real one
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


@pytest.fixture
def setup(tmp_path):
    v = tmp_path / "vault"
    vault_mod.init(v)
    root = tmp_path / "Progetti"
    root.mkdir()
    return v, root


def test_root_is_required_and_validated(setup, tmp_path):
    v, root = setup
    with pytest.raises(MnemoError) as info:
        projects.new_project(v, "Prova")
    assert info.value.code == "NO_PROJECTS_ROOT"
    with pytest.raises(MnemoError):
        projects.set_root(v, str(tmp_path / "missing"))
    with pytest.raises(MnemoError):
        projects.set_root(v, str(v / "wiki"))
    out = projects.set_root(v, str(root))
    assert out["root"] == str(root.resolve()) and projects.get_root(v) == root.resolve()


def test_new_project_creates_folder_claude_md_git_and_source(setup):
    v, root = setup
    projects.set_root(v, str(root))
    out = projects.new_project(v, "App Ricette", description="App per salvare ricette dal telefono.")
    folder = root / "App Ricette"
    assert out["path"] == str(folder.resolve()) and out["next"] == f"/cd {folder.resolve()}"
    assert "App per salvare ricette" in (folder / "CLAUDE.md").read_text("utf-8")
    assert out["git_init"] is ((folder / ".git").is_dir())
    assert sources.load_sources(v)["sources"][out["source"]]["root"] == str(folder.resolve())
    listed = {p["name"]: p["source"] for p in projects.list_projects(v)["projects"]}
    assert listed["App Ricette"] == out["source"]


@pytest.mark.parametrize("name", ["", "a/b", "..", "CON", "nome.", "x" * 61, ".nascosto", "c:x"])
def test_bad_names_are_refused(setup, name):
    v, root = setup
    projects.set_root(v, str(root))
    with pytest.raises(ValidationError):
        projects.new_project(v, name)
    assert list(root.iterdir()) == []


def test_existing_folder_is_never_touched(setup):
    v, root = setup
    projects.set_root(v, str(root))
    (root / "Esistente").mkdir()
    (root / "Esistente" / "file.txt").write_text("mio", "utf-8")
    for name in ("Esistente", "esistente"):
        with pytest.raises(ConflictError):
            projects.new_project(v, name)
    assert (root / "Esistente" / "file.txt").read_text("utf-8") == "mio"


def test_session_start_in_root_points_to_new_project_skill(setup):
    v, root = setup
    projects.set_root(v, str(root))
    context = hooks.build_context(v, str(root))
    assert context and "mnemoloci-new-project" in context
    assert hooks.build_context(v, str(root.parent)) is None  # elsewhere: nothing to add on an empty vault


def test_engine_starts_from_a_folder_that_contains_a_mnemoloci_folder(tmp_path):
    # A folder that contains the source checkout `mnemoloci\`: without -P, `python -m` imports that
    # folder as an empty namespace package and every plugin hook and the MCP server fail.
    (tmp_path / "mnemoloci").mkdir()
    plain = subprocess.run([sys.executable, "-m", "mnemoloci", "--version"], cwd=tmp_path, capture_output=True, text=True)
    safe = subprocess.run([sys.executable, "-P", "-m", "mnemoloci", "--version"], cwd=tmp_path, capture_output=True, text=True)
    assert plain.returncode != 0
    assert safe.returncode == 0 and "mnemoloci" in safe.stdout
    plugin = Path(__file__).resolve().parents[1] / "plugin"
    launch = (plugin / "hooks" / "hooks.json").read_text("utf-8") + (plugin / ".mcp.json").read_text("utf-8")
    # the plugin goes through its launcher, which starts the engine with -P
    assert "python -m mnemoloci" not in launch and "${CLAUDE_PLUGIN_ROOT}/bin/mnemoloci-run" in launch
    assert '"-P", "$here/launch.py"' not in launch and '-P "$here/launch.py"' in (plugin / "bin" / "mnemoloci-run").read_text("utf-8")


def test_rc_autostart_marker(setup, tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows only")
    v, root = setup
    projects.set_root(v, str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(projects.shutil, "which", lambda name: r"C:\fake\claude.exe" if name == "claude" else None)
    out = projects.rc_autostart(v, True)
    launcher = projects._startup_file()
    text = launcher.read_text("utf-8")
    assert "remote-control" in text and str(root.resolve()) in text and projects.STARTUP_MARKER in text
    other = tmp_path / "Altri progetti"
    other.mkdir()
    assert projects.set_root(v, str(other))["autostart_updated"] is True
    assert str(other.resolve()) in launcher.read_text("utf-8")
    assert projects.rc_autostart(v, False)["autostart"] is False and not launcher.exists()
    assert projects.set_root(v, str(root))["autostart_updated"] is False  # nothing of ours to update


def test_api_projects_root_owner_sets_phone_only_reads(setup):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api, remote

    v, root = setup
    c = TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=True), base_url="http://127.0.0.1:8790")
    owner = {"Authorization": "Bearer launch"}
    assert c.get("/api/projects", headers=owner).json() == {"ok": True, "root": None, "projects": []}
    out = c.post("/api/projects/root", json={"root": str(root)}, headers=owner).json()
    assert out["ok"] and out["root"] == str(root.resolve()) and projects.get_root(v) == root.resolve()
    assert c.post("/api/projects/root", json={"root": "relativo"}, headers=owner).json()["code"] == "NOT_ABSOLUTE"
    (root / "Uno").mkdir()
    assert [p["name"] for p in c.get("/api/projects", headers=owner).json()["projects"]] == ["Uno"]

    host = "mypc.tail1234.ts.net"
    remote.allow_hosts(v, [host])
    code = remote.create_pairing_code(v)["code"]
    phone = {"Authorization": f"Bearer {remote.claim(v, code, 'iPhone')['token']}", "Host": host}
    assert c.get("/api/projects", headers=phone).json()["root"] == str(root.resolve())
    refused = c.post("/api/projects/root", json={"root": str(root.parent)}, headers=phone)
    assert refused.status_code == 403 and refused.json()["code"] == "OWNER_ONLY"
    assert projects.get_root(v) == root.resolve()


def test_root_must_be_absolute(setup):
    v, _ = setup
    for bad in ("", "Progetti", r"..\x"):
        with pytest.raises(MnemoError) as info:
            projects.set_root(v, bad)
        assert info.value.code == "NOT_ABSOLUTE"
