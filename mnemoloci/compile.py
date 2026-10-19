"""Two-phase compile, with Claude Code as the LLM.

plan    -> numbered, secret-redacted packets for every changed source file
(Claude writes one extract JSON per packet)
merge   -> ALL extracts must exist; concepts are grouped deterministically across
           every source and existing page before a single page is written
(Claude writes one page per job)
submit  -> validate; safe pages go live in one transaction, risky ones are held
           in the review queue; manifest is updated only after success
"""

import json
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .core import txn
from .core.atomic import atomic_json
from .core.errors import MnemoError
from .core.hashing import canonical_hash, sha256_bytes, sha256_file
from .core.lock import VaultLock
from .inbox import PENDING as INBOX_PENDING
from .index import update as index_update
from .markdown import CITATION, slugify
from .pages import alias_map, iter_pages
from .publish import build_writes, stamp_now
from .review import save_candidate
from .secrets import redact
from . import content_guard
from .sources import EXTERNAL_KINDS, load_manifest, load_sources, resolve_root, scan
from .validate import ENTITY_TYPES, LineCounter, check_page, independent_sources
from .vault import KIND_DIRS, engine, load_config, now_iso, require_vault, save_state, valid_language

RUN_SCHEMA = "mnemoloci.run.v1"
PACKET_SCHEMA = "mnemoloci.packet.v1"
EXTRACT_SCHEMA = "mnemoloci.extract.v1"
JOB_SCHEMA = "mnemoloci.job.v1"
CHUNK_LINES = 300
CHUNK_CHARS = 12_000
KIND_ORDER = ["concept", "entity", "comparison", "overview"]
EXTRACT_PROVENANCE = {"extracted", "inferred", "ambiguous"}


def _work(vault: Path) -> Path:
    return engine(vault) / "work"


def _active_run_id(vault: Path) -> str | None:
    try:
        return json.loads((_work(vault) / "active.json").read_text("utf-8")).get("run_id")
    except FileNotFoundError:
        return None


def _set_active(vault: Path, run_id: str | None) -> None:
    atomic_json(_work(vault) / "active.json", {"run_id": run_id})


def _load_run(vault: Path) -> dict[str, Any]:
    run_id = _active_run_id(vault)
    if not run_id:
        raise MnemoError("NO_ACTIVE_RUN", "no compile run in progress (run: mnemoloci compile plan)")
    return json.loads((_work(vault) / run_id / "run.json").read_text("utf-8"))


def _save_run(vault: Path, run: dict[str, Any]) -> None:
    atomic_json(_work(vault) / run["run_id"] / "run.json", run)


SOUL_PATH = "_meta/SOUL.md"
SOUL_CHARS = 4_000


def editorial_instructions(vault: Path) -> str | None:
    """The user's own writing guidelines for the wiki: tone, what to emphasise, what to leave out. Carried into every packet and job."""
    path = Path(vault) / SOUL_PATH
    if not path.is_file():
        return None
    text = path.read_text("utf-8", errors="replace").strip()
    return text[:SOUL_CHARS] or None


def set_instructions(vault: Path, text: str) -> dict[str, Any]:
    require_vault(vault)
    text = (text or "").strip()
    if not text:
        raise MnemoError("EMPTY_TEXT", "the instructions are empty")
    with VaultLock(vault) as lock:
        target = Path(vault) / SOUL_PATH
        entry = {"path": SOUL_PATH, "action": "create", "content": text + "\n"}
        if target.is_file():
            entry = {"path": SOUL_PATH, "action": "replace", "content": text + "\n", "expected_sha256": sha256_file(target)}
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"soul-{time.time_ns()}", "operation": "instructions",
                  "writes": [entry]}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    return {"path": SOUL_PATH, "chars": len(text), "truncated_in_packets": len(text) > SOUL_CHARS,
            "note": "used from the next compile plan; existing pages are not rewritten automatically"}


def _chunks(lines: list[str]) -> list[dict[str, Any]]:
    chunks, start, buf, size = [], 1, [], 0
    for number, line in enumerate(lines, 1):
        numbered = f"{number:>5}| {line}"
        if buf and (len(buf) >= CHUNK_LINES or size + len(numbered) > CHUNK_CHARS):
            chunks.append({"start_line": start, "end_line": number - 1, "text": "\n".join(buf)})
            start, buf, size = number, [], 0
        buf.append(numbered)
        size += len(numbered) + 1
    if buf:
        chunks.append({"start_line": start, "end_line": start + len(buf) - 1, "text": "\n".join(buf)})
    return chunks


