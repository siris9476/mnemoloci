"""Open Knowledge Format v0.2 (Google Cloud) export and import.

Spec: https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md
- Export: one concept per wiki page, `type` required, provenance in `sources`
  with footnotes keyed by source id, trust in `generated`/`verified` (human
  approval only when the user approved the page in review), staleness in
  `stale_after`, links as bundle-relative markdown links, index.md + log.md.
  Mnemoloci-only metadata lives in an `x-mnemoloci` extension block.
- Import: tolerant consumer (§11): unknown types/keys kept, broken links
  allowed. Every concept is staged in the review queue as an external source;
  the original frontmatter is preserved under `x-okf`.
"""

import json
import os
import posixpath
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from . import __version__, content_guard, i18n
from .core.errors import MnemoError
from .core.hashing import sha256_bytes, sha256_file
from .core.lock import VaultLock
from .core.paths import is_link_like
from .markdown import CITATION, WIKILINK, render_page, slugify, split_frontmatter
from .pages import alias_map, iter_pages
from .review import save_candidate
from .secrets import redact
from .sources import load_sources, resolve_root, source_root
from .vault import KIND_DIRS, engine, load_config, now_iso, require_vault, save_state

OKF_VERSION = "0.2"
PRODUCER = f"mnemoloci/{__version__}"
TYPE_OF_KIND = {"concept": "Concept", "entity": "Entity", "comparison": "Comparison", "overview": "Overview",
                "query": "Answer"}
KIND_OF_TYPE = {v.casefold(): k for k, v in TYPE_OF_KIND.items()}
BUNDLE_DIR_OF_KIND = {"concept": "concepts", "entity": "entities", "comparison": "comparisons", "overview": "overviews",
                      "query": "answers"}
RESERVED = {"index.md", "log.md"}
MAX_IMPORT_FILES = 2000
MAX_IMPORT_BYTES = 2 * 1024 * 1024


def _iso(value: Any) -> str | None:
    """Our 'YYYY-MM-DD' / local ISO values -> ISO 8601 with an explicit UTC offset (spec §5)."""
    if not value:
        return None
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00")) if "T" in text else datetime.fromisoformat(f"{text}T00:00:00+00:00")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _approvals(vault: Path) -> dict[str, str]:
    """target path -> when the user approved it in the review queue."""
    out = {}
    for f in (engine(vault) / "candidates" / "archive").glob("approved-*.json"):
        if f.name.endswith(".original.json"):
            continue
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        out[data.get("target_path")] = data.get("decided")
    return out


def _source_id(key: str, used: dict[str, str]) -> str:
    if key in used:
        return used[key]
    base = slugify(key)[:48] or "source"
    sid, n = base, 2
    while sid in used.values():
        sid, n = f"{base}-{n}", n + 1
    used[key] = sid
    return sid


