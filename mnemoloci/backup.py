"""Git backup of the vault: every change becomes a commit, so any earlier state can be restored,
and an optional remote you choose keeps an off-machine copy.

A commit is taken under the vault lock and refused while a transaction is
interrupted, so it never captures a half-applied write. Derived data (search
indexes, compile scratch, the lock) and device pairings stay out of git.
Pushing only ever happens to the remote the user configured, and never on its own
unless `auto_push` was chosen explicitly.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import txn
from .core.errors import MnemoError
from .core.lock import VaultLock
from .vault import load_config, require_vault, save_state

GITIGNORE = """# Mnemoloci: derived or machine-local data, rebuilt automatically
.mnemoloci/lock/
.mnemoloci/work/
.mnemoloci/transactions/
.mnemoloci/*.sqlite
.mnemoloci/*.sqlite-*
.mnemoloci/remote.json
.mnemoloci/embedding_keys.json
.mnemoloci/connectors.json
.obsidian/workspace*.json
.trash/
"""
MARKER = "# Mnemoloci: derived or machine-local data"


def _git(vault: Path, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    if not shutil.which("git"):
        raise MnemoError("NO_GIT", "git is not installed or not on PATH")
    # never wait on a credential prompt nobody can see (scheduled runs, hooks, the app)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    out = subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout, env=env)
    if check and out.returncode != 0:
        raise MnemoError("GIT_FAILED", f"git {args[0]} failed: {(out.stderr or out.stdout).strip()[:400]}")
    return out


def _is_repo(vault: Path) -> bool:
    return (Path(vault) / ".git").exists()


def _identity(vault: Path) -> list[str]:
    """Use the user's git identity; fall back to a local one so commits never fail."""
    if _git(vault, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", "user.name=Mnemoloci", "-c", "user.email=mnemoloci@localhost"]


def settings(vault: Path) -> dict[str, Any]:
    return {"auto_commit": False, "auto_push": False, **(load_config(vault).get("backup") or {})}


def _ensure_gitignore(vault: Path) -> bool:
    """Append any GITIGNORE line not already present -- not just once, guarded by MARKER, but
    every time: a vault set up before a new sensitive path (e.g. embedding_keys.json) existed
    would otherwise never get the new exclusion, and the very next commit could capture it.
    Idempotent and safe to call before every commit, not only at `init`."""
    ignore = vault / ".gitignore"
    text = ignore.read_text("utf-8") if ignore.is_file() else ""
    have = set(text.splitlines())
    already_set_up = MARKER in text
    missing = [line for line in GITIGNORE.splitlines() if line and line not in have]
    if already_set_up and not missing:
        return False
    prefix = (text.rstrip() + "\n\n") if text.strip() else ""
    body = "\n".join(missing) if already_set_up else GITIGNORE.rstrip()
    ignore.write_text(prefix + body + "\n", "utf-8")
    return True


def init(vault: Path, *, remote: str | None = None, auto_commit: bool = True, auto_push: bool = False) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    if auto_push and not remote and not (_is_repo(vault) and _git(vault, "remote", check=False).stdout.strip()):
        raise MnemoError("NO_REMOTE", "auto_push needs a remote: pass --remote <url>")
    with VaultLock(vault) as lock:
        created = not _is_repo(vault)
        if created:
            _git(vault, "init", "-q")
        _ensure_gitignore(vault)
        if remote:
            existing = _git(vault, "remote", check=False).stdout.split()
            _git(vault, "remote", "set-url" if "origin" in existing else "add", "origin", remote)
        config = load_config(vault)
        config["backup"] = {"auto_commit": auto_commit, "auto_push": auto_push}
        save_state(vault, lock, "config.json", config)
    first = commit(vault, "Mnemoloci: primo backup del vault")
    return {"repository": str(vault), "created": created, "remote": remote, **config["backup"], "first_commit": first}


STALE_GIT_LOCK = 15 * 60  # seconds: no git command of ours runs that long


def stale_git_lock(vault: Path) -> Path | None:
    """.git/index.lock left by a git killed mid-commit (e.g. a hook stopped at exit): every later
    backup would fail with "index.lock exists" until it goes."""
    path = Path(vault) / ".git" / "index.lock"
    try:
        return path if time.time() - path.stat().st_mtime > STALE_GIT_LOCK else None
    except OSError:
        return None


def _clear_stale_git_lock(vault: Path) -> str | None:
    stale = stale_git_lock(vault)
    if stale is None:
        return None
    parked = stale.with_name(f"index.lock.stale-{time.strftime('%Y%m%d-%H%M%S')}")
    stale.replace(parked)  # moved aside, not deleted (it is an empty marker file anyway)
    return parked.name


def commit(vault: Path, message: str | None = None, *, lock: VaultLock | None = None,
           git_timeout: int = 120, lock_timeout: float = 10.0) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    if not _is_repo(vault):
        raise MnemoError("NO_BACKUP", "backup not set up: run `mnemoloci backup init`")

    def take(held: VaultLock) -> dict[str, Any]:
        if txn.incomplete_transactions(vault):
            raise MnemoError("INCOMPLETE_TRANSACTION", "an interrupted write must be recovered first (txn recover)")
        cleared = _clear_stale_git_lock(vault)
        _ensure_gitignore(vault)  # a vault set up before a newly-sensitive path existed self-heals here too
        _git(vault, "add", "-A", timeout=git_timeout)
        if not _git(vault, "status", "--porcelain", timeout=git_timeout).stdout.strip():
            return {"committed": False, "note": "nothing changed since the last backup"}
        _git(vault, *_identity(vault), "commit", "-q", "-m", message or f"Mnemoloci: backup {time.strftime('%Y-%m-%d %H:%M')}",
             timeout=git_timeout)
        head = _git(vault, "rev-parse", "--short", "HEAD").stdout.strip()
        files = len([f for f in _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.splitlines() if f.strip()])
        return {"committed": True, "commit": head, "files": files, **({"stale_git_lock_moved": cleared} if cleared else {})}

    if lock is not None:
        return take(lock)
    with VaultLock(vault, timeout=lock_timeout) as own:
        return take(own)


def push(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    if not _git(vault, "remote", check=False).stdout.strip():
        raise MnemoError("NO_REMOTE", "no remote configured: `mnemoloci backup init --remote <url>`")
    branch = _git(vault, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(vault, "push", "-u", "origin", branch, timeout=300)
    return {"pushed": True, "branch": branch}


def now(vault: Path, *, message: str | None = None, push_after: bool = False) -> dict[str, Any]:
    result = commit(vault, message)
    if push_after:
        result["push"] = push(vault)
    return result


def status(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    if not _is_repo(vault):
        return {"enabled": False, "note": "run `mnemoloci backup init` to start versioning the vault"}
    last = _git(vault, "log", "-1", "--format=%h|%cI|%s", check=False).stdout.strip().split("|", 2)
    pending = [line for line in _git(vault, "status", "--porcelain").stdout.splitlines() if line.strip()]
    remote = _git(vault, "remote", "get-url", "origin", check=False).stdout.strip() or None
    ahead = None
    if remote:
        counted = _git(vault, "rev-list", "--count", "@{u}..HEAD", check=False)
        ahead = int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else None
    return {"enabled": True, **settings(vault), "remote": remote, "unpushed_commits": ahead,
            "last_commit": {"id": last[0], "at": last[1], "message": last[2]} if len(last) == 3 else None,
            "uncommitted_changes": len(pending)}


def history(vault: Path, path: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Recent backups, optionally of one note: every earlier version stays recoverable."""
    require_vault(vault)
    args = ["log", f"-{max(1, limit)}", "--format=%h|%cI|%s"] + (["--", path] if path else [])
    rows = [line.split("|", 2) for line in _git(vault, *args).stdout.splitlines() if line.count("|") >= 2]
    return {"commits": [{"id": r[0], "at": r[1], "message": r[2]} for r in rows]}


def content_at(vault: Path, path: str, commit: str) -> str | None:
    """The full text of `path` as it was at `commit`, or None if that commit predates the file
    (git show fails cleanly rather than raising, since "not there yet" is an expected case when
    walking a page's whole history from its first commit)."""
    require_vault(vault)
    result = _git(vault, "show", f"{commit}:{path}", check=False)
    return result.stdout if result.returncode == 0 else None


def _push_detached(vault: Path) -> None:
    """The hook has a short time budget: the push runs on its own and is not awaited, without a
    window, surviving the hook's end, and never waiting on a credential dialog."""
    from .runtime import detached_popen

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    detached_popen(["git", "-C", str(vault), "push", "-q"], env=env)


def on_session_end(vault: Path, title: str) -> dict[str, Any] | None:
    """Called by the SessionEnd hook when auto_commit is on; never raises. Takes the vault lock only
    for the commit, with short timeouts: the hook is killed after 20 s."""
    try:
        conf = settings(vault)
        if not conf["auto_commit"] or not _is_repo(vault):
            return None
        result = commit(vault, f"Mnemoloci: fine sessione - {title}"[:200], git_timeout=12, lock_timeout=4.0)
        if result.get("committed") and conf["auto_push"]:
            _push_detached(vault)
            result["push"] = "started"
        return result
    except (MnemoError, OSError, subprocess.SubprocessError) as exc:
        return {"error": getattr(exc, "code", type(exc).__name__)}