_TREE_SITTER_LANG = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".cs": "c_sharp", ".java": "java", ".kt": "kotlin",
    ".kts": "kotlin", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".swift": "swift", ".css": "css", ".scss": "css",
    ".html": "html", ".lua": "lua", ".scala": "scala", ".vue": "vue",
}


def _ast_boundaries(lines: list[str], suffix: str) -> set[int] | None:
    """Line numbers (1-based) right after which a top-level definition ends -- the only places
    an AST-aware cut is allowed. None whenever there's no grammar for `suffix`, tree-sitter isn't
    installed, or parsing fails: a chunking hiccup must never be the reason a file fails to
    compile, so the caller falls back to the plain line-based `_chunks` silently."""
    lang = _TREE_SITTER_LANG.get(suffix)
    if not lang:
        return None
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return None
    try:
        tree = get_parser(lang).parse("\n".join(lines).encode("utf-8"))
        return {node.end_point[0] + 1 for node in tree.root_node.children}
    except Exception:  # noqa: BLE001 - any parser error: no boundaries, fall back to _chunks
        return None


def _chunks_ast(lines: list[str], boundaries: set[int]) -> list[dict[str, Any]]:
    """Same shape and size caps as `_chunks`, but a cut only ever lands on a boundary (right after
    a complete function/class), never mid-definition -- except the hard ceiling below, so one
    pathologically long definition (a huge generated file) still can't produce one giant chunk."""
    allowed, ceiling = boundaries | {len(lines)}, CHUNK_LINES * 4
    chunks, start, buf, size = [], 1, [], 0
    for number, line in enumerate(lines, 1):
        numbered = f"{number:>5}| {line}"
        buf.append(numbered)
        size += len(numbered) + 1
        big_enough = len(buf) >= CHUNK_LINES or size > CHUNK_CHARS
        if (number in allowed and big_enough) or len(buf) >= ceiling:
            chunks.append({"start_line": start, "end_line": number, "text": "\n".join(buf)})
            start, buf, size = number + 1, [], 0
    if buf:
        chunks.append({"start_line": start, "end_line": start + len(buf) - 1, "text": "\n".join(buf)})
    return chunks