def export_okf(vault: Path, out: str, *, human_id: str | None = None, include_memory: bool = False,
               include_sources: bool = False) -> dict[str, Any]:
    require_vault(vault)
    out_dir = Path(out).resolve()
    if out_dir == Path(vault).resolve() or Path(vault).resolve() in out_dir.parents:
        raise MnemoError("BAD_OUT_DIR", "export the bundle outside the vault")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise MnemoError("OUT_DIR_NOT_EMPTY", f"{out_dir} exists and is not empty")
    human = f"human:{human_id or slugify(os.environ.get('USERNAME') or os.environ.get('USER') or 'owner')}"
    target_lang = load_config(vault)["language"]["target"]
    registry = load_sources(vault)["sources"]
    approvals = _approvals(vault)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    roots = ("wiki", "memory") if include_memory else ("wiki",)
    pages = [p for p in iter_pages(vault, roots) if not p.error]
    bundle_path: dict[str, str] = {}
    for p in pages:
        kind = p.frontmatter.get("kind")
        if kind in BUNDLE_DIR_OF_KIND:
            folder = BUNDLE_DIR_OF_KIND[kind]
        else:
            folder = "memory/" + str(p.frontmatter.get("memoryKind") or "notes")
        bundle_path[p.slug] = f"{folder}/{p.slug}.md"
    resolve = alias_map(pages)

    files: dict[str, str] = {}
    source_files: dict[str, str] = {}
    listing: dict[str, list[tuple[str, str, str]]] = {}
    log: dict[str, list[str]] = {}
    for p in pages:
        fm = p.frontmatter
        path = bundle_path[p.slug]
        used: dict[str, str] = {}
        declared = [s for s in fm.get("sources") or [] if isinstance(s, dict) and s.get("key")]
        okf_sources, ext_sources = [], []
        for s in declared:
            sid = _source_id(s["key"], used)
            name, _, rel = s["key"].partition("/")
            entry = {"id": sid, "title": s["key"]}
            if include_sources and name in registry and rel:
                ref = f"references/{s['key']}.md"
                entry["resource"] = "/" + ref
                source_files.setdefault(s["key"], ref)
            else:
                entry["resource"] = f"mnemoloci-source:{s['key']}"  # scope descriptor, not followable (§5.1)
            src_path = resolve_root(vault, registry[name]) / rel if name in registry and rel else None
            if src_path and src_path.is_file():
                modified = datetime.fromtimestamp(src_path.stat().st_mtime, timezone.utc)
                entry["last_modified"] = modified.strftime("%Y-%m-%dT%H:%M:%SZ")
            okf_sources.append(entry)
            ext_sources.append({"id": sid, "key": s["key"], "sha256": s.get("sha256")})

        cites = []

        def cite(m: re.Match) -> str:
            key, start = m.group(1).strip(), int(m.group(2))
            sid = _source_id(key, used)
            cites.append({"id": sid, "key": key, "start": start, "end": int(m.group(3) or start)})
            return f"[^{sid}]"

        body = CITATION.sub(cite, p.body)

        def link(m: re.Match) -> str:
            raw = m.group(0)
            label_match = re.search(r"\|([^\]\n]*)\]\]$", raw)
            target = slugify(m.group(1).strip().split("/")[-1])
            label = label_match.group(1) if label_match else m.group(1).strip()
            slug = resolve.get(target)
            if not slug or slug not in bundle_path:
                return label  # no fake link to a page that is not in the bundle
            return f"[{label}]({posixpath.relpath(bundle_path[slug], str(PurePosixPath(path).parent))})"

        body = WIKILINK.sub(link, body)
        body = re.sub(r"^> \[!\w+\][-+]?\s*(.*)$", r"> **\1**", body, flags=re.MULTILINE)  # callout -> plain quote
        footnotes = []
        for sid in dict.fromkeys(c["id"] for c in cites):
            key = next(c["key"] for c in cites if c["id"] == sid)
            footnotes.append(f"[^{sid}]: {key}")
        if footnotes:
            body = body.rstrip() + "\n\n" + "\n".join(footnotes) + "\n"

        freshness = "n/a"
        states = []
        for s in ext_sources:
            name, _, rel = s["key"].partition("/")
            src_path = resolve_root(vault, registry[name]) / rel if name in registry and rel else None
            states.append("orphaned" if not src_path or not src_path.is_file()
                          else "stale" if sha256_file(src_path) != s["sha256"] else "fresh")
        if states:
            freshness = "orphaned" if "orphaned" in states else "stale" if "stale" in states else "fresh"

        kind = fm.get("kind")
        okf_fm: dict[str, Any] = {"type": TYPE_OF_KIND.get(kind) or str(fm.get("memoryKind") or "Note").title(),
                                  "title": p.title}
        if fm.get("summary"):
            okf_fm["description"] = " ".join(str(fm["summary"]).split())
        if isinstance(fm.get("tags"), list) and fm["tags"]:
            okf_fm["tags"] = [str(t) for t in fm["tags"]]
        okf_fm["generated"] = {"by": PRODUCER, "at": _iso(fm.get("updated") or fm.get("created")) or now}
        if not path.startswith("memory/") and approvals.get(p.path):
            okf_fm["verified"] = {"by": human, "at": _iso(approvals[p.path]) or now}
        okf_fm["status"] = "deprecated" if fm.get("status") in ("superseded", "retired") else "stable"
        if freshness in ("stale", "orphaned"):
            okf_fm["stale_after"] = now  # its sources changed: stale as of this export (§5.5)
        if okf_sources:
            okf_fm["sources"] = okf_sources
        okf_fm["x-mnemoloci"] = {
            "slug": p.slug, "vault_path": p.path, "aliases": p.aliases,
            "language": fm.get("language", target_lang), "originalLanguage": fm.get("originalLanguage", []),
            "confidence": fm.get("confidence"), "provenanceState": fm.get("provenanceState"),
            "contradictedBy": fm.get("contradictedBy") or [], "freshness": freshness,
            "sources": ext_sources, "citations": cites, "sha256": p.sha256,
        }
        files[path] = _render_doc(okf_fm, body)
        folder = str(PurePosixPath(path).parent)
        listing.setdefault(folder, []).append((p.title, PurePosixPath(path).name, okf_fm.get("description", "")))
        day = (okf_fm["generated"]["at"] or now)[:10]
        log.setdefault(day, []).append(f"* **Update**: [{p.title}](/{path})")

    for key, ref in source_files.items():
        name, _, rel = key.partition("/")
        src = source_root(vault, name) / rel
        if not src.is_file():
            continue
        text, _ = redact(src.read_text("utf-8", errors="replace"))
        fence = "````" if "```" in text else "```"
        files[ref] = _render_doc({"type": "Reference", "title": key,
                                  "generated": {"by": PRODUCER, "at": now}},
                                 i18n.t(vault, f"Copia (con segreti oscurati) della fonte `{key}`.", f"Copy (secrets redacted) of the source `{key}`.")
                                 + f"\n\n{fence}\n{text}\n{fence}\n")
        listing.setdefault(str(PurePosixPath(ref).parent), []).append((key, PurePosixPath(ref).name, i18n.t(vault, "fonte originale", "original source")))

    for folder in sorted({f for f in listing} | {str(p) for f in listing for p in PurePosixPath(f).parents if str(p) != "."}):
        if folder == ".":
            continue
        entries = sorted(listing.get(folder, []), key=lambda e: e[0].casefold())
        subdirs = sorted({str(PurePosixPath(f).relative_to(folder).parts[0]) for f in listing
                          if f != folder and PurePosixPath(f).is_relative_to(folder)})
        lines = []
        if subdirs:
            lines += ["# Subdirectories", ""] + [f"* [{d}]({d}/index.md)" for d in subdirs] + [""]
        if entries:
            lines += ["# Concepts", ""] + [f"* [{t}]({n})" + (f" - {d}" if d else "") for t, n, d in entries]
        files[f"{folder}/index.md"] = "\n".join(lines).rstrip() + "\n"
    top = sorted({PurePosixPath(f).parts[0] for f in listing})
    files["index.md"] = (f"---\nokf_version: \"{OKF_VERSION}\"\n---\n\n# Subdirectories\n\n"
                         + "\n".join(f"* [{d}]({d}/index.md)" for d in top) + "\n")
    files["log.md"] = "# Update Log\n\n" + "\n\n".join(
        f"## {day}\n" + "\n".join(entries) for day, entries in sorted(log.items(), reverse=True)) + "\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    problems = check_conformance(out_dir)
    return {"out": str(out_dir), "concepts": len(pages), "references": len(source_files),
            "okf_version": OKF_VERSION, "conformance_problems": problems}


