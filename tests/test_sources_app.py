"""Sources section of the app: list, add an Obsidian vault, check one source."""

import pytest

from mnemoloci import sessions, sources
from mnemoloci import vault as vault_mod
from mnemoloci.core.errors import MnemoError
from mnemoloci.core.lock import VaultLock


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


@pytest.fixture
def obsidian(tmp_path):
    root = tmp_path / "Il mio Vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "idee.md").write_text("# Idee\n\nIl lock protegge il vault.\n", "utf-8")
    (root / ".obsidian" / "app.json").write_text("{}", "utf-8")
    return root


def test_names_overview_and_check(v, obsidian):
    assert sources.free_name(v, str(obsidian)) == "il-mio-vault"
    sources.add_obsidian(v, str(obsidian), sources.free_name(v, str(obsidian)))
    assert sources.free_name(v, str(obsidian)) == "il-mio-vault-2"  # never collides
    [row] = sources.overview(v)["sources"]
    assert (row["name"], row["kind"], row["to_compile"], row["checked"]) == ("il-mio-vault", "notes", 0, None)
    out = sources.check(v, "il-mio-vault")
    assert (out["files"], out["to_compile"]) == (1, 1)
    [row] = sources.overview(v)["sources"]
    assert row["to_compile"] == 1 and row["checked"]
    (obsidian / "idee.md").unlink()  # gone from the vault: reported, and nothing to compile
    assert sources.check(v, "il-mio-vault")["to_compile"] == 0
    [row] = sources.overview(v)["sources"]
    assert row["to_compile"] == 0


def test_app_adds_an_obsidian_vault_only_from_the_pc(v, obsidian, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api, remote

    c = TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=True), base_url="http://127.0.0.1:8790")
    owner = {"Authorization": "Bearer launch"}
    assert c.post("/api/sources/obsidian", json={"path": "relativo"}, headers=owner).json()["code"] == "NOT_ABSOLUTE"
    plain = tmp_path / "cartella"
    plain.mkdir()
    assert c.post("/api/sources/obsidian", json={"path": str(plain)}, headers=owner).json()["code"] == "NOT_OBSIDIAN_VAULT"
    out = c.post("/api/sources/obsidian", json={"path": f'"{obsidian}"'}, headers=owner).json()
    assert out["ok"] and out["name"] == "il-mio-vault" and out["notes"] == 1
    assert c.post("/api/sources/il-mio-vault/check", json={}, headers=owner).json()["to_compile"] == 1
    assert [s["name"] for s in c.get("/api/sources", headers=owner).json()["sources"]] == ["il-mio-vault"]

    host = "mypc.tail1234.ts.net"
    remote.allow_hosts(v, [host])
    code = c.post("/api/remote/pair", json={}, headers=owner).json()["code"]
    token = c.post("/api/remote/claim", json={"code": code, "name": "p"}).json()["token"]
    phone = {"Authorization": f"Bearer {token}", "Host": host}
    assert c.get("/api/sources", headers=phone).json()["ok"]  # the phone may look
    assert c.post("/api/sources/obsidian", json={"path": str(obsidian)}, headers=phone).json()["code"] == "OWNER_ONLY"
    with pytest.raises(MnemoError):
        sources.check(v, "nessuna")


def test_docs_only_scope_reads_documents_first_and_code_on_request(v, tmp_path):
    project = tmp_path / "progetto"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("# Progetto\n\nCosa fa.\n", "utf-8")
    (project / "package.json").write_text('{"name": "p"}\n', "utf-8")
    (project / "src" / "app.ts").write_text("export const x = 1;\n", "utf-8")
    (project / "tsconfig.json").write_text("{}\n", "utf-8")
    sources.add_source(v, str(project), "progetto", "project", "docs")
    report = sources.scan(v, "progetto")["progetto"]
    assert sorted(c["rel"] for c in report["changed"]) == ["README.md", "package.json"]
    assert report["skipped"]["code-not-in-scope"] == 2
    assert sources.overview(v)["sources"][0]["scope"] == "docs"
    sources.set_scope(v, "progetto", "all")
    assert len(sources.scan(v, "progetto")["progetto"]["changed"]) == 4
    with pytest.raises(MnemoError):
        sources.set_scope(v, "progetto", "tutto")


def test_in_vault_sources_store_a_vault_relative_root_not_an_absolute_one(v):
    with VaultLock(v) as lock:
        sessions._ensure_source(v, lock)
    entry = sources.load_sources(v)["sources"][sessions.SOURCE_NAME]
    assert entry["root_rel"] == sessions.RAW_ROOT and "root" not in entry
    assert sources.resolve_root(v, entry) == v / sessions.RAW_ROOT
    assert sources.source_root(v, sessions.SOURCE_NAME) == v / sessions.RAW_ROOT


def test_in_vault_legacy_absolute_root_is_migrated_on_next_touch(v):
    """An entry written before the portable-path fix (an absolute `root`, no `root_rel`) is
    rewritten to the new format the next time its producer module runs -- self-healing, same
    idempotent pattern as backup.py's _ensure_gitignore()."""
    with VaultLock(v) as lock:
        state = sources.load_sources(v)
        state["sources"][sessions.SOURCE_NAME] = {
            "root": str((v / sessions.RAW_ROOT).resolve()), "kind": "sessions",
            "added": "2020-01-01T00:00:00", "in_vault": True,
        }
        vault_mod.save_state(v, lock, "sources.json", state)
        sessions._ensure_source(v, lock)
    entry = sources.load_sources(v)["sources"][sessions.SOURCE_NAME]
    assert entry["root_rel"] == sessions.RAW_ROOT and "root" not in entry
    assert entry["added"] == "2020-01-01T00:00:00"  # preserved across the migration


