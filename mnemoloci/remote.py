"""Access from the phone through Tailscale.

The app server stays bound to 127.0.0.1; `tailscale serve` exposes it inside the
user's private tailnet over HTTPS. Access still needs a device token:
- pairing: the PC creates a one-time 8-character code (10 minutes, 5 attempts);
  the phone types it once and receives a long-lived token. A code is used instead
  of a link because on iOS a home-screen web app does not share storage with Safari;
- only sha256 hashes of tokens and codes are stored;
- device management (pair, revoke) is allowed only from the PC itself;
- optionally the Tailscale-User-Login header (added by Tailscale Serve, which
  strips spoofed copies) must match an allowed login.
"""

import hmac
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from . import winlaunch
from .core.errors import ConflictError, MnemoError, ValidationError
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .vault import load_state, now_iso, require_vault, save_state

STATE_SCHEMA = "mnemoloci.remote.v1"
DEFAULT_PORT = 8790
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_TTL = 600
MAX_ATTEMPTS = 5
LAST_SEEN_EVERY = 60


def _state(vault: Path) -> dict[str, Any]:
    return load_state(vault, "remote.json", STATE_SCHEMA,
                      {"devices": [], "allowed_hosts": [], "allowed_logins": [], "pairing": None})


def _hash(secret: str) -> str:
    return sha256_bytes(secret.encode("utf-8"))


