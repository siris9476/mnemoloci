"""Personal profile and goals, stored as Obsidian notes under _meta/.

The structured data lives in the note's frontmatter (source of truth); the body
is regenerated for reading. Every change is a read-modify-write transaction
under the vault lock. Nothing is deleted: an updated fact keeps its previous
values, a dropped goal stays with status "dropped".
"""

import re
import time
from pathlib import Path
from typing import Any, Callable

from . import content_guard, i18n, memory
from .core import txn
from .core.errors import MnemoError, ValidationError
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .markdown import render_page, split_frontmatter
from .secrets import redact
from .vault import require_vault

PROFILE_PATH = "_meta/profile.md"
GOALS_PATH = "_meta/goals.md"
PROFILE_SCHEMA = "mnemoloci.profile.v1"
GOALS_SCHEMA = "mnemoloci.goals.v1"
MAX_FACTS = 80
MAX_ACTIVE_GOALS = 12
MAX_TEXT = 300
GOAL_STATUSES = ("active", "paused", "done", "dropped")
STATUS_LABELS = {"active": "Attivi", "paused": "In pausa", "done": "Completati", "dropped": "Abbandonati"}
STATUS_LABELS_EN = {"active": "Active", "paused": "Paused", "done": "Done", "dropped": "Dropped"}


def _t(it: str, en: str) -> str:
    """Italian or English by the language in force (the note being rendered sets it, see `_mutate`)."""
    return i18n.t(None, it, en)

# Identifiers too dangerous to keep in a note that is injected into every session.
_HIGH_RISK = [
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")),
    ("codice-fiscale", re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.I)),
    ("card-number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd|pin)\b\s*[:=]")),
]


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def check_text(text: str, field: str = "text") -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("EMPTY_TEXT", f"{field} is empty")
    text = text.strip()
    if "\n" in text or len(text) > MAX_TEXT:
        raise ValidationError("TEXT_TOO_LONG", f"{field} must be one line of at most {MAX_TEXT} characters")
    _, secrets_found = redact(text)
    if secrets_found:
        raise ValidationError("SECRET_IN_TEXT", f"{field} contains something that looks like a secret")
    for name, pattern in _HIGH_RISK:
        for m in pattern.finditer(text):
            if name == "card-number" and not _luhn(re.sub(r"\D", "", m.group(0))):
                continue
            raise ValidationError("SENSITIVE_DATA", f"{field} contains a {name}; not stored in memory notes")
    content_guard.require_safe(text, field)  # injected into every session: no hidden orders
    return text


def _load_note(vault: Path, rel: str, schema: str, key: str) -> tuple[list[dict[str, Any]], str | None, str]:
    path = Path(vault) / rel
    if not path.is_file():
        return [], None, ""
    data = path.read_bytes()
    text = data.decode("utf-8")
    fm, _ = split_frontmatter(text)
    if not fm or fm.get("schema") != schema or not isinstance(fm.get(key), list):
        raise MnemoError("PERSONAL_NOTE_INVALID", f"{rel} is not a valid {schema} note")
    return fm[key], sha256_bytes(data), text


def _load(vault: Path, rel: str, schema: str, key: str) -> tuple[list[dict[str, Any]], str | None]:
    items, sha, text = _load_note(vault, rel, schema, key)
    if key == "todos" and text:
        _import_ticks(items, text)  # a box ticked in Obsidian counts as done when read, too
    return items, sha


_TODO_LINE = re.compile(r"^- \[([ xX])\] \*\*\[(t\d+)\]\*\*", re.MULTILINE)
_BOX = re.compile(r"^- \[[ xX]\] ", re.MULTILINE)


def _import_ticks(todos: list[dict[str, Any]], text: str) -> list[str]:
    """Todos ticked (or unticked) by hand in Obsidian: the checkbox wins over the stored status."""
    boxes = {m.group(2): m.group(1) != " " for m in _TODO_LINE.finditer(split_frontmatter(text)[1])}
    changed = []
    for todo in todos:
        ticked = boxes.get(todo["id"])
        if ticked is True and todo["status"] == "open":
            todo.update(status="done", closed=_today(), updated=_today())
            changed.append(todo["id"])
        elif ticked is False and todo["status"] == "done":
            todo.update(status="open", closed=None, updated=_today())
            changed.append(todo["id"])
    return changed


