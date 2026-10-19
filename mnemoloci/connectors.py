"""Push-style ingest via periodic OAuth sync -- the practical equivalent, for a program that runs
on the user's own PC with no public server, of "real-time connectors"
(Google Drive, Gmail, Notion, OneDrive, GitHub). Those services can push notifications only to an
always-on cloud service with a public address; Mnemoloci
instead authenticates once with each service's OAuth "installed app" flow (a redirect to a random
localhost port is Google's own documented, supported flow for the "Desktop app" client type -- no
public server, no fixed redirect URI to pre-register) and then checks periodically -- from the
daily scheduled job (schedule.py) or a manual "Sincronizza ora" -- whether the connected folder
changed.

All five providers from the original comparison (Google Drive, GitHub, OneDrive, Notion, Gmail)
are implemented. Two different `auth_kind`s, picked per provider based on what's actually simplest
and correct for a *personal, single-user* deployment (not a multi-tenant SaaS, where a full OAuth
App flow would be the only option):
- `"oauth"` (google_drive, onedrive, gmail): the full "installed app" consent dance in `authorize()`
  below -- needed because these providers have no simpler personal-access-token equivalent for
  their file/mail APIs.
- `"token"` (github, notion): a single pasted token is the standard, documented way for a personal
  script to access one's own data on these two specifically (a GitHub fine-grained PAT, a Notion
  "Internal Integration" token) -- no browser flow, no client_id/secret, nothing to register beyond
  generating the token itself.

Nothing here ever calls an LLM: a synced file/page/message lands exactly like any other external
clip (secret redaction, prompt-injection scan, always held for review, mine=False, never a bypass)
via clips.add_text() -- the same module every other ingest path already uses.
Understanding/extracting it is still either a live Claude Code session or an explicit
`compile extract-local` run, never automatic."""

import base64
import hashlib
import http.server
import json
import secrets
import socketserver
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from .core.errors import MnemoError
from .core.lock import VaultLock
from .vault import load_state, require_vault, save_state

CONNECTORS_SCHEMA = "mnemoloci.connectors.v1"
AUTH_TIMEOUT = 300  # seconds to wait for the user to finish the browser consent
HTTP_TIMEOUT = 30

PROVIDERS: dict[str, dict[str, Any]] = {
    "google_drive": {
        "display_name": "Google Drive", "auth_kind": "oauth",
        "auth_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/drive.readonly",
        "implemented": True,
    },
    "onedrive": {
        "display_name": "OneDrive", "auth_kind": "oauth",
        # "common" (not "consumers"): works for both personal Microsoft accounts and work/school ones
        "auth_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "offline_access Files.Read",
        # required for Microsoft's public-client registration; the Azure Portal UI only accepts a
        # bare "http://localhost" redirect URI (not "127.0.0.1"), but ignores the actual port at
        # match time (RFC 8252) -- so a random port still works, just under that hostname
        "pkce": True, "redirect_host": "localhost",
        "implemented": True,
    },
    "gmail": {
        "display_name": "Gmail", "auth_kind": "oauth",
        "auth_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "implemented": True,
    },
    "github": {"display_name": "GitHub", "auth_kind": "token", "implemented": True},
    "notion": {"display_name": "Notion", "auth_kind": "token", "implemented": True},
}


def _state(vault: Path) -> dict[str, Any]:
    return load_state(vault, "connectors.json", CONNECTORS_SCHEMA, {})


def _provider(name: str) -> dict[str, Any]:
    spec = PROVIDERS.get(name)
    if spec is None:
        raise MnemoError("BAD_PROVIDER", f"provider must be one of {sorted(PROVIDERS)}")
    return spec


