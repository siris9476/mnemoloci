"""Create skills into a review folder, never straight into an agent's skills dir.

A draft folder written by the agent is validated and security
scanned BEFORE anything is written; only then is it published to
<vault>/_generated-skills/<name>/ with a SKILL_FACTORY.md provenance manifest.
Installing into Claude Code / ~/.agents is always a separate explicit command.
"""

import os
import re
import shutil
import subprocess
import time
from collections import Counter
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any

from . import graph_analysis, index
from .core import txn
from .core.errors import ConflictError, MnemoError, ValidationError
from .core.hashing import sha256_bytes, sha256_file
from .core.lock import VaultLock
from .core.paths import is_link_like, validate_relpath
from .markdown import render_page, slugify, split_frontmatter
from .pages import load_page
from .sessions import claude_dirs
from .skill_guard import scan_dir
from .skill_registry import INSTALLED, REVIEW_DIR, invalidate, name_locations, read_skill
from .sources import load_sources, resolve_root
from .vault import engine, load_state, now_iso, require_vault, save_state

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_SKILL_CHARS = 100_000
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 60
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}
MANIFEST = "SKILL_FACTORY.md"
STATE_SCHEMA = "mnemoloci.skills.v1"
ORIGINS = {"vault", "sessions", "git", "manual", "instincts"}
MATURE_CONFIDENCE = 0.8


def validate_draft(draft: Path) -> tuple[list[str], dict[str, Any]]:
    draft = Path(draft)
    errors: list[str] = []
    skill_md = draft / "SKILL.md"
    if not skill_md.is_file():
        return ["SKILL.md is missing"], {}
    text = skill_md.read_text("utf-8", errors="replace")
    if len(text) > MAX_SKILL_CHARS:
        errors.append(f"SKILL.md is over {MAX_SKILL_CHARS} characters")
    try:
        fm, body = split_frontmatter(text)
    except ValidationError as exc:
        return [exc.message], {}
    if not fm:
        return ["SKILL.md needs YAML frontmatter with name and description"], {}
    name, description = fm.get("name"), fm.get("description")
    if not isinstance(name, str) or not NAME.match(name) or len(name) > MAX_NAME:
        errors.append("name must match ^[a-z0-9][a-z0-9._-]*$ and be at most 64 characters")
    elif name != draft.name:
        errors.append(f"folder name {draft.name!r} must equal the skill name {name!r}")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        errors.append(f"description must be non-empty and at most {MAX_DESCRIPTION} characters")
    if not body.strip():
        errors.append("SKILL.md body is empty")
    files = []
    for dirpath, dirnames, filenames in os.walk(draft):
        for d in list(dirnames):
            if is_link_like(os.lstat(Path(dirpath) / d)):
                errors.append(f"symlink/junction not allowed: {d}")
                dirnames.remove(d)
        for fname in filenames:
            rel = (Path(dirpath) / fname).relative_to(draft).as_posix()
            files.append(rel)
            try:
                validate_relpath(rel)
            except ValidationError as exc:
                errors.append(exc.message)
                continue
            if rel == MANIFEST:
                errors.append(f"{MANIFEST} is written by the engine; remove it from the draft")
            elif rel != "SKILL.md" and PurePosixPath(rel).parts[0] not in ALLOWED_SUBDIRS:
                errors.append(f"{rel}: extra files must live under {sorted(ALLOWED_SUBDIRS)}")
            elif (draft / rel).stat().st_size > MAX_FILE_BYTES:
                errors.append(f"{rel} is larger than 1 MiB")
    if len(files) > MAX_FILES:
        errors.append(f"{len(files)} files; at most {MAX_FILES}")
    return errors, {"name": name, "description": description, "files": sorted(files)}


def draft_dir(vault: Path, name: str) -> dict[str, Any]:
    require_vault(vault)
    if not NAME.match(name or "") or len(name) > MAX_NAME:
        raise ValidationError("BAD_SKILL_NAME", "name must match ^[a-z0-9][a-z0-9._-]*$ (max 64)")
    path = engine(vault) / "work" / "skills" / name
    path.mkdir(parents=True, exist_ok=True)
    taken = [loc for loc in name_locations(vault, name)]
    return {"draft_dir": str(path), "name_already_used_at": taken}


def _page_maturity(vault: Path, rel: str, registry: dict[str, Any]) -> tuple[bool, list[str]]:
    page = load_page(vault, rel)
    fm, reasons = page.frontmatter, []
    if page.error:
        return False, [page.error]
    conf = fm.get("confidence")
    if not isinstance(conf, (int, float)) or conf < MATURE_CONFIDENCE:
        reasons.append(f"confidence {conf} < {MATURE_CONFIDENCE}")
    if fm.get("provenanceState") not in ("extracted", "merged"):
        reasons.append(f"provenanceState {fm.get('provenanceState')}")
    if fm.get("contradictedBy"):
        reasons.append("contradicted")
    for src in fm.get("sources") or []:
        name, _, relpath = str(src.get("key", "")).partition("/")
        entry = registry.get(name)
        path = resolve_root(vault, entry) / relpath if entry else None
        if not path or not path.is_file() or sha256_file(path) != src.get("sha256"):
            reasons.append(f"source {src.get('key')} changed or missing (stale)")
    return not reasons, reasons