def plan(vault: Path, *, source: str | None = None, limit: int = 25, stale_only: bool = False) -> dict[str, Any]:
    """stale_only: only changed files that existing pages cite, i.e. refresh the pages that went stale
   , leaving brand-new material for a normal run."""
    require_vault(vault)
    if _active_run_id(vault):
        raise MnemoError("RUN_IN_PROGRESS", f"run {_active_run_id(vault)} is active (compile status / abandon)")
    # hashing the sources can take long on big projects: done before taking the lock, so hooks and
    # the app are not kept waiting; every file is re-hashed when it is read below
    report = scan(vault, source)
    with VaultLock(vault) as lock:
        if _active_run_id(vault):
            raise MnemoError("RUN_IN_PROGRESS", f"run {_active_run_id(vault)} is active (compile status / abandon)")
        changed = sorted(
            (c for src in report.values() for c in src.get("changed", [])),
            key=lambda c: (c["priority"], c["key"]),
        )
        if stale_only:
            cited = {s.get("key") for p in iter_pages(vault, ("wiki",)) if not p.error
                     for s in p.frontmatter.get("sources") or [] if isinstance(s, dict)}
            changed = [c for c in changed if c["key"] in cited]
        missing = [k for src in report.values() for k in src.get("missing", [])]
        if missing:
            manifest = load_manifest(vault)
            for key in missing:
                manifest["files"][key]["missing"] = True
            save_state(vault, lock, "manifest.json", manifest)
        selected = changed[: max(1, limit)]
        if not selected:
            return {"run_id": None, "message": "nothing to compile", "missing": missing}

        run_id = time.strftime("r%Y%m%d-%H%M%S")
        while (_work(vault) / run_id).exists():
            run_id += "x"
        run_dir = _work(vault) / run_id
        for sub in ("packets", "extract", "jobs", "pages"):
            (run_dir / sub).mkdir(parents=True)
        roots = {n: resolve_root(vault, e) for n, e in load_sources(vault)["sources"].items()}
        soul = editorial_instructions(vault)
        files, awaiting = [], []
        for i, c in enumerate(selected):
            path = roots[c["source"]] / c["rel"]
            data = path.read_bytes()
            if sha256_bytes(data) != c["sha256"]:
                continue  # changed between scan and read; next run picks it up
            if c["kind"] == "inbox" and INBOX_PENDING.encode("utf-8") in data:
                awaiting.append(c["key"])  # screenshot not transcribed yet
                continue
            text, redactions = redact(data.decode("utf-8", errors="replace"))
            lines = text.splitlines()
            boundaries = _ast_boundaries(lines, Path(c["rel"]).suffix.lower())
            chunks = _chunks_ast(lines, boundaries) if boundaries is not None else _chunks(lines)
            packet_path = run_dir / "packets" / f"{i:04d}.json"
            extract_path = run_dir / "extract" / f"{i:04d}.json"
            atomic_json(packet_path, {
                "schema": PACKET_SCHEMA, "run_id": run_id, "index": i, "source_key": c["key"],
                "source_kind": c["kind"], "sha256": c["sha256"], "total_lines": len(lines),
                # External text is material to study, never instructions to follow.
                "untrusted": c["kind"] in EXTERNAL_KINDS,
                "injection": content_guard.summary(text)["verdict"],
                "redactions": redactions, "extract_path": str(extract_path), "chunks": chunks,
                **({"editorial_instructions": soul} if soul else {}),
            })
            files.append({
                "index": i, "key": c["key"], "source": c["source"], "kind": c["kind"],
                "sha256": c["sha256"], "total_lines": len(lines), "status": c["status"],
                "packet": str(packet_path), "extract": str(extract_path),
            })
        if not files:  # nothing ready: drop the empty run folder just made (engine scratch, not user data)
            shutil.rmtree(run_dir)
            return {"run_id": None, "message": "nothing ready to compile", "awaiting_transcription": awaiting,
                    "missing": missing}
        run = {"schema": RUN_SCHEMA, "run_id": run_id, "created": now_iso(), "state": "extracting", "files": files, "jobs": []}
        _save_run(vault, run)
        _set_active(vault, run_id)
    return {
        "awaiting_transcription": awaiting,
        "run_id": run_id,
        "state": "extracting",
        "files": [{"index": f["index"], "key": f["key"], "packet": f["packet"], "extract": f["extract"]} for f in files],
        "remaining_after_this_run": len(changed) - len(selected),
        "missing_sources": missing,
    }


