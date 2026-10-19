import base64
import json
import threading
import urllib.parse
import urllib.request

import pytest

from mnemoloci import clips, connectors, vault as vault_mod
from mnemoloci.core.errors import MnemoError


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def _fake_browser_open(code: str = "test-code", error: str | None = None):
    """Simulates the user completing consent in a real browser: hits the local callback URL in a
    background thread (authorize()'s local server isn't servicing requests yet when
    webbrowser.open() is called -- it starts handling only right after, so this must not block)."""
    def opener(url: str) -> bool:
        redirect_uri = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
        query = {"error": error} if error else {"code": code}

        def hit():
            try:
                urllib.request.urlopen(f"{redirect_uri}?{urllib.parse.urlencode(query)}", timeout=5)
            except Exception:
                pass
        threading.Thread(target=hit, daemon=True).start()
        return True
    return opener


def _fake_token_request(access_token="acc-1", refresh_token="ref-1", expires_in=3600):
    def fake(spec, body):
        return {"access_token": access_token, "refresh_token": refresh_token, "expires_in": expires_in}
    return fake


def test_list_connectors_shows_all_providers_none_connected(v):
    out = connectors.list_connectors(v)["connectors"]
    names = {c["provider"] for c in out}
    assert names == {"google_drive", "github", "onedrive", "notion", "gmail"}
    assert all(c["implemented"] and c["connected"] is False for c in out)
    kinds = {c["provider"]: c["auth_kind"] for c in out}
    assert kinds == {"google_drive": "oauth", "onedrive": "oauth", "gmail": "oauth",
                     "github": "token", "notion": "token"}


def test_set_app_credentials_validates(v):
    with pytest.raises(MnemoError) as info:
        connectors.set_app_credentials(v, "not-a-provider", client_id="x", client_secret="y")
    assert info.value.code == "BAD_PROVIDER"
    with pytest.raises(MnemoError) as info:
        connectors.set_app_credentials(v, "google_drive", client_id="", client_secret="y")
    assert info.value.code == "BAD_CREDENTIALS"
    out = connectors.set_app_credentials(v, "google_drive", client_id="cid", client_secret="csecret")
    assert out == {"provider": "google_drive", "credentials_set": True}


def test_authorize_rejects_wrong_auth_kind_and_missing_credentials(v):
    with pytest.raises(MnemoError) as info:
        connectors.authorize(v, "github")  # github is a "token" provider, not "oauth"
    assert info.value.code == "WRONG_AUTH_KIND"
    with pytest.raises(MnemoError) as info:
        connectors.authorize(v, "google_drive")  # no set_app_credentials yet
    assert info.value.code == "NO_CREDENTIALS"


def test_set_app_credentials_rejects_token_providers_and_vice_versa(v):
    with pytest.raises(MnemoError) as info:
        connectors.set_app_credentials(v, "github", client_id="x", client_secret="y")
    assert info.value.code == "WRONG_AUTH_KIND"
    with pytest.raises(MnemoError) as info:
        connectors.set_token(v, "google_drive", "some-token")
    assert info.value.code == "WRONG_AUTH_KIND"


def test_authorize_end_to_end_stores_tokens(v, monkeypatch):
    connectors.set_app_credentials(v, "google_drive", client_id="cid", client_secret="csecret")
    monkeypatch.setattr(connectors.webbrowser, "open", _fake_browser_open())
    monkeypatch.setattr(connectors, "_token_request", _fake_token_request())

    out = connectors.authorize(v, "google_drive")
    assert out == {"provider": "google_drive", "connected": True}
    state = connectors._state(v)["google_drive"]
    assert state["refresh_token"] == "ref-1" and state["access_token"] == "acc-1"
    assert connectors.list_connectors(v)["connectors"][0]["connected"] is True


def test_authorize_reports_denied_consent(v, monkeypatch):
    connectors.set_app_credentials(v, "google_drive", client_id="cid", client_secret="csecret")
    monkeypatch.setattr(connectors.webbrowser, "open", _fake_browser_open(error="access_denied"))

    with pytest.raises(MnemoError) as info:
        connectors.authorize(v, "google_drive")
    assert info.value.code == "OAUTH_DENIED"


