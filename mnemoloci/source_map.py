"""What a source contributes, folder by folder, and the user's choices about it.

The configuration lives in the source registry (.mnemoloci/sources.json):
- scope: "docs" (documents and project files) or "all" (code too);
- exclude: folders never read;
- include: folders read even though an automatic rule would skip them (a folder of the
  user's that happens to be called `vendor`, a git-ignored notes folder...).
The most specific choice wins (exclude `a`, include `a/b`: only a/b is read).

The desktop app shows and edits it (Fonti page); Claude reads it with `source show` before
compiling and updates it with `source exclude / include / scope` when the user asks in
the conversation. Nothing in the source folder is ever written.
"""

import os
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .core.errors import MnemoError
from .core.lock import VaultLock
from .sources import SKIP_DIRS, _under, add_source, classify_source, free_name, load_sources, resolve_root
from .vault import load_config, require_vault, save_state

CACHE_SECONDS = 60
_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
KIND = {0: "docs", 1: "project", 2: "config", 3: "code"}
USER_REASONS = {"excluded-by-user"}


def _clean(folder: str) -> str:
    rel = str(folder).replace("\\", "/").strip().strip("/")
    if not rel or rel.startswith("..") or "/../" in f"/{rel}/" or ":" in rel or rel.startswith("/"):
        raise MnemoError("BAD_FOLDER", f"{folder!r}: give a folder relative to the source, e.g. tools/vendor")
    return rel


def _entry(vault: Path, name: str) -> dict[str, Any]:
    entry = load_sources(vault)["sources"].get(name)
    if entry is None:
        raise MnemoError("NO_SUCH_SOURCE", f"unknown source {name!r}")
    return entry


def _details(vault: Path, name: str, entry: dict[str, Any], fresh: bool = False) -> list[dict[str, Any]]:
    root = resolve_root(vault, entry)
    key = (str(vault), name, str(root), entry.get("scope", "all"), tuple(entry.get("exclude") or ()),
           tuple(entry.get("include") or ()))
    hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if not root.is_dir():
        raise MnemoError("SOURCE_MISSING", f"source root missing: {root}")
    details = classify_source(root, load_config(vault)["ingest"]["max_file_bytes"], entry.get("scope", "all"),
                              entry.get("exclude"), entry.get("include"), hash_files=False)
    _cache[key] = (time.time(), details)
    return details


def _is_git(root: Path) -> bool:
    return (root / ".git").exists()


def _auto_reason(root: Path, rel: str) -> str | None:
    """Why the automatic rules skip this folder (None: they do not)."""
    parts = PurePosixPath(rel).parts
    if any(p in SKIP_DIRS for p in parts):
        return "excluded-dir"
    for i in range(1, len(parts) + 1):
        if (root.joinpath(*parts[:i]) / "pyvenv.cfg").is_file():
            return "virtualenv"
    if _is_git(root):
        try:
            if subprocess.run(["git", "-C", str(root), "check-ignore", "-q", rel + "/"], capture_output=True,
                              timeout=20).returncode == 0:
                return "gitignored"
        except (OSError, subprocess.SubprocessError):
            pass
    elif any(p.startswith(".") for p in parts):
        return "hidden"
    return None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    read = {k: 0 for k in KIND.values()}
    skipped: dict[str, int] = {}
    for d in rows:
        if d["reason"] is None:
            read[KIND.get(d["priority"], "code")] += 1
        else:
            skipped[d["reason"]] = skipped.get(d["reason"], 0) + 1
    return {"read": read, "read_total": sum(read.values()), "skipped": skipped}


def tree(vault: Path, name: str, path: str = "", *, fresh: bool = False) -> dict[str, Any]:
    """One level of the source: its folders (read, excluded by you, skipped automatically, forced in)
    with what each contributes, and its files with the reason each is read or skipped."""
    require_vault(vault)
    entry = _entry(vault, name)
    root = resolve_root(vault, entry)
    path = _clean(path) if path else ""
    exclude, include = list(entry.get("exclude") or []), list(entry.get("include") or [])
    details = _details(vault, name, entry, fresh)
    prefix = f"{path}/" if path else ""
    here = root / path if path else root
    if not here.is_dir():
        raise MnemoError("NO_SUCH_FOLDER", f"{path} is not a folder of {name}")
    below: dict[str, list[dict[str, Any]]] = {}
    files_here: dict[str, dict[str, Any]] = {}
    for d in details:
        if not d["rel"].startswith(prefix):
            continue
        rest = d["rel"][len(prefix):]
        head, _, tail = rest.partition("/")
        if tail:
            below.setdefault(head, []).append(d)
        else:
            files_here[head] = d
    folders, files = [], []
    level_reason = _auto_reason(root, path) if path and not _under(path, include) else None
    for item in sorted(os.scandir(here), key=lambda e: (not e.is_dir(), e.name.casefold())):
        if item.name == ".git":
            continue
        rel = prefix + item.name
        if item.is_dir(follow_symlinks=False):
            rows = below.get(item.name, [])
            inc, exc = _under(rel, include), _under(rel, exclude)
            if exc > inc:
                status, reason = "excluded", "excluded-by-user"
            elif inc and inc == len(rel) + 1:
                status, reason = "forced", None
            else:
                auto = None if inc else _auto_reason(root, rel)
                if auto:
                    status, reason = "auto", auto
                elif not rows:
                    status, reason = "empty", None
                else:
                    status, reason = "included", None
            folders.append({"name": item.name, "rel": rel, "status": status, "reason": reason,
                            "selected": status in ("included", "forced", "empty"), **_summary(rows)})
        else:
            d = files_here.get(item.name)
            if d:
                why = d["reason"]
            elif _under(rel, exclude) > _under(rel, include):
                why = "excluded-by-user"
            else:  # never listed: this folder is skipped automatically, or git ignores the file
                why = level_reason or ("gitignored" if _is_git(root) else "excluded-dir")
            files.append({"name": item.name, "rel": rel, "read": bool(d) and d["reason"] is None, "reason": why,
                          "kind": KIND.get(d["priority"]) if d and d["priority"] is not None else None})
    return {"source": name, "root": str(root), "path": path, "scope": entry.get("scope", "all"),
            "exclude": exclude, "include": include, "folders": folders, "files": files,
            "totals": _summary(details), "here": _summary([d for d in details if d["rel"].startswith(prefix)])}