def status(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    run_id = _active_run_id(vault)
    if not run_id:
        return {"active": None}
    run = _load_run(vault)
    extracted = sum(1 for f in run["files"] if Path(f["extract"]).is_file())
    written = sum(1 for j in run["jobs"] if Path(j["page"]).is_file())
    return {"active": run_id, "state": run["state"], "files": len(run["files"]), "extracted": extracted,
            "jobs": len(run["jobs"]), "pages_written": written}


def abandon(vault: Path) -> dict[str, Any]:
    with VaultLock(vault):
        run = _load_run(vault)
        run["state"] = "abandoned"
        _save_run(vault, run)
        _set_active(vault, None)
    return {"abandoned": run["run_id"], "note": "work files kept for audit; sources stay pending"}


def _validate_extract(data: Any, entry: dict[str, Any], target: str) -> list[str]:
    errors = []
    if not isinstance(data, dict) or data.get("schema") != EXTRACT_SCHEMA:
        return [f"schema must be {EXTRACT_SCHEMA}"]
    if data.get("source_key") != entry["key"] or data.get("sha256") != entry["sha256"]:
        errors.append("source_key/sha256 do not match the packet")
    source_language = data.get("source_language", target)
    if not valid_language(source_language):
        errors.append("source_language must be an ISO 639-1 code such as en or it")
    extracted_by = data.get("extracted_by")  # absent = Claude, as always; a non-empty string
    if extracted_by is not None and (not isinstance(extracted_by, str) or not extracted_by.strip()):
        errors.append("extracted_by must be a non-empty string when present")
    concepts = data.get("concepts")
    if not isinstance(concepts, list):
        return errors + ["concepts must be a list (may be empty)"]
    for n, c in enumerate(concepts):
        where = f"concepts[{n}]"
        if not isinstance(c, dict):
            errors.append(f"{where} must be an object")
            continue
        if not isinstance(c.get("title"), str) or not c["title"].strip() or len(c["title"]) > 120:
            errors.append(f"{where}.title must be a non-empty string (max 120)")
        translated = c.get("translated_title")
        if source_language != target and (not isinstance(translated, str) or not translated.strip() or len(translated) > 120):
            errors.append(f"{where}.translated_title (in '{target}') is required when source_language is '{source_language}'")
        if c.get("kind") not in KIND_ORDER:  # "query" pages are saved answers, never extracted
            errors.append(f"{where}.kind must be one of {KIND_ORDER}")
        if c.get("entity_type") is not None and c.get("entity_type") not in ENTITY_TYPES:
            errors.append(f"{where}.entity_type must be one of {sorted(ENTITY_TYPES)}")
        if not isinstance(c.get("summary"), str) or not c["summary"].strip():
            errors.append(f"{where}.summary is required")
        conf = c.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            errors.append(f"{where}.confidence must be a number within 0..1")
        if c.get("provenance") not in EXTRACT_PROVENANCE:
            errors.append(f"{where}.provenance must be one of {sorted(EXTRACT_PROVENANCE)}")
        for field in ("aliases", "related", "contradicts"):
            value = c.get(field, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f"{where}.{field} must be a list of strings")
        claims = c.get("claims")
        if not isinstance(claims, list) or not claims:
            errors.append(f"{where}.claims must be a non-empty list")
            continue
        for m, claim in enumerate(claims):
            lines = claim.get("lines") if isinstance(claim, dict) else None
            if (not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not isinstance(lines, list)
                    or len(lines) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in lines)):
                errors.append(f"{where}.claims[{m}] must be {{text, lines:[start,end]}}")
            elif not 1 <= lines[0] <= lines[1] <= entry["total_lines"]:
                errors.append(f"{where}.claims[{m}].lines {lines} outside 1..{entry['total_lines']}")
    return errors


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _pick_title(titles: list[str]) -> str:
    """Most frequent title; ties go to the first seen. Titles arrive in run file
    order (docs before config before code, then path), which is fixed, so the
    choice never depends on ingestion order."""
    counts = Counter(titles)
    return sorted(counts, key=lambda t: (-counts[t], titles.index(t)))[0]


# Conservative on purpose: better to miss a real cross-project connection than to nag the writer
# with weak matches on every new page. Not exposed as config (yet) -- revisit once it's actually
# been used against a real vault and either proves too strict or too noisy.
CROSS_PROJECT_SIMILARITY_THRESHOLD = 0.82
CROSS_PROJECT_SIMILARITY_LIMIT = 5


def _cross_project_similar(vault: Path, by_path: dict[str, Any], contributions: list[dict[str, Any]],
                           title: str) -> list[dict[str, Any]]:
    """A brand-new page's closest existing relatives from a DIFFERENT project, by meaning rather
    than by name -- compile.merge()'s own dedup only unions concepts whose title/alias slugs
    collide, so a concept described differently in two projects never gets connected there. This
    never merges or links anything itself: it only attaches a suggestion to the job, for whichever
    session writes the page to judge and act on (or not) -- same split as everywhere else in this
    engine, detection here, understanding in the session. Silently empty when embeddings aren't
    set up or the index hasn't been built yet, same as hybrid search degrading to BM25-only."""
    from . import embed

    if not (embed.available(vault) and embed.has_vectors(vault)):
        return []
    own_projects = {c["source_key"].split("/", 1)[0] for c in contributions}
    query = f"{title}. " + " ".join(c["summary"] for c in contributions if c.get("summary"))
    hits = []
    for path, score in embed.search(vault, query, limit=CROSS_PROJECT_SIMILARITY_LIMIT * 2):
        if score < CROSS_PROJECT_SIMILARITY_THRESHOLD:
            continue
        page = by_path.get(path)
        if page is None:
            continue
        page_projects = {s["key"].split("/", 1)[0] for s in (page.frontmatter.get("sources") or [])
                         if isinstance(s, dict) and "key" in s}
        if page_projects & own_projects:
            continue  # shares a project with this job already: not the cross-project signal we want
        hits.append({"slug": page.slug, "title": page.title, "projects": sorted(page_projects), "score": round(score, 3)})
    return hits[:CROSS_PROJECT_SIMILARITY_LIMIT]


