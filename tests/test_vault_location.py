"""Which vault Mnemoloci uses: argument > MNEMOLOCI_VAULT > ~/.mnemoloci/vault.txt > ~/Mnemoloci, and the
`vault` command and the app's Vault section that change the registered one."""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient

from mnemoloci import api, cli, config
from mnemoloci import vault as vault_mod
from mnemoloci.core.errors import MnemoError

TOKEN = "t0ken"
PORT = 8767


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MNEMOLOCI_VAULT", raising=False)


def test_default_is_the_home_folder_not_a_fixed_drive(tmp_path):
    assert config.resolve_vault(None) == tmp_path / "home" / "Mnemoloci"
    assert config.vault_source() == "default"


def test_registered_vault_beats_the_default_and_the_environment_beats_it(tmp_path, monkeypatch):
    registered = tmp_path / "registered"
    config.record_vault(registered)
    assert config.resolve_vault(None) == registered.resolve() and config.vault_source() == "registered"
    monkeypatch.setenv("MNEMOLOCI_VAULT", str(tmp_path / "env"))
    assert config.resolve_vault(None) == tmp_path / "env" and config.vault_source() == "environment"
    assert config.resolve_vault(str(tmp_path / "arg")) == tmp_path / "arg" and config.vault_source(str(tmp_path / "arg")) == "argument"
    assert config.vault_info()["overridden"] is True


def test_first_init_registers_the_vault_and_later_ones_do_not(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    out = cli._init(first)
    assert Path(out["registered_vault"]) == first.resolve()
    out = cli._init(second)
    assert "registered_vault" not in out
    assert config.resolve_vault(None) == first.resolve()


def test_init_does_not_register_when_the_environment_chooses(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOLOCI_VAULT", str(tmp_path / "env"))
    out = cli._init(tmp_path / "env")
    assert "registered_vault" not in out and config.registered_vault() is None


def test_switching_needs_an_absolute_path_to_an_existing_vault(tmp_path):
    with pytest.raises(MnemoError) as exc:
        config.switch_vault("relative/path")
    assert exc.value.code == "NOT_ABSOLUTE"
    with pytest.raises(MnemoError) as exc:
        config.switch_vault(str(tmp_path / "nothing"))
    assert exc.value.code == "NOT_A_VAULT" and config.registered_vault() is None
    vault_mod.init(tmp_path / "real")
    result = config.switch_vault(str(tmp_path / "real"))
    assert result["restart_needed"] is True and Path(result["path"]) == (tmp_path / "real").resolve()


def test_the_command_line_shows_and_sets_the_vault(tmp_path, capsys):
    vault_mod.init(tmp_path / "v")
    assert cli.main(["vault", "set", str(tmp_path / "v")]) == 0
    capsys.readouterr()
    assert cli.main(["vault", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["source"] == "registered" and shown["initialized"] is True
    assert cli.main(["--vault", str(tmp_path / "other"), "vault", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "argument"


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    c = TestClient(api.create_app(root, TOKEN, {"port": PORT}), base_url=f"http://127.0.0.1:{PORT}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c, tmp_path


def test_the_app_reports_and_changes_the_vault(client):
    c, tmp_path = client
    got = c.get("/api/vault").json()
    assert got["ok"] and got["source"] == "default" and "overridden" in got
    vault_mod.init(tmp_path / "second")
    done = c.post("/api/vault", json={"path": str(tmp_path / "second")}).json()
    assert done["ok"] and done["restart_needed"] is True
    assert c.get("/api/vault").json()["registered"] == str((tmp_path / "second").resolve())
    bad = c.post("/api/vault", json={"path": str(tmp_path / "missing")}).json()
    assert not bad["ok"] and bad["code"] == "NOT_A_VAULT"


def test_the_vault_location_is_for_the_owner_only(client):
    c, _ = client
    c.headers.pop("Authorization")
    assert c.get("/api/vault").status_code == 401
    assert c.post("/api/vault", json={"path": "x"}).status_code == 401