def test_authorize_times_out_cleanly_if_the_browser_never_responds(v, monkeypatch):
    connectors.set_app_credentials(v, "google_drive", client_id="cid", client_secret="csecret")
    monkeypatch.setattr(connectors, "AUTH_TIMEOUT", 1)  # keep the test fast
    monkeypatch.setattr(connectors.webbrowser, "open", lambda url: True)  # nobody ever calls back

    with pytest.raises(MnemoError) as info:
        connectors.authorize(v, "google_drive")
    assert info.value.code == "OAUTH_TIMEOUT"


def test_set_folder_requires_connection_first(v):
    with pytest.raises(MnemoError) as info:
        connectors.set_folder(v, "google_drive", "folder123")
    assert info.value.code == "NOT_CONNECTED"


def _connect(v, monkeypatch):
    connectors.set_app_credentials(v, "google_drive", client_id="cid", client_secret="csecret")
    monkeypatch.setattr(connectors.webbrowser, "open", _fake_browser_open())
    monkeypatch.setattr(connectors, "_token_request", _fake_token_request())
    connectors.authorize(v, "google_drive")


def test_sync_requires_connection_and_folder(v, monkeypatch):
    with pytest.raises(MnemoError) as info:
        connectors.sync(v, "google_drive")
    assert info.value.code == "NOT_CONNECTED"
    _connect(v, monkeypatch)
    with pytest.raises(MnemoError) as info:
        connectors.sync(v, "google_drive")
    assert info.value.code == "NO_FOLDER"


def test_sync_google_drive_imports_text_skips_binary_never_bypasses_review(v, monkeypatch):
    _connect(v, monkeypatch)
    connectors.set_folder(v, "google_drive", "folder123", "Appunti")

    calls = []

    def fake_drive_get(vault, path, params):
        calls.append((path, params))
        if path == "/files":
            body = json.dumps({"files": [
                {"id": "f1", "name": "note.txt", "mimeType": "text/plain"},
                {"id": "f2", "name": "sheet", "mimeType": "application/vnd.google-apps.spreadsheet"},
                {"id": "f3", "name": "doc", "mimeType": connectors.GOOGLE_DOC_MIME},
            ]}).encode("utf-8")
            return body, "application/json"
        if path == "/files/f1":
            return b"Testo semplice da un file .txt.", "text/plain"
        if path == "/files/f3/export":
            return "Contenuto del Google Doc.".encode("utf-8"), "text/plain"
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(connectors, "_drive_get", fake_drive_get)
    added = []
    monkeypatch.setattr(clips, "add_text", lambda vault, text, *, title, note=None, mine=False, url=None:
                        added.append({"title": title, "mine": mine, "text": text}) or {"path": f"raw/clips/{title}.md"})

    result = connectors.sync(v, "google_drive")
    assert result["imported"] == ["note.txt", "doc"]
    assert result["skipped"] == ["sheet"]
    assert all(a["mine"] is False for a in added)  # never bypasses the review queue
    assert connectors._state(v)["google_drive"]["last_sync"]


def test_sync_all_isolates_provider_failures(v, monkeypatch):
    _connect(v, monkeypatch)
    connectors.set_folder(v, "google_drive", "folder123")

    def failing_drive_get(vault, path, params):
        raise MnemoError("GOOGLE_DRIVE_FAILED", "boom")

    monkeypatch.setattr(connectors, "_drive_get", failing_drive_get)
    out = connectors.sync_all(v)
    assert "google_drive" in out and "error" in out["google_drive"]
    assert "GOOGLE_DRIVE_FAILED" in out["google_drive"]["error"]


def test_sync_all_skips_disabled_and_unconnected_providers(v):
    assert connectors.sync_all(v) == {}  # nothing connected yet


def test_disable_removes_credentials_but_not_imported_files(v, monkeypatch):
    _connect(v, monkeypatch)
    connectors.set_folder(v, "google_drive", "folder123")
    out = connectors.disable(v, "google_drive")
    assert out == {"provider": "google_drive", "disabled": True}
    assert connectors.list_connectors(v)["connectors"][0]["connected"] is False
    with pytest.raises(MnemoError) as info:
        connectors.sync(v, "google_drive")
    assert info.value.code == "NOT_CONNECTED"


