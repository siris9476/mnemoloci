"""Registry of every Agent Skill on this machine: Claude Code user skills, installed
plugins, marketplace catalogs (installable, not installed), ~/.agents/skills
(Kimi/Codex), a project's .claude/skills, and this vault's review folder.
Derived data in the vault's SQLite index; rebuilt by scanning."""

import json
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .core.errors import MnemoError, ValidationError
from .core.lock import VaultLock
from .core.paths import is_link_like
from .index import _fold, _tokens, db_path
from .markdown import slugify, split_frontmatter
from .sessions import claude_dirs
from .vault import require_vault

INSTALLED = {"claude", "plugin", "agents", "project"}
REVIEW_DIR = "_generated-skills"
STALE_SECONDS = 600
# Mnemoloci is itself installed as a local-path marketplace plugin (its marketplace.json
# points "source" at "./plugin", not a github checkout), so its skills never land under
# plugins/cache or plugins/marketplaces like a normal installed plugin. Scan its own source
# directly, otherwise `skills find/show/stocktake/triggers` can never see Mnemoloci's own skills.
_SELF_PLUGIN_SKILLS = Path(__file__).resolve().parent.parent / "plugin" / "skills"
_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS skills(path TEXT PRIMARY KEY, name TEXT, label TEXT, plugin TEXT,
    description TEXT, link INTEGER, error TEXT, mtime_ns INTEGER, size INTEGER, body TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(path UNINDEXED, name, description, body,
    tokenize='unicode61 remove_diacritics 2');
CREATE INDEX IF NOT EXISTS skills_name ON skills(name);
"""


def roots(vault: Path, project_dir: str | None = None) -> list[tuple[str, Path, str]]:
    out: list[tuple[str, Path, str]] = []
    for base in claude_dirs():
        out += [("claude", base / "skills", "*/SKILL.md"),
                ("plugin", base / "plugins" / "cache", "**/skills/*/SKILL.md"),
                ("marketplace", base / "plugins" / "marketplaces", "**/skills/*/SKILL.md")]
    out.append(("agents", Path.home() / ".agents" / "skills", "*/SKILL.md"))
    if project_dir:
        out.append(("project", Path(project_dir) / ".claude" / "skills", "*/SKILL.md"))
    out.append(("review", Path(vault) / REVIEW_DIR, "*/SKILL.md"))
    out.append(("plugin", _SELF_PLUGIN_SKILLS, "*/SKILL.md"))
    return out


def _plugin_name(label: str, path: Path, root: Path) -> str | None:
    if root == _SELF_PLUGIN_SKILLS:
        return "mnemoloci"
    if label not in ("plugin", "marketplace"):
        return None
    parts = path.relative_to(root).parts
    return "/".join(parts[:2]) if len(parts) > 3 else parts[0]


def read_skill(path: Path) -> dict[str, Any]:
    text = path.read_text("utf-8", errors="replace")[:65536]
    try:
        fm, body = split_frontmatter(text)
        error = None if fm else "no frontmatter"
    except ValidationError as exc:
        fm, body, error = None, text, exc.message
    fm = fm or {}
    name = fm.get("name") if isinstance(fm.get("name"), str) else path.parent.name
    description = fm.get("description") if isinstance(fm.get("description"), str) else ""
    return {"name": name.strip(), "description": " ".join(description.split()), "body": body[:2000], "error": error}


def _connect(vault: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(vault))
    columns = {r[1] for r in conn.execute("PRAGMA table_info(skills)")}
    if columns and "mtime_ns" not in columns:  # derived cache from an older layout: rebuild it
        conn.executescript("DROP TABLE skills; DROP TABLE IF EXISTS skills_fts;")
    conn.executescript(_DDL)
    return conn


def scan(vault: Path, project_dir: str | None = None) -> dict[str, Any]:
    """Incremental: a SKILL.md whose mtime and size are unchanged is not re-read."""
    require_vault(vault)
    counts: dict[str, int] = {}
    reread = 0
    with VaultLock(vault), closing(_connect(vault)) as conn, conn:
        cached = {r[0]: r for r in conn.execute(
            "SELECT path, name, description, error, mtime_ns, size, body FROM skills")}
        conn.execute("DELETE FROM skills")
        conn.execute("DELETE FROM skills_fts")
        seen: set[str] = set()
        for label, root, pattern in roots(vault, project_dir):
            if not root.is_dir():
                continue
            for path in sorted(root.glob(pattern)):
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = path.stat()
                    old = cached.get(key)
                    if old and old[4] == st.st_mtime_ns and old[5] == st.st_size:
                        info = {"name": old[1], "description": old[2], "error": old[3], "body": old[6]}
                    else:
                        info = read_skill(path)
                        reread += 1
                    mtime, size = st.st_mtime_ns, st.st_size
                except OSError as exc:
                    info = {"name": path.parent.name, "description": "", "body": "", "error": str(exc)}
                    mtime = size = None
                link = is_link_like(path.parent.lstat())
                conn.execute("INSERT INTO skills VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (key, info["name"], label, _plugin_name(label, path, root), info["description"],
                              int(link), info["error"], mtime, size, info["body"]))
                conn.execute("INSERT INTO skills_fts VALUES(?,?,?,?)", (key, info["name"], info["description"], info["body"]))
                counts[label] = counts.get(label, 0) + 1
        conn.execute("INSERT OR REPLACE INTO meta VALUES('skills_scanned_at', ?)", (str(time.time()),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('skills_project', ?)", (project_dir or "",))
    return {"skills": sum(counts.values()), "by_location": counts, "reread": reread}


def invalidate(vault: Path) -> None:
    """Force the next lookup to rescan (after we add or install a skill)."""
    with closing(_connect(vault)) as conn, conn:
        conn.execute("DELETE FROM meta WHERE key='skills_scanned_at'")


def _ensure_fresh(vault: Path, project_dir: str | None) -> None:
    with closing(_connect(vault)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='skills_scanned_at'").fetchone()
        proj = conn.execute("SELECT value FROM meta WHERE key='skills_project'").fetchone()
    if not row or time.time() - float(row[0]) > STALE_SECONDS or (proj[0] if proj else "") != (project_dir or ""):
        scan(vault, project_dir)


def _group(rows: list[tuple]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for path, name, label, plugin, description, link, error in rows:
        entry = grouped.setdefault(name, {"name": name, "description": description, "locations": []})
        if not entry["description"] and description:
            entry["description"] = description
        entry["locations"].append({"label": label, "path": path, "plugin": plugin, "link": bool(link), "error": error})
    for entry in grouped.values():
        labels = {loc["label"] for loc in entry["locations"]}
        entry["installed"] = bool(labels & INSTALLED)
        entry["in_review"] = "review" in labels
    return grouped


def find(vault: Path, query: str, *, limit: int = 10, project_dir: str | None = None,
         installed_only: bool = False) -> dict[str, Any]:
    require_vault(vault)
    _ensure_fresh(vault, project_dir)
    tokens = _tokens(query)
    if not tokens:
        raise MnemoError("EMPTY_QUERY", "nothing to search for")
    match = " OR ".join(f'"{t}"' for t in tokens)
    with closing(_connect(vault)) as conn:
        hits = conn.execute(
            "SELECT path, bm25(skills_fts, 0, 10.0, 4.0, 1.0) FROM skills_fts WHERE skills_fts MATCH ? ORDER BY 2 LIMIT 300",
            (match,),
        ).fetchall()
        score = {p: -s for p, s in hits}
        names = {r[0] for r in conn.execute(
            f"SELECT DISTINCT name FROM skills WHERE path IN ({','.join('?' * len(score))})", list(score)
        ).fetchall()} if score else set()
        rows = conn.execute(
            f"SELECT path, name, label, plugin, description, link, error FROM skills WHERE name IN ({','.join('?' * len(names))})",
            sorted(names),
        ).fetchall() if names else []
    grouped = _group(rows)
    wanted, target = {_fold(t) for t in tokens}, slugify(query)

    def rank(entry: dict[str, Any]) -> tuple:
        in_name = {_fold(t) for t in _tokens(entry["name"].replace("-", " ").replace("_", " "))}
        in_desc = {_fold(t) for t in _tokens(entry["description"])}
        best = max((score.get(loc["path"], 0.0) for loc in entry["locations"]), default=0.0)
        return (slugify(entry["name"]) != target, -len(wanted & in_name), -len(wanted & in_desc),
                not entry["installed"], -best, entry["name"])

    results = sorted(grouped.values(), key=rank)
    if installed_only:
        results = [r for r in results if r["installed"]]
    for r in results:
        if len(r["description"]) > 300:
            r["description"] = r["description"][:299] + "…"
    return {"query": query, "results": results[:limit], "total_matches": len(results)}


def show(vault: Path, name: str, project_dir: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    _ensure_fresh(vault, project_dir)
    with closing(_connect(vault)) as conn:
        rows = conn.execute("SELECT path, name, label, plugin, description, link, error FROM skills WHERE name = ?",
                            (name,)).fetchall()
    if not rows:
        raise MnemoError("NO_SUCH_SKILL", f"no skill named {name!r} (try: mnemoloci skills find {name})")
    entry = _group(rows)[name]
    preferred = sorted(entry["locations"], key=lambda loc: (loc["label"] not in INSTALLED, loc["label"] != "review"))[0]
    text = Path(preferred["path"]).read_text("utf-8", errors="replace")
    entry["preview_from"] = preferred["path"]
    entry["preview"] = "\n".join(text.splitlines()[:80])
    return entry


def remote(query: str, limit: int = 10) -> dict[str, Any]:
    """Search public GitHub for SKILL.md files. Read-only."""
    if not shutil.which("gh"):
        raise MnemoError("GH_UNAVAILABLE", "GitHub CLI (gh) not found on PATH")
    try:
        out = subprocess.run(
            ["gh", "search", "code", query, "--filename", "SKILL.md", "--limit", str(limit),
             "--json", "repository,path,url"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise MnemoError("GH_FAILED", f"gh search failed: {exc}") from exc
    items = json.loads(out or "[]")
    return {"query": query, "results": [
        {"repository": i["repository"]["nameWithOwner"], "path": i["path"], "url": i["url"]} for i in items
    ], "note": "unvetted third-party code: audit before installing anything"}


def name_locations(vault: Path, name: str) -> list[dict[str, Any]]:
    _ensure_fresh(vault, None)
    with closing(_connect(vault)) as conn:
        rows = conn.execute("SELECT path, name, label, plugin, description, link, error FROM skills WHERE name = ?",
                            (name,)).fetchall()
    return _group(rows)[name]["locations"] if rows else []
