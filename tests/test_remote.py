import time

import pytest

from mnemoloci import remote, vault as vault_mod
from mnemoloci.core.errors import ConflictError, MnemoError, ValidationError

HOST = "mypc.tail1234.ts.net"


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def test_pairing_is_one_time_and_stores_only_hashes(v):
    code = remote.create_pairing_code(v)["code"]
    out = remote.claim(v, code.lower().replace("-", " "), "iPhone")  # forgiving about case and separators
    state = (v / ".mnemoloci/remote.json").read_text("utf-8")
    assert out["token"] not in state and code.replace("-", "") not in state
    assert remote.authenticate(v, out["token"])["name"] == "iPhone"
    with pytest.raises(ConflictError):
        remote.claim(v, code, "second")  # already used


def test_wrong_codes_burn_the_code(v):
    remote.create_pairing_code(v)
    for _ in range(remote.MAX_ATTEMPTS):
        with pytest.raises(ValidationError):
            remote.claim(v, "AAAA-AAAA", "x")
    with pytest.raises(ConflictError) as info:
        remote.claim(v, "AAAA-AAAA", "x")
    assert info.value.code == "NO_PAIRING_CODE"


def test_expired_code_is_refused(v, monkeypatch):
    code = remote.create_pairing_code(v)["code"]
    monkeypatch.setattr(time, "time", lambda: 10 ** 11)
    with pytest.raises(ConflictError):
        remote.claim(v, code, "x")


def test_revoked_device_is_locked_out_but_kept(v):
    token = remote.claim(v, remote.create_pairing_code(v)["code"], "iPhone")["token"]
    dev = remote.devices(v)["devices"][0]
    remote.revoke(v, dev["id"])
    assert remote.authenticate(v, token) is None
    assert remote.devices(v)["devices"][0]["revoked"]


def test_autostart_writes_and_removes_only_its_own_file(v, tmp_path, monkeypatch):
    if not remote.os.name == "nt":
        pytest.skip("Windows only")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    out = remote.autostart(v, True)
    launcher = remote._startup_file()
    assert launcher.is_file() and "remote serve" in launcher.read_text("utf-8")
    assert remote.autostart(v, False)["autostart"] is False and not launcher.exists()


def _client(v, remote_mode=True):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api

    return TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=remote_mode), base_url="http://127.0.0.1:8790")


def test_api_remote_access_rules(v):
    c = _client(v)
    remote_host = {"Host": HOST}
    token = c.post("/api/remote/claim", json={"code": "nope", "name": "x"}).json()
    assert token["code"] == "NO_PAIRING_CODE"  # public endpoint, but no code yet

    code = c.post("/api/remote/pair", json={}, headers={"Authorization": "Bearer launch"}).json()["code"]
    device_token = c.post("/api/remote/claim", json={"code": code, "name": "iPhone"}).json()["token"]
    auth = {"Authorization": f"Bearer {device_token}"}

    assert c.get("/api/status", headers={**auth, **remote_host}).status_code == 403  # host not allowed yet
    remote.allow_hosts(v, [HOST])
    me = c.get("/api/whoami", headers={**auth, **remote_host}).json()
    assert me["role"] == "device" and me["device"]["name"] == "iPhone"
    assert c.get("/api/status", headers={**auth, **remote_host}).json()["ok"]

    # the PC's launch token is only valid from the PC itself
    assert c.get("/api/status", headers={"Authorization": "Bearer launch", **remote_host}).status_code == 401
    # a phone cannot pair more devices or revoke
    assert c.post("/api/remote/pair", json={}, headers={**auth, **remote_host}).json()["code"] == "OWNER_ONLY"

    remote.allow_logins(v, ["owner@example.com"])
    assert c.get("/api/status", headers={**auth, **remote_host, "Tailscale-User-Login": "intruder@example.com"}).status_code == 403
    assert c.get("/api/status", headers={**auth, **remote_host, "Tailscale-User-Login": "Owner@Example.com"}).json()["ok"]
    # with a login allow-list, a remote request without the Tailscale header is refused (fail closed)
    assert c.get("/api/status", headers={**auth, **remote_host}).json()["code"] == "TAILSCALE_USER_NOT_ALLOWED"

    dev_id = remote.devices(v)["devices"][0]["id"]
    assert c.post(f"/api/remote/revoke/{dev_id}", json={}, headers={"Authorization": "Bearer launch"}).json()["ok"]
    login = {"Tailscale-User-Login": "owner@example.com"}
    assert c.get("/api/status", headers={**auth, **remote_host, **login}).status_code == 401


def test_device_scope_body_cap_and_numbers(v):
    c = _client(v)
    remote.allow_hosts(v, [HOST])
    code = c.post("/api/remote/pair", json={}, headers={"Authorization": "Bearer launch"}).json()["code"]
    auth = {"Authorization": f"Bearer {c.post('/api/remote/claim', json={'code': code, 'name': 'p'}).json()['token']}",
            "Host": HOST}
    dev_id = remote.devices(v)["devices"][0]["id"]
    assert remote.devices(v)["devices"][0]["scope"] == "capture"
    assert c.get("/api/whoami", headers=auth).json()["device"]["scope"] == "capture"

    # capture: reading and capturing work, changing goals does not
    assert c.post("/api/todos/add", json={"text": "comprare il latte"}, headers=auth).json()["ok"]
    denied = c.post("/api/goals/add", json={"text": "imparare Rust"}, headers=auth).json()
    assert denied["code"] == "DEVICE_SCOPE"
    remote.set_scope(v, dev_id, "full")
    assert c.post("/api/goals/add", json={"text": "imparare Rust"}, headers=auth).json()["ok"]
    with pytest.raises(MnemoError):
        remote.set_scope(v, dev_id, "root")

    big = b'{"text": "' + b"x" * (3 * 1024 * 1024) + b'"}'
    r = c.post("/api/todos/add", content=big, headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert c.get("/api/search?q=x&limit=abc", headers=auth).json()["code"] == "BAD_REQUEST"
    assert c.get("/api/search?q=x&limit=99999", headers=auth).json()["ok"]  # clamped, not an error


def test_local_app_has_no_remote_surface(v):
    c = _client(v, remote_mode=False)
    remote.allow_hosts(v, [HOST])
    assert c.get("/api/status", headers={"Authorization": "Bearer launch", "Host": HOST}).status_code == 403
    assert c.post("/api/remote/claim", json={"code": "x", "name": "y"}).status_code in (404, 405)