def merge(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault):
        run = _load_run(vault)
        if run["state"] not in ("extracting", "writing"):
            raise MnemoError("BAD_RUN_STATE", f"run is {run['state']}")
        language = load_config(vault)["language"]
        target = language["target"]
        roots = {n: resolve_root(vault, e) for n, e in load_sources(vault)["sources"].items()}
        problems, items, excluded = {}, [], []
        for entry in run["files"]:
            path = Path(entry["extract"])
            if not path.is_file():
                problems[entry["key"]] = ["extract file missing: " + str(path)]
                continue
            try:
                data = json.loads(path.read_text("utf-8"))
            except ValueError as exc:
                problems[entry["key"]] = [f"invalid JSON: {exc}"]
                continue
            errors = _validate_extract(data, entry, target)
            if errors:
                problems[entry["key"]] = errors
                continue
            source_file = roots[entry["source"]] / entry["key"].split("/", 1)[1]
            if not source_file.is_file() or sha256_file(source_file) != entry["sha256"]:
                entry["status"] = "changed-during-run"
                excluded.append(entry["key"])
                continue
            entry["status"] = "extracted"
            lang = data.get("source_language", target)
            for c in data["concepts"]:
                display = c["translated_title"].strip() if lang != target else c["title"]
                items.append((entry, dict(c, _lang=lang, _display=display, _extracted_by=data.get("extracted_by"))))
        if problems:
            raise MnemoError("EXTRACT_INCOMPLETE", "every packet needs a valid extract before merge", problems)

        # Saved answers are never merge targets: a concept must not be folded into an answer page.
        existing = [p for p in iter_pages(vault, ("wiki",)) if not p.error and p.frontmatter.get("kind") != "query"]
        by_slug = {p.slug: p for p in existing}
        by_path = {p.path: p for p in existing}
        amap = alias_map(existing)

        def names(c: dict[str, Any]) -> list[str]:
            return [c["_display"], c["title"], *(c.get("translated_title") or "",), *c.get("aliases", [])]

        def keys(c: dict[str, Any]) -> list[str]:
            # "untitled" (a name with no letters) is never a merge key: it would join unrelated concepts
            return [k for k in (slugify(n) for n in names(c) if n.strip()) if k != "untitled"]

        # all concepts sharing a name are grouped first, across every source; then a group goes to the
        # existing page one of its members names, so the same concept never ends up split between an
        # existing page and a new duplicate
        uf = _UnionFind()
        key_owner: dict[str, int] = {}
        for i, (_, c) in enumerate(items):
            uf.find(i)
            for k in keys(c):
                if k in key_owner:
                    uf.union(i, key_owner[k])
                else:
                    key_owner[k] = i
        clusters: dict[int, list[int]] = {}
        for i in range(len(items)):
            clusters.setdefault(uf.find(i), []).append(i)
        groups: dict[str, list[int]] = {}
        for root, members in sorted(clusters.items()):
            direct = {i: next((amap[k] for k in keys(items[i][1]) if k in amap), None) for i in members}
            hits = Counter(s for s in direct.values() if s)
            if not hits:
                groups[f"new:{root}"] = members
                continue
            fallback = sorted(hits, key=lambda s: (-hits[s], s))[0]
            for i in members:  # a member that names an existing page goes there; the rest follow the group
                groups.setdefault(f"existing:{direct[i] or fallback}", []).append(i)

        resolved: dict[str, str] = dict(amap)
        plans = []
        for gkey, members in groups.items():
            concepts = [items[i][1] for i in members]
            if gkey.startswith("existing:"):
                page = by_slug[gkey.split(":", 1)[1]]
                slug, title = page.slug, page.title
                kind = page.frontmatter.get("kind") if page.frontmatter.get("kind") in KIND_DIRS else "concept"
            else:
                title = _pick_title([c["_display"] for c in concepts])
                slug = slugify(title)
                kinds = Counter(c["kind"] for c in concepts)
                kind = sorted(kinds, key=lambda k: (-kinds[k], KIND_ORDER.index(k)))[0]
                page = None
            for c in concepts:
                for k in keys(c):
                    resolved.setdefault(k, slug)
            plans.append((slug, title, kind, page, members))

        run_dir = _work(vault) / run["run_id"]
        soul = editorial_instructions(vault)
        jobs = []
        for slug, title, kind, page, members in sorted(plans, key=lambda p: p[0]):
            contributions, aliases = [], set()
            existing_cites = _page_citations(page)
            for i in members:
                entry, c = items[i]
                aliases.update(a for a in names(c) if a.strip() and slugify(a) != slugify(title))
                contributions.append({
                    "source_key": entry["key"], "sha256": entry["sha256"], "title": c["_display"],
                    "original_title": c["title"], "source_language": c["_lang"],
                    "summary": c["summary"], "confidence": c["confidence"], "provenance": c["provenance"],
                    "claims": [{"text": cl["text"], "cite": f"^[{entry['key']}#L{cl['lines'][0]}-L{cl['lines'][1]}]",
                                **({"already_cited": True}
                                   if _overlaps_existing(existing_cites, entry["key"], cl["lines"]) else {})}
                               for cl in c["claims"]],
                    "related": sorted({resolved.get(slugify(r), slugify(r)) for r in c.get("related", [])} - {slug}),
                    "contradicts": sorted({resolved.get(slugify(r), slugify(r)) for r in c.get("contradicts", [])} - {slug}),
                    "extracted_by": c.get("_extracted_by"),
                })
            target_path = page.path if page else f"{KIND_DIRS[kind]}/{slug}.md"
            existing_info = None
            if page:
                existing_info = {"path": page.path, "sha256": page.sha256}
                aliases.update(page.aliases)
            elif (Path(vault) / target_path).is_file():
                existing_info = {"path": target_path, "sha256": sha256_file(Path(vault) / target_path)}
            sources = {}
            if page and isinstance(page.frontmatter.get("sources"), list):
                for s in page.frontmatter["sources"]:
                    if isinstance(s, dict) and "key" in s:
                        sources[s["key"]] = s.get("sha256")
            for contrib in contributions:
                sources[contrib["source_key"]] = contrib["sha256"]
            confidences = [c["confidence"] for c in contributions]
            if page and isinstance(page.frontmatter.get("confidence"), (int, float)):
                confidences.append(page.frontmatter["confidence"])
            provenance = "merged" if len(sources) > 1 else contributions[0]["provenance"]
            originals = sorted({c["source_language"] for c in contributions} - {target}) if language["keep_original"] else []
            if page and language["keep_original"] and isinstance(page.frontmatter.get("originalLanguage"), list):
                originals = sorted(set(originals) | set(page.frontmatter["originalLanguage"]))
            page_file = run_dir / "pages" / f"{slug}.md"
            frontmatter = {
                "title": title, "kind": kind, "summary": "", "aliases": sorted(aliases), "language": target,
            }
            if originals:
                frontmatter["originalLanguage"] = originals
            if kind == "entity":  # person / organization / place / tool / project / event
                types = Counter(items[i][1].get("entity_type") for i in members if items[i][1].get("entity_type"))
                entity_type = (page.frontmatter.get("entityType") if page else None) or (
                    sorted(types, key=lambda t: (-types[t], t))[0] if types else None)
                if entity_type:
                    frontmatter["entityType"] = entity_type
            job = {
                "schema": JOB_SCHEMA, "slug": slug, "title": title, "kind": kind, "target_path": target_path,
                "existing": existing_info, "page_file": str(page_file), "contributions": contributions,
                "target_language": target, "original_languages": originals,
                "suggested_frontmatter": {
                    **frontmatter,
                    "sources": [{"key": k, "sha256": v} for k, v in sorted(sources.items())],
                    "confidence": round(min(confidences), 2), "provenanceState": provenance,
                    "contradictedBy": sorted({s for c in contributions for s in c["contradicts"]}),
                    "updated": time.strftime("%Y-%m-%d"),
                },
                "related_slugs": sorted({s for c in contributions for s in c["related"]}),
                **({"editorial_instructions": soul} if soul else {}),
                # only for a genuinely new page: an existing one already has its own identity,
                # and a lower-value case for this specific nudge
                **({"cross_project_similar": _cross_project_similar(vault, by_path, contributions, title)}
                   if page is None else {}),
            }
            job_file = run_dir / "jobs" / f"{slug}.json"
            atomic_json(job_file, job)
            jobs.append({"slug": slug, "title": title, "kind": kind, "target_path": target_path,
                         "existing": existing_info, "job": str(job_file), "page": str(page_file)})
        run["jobs"] = jobs
        run["state"] = "writing"
        _save_run(vault, run)
    return {"run_id": run["run_id"], "state": "writing", "jobs": jobs, "excluded_changed_sources": excluded}