def _save(vault: Path, lock: VaultLock, rel: str, content: str, base_sha: str | None, operation: str,
          extra: list[dict[str, Any]] | None = None) -> None:
    entry = {"path": rel, "content": content, "action": "create" if base_sha is None else "replace"}
    if base_sha:
        entry["expected_sha256"] = base_sha
    bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"{operation}-{time.time_ns()}", "operation": operation,
              "writes": [entry, *(extra or [])]}
    txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|")


def render_profile(facts: list[dict[str, Any]]) -> str:
    rows = [f"| {_cell(f['key'])} | {_cell(f['value'])} | {f.get('confidence', '')} | {_cell(f.get('source', ''))} | {f.get('updated', '')} |"
            for f in sorted(facts, key=lambda f: f["key"])]
    title = _t("Profilo", "Profile")
    body = (_t(f"# {title}\n\n> Nota generata da Mnemoloci: modificala con `mnemoloci profile set`.\n\n"
               "| Fatto | Valore | Confidenza | Fonte | Aggiornato |\n|---|---|---|---|---|\n",
               f"# {title}\n\n> Note generated by Mnemoloci: edit it with `mnemoloci profile set`.\n\n"
               "| Fact | Value | Confidence | Source | Updated |\n|---|---|---|---|---|\n") + "\n".join(rows))
    return render_page({"title": title, "schema": PROFILE_SCHEMA, "facts": facts}, body)


def render_goals(goals: list[dict[str, Any]]) -> str:
    title = _t("Obiettivi", "Goals")
    labels = STATUS_LABELS if _t("it", "en") == "it" else STATUS_LABELS_EN
    lines = [f"# {title}", "", _t("> Nota generata da Mnemoloci: modificala con `mnemoloci goals`.",
                                   "> Note generated by Mnemoloci: edit it with `mnemoloci goals`."), ""]
    for status in GOAL_STATUSES:
        group = [g for g in goals if g["status"] == status]
        if not group:
            continue
        lines += [f"## {labels[status]}", ""]
        for g in group:
            extra = [f"area: {g['area']}"] if g.get("area") else []
            extra += [f"[[{s}]]" for s in g.get("links", [])]
            extra.append(_t(f"dal {g['created']}", f"since {g['created']}") if status == "active" else f"{status} {g['updated']}")
            lines.append(f"- **[{g['id']}]** {g['text']} · " + " · ".join(extra))
        lines.append("")
    return render_page({"title": title, "schema": GOALS_SCHEMA, "goals": goals}, "\n".join(lines))


def _mutate(vault: Path, rel: str, schema: str, key: str, render: Callable, op: str,
            change: Callable[[list[dict[str, Any]]], Any]) -> Any:
    require_vault(vault)
    with VaultLock(vault) as lock, i18n.forced(i18n.lang(vault)):
        items, base, text = _load_note(vault, rel, schema, key)
        extra = []
        # the note is regenerated from its frontmatter: anything written by hand in the body would
        # be lost, so a copy of the edited note is kept next to it (ticked boxes are imported)
        if text and _BOX.sub("- [ ] ", text) != _BOX.sub("- [ ] ", render(items)):
            copy = f"{Path(rel).parent.as_posix()}/modifiche/{Path(rel).stem}-{time.strftime('%Y%m%d-%H%M%S')}.md"
            extra.append({"path": copy, "action": "create", "content": text})
        if key == "todos" and text:
            _import_ticks(items, text)
        result = change(items)
        _save(vault, lock, rel, render(items), base, op, extra)
    if extra and isinstance(result, dict):
        result = {**result, "hand_edits_kept_in": extra[0]["path"]}
    return result