def _git_material(repo: str, commits: int = 300) -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", f"-n{commits}", "--no-merges", "--pretty=format:%x1e%s", "--name-only"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise MnemoError("GIT_FAILED", f"cannot read git history of {repo}: {exc}") from exc
    subjects, files, pairs, prefixes = [], Counter(), Counter(), Counter()
    for block in out.split("\x1e")[1:]:
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        subjects.append(lines[0])
        m = re.match(r"^(\w+)(\(.+?\))?!?:", lines[0])
        prefixes[m.group(1).lower() if m else "(none)"] += 1
        changed = sorted(set(lines[1:]))[:40]
        files.update(changed)
        pairs.update(" + ".join(p) for p in combinations(changed, 2))
    return {
        "repo": repo, "commits": len(subjects), "commit_types": dict(prefixes.most_common(10)),
        "most_changed_files": files.most_common(20), "co_changed_pairs": pairs.most_common(15),
        "recent_subjects": subjects[:40],
    }


def material(vault: Path, *, query: str | None = None, git: str | None = None, limit: int = 15) -> dict[str, Any]:
    """Collect evidence for a new skill: mature wiki pages, memory notes, git patterns."""
    require_vault(vault)
    if not query and not git:
        raise MnemoError("NO_MATERIAL_QUERY", "give --query and/or --git")
    out: dict[str, Any] = {}
    if query:
        registry = load_sources(vault)["sources"]
        hits = index.query(vault, query, limit=limit * 2, expand=True)
        mature, excluded, memory_notes = [], [], []
        for hit in [*hits["results"], *hits.get("related", [])]:
            rel = hit["path"]
            if rel.startswith("memory/"):
                memory_notes.append({"path": rel, "title": hit["title"]})
                continue
            ok, reasons = _page_maturity(vault, rel, registry)
            (mature if ok else excluded).append({"path": rel, "title": hit["title"], **({} if ok else {"why": reasons})})
        out.update(mature_pages=mature[:limit], excluded_pages=excluded, memory_notes=memory_notes[:limit])
    if git:
        out["git"] = _git_material(git)
    out["next"] = ("mnemoloci skills draft <name>, write SKILL.md there, then "
                   "mnemoloci skills submit <draft_dir> --origin vault|sessions|git|manual --source <path>...")
    return out


def suggest_clusters(vault: Path, *, limit: int = 10, min_mature: int = 3, max_mature: int = 40) -> dict[str, Any]:
    """Find candidate skill material without a manual --query: pages more linked to each other
    than to the rest (the same Louvain communities graph_analysis.analyze uses for the graph view),
    kept only where enough of them are mature (_page_maturity). No --query still means no automatic
    guess among unrelated pages -- a community under `min_mature` is simply left out.

    On a vault with hub-links, one Louvain pass often folds an entire project into a single giant
    community (seen for real: 300+ pages) -- too broad to be one skill's material, and well past
    what a draft can hold anyway (`MAX_FILES`). Rather than guess which slice of a mega-project is
    "the real topic", `max_mature` drops the whole community: that project already has its own
    overview page, and a real skill for a specific slice of it still wants a human-chosen --query."""
    require_vault(vault)
    registry = load_sources(vault)["sources"]
    pages, adj, _inbound = graph_analysis._graph(vault)
    member = graph_analysis._communities(adj)
    groups: dict[str, list[str]] = {}
    for slug, label in member.items():
        groups.setdefault(label, []).append(slug)
    clusters = []
    for label, slugs in groups.items():
        mature = [s for s in slugs if _page_maturity(vault, pages[s].path, registry)[0]]
        if len(mature) < min_mature or len(mature) > max_mature:
            continue
        hub = max(mature, key=lambda s: (len(adj[s]), s))
        clusters.append({
            "community": label, "size": len(slugs),
            "mature_pages": [{"slug": s, "path": pages[s].path, "title": pages[s].title} for s in sorted(mature)],
            "hub_page": {"slug": hub, "title": pages[hub].title},
            "suggested_name": slugify(pages[hub].title)[:MAX_NAME] or hub,
        })
    clusters.sort(key=lambda c: (-len(c["mature_pages"]), c["community"]))
    return {"clusters": clusters[:limit],
            "next": "mnemoloci skills material --query '<cluster topic>' to gather the material, then draft/submit"}