def _render_doc(frontmatter: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1000).rstrip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


def check_conformance(bundle: Path) -> list[str]:
    """OKF v0.2 §11: every non-reserved .md has parseable frontmatter with a non-empty `type`."""
    problems = []
    for path in sorted(Path(bundle).rglob("*.md")):
        rel = path.relative_to(bundle).as_posix()
        text = path.read_text("utf-8", errors="replace")
        try:
            fm, _ = split_frontmatter(text)
        except MnemoError as exc:
            problems.append(f"{rel}: {exc.message}")
            continue
        if path.name in RESERVED:
            # §8 forbids frontmatter in index.md except `okf_version` at the root. §9 says
            # nothing of the kind for log.md (Google's own acme_retail bundle has one).
            if path.name == "index.md" and fm and (rel != "index.md" or set(fm) - {"okf_version"}):
                problems.append(f"{rel}: index.md may carry frontmatter only at the root (okf_version)")
            continue
        if not fm or not str(fm.get("type") or "").strip():
            problems.append(f"{rel}: missing required `type`")
    return problems


# ----------------------------------------------------------------------- import

# Our confidence for imported knowledge, derived only from the spec's trust tiers (§5.3).
TIER_CONFIDENCE = {"human-reviewed": 0.8, "machine-confirmed": 0.6, "unverified": 0.4}