def test_portable_root_is_home_relative_when_possible(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sources.Path, "home", lambda: home.resolve())
    inside = home / "Progetti" / "foo"
    inside.mkdir(parents=True)
    assert sources._portable_root(inside) == "~/Progetti/foo"
    outside = tmp_path / "altrove"
    outside.mkdir()
    assert sources._portable_root(outside) == str(outside)


def test_add_source_stores_a_portable_root_and_resolve_root_expands_it_back(monkeypatch, tmp_path, v):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sources.Path, "home", lambda: home.resolve())
    project = home / "Progetti" / "app"
    project.mkdir(parents=True)
    result = sources.add_source(v, str(project), "app")
    assert result["root"] == "~/Progetti/app"
    entry = sources.load_sources(v)["sources"]["app"]
    assert entry["root"] == "~/Progetti/app"
    assert sources.resolve_root(v, entry) == project
    assert sources.source_root(v, "app") == project


def test_provenance_report_groups_by_kind_and_source_and_flags_unique_and_stale(v, tmp_path):
    from mnemoloci.core import txn as txn_mod
    from mnemoloci.core.hashing import sha256_file
    from mnemoloci.markdown import render_page

    proj_a, proj_b = tmp_path / "proj_a", tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / "a.md").write_text("Contenuto A\n", "utf-8")
    (proj_b / "b.md").write_text("Contenuto B\n", "utf-8")
    sources.add_source(v, str(proj_a), "proj-a")
    sources.add_source(v, str(proj_b), "proj-b")
    sha_a, sha_b = sha256_file(proj_a / "a.md"), sha256_file(proj_b / "b.md")

    def fm(title, keys):
        return {"title": title, "kind": "concept", "summary": f"{title}.", "aliases": [],
                "sources": keys, "confidence": 0.9, "provenanceState": "extracted",
                "contradictedBy": [], "updated": "2026-09-24", "language": "it"}

    pages = {
        "only-a": (fm("Only A", [{"key": "proj-a/a.md", "sha256": sha_a}]), "x ^[proj-a/a.md#L1-L1]"),
        "both": (fm("Both", [{"key": "proj-a/a.md", "sha256": sha_a}, {"key": "proj-b/b.md", "sha256": sha_b}]),
                 "x ^[proj-a/a.md#L1-L1] ^[proj-b/b.md#L1-L1]"),
        "stale": (fm("Stale", [{"key": "proj-a/a.md", "sha256": "0" * 64}]), "x ^[proj-a/a.md#L1-L1]"),
    }
    b = {"schema": txn_mod.BUNDLE_SCHEMA, "operation_id": "seed-provenance", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{slug}.md", "action": "create", "content": render_page(f, body)}
                    for slug, (f, body) in pages.items()]}
    txn_mod.apply(v, b, txn_mod.prepare(v, b).approval_sha256)

    report = sources.provenance_report(v)
    assert report["by_source"]["proj-a"] == ["wiki/concepts/both.md", "wiki/concepts/only-a.md", "wiki/concepts/stale.md"]
    assert report["by_source"]["proj-b"] == ["wiki/concepts/both.md"]
    assert report["by_kind"]["project"] == ["wiki/concepts/both.md", "wiki/concepts/only-a.md", "wiki/concepts/stale.md"]
    assert report["unique_to_source"]["proj-a"] == ["wiki/concepts/only-a.md", "wiki/concepts/stale.md"]
    assert "proj-b" not in report["unique_to_source"]  # "both" cites two sources: not unique to either
    assert report["unique_to_kind"]["project"] == ["wiki/concepts/both.md", "wiki/concepts/only-a.md", "wiki/concepts/stale.md"]
    assert report["stale_by_source"]["proj-a"] == ["wiki/concepts/stale.md"]
    assert "proj-b" not in report["stale_by_source"]


def test_excluded_subfolders_and_virtualenvs_are_skipped(v, tmp_path):
    project = tmp_path / "progetto2"
    (project / "vendor_copy" / "docs").mkdir(parents=True)
    (project / ".venv_broken_py312" / "Lib").mkdir(parents=True)
    (project / "jadx_out" / "sources").mkdir(parents=True)
    (project / "README.md").write_text("# Mio\n", "utf-8")
    (project / "vendor_copy" / "docs" / "manual.md").write_text("# Altrui\n", "utf-8")
    (project / ".venv_broken_py312" / "pyvenv.cfg").write_text("home = x\n", "utf-8")
    (project / ".venv_broken_py312" / "Lib" / "readme.txt").write_text("libreria\n", "utf-8")
    (project / "jadx_out" / "sources" / "A.java").write_text("class A {}\n", "utf-8")
    sources.add_source(v, str(project), "progetto2", "project")
    changed = sorted(c["rel"] for c in sources.scan(v, "progetto2")["progetto2"]["changed"])
    assert changed == ["README.md", "vendor_copy/docs/manual.md"]  # no virtualenv, no decompiled code
    sources.set_exclude(v, "progetto2", ["vendor_copy\\"])
    assert [c["rel"] for c in sources.scan(v, "progetto2")["progetto2"]["changed"]] == ["README.md"]
    assert sources.overview(v)["sources"][0]["exclude"] == ["vendor_copy"]
    with pytest.raises(MnemoError):
        sources.set_exclude(v, "progetto2", ["../fuori"])
    sources.set_exclude(v, "progetto2", ["vendor_copy"], add=False)
    assert len(sources.scan(v, "progetto2")["progetto2"]["changed"]) == 2