def _manifest(name: str, origin: str, sources: list[str], scan: dict[str, Any], files: dict[str, str]) -> str:
    fm = {"schema": "mnemoloci.skill-factory.v1", "name": name, "origin": origin, "created": now_iso(),
          "sources": sources, "guard_verdict": scan["verdict"], "guard_findings": scan["findings"],
          "files": [{"path": p, "sha256": h} for p, h in sorted(files.items())]}
    body = (f"# Provenienza della skill `{name}`\n\n"
            f"Generata da Mnemoloci (origine: {origin}). File escluso dall'installazione.\n\n"
            f"- Controllo di sicurezza: **{scan['verdict']}** ({len(scan['findings'])} segnalazioni)\n"
            + "".join(f"- Fonte: {s}\n" for s in sources))
    return render_page(fm, body)


def submit(vault: Path, draft: str, *, origin: str, sources: list[str] | None = None,
           replace: bool = False) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    draft_path = Path(draft).resolve()
    if origin not in ORIGINS:
        raise ValidationError("BAD_ORIGIN", f"origin must be one of {sorted(ORIGINS)}")
    errors, meta = validate_draft(draft_path)
    if errors:
        raise ValidationError("SKILL_INVALID", "fix the draft and submit again", errors)
    scan = scan_dir(draft_path)
    if scan["verdict"] == "dangerous":
        raise ConflictError("SKILL_BLOCKED", "security scan found dangerous content; nothing was written",
                            [f for f in scan["findings"] if f["severity"] == "dangerous"])
    name = meta["name"]
    installed_elsewhere = [loc for loc in name_locations(vault, name) if loc["label"] in INSTALLED]
    state = load_state(vault, "skills.json", STATE_SCHEMA, {"generated": {}, "installs": []})
    ours = {i["path"] for i in state["installs"] if i["name"] == name}
    foreign = [loc for loc in installed_elsewhere if str(Path(loc["path"]).parent) not in ours]
    if foreign:
        raise ConflictError("NAME_TAKEN", f"a different skill named {name!r} is already installed; pick another name",
                            foreign)
    target_dir = vault / REVIEW_DIR / name
    with VaultLock(vault) as lock:
        if target_dir.exists():
            if not replace:
                raise ConflictError("SKILL_EXISTS", f"{REVIEW_DIR}/{name} exists; use --replace to publish a new version")
            versions = engine(vault) / "skill-versions" / name
            versions.mkdir(parents=True, exist_ok=True)
            os.replace(target_dir, versions / time.strftime("%Y%m%d-%H%M%S"))
        hashes = {rel: sha256_bytes((draft_path / rel).read_bytes()) for rel in meta["files"]}
        writes = [{"path": f"{REVIEW_DIR}/{name}/{rel}", "action": "create",
                   "content_file": str(draft_path / rel), "sha256": sha}  # content_file keeps assets byte-exact
                  for rel, sha in hashes.items()]
        writes.append({"path": f"{REVIEW_DIR}/{name}/{MANIFEST}", "action": "create",
                       "content": _manifest(name, origin, list(sources or []), scan, hashes)})
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"skill-{name}-{time.time_ns()}",
                  "operation": "skill-submit", "writes": writes}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        state["generated"][name] = {"origin": origin, "sources": list(sources or []), "submitted": now_iso(),
                                    "files": hashes, "guard_verdict": scan["verdict"]}
        save_state(vault, lock, "skills.json", state)
        invalidate(vault)
    return {"name": name, "review_path": str(target_dir), "guard_verdict": scan["verdict"],
            "findings": scan["findings"],
            "next": f"review it, then: mnemoloci skills install {name} --target claude|agents"}


def patch(vault: Path, name: str, *, old: str, new: str, file: str = "SKILL.md") -> dict[str, Any]:
    """Edit a generated skill: `old` must occur exactly once.
    The result is re-validated and re-scanned and published as a new version; the previous one is
    archived. Installed copies are not touched: reinstall with `skills install --replace`."""
    require_vault(vault)
    vault = Path(vault)
    source = vault / REVIEW_DIR / name
    if not (source / "SKILL.md").is_file():
        raise MnemoError("NO_SUCH_GENERATED_SKILL", f"{REVIEW_DIR}/{name} does not exist (only generated skills can be patched)")
    rel = validate_relpath(file).as_posix()
    if rel == MANIFEST:
        raise ValidationError("BAD_FILE", f"{MANIFEST} is written by the engine")
    draft = engine(vault) / "work" / "skills" / f"patch-{time.time_ns()}" / name
    shutil.copytree(source, draft, ignore=shutil.ignore_patterns(MANIFEST))
    target = draft / rel
    if not target.is_file():
        raise MnemoError("NO_SUCH_FILE", f"{rel} is not part of {name}")
    text = target.read_text("utf-8")
    count = text.count(old) if old else 0
    if count != 1:
        raise ValidationError("PATCH_NOT_UNIQUE", f"the text to replace occurs {count} times in {rel}; it must occur exactly once")
    target.write_text(text.replace(old, new, 1), "utf-8")
    state = load_state(vault, "skills.json", STATE_SCHEMA, {"generated": {}, "installs": []})
    info = state["generated"].get(name, {})
    result = submit(vault, str(draft), origin=info.get("origin", "manual"), sources=info.get("sources"), replace=True)
    installed = [i["path"] for i in state["installs"] if i["name"] == name]
    return {**result, "patched": rel, "installed_copies": installed,
            "next": f"mnemoloci skills install {name} --target … --replace" if installed else result["next"]}