def _trust_tier(fm: dict[str, Any]) -> str:
    verified = fm.get("verified")
    if isinstance(verified, dict):
        events = [verified]  # a bare mapping is a one-element list (§5.2)
    elif isinstance(verified, list):
        events = [v for v in verified if isinstance(v, dict)]
    else:
        events = []
    if not events:
        return "unverified"
    return "human-reviewed" if any(str(e.get("by") or "").startswith("human:") for e in events) else "machine-confirmed"


def _is_stale(fm: dict[str, Any]) -> bool:
    raw = str(fm.get("stale_after") or "")
    if "T" not in raw:
        return False  # a date without time and offset is ambiguous: ignored, as in the reference agent
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return when.tzinfo is not None and datetime.now(timezone.utc) >= when

_MD_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\s]+\.md)(#[^)]*)?\)")


def _bundle_docs(bundle: Path) -> list[tuple[str, Path]]:
    docs = []
    for dirpath, dirnames, filenames in os.walk(bundle):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not is_link_like(os.lstat(Path(dirpath) / d))]
        for name in filenames:
            path = Path(dirpath) / name
            if name.endswith(".md") and name not in RESERVED and not is_link_like(os.lstat(path)):
                docs.append((path.relative_to(bundle).as_posix(), path))
    if len(docs) > MAX_IMPORT_FILES:
        raise MnemoError("BUNDLE_TOO_LARGE", f"{len(docs)} documents; the limit is {MAX_IMPORT_FILES}")
    return sorted(docs)