def show(vault: Path, name: str) -> dict[str, Any]:
    """The configuration and what it leads to, for Claude before a compile."""
    t = tree(vault, name)
    return {k: t[k] for k in ("source", "root", "scope", "exclude", "include", "totals")} | {
        "top_folders": [{k: f[k] for k in ("rel", "status", "reason", "read_total")} for f in t["folders"]],
        "change_with": ["mnemoloci source exclude NAME FOLDER...", "mnemoloci source include NAME FOLDER...",
                        "mnemoloci source scope NAME docs|all", "the Fonti page of the app"]}


def set_folder(vault: Path, name: str, folder: str, selected: bool) -> dict[str, Any]:
    """Tick or untick a folder: untick excludes it; tick removes an exclusion and, if an automatic
    rule or an excluded parent would still skip it, forces it in."""
    require_vault(vault)
    rel = _clean(folder)
    with VaultLock(vault) as lock:
        state = load_sources(vault)
        entry = state["sources"].get(name)
        if entry is None:
            raise MnemoError("NO_SUCH_SOURCE", f"unknown source {name!r}")
        root = resolve_root(vault, entry)
        if not (root / rel).is_dir():
            raise MnemoError("NO_SUCH_FOLDER", f"{rel} is not a folder of {name}")
        exclude = set(entry.get("exclude") or [])
        include = set(entry.get("include") or [])
        if selected:
            exclude.discard(rel)
            exclude = {e for e in exclude if not e.startswith(rel + "/")}
            still_out = _under(rel, sorted(exclude)) > _under(rel, sorted(include)) or _auto_reason(root, rel)
            if still_out:
                include.add(rel)
        else:
            include.discard(rel)
            include = {i for i in include if not i.startswith(rel + "/")}
            exclude.add(rel)
        for key, value in (("exclude", exclude), ("include", include)):
            if value:
                entry[key] = sorted(value)
            else:
                entry.pop(key, None)
        save_state(vault, lock, "sources.json", state)
    return {"source": name, "folder": rel, "selected": selected, "exclude": sorted(exclude), "include": sorted(include)}


def include(vault: Path, name: str, folders: list[str]) -> dict[str, Any]:
    out = {}
    for folder in folders:
        out = set_folder(vault, name, folder, True)
    return out


def add_projects(vault: Path, parent: str, *, docs_only: bool = True, children: bool = False,
                 name: str | None = None) -> dict[str, Any]:
    """Register one project folder, or (children) every non-empty subfolder of `parent` not
    registered yet. Nothing in those folders is written."""
    require_vault(vault)
    base = Path(parent).expanduser()
    if not base.is_absolute():
        raise MnemoError("NOT_ABSOLUTE", "give the full path of the folder, e.g. C:\\Users\\you\\Projects\\App")
    if not base.is_dir():
        raise MnemoError("NO_SUCH_DIR", f"{base} is not a folder")
    known = {resolve_root(vault, e).resolve() for e in load_sources(vault)["sources"].values()}
    targets = sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold()) \
        if children else [base]
    added, skipped = [], []
    here = Path(vault).resolve()
    for folder in targets:
        if folder.resolve() == here or folder.resolve() in here.parents:
            skipped.append({"folder": str(folder), "why": "the vault itself"})
            continue
        if folder.resolve() in known:
            skipped.append({"folder": str(folder), "why": "already registered"})
            continue
        if children and not any(folder.iterdir()):
            skipped.append({"folder": str(folder), "why": "empty"})
            continue
        source = name if (name and not children) else free_name(vault, str(folder))
        added.append(add_source(vault, str(folder), source, "project", "docs" if docs_only else "all"))
    return {"added": added, "skipped": skipped,
            "next": "look at each source in the Fonti page (or `source show NAME`), then compile"}
