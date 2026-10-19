"""The catalogue of everything Mnemoloci does, and where in the web app each thing can be done.

It feeds the app's "Funzionalità" page and a test that keeps it honest: every command-line command
must belong to a feature (or be listed as internal), and a feature that claims a web-app view must
point at one that exists. So when a command is added and nobody puts it here, the tests fail.

Levels: "full" (everything the command does is available in the app), "partial" (the main part is),
"none" (command line only), "internal" (not something a person manages: a server, a hook, a journal)."""

from typing import Any

LEVELS = ("full", "partial", "none", "internal")
CATEGORIES = (
    ('knowledge', 'Knowledge and wiki'),
    ('research', 'Search and questions'),
    ('quality', 'Quality and maintenance'),
    ('memory', 'Personal memory'),
    ("claude", 'Sessions, skills and projects'),
    ('system', 'System'),
)

# id, category, title, description, commands, app view, level
_ROWS: list[tuple[str, str, str, str, tuple[str, ...], str | None, str]] = [
    ('sources', 'knowledge', 'Knowledge sources',
     'Register project folders and Obsidian vaults as sources, decide what to read (documents only or code too) and which folders to exclude. In the app you register, limit and check them; retiring a source and the per-source report are terminal only.',
     ("source", "scan"), "sources", "partial"),
    ('compile', 'knowledge', 'Wiki compilation',
     'Turns the changed files of your sources into pages with line-level citations, in two phases; every write has an approved plan and a rollback. Extraction without Claude is in Settings.',
     ("compile",), "sources", "partial"),
    ('review', 'knowledge', 'Review queue',
     'Uncertain pages, pages in contradiction or without valid citations wait for your approval before they are published.',
     ("review",), "review", "full"),
    ('capture', 'knowledge', 'Capture web pages, PDFs, videos and files',
     'Save web pages, PDFs, YouTube videos, files and text as sources to compile.',
     ("ingest",), "clips", "full"),
    ('images', 'knowledge', 'Image inbox',
     'Screenshots and images from your phone, Telegram or files, with transcription. In the app you see and receive them; transcription and OCR are done by Claude or from the terminal.',
     ("inbox",), "inbox", "partial"),
    ('imports', 'knowledge', 'Imports from other tools',
     'Bring in knowledge exported from other systems (OKF), Codex sessions and chats.',
     ("import",), "tools", "full"),
    ('exports', 'knowledge', 'Exports',
     'Export the graph or the whole vault in open formats (OKF, JSON-LD, Marp, llms.txt).',
     ("export",), "tools", "full"),
    ("obsidian", 'knowledge', 'Views for Obsidian',
     'Generates maps (canvas), dashboards (Bases) and graph colours for Obsidian.',
     ("obsidian",), "tools", "full"),
    ('filing', 'knowledge', 'Navigation notes for filing methods',
     'Creates navigation notes following a filing method, without moving pages.',
     ("filing",), "tools", "full"),
    ('voice', 'knowledge', 'Write in your own voice',
     'Collects samples of your writing so it can be written in your style.',
     ("voice",), "tools", "full"),
    ("briefing", 'knowledge', 'Briefings and explanations',
     'Checks and saves briefings, plain explanations and lessons as wiki pages. In the app you check a text; saving it as a page is done by Claude or from the terminal.',
     ("brief",), "tools", "partial"),
    ('web-research', 'knowledge', 'Guided web research',
     'A web search with clear limits and a reviewed merge before anything enters the wiki.',
     ("research",), None, "none"),

    ('research', 'research', 'Search the vault',
     'Full-text search with link expansion and, when on, semantic search.',
     ("query", "index"), "search", "full"),
    ('context', 'research', 'Context pack',
     'Gathers the right pages for a question within a token budget (private pages stay out).',
     ("context",), "tools", "full"),
    ('questions', 'research', 'Questions to the vault (reason)',
     'Has Claude answer from the vault, read-only, with a spending cap and a log of every question. The cap is set in Settings.',
     ("reason", "reason-history"), "tools", "full"),
    ('answers', 'research', 'Saved answers',
     'Good answers become wiki pages; ones that have gone stale are flagged. In the app you see them and check the stale ones; new ones are saved by Claude or from the terminal.',
     ("answer",), "answers", "partial"),
    ('people', 'research', 'People and entities',
     'People and other entities ranked by how much they matter now.',
     ("people",), "people", "full"),
    ('graph', 'research', 'Link graph',
     'The page graph, its analysis (bridges, islands, central nodes) and finding pages close by links and time (in Tools).',
     ("graph",), "graph", "full"),

    ('maintenance', 'quality', 'Wiki maintenance',
     'Broken links, missing connections, tags, duplicate pages to merge, citation checks. The app has link repair and tags.',
     ("maintain", "tags"), "maintain", "partial"),
    ('checks', 'quality', 'Vault check and quality',
     'Health check, quality score with its trend and a measure of search quality. In the app: status and score on the Status page, measure and history in Tools.',
     ("lint", "quality", "quality-history", "quality-eval"), "status", "full"),
    ('diagnosis', 'quality', 'Installation diagnosis',
     'Checks that the engine, plugin, hooks, git and search work, with one-click repairs.',
     ("doctor",), "status", "full"),
    ("privacy", 'quality', 'Privacy check',
     'Looks for credentials and personal data that ended up in the vault by mistake.',
     ("privacy",), "tools", "full"),
    ('history', 'quality', 'History of a page',
     'Rebuilds the whole recorded history of a wiki page (belief tracing).',
     ("reflect",), "tools", "full"),
    ('context-cost', 'quality', 'Context cost',
     'How much what is injected at the start of a session weighs in a project, section by section.',
     ("context-cost",), "tools", "full"),
    ('log-summaries', 'quality', 'Monthly log summaries',
     'Summarises the operations log month by month, without touching the log.',
     ("log-fold",), "tools", "full"),

    ('profile', 'memory', 'Profile',
     'Stable facts about you (role, tools, style) that Claude reads at every session.',
     ("profile",), "profile", "full"),
    ('goals', 'memory', 'Goals',
     'Active, paused or closed goals, linked to wiki pages.',
     ("goals",), "goals", "full"),
    ("todo", 'memory', 'To-dos',
     'Todos that carry over from one session to the next, with a due date and a project.',
     ("todo",), "today", "full"),
    ('memory', 'memory', 'Working memory',
     'Decisions, facts, lessons, preferences, procedures and handoffs; they can be retracted or suppressed. In the app you review, retract and suppress them; new notes and replacing an old one are done by Claude or from the terminal.',
     ("memory",), 'memory', "partial"),
    ('instincts', 'memory', 'Instincts',
     'Rules learned from your corrections, to be approved before use. The app lets you approve and reject them.',
     ("instincts",), "review", "partial"),
    ('day', 'memory', 'Day and week',
     "What happened today, on a given day or this week, and the weekly review with next week's plan.",
     ("digest", "weekly"), "today", "full"),

    ('sessions', "claude", 'Claude Code sessions',
     'Search and reread past sessions; file history, file risk, commits and topics.',
     ("sessions",), "sessions", "partial"),
    ("skill", "claude", 'Skills',
     'Find, create, audit and install Agent Skills; measure which ones are used. The app lets you see and archive them.',
     ("skills",), "skills", "partial"),
    ('projects', "claude", 'New projects for Remote Control',
     'Project folders created from your phone for Remote Control sessions.',
     ("projects",), "sources", "partial"),
    ('phone', "claude", 'Use from your phone',
     'Use the app from your phone through Tailscale, with paired devices and permissions.',
     ("remote",), "sources", "partial"),
    ('connectors', "claude", 'Connectors',
     'Periodic sync from Google Drive, OneDrive, Gmail, GitHub and Notion.',
     ("connectors",), "sources", "full"),

    ('settings', 'system', 'Settings',
     'Language, guidelines, review rules, semantic search, local extraction and the other adjustments.',
     ("config", "vault"), "settings", "full"),
    ("backup", 'system', "Backup",
     'Git versions of the vault, with an optional remote copy. In the app: settings and an immediate backup; starting it is done from the terminal.',
     ("backup",), "settings", "partial"),
    ('nightly', 'system', 'Nightly job',
     'Every day: session index, digest, quality, backup and verification, never calling a model.',
     ("schedule",), "settings", "full"),
    ('create', 'system', 'Create a vault',
     'Creates a new vault, with the folders and the basic configuration.',
     ("init",), None, "none"),
    ("mcp", 'system', 'MCP server',
     'Lets Claude read the vault, read-only. The plugin starts it: there is nothing to manage.',
     ("mcp",), None, "internal"),
]

# Commands a person never runs by hand: each with the reason it is not a feature.
INTERNAL = {
    "hook": "entry points of the Claude Code hooks, called by Claude",
    "txn": "low-level journalled writes, used by the other commands",
    "scheduled-run": "the nightly job itself, called by the task scheduler",
    "app": "starts this app",
}


def catalogue() -> dict[str, Any]:
    features = [{"id": i, "category": c, "title": t, "description": d, "commands": list(cmds), "view": v, "level": lvl}
                for i, c, t, d, cmds, v, lvl in _ROWS]
    counted = [f for f in features if f["level"] != "internal"]
    return {
        "categories": [{"id": i, "title": t} for i, t in CATEGORIES],
        "features": features,
        "internal": INTERNAL,
        "summary": {"total": len(counted), **{lvl: sum(1 for f in counted if f["level"] == lvl) for lvl in ("full", "partial", "none")}},
    }
