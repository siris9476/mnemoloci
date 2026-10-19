import fnmatch
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .core.errors import MnemoError
from .core.hashing import sha256_file
from .core.lock import VaultLock
from .core.paths import resolve_in_vault
from .secrets import SECRET_FILE
from .vault import load_config, load_state, now_iso, require_vault, save_state

SOURCES_SCHEMA = "mnemoloci.sources.v1"
MANIFEST_SCHEMA = "mnemoloci.manifest.v1"
SOURCE_KINDS = {"project", "import", "inbox", "sessions", "notes"}
EXTERNAL_KINDS = {"import", "inbox"}  # web clips (source `clips`) are kind "import": always reviewed
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SCOPES = ("all", "docs")  # docs: only documents and project files (README, CLAUDE.md, notes, package.json...)

DOC_EXT = {".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc", ".org"}
CONFIG_EXT = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".properties"}
CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".java", ".kt", ".kts", ".go", ".rs",
    ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cc", ".swift", ".dart", ".vue", ".svelte", ".sql",
    ".sh", ".ps1", ".psm1", ".bat", ".cmd", ".html", ".css", ".scss", ".lua", ".r", ".scala", ".fs",
    ".csproj", ".sln", ".gradle", ".proto", ".graphql",
}
PROJECT_FILES = [
    "readme*", "changelog*", "contributing*", "architecture*", "claude.md", "agents.md",
    "package.json", "pyproject.toml", "setup.py", "cargo.toml", "go.mod", "*.csproj", "*.sln",
    "pom.xml", "build.gradle*", "dockerfile", "docker-compose*.yml", "makefile",
]
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "out", "bin", "obj",
    "target", ".next", ".nuxt", ".svelte-kit", ".idea", ".vs", ".vscode", "coverage", ".pytest_cache",
    ".mypy_cache", ".tox", ".gradle", ".dart_tool", "Pods", ".obsidian", ".mnemoloci", ".trash", "vendor",
    "site-packages",
    # output of decompilers: somebody else's code, never the user's knowledge
    "jadx_out", "jadx-out", "jadx_output", "decompiled", "apktool_out",
}
SKIP_FILES = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "composer.lock",
    "gemfile.lock", "*.min.js", "*.min.css", "*.map", "*.snap",
]


def _priority(rel: str) -> int | None:
    name = PurePosixPath(rel).name.lower()
    if any(fnmatch.fnmatch(name, p) for p in SKIP_FILES):
        return None
    suffix = PurePosixPath(rel).suffix.lower()
    if any(fnmatch.fnmatch(name, p) for p in PROJECT_FILES):
        return 0 if suffix in DOC_EXT else 1
    if suffix in DOC_EXT:
        return 0
    if suffix in CONFIG_EXT:
        return 2
    if suffix in CODE_EXT:
        return 3
    return None


def load_sources(vault: Path) -> dict[str, Any]:
    return load_state(vault, "sources.json", SOURCES_SCHEMA, {"sources": {}})


def load_manifest(vault: Path) -> dict[str, Any]:
    return load_state(vault, "manifest.json", MANIFEST_SCHEMA, {"files": {}})


def _portable_root(root: Path) -> str:
    """A source root as a string that survives moving the vault to another machine when possible:
    ~-relative under the user's home, falling back to an absolute path (an external drive/folder
    genuinely can't be made portable without the user re-registering it there)."""
    try:
        rel = root.relative_to(Path.home())
    except ValueError:
        return str(root)
    return "~/" + rel.as_posix()


def resolve_root(vault: Path, entry: dict[str, Any]) -> Path:
    """The source's root as an absolute path, whatever format it is stored in: vault-relative
    (in-vault sources, resolved from the current vault path, never a stale absolute string),
    ~-relative, or a legacy absolute path (old external sources added before this existed)."""
    if entry.get("in_vault") and "root_rel" in entry:
        return resolve_in_vault(vault, PurePosixPath(entry["root_rel"]))
    root = entry.get("root", "")
    return Path(root).expanduser() if root.startswith("~") else Path(root)