def _norm_code(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def create_pairing_code(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    with VaultLock(vault) as lock:
        state = _state(vault)
        state["pairing"] = {"code_sha256": _hash(code), "expires": time.time() + CODE_TTL, "attempts": 0}
        save_state(vault, lock, "remote.json", state)
    return {"code": f"{code[:4]}-{code[4:]}", "expires_in_seconds": CODE_TTL,
            "note": "type it on the phone; it works once and expires in 10 minutes"}


def claim(vault: Path, code: str, device_name: str) -> dict[str, Any]:
    require_vault(vault)
    name = " ".join(str(device_name or "telefono").split())[:40] or "telefono"
    with VaultLock(vault) as lock:
        state = _state(vault)
        pairing = state.get("pairing")
        if not pairing or time.time() > pairing["expires"]:
            state["pairing"] = None
            save_state(vault, lock, "remote.json", state)
            raise ConflictError("NO_PAIRING_CODE", "no valid pairing code: create a new one on the PC")
        if not hmac.compare_digest(_hash(_norm_code(code)), pairing["code_sha256"]):
            pairing["attempts"] += 1
            if pairing["attempts"] >= MAX_ATTEMPTS:
                state["pairing"] = None  # too many wrong guesses: burn the code
            save_state(vault, lock, "remote.json", state)
            raise ValidationError("WRONG_CODE", "wrong pairing code")
        token = secrets.token_urlsafe(32)
        # a new device can read and capture (links, texts, todos); changing the wiki, review decisions,
        # goals and profile need `remote grant <id> --full` from the PC
        device = {"id": f"dev_{secrets.token_hex(4)}", "name": name, "token_sha256": _hash(token),
                  "created": now_iso(), "last_seen": None, "revoked": None, "scope": "capture"}
        state["devices"].append(device)
        state["pairing"] = None
        save_state(vault, lock, "remote.json", state)
    return {"device_id": device["id"], "name": name, "token": token}


def authenticate(vault: Path, token: str) -> dict[str, Any] | None:
    """Return the paired device for this token, or None. Updates last_seen now and then."""
    if not token:
        return None
    digest = _hash(token)
    state = _state(vault)
    device = next((d for d in state["devices"] if not d["revoked"] and hmac.compare_digest(d["token_sha256"], digest)), None)
    if device is None:
        return None
    seen = device.get("last_seen_epoch") or 0
    if time.time() - seen > LAST_SEEN_EVERY:
        try:
            with VaultLock(vault, timeout=0.5) as lock:
                fresh = _state(vault)
                for d in fresh["devices"]:
                    if d["id"] == device["id"]:
                        d["last_seen"], d["last_seen_epoch"] = now_iso(), time.time()
                save_state(vault, lock, "remote.json", fresh)
        except MnemoError:
            pass  # busy vault: last_seen is cosmetic, never block a request for it
    return device


def devices(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    state = _state(vault)
    return {"devices": [{**{k: d.get(k) for k in ("id", "name", "created", "last_seen", "revoked")},
                         "scope": d.get("scope", "capture")} for d in state["devices"]],
            "allowed_hosts": state["allowed_hosts"], "allowed_logins": state["allowed_logins"],
            "pairing_active": bool(state.get("pairing") and time.time() < state["pairing"]["expires"])}


def revoke(vault: Path, device_id: str) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        state = _state(vault)
        device = next((d for d in state["devices"] if d["id"] == device_id), None)
        if device is None:
            raise MnemoError("NO_SUCH_DEVICE", f"no device {device_id}")
        device["revoked"] = now_iso()  # kept for audit, never usable again
        save_state(vault, lock, "remote.json", state)
    return {"revoked": device_id}


SCOPES = ("capture", "full")


def set_scope(vault: Path, device_id: str, scope: str) -> dict[str, Any]:
    """capture: read everything, save links/texts/todos/digests. full: also edit pages, approve
    reviews, change goals, profile, tags and links (a stolen token could then change the wiki)."""
    require_vault(vault)
    if scope not in SCOPES:
        raise MnemoError("BAD_SCOPE", f"scope must be one of {SCOPES}")
    with VaultLock(vault) as lock:
        state = _state(vault)
        device = next((d for d in state["devices"] if d["id"] == device_id and not d.get("revoked")), None)
        if device is None:
            raise MnemoError("NO_SUCH_DEVICE", f"no active device {device_id}")
        device["scope"] = scope
        save_state(vault, lock, "remote.json", state)
    return {"device": device_id, "scope": scope}


def _set_list(vault: Path, key: str, values: list[str], add: bool) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        state = _state(vault)
        current = set(state[key])
        cleaned = {v.strip().lower().rstrip(".") for v in values if v.strip()}
        state[key] = sorted(current | cleaned if add else current - cleaned)
        save_state(vault, lock, "remote.json", state)
    return {key: state[key]}


def allow_hosts(vault: Path, hosts: list[str], add: bool = True) -> dict[str, Any]:
    for h in hosts:
        if "/" in h or " " in h:
            raise ValidationError("BAD_HOST", f"{h!r} is not a host name (e.g. mypc.tail1234.ts.net)")
    return _set_list(vault, "allowed_hosts", hosts, add)


def allow_logins(vault: Path, logins: list[str], add: bool = True) -> dict[str, Any]:
    return _set_list(vault, "allowed_logins", logins, add)


def access_policy(vault: Path) -> tuple[set[str], set[str]]:
    state = _state(vault)
    return set(state["allowed_hosts"]), set(state["allowed_logins"])


def setup_instructions(vault: Path, port: int = DEFAULT_PORT) -> dict[str, Any]:
    return {
        "steps": [
            "1. iPhone: install the Tailscale app and sign in with the SAME account as this PC.",
            f"2. PC, in an Administrator terminal: tailscale serve --bg --https=443 localhost:{port}",
            "   (if asked, enable HTTPS certificates for your tailnet in the Tailscale admin console)",
            "3. PC: tailscale serve status  -> note the https://<name>.<tailnet>.ts.net address",
            "4. PC: python -P -m mnemoloci remote allow-host <name>.<tailnet>.ts.net",
            "5. PC: python -P -m mnemoloci remote allow-login <your tailscale login e-mail>   (recommended)",
            f"6. PC: python -P -m mnemoloci remote serve   (or: remote autostart --enable)",
            "7. PC: python -P -m mnemoloci remote pair   -> 8-character code",
            "8. iPhone Safari: open the https address, Share > Add to Home Screen, open it, type the code.",
        ],
        "undo": ["tailscale serve reset", "python -P -m mnemoloci remote autostart --disable",
                 "python -P -m mnemoloci remote revoke <device id>"],
        "current": devices(vault),
    }


STARTUP_NAME = "Mnemoloci remote.cmd"
LINUX_SERVICE = "mnemoloci-remote"
STARTUP_MARKER = "mnemoloci-remote-autostart (created by: python -P -m mnemoloci remote autostart --enable)"


def _startup_file() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_NAME


def autostart(vault: Path, enable: bool) -> dict[str, Any]:
    """Start the phone server at login (explicit opt-in; `--disable` removes our own file).
    Windows: a launcher in the Startup folder; Linux: a systemd user service (see posixlaunch)."""
    if os.name != "nt":
        from . import posixlaunch

        if not enable:
            return posixlaunch.stop_at_login(LINUX_SERVICE)
        argv = [sys.executable, "-P", "-m", "mnemoloci", "--vault", str(Path(vault).resolve()), "remote", "serve"]
        return {**posixlaunch.start_at_login(LINUX_SERVICE, argv, description="Mnemoloci phone access"),
                "runs": " ".join(argv)}
    target = _startup_file()
    if not enable:
        if target.is_file() and STARTUP_MARKER in target.read_text("utf-8", errors="replace"):
            target.unlink()  # only ever our own launcher
            return {"autostart": False, "removed": str(target)}
        return {"autostart": False, "note": "not enabled"}
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else Path(sys.executable)
    target.parent.mkdir(parents=True, exist_ok=True)
    winlaunch.write_batch(target, STARTUP_MARKER, [
        f'start "" {winlaunch.batch_arg(runner)} -P -m mnemoloci --vault {winlaunch.batch_arg(Path(vault).resolve())} remote serve'])
    return {"autostart": True, "file": str(target), "runs": f"{runner} -m mnemoloci remote serve"}
