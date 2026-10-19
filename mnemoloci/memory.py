"""Working memory notes: decisions, facts, lessons,
handoffs, preferences, runbooks. Create-only; superseded notes are marked, never
deleted; there is no decay or expiry."""

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from . import content_guard, i18n
from .core import txn
from .core.errors import MnemoError, ValidationError
from .core.lock import VaultLock
from .index import update as index_update
from .markdown import render_page, significant_words, slugify
from .pages import iter_pages, load_page
from .secrets import redact
from .vault import require_vault

MEMORY_SCHEMA = "mnemoloci.memory.v1"
KINDS = {
    "decision": "memory/decisions",
    "fact": "memory/facts",
    "lesson": "memory/lessons",
    "handoff": "memory/handoffs",
    "preference": "memory/preferences",
    "runbook": "memory/runbooks",
}
MAX_BODY = 20_000
REINFORCED_KINDS = {"lesson", "preference"}  # learned again -> confirmed, never decayed
CONFIDENCE = ("verified", "inferred", "unverified")
GLOBAL_KINDS = {"lesson", "preference", "runbook"}  # re-injected in any project when no project is set
LONG_BODY = 8_000
_SPEAKER = re.compile(r"^\s*(?:[-*>]\s*)?(?:utente|user|assistant|assistente|claude|human|umano)\s*[:>]", re.I | re.M)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic|jan|may|jun|jul|aug|sep|oct|dec)\w*\s+\d{4}\b", re.I)
# a measurement, price or version that will not stay true: "9,3 s/token", "45%", "2 GB", "v8.4"
_VOLATILE = re.compile(r"\b\d[\d.,]*\s?(?:%|€|\$|(?:ms|s/token|tok/s|gb|mb|kb|x)\b)|\bv\d+\.\d+", re.I)


def contract(kind: str, project: str | None, body: str, confidence: str, verification: str | None,
             generality: str | None, vault: Path | None = None) -> dict[str, Any]:
    """What a saved memory claims about itself, kept honest without refusing it. Only a note that
    is not a memory at all (a pasted transcript) is refused; a weak claim is kept, its confidence
    is capped at `inferred` and the caller is told why. Returns the confidence to store, the one
    claimed, and the warnings."""
    if len(_SPEAKER.findall(body)) >= 4:
        raise ValidationError("TRANSCRIPT_LIKE", "this looks like a pasted conversation, not a memory: "
                              "write the lesson, decision or fact it contains")
    def T(it: str, en: str) -> str:
        return i18n.t(vault, it, en)

    warnings: list[str] = []
    stored = confidence
    if confidence == "verified":
        if not verification or len(verification) < 10:
            warnings.append(T("segnata `verified` senza `verification`: portata a `inferred` "
                              "(di' come l'hai verificata con --verification)",
                              "marked `verified` without `verification`: lowered to `inferred` "
                              "(say how you verified it with --verification)"))
            stored = "inferred"
        elif _VOLATILE.search(body) and not _DATE.search(f"{body} {verification}"):
            warnings.append(T("contiene cifre o versioni che invecchiano senza una data: portata a `inferred` "
                              "(scrivi quando le hai misurate)",
                              "contains figures or versions that go stale and no date: lowered to `inferred` "
                              "(write when you measured them)"))
            stored = "inferred"
    if not project and kind in GLOBAL_KINDS and not generality:
        warnings.append(T("nota senza progetto, quindi valida ovunque: di' perché con --generality "
                          "(o legala a un progetto con --project)",
                          "note without a project, so valid everywhere: say why with --generality "
                          "(or tie it to a project with --project)"))
    if len(body) > LONG_BODY:
        warnings.append(T(f"lunga ({len(body)} caratteri): una memoria breve viene letta, una lunga no",
                          f"long ({len(body)} characters): a short memory gets read, a long one does not"))
    return {"confidence": stored, "claimed": confidence, "warnings": warnings}


