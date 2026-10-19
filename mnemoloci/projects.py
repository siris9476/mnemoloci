"""New projects created on the fly under one root folder, for Claude Code Remote
Control sessions started from the phone.

`claude remote-control` (server mode) runs in the root; a session opened from the
phone starts there, `projects new` creates the folder, and the user types `/cd`
(Claude cannot run /cd itself: it is a user-only command by design). Outside a
git repository, Claude Code's workspace trust on the root covers subfolders.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import winlaunch
from .core.errors import ConflictError, MnemoError, ValidationError
from .core.lock import VaultLock
from .core.paths import validate_relpath
from .sources import add_source, load_sources, resolve_root
from .vault import load_config, require_vault, save_state

STARTUP_NAME = "Claude remote-control (Mnemoloci).cmd"
LINUX_SERVICE = "mnemoloci-claude-remote-control"
STARTUP_MARKER = "mnemoloci-claude-rc-autostart (created by: python -P -m mnemoloci projects rc-autostart --enable)"
CLAUDE_MD = """# {name}

{description}

- Progetto creato il {date} da Mnemoloci (sessione Claude Code dal telefono).
- Il second brain Mnemoloci lo conosce come fonte `{source}`: dopo del lavoro importante, chiedi
  "aggiorna il second brain con questo progetto".