def add_source(vault: Path, path: str, name: str, kind: str = "project", scope: str = "all") -> dict[str, Any]:
    """scope "docs": only documents and project files are read (the code can be added later with
    set_scope(..., "all")): a large project becomes knowledge in a few sessions, not hundreds."""
    require_vault(vault)
    if scope not in SCOPES:
        raise MnemoError("BAD_SCOPE", f"scope must be one of {SCOPES}")
    if not _NAME.match(name or ""):
        raise MnemoError("BAD_SOURCE_NAME", "name must be lowercase letters, digits, dashes (max 40)")
    if kind not in SOURCE_KINDS:
        raise MnemoError("BAD_SOURCE_KIND", f"kind must be one of {sorted(SOURCE_KINDS)}")
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise MnemoError("NO_SUCH_DIR", f"{root} is not a directory")
    if root == Path(vault).resolve() or Path(vault).resolve() in root.parents:
        raise MnemoError("SOURCE_IN_VAULT", "a source cannot live inside the vault")
    with VaultLock(vault) as lock:
        state = load_sources(vault)
        if name in state["sources"]:
            raise MnemoError("SOURCE_EXISTS", f"source {name!r} already registered")
        entry = {"root": _portable_root(root), "kind": kind, "added": now_iso(), **({"scope": scope} if scope != "all" else {})}
        state["sources"][name] = entry
        save_state(vault, lock, "sources.json", state)
    return {"name": name, **entry}


def add_obsidian(vault: Path, path: str, name: str) -> dict[str, Any]:
    """Adopt an existing Obsidian vault as a read-only source of the user's own notes: it is never written to; its notes are compiled like any source,
    without the external-source review. .obsidian/ and .trash/ are skipped."""
    root = Path(path).expanduser().resolve()
    if not (root / ".obsidian").is_dir():
        raise MnemoError("NOT_OBSIDIAN_VAULT", f"{root} has no .obsidian folder: it is not an Obsidian vault")
    result = add_source(vault, str(root), name, "notes")
    notes = sum(1 for p in root.rglob("*.md") if ".obsidian" not in p.parts and ".trash" not in p.parts)
    return {**result, "notes": notes, "next": f"mnemoloci compile plan --source {name}"}


def retire_source(vault: Path, name: str, *, reason: str, retired: bool = True) -> dict[str, Any]:
    """Stop following a source: it is no longer
    scanned and its pages are no longer reported stale or orphaned; nothing is deleted.
    Pages built only from it are listed so the user can decide about them."""
    require_vault(vault)
    with VaultLock(vault) as lock:
        state = load_sources(vault)
        entry = state["sources"].get(name)
        if entry is None:
            raise MnemoError("NO_SUCH_SOURCE", f"unknown source {name!r}")
        if retired:
            entry.update(retired=now_iso(), retired_reason=(reason or "").strip() or None)
        else:
            entry.pop("retired", None)
            entry.pop("retired_reason", None)
        save_state(vault, lock, "sources.json", state)
    from .pages import iter_pages

    only, partly = [], []
    for page in iter_pages(vault, ("wiki",)):
        keys = [str(s.get("key", "")) for s in page.frontmatter.get("sources") or [] if isinstance(s, dict)] if not page.error else []
        mine = [k for k in keys if k.split("/", 1)[0] == name]
        if mine:
            (only if len(mine) == len(keys) else partly).append(page.path)
    return {"source": name, "retired": retired, "pages_only_from_it": only, "pages_partly_from_it": partly,
            "note": "pages stay; retract or edit them only if the user wants" if retired else "followed again"}


def active_sources(registry: dict[str, Any]) -> dict[str, Any]:
    return {n: e for n, e in registry.items() if not e.get("retired")}


def source_root(vault: Path, name: str) -> Path:
    entry = load_sources(vault)["sources"].get(name)
    if entry is None:
        raise MnemoError("NO_SUCH_SOURCE", f"unknown source {name!r}")
    return resolve_root(vault, entry)


def _git_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = Path(dirpath).relative_to(root)
        for fname in filenames:
            found.append((rel_dir / fname).as_posix())
    return found


def _is_utf8_text(path: Path) -> bool:
    with open(path, "rb") as handle:
        sample = handle.read(8192)
    if b"\0" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start >= len(sample) - 4  # a multibyte char cut by the sample boundary
    return True