def test_ensure_access_token_refreshes_when_expired(v, monkeypatch):
    _connect(v, monkeypatch)
    with connectors.VaultLock(v) as lock:
        state = connectors._state(v)
        state["google_drive"]["token_expiry"] = 0  # force it stale
        connectors.save_state(v, lock, "connectors.json", state)

    monkeypatch.setattr(connectors, "_token_request", _fake_token_request(access_token="acc-2", refresh_token=None))
    token = connectors._ensure_access_token(v, "google_drive")
    assert token == "acc-2"
    saved = connectors._state(v)["google_drive"]
    assert saved["access_token"] == "acc-2"
    assert saved["refresh_token"] == "ref-1"  # kept from the original consent, not overwritten with None


def test_ensure_access_token_reuses_unexpired_token_without_a_network_call(v, monkeypatch):
    _connect(v, monkeypatch)
    monkeypatch.setattr(connectors, "_token_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not refresh")))
    assert connectors._ensure_access_token(v, "google_drive") == "acc-1"


def _connect_oauth(v, monkeypatch, provider):
    connectors.set_app_credentials(v, provider, client_id="cid", client_secret="csecret")
    monkeypatch.setattr(connectors.webbrowser, "open", _fake_browser_open())
    monkeypatch.setattr(connectors, "_token_request", _fake_token_request())
    connectors.authorize(v, provider)


# ---------- OneDrive (oauth, PKCE) ----------

def test_authorize_onedrive_sends_pkce_params_and_localhost_redirect(v, monkeypatch):
    connectors.set_app_credentials(v, "onedrive", client_id="cid", client_secret="csecret")
    seen = {}

    def fake_open(url):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        seen["redirect_uri"] = params["redirect_uri"][0]
        seen["has_challenge"] = "code_challenge" in params and params["code_challenge_method"][0] == "S256"
        return _fake_browser_open()(url)

    token_bodies = []

    def fake_token_request(spec, body):
        token_bodies.append(urllib.parse.parse_qs(body.decode("utf-8")))
        return {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}

    monkeypatch.setattr(connectors.webbrowser, "open", fake_open)
    monkeypatch.setattr(connectors, "_token_request", fake_token_request)

    connectors.authorize(v, "onedrive")
    assert seen["redirect_uri"].startswith("http://localhost:")
    assert seen["has_challenge"] is True
    assert "code_verifier" in token_bodies[0]  # PKCE verifier sent back on the token exchange


def test_sync_onedrive_imports_text_skips_folders_and_binaries(v, monkeypatch):
    _connect_oauth(v, monkeypatch, "onedrive")
    connectors.set_folder(v, "onedrive", "folder123")

    def fake_onedrive_get(vault, path):
        if path == "/me/drive/items/folder123/children":
            body = json.dumps({"value": [
                {"id": "i1", "name": "note.txt", "file": {"mimeType": "text/plain"}, "lastModifiedDateTime": "2026-01-01T00:00:00Z"},
                {"id": "i2", "name": "sub", "folder": {}},
                {"id": "i3", "name": "photo.png", "file": {"mimeType": "image/png"}, "lastModifiedDateTime": "2026-01-01T00:00:00Z"},
            ]}).encode("utf-8")
            return body, "application/json"
        if path == "/me/drive/items/i1/content":
            return b"Contenuto OneDrive.", "text/plain"
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(connectors, "_onedrive_get", fake_onedrive_get)
    added = []
    monkeypatch.setattr(clips, "add_text", lambda vault, text, *, title, note=None, mine=False, url=None:
                        added.append({"title": title, "mine": mine}) or {"path": f"raw/clips/{title}.md"})

    result = connectors.sync(v, "onedrive")
    assert result["imported"] == ["note.txt"]
    assert result["skipped"] == ["photo.png"]
    assert all(a["mine"] is False for a in added)


# ---------- GitHub (token, sha diffing) ----------

def test_github_set_token_connects_without_a_browser_flow(v):
    out = connectors.set_token(v, "github", "ghp_test123")
    assert out == {"provider": "github", "connected": True}
    listed = connectors.list_connectors(v)["connectors"]
    assert next(c for c in listed if c["provider"] == "github")["connected"] is True


def test_github_set_token_validates(v):
    with pytest.raises(MnemoError) as info:
        connectors.set_token(v, "github", "")
    assert info.value.code == "BAD_TOKEN"
    with pytest.raises(MnemoError) as info:
        connectors.set_token(v, "not-a-provider", "x")
    assert info.value.code == "BAD_PROVIDER"