"""


def get_root(vault: Path) -> Path | None:
    raw = load_config(vault).get("projects", {}).get("root")
    return Path(raw) if raw else None


def set_root(vault: Path, path: str) -> dict[str, Any]:
    require_vault(vault)
    raw = Path((path or "").strip().strip('"')).expanduser()
    if not raw.is_absolute():
        raise MnemoError("NOT_ABSOLUTE", "give a full path, e.g. C:\\Users\\you\\Projects")
    root = raw.resolve()
    if not root.is_dir():
        raise MnemoError("NO_SUCH_DIR", f"{root} is not a directory")
    vault_path = Path(vault).resolve()
    if root == vault_path or vault_path in root.parents:
        raise MnemoError("ROOT_IN_VAULT", "the projects root cannot be the vault or a folder inside it")
    with VaultLock(vault) as lock:
        config = load_config(vault)
        config["projects"] = {"root": str(root)}
        save_state(vault, lock, "config.json", config)
    git_toplevel = _git_toplevel(root)
    return {"root": str(root), "inside_git_repo": git_toplevel, "autostart_updated": _refresh_autostart(root),
            "trust_note": ("Claude Code keys trust on the git root: new subfolders are covered once that repo is trusted"
                           if git_toplevel else
                           "run `claude` once in this folder and accept the trust dialog: it then covers every subfolder")}


def _git_toplevel(path: Path) -> str | None:
    if not shutil.which("git"):
        return None
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30)
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _check_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 60:
        raise ValidationError("BAD_PROJECT_NAME", "project name must be 1-60 characters")
    if "/" in name or "\\" in name:
        raise ValidationError("BAD_PROJECT_NAME", "a project name is one folder name, without / or \\")
    try:
        validate_relpath(name)  # one portable path component: no / \ : .. reserved names, trailing dot/space
    except ValidationError as exc:
        raise ValidationError("BAD_PROJECT_NAME", exc.message) from exc
    if name.startswith("."):
        raise ValidationError("BAD_PROJECT_NAME", "project name cannot start with a dot")
    return name


def new_project(vault: Path, name: str, *, description: str = "", git: bool = True,
                register: bool = True) -> dict[str, Any]:
    require_vault(vault)
    root = get_root(vault)
    if root is None:
        raise MnemoError("NO_PROJECTS_ROOT", "set it first: python -P -m mnemoloci projects root <folder>")
    name = _check_name(name)
    folder = root / name
    if folder.exists() or any(p.name.casefold() == name.casefold() for p in root.iterdir()):
        raise ConflictError("PROJECT_EXISTS", f"{folder} already exists: use /cd to open it instead")
    folder.mkdir()
    source_name = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")[:40] or "progetto"
    existing_sources = load_sources(vault)["sources"]
    base, n = source_name, 2
    while source_name in existing_sources:
        source_name, n = f"{base[:36]}-{n}", n + 1
    (folder / "CLAUDE.md").write_text(CLAUDE_MD.format(
        name=name, description=(description or "Descrizione da completare.").strip(),
        date=time.strftime("%Y-%m-%d"), source=source_name), "utf-8")
    git_done = False
    if git and shutil.which("git") and not _git_toplevel(root):
        git_done = subprocess.run(["git", "-C", str(folder), "init", "-q"], capture_output=True, timeout=60).returncode == 0
    if register:
        add_source(vault, str(folder), source_name, "project")
    return {"path": str(folder), "git_init": git_done, "source": source_name if register else None,
            "next": f"/cd {folder}",
            "note": "type the /cd line yourself: Claude cannot run /cd (user-only command)"}


def list_projects(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    root = get_root(vault)
    if root is None:
        return {"root": None, "projects": []}
    registered = {resolve_root(vault, e).resolve(): n for n, e in load_sources(vault)["sources"].items()}
    items = [{"name": p.name, "path": str(p), "source": registered.get(p.resolve())}
             for p in sorted(root.iterdir(), key=lambda p: p.name.casefold()) if p.is_dir() and not p.name.startswith(".")]
    return {"root": str(root), "projects": items}


def _startup_file() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_NAME


def _write_launcher(target: Path, root: Path, claude: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    winlaunch.write_batch(target, STARTUP_MARKER, [f"cd /d {winlaunch.batch_arg(root)}",
                                                   f'start "Claude remote-control" /min {winlaunch.batch_arg(claude)} remote-control'])


def _refresh_autostart(root: Path) -> bool:
    """A launcher we created still points at the old root: rewrite it. Never touches other files."""
    if os.name != "nt":
        from . import posixlaunch

        claude = shutil.which("claude")
        if not claude or posixlaunch.autostart_file(LINUX_SERVICE) is None:
            return False
        posixlaunch.start_at_login(LINUX_SERVICE, [claude, "remote-control"], workdir=root,
                                   description="Claude remote-control (Mnemoloci)")
        return True
    if not os.environ.get("APPDATA"):
        return False
    target = _startup_file()
    if not target.is_file() or STARTUP_MARKER not in target.read_text("utf-8", errors="replace"):
        return False
    claude = shutil.which("claude")
    if not claude:
        return False
    _write_launcher(target, root, claude)
    return True


def rc_autostart(vault: Path, enable: bool) -> dict[str, Any]:
    """Start `claude remote-control` in the projects root at login (explicit opt-in).
    Windows: a launcher in the Startup folder; Linux: a systemd user service (see posixlaunch)."""
    if os.name != "nt":
        from . import posixlaunch

        if not enable:
            return posixlaunch.stop_at_login(LINUX_SERVICE)
        root = get_root(vault)
        if root is None:
            raise MnemoError("NO_PROJECTS_ROOT", "set it first: python3 -P -m mnemoloci projects root <folder>")
        claude = shutil.which("claude")
        if not claude:
            raise MnemoError("NO_CLAUDE", "the claude CLI is not on PATH")
        out = posixlaunch.start_at_login(LINUX_SERVICE, [claude, "remote-control"], workdir=root,
                                         description="Claude remote-control (Mnemoloci)")
        return {**out, "runs": f"{claude} remote-control  (in {root})",
                "before_first_use": ["run `claude` once in the root and accept the trust dialog",
                                     "run `claude remote-control` once by hand and answer y to 'Enable Remote Control?'"]}
    target = _startup_file()
    if not enable:
        if target.is_file() and STARTUP_MARKER in target.read_text("utf-8", errors="replace"):
            target.unlink()  # only ever our own launcher
            return {"autostart": False, "removed": str(target)}
        return {"autostart": False, "note": "not enabled"}
    root = get_root(vault)
    if root is None:
        raise MnemoError("NO_PROJECTS_ROOT", "set it first: python -P -m mnemoloci projects root <folder>")
    claude = shutil.which("claude")
    if not claude:
        raise MnemoError("NO_CLAUDE", "the claude CLI is not on PATH")
    _write_launcher(target, root, claude)
    return {"autostart": True, "file": str(target), "runs": f'"{claude}" remote-control  (in {root})',
            "before_first_use": ["run `claude` once in the root and accept the trust dialog",
                                 "run `claude remote-control` once by hand and answer y to 'Enable Remote Control?'"]}