def _venv_dirs(root: Path, files: list[str]) -> set[str]:
    """Folders that are Python virtual environments, whatever their name (.venv_broken_py312,
    venv-tools...): they hold a pyvenv.cfg. Their thousands of files are libraries, not the project."""
    return {str(PurePosixPath(rel).parent) for rel in files if PurePosixPath(rel).name == "pyvenv.cfg"} | {
        d.name for d in root.iterdir() if d.is_dir() and (d / "pyvenv.cfg").is_file()}


def _walk_all(root: Path, rel: str) -> list[str]:
    """Every file under root/rel, without the automatic pruning (a folder the user forced in)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root / rel):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fname in filenames:
            found.append((Path(dirpath) / fname).relative_to(root).as_posix())
    return found


def _under(rel: str, folders: list[str]) -> int:
    """Length of the longest folder in `folders` that contains rel (0: none)."""
    return max((len(f) + 1 for f in folders if rel == f or rel.startswith(f + "/")), default=0)


def classify_source(root: Path, max_bytes: int, scope: str = "all", exclude: list[str] | None = None,
                    include: list[str] | None = None, *, hash_files: bool = True) -> list[dict[str, Any]]:
    """Every candidate file of a source with the reason it is skipped (None: it is read).
    Rules, in order: the user's choices (exclude / include, the most specific wins), then the
    automatic ones (tool folders, virtualenvs), secrets, type, scope, size, binary content."""
    exclude, include = list(exclude or []), list(include or [])
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    for folder in include:  # forced in: listed even if git ignores it or the walk prunes it
        if (root / folder).is_dir():
            files += _walk_all(root, folder)
    venvs = _venv_dirs(root, files)
    details = []
    for rel in sorted(set(files)):
        parts = PurePosixPath(rel).parts
        inc, exc = _under(rel, include), _under(rel, exclude)
        reason = None
        if exc > inc:
            reason = "excluded-by-user"
        elif not inc and any(p in SKIP_DIRS for p in parts[:-1]):
            reason = "excluded-dir"
        elif not inc and any("/".join(parts[:i]) in venvs for i in range(1, len(parts))):
            reason = "virtualenv"
        elif SECRET_FILE.search(rel):
            reason = "secret-file"
        priority = _priority(rel) if reason is None else None
        if reason is None and priority is None:
            reason = "unsupported-type"
        if reason is None and scope == "docs" and priority > 1:
            reason = "code-not-in-scope"  # config and code: added when the source's scope becomes "all"
        size, sha = None, None
        if reason is None:
            path = root / rel
            try:
                st = os.lstat(path)
            except OSError:
                st = None
            if st is None:
                reason = "unreadable"
            elif not os.path.isfile(path) or os.path.islink(path):
                reason = "not-regular"
            elif st.st_size == 0:
                reason = "empty"
            elif st.st_size > max_bytes:
                reason = "too-large"
            elif not _is_utf8_text(path):
                reason = "binary-or-not-utf8"
            else:
                size = st.st_size
                if hash_files:
                    sha = sha256_file(path)
        if priority is None:
            priority = _priority(rel)
        details.append({"rel": rel, "reason": reason, "priority": priority, "size": size, "sha256": sha})
    return details


def enumerate_source(root: Path, max_bytes: int, scope: str = "all", exclude: list[str] | None = None,
                     include: list[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept, skipped = [], {}
    for d in classify_source(root, max_bytes, scope, exclude, include):
        if d["reason"] is None:
            kept.append({"rel": d["rel"], "size": d["size"], "priority": d["priority"], "sha256": d["sha256"]})
        else:
            skipped[d["reason"]] = skipped.get(d["reason"], 0) + 1
    return kept, skipped


def scan(vault: Path, name: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    max_bytes = load_config(vault)["ingest"]["max_file_bytes"]
    registry = load_sources(vault)["sources"]
    manifest = load_manifest(vault)["files"]
    names = [name] if name else sorted(active_sources(registry))
    report = {}
    for src in names:
        if src not in registry:
            raise MnemoError("NO_SUCH_SOURCE", f"unknown source {src!r}")
        entry = registry[src]
        if entry.get("retired"):
            report[src] = {"root": str(resolve_root(vault, entry)), "retired": entry["retired"], "files": 0,
                           "changed": [], "unchanged": 0, "missing": [], "skipped": {}}
            continue
        root = resolve_root(vault, entry)
        if not root.is_dir():
            report[src] = {"error": f"source root missing: {root}"}
            continue
        files, skipped = enumerate_source(root, max_bytes, entry.get("scope", "all"), entry.get("exclude"),
                                          entry.get("include"))
        current = {f"{src}/{f['rel']}": f for f in files}
        changed = []
        for key, f in current.items():
            known = manifest.get(key)
            if known is None or known["sha256"] != f["sha256"]:
                changed.append({"key": key, "source": src, "kind": entry["kind"], "status": "new" if known is None else "modified", **f})
        missing = sorted(k for k, v in manifest.items() if k.startswith(f"{src}/") and k not in current and not v.get("missing")
                         and not (root / k[len(src) + 1:]).is_file())  # still there, only out of scope: not missing
        report[src] = {
            "root": str(root),
            "files": len(current),
            "changed": sorted(changed, key=lambda c: (c["priority"], c["key"])),
            "unchanged": len(current) - len(changed),
            "missing": missing,
            "skipped": skipped,
        }
    if name is None:  # a full scan: remember what waits to be compiled, for the session start and Oggi
        from .core.atomic import atomic_json
        from .vault import engine

        atomic_json(engine(vault) / "pending_sources.json", {
            "at": now_iso(), "changed": {s: len(r.get("changed", [])) for s, r in report.items() if r.get("changed")}})
    return report


def pending_summary(vault: Path) -> dict[str, Any] | None:
    """Changed files per source as of the last full scan (cheap: no hashing)."""
    import json

    from .vault import engine

    try:
        return json.loads((engine(vault) / "pending_sources.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None


def set_scope(vault: Path, name: str, scope: str) -> dict[str, Any]:
    """Widen (or narrow) what a source contributes: "docs" or "all". Narrowing never deletes pages:
    files that leave the scope are simply no longer scanned."""
    require_vault(vault)
    if scope not in SCOPES:
        raise MnemoError("BAD_SCOPE", f"scope must be one of {SCOPES}")
    with VaultLock(vault) as lock:
        state = load_sources(vault)
        entry = state["sources"].get(name)
        if entry is None:
            raise MnemoError("NO_SUCH_SOURCE", f"unknown source {name!r}")
        if scope == "all":
            entry.pop("scope", None)
        else:
            entry["scope"] = scope
        save_state(vault, lock, "sources.json", state)
    return {"name": name, "scope": scope, "next": f"mnemoloci scan --source {name}"}


def set_exclude(vault: Path, name: str, folders: list[str], *, add: bool = True) -> dict[str, Any]:
    """Subfolders of a source that are not the user's knowledge (a vendored repo, a toolchain,
    generated output): skipped by scan and compile. The source folder itself is never touched."""
    require_vault(vault)
    cleaned = []
    for folder in folders:
        rel = str(folder).replace("\\", "/").strip().strip("/")
        if not rel or rel.startswith("..") or ":" in rel or rel.startswith("/"):
            raise MnemoError("BAD_EXCLUDE", f"{folder!r}: give a folder relative to the source, e.g. tools/vendor")
        cleaned.append(rel)
    with VaultLock(vault) as lock:
        state = load_sources(vault)
        entry = state["sources"].get(name)
        if entry is None:
            raise MnemoError("NO_SUCH_SOURCE", f"unknown source {name!r}")
        current = set(entry.get("exclude") or [])
        current = current | set(cleaned) if add else current - set(cleaned)
        if current:
            entry["exclude"] = sorted(current)
        else:
            entry.pop("exclude", None)
        save_state(vault, lock, "sources.json", state)
    return {"name": name, "exclude": sorted(current)}


def free_name(vault: Path, folder: str) -> str:
    """A source name from a folder name ("Il mio Vault" -> il-mio-vault), made unique."""
    from .markdown import slugify

    base = re.sub(r"[^a-z0-9-]+", "-", slugify(Path(folder).name).encode("ascii", "ignore").decode()).strip("-")[:36]
    base = base if _NAME.match(base or "") else "vault"
    taken = load_sources(vault)["sources"]
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def overview(vault: Path) -> dict[str, Any]:
    """Every registered source with what waits to be compiled, as of its last check (no hashing)."""
    require_vault(vault)
    pending = pending_summary(vault) or {}
    checked = pending.get("checked") or {}
    out = []
    for name, entry in sorted(load_sources(vault)["sources"].items()):
        root = resolve_root(vault, entry)
        out.append({"name": name, "kind": entry["kind"], "root": str(root), "added": entry.get("added"),
                    "scope": entry.get("scope", "all"), "exclude": entry.get("exclude") or [],
                    "retired": entry.get("retired"), "exists": root.is_dir(),
                    "to_compile": (pending.get("changed") or {}).get(name, 0),
                    "checked": checked.get(name) or pending.get("at")})
    return {"sources": out, "last_full_check": pending.get("at")}


def provenance_report(vault: Path) -> dict[str, Any]:
    """Which vault pages came from which source: grouped by
    source kind and by exact source name (to distinguish claude-sessions from codex-sessions from
    chats, all kind "sessions"), which pages are built from only one source/kind, and which have a
    citation whose source file has changed since the page was written (sha256 already carried on
    every source entry, so this staleness check is free)."""
    require_vault(vault)
    from .pages import iter_pages

    registry = load_sources(vault)["sources"]
    by_kind: dict[str, list[str]] = {}
    by_source: dict[str, list[str]] = {}
    unique_to_kind: dict[str, list[str]] = {}
    unique_to_source: dict[str, list[str]] = {}
    stale_by_source: dict[str, list[str]] = {}
    file_sha: dict[str, str | None] = {}

    def current_sha(key: str) -> str | None:
        if key not in file_sha:
            sname, _, rel = key.partition("/")
            entry = registry.get(sname)
            path = resolve_root(vault, entry) / rel if entry and rel else None
            file_sha[key] = sha256_file(path) if path and path.is_file() else None
        return file_sha[key]

    for page in iter_pages(vault, ("wiki",)):
        if page.error:
            continue
        items = [s for s in page.frontmatter.get("sources") or [] if isinstance(s, dict) and s.get("key")]
        names, kinds, stale_names = set(), set(), set()
        for s in items:
            key = str(s["key"])
            name = key.partition("/")[0]
            names.add(name)
            kinds.add((registry.get(name) or {}).get("kind", "unknown"))
            sha = current_sha(key)
            if sha is not None and sha != s.get("sha256"):
                stale_names.add(name)
        for name in names:
            by_source.setdefault(name, []).append(page.path)
        for kind in kinds:
            by_kind.setdefault(kind, []).append(page.path)
        if len(names) == 1:
            unique_to_source.setdefault(next(iter(names)), []).append(page.path)
        if len(kinds) == 1:
            unique_to_kind.setdefault(next(iter(kinds)), []).append(page.path)
        for name in stale_names:
            stale_by_source.setdefault(name, []).append(page.path)

    return {
        "by_kind": {k: sorted(v) for k, v in sorted(by_kind.items())},
        "by_source": {k: sorted(v) for k, v in sorted(by_source.items())},
        "unique_to_kind": {k: sorted(v) for k, v in sorted(unique_to_kind.items())},
        "unique_to_source": {k: sorted(v) for k, v in sorted(unique_to_source.items())},
        "stale_by_source": {k: sorted(v) for k, v in sorted(stale_by_source.items())},
    }


def check(vault: Path, name: str) -> dict[str, Any]:
    """Scan one source now and remember its count next to the last full scan's."""
    from .core.atomic import atomic_json
    from .vault import engine

    report = scan(vault, name)[name]
    if "error" in report:
        raise MnemoError("SOURCE_MISSING", report["error"])
    pending = pending_summary(vault) or {"at": None, "changed": {}}
    pending.setdefault("changed", {})[name] = len(report["changed"])
    if not pending["changed"][name]:
        pending["changed"].pop(name)
    pending.setdefault("checked", {})[name] = now_iso()
    atomic_json(engine(vault) / "pending_sources.json", pending)
    return {"name": name, "files": report["files"], "to_compile": len(report["changed"]),
            "missing": len(report["missing"])}