def profile_show(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    return {"facts": _load(vault, PROFILE_PATH, PROFILE_SCHEMA, "facts")[0]}


def profile_set(vault: Path, key: str, value: str, *, confidence: float = 1.0, source: str = "utente") -> dict[str, Any]:
    key = check_text(key, "key").casefold()
    value = check_text(value, "value")
    source = check_text(source, "source")
    if not 0 <= confidence <= 1:
        raise ValidationError("BAD_CONFIDENCE", "confidence must be within 0..1")

    def change(facts: list[dict[str, Any]]) -> dict[str, Any]:
        existing = next((f for f in facts if f["key"] == key), None)
        if existing is None:
            if len(facts) >= MAX_FACTS:
                raise ValidationError("PROFILE_FULL", f"profile already has {MAX_FACTS} facts")
            existing = {"key": key, "previous": []}
            facts.append(existing)
        elif existing["value"] != value:
            existing.setdefault("previous", []).append({"value": existing["value"], "until": _today()})
        existing.update(value=value, confidence=confidence, source=source, updated=_today())
        return existing

    return {"fact": _mutate(vault, PROFILE_PATH, PROFILE_SCHEMA, "facts", render_profile, "profile-set", change)}


def goals_list(vault: Path, *, include_closed: bool = False) -> dict[str, Any]:
    require_vault(vault)
    goals = _load(vault, GOALS_PATH, GOALS_SCHEMA, "goals")[0]
    return {"goals": goals if include_closed else [g for g in goals if g["status"] in ("active", "paused")]}


def goals_add(vault: Path, text: str, *, area: str | None = None, links: list[str] | None = None) -> dict[str, Any]:
    text = check_text(text)
    area = check_text(area, "area") if area else None

    def change(goals: list[dict[str, Any]]) -> dict[str, Any]:
        if sum(1 for g in goals if g["status"] == "active") >= MAX_ACTIVE_GOALS:
            raise ValidationError("TOO_MANY_GOALS", f"at most {MAX_ACTIVE_GOALS} active goals; pause or close one first")
        if any(g["text"].casefold() == text.casefold() and g["status"] in ("active", "paused") for g in goals):
            raise ValidationError("DUPLICATE_GOAL", "an open goal with this text already exists")
        number = 1 + max((int(g["id"][1:]) for g in goals), default=0)
        goal = {"id": f"g{number}", "text": text, "status": "active", "area": area, "links": list(links or []),
                "created": _today(), "updated": _today()}
        goals.append(goal)
        return goal

    return {"goal": _mutate(vault, GOALS_PATH, GOALS_SCHEMA, "goals", render_goals, "goals-add", change)}


TODOS_PATH = "_meta/todos.md"
TODOS_SCHEMA = "mnemoloci.todos.v1"
TODO_STATUSES = ("open", "done", "dropped")
MAX_OPEN_TODOS = 100
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm_project(project: str | None) -> str | None:
    """What a todo stores: the project's identity (@name) when its folder has a .mnemoloci-project
    file, else the normalised path."""
    return (memory.project_identity(project) or str(Path(project)).rstrip("\\/").casefold()) if project else None


def render_todos(todos: list[dict[str, Any]]) -> str:
    title = _t("Cose da fare", "To-dos")
    lines = [f"# {title}", "", _t("> Nota generata da Mnemoloci: modificala con `mnemoloci todo`.",
                                   "> Note generated by Mnemoloci: edit it with `mnemoloci todo`."), ""]
    for status, label in (("open", _t("Aperte", "Open")), ("done", _t("Fatte", "Done")),
                          ("dropped", _t("Lasciate perdere", "Dropped"))):
        group = [t for t in todos if t["status"] == status]
        if not group:
            continue
        lines += [f"## {label}", ""]
        for t in sorted(group, key=lambda t: (t.get("due") or "9999", t["created"])):
            extra = [_t(f"scade {t['due']}", f"due {t['due']}")] if t.get("due") else []
            extra += [_t(f"obiettivo {t['goal']}", f"goal {t['goal']}")] if t.get("goal") else []
            extra += [_t(f"progetto `{t['project']}`", f"project `{t['project']}`")] if t.get("project") else []
            box = "x" if status == "done" else " "
            lines.append(f"- [{box}] **[{t['id']}]** {t['text']}" + (" · " + " · ".join(extra) if extra else ""))
        lines.append("")
    return render_page({"title": title, "schema": TODOS_SCHEMA, "todos": todos}, "\n".join(lines))


def todos_list(vault: Path, *, project: str | None = None, include_closed: bool = False) -> dict[str, Any]:
    """Open todos; with `project`, only that project's plus the global ones (no project)."""
    require_vault(vault)
    todos = _load(vault, TODOS_PATH, TODOS_SCHEMA, "todos")[0]
    wanted = project or None  # covers() normalises, and needs the real path to look for the identity file
    out = [t for t in todos if (include_closed or t["status"] == "open")
           and (wanted is None or t.get("project") is None or memory.covers(t["project"], wanted))]
    out.sort(key=lambda t: (t["status"] != "open", t.get("due") or "9999", t["created"]))
    return {"todos": out}


def _check_due(due: str | None) -> str | None:
    if due and not _DATE.match(due):
        raise ValidationError("BAD_DATE", "due must be YYYY-MM-DD")
    return due or None


def todos_add(vault: Path, text: str, *, project: str | None = None, due: str | None = None,
              goal: str | None = None) -> dict[str, Any]:
    text = check_text(text)
    due = _check_due(due)

    def change(todos: list[dict[str, Any]]) -> dict[str, Any]:
        if sum(1 for t in todos if t["status"] == "open") >= MAX_OPEN_TODOS:
            raise ValidationError("TOO_MANY_TODOS", f"at most {MAX_OPEN_TODOS} open todos; close some first")
        if any(t["text"].casefold() == text.casefold() and t["status"] == "open" for t in todos):
            raise ValidationError("DUPLICATE_TODO", "an open todo with this text already exists")
        number = 1 + max((int(t["id"][1:]) for t in todos), default=0)
        todo = {"id": f"t{number}", "text": text, "status": "open", "project": _norm_project(project), "due": due,
                "goal": goal or None, "created": _today(), "updated": _today()}
        todos.append(todo)
        return todo

    return {"todo": _mutate(vault, TODOS_PATH, TODOS_SCHEMA, "todos", render_todos, "todo-add", change)}


def todos_update(vault: Path, todo_id: str, *, status: str | None = None, text: str | None = None,
                 due: str | None = None) -> dict[str, Any]:
    if status is not None and status not in TODO_STATUSES:
        raise ValidationError("BAD_STATUS", f"status must be one of {TODO_STATUSES}")
    text = check_text(text) if text else None
    due = _check_due(due)

    def change(todos: list[dict[str, Any]]) -> dict[str, Any]:
        todo = next((t for t in todos if t["id"] == todo_id), None)
        if todo is None:
            raise MnemoError("NO_SUCH_TODO", f"no todo {todo_id}")
        if text and text != todo["text"]:
            todo.setdefault("previous_text", []).append(todo["text"])
            todo["text"] = text
        if due:
            todo["due"] = due
        if status and status != todo["status"]:
            todo["status"] = status
            todo["closed"] = _today() if status != "open" else None
        todo["updated"] = _today()
        return todo

    return {"todo": _mutate(vault, TODOS_PATH, TODOS_SCHEMA, "todos", render_todos, "todo-update", change)}


def goals_update(vault: Path, goal_id: str, *, status: str | None = None, text: str | None = None,
                 area: str | None = None, add_links: list[str] | None = None) -> dict[str, Any]:
    if status is not None and status not in GOAL_STATUSES:
        raise ValidationError("BAD_STATUS", f"status must be one of {GOAL_STATUSES}")
    text = check_text(text) if text else None
    area = check_text(area, "area") if area else None

    def change(goals: list[dict[str, Any]]) -> dict[str, Any]:
        goal = next((g for g in goals if g["id"] == goal_id), None)
        if goal is None:
            raise MnemoError("NO_SUCH_GOAL", f"no goal {goal_id}")
        if status == "active" and goal["status"] != "active" and sum(1 for g in goals if g["status"] == "active") >= MAX_ACTIVE_GOALS:
            raise ValidationError("TOO_MANY_GOALS", f"at most {MAX_ACTIVE_GOALS} active goals")
        if text and text != goal["text"]:
            goal.setdefault("previous_text", []).append(goal["text"])
            goal["text"] = text
        if status:
            goal["status"] = status
        if area:
            goal["area"] = area
        for slug in add_links or []:
            if slug not in goal["links"]:
                goal["links"].append(slug)
        goal["updated"] = _today()
        return goal

    return {"goal": _mutate(vault, GOALS_PATH, GOALS_SCHEMA, "goals", render_goals, "goals-update", change)}