def test_sync_github_imports_new_files_and_skips_unchanged_ones_next_time(v, monkeypatch):
    connectors.set_token(v, "github", "ghp_test123")
    connectors.set_folder(v, "github", "owner/repo/docs")

    calls = []

    def fake_github_get(vault, path):
        calls.append(path)
        if path == "/repos/owner/repo/contents/docs":
            return [{"type": "file", "name": "readme.md", "path": "docs/readme.md", "sha": "sha1"}]
        if path == "/repos/owner/repo/contents/docs/readme.md":
            return {"encoding": "base64", "content": base64.b64encode(b"Contenuto del repo.").decode("ascii")}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(connectors, "_github_get", fake_github_get)
    added = []
    monkeypatch.setattr(clips, "add_text", lambda vault, text, *, title, note=None, mine=False, url=None:
                        added.append(title) or {"path": f"raw/clips/{title}.md"})

    result = connectors.sync(v, "github")
    assert result["imported"] == ["docs/readme.md"]
    assert added == ["readme.md"]

    # second sync, same sha -> nothing re-imported (only the listing call happens, not the file fetch)
    calls.clear()
    result2 = connectors.sync(v, "github")
    assert result2["imported"] == [] and calls == ["/repos/owner/repo/contents/docs"]


def test_sync_github_rejects_bad_folder_format(v, monkeypatch):
    connectors.set_token(v, "github", "ghp_test123")
    connectors.set_folder(v, "github", "not-owner-slash-repo")
    with pytest.raises(MnemoError) as info:
        connectors.sync(v, "github")
    assert info.value.code == "BAD_FOLDER"


# ---------- Notion (token, recursive blocks) ----------

def test_sync_notion_flattens_blocks_to_text(v, monkeypatch):
    connectors.set_token(v, "notion", "secret_test")
    connectors.set_folder(v, "notion", "db123")

    def fake_notion_request(vault, method, path, body=None):
        if path == "/databases/db123/query":
            return {"results": [{"id": "page1", "properties": {"Name": {"type": "title",
                    "title": [{"plain_text": "Appunto"}]}}}]}
        if path.startswith("/blocks/page1/children"):
            return {"results": [
                {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Titolo"}]}, "has_children": False},
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Un paragrafo."}]}, "has_children": False},
            ], "has_more": False}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(connectors, "_notion_request", fake_notion_request)
    added = []
    monkeypatch.setattr(clips, "add_text", lambda vault, text, *, title, note=None, mine=False, url=None:
                        added.append({"title": title, "text": text}) or {"path": f"raw/clips/{title}.md"})

    result = connectors.sync(v, "notion")
    assert result["imported"] == ["Appunto"]
    assert added[0]["text"] == "# Titolo\nUn paragrafo."


# ---------- Gmail (oauth, reuses Google's flow, MIME parsing) ----------

def test_sync_gmail_extracts_plain_text_from_multipart(v, monkeypatch):
    _connect_oauth(v, monkeypatch, "gmail")
    connectors.set_folder(v, "gmail", "label:appunti")
    plain_text_b64 = base64.urlsafe_b64encode(b"Testo semplice dell email.").decode("ascii").rstrip("=")

    def fake_gmail_get(vault, path, params=None):
        if path == "/users/me/messages":
            assert params["q"] == "label:appunti"
            return {"messages": [{"id": "m1"}]}
        if path == "/users/me/messages/m1":
            return {"payload": {"headers": [{"name": "Subject", "value": "Oggetto test"}],
                    "mimeType": "multipart/alternative", "parts": [
                        {"mimeType": "text/html", "body": {"data": "aGVsbG8="}},
                        {"mimeType": "text/plain", "body": {"data": plain_text_b64}},
                    ]}}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(connectors, "_gmail_get", fake_gmail_get)
    added = []
    monkeypatch.setattr(clips, "add_text", lambda vault, text, *, title, note=None, mine=False, url=None:
                        added.append({"title": title, "text": text}) or {"path": f"raw/clips/{title}.md"})

    result = connectors.sync(v, "gmail")
    assert result["imported"] == ["Oggetto test"]
    assert added[0]["text"] == "Testo semplice dell email."


def test_gmail_query_appends_after_timestamp_from_last_sync(v):
    cfg = {"folder_id": "label:appunti", "last_sync": "2026-01-15T10:00:00Z"}
    query = connectors._gmail_query(cfg)
    assert query.startswith("label:appunti after:")