def import_okf(vault: Path, bundle_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    require_vault(vault)
    bundle = Path(bundle_dir).resolve()
    if not bundle.is_dir():
        raise MnemoError("NO_SUCH_DIR", f"{bundle} is not a directory")
    target_lang = load_config(vault)["language"]["target"]
    source_name = ("okf-" + slugify(bundle.name))[:40].rstrip("-")
    docs = _bundle_docs(bundle)

    parsed, skipped = [], []
    for rel, path in docs:
        if path.stat().st_size > MAX_IMPORT_BYTES:
            skipped.append({"path": rel, "reason": "larger than 2 MB"})
            continue
        text = path.read_text("utf-8", errors="replace")
        try:
            fm, body = split_frontmatter(text)
        except MnemoError as exc:
            skipped.append({"path": rel, "reason": exc.message})
            continue
        if not fm or not str(fm.get("type") or "").strip():
            skipped.append({"path": rel, "reason": "no `type` (not an OKF concept)"})
            continue
        parsed.append((rel, fm, body, text))

    # Concept ID -> vault slug; a stem used twice gets its folder as a prefix.
    stems: dict[str, int] = {}
    for rel, *_ in parsed:
        stems[slugify(PurePosixPath(rel).stem)] = stems.get(slugify(PurePosixPath(rel).stem), 0) + 1
    slug_of = {}
    for rel, *_ in parsed:
        stem = slugify(PurePosixPath(rel).stem)
        slug_of[rel] = stem if stems[stem] == 1 else slugify(rel[:-3])
    existing = {p.slug for p in iter_pages(vault, ("wiki",))}

    staged = []
    for rel, fm, body, text in parsed:
        kind = KIND_OF_TYPE.get(str(fm["type"]).casefold(), "concept")
        here = PurePosixPath(rel).parent

        def to_wikilink(m: re.Match) -> str:
            href = m.group(2)
            if re.match(r"^[a-z][a-z0-9+.-]*:", href):
                return m.group(0)  # external URL: keep
            target = PurePosixPath(href.lstrip("/")) if href.startswith("/") else here / href
            parts: list[str] = []
            for part in target.parts:
                if part == "..":
                    if parts:
                        parts.pop()
                elif part != ".":
                    parts.append(part)
            normalized = "/".join(parts)
            slug = slug_of.get(normalized)
            return f"[[{slug}|{m.group(1)}]]" if slug else m.group(1)

        # a foreign bundle is untrusted: redacted (in-line, line numbers kept) and scanned for
        # instructions aimed at an agent before anything is written, even to the review queue
        converted, redactions = redact(_MD_LINK.sub(to_wikilink, body).strip())
        fm = json.loads(redact(json.dumps(fm, ensure_ascii=False))[0])
        guard = content_guard.summary(converted)
        lines = text.count("\n") + 1
        key = f"{source_name}/{rel}"
        converted += f"\n\n---\nImportato dal pacchetto OKF `{bundle.name}` ^[{key}#L1-L{lines}]"
        slug = slug_of[rel]
        tier = _trust_tier(fm)
        page_fm = {
            "title": str(fm.get("title") or PurePosixPath(rel).stem),
            "kind": kind,
            "summary": " ".join(str(fm.get("description") or "").split())[:500],
            "aliases": [],
            "language": target_lang,
            "sources": [{"key": key, "sha256": sha256_bytes(text.encode("utf-8"))}],
            "confidence": TIER_CONFIDENCE[tier],
            "provenanceState": "extracted",
            "contradictedBy": [],
            "updated": time.strftime("%Y-%m-%d"),
            "x-okf": fm,  # the producer's original frontmatter, preserved (§4.1)
        }
        target_path = f"{KIND_DIRS[kind]}/{slug}.md"
        reasons = [{"code": "external-source",
                    "detail": f"OKF bundle {bundle.name}: {rel} (type {fm['type']}, trust: {tier})"}]
        if _is_stale(fm):
            reasons.append({"code": "external-source", "detail": f"stale since {fm.get('stale_after')} (stale_after)"})
        if slug in existing:
            reasons.append({"code": "external-source", "detail": f"would replace the existing page {slug}"})
        if guard["verdict"] != "safe":
            reasons.append({"code": "external-source", "detail": f"injection suspect: {guard['count']} passage(s) read like instructions "
                                                                   f"to an AI ({guard['verdict']})"})
        if redactions:
            page_fm["redactions"] = redactions
        staged.append({"slug": slug, "target_path": target_path, "content": render_page(page_fm, converted),
                       "reasons": reasons, "okf_path": rel})

    if dry_run:
        return {"dry_run": True, "bundle": str(bundle), "would_stage": [{k: s[k] for k in ("slug", "okf_path", "target_path")} for s in staged],
                "skipped": skipped}

    with VaultLock(vault) as lock:
        state = load_sources(vault)
        if source_name not in state["sources"]:
            state["sources"][source_name] = {"root": str(bundle), "kind": "import", "added": now_iso()}
            save_state(vault, lock, "sources.json", state)
        ids = []
        for s in staged:
            target = Path(vault) / s["target_path"]
            base = sha256_file(target) if target.is_file() else None
            ids.append(save_candidate(vault, lock, run_id=f"okf-import-{int(time.time())}", slug=s["slug"],
                                      target_path=s["target_path"], base_sha256=base, content=s["content"],
                                      reasons=s["reasons"]))
    return {"dry_run": False, "bundle": str(bundle), "source": source_name, "staged": len(ids),
            "candidates": ids, "skipped": skipped, "next": "mnemoloci review list"}
