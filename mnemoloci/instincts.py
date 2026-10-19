"""Instincts: small learned rules ("when X, do Y") derived from the user's
corrections and confirmations in past Claude Code sessions.

An instinct is NEVER
injected into a session until the user approves it. Evidence must point at real
moments the engine mined from transcripts. Confidence is a fixed function of how
many distinct sessions support it. Nothing decays; rejected and retired instincts
are kept with their status.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from .core import txn
from .core.errors import MnemoError, ValidationError
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .index import update as index_update
from .markdown import render_page, slugify
from .memory import covers, norm_project
from .pages import iter_pages
from .personal import check_text
from .secrets import redact
from .sessions import claude_dirs, parse_transcript
from .vault import engine, load_state, now_iso, require_vault, save_state

SCHEMA = "mnemoloci.instinct.v1"
PROPOSALS_SCHEMA = "mnemoloci.instinct-proposals.v1"
MOMENTS_SCHEMA = "mnemoloci.instinct-moments.v1"
FOLDER = "memory/instincts"
STATUSES = ("proposed", "active", "rejected", "retired")
MAX_INJECTED = 8

_CORRECTION_START = re.compile(
    r"^\s*(no[\s,.!]|nope\b|sbagliat|invece\b|in realt[aàá]\b|aspetta\b|fermo\b|smettila|basta\b|"
    r"ti (ho|avevo) (detto|chiesto)|perch[eéè] hai\b|non devi|non volevo|wrong\b|don'?t\b|stop\b|instead\b|"
    r"i said\b|that'?s not\b)", re.IGNORECASE)
# A bare leading "non" is often a question ("non si può fare…?"): weaker signal.
_NON_START = re.compile(r"^\s*non\b", re.IGNORECASE)
_CORRECTION_ANY = re.compile(
    r"\b(non (è|e'|era) (quello|questo|corretto|giusto)|non ho (mai )?(chiesto|voluto)|ti avevo chiesto|"
    r"sei sicur[oa]|non essere pigr[oa]|ricordati (di|che)|la prossima volta|d'ora in poi|mai pi[uù]|"
    r"not what i (asked|meant)|next time|from now on)\b", re.IGNORECASE)
_CONFIRMATION = re.compile(
    r"^\s*(perfetto|esatto|ottimo|giustissimo|s[iì],? (così|esatto|perfetto)|va benissimo|proprio così|"
    r"exactly|perfect|great,? keep)", re.IGNORECASE)


def _classify(text: str) -> tuple[str, float] | None:
    if _CORRECTION_START.search(text):
        return "correction", 1.0
    if _CORRECTION_ANY.search(text):
        return "correction", 0.7
    if _NON_START.search(text):
        return "correction", 0.5
    if _CONFIRMATION.search(text):
        return "confirmation", 0.6
    return None


def _cap(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def confidence_for(sessions: int) -> float:
    """Evidence table: more independent sessions, more confidence."""
    if sessions >= 11:
        return 0.85
    if sessions >= 6:
        return 0.7
    if sessions >= 3:
        return 0.5
    return 0.3


def _instinct_id(trigger: str, action: str) -> str:
    """Stable id from the rule's text; long rules get a hash of the whole text, so two rules that
    only differ after the first 70 characters never share an id."""
    slug = slugify(f"{trigger} {action}")
    if len(slug) <= 70:
        return f"ins_{slug}"
    full = " ".join(f"{trigger} {action}".casefold().split())  # the whole text: slugify stops at 80 chars
    return f"ins_{slug[:60].rstrip('-')}-{sha256_bytes(full.encode('utf-8'))[:8]}"


def mine(vault: Path, *, include: list[str] | None = None, since: str | None = None,
         limit: int = 200) -> dict[str, Any]:
    """Find correction/confirmation moments in transcripts; write a packet for the agent."""
    require_vault(vault)
    include = [s.casefold() for s in include or []]
    with VaultLock(vault) as lock:
        state = load_state(vault, "instinct_moments.json", MOMENTS_SCHEMA, {"moments": {}})
        new = []
        for base in claude_dirs():
            for path in sorted((base / "projects").glob("*/*.jsonl")):
                info = parse_transcript(path)
                cwd = info.get("cwd") or path.parent.name
                if include and not any(s in cwd.casefold() for s in include):
                    continue
                messages = info["messages"]
                for prev, msg in zip(messages, messages[1:]):
                    if msg["role"] != "user" or prev["role"] != "assistant":
                        continue
                    if since and msg["ts"][:10] < since:
                        continue
                    kind = _classify(msg["text"])
                    if not kind:
                        continue
                    key = f"{info['session_id']}@{msg['ts']}"
                    if key in state["moments"]:
                        continue
                    moment = {
                        "id": key, "session_id": info["session_id"], "ts": msg["ts"], "project": cwd,
                        "kind": kind[0], "strength": kind[1],
                        "user": _cap(redact(msg["text"])[0], 700),
                        "assistant_before": _cap(redact(prev["text"])[0][-700:], 700),
                    }
                    new.append(moment)
        new.sort(key=lambda m: (-m["strength"], m["ts"]))
        new = new[:limit]
        for moment in new:  # only what is handed over counts as seen: the rest comes back next time
            state["moments"][moment["id"]] = {k: moment[k] for k in ("session_id", "ts", "project", "kind", "user")}
        save_state(vault, lock, "instinct_moments.json", state)
        out_dir = engine(vault) / "work" / "instincts"
        out_dir.mkdir(parents=True, exist_ok=True)
        packet = out_dir / f"moments-{time.strftime('%Y%m%d-%H%M%S')}.json"
        packet.write_text(json.dumps({"schema": MOMENTS_SCHEMA, "moments": new}, ensure_ascii=False, indent=1), "utf-8")
    return {"new_moments": len(new), "packet": str(packet), "known_moments": len(state["moments"]),
            "next": "write proposals, then: mnemoloci instincts propose <file>"}


def _notes(vault: Path) -> dict[str, Any]:
    return {p.frontmatter.get("id"): p for p in iter_pages(vault, ("memory",))
            if not p.error and p.frontmatter.get("schema") == SCHEMA}


def _render(fm: dict[str, Any]) -> str:
    lines = [f"# {fm['title']}", "", f"**Quando:** {fm['trigger']}", "", f"**Allora:** {fm['action']}", "",
             f"Stato: {fm['status']} · confidenza {fm['confidence']} · ambito: {fm['scope']}", "", "## Evidenze", ""]
    lines += [f"- {e['ts'][:16].replace('T', ' ')} ({e['session_id'][:8]}): {e['quote']}" for e in fm["evidence"]]
    return render_page(fm, "\n".join(lines))


def _apply(vault: Path, lock: VaultLock, writes: list[dict[str, Any]], operation: str) -> None:
    bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"{operation}-{time.time_ns()}", "operation": operation,
              "writes": writes}
    txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    index_update(vault, [w["path"] for w in writes], lock)


def propose(vault: Path, proposals_file: str) -> dict[str, Any]:
    require_vault(vault)
    try:
        data = json.loads(Path(proposals_file).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise MnemoError("BAD_PROPOSALS", f"cannot read {proposals_file}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != PROPOSALS_SCHEMA or not isinstance(data.get("proposals"), list):
        raise ValidationError("BAD_PROPOSALS", f"schema must be {PROPOSALS_SCHEMA} with a proposals list")
    with VaultLock(vault) as lock:
        moments = load_state(vault, "instinct_moments.json", MOMENTS_SCHEMA, {"moments": {}})["moments"]
        existing = _notes(vault)
        problems, writes, created, merged = {}, [], [], []
        for n, p in enumerate(data["proposals"]):
            where = f"proposals[{n}]"
            try:
                trigger = check_text(p.get("trigger"), "trigger")
                action = check_text(p.get("action"), "action")
            except ValidationError as exc:
                problems[where] = [exc.message]
                continue
            scope = p.get("scope") or "global"
            evidence = []
            for e in p.get("evidence") or []:
                key = f"{e.get('session_id')}@{e.get('ts')}"
                if key not in moments:
                    problems.setdefault(where, []).append(f"evidence {key} is not a mined moment")
                    continue
                evidence.append({"session_id": e["session_id"], "ts": e["ts"], "project": moments[key]["project"],
                                 "quote": _cap(redact(str(e.get("quote") or moments[key]["user"]))[0], 300)})
            if not evidence:
                problems.setdefault(where, []).append("at least one valid evidence moment is required")
                continue
            if where in problems:
                continue
            iid = _instinct_id(trigger, action)
            rel = f"{FOLDER}/{iid[4:]}.md"
            old = existing.get(iid)
            if old:
                fm = dict(old.frontmatter)
                seen = {(e["session_id"], e["ts"]) for e in fm["evidence"]}
                fm["evidence"] = fm["evidence"] + [e for e in evidence if (e["session_id"], e["ts"]) not in seen]
                fm["updated"] = now_iso()
                merged.append(iid)
                action_kind, base = "replace", old.sha256
            else:
                fm = {"title": _cap(f"{trigger} → {action}", 120), "schema": SCHEMA, "id": iid, "trigger": trigger,
                      "action": action, "scope": scope, "status": "proposed", "evidence": evidence,
                      "created": now_iso(), "updated": now_iso()}
                created.append(iid)
                action_kind, base = "create", None
            fm["confidence"] = confidence_for(len({e["session_id"] for e in fm["evidence"]}))
            entry = {"path": rel, "action": action_kind, "content": _render(fm)}
            if base:
                entry["expected_sha256"] = base
            writes.append(entry)
        if problems:
            raise ValidationError("PROPOSALS_INVALID", "fix these proposals and submit again", problems)
        if writes:
            _apply(vault, lock, writes, "instincts-propose")
    return {"created": created, "merged_evidence": merged,
            "note": "proposed instincts are NOT used until approved (mnemoloci instincts approve <id>)"}


def list_instincts(vault: Path, status: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    out = []
    for iid, page in sorted(_notes(vault).items()):
        fm = page.frontmatter
        if status and fm.get("status") != status:
            continue
        out.append({"id": iid, "status": fm.get("status"), "trigger": fm.get("trigger"), "action": fm.get("action"),
                    "scope": fm.get("scope"), "confidence": fm.get("confidence"),
                    "evidence": len(fm.get("evidence", [])), "path": page.path})
    return {"instincts": out}


def set_status(vault: Path, iid: str, status: str, reason: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    if status not in STATUSES:
        raise ValidationError("BAD_STATUS", f"status must be one of {STATUSES}")
    with VaultLock(vault) as lock:
        page = _notes(vault).get(iid)
        if page is None:
            raise MnemoError("NO_SUCH_INSTINCT", f"no instinct {iid}")
        fm = dict(page.frontmatter, status=status, updated=now_iso())
        if status == "active":
            fm["approved"] = now_iso()
        if reason:
            fm["status_reason"] = reason
        _apply(vault, lock, [{"path": page.path, "action": "replace", "expected_sha256": page.sha256,
                              "content": _render(fm)}], f"instinct-{status}")
    return {"id": iid, "status": status}


_EVOLVE_STOP = {"il", "lo", "la", "gli", "le", "un", "una", "di", "da", "del", "della", "per", "con", "che", "non",
                "quando", "utente", "chiede", "sempre", "prima", "dopo", "the", "and", "when", "user"}


def evolve(vault: Path, *, min_size: int = 2) -> dict[str, Any]:
    """Group approved instincts that share a subject: each group is material
    for one skill, which Claude drafts and submits with `skills submit --origin instincts`."""
    require_vault(vault)
    active = [i for i in list_instincts(vault, "active")["instincts"]]
    words = {i["id"]: {w for w in re.findall(r"[a-z0-9]{4,}", slugify(f"{i['trigger']} {i['action']}").replace("-", " "))
                       if w not in _EVOLVE_STOP} for i in active}
    parent = {i["id"]: i["id"] for i in active}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ids = sorted(words)
    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            union = words[a] | words[b]
            if union and len(words[a] & words[b]) / len(union) >= 0.25:
                parent[find(b)] = find(a)
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in active:
        groups.setdefault(find(i["id"]), []).append(i)
    clusters = []
    for members in groups.values():
        if len(members) < min_size:
            continue
        shared = set.intersection(*(words[m["id"]] for m in members)) or set.union(*(words[m["id"]] for m in members))
        clusters.append({"theme": sorted(shared)[:6], "instincts": [
            {"id": m["id"], "trigger": m["trigger"], "action": m["action"], "evidence": m["evidence"],
             "path": m["path"]} for m in sorted(members, key=lambda m: m["id"])]})
    clusters.sort(key=lambda c: -len(c["instincts"]))
    return {"clusters": clusters, "active": len(active),
            "next": "draft a skill from a cluster (mnemoloci skills draft <name>), then "
                    "`skills submit <draft> --origin instincts --source <instinct path>...`"}


EXPORT_SCHEMA = "mnemoloci.instincts-export.v1"


def _rule_words(i: dict[str, Any]) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", slugify(f"{i['trigger']} {i['action']}").replace("-", " "))
            if w not in _EVOLVE_STOP}


def promotion_candidates(vault: Path) -> dict[str, Any]:
    """Project-scoped active instincts that recur in two or more projects: the user decides with `instincts promote`."""
    require_vault(vault)
    # C:\Users\you\P and c:/users/you/p are one project
    local = [dict(i, words=_rule_words(i), scope=norm_project(i["scope"]) or i["scope"])
             for i in list_instincts(vault, "active")["instincts"] if (i.get("scope") or "global") != "global"]
    groups, used = [], set()
    for n, a in enumerate(local):
        if a["id"] in used:
            continue
        same = [a] + [b for b in local[n + 1:] if b["id"] not in used and b["scope"] != a["scope"]
                      and a["words"] and len(a["words"] & b["words"]) / len(a["words"] | b["words"]) >= 0.6]
        projects = {i["scope"] for i in same}
        if len(projects) >= 2:
            used |= {i["id"] for i in same}
            groups.append({"instincts": [{k: i[k] for k in ("id", "trigger", "action", "scope")} for i in same],
                           "projects": sorted(projects)})
    return {"candidates": groups, "next": "mnemoloci instincts promote <id>  (then retire the project copies you no longer need)"}


def promote(vault: Path, iid: str) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        page = _notes(vault).get(iid)
        if page is None:
            raise MnemoError("NO_SUCH_INSTINCT", f"no instinct {iid}")
        fm = dict(page.frontmatter)
        if (fm.get("scope") or "global") == "global":
            return {"id": iid, "scope": "global", "note": "already global"}
        fm.update(promotedFrom=fm.get("scope"), scope="global", promoted=now_iso(), updated=now_iso())
        _apply(vault, lock, [{"path": page.path, "action": "replace", "expected_sha256": page.sha256,
                              "content": _render(fm)}], "instinct-promote")
    return {"id": iid, "scope": "global", "was": fm["promotedFrom"]}


def export_instincts(vault: Path, out: str, *, statuses: tuple[str, ...] = ("active", "proposed")) -> dict[str, Any]:
    """Portable copy of the rules (to another PC or vault). Evidence quotes are already redacted."""
    require_vault(vault)
    rules = []
    for page in _notes(vault).values():
        fm = page.frontmatter
        if fm.get("status") in statuses:
            rules.append({k: fm.get(k) for k in ("id", "trigger", "action", "scope", "status", "confidence", "evidence")})
    Path(out).write_text(json.dumps({"schema": EXPORT_SCHEMA, "exported": now_iso(), "instincts": rules},
                                    ensure_ascii=False, indent=1), "utf-8")
    return {"out": str(Path(out).resolve()), "instincts": len(rules)}


def import_instincts(vault: Path, file: str) -> dict[str, Any]:
    """Rules from another vault arrive as `proposed`: never used until approved here."""
    require_vault(vault)
    try:
        data = json.loads(Path(file).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError("BAD_FILE", f"cannot read {file}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != EXPORT_SCHEMA:
        raise ValidationError("BAD_FILE", f"not a {EXPORT_SCHEMA} file")
    created, skipped = [], []
    with VaultLock(vault) as lock:
        existing = _notes(vault)
        writes = []
        for rule in data.get("instincts") or []:
            try:
                trigger, action = check_text(rule.get("trigger"), "trigger"), check_text(rule.get("action"), "action")
            except ValidationError as exc:
                skipped.append({"rule": str(rule.get("trigger"))[:60], "reason": exc.message})
                continue
            iid = _instinct_id(trigger, action)
            if iid in existing or any(w["path"].endswith(f"/{iid[4:]}.md") for w in writes):
                skipped.append({"rule": trigger[:60], "reason": "already here"})
                continue
            evidence = [{"session_id": str(e.get("session_id", "import")), "ts": str(e.get("ts", "")),
                         "project": e.get("project"), "quote": _cap(redact(str(e.get("quote", "")))[0], 300)}
                        for e in rule.get("evidence") or [] if isinstance(e, dict)]
            fm = {"title": _cap(f"{trigger} → {action}", 120), "schema": SCHEMA, "id": iid, "trigger": trigger,
                  "action": action, "scope": rule.get("scope") or "global", "status": "proposed", "imported": True,
                  "evidence": evidence or [{"session_id": "import", "ts": now_iso(), "project": None, "quote": "(importato)"}],
                  "confidence": confidence_for(len({e["session_id"] for e in evidence}) or 1),
                  "created": now_iso(), "updated": now_iso()}
            writes.append({"path": f"{FOLDER}/{iid[4:]}.md", "action": "create", "content": _render(fm)})
            created.append(iid)
        if writes:
            _apply(vault, lock, writes, "instincts-import")
    return {"created": created, "skipped": skipped, "note": "imported rules are proposed: approve the ones that apply here"}


def active_for(vault: Path, cwd: str | None) -> list[dict[str, Any]]:
    """Approved instincts that apply here: global ones plus this project's."""
    here = norm_project(cwd)
    chosen = []
    for page in _notes(vault).values():
        fm = page.frontmatter
        if fm.get("status") != "active":
            continue
        scope = fm.get("scope") or "global"
        if scope == "global" or (here and covers(scope, here)):
            chosen.append(fm)
    chosen.sort(key=lambda f: (-float(f.get("confidence", 0)), f.get("id", "")))
    return chosen[:MAX_INJECTED]
