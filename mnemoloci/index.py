"""Derived SQLite search index. The vault is the source of truth: this file can
always be rebuilt from the Markdown pages and is never authoritative."""

import json
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .core.errors import MnemoError
from .core.lock import VaultLock
from .markdown import slugify, wikilink_targets
from .pages import Page, iter_pages, load_page
from .vault import engine, require_vault

SCHEMA_VERSION = "2"  # 2: names table (links resolved through titles and aliases)
_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pages(
    path TEXT PRIMARY KEY, slug TEXT, title TEXT, kind TEXT, summary TEXT, aliases TEXT,
    confidence REAL, provenance TEXT, sha256 TEXT, updated TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    path UNINDEXED, title, aliases, summary, body, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS links(src_path TEXT, dst_slug TEXT);
CREATE INDEX IF NOT EXISTS links_src ON links(src_path);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst_slug);
CREATE INDEX IF NOT EXISTS pages_slug ON pages(slug);
CREATE TABLE IF NOT EXISTS names(key TEXT, slug TEXT, path TEXT);
CREATE INDEX IF NOT EXISTS names_key ON names(key);
CREATE INDEX IF NOT EXISTS names_path ON names(path);
"""


def db_path(vault: Path) -> Path:
    return engine(vault) / "index.sqlite"


def _connect(vault: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(vault))
    conn.executescript(_DDL)
    return conn


def _upsert(conn: sqlite3.Connection, page: Page) -> None:
    _remove(conn, page.path)
    if page.error or page.merged_into:  # a merge stub is not searchable: its page is
        return
    fm = page.frontmatter
    confidence = fm.get("confidence")
    conn.execute(
        "INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            page.path, page.slug, page.title, str(fm.get("kind", "")), str(fm.get("summary", "")),
            json.dumps(page.aliases, ensure_ascii=False),
            float(confidence) if isinstance(confidence, (int, float)) else None,
            str(fm.get("provenanceState", "")), page.sha256, str(fm.get("updated", "")),
        ),
    )
    conn.execute(
        "INSERT INTO pages_fts VALUES(?,?,?,?,?)",
        (page.path, page.title, " ".join(page.aliases), str(fm.get("summary", "")), page.body),
    )
    conn.executemany(
        "INSERT INTO links VALUES(?,?)", [(page.path, t) for t in sorted(set(wikilink_targets(page.body)))]
    )
    # every name a [[link]] can use for this page: its slug, title and aliases
    keys = {page.slug, slugify(page.title), *(slugify(a) for a in page.aliases)} - {"untitled"}
    conn.executemany("INSERT INTO names VALUES(?,?,?)", [(k, page.slug, page.path) for k in sorted(keys)])


def _remove(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM pages WHERE path=?", (path,))
    conn.execute("DELETE FROM pages_fts WHERE path=?", (path,))
    conn.execute("DELETE FROM links WHERE src_path=?", (path,))
    conn.execute("DELETE FROM names WHERE path=?", (path,))


def rebuild(vault: Path, lock: VaultLock | None = None) -> dict[str, Any]:
    require_vault(vault)
    own = None
    if lock is None:
        own = lock = VaultLock(vault)
        lock.acquire()
    try:
        with closing(_connect(vault)) as conn, conn:
            # one transaction: a rebuild killed half-way (a hook timing out) leaves the old index,
            # and a first build leaves no "schema" row, so the next non-hook caller builds it again
            conn.execute("DELETE FROM meta WHERE key='schema'")
            for table in ("pages", "pages_fts", "links", "names"):
                conn.execute(f"DELETE FROM {table}")
            count = 0
            for page in iter_pages(vault):
                _upsert(conn, page)
                count += 1
            conn.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
        return {"indexed": count}
    finally:
        if own:
            own.release()


def ready(vault: Path) -> bool:
    """The index exists, was built to the end, and has the current layout."""
    if not db_path(vault).exists():
        return False
    try:
        with closing(sqlite3.connect(db_path(vault))) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == SCHEMA_VERSION


def update(vault: Path, paths: Iterable[str], lock: VaultLock) -> None:
    lock.assert_held()
    if not ready(vault):
        rebuild(vault, lock)
        return
    with closing(_connect(vault)) as conn, conn:
        for rel in paths:
            if (Path(vault) / rel).is_file():
                _upsert(conn, load_page(vault, rel))
            else:
                _remove(conn, rel)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text, flags=re.UNICODE)[:32]


def _fts_query(text: str) -> str | None:
    tokens = _tokens(text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def _rerank(text: str, rows: list[dict[str, Any]], aliases: dict[str, list[str]]) -> list[dict[str, Any]]:
    """BM25 alone is unreliable on small vaults: FTS5 clamps the IDF of a term that
    appears in more than half the pages to ~0. Rank first by exact title/alias
    match, then by how many query words the title/aliases/summary cover, then BM25."""
    wanted = {_fold(t) for t in _tokens(text)}
    target = slugify(text)

    def key(row: dict[str, Any]) -> tuple:
        names = [row["title"], *aliases.get(row["path"], [])]
        head = set(_fold(t) for t in _tokens(" ".join(names) + " " + (row["summary"] or "")))
        return (not _exact(target, row, aliases), -len(wanted & head), -row["score"], row["slug"])

    return sorted(rows, key=key)


def _exact(target: str, row: dict[str, Any], aliases: dict[str, list[str]]) -> bool:
    names = [row["title"], *aliases.get(row["path"], [])]
    return target in {slugify(n) for n in names} or target == row["slug"]


RRF_K = 60  # standard Reciprocal Rank Fusion constant


def _semantic_mode(vault: Path, mode: str) -> bool:
    if mode == "lexical":
        return False
    from . import embed

    usable = embed.available(vault) and embed.has_vectors(vault)
    if mode == "hybrid" and not usable:
        raise MnemoError("NO_EMBEDDINGS", "run `mnemoloci index embed` first (needs the semantic extra)")
    return usable


def _row(r: tuple) -> dict[str, Any]:
    return {"path": r[0], "slug": r[1], "title": r[2], "kind": r[3], "summary": r[4], "confidence": r[5],
            "snippet": (r[7] or "").strip() or r[4], "score": -r[8] if r[8] is not None else 0.0}


def _in_time(vault: Path, results: list[dict[str, Any]], as_of: str | None,
            include_expired: bool = False) -> list[dict[str, Any]]:
    """With as_of: only pages valid on that date. Without: pages expired as of today are excluded
    by default (nothing is deleted -- pass include_expired=True to get them back, still flagged
    with `expired`/`superseded_by` so the caller knows why)."""
    today = as_of or time.strftime("%Y-%m-%d")
    kept = []
    for r in results:
        fm = load_page(vault, r["path"]).frontmatter
        start, end = str(fm.get("validFrom") or ""), str(fm.get("validUntil") or "")
        if as_of and ((start and start > as_of) or (end and end < as_of)):
            continue
        expired = bool(end) and end < today
        if expired and not include_expired:
            continue
        if expired:
            r["expired"] = end
            if fm.get("supersededBy"):
                r["superseded_by"] = fm["supersededBy"]
        kept.append(r)
    return kept


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def query(vault: Path, text: str, *, limit: int = 10, expand: bool = True, mode: str = "auto",
          as_of: str | None = None, include_expired: bool = False, build: bool = True) -> dict[str, Any]:
    """mode: auto (hybrid when vectors exist) | lexical | hybrid (BM25 + embeddings, fused by RRF).
    as_of (YYYY-MM-DD): only pages valid on that date (validFrom/validUntil).
    include_expired: pages expired as of today are excluded by default; pass True to get them
    back (still flagged `expired`/`superseded_by`). Ignored when as_of is given.
    build=False (hooks): never build the index here; with no usable index, no results."""
    require_vault(vault)
    if as_of and not _DATE.match(as_of):
        raise MnemoError("BAD_DATE", "as_of must be YYYY-MM-DD")
    if not ready(vault):
        if not build:
            return {"query": text, "results": [], "related": [], "mode": "lexical", "index": "not built"}
        rebuild(vault)
    match = _fts_query(text)
    if match is None:
        return {"query": text, "results": [], "mode": "lexical"}
    semantic = _semantic_mode(vault, mode)
    pool = max(limit * 3, 30)
    with closing(_connect(vault)) as conn:
        rows = conn.execute(
            """SELECT p.path, p.slug, p.title, p.kind, p.summary, p.confidence, p.aliases,
                      snippet(pages_fts, 4, '[', ']', '…', 14), bm25(pages_fts, 0, 10.0, 6.0, 3.0, 1.0) AS score
               FROM pages_fts JOIN pages p ON p.path = pages_fts.path
               WHERE pages_fts MATCH ? ORDER BY score LIMIT ?""",
            (match, pool),
        ).fetchall()
        candidates = {r[0]: _row(r) for r in rows}
        aliases = {r[0]: json.loads(r[6] or "[]") for r in rows}
        lexical = _rerank(text, list(candidates.values()), aliases)
        if semantic:
            from . import embed

            hits = embed.search(vault, text, limit=pool)
            missing = [p for p, _ in hits if p not in candidates]
            for r in conn.execute(
                f"""SELECT path, slug, title, kind, summary, confidence, aliases, summary, NULL FROM pages
                    WHERE path IN ({','.join('?' * len(missing))})""", missing).fetchall() if missing else []:
                candidates[r[0]] = _row(r)
                aliases[r[0]] = json.loads(r[6] or "[]")
            fused: dict[str, float] = {}
            for rank, row in enumerate(lexical):
                fused[row["path"]] = fused.get(row["path"], 0.0) + 1 / (RRF_K + rank + 1)
            for rank, (path, cosine) in enumerate(hits):
                fused[path] = fused.get(path, 0.0) + 1 / (RRF_K + rank + 1)
                candidates[path]["semantic"] = round(cosine, 4)
            target = slugify(text)
            ordered = sorted(fused, key=lambda p: (not _exact(target, candidates[p], aliases), -fused[p], p))
            results = [dict(candidates[p], score=fused[p]) for p in ordered]
        else:
            results = lexical
        # filter by date over the whole candidate pool, then cut: filtering (as_of, or expiry
        # exclusion by default) must not shrink the result count below `limit` if more qualify
        results = _in_time(vault, results, as_of, include_expired)[:limit]
        for r in results:
            r["score"] = float(f"{r['score']:.6g}")
        related = []
        if expand and results:
            seen = {r["slug"] for r in results}
            for hit in results[:3]:
                neighbours = conn.execute(
                    """SELECT p.path, p.slug, p.title, p.summary FROM links l JOIN names n ON n.key = l.dst_slug
                       JOIN pages p ON p.slug = n.slug WHERE l.src_path = ?
                       UNION
                       SELECT p.path, p.slug, p.title, p.summary FROM links l JOIN names n ON n.key = l.dst_slug
                       JOIN pages p ON p.path = l.src_path WHERE n.slug = ?""",
                    (hit["path"], hit["slug"]),
                ).fetchall()
                for path, slug, title, summary in neighbours:
                    if slug not in seen:
                        seen.add(slug)
                        related.append({"path": path, "slug": slug, "title": title, "summary": summary, "via": hit["slug"]})
    return {"query": text, "results": results, "related": related, "mode": "hybrid" if semantic else "lexical"}
