import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient

from mnemoloci import api, schedule, settings, vault as vault_mod
from mnemoloci.core.errors import MnemoError, ValidationError

TOKEN = "t0ken"
PORT = 8766


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


@pytest.fixture
def client(v):
    c = TestClient(api.create_app(v, TOKEN, {"port": PORT}), base_url=f"http://127.0.0.1:{PORT}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c, v


def test_get_returns_every_section_with_its_choices(v):
    got = settings.get(v)
    assert got["language"] == {"target": "it", "keep_original": True}
    assert got["review"]["low_confidence_threshold"] == 0.5 and "low-confidence" in got["review"]["choices"]
    assert "local-extraction" not in got["review"]["choices"] and "local-extraction" in got["review"]["always"]
    assert got["ingest"]["max_file_bytes"] == 512 * 1024
    assert got["reflection"]["every"] == 20 and got["reason"]["maxBudgetUsd"] == 0.5
    assert got["backup"]["enabled"] is False and got["instructions"]["text"] == ""


def test_language_and_instructions(v):
    settings.update(v, "language", {"target": "en", "keep_original": False})
    assert settings.get(v)["language"] == {"target": "en", "keep_original": False}
    settings.update(v, "instructions", {"text": "Tono asciutto. Niente elenchi puntati."})
    assert settings.get(v)["instructions"]["text"].startswith("Tono asciutto")
    with pytest.raises(MnemoError) as exc:
        settings.update(v, "language", {"target": "italiano!"})
    assert exc.value.code == "BAD_LANGUAGE"
    with pytest.raises(ValidationError):
        settings.update(v, "language", {"keep_original": "sì"})


def test_review_rules_are_validated_and_the_always_held_mode_is_kept(v):
    config = vault_mod.load_config(v)
    assert "local-extraction" in config["review"]["hold"]
    settings.update(v, "review", {"hold": ["contradicted"], "low_confidence_threshold": 0.7, "treat_missing_confidence_as": "ok"})
    saved = vault_mod.load_config(v)["review"]
    assert saved["hold"] == ["contradicted", "local-extraction"]
    assert saved["low_confidence_threshold"] == 0.7 and saved["treat_missing_confidence_as"] == "ok"
    for bad in ({"hold": ["inventato"]}, {"hold": "contradicted"}, {"low_confidence_threshold": 1.5},
                {"low_confidence_threshold": "alto"}, {"treat_missing_confidence_as": "forse"}, {"hold": ["local-extraction"]}):
        with pytest.raises(ValidationError):
            settings.update(v, "review", bad)


def test_ingest_reflection_and_reason_are_bounded(v):
    settings.update(v, "ingest", {"max_file_bytes": 2048})
    settings.update(v, "reflection", {"every": 0})
    settings.update(v, "reason", {"maxBudgetUsd": 1.256})
    got = settings.get(v)
    assert got["ingest"]["max_file_bytes"] == 2048 and got["reflection"]["every"] == 0 and got["reason"]["maxBudgetUsd"] == 1.26
    for section, bad in (("ingest", {"max_file_bytes": 10}), ("ingest", {}), ("reflection", {"every": -1}),
                         ("reflection", {"every": 9999}), ("reason", {"maxBudgetUsd": 0}), ("reason", {"maxBudgetUsd": 500})):
        with pytest.raises(ValidationError):
            settings.update(v, section, bad)


def test_the_reason_budget_setting_is_what_reason_uses(v):
    from mnemoloci import reason

    settings.update(v, "reason", {"maxBudgetUsd": 2})
    assert reason._budget(v, None) == 2.0


def test_backup_switches_need_a_started_backup_and_a_remote_for_push(v):
    with pytest.raises(ValidationError) as exc:
        settings.update(v, "backup", {"auto_commit": True})
    assert exc.value.code == "NO_BACKUP"
    settings.update(v, "backup", {"auto_commit": False, "auto_push": False})  # turning off is always allowed
    from mnemoloci import backup

    backup.init(v)
    settings.update(v, "backup", {"auto_commit": True})
    assert settings.get(v)["backup"]["auto_commit"] is True
    with pytest.raises(ValidationError) as exc:
        settings.update(v, "backup", {"auto_push": True})
    assert exc.value.code == "NO_REMOTE"


def test_schedule_validates_the_time_and_calls_the_scheduler(v, monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "enable", lambda vault, at: calls.append(("on", at)) or {})
    monkeypatch.setattr(schedule, "disable", lambda: calls.append(("off",)) or {})
    monkeypatch.setattr(schedule, "status", lambda vault: {"enabled": bool(calls) and calls[-1][0] == "on"})
    assert settings.update(v, "schedule", {"enabled": True, "time": "07:45"})["settings"] == {"enabled": True}
    assert settings.update(v, "schedule", {"enabled": False})["settings"] == {"enabled": False}
    assert calls == [("on", "07:45"), ("off",)]
    for bad in ({"enabled": True, "time": "25:00"}, {"enabled": "sì"}, {}):
        with pytest.raises(ValidationError):
            settings.update(v, "schedule", bad)


def test_unknown_section(v):
    with pytest.raises(ValidationError) as exc:
        settings.update(v, "segreti", {})
    assert exc.value.code == "BAD_SECTION"


# ---------- HTTP ----------

def test_api_reads_and_writes(client):
    c, v = client
    assert c.get("/api/settings").json()["review"]["low_confidence_threshold"] == 0.5
    ok = c.post("/api/settings/reflection", json={"every": 7}).json()
    assert ok["ok"] and ok["settings"]["every"] == 7
    bad = c.post("/api/settings/reflection", json={"every": "tanti"})
    assert bad.status_code == 400 and bad.json()["code"] == "BAD_SETTING"
    assert c.post("/api/settings/segreti", json={}).json()["code"] == "BAD_SECTION"


def test_api_schedule_status_is_a_separate_read(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(schedule, "status", lambda vault: {"enabled": False, "task": "x", "last_run": None})
    assert c.get("/api/settings/schedule").json()["enabled"] is False


def test_only_the_owner_may_change_settings(client):
    c, v = client
    from mnemoloci import remote

    c.headers.pop("Authorization")
    assert c.get("/api/settings").status_code == 401
    assert c.post("/api/settings/reflection", json={"every": 1}).status_code == 401
    assert vault_mod.load_config(v).get("reflection") is None


def test_api_serves_the_feature_catalogue(client):
    c, _ = client
    got = c.get("/api/features").json()
    assert got["ok"] and got["summary"]["total"] > 30 and any(f["id"] == "settings" for f in got["features"])
    c.headers.pop("Authorization")
    assert c.get("/api/features").status_code == 401