def _rfc3339_utc_now() -> str:
    """Google Drive's `modifiedTime > '...'` filter needs strict RFC3339 -- Python's %z strftime
    produces "+0200" (no colon), which Drive rejects; a plain UTC "Z" timestamp always parses."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connected(spec: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("token")) if spec["auth_kind"] == "token" else bool(cfg.get("refresh_token"))


def list_connectors(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    state = _state(vault)
    out = []
    for name, spec in PROVIDERS.items():
        cfg = state.get(name) or {}
        has_credentials = bool(cfg.get("token")) if spec["auth_kind"] == "token" else bool(cfg.get("client_id"))
        out.append({
            "provider": name, "display_name": spec["display_name"], "implemented": spec["implemented"],
            "auth_kind": spec["auth_kind"], "has_credentials": has_credentials, "connected": _connected(spec, cfg),
            "folder_id": cfg.get("folder_id"), "folder_name": cfg.get("folder_name"),
            "last_sync": cfg.get("last_sync"), "enabled": bool(cfg.get("enabled", True)),
        })
    return {"connectors": out}


def set_app_credentials(vault: Path, provider: str, *, client_id: str, client_secret: str) -> dict[str, Any]:
    """The OAuth app's own credentials, registered by the user on the provider's developer
    console (e.g. Google Cloud Console) -- Mnemoloci cannot create these itself. Only for
    `auth_kind: "oauth"` providers; a "token" provider (github, notion) uses `set_token` instead."""
    spec = _provider(provider)
    if spec["auth_kind"] != "oauth":
        raise MnemoError("WRONG_AUTH_KIND", f"{spec['display_name']} uses a token, not OAuth credentials: use set_token")
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not client_id or not client_secret:
        raise MnemoError("BAD_CREDENTIALS", "client_id and client_secret are both required")
    with VaultLock(vault) as lock:
        state = _state(vault)
        cfg = state.setdefault(provider, {})
        cfg["client_id"], cfg["client_secret"] = client_id, client_secret
        save_state(vault, lock, "connectors.json", state)
    return {"provider": provider, "credentials_set": True}


def set_token(vault: Path, provider: str, token: str) -> dict[str, Any]:
    """A personal-access-token-style credential (a GitHub fine-grained PAT, a Notion Internal
    Integration token) -- saving it IS connecting, no separate browser step. Only for
    `auth_kind: "token"` providers."""
    spec = _provider(provider)
    if spec["auth_kind"] != "token":
        raise MnemoError("WRONG_AUTH_KIND", f"{spec['display_name']} uses OAuth, not a token: use set_app_credentials + connect")
    token = (token or "").strip()
    if not token:
        raise MnemoError("BAD_TOKEN", "the token is empty")
    with VaultLock(vault) as lock:
        state = _state(vault)
        state.setdefault(provider, {})["token"] = token
        save_state(vault, lock, "connectors.json", state)
    return {"provider": provider, "connected": True}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.oauth_result = {"code": (params.get("code") or [None])[0],
                                    "error": (params.get("error") or [None])[0]}
        ok = self.server.oauth_result["code"] and not self.server.oauth_result["error"]
        message = ("Connected. You can close this tab and go back to Mnemoloci." if ok
                  else "Something went wrong. Go back to Mnemoloci and try again.")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence default stderr logging
        pass


def _token_request(spec: dict[str, Any], body: bytes) -> dict[str, Any]:
    req = urllib.request.Request(spec["token_endpoint"], data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("OAUTH_TOKEN_FAILED", f"token exchange failed ({exc.code}): {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("OAUTH_TOKEN_FAILED", f"cannot reach {spec['token_endpoint']}: {exc}") from exc


def _pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for the OAuth2 PKCE extension -- Microsoft requires it for
    public clients; Google accepts it too, so it would be harmless to send everywhere, but only
    providers that ask for it (`spec["pkce"]`) get the extra round-trip parameters."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize(vault: Path, provider: str) -> dict[str, Any]:
    """One-time consent flow: open the browser, catch the redirect on a temporary local server (a
    random port -- Google's and Microsoft's own documented "loopback" flows need no pre-registered
    fixed URI, though Microsoft's redirect URI must be registered as bare "http://localhost", not
    "127.0.0.1"), exchange the code for tokens. Blocking: run this in a background thread/threadpool
    when calling it from a server handler, since it waits on the user to act in the browser."""
    require_vault(vault)
    spec = _provider(provider)
    if not spec["implemented"]:
        raise MnemoError("NOT_IMPLEMENTED", f"{spec['display_name']} cannot be connected yet")
    if spec["auth_kind"] != "oauth":
        raise MnemoError("WRONG_AUTH_KIND", f"{spec['display_name']} uses a token: use set_token, not connect")
    cfg = (_state(vault).get(provider) or {})
    client_id, client_secret = cfg.get("client_id"), cfg.get("client_secret")
    if not client_id:
        raise MnemoError("NO_CREDENTIALS", f"first register the OAuth credentials for {spec['display_name']}")

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _CallbackHandler)
    httpd.timeout = AUTH_TIMEOUT
    httpd.oauth_result = None  # type: ignore[attr-defined]
    port = httpd.server_address[1]
    redirect_uri = f"http://{spec.get('redirect_host', '127.0.0.1')}:{port}/callback"
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": spec["scope"], "access_type": "offline", "prompt": "consent"}
    verifier = None
    if spec.get("pkce"):
        verifier, challenge = _pkce_pair()
        params.update(code_challenge=challenge, code_challenge_method="S256")
    auth_url = spec["auth_endpoint"] + "?" + urllib.parse.urlencode(params)
    try:
        webbrowser.open(auth_url)
        httpd.handle_request()
    finally:
        httpd.server_close()
    result = httpd.oauth_result  # type: ignore[attr-defined]
    if result is None:
        raise MnemoError("OAUTH_TIMEOUT", f"nessuna risposta dal browser entro {AUTH_TIMEOUT}s")
    if result.get("error") or not result.get("code"):
        raise MnemoError("OAUTH_DENIED", f"autorizzazione non concessa: {result.get('error') or 'nessun code ricevuto'}")

    token_body = {"client_id": client_id, "client_secret": client_secret, "code": result["code"],
                 "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
    if verifier:
        token_body["code_verifier"] = verifier
    tokens = _token_request(spec, urllib.parse.urlencode(token_body).encode("utf-8"))
    with VaultLock(vault) as lock:
        state = _state(vault)
        saved = state.setdefault(provider, {})
        saved.update(client_id=client_id, client_secret=client_secret,
                     refresh_token=tokens.get("refresh_token") or saved.get("refresh_token"),
                     access_token=tokens["access_token"], token_expiry=time.time() + tokens.get("expires_in", 3600))
        save_state(vault, lock, "connectors.json", state)
    if not saved.get("refresh_token"):
        raise MnemoError("NO_REFRESH_TOKEN", "no refresh_token received: revoke the app's access from your "
                         "account and try again (a clean first consent is needed)")
    return {"provider": provider, "connected": True}


def _ensure_access_token(vault: Path, provider: str) -> str:
    """A valid access token. For a `"token"` provider that's just the stored token, unchanged --
    a GitHub PAT or Notion Internal Integration token isn't refreshed via OAuth, the user replaces
    it manually (set_token again) if it's ever revoked or expires. For `"oauth"` providers, refresh
    it first if it has (or is about to) expire."""
    spec = _provider(provider)
    cfg = _state(vault).get(provider) or {}
    if not _connected(spec, cfg):
        raise MnemoError("NOT_CONNECTED", f"{provider} is not connected yet: run connect first")
    if spec["auth_kind"] == "token":
        return cfg["token"]
    if cfg.get("access_token") and time.time() < (cfg.get("token_expiry") or 0) - 60:
        return cfg["access_token"]
    tokens = _token_request(spec, urllib.parse.urlencode({
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"], "grant_type": "refresh_token",
    }).encode("utf-8"))
    with VaultLock(vault) as lock:
        state = _state(vault)
        saved = state.setdefault(provider, {})
        saved["access_token"] = tokens["access_token"]
        saved["token_expiry"] = time.time() + tokens.get("expires_in", 3600)
        save_state(vault, lock, "connectors.json", state)
    return tokens["access_token"]


def set_folder(vault: Path, provider: str, folder_id: str, folder_name: str | None = None) -> dict[str, Any]:
    spec = _provider(provider)
    folder_id = (folder_id or "").strip()
    if not folder_id:
        raise MnemoError("BAD_FOLDER", "folder_id is required")
    with VaultLock(vault) as lock:
        state = _state(vault)
        cfg = state.get(provider) or {}
        if not _connected(spec, cfg):
            raise MnemoError("NOT_CONNECTED", f"{provider} is not connected yet: run connect first")
        cfg["folder_id"], cfg["folder_name"] = folder_id, (folder_name or "").strip() or None
        state[provider] = cfg
        save_state(vault, lock, "connectors.json", state)
    return {"provider": provider, "folder_id": folder_id, "folder_name": cfg["folder_name"]}


def disable(vault: Path, provider: str) -> dict[str, Any]:
    """Revokes the stored tokens; never touches files already imported."""
    _provider(provider)
    with VaultLock(vault) as lock:
        state = _state(vault)
        state.pop(provider, None)
        save_state(vault, lock, "connectors.json", state)
    return {"provider": provider, "disabled": True}


DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


def _drive_get(vault: Path, path: str, params: dict[str, str]) -> tuple[bytes, str]:
    token = _ensure_access_token(vault, "google_drive")
    url = f"{DRIVE_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("GOOGLE_DRIVE_FAILED", f"Drive API error {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("GOOGLE_DRIVE_FAILED", f"cannot reach Google Drive: {exc}") from exc


def _drive_file_text(f: dict[str, Any], vault: Path) -> str | None:
    """None for a file type this can't turn into text yet (Sheets, Slides, Forms, binaries...)."""
    if f["mimeType"] == GOOGLE_DOC_MIME:
        body, _ = _drive_get(vault, f"/files/{f['id']}/export", {"mimeType": "text/plain"})
        return body.decode("utf-8", errors="replace")
    if f["mimeType"].startswith("application/vnd.google-apps."):
        return None
    body, content_type = _drive_get(vault, f"/files/{f['id']}", {"alt": "media"})
    if not (content_type.startswith("text/") or content_type in ("application/json", "application/xml")):
        return None
    return body.decode("utf-8", errors="replace")