def norm_project(project: str | None) -> str | None:
    if not project:
        return None
    return str(Path(project)).rstrip("\\/").casefold()


IDENTITY_FILE = ".mnemoloci-project"


def project_identity(path: str | None) -> str | None:
    """"@name" when the folder, or one above it, holds a `.mnemoloci-project` file (its first line is
    the name). The name follows the project when the folder is renamed or opened from another
    machine; a note stored under it is found from anywhere inside the project."""
    if not path or path.startswith("@"):
        return path.casefold() if path else None
    try:
        folder = Path(path)
        for candidate in (folder, *folder.parents):
            marker = candidate / IDENTITY_FILE
            if marker.is_file():
                for line in marker.read_text("utf-8", "replace").splitlines():
                    name = line.strip()
                    if name and not name.startswith("#"):
                        return "@" + name.casefold()
                return None
    except OSError:
        return None
    return None


def project_key(project: str | None) -> str | None:
    """What a project is compared by: its identity when it has one, else the normalised path."""
    return project_identity(project) or norm_project(project)


def stored_project(project: str | None) -> str | None:
    """What to write in a note: the identity when the folder has one, else the path as given."""
    return project_identity(project) or project


def covers(project: str | None, here: str | None) -> bool:
    """A note for project C:\\Users\\you\\proj also applies to a session opened in C:\\Users\\you\\proj\\src."""
    if project and project.startswith("@"):
        return project_identity(here) == project.casefold()
    if here and here.startswith("@"):
        return project_identity(project) == here.casefold()
    p, h = norm_project(project), norm_project(here)
    return bool(p and h) and (h == p or h.startswith(p + "\\") or h.startswith(p + "/"))


RETRY_WINDOW = 24 * 3600  # an identical automatic note inside this window is a retry, not a new note


