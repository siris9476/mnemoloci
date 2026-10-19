"""The things you run on demand rather than configure: questions to the vault, checks, exports, imports.

Each tool is described by a small spec (fields, types, limits) that the web app turns into a form, and
run here with the same validation whichever door it comes through. `spends` marks a tool that costs
money: it runs only when the caller says it understood (`confirm_cost`)."""

from pathlib import Path
from typing import Any, Callable

from . import (briefing, chat_import, citecheck, codex_import, context_pack, filing, folds, graph_analysis, hooks,
               obsidian, okf, quality, reason, reflect, secrets, voice)
from .core.errors import ValidationError
from .vault import load_config, require_vault

MAX_TEXT, MAX_LONG = 2_000, 20_000


def F(name: str, label: str, type: str = "text", *, required: bool = False, default: Any = None, hint: str = "",
      low: float | None = None, high: float | None = None, choices: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "label": label, "type": type, "required": required, "default": default, "hint": hint,
            "min": low, "max": high, "choices": choices}


# ---- each tool: how to run it from validated parameters ----
def _export(name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    from . import export  # export imports the web api: loaded when a tool runs, not at import time

    return getattr(export, name)(*args, **kwargs)


def _list(p: dict[str, Any], key: str) -> list[str] | None:
    """A comma-separated text field as a list (None when empty)."""
    parts = [x.strip() for x in str(p.get(key) or "").split(",") if x.strip()]
    return parts or None


def _needs_backup(vault: Path) -> None:
    """The history of a page comes from the git backup: say so instead of showing a git error."""
    from . import backup

    if not backup.status(vault).get("enabled"):
        raise ValidationError("NO_BACKUP", "the history of a page comes from the backup: start it first (Settings, or `mnemoloci backup init`)")


def _opt(p: dict[str, Any], key: str) -> Any:
    return p.get(key) if p.get(key) not in ("", None) else None


_RUN: dict[str, Callable[[Path, dict[str, Any]], dict[str, Any]]] = {
    "reason": lambda v, p: reason.ask(v, p["question"], budget=p.get("budget"), model=_opt(p, "model"),
                                       roots=[p["root"]] if _opt(p, "root") else None),
    "reason-history": lambda v, p: reason.history(v, p["limit"]),
    "check-citations": lambda v, p: citecheck.check(v, source=_opt(p, "source"), limit=p["limit"]),
    "context-cost": lambda v, p: hooks.context_cost(v, _opt(p, "cwd")),
    "context": lambda v, p: context_pack.build(v, p["question"], budget=p["budget"], summary_only=p["summary_only"],
                                               include_memory=not p["no_memory"]),
    "quality-eval": lambda v, p: quality.retrieval_eval(v, k=p["k"], record=p["record"]),
    "quality-history": lambda v, p: {"history": quality.history(v, p["limit"])},
    "privacy-audit": lambda v, p: secrets.audit_vault(v),
    "reflect-trace": lambda v, p: _needs_backup(v) or reflect.trace(v, p["path"], limit=p["limit"]),
    "graph-backfill": lambda v, p: graph_analysis.backfill(v, p["path"], at=_opt(p, "at"), window_days=p["window_days"],
                                                           hops=p["hops"], limit=p["limit"]),
    "export-graph": lambda v, p: _export("export_graph", v, p["format"], p["out"]),
    "export-marp": lambda v, p: _export("export_marp", v, p["note"], p["out"], theme=p["theme"]),
    "export-jsonld": lambda v, p: _export("export_jsonld", v, p["out"]),
    "export-llms": lambda v, p: _export("export_llms", v, p["out"], title=p["title"]),
    "export-vault": lambda v, p: _export("export_vault", v, p["out"], _opt(p, "project_id")),
    "export-okf": lambda v, p: okf.export_okf(v, p["out"], human_id=_opt(p, "human_id"), include_memory=p["include_memory"],
                                              include_sources=p["include_sources"]),
    "import-okf": lambda v, p: okf.import_okf(v, p["bundle"], dry_run=p["dry_run"]),
    "import-codex": lambda v, p: codex_import.import_codex(v, home=_opt(p, "home"), since=_opt(p, "since"), dry_run=p["dry_run"]),
    "import-chats": lambda v, p: chat_import.import_export(v, p["export"], include=_list(p, "include"), exclude=_list(p, "exclude"),
                                                           since=_opt(p, "since"), dry_run=p["dry_run"]),
    "obsidian-canvas": lambda v, p: obsidian.canvas(v, p["slug"], depth=p["depth"]),
    "obsidian-bases": lambda v, p: obsidian.bases(v),
    "obsidian-graph-colors": lambda v, p: obsidian.graph_colors(v),
    "log-fold": lambda v, p: folds.fold(v, month=_opt(p, "month")),
    "filing": lambda v, p: filing.generate(v, p["mode"]),
    "voice": lambda v, p: voice.samples(v, limit=p["limit"]),
    "brief-check": lambda v, p: briefing.check(v, p["text"]),
}

_DRY = F("dry_run", 'Preview only, write nothing', "bool", default=True)
_OUT = lambda hint="": F("out", 'Where to write (full path)', "abspath", required=True, hint=hint)  # noqa: E731

# id, group, title, help, fields, spends, writes
SPECS: list[dict[str, Any]] = [
    {"id": "reason", "group": 'research', "title": 'Question to the vault',
     "help": 'Claude answers by reading the vault, read-only, and cites the pages. It spends tokens: it only starts if you confirm.',
     "fields": [F("question", 'Question', "textarea", required=True),
                F("budget", 'Spending cap in dollars (empty = the one saved in Settings)', "float", low=0.01, high=20),
                F("model", "Model (empty = Claude's default)"),
                F("root", 'Another folder it may read (full path, optional)', "abspath")],
     "spends": True, "writes": False},
    {"id": "reason-history", "group": 'research', "title": 'Question history',
     "help": 'Past questions with cost, turns and time.', "fields": [F("limit", 'How many to show', "int", default=20, low=1, high=200)],
     "spends": False, "writes": False},
    {"id": "context", "group": 'research', "title": 'Context pack',
     "help": 'The right pages for a question within a token budget; private pages stay out.',
     "fields": [F("question", 'Question', "textarea", required=True), F("budget", 'Token budget', "int", default=4000, low=500, high=50000),
                F("no_memory", 'Do not include memory notes', "bool", default=False),
                F("summary_only", 'Page summaries only', "bool", default=False)], "spends": False, "writes": False},
    {"id": "check-citations", "group": 'quality', "title": 'Citation check',
     "help": 'Citations whose cited lines share too little with the sentence. It is a warning: about one in ten real ones shows up even when correct.',
     "fields": [F("source", 'Only one source (name, optional)'), F("limit", 'How many to show', "int", default=50, low=1, high=500)],
     "spends": False, "writes": False},
    {"id": "context-cost", "group": 'quality', "title": 'Context cost',
     "help": 'How much what is injected at the start of a session in a project folder weighs, section by section.',
     "fields": [F("cwd", 'Project folder (optional)', "text")], "spends": False, "writes": False},
    {"id": "quality-eval", "group": 'quality', "title": 'Search quality',
     "help": 'Measures how well search finds the right pages for known questions (recall and MRR).',
     "fields": [F("k", 'How many results to consider', "int", default=5, low=1, high=20),
                F("record", 'Record the result in the history', "bool", default=False)], "spends": False, "writes": False},
    {"id": "quality-history", "group": 'quality', "title": 'Quality history',
     "help": 'The quality scores recorded over time.', "fields": [F("limit", 'How many to show', "int", default=30, low=1, high=365)],
     "spends": False, "writes": False},
    {"id": "privacy-audit", "group": 'quality', "title": 'Privacy check',
     "help": 'Looks for credentials and personal data that ended up by mistake in pages, memory and service files.',
     "fields": [], "spends": False, "writes": False},
    {"id": "reflect-trace", "group": 'quality', "title": 'History of a page',
     "help": 'The whole recorded history of a wiki page.',
     "fields": [F("path", 'Path of the page in the vault (e.g. wiki/concepts/name.md)', required=True),
                F("limit", 'How many entries', "int", default=20, low=1, high=200)], "spends": False, "writes": False},
    {"id": "graph-backfill", "group": 'quality', "title": 'Pages close by links and time',
     "help": 'The pages close to a page in the graph and in the same period.',
     "fields": [F("path", 'Path of the page in the vault', required=True), F("at", 'Reference date (YYYY-MM-DD, optional)'),
                F("window_days", 'Window in days', "int", default=14, low=1, high=365), F("hops", 'Hops in the graph', "int", default=2, low=1, high=5),
                F("limit", 'How many pages', "int", default=20, low=1, high=100)], "spends": False, "writes": False},
    {"id": "export-graph", "group": 'export', "title": 'Export the graph',
     "help": 'The link graph in a format for other tools.',
     "fields": [F("format", 'Format', "select", required=True, choices=["graphml", "json", "html", "cypher", "sql"], default="html"), _OUT()],
     "spends": False, "writes": True},
    {"id": "export-okf", "group": 'export', "title": 'Export as OKF',
     "help": 'An OKF package with the wiki pages.',
     "fields": [_OUT(), F("human_id", 'Person identifier (optional)'), F("include_memory", 'Include memory', "bool", default=False),
                F("include_sources", 'Include sources', "bool", default=False)], "spends": False, "writes": True},
    {"id": "export-marp", "group": 'export', "title": 'Export a page as a presentation',
     "help": 'A wiki page as a Marp presentation.',
     "fields": [F("note", 'Page (vault path or name)', required=True), _OUT(), F("theme", 'Theme', default="default")],
     "spends": False, "writes": True},
    {"id": "export-jsonld", "group": 'export', "title": 'Export as JSON-LD',
     "help": 'The wiki as a graph of linked data.', "fields": [_OUT()], "spends": False, "writes": True},
    {"id": "export-llms", "group": 'export', "title": 'Export for models (llms.txt)',
     "help": 'An index of the wiki made to be read by a model.', "fields": [_OUT(), F("title", 'Title', default="Second brain")],
     "spends": False, "writes": True},
    {"id": "export-vault", "group": 'export', "title": 'Export the whole vault',
     "help": 'A copy of the vault, or of just one project.', "fields": [_OUT(), F("project_id", 'Only one project (optional)')],
     "spends": False, "writes": True},
    {"id": "import-okf", "group": 'import', "title": 'Import from OKF',
     "help": 'Brings an OKF package into the vault. The preview shows what would come in.',
     "fields": [F("bundle", 'Package folder (full path)', "abspath", required=True), _DRY], "spends": False, "writes": True},
    {"id": "import-codex", "group": 'import', "title": 'Import Codex sessions',
     "help": 'Codex conversations become sources to compile.',
     "fields": [F("home", 'Codex folder (empty = the default one)', "abspath"), F("since", 'Only from this date (YYYY-MM-DD)'), _DRY],
     "spends": False, "writes": True},
    {"id": "import-chats", "group": 'import', "title": 'Import exported chats',
     "help": 'Chats exported from other assistants become sources to compile.',
     "fields": [F("export", 'Export file or folder (full path)', "abspath", required=True),
                F("include", 'Only chats with these titles (comma-separated)'), F("exclude", 'Skip chats with these titles (comma-separated)'),
                F("since", 'Only from this date (YYYY-MM-DD)'), _DRY], "spends": False, "writes": True},
    {"id": "obsidian-canvas", "group": "obsidian", "title": 'Canvas map of a page',
     "help": 'A map for Obsidian with the page and the ones linked to it.',
     "fields": [F("slug", 'Page at the centre (name)', required=True), F("depth", 'Depth', "int", default=1, low=1, high=2)],
     "spends": False, "writes": True},
    {"id": "obsidian-bases", "group": "obsidian", "title": 'Bases dashboards',
     "help": 'Creates the dashboards for Obsidian.', "fields": [], "spends": False, "writes": True},
    {"id": "obsidian-graph-colors", "group": "obsidian", "title": 'Graph colours',
     "help": 'Sets the Obsidian graph colours by page type.', "fields": [], "spends": False, "writes": True},
    {"id": "log-fold", "group": "obsidian", "title": 'Monthly log summary',
     "help": 'Summarises one month of the operations log, without touching the log.',
     "fields": [F("month", 'Month (YYYY-MM, empty = the last complete one)')], "spends": False, "writes": True},
    {"id": "filing", "group": "obsidian", "title": 'Navigation notes',
     "help": 'Navigation notes following a filing method; pages are not moved.',
     "fields": [F("mode", 'Method', "select", required=True, choices=["para", "lyt", "zettelkasten"], default="para")],
     "spends": False, "writes": True},
    {"id": "voice", "group": "obsidian", "title": 'Samples of your voice',
     "help": 'Samples of your writing, to write in your style.', "fields": [F("limit", 'How many samples', "int", default=8, low=1, high=50)],
     "spends": False, "writes": False},
    {"id": "brief-check", "group": "obsidian", "title": 'Check a briefing',
     "help": 'Checks a briefing or explanation text before saving it as a page.',
     "fields": [F("text", 'Text', "textarea", required=True)], "spends": False, "writes": False},
]
GROUPS = (('research', 'Questions and search'), ('quality', 'Checks'), ('export', 'Exports'), ('import', 'Imports'),
          ("obsidian", 'Obsidian and writing'))
_BY_ID = {s["id"]: s for s in SPECS}


def catalogue() -> dict[str, Any]:
    return {"groups": [{"id": i, "title": t} for i, t in GROUPS], "tools": SPECS}


def _clean(spec: dict[str, Any], params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValidationError("BAD_PARAMS", "servono i valori dello strumento")
    out: dict[str, Any] = {}
    for f in spec["fields"]:
        name, kind, raw = f["name"], f["type"], params.get(f["name"])
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        if blank:
            if f["required"]:
                raise ValidationError("MISSING_PARAM", f"manca: {f['label']}")
            out[name] = f["default"] if f["default"] is not None else (False if kind == "bool" else None)
            continue
        if kind in ("text", "textarea", "abspath", "select"):
            if not isinstance(raw, str):
                raise ValidationError("BAD_PARAM", f"{f['label']} must be text")
            value = raw.strip()
            if len(value) > (MAX_LONG if kind == "textarea" else MAX_TEXT):
                raise ValidationError("TEXT_TOO_LONG", f"{f['label']} is too long")
            if kind == "abspath" and not Path(value).is_absolute():
                raise ValidationError("NOT_ABSOLUTE", f"{f['label']}: the full path is needed, for example C:\\Users\\you\\Folder")
            if kind == "select" and value not in (f["choices"] or []):
                raise ValidationError("BAD_PARAM", f"{f['label']} must be one of {f['choices']}")
            out[name] = value
        elif kind == "bool":
            if not isinstance(raw, bool):
                raise ValidationError("BAD_PARAM", f"{f['label']} must be true or false")
            out[name] = raw
        else:
            try:
                number = float(raw) if kind == "float" else int(raw)
            except (TypeError, ValueError):
                raise ValidationError("BAD_PARAM", f"{f['label']} must be a number") from None
            if (f["min"] is not None and number < f["min"]) or (f["max"] is not None and number > f["max"]):
                raise ValidationError("BAD_PARAM", f"{f['label']} must be between {f['min']:g} and {f['max']:g}")
            out[name] = number
    return out


def run(vault: Path, tool: str, params: Any, *, confirm_cost: bool = False) -> dict[str, Any]:
    require_vault(vault)
    spec = _BY_ID.get(tool)
    if spec is None:
        raise ValidationError("NO_SUCH_TOOL", f"unknown tool: {tool}")
    clean = _clean(spec, params)
    if spec["spends"] and confirm_cost is not True:
        cap = clean.get("budget") or float((load_config(vault).get("reason") or {}).get("maxBudgetUsd", reason.DEFAULT_BUDGET_USD))
        raise ValidationError("CONFIRM_COST", f"this action spends tokens, up to {cap:g} dollars: confirm it to run it",
                              {"cap_usd": cap})
    return {"tool": tool, "result": _RUN[tool](Path(vault), clean)}