def _sync_google_drive(vault: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    from . import clips

    query = f"'{cfg['folder_id']}' in parents and trashed = false"
    if cfg.get("last_sync"):
        query += f" and modifiedTime > '{cfg['last_sync']}'"
    # capped at 100 files per sync for now (no pagination yet): fine for a personal folder that
    # gets checked periodically, revisit if someone drops hundreds of files in at once
    body, _ = _drive_get(vault, "/files", {"q": query, "fields": "files(id,name,mimeType,modifiedTime)", "pageSize": "100"})
    files = json.loads(body).get("files", [])
    imported, skipped, errors = [], [], []
    for f in files:
        try:
            text = _drive_file_text(f, vault)
            if text is None:
                skipped.append(f["name"])
                continue
            clips.add_text(vault, text, title=f["name"], note=f"Google Drive: {f['name']}", mine=False)
            imported.append(f["name"])
        except MnemoError as exc:
            errors.append(f"{f['name']}: {exc.code}: {exc.message}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


ONEDRIVE_API = "https://graph.microsoft.com/v1.0"


def _onedrive_get(vault: Path, path: str) -> tuple[bytes, str]:
    token = _ensure_access_token(vault, "onedrive")
    req = urllib.request.Request(f"{ONEDRIVE_API}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("ONEDRIVE_FAILED", f"OneDrive API error {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("ONEDRIVE_FAILED", f"cannot reach OneDrive: {exc}") from exc


def _sync_onedrive(vault: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    from . import clips

    # Graph's $filter doesn't support lastModifiedDateTime on /children: list one folder level
    # (like Drive) and diff client-side against last_sync instead
    body, _ = _onedrive_get(vault, f"/me/drive/items/{cfg['folder_id']}/children")
    items = json.loads(body).get("value", [])
    last_sync = cfg.get("last_sync")
    imported, skipped, errors = [], [], []
    for item in items:
        if "folder" in item:
            continue  # one level only, same scope as the other connectors for now
        if last_sync and item.get("lastModifiedDateTime", "") <= last_sync:
            continue
        try:
            mime = (item.get("file") or {}).get("mimeType", "")
            if not (mime.startswith("text/") or mime in ("application/json", "application/xml")):
                skipped.append(item["name"])
                continue
            content, _ = _onedrive_get(vault, f"/me/drive/items/{item['id']}/content")
            clips.add_text(vault, content.decode("utf-8", errors="replace"), title=item["name"],
                          note=f"OneDrive: {item['name']}", mine=False)
            imported.append(item["name"])
        except MnemoError as exc:
            errors.append(f"{item['name']}: {exc.code}: {exc.message}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


def _github_get(vault: Path, path: str) -> Any:
    token = _ensure_access_token(vault, "github")
    req = urllib.request.Request(f"https://api.github.com{path}",
                                 headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("GITHUB_FAILED", f"GitHub API error {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("GITHUB_FAILED", f"cannot reach GitHub: {exc}") from exc


def _sync_github(vault: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """folder_id here is "owner/repo" or "owner/repo/path/inside/it". The Contents API has no
    since-filter, so changes are detected by comparing each file's git blob sha to the sha seen at
    the last sync (stored per path) -- same idea as Drive's modifiedTime diff, just content-hash
    based since that's what GitHub actually exposes here. One directory level only, like the other
    connectors for now: no recursion into subfolders yet."""
    from . import clips

    parts = cfg["folder_id"].strip("/").split("/", 2)
    if len(parts) < 2:
        raise MnemoError("BAD_FOLDER", "use the format owner/repo or owner/repo/path")
    owner, repo = parts[0], parts[1]
    subpath = parts[2] if len(parts) > 2 else ""
    entries = _github_get(vault, f"/repos/{owner}/{repo}/contents/{subpath}")
    entries = entries if isinstance(entries, list) else [entries]
    known = cfg.get("known_shas") or {}
    new_known = dict(known)
    imported, skipped, errors = [], [], []
    for entry in entries:
        if entry.get("type") != "file":
            continue
        if known.get(entry["path"]) == entry["sha"]:
            continue
        try:
            data = _github_get(vault, f"/repos/{owner}/{repo}/contents/{entry['path']}")
            if data.get("encoding") != "base64" or "content" not in data:
                skipped.append(entry["path"])
                continue
            try:
                text = base64.b64decode(data["content"]).decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(entry["path"])
                continue
            clips.add_text(vault, text, title=entry["name"], note=f"GitHub: {owner}/{repo}/{entry['path']}", mine=False)
            imported.append(entry["path"])
            new_known[entry["path"]] = entry["sha"]
        except MnemoError as exc:
            errors.append(f"{entry['path']}: {exc.code}: {exc.message}")
    with VaultLock(vault) as lock:
        state = _state(vault)
        state.setdefault("github", {})["known_shas"] = new_known
        save_state(vault, lock, "connectors.json", state)
    return {"imported": imported, "skipped": skipped, "errors": errors}


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_TEXT_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
                     "numbered_list_item", "to_do", "quote", "code"}
NOTION_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
                "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> "}


def _notion_request(vault: Path, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _ensure_access_token(vault, "notion")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{NOTION_API}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("NOTION_FAILED", f"Notion API error {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("NOTION_FAILED", f"cannot reach Notion: {exc}") from exc


def _notion_title(page: dict[str, Any]) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            text = "".join(t.get("plain_text", "") for t in prop["title"])
            return text or "Senza titolo"
    return "Senza titolo"


def _notion_block_text(vault: Path, block_id: str, depth: int = 0) -> str:
    if depth > 6:  # guard against pathological nesting, not expected in normal notes
        return ""
    lines: list[str] = []
    cursor = None
    while True:
        params = {"page_size": "100"}
        if cursor:
            params["start_cursor"] = cursor
        result = _notion_request(vault, "GET", f"/blocks/{block_id}/children?{urllib.parse.urlencode(params)}")
        for block in result.get("results", []):
            kind = block.get("type")
            if kind in NOTION_TEXT_BLOCKS:
                text = "".join(t.get("plain_text", "") for t in block.get(kind, {}).get("rich_text", []))
                if text:
                    lines.append(NOTION_PREFIX.get(kind, "") + text)
            if block.get("has_children"):
                nested = _notion_block_text(vault, block["id"], depth + 1)
                if nested:
                    lines.append(nested)
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return "\n".join(lines)


def _sync_notion(vault: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """folder_id here is a Notion database ID. The integration must already be connected to that
    database from Notion's own UI ("Add connections"), or every call below 404s."""
    from . import clips

    query_body: dict[str, Any] = {"sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]}
    if cfg.get("last_sync"):
        query_body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"after": cfg["last_sync"]}}
    result = _notion_request(vault, "POST", f"/databases/{cfg['folder_id']}/query", query_body)
    pages = result.get("results", [])
    imported, skipped, errors = [], [], []
    for page in pages:
        title = _notion_title(page)
        try:
            text = _notion_block_text(vault, page["id"])
            if not text.strip():
                skipped.append(title)
                continue
            clips.add_text(vault, text, title=title, note=f"Notion: {title}", mine=False)
            imported.append(title)
        except MnemoError as exc:
            errors.append(f"{title}: {exc.code}: {exc.message}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


def _gmail_get(vault: Path, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    token = _ensure_access_token(vault, "gmail")
    url = f"{GMAIL_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("GMAIL_FAILED", f"Gmail API error {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("GMAIL_FAILED", f"cannot reach Gmail: {exc}") from exc


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _gmail_plain_text(payload: dict[str, Any]) -> str | None:
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        return _b64url_decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts") or []:
        text = _gmail_plain_text(part)
        if text:
            return text
    return None


def _gmail_query(cfg: dict[str, Any]) -> str:
    query = cfg["folder_id"]
    if cfg.get("last_sync"):
        import calendar
        ts = calendar.timegm(time.strptime(cfg["last_sync"], "%Y-%m-%dT%H:%M:%SZ"))
        query += f" after:{ts}"
    return query


def _sync_gmail(vault: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """folder_id here is a Gmail search query (e.g. "label:appunti" or "from:qualcuno@esempio.com"),
    not literally a folder -- Gmail has labels, not folders. Capped at 50 messages per sync, same
    reasoning as Drive's 100-file cap."""
    from . import clips

    listing = _gmail_get(vault, "/users/me/messages", {"q": _gmail_query(cfg), "maxResults": "50"})
    imported, skipped, errors = [], [], []
    for m in listing.get("messages", []):
        try:
            msg = _gmail_get(vault, f"/users/me/messages/{m['id']}", {"format": "full"})
            headers = (msg.get("payload") or {}).get("headers", [])
            subject = next((h["value"] for h in headers if h.get("name", "").lower() == "subject"), m["id"])
            text = _gmail_plain_text(msg.get("payload") or {})
            if not text:
                skipped.append(subject)
                continue
            clips.add_text(vault, text, title=subject, note=f"Gmail: {subject}", mine=False)
            imported.append(subject)
        except MnemoError as exc:
            errors.append(f"{m['id']}: {exc.code}: {exc.message}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


_SYNC_FUNCTIONS = {"google_drive": _sync_google_drive, "onedrive": _sync_onedrive, "github": _sync_github,
                  "notion": _sync_notion, "gmail": _sync_gmail}


def sync(vault: Path, provider: str) -> dict[str, Any]:
    require_vault(vault)
    spec = _provider(provider)
    if not spec["implemented"]:
        raise MnemoError("NOT_IMPLEMENTED", f"{spec['display_name']} cannot be connected yet")
    cfg = _state(vault).get(provider) or {}
    if not _connected(spec, cfg):
        raise MnemoError("NOT_CONNECTED", f"{provider} is not connected yet: run connect first")
    if not cfg.get("folder_id"):
        raise MnemoError("NO_FOLDER", f"no folder set for {provider}: run set-folder first")
    result = _SYNC_FUNCTIONS[provider](vault, cfg)
    with VaultLock(vault) as lock:
        state = _state(vault)
        state.setdefault(provider, {})["last_sync"] = _rfc3339_utc_now()
        save_state(vault, lock, "connectors.json", state)
    return {"provider": provider, **result}


def sync_all(vault: Path) -> dict[str, Any]:
    """Every connected, enabled, implemented provider -- called from the daily scheduled job
    (schedule.py). One provider failing never stops the others. No LLM call anywhere in this path:
    only reading/downloading files, same trust level as the existing source scan."""
    require_vault(vault)
    state = _state(vault)
    results: dict[str, Any] = {}
    for name, cfg in state.items():
        spec = PROVIDERS.get(name)
        if spec is None or not isinstance(cfg, dict) or not _connected(spec, cfg) or not cfg.get("folder_id"):
            continue
        if not cfg.get("enabled", True) or not spec.get("implemented"):
            continue
        try:
            results[name] = sync(vault, name)
        except MnemoError as exc:
            results[name] = {"error": f"{exc.code}: {exc.message}"}
    return results