def content_key(kind: str, project: str | None, title: str, body: str) -> str:
    """What identifies a note by its content alone (kind, project, title, text; spacing and case
    ignored). The same key twice is the same save done again: a hook that fired twice, or a queued
    handoff replayed."""
    parts = (kind, project_key(project) or "", title, body)
    text = "\x1f".join(" ".join(p.split()).casefold() for p in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _recent_duplicate(vault: Path, kind: str, key: str) -> Any | None:
    """A memory note with this content key saved within RETRY_WINDOW and not retracted."""
    now = time.time()
    for page in iter_pages(vault, ("memory",)):
        pfm = page.frontmatter
        if page.error or pfm.get("contentKey") != key or pfm.get("memoryKind") != kind or pfm.get("status") == "retracted":
            continue
        try:
            made = time.mktime(time.strptime(str(pfm.get("created")), "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if 0 <= now - made <= RETRY_WINDOW:
            return page
    return None


DUPLICATE_SCORE = 0.75  # a lesson/preference this close to an existing one is the same one: confirm it
RELATED_SCORE = 0.35  # close enough to mention to the caller, never enough to act on
# On the real vault's memory notes the score was 0.90 for the one true duplicate and <= 0.12 for every
# other pair of the same kind, so both thresholds sit in an empty gap.


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similarity(title_a: str, body_a: str, title_b: str, body_b: str) -> float:
    """0..1: title words weigh more than the body's (0.6 / 0.4), both as plain word overlap."""
    return round(0.6 * _jaccard(significant_words(title_a), significant_words(title_b))
                 + 0.4 * _jaccard(significant_words(body_a[:2000]), significant_words(body_b[:2000])), 3)


def _similar(vault: Path, kind: str, project: str | None, title: str, body: str) -> list[tuple[float, Any]]:
    """Active notes of the same kind and project that resemble this one, best first. The same
    title is a certain match whatever the body says."""
    wanted = project_key(project)
    found = []
    for page in iter_pages(vault, ("memory",)):
        pfm = page.frontmatter
        if (page.error or pfm.get("schema") != MEMORY_SCHEMA or pfm.get("memoryKind") != kind
                or pfm.get("status") != "active" or project_key(pfm.get("project")) != wanted):
            continue
        same = slugify(str(pfm.get("title", ""))) == slugify(title)
        found.append((1.0 if same else similarity(title, body, str(pfm.get("title", "")), page.body), page))
    return sorted(found, key=lambda m: -m[0])


def _apply(vault: Path, lock: VaultLock, operation: str, writes: list[dict[str, Any]]) -> None:
    bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"{operation}-{time.time_ns()}", "operation": operation,
              "writes": writes}
    txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    index_update(vault, [w["path"] for w in writes], lock)


def add(vault: Path, kind: str, title: str, text: str, *, project: str | None = None,
        tags: list[str] | None = None, source: str = "manual", auto: bool = False,
        confidence: str | None = None, verification: str | None = None, generality: str | None = None,
        lock: VaultLock | None = None) -> dict[str, Any]:
    require_vault(vault)
    if kind not in KINDS:
        raise ValidationError("BAD_MEMORY_KIND", f"kind must be one of {sorted(KINDS)}")
    confidence = confidence or ("unverified" if auto else "inferred")
    if confidence not in CONFIDENCE:
        raise ValidationError("BAD_CONFIDENCE", f"confidence must be one of {list(CONFIDENCE)}")
    project = stored_project(project)
    verification = " ".join((verification or "").split()) or None
    generality = " ".join((generality or "").split()) or None
    if verification:
        verification, _ = redact(verification)
    if generality:
        generality, _ = redact(generality)
    title = (title or "").strip()
    if not title or len(title) > 200 or "\n" in title:
        raise ValidationError("BAD_TITLE", "title must be one line, max 200 chars")
    title, title_redactions = redact(title)  # the title becomes a heading and a file name
    body, redactions = redact((text or "").strip())
    redactions += title_redactions
    if not body:
        raise ValidationError("EMPTY_TEXT", "memory text is empty")
    # Memory is re-injected into future sessions: explicit notes that read like orders to
    # an AI are refused; automatic ones (hooks quoting the transcript) are neutralised.
    if auto:
        body, _ = content_guard.neutralize(body)
    else:
        if len(_SPEAKER.findall(body)) >= 4:  # say what it is before the injection guard calls it something else
            raise ValidationError("TRANSCRIPT_LIKE", "this looks like a pasted conversation; save the conclusion, not the transcript")
        content_guard.require_safe(f"{title}\n{body}\n{verification or ''}\n{generality or ''}", "this memory note")
    if len(body) > MAX_BODY:
        raise ValidationError("TEXT_TOO_LONG", f"memory text over {MAX_BODY} chars; summarize it first")
    checked = ({"confidence": confidence, "claimed": confidence, "warnings": []} if auto
               else contract(kind, project, body, confidence, verification, generality, vault))
    date = time.strftime("%Y-%m-%d")
    base = f"{KINDS[kind]}/{date}-{slugify(title)[:60]}"
    fm = {
        "title": title, "schema": MEMORY_SCHEMA, "id": "", "memoryKind": kind, "status": "active",
        "project": project, "tags": list(tags or []), "source": source, "auto": auto,
        "confidence": checked["confidence"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "redactions": redactions,
        "contentKey": content_key(kind, project, title, body),
    }
    if checked["claimed"] != checked["confidence"]:
        fm["claimedConfidence"] = checked["claimed"]
    if verification:
        fm["verification"] = verification
    if generality:
        fm["generality"] = generality

    def reinforce(held: VaultLock, matches: list[tuple[float, Any]]) -> dict[str, Any] | None:
        """The same lesson/preference learned again (same title, or the same thing in other words)
        strengthens the note instead of duplicating it."""
        if kind not in REINFORCED_KINDS or auto or not matches or matches[0][0] < DUPLICATE_SCORE:
            return None
        score, page = matches[0]
        pfm = page.frontmatter
        count = int(pfm.get("confirmations", 1)) + 1
        new_fm = dict(pfm, confirmations=count, lastConfirmed=time.strftime("%Y-%m-%d"),
                      redactions=int(pfm.get("redactions", 0)) + redactions)
        same_title = slugify(str(pfm.get("title", ""))) == slugify(title)
        today = time.strftime("%Y-%m-%d")
        new_body = (page.body.rstrip() + "\n\n"
                    + i18n.t(vault, f"## Confermata il {today} ({count}ª volta)", f"## Confirmed on {today} (time {count})")
                    + "\n\n"
                    + ("" if same_title else i18n.t(vault, f"_Ripresa con il titolo «{title}»._",
                                                     f"_Taken up again under the title «{title}»._") + "\n\n") + body)
        _apply(vault, held, "memory-reinforce", [{"path": page.path, "action": "replace",
                                                   "expected_sha256": page.sha256,
                                                   "content": render_page(new_fm, new_body)}])
        return {"id": pfm.get("id"), "path": page.path, "redactions": redactions, "reinforced": True,
                "confirmations": count, "matched": "title" if same_title else "similar", "score": score}

    def write(held: VaultLock) -> dict[str, Any]:
        if auto or kind == "handoff":  # the same save done again (a retried hook, a replayed queue) writes nothing
            same = _recent_duplicate(vault, kind, fm["contentKey"])
            if same is not None:
                return {"id": same.frontmatter.get("id"), "path": same.path, "redactions": 0, "duplicate": True}
        matches = [] if auto else _similar(vault, kind, project, title, body)
        again = reinforce(held, matches)
        if again:
            return again
        rel, n = f"{base}.md", 2
        while (Path(vault) / rel).exists():
            rel, n = f"{base}-{n}.md", n + 1
        fm["id"] = f"mem_{Path(rel).stem}"
        _apply(vault, held, "memory-add", [{"path": rel, "action": "create", "content": render_page(fm, f"# {title}\n\n{body}")}])
        result = {"id": fm["id"], "path": rel, "redactions": redactions, "confidence": checked["confidence"]}
        if checked["warnings"]:
            result["warnings"] = checked["warnings"]
        related = [(s, p) for s, p in matches if s >= RELATED_SCORE][:3]
        if related:  # reported, never acted on: only a person knows whether it is the same thing
            result["similar"] = [{"id": p.frontmatter.get("id"), "title": p.frontmatter.get("title"), "score": s}
                                 for s, p in related]
        return result

    if lock is not None:
        return write(lock)
    with VaultLock(vault) as own:
        return write(own)


def _find(vault: Path, memory_id: str):
    for page in iter_pages(vault, ("memory",)):
        if page.frontmatter.get("id") == memory_id or page.path == memory_id:
            return page
    raise MnemoError("NO_SUCH_MEMORY", f"no memory note {memory_id}")


def supersede(vault: Path, memory_id: str, by: str, *, lock: VaultLock | None = None) -> dict[str, Any]:
    require_vault(vault)

    def mark(held: VaultLock) -> dict[str, Any]:
        old, new = _find(vault, memory_id), _find(vault, by)
        fm = dict(old.frontmatter, status="superseded", supersededBy=new.frontmatter.get("id"))
        _apply(vault, held, "memory-supersede", [{
            "path": old.path, "action": "replace", "expected_sha256": old.sha256,
            "content": render_page(fm, old.body),
        }])
        return {"superseded": old.frontmatter.get("id"), "by": new.frontmatter.get("id")}

    if lock is not None:
        return mark(lock)
    with VaultLock(vault) as own:
        return mark(own)


def retract(vault: Path, memory_id: str, *, reason: str, redact_text: bool = False) -> dict[str, Any]:
    """The safe alternative to forgetting (without deletion): the note stays,
    marked retracted, and is never injected or suggested again. With redact_text its body is
    replaced by a placeholder; the file remains, and earlier text survives only in the git backup."""
    require_vault(vault)
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("NO_REASON", "say why the note is retracted")
    with VaultLock(vault) as lock:
        page = _find(vault, memory_id)
        fm = dict(page.frontmatter, status="retracted", retracted=time.strftime("%Y-%m-%d"), retractedReason=reason)
        body = page.body
        if redact_text:
            fm["redacted"] = True
            body = f"# {fm.get('title', '')}\n\n_[Testo ritirato e oscurato il {fm['retracted']}: {reason}]_"
        _apply(vault, lock, "memory-retract", [{"path": page.path, "action": "replace", "expected_sha256": page.sha256,
                                                "content": render_page(fm, body)}])
    return {"retracted": fm.get("id"), "path": page.path, "redacted": redact_text,
            "note": "kept, never injected again" + ("; the earlier text is only in the git backup, if enabled"
                                                    if redact_text else "")}


def suppress(vault: Path, memory_id: str) -> dict[str, Any]:
    """Hide a note from hints/listings without saying it was wrong (a reversible top-down suppression, not a deletion or a retraction). Unlike `retract`, no reason is required and there's a way back -- for a note
    that's just not relevant right now, not one that turned out to be false."""
    require_vault(vault)
    with VaultLock(vault) as lock:
        page = _find(vault, memory_id)
        if page.frontmatter.get("status") != "active":
            raise MnemoError("NOT_ACTIVE", f"{memory_id} is {page.frontmatter.get('status')}, not active")
        fm = dict(page.frontmatter, status="suppressed", suppressed=time.strftime("%Y-%m-%d"))
        _apply(vault, lock, "memory-suppress", [{"path": page.path, "action": "replace",
                                                  "expected_sha256": page.sha256, "content": render_page(fm, page.body)}])
    return {"suppressed": fm.get("id"), "path": page.path, "note": "kept, hidden from hints; `memory unsuppress` reverses this"}


def unsuppress(vault: Path, memory_id: str) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        page = _find(vault, memory_id)
        if page.frontmatter.get("status") != "suppressed":
            raise MnemoError("NOT_SUPPRESSED", f"{memory_id} is {page.frontmatter.get('status')}, not suppressed")
        fm = {k: v for k, v in page.frontmatter.items() if k != "suppressed"}
        fm["status"] = "active"
        _apply(vault, lock, "memory-unsuppress", [{"path": page.path, "action": "replace",
                                                    "expected_sha256": page.sha256, "content": render_page(fm, page.body)}])
    return {"unsuppressed": fm.get("id"), "path": page.path}


def list_memory(vault: Path, *, kind: str | None = None, project: str | None = None,
                status: str | None = "active", source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    require_vault(vault)
    wanted = project or None  # covers() normalises, and needs the real path to look for the identity file
    out = []
    for page in iter_pages(vault, ("memory",)):
        fm = page.frontmatter
        if page.error or fm.get("schema") != MEMORY_SCHEMA:
            continue
        if kind and fm.get("memoryKind") != kind:
            continue
        if status and fm.get("status") != status:
            continue
        if source and fm.get("source") != source:
            continue
        if wanted and not covers(fm.get("project"), wanted):
            continue
        out.append({"id": fm.get("id"), "kind": fm.get("memoryKind"), "title": fm.get("title"),
                    "project": fm.get("project"), "created": fm.get("created"), "path": page.path,
                    "auto": fm.get("auto", False), "source": fm.get("source", "manual"), "tags": fm.get("tags") or [],
                    "confidence": fm.get("confidence"), "retractedReason": fm.get("retractedReason")})
    out.sort(key=lambda m: str(m["created"]), reverse=True)
    return out[:limit]


def read(vault: Path, memory_id: str) -> dict[str, Any]:
    page = _find(vault, memory_id)
    return {"path": page.path, "frontmatter": page.frontmatter, "body": load_page(vault, page.path).body}