def _page_citations(page: Any) -> list[tuple[str, int, int]]:
    """Every (source key, first line, last line) the page body already cites."""
    if page is None:
        return []
    return [(m.group(1).strip(), int(m.group(2)), int(m.group(3) or m.group(2))) for m in CITATION.finditer(page.body)]


def _overlaps_existing(cites: list[tuple[str, int, int]], key: str, lines: list[int]) -> bool:
    """A claim whose lines overlap a range the existing page already cites from the same source is
    probably a restatement (a still-growing source re-extracted whole): the page writer should check
    it against that page before adding it again."""
    first, last = lines[0], lines[1]
    return any(k == key and a <= last and first <= b for k, a, b in cites)


def _weak_citations(vault: Path, pages: dict[str, str]) -> dict[str, Any]:
    """Citations whose cited lines share too little with the sentence: shown next to the dry run,
    never a reason to hold a page and never part of the approval hash. A failure here must not
    stop a submit."""
    from . import citecheck

    try:
        found = citecheck.weak_in_texts(vault, pages, limit=20)
    except Exception as exc:  # noqa: BLE001 - an advisory check
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"citations": found["citations"], "unchecked": found["unchecked"], "weak": found["weak"], "items": found["items"]}


def submit(vault: Path, *, approved: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    config = load_config(vault)
    with VaultLock(vault) as lock:
        run = _load_run(vault)
        if run["state"] != "writing":
            raise MnemoError("BAD_RUN_STATE", f"run is {run['state']}; run compile merge first")
        registry = load_sources(vault)["sources"]
        counter = LineCounter(vault, registry)

        def kind_of(key: str) -> str | None:
            entry = registry.get(key.split("/", 1)[0])
            return entry["kind"] if entry else None

        # an earlier approved submit wrote the pages but was stopped before the bookkeeping
        # (candidates, manifest, index): finish that one instead of reporting the pages as changed
        prefix = f"compile-{run['run_id']}-"
        tx_root = txn.transactions_dir(vault)
        finished = sorted(p for p in tx_root.iterdir() if p.name.startswith(prefix) and (p / "result.json").is_file()) \
            if tx_root.is_dir() else []
        resumed = json.loads((finished[-1] / "result.json").read_text("utf-8")) if finished else None

        problems, live, held, bases = {}, {}, [], {}
        for job_ref in run["jobs"]:
            job = json.loads(Path(job_ref["job"]).read_text("utf-8"))
            page_file = Path(job["page_file"])
            if not page_file.is_file():
                problems[job["slug"]] = ["page file missing: " + str(page_file)]
                continue
            content = page_file.read_text("utf-8")
            required = {c["source_key"]: c["sha256"] for c in job["contributions"]}
            hard, reasons, fm = check_page(content, config=config, line_count=counter, source_kind=kind_of,
                                           required_sources=required, original_languages=job.get("original_languages"),
                                           independence=lambda keys: independent_sources(vault, keys, registry))
            extractors = {c["extracted_by"] for c in job["contributions"] if c.get("extracted_by")}
            if extractors:
                # unconditional, unlike every other hold reason: not gated on config["review"]["hold"]
                # (which is a saved-once list -- an already-initialized vault would simply never
                # have gained "local-extraction" there, the exact staleness bug just fixed for
                # .gitignore). A page with even one contribution from a local model never publishes
                # on confidence alone -- only a human review does. This is the one thing the user
                # asked to be non-negotiable, not just a default someone could configure away.
                reasons.append({"code": "local-extraction", "detail": f"estratto da: {', '.join(sorted(extractors))}"})
            base = job["existing"]["sha256"] if job["existing"] else None
            target = job["target_path"] if job["existing"] else f"{KIND_DIRS.get(fm.get('kind'), KIND_DIRS[job['kind']])}/{job['slug']}.md"
            current = sha256_file(Path(vault) / target) if (Path(vault) / target).is_file() else None
            if current != base and not (resumed and current == sha256_bytes(content.encode("utf-8"))):
                hard.append(f"{target} changed since merge; run compile merge again")
            if hard:
                problems[job["slug"]] = hard
            elif reasons:
                held.append({"slug": job["slug"], "target_path": target, "base_sha256": base,
                             "content": content, "reasons": reasons})
            else:
                live[target] = content
                bases[target] = base
        if problems:
            raise MnemoError("PAGES_INVALID", "fix these pages and submit again", problems)
        if resumed:
            return _finish_submit(vault, lock, run, live, held, resumed,
                                  {"run_id": run["run_id"], "resumed": True,
                                   "live": [{"path": p} for p in sorted(live)],
                                   "held": [{"slug": h["slug"], "reasons": h["reasons"]} for h in held]})

        if approved is None:
            run["submit_stamp"] = stamp_now()
            _save_run(vault, run)
        stamp = run.get("submit_stamp")
        if not stamp:
            raise MnemoError("PLAN_CHANGED", "run compile submit without --approved first")
        live_slugs = sorted(Path(p).stem for p in live)
        log_entry = f"compile {run['run_id']} — pubblicate {len(live)}, in revisione {len(held)}"
        if live_slugs:
            log_entry += "\n" + "\n".join(f"- [[{s}]]" for s in live_slugs)
        attempt = 1 + (sum(1 for p in tx_root.iterdir() if p.name.startswith(prefix)) if tx_root.is_dir() else 0)
        bundle = {
            "schema": txn.BUNDLE_SCHEMA,
            "operation_id": f"{prefix}{attempt}",
            "operation": "compile",
            "writes": build_writes(vault, live, log_entry, stamp, bases),
        }
        tx_plan = txn.prepare(vault, bundle)
        approval = canonical_hash({
            "txn": tx_plan.approval_sha256,
            "held": sorted((h["slug"], sha256_bytes(h["content"].encode("utf-8"))) for h in held),
        })
        summary = {
            "run_id": run["run_id"], "approval_sha256": approval,
            "live": [{"path": p} for p in sorted(live)],
            "held": [{"slug": h["slug"], "reasons": h["reasons"]} for h in held],
            "weak_citations": _weak_citations(vault, {**live, **{h["target_path"]: h["content"] for h in held}}),
        }
        if approved is None:
            return {"dry_run": True, **summary, "next": "compile submit --approved <approval_sha256>"}
        if approved != approval:
            raise MnemoError("PLAN_CHANGED", "pages or vault changed since the dry run; run submit again")

        result = txn.apply(vault, bundle, tx_plan.approval_sha256, lock=lock)
        return _finish_submit(vault, lock, run, live, held, result, summary)


def _finish_submit(vault: Path, lock: VaultLock, run: dict[str, Any], live: dict[str, str],
                   held: list[dict[str, Any]], result: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Bookkeeping after the pages are live. Every step is idempotent (candidate ids come from
    their content), so a submit stopped half-way is finished by running submit again."""
    candidates = [save_candidate(vault, lock, run_id=run["run_id"], **h) for h in held]
    manifest = load_manifest(vault)
    for entry in run["files"]:
        if entry["status"] == "extracted":
            manifest["files"][entry["key"]] = {"sha256": entry["sha256"], "compiled_at": now_iso(), "run_id": run["run_id"]}
    save_state(vault, lock, "manifest.json", manifest)
    index_update(vault, list(live), lock)
    run["state"] = "complete"
    run["finished"] = now_iso()
    _save_run(vault, run)
    _set_active(vault, None)
    return {**summary, "dry_run": False, "transaction": result["operation_id"], "candidates": candidates}