def list_generated(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    state = load_state(vault, "skills.json", STATE_SCHEMA, {"generated": {}, "installs": []})
    out = []
    for folder in sorted((Path(vault) / REVIEW_DIR).glob("*/SKILL.md")):
        name = folder.parent.name
        info = read_skill(folder)
        installs = [i for i in state["installs"] if i["name"] == name]
        out.append({"name": name, "description": info["description"], **state["generated"].get(name, {}),
                    "installed_at": [i["path"] for i in installs]})
    return {"generated": out}


def _install_root(target: str, project_dir: str | None) -> Path:
    if target == "claude":
        dirs = claude_dirs()
        return (dirs[0] if dirs else Path.home() / ".claude") / "skills"
    if target == "agents":
        return Path.home() / ".agents" / "skills"
    if target == "project":
        if not project_dir:
            raise MnemoError("NO_PROJECT_DIR", "--project-dir is required for target project")
        return Path(project_dir) / ".claude" / "skills"
    raise MnemoError("BAD_TARGET", "target must be claude, agents or project")


def install(vault: Path, name: str, *, target: str, replace: bool = False, project_dir: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    source = vault / REVIEW_DIR / name
    if not (source / "SKILL.md").is_file():
        raise MnemoError("NO_SUCH_GENERATED_SKILL", f"{REVIEW_DIR}/{name} does not exist")
    staged = engine(vault) / "work" / "install" / name
    with VaultLock(vault) as lock:
        if staged.exists():
            versions = engine(vault) / "work" / "install-stale"
            versions.mkdir(parents=True, exist_ok=True)
            os.replace(staged, versions / f"{name}-{time.time_ns()}")
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns(MANIFEST))
        errors, _ = validate_draft(staged)  # re-check: the review copy may have been edited in Obsidian
        if errors:
            raise ValidationError("SKILL_INVALID", "the reviewed skill no longer validates", errors)
        scan = scan_dir(staged)
        if scan["verdict"] == "dangerous":
            raise ConflictError("SKILL_BLOCKED", "security scan found dangerous content; not installed",
                                [f for f in scan["findings"] if f["severity"] == "dangerous"])
        root = _install_root(target, project_dir)
        root.mkdir(parents=True, exist_ok=True)
        dest = root / name
        backup = None
        if dest.exists() or dest.is_symlink():
            if not replace:
                raise ConflictError("NAME_TAKEN", f"{dest} already exists; use --replace (the old copy is backed up)")
            backup = engine(vault) / "skill-backups" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
        temp = root / f".{name}.installing-{time.time_ns()}"
        shutil.copytree(staged, temp)
        os.replace(temp, dest)
        state = load_state(vault, "skills.json", STATE_SCHEMA, {"generated": {}, "installs": []})
        state["installs"].append({"name": name, "target": target, "path": str(dest), "installed": now_iso(),
                                  "backup": str(backup) if backup else None, "guard_verdict": scan["verdict"]})
        save_state(vault, lock, "skills.json", state)
        invalidate(vault)
    return {"installed": name, "path": str(dest), "backup_of_previous": str(backup) if backup else None,
            "guard_verdict": scan["verdict"], "note": "visible to Claude Code from the next session"}


def audit(vault: Path | None, *, name: str | None = None, path: str | None = None) -> dict[str, Any]:
    """Security-scan any existing skill (installed, marketplace, third-party folder)."""
    if path:
        folder = Path(path)
        folder = folder.parent if folder.name == "SKILL.md" else folder
        return {"path": str(folder), **scan_dir(folder)}
    if not name or vault is None:
        raise MnemoError("NOTHING_TO_AUDIT", "give a skill name or --path")
    locations = name_locations(vault, name)
    if not locations:
        raise MnemoError("NO_SUCH_SKILL", f"no skill named {name!r}")
    return {"name": name, "results": [{"label": loc["label"], "path": str(Path(loc["path"]).parent),
                                       **scan_dir(Path(loc["path"]).parent)} for loc in locations]}
