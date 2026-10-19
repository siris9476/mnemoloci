"""Installation health check: one command that says whether everything Mnemoloci relies on is in
place, with the fix for each problem.

`run` only reads. A check whose problem the engine can solve safely by itself carries
a `repair` id; `fix` runs those repairs (all of them, or the ones asked for) and checks
again. Nothing a repair does deletes anything: interrupted writes are settled by
`txn recover` (which parks, never deletes), a stale git lock is moved aside.
Problems that need the user (install Python, the plugin, an extra) keep a `fix`
text with the exact command instead.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__, i18n, runtime
from .core import txn
from .core.errors import MnemoError
from .vault import engine, is_initialized, load_config

def _(it: str, en: str) -> str:
    """The text in the language asked for (the app sends its interface language; otherwise the vault's)."""
    return it if i18n.lang() == "it" else en


REPAIRS = {
    "python": ("registra questo Python per il plugin e l'app", "remember this Python for the plugin and the app"),
    "transactions": ("sistema le scritture interrotte (txn recover: non cancella nulla)", "settle interrupted writes (txn recover: nothing is deleted)"),
    "git-lock": ("sposta da parte il lock di git bloccato", "move the stale git lock aside"),
    "handoffs": ("salva gli handoff in coda", "save the handoffs waiting in the queue"),
    "search-index": ("ricostruisce l'indice di ricerca (dalle note, nient'altro cambia)", "rebuild the search index (from the notes, nothing else changes)"),
    "embeddings": ("calcola gli embedding mancanti (può richiedere minuti)", "compute the missing embeddings (can take minutes)"),
}


PY = "python" if os.name == "nt" else "python3"  # in the commands suggested to the user
ON_REQUEST = {"embeddings"}  # never part of "repair everything"


def _check(name: str, status: str, detail: str, fix: str | None = None, repair: str | None = None) -> dict[str, Any]:
    out = {"check": name, "status": status, "detail": detail}
    if fix:
        out["fix"] = fix
    if repair and status != "ok":
        out["repair"] = repair
        out["repair_label"] = _(*REPAIRS[repair])
    return out


def _maintenance_drift(vault: Path) -> dict[str, Any]:
    """How many `compile submit` blocks have landed since the last `maintain links`/`fix-links`/
    `hub-links` run -- the routine the user has to remember to re-run after every block, and has
    already forgotten once this project (232 broken links, 983 orphans accumulated over many
    blocks before anyone closed the loop). Transaction directories are never deleted, so their
    mtime is a reliable, free history of every write the engine has ever applied -- no separate
    log to keep in sync."""
    root = txn.transactions_dir(vault)
    if not root.is_dir():
        return _check("maintenance", "ok", _("nessuna compilazione ancora", "no compiles yet"))
    dirs = [d for d in root.iterdir() if d.is_dir()]

    def latest(prefix: str) -> float | None:
        times = [d.stat().st_mtime for d in dirs if d.name.startswith(prefix)]
        return max(times) if times else None

    # any one of the three counts: fix-links/links/hub-links only leave a transaction behind when
    # they actually change something, so a healthy block where e.g. fix-links finds nothing to
    # repair would look like "fix-links never ran" forever if all three were required separately
    last_run = max((t for t in (latest("links-"), latest("fixlinks-"), latest("hublinks-")) if t is not None), default=None)
    compiles = [d for d in dirs if d.name.startswith("compile-")]
    if not compiles:
        return _check("maintenance", "ok", _("nessuna compilazione ancora", "no compiles yet"))
    pending = len(compiles) if last_run is None else sum(1 for d in compiles if d.stat().st_mtime > last_run)
    if pending == 0:
        return _check("maintenance", "ok", _("routine eseguita dopo l'ultimo blocco", "routine run after the last block"))
    detail = (_(f"{pending} blocco/i di compile da quando è stata eseguita l'ultima volta (fix-links/links/hub-links)",
                f"{pending} compile block(s) since it last ran (fix-links/links/hub-links)") if last_run else
              _(f"{pending} blocco/i di compile, manutenzione mai eseguita (fix-links/links/hub-links)",
                f"{pending} compile block(s), maintenance never run (fix-links/links/hub-links)"))
    return _check("maintenance", "warn", detail,
                  f"{PY} -P -m mnemoloci maintain fix-links; {PY} -P -m mnemoloci maintain links; {PY} -P -m mnemoloci maintain hub-links")


def _plugin(claude_dir: Path) -> tuple[dict[str, Any] | None, bool | None]:
    try:
        data = json.loads((claude_dir / "plugins" / "installed_plugins.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None, None
    entry = (data.get("plugins") or {}).get("mnemoloci@mnemoloci")
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    enabled = None
    try:
        settings = json.loads((claude_dir / "settings.json").read_text("utf-8"))
        enabled = (settings.get("enabledPlugins") or {}).get("mnemoloci@mnemoloci")
    except (OSError, ValueError):
        pass
    return entry, enabled


def _python_check() -> dict[str, Any]:
    """The interpreter the plugin's launcher and the desktop app will use."""
    recorded = runtime.recorded_python()
    if recorded:
        ok, detail = runtime.can_import(recorded)
        if ok:
            return _check("plugin-python", "ok", f"{recorded} (mnemoloci {detail})")
        return _check("plugin-python", "fail", _(f"è registrato {recorded}, ma non riesce a importare mnemoloci: {detail}", f"{recorded} is recorded but cannot import mnemoloci: {detail}"),
                      f"{PY} -P -m mnemoloci doctor --fix", "python")
    on_path = next((p for p in map(shutil.which, runtime.python_names()) if p), None)
    ok = bool(on_path) and runtime.can_import(on_path)[0]
    return _check("plugin-python", "warn" if ok else "fail",
                  _(f"nessun Python registrato: il plugin ripiega sul Python del PATH ({on_path or 'nessuno'})", f"no Python recorded: the plugin falls back to the Python on PATH ({on_path or 'none'})")
                  + ("" if ok else _(", che non riesce a importare mnemoloci: hook e MCP restano muti", ", which cannot import mnemoloci, so hooks and MCP stay silent")),
                  f"{PY} -P -m mnemoloci doctor --fix", "python")


def run(vault: Path, lang: str | None = None) -> dict[str, Any]:
    """lang: "it" or "en", the language of the texts (the app passes its interface language); by
    default the vault's own."""
    with i18n.forced(lang or i18n.lang(vault)):
        return _run(vault)


def _run(vault: Path) -> dict[str, Any]:
    checks = []
    add = checks.append

    add(_check("python", "ok" if sys.version_info >= (3, 11) else "fail",
               _(f"{sys.version.split()[0]} in {sys.executable}", f"{sys.version.split()[0]} at {sys.executable}"), None if sys.version_info >= (3, 11) else _("installa Python 3.11 o successivo", "install Python 3.11 or later")))
    here = Path(__file__).resolve().parent
    add(_check("engine", "ok", _(f"mnemoloci {__version__} da {here}", f"mnemoloci {__version__} from {here}")))
    ok, detail = runtime.can_import(runtime.console_python())
    add(_check("launch", "ok" if ok else "fail",
               _(f"`{PY} -P -m mnemoloci` trova il motore", f"`{PY} -P -m mnemoloci` finds the engine") if ok else detail,
               None if ok else _(f"{PY} -m pip install -e <repo>  (nel Python usato dal plugin)", f"{PY} -m pip install -e <repo>  (in the Python the plugin uses)")))
    add(_python_check())

    if not is_initialized(vault):
        add(_check("vault", "fail", _(f"nessun vault in {vault}", f"no vault at {vault}"), f"{PY} -P -m mnemoloci init"))
        return _summary(checks)
    try:
        config = load_config(vault)
        add(_check("vault", "ok", _(f"{vault}, lingua {config['language']['target']}", f"{vault}, language {config['language']['target']}")))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        add(_check("vault", "fail", _(f"configurazione illeggibile: {exc}", f"config unreadable: {exc}"), _("ripristina .mnemoloci/config.json dal backup", "restore .mnemoloci/config.json from the backup")))
        return _summary(checks)

    # owner.json outlives the lock by design: the only reliable test is to try to take it for a moment
    from .core.lock import VaultLock

    try:
        with VaultLock(vault, timeout=0.5):
            pass
        add(_check("lock", "ok", _("libero", "free")))
    except MnemoError as exc:
        add(_check("lock", "warn", exc.message, _("un altro comando di Mnemoloci sta scrivendo: aspetta che finisca", "another Mnemoloci command is writing: wait for it to finish")))
    stuck = txn.incomplete_transactions(vault)
    add(_check("transactions", "fail" if stuck else "ok", _(f"{len(stuck)} interrotta/e", f"{len(stuck)} interrupted") if stuck else _("nessuna interrotta", "none interrupted"),
               f"{PY} -P -m mnemoloci txn recover" if stuck else None, "transactions"))
    from .compile import status as compile_status

    active = compile_status(vault).get("active")
    add(_check("compile", "warn" if active else "ok", _(f"compilazione {active} ancora aperta", f"run {active} still open") if active else _("nessuna compilazione aperta", "no open run"),
               _("finiscila (compile merge/submit) oppure `compile abandon`", "finish it (compile merge/submit) or `compile abandon`") if active else None))
    add(_maintenance_drift(vault))

    from .sessions import claude_dirs

    dirs = claude_dirs()
    add(_check("claude-config", "ok" if dirs else "fail", ", ".join(map(str, dirs)) or _("non trovata", "not found"),
               None if dirs else _("imposta CLAUDE_CONFIG_DIR", "set CLAUDE_CONFIG_DIR")))
    if dirs:
        entry, enabled = _plugin(dirs[0])
        if not entry:
            add(_check("plugin", "fail", _("plugin mnemoloci non installato", "mnemoloci plugin not installed"),
                       "claude plugin marketplace add <repo>; claude plugin install mnemoloci@mnemoloci --scope user"))
        else:
            same = entry.get("version") == __version__
            add(_check("plugin", "ok" if same and enabled is not False else "warn",
                       _(f"installato {entry.get('version')} (motore {__version__})", f"installed {entry.get('version')} (engine {__version__})") + ("" if enabled is not False else _(", disattivato", ", disabled")),
                       None if same and enabled is not False else
                       _("claude plugin marketplace update mnemoloci; claude plugin update mnemoloci@mnemoloci; poi riavvia Claude Code", "claude plugin marketplace update mnemoloci; claude plugin update mnemoloci@mnemoloci; then restart Claude Code")))
            hooks_file = Path(entry.get("installPath", "")) / "hooks" / "hooks.json"
            try:
                events = sorted(json.loads(hooks_file.read_text("utf-8")).get("hooks", {}))
                add(_check("hooks", "ok", ", ".join(events)))
            except (OSError, ValueError):
                add(_check("hooks", "fail", _(f"impossibile leggere {hooks_file}", f"cannot read {hooks_file}"), _("reinstalla il plugin", "reinstall the plugin")))

    for module, extra, why in (("mcp", "mcp", _("Server MCP", "MCP server")), ("uvicorn", "app", _("App desktop e accesso dal telefono", "Desktop app and phone access")),
                               ("pypdf", "ingest", _("Salvataggio dei PDF", "PDF capture")), ("youtube_transcript_api", "ingest", _("Salvataggio dei video YouTube", "YouTube capture")),
                               ("pytesseract", "ocr", _("OCR delle immagini nell'inbox", "OCR for inbox images")),
                               ("faster_whisper", "transcribe", _("Trascrizione dei video locali", "Local video transcription")),
                               ("tree_sitter_language_pack", "codechunk", _("Chunking AST del codice in compile", "AST-aware code chunking in compile"))):
        found = importlib.util.find_spec(module) is not None
        add(_check(f"extra:{module}", "ok" if found else "warn", f"{why}: {_('disponibile', 'available') if found else _('non installato', 'not installed')}",
                   None if found else f'{PY} -m pip install "mnemoloci[{extra}]"'))
    embeddings_cfg = load_config(vault)["embeddings"]
    has_numpy = importlib.util.find_spec("numpy") is not None
    if embeddings_cfg["provider"] == "ollama":
        add(_check("extra:embeddings", "ok" if has_numpy else "warn",
                   _(f"Ricerca semantica via Ollama ({embeddings_cfg['ollama_model']})", f"Semantic search via Ollama ({embeddings_cfg['ollama_model']})"),
                   None if has_numpy else f'{PY} -m pip install "mnemoloci[semantic]"'))
    else:
        found = has_numpy and importlib.util.find_spec("fastembed") is not None
        add(_check("extra:fastembed", "ok" if found else "warn",
                   f"{_('Ricerca semantica', 'Semantic search')}: {_('disponibile', 'available') if found else _('non installato', 'not installed')}",
                   None if found else f'{PY} -m pip install "mnemoloci[semantic]"'))
    add(_check("git", "ok" if shutil.which("git") else "warn", _("trovato", "found") if shutil.which("git") else _("non è nel PATH", "not on PATH"),
               None if shutil.which("git") else _("installa Git for Windows (serve per il backup e la provenienza del codice)", "install Git for Windows (needed by backup and code provenance)")))

    from . import backup, embed, index

    try:
        status = backup.status(vault)
        add(_check("backup", "ok" if status["enabled"] else "warn",
                   (_("ultimo", "last") + f" {status['last_commit']['at'][:16] if status.get('last_commit') else _('mai', 'never')}, "
                    + _(f"{status.get('uncommitted_changes', 0)} modifiche non salvate",
                        f"{status.get('uncommitted_changes', 0)} changes not saved"))
                   if status["enabled"] else _("non attivo", "not enabled"),
                   None if status["enabled"] else f"{PY} -P -m mnemoloci backup init"))
    except Exception as exc:  # noqa: BLE001
        add(_check("backup", "warn", str(exc)[:200]))
    stale_lock = backup.stale_git_lock(vault)
    if stale_lock:
        add(_check("git-lock", "warn", _(f"{stale_lock} ha più di {backup.STALE_GIT_LOCK // 60} minuti", f"{stale_lock} is older than {backup.STALE_GIT_LOCK // 60} minutes"),
                   f"{PY} -P -m mnemoloci doctor --fix", "git-lock"))
    from .hooks import PENDING_HANDOFFS

    queued = list((engine(vault) / "work" / PENDING_HANDOFFS).glob("*.json"))
    add(_check("handoffs", "warn" if queued else "ok",
               _(f"{len(queued)} handoff in attesa (il vault era occupato quando una sessione è finita)", f"{len(queued)} handoff(s) waiting (the vault was busy when a session ended)") if queued else _("nessuno in attesa", "none waiting"),
               _("li salva il prossimo avvio di sessione, oppure `doctor --fix`", "saved by the next session start, or `doctor --fix`") if queued else None, "handoffs"))
    ready = index.ready(vault)
    add(_check("search-index", "ok" if ready else "warn",
               _("pronto", "ready") if ready else _("mancante, incompleto o di una versione vecchia (si ricostruisce alla prossima ricerca)", "missing, incomplete or old layout (rebuilt on the next search)"),
               None if ready else f"{PY} -P -m mnemoloci index rebuild", "search-index"))
    if embed.available(vault):
        stale = embed.stale_count(vault) if embed.has_vectors(vault) else None
        add(_check("embeddings", "ok" if stale == 0 else "warn",
                   _("aggiornati", "up to date") if stale == 0 else _("mai calcolati", "never computed") if stale is None else _(f"{stale} pagine da ricalcolare", f"{stale} pages to re-embed"),
                   None if stale == 0 else f"{PY} -P -m mnemoloci index embed", "embeddings"))
    if True:  # Windows Task Scheduler, or cron / systemd timer on Linux
        from . import schedule

        try:
            sched = schedule.status(vault)
            add(_check("schedule", "ok" if sched["enabled"] else "info",
                       _("lavoro giornaliero attivo", "daily job enabled") if sched["enabled"] else _("lavoro giornaliero non attivo (facoltativo)", "daily job not enabled (optional)")))
        except Exception:  # noqa: BLE001
            pass
    return _summary(checks)


def _summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail", "info")}
    return {"healthy": counts["fail"] == 0, "counts": counts, "checks": checks,
            "repairable": [c["repair"] for c in checks if c.get("repair")]}


def _repair(vault: Path, name: str) -> dict[str, Any]:
    from . import backup, embed, index
    from .core.lock import VaultLock

    def handoffs() -> dict[str, Any]:
        from .hooks import replay_handoffs

        with VaultLock(vault) as lock:
            return {"saved": replay_handoffs(vault, lock)}

    def git_lock() -> dict[str, Any]:
        with VaultLock(vault):
            return {"moved": backup._clear_stale_git_lock(vault)}

    actions: dict[str, Callable[[], Any]] = {
        "python": lambda: runtime.record_python(),
        "transactions": lambda: {"recovered": txn.recover(vault)},
        "git-lock": git_lock,
        "handoffs": handoffs,
        "search-index": lambda: index.rebuild(vault),
        "embeddings": lambda: embed.build(vault),
    }
    if name not in actions:
        raise MnemoError("NO_SUCH_REPAIR", _(f"riparazione sconosciuta {name!r}; disponibili: {sorted(actions)}", f"unknown repair {name!r}; available: {sorted(actions)}"))
    return actions[name]()


def fix(vault: Path, only: list[str] | None = None, lang: str | None = None) -> dict[str, Any]:
    with i18n.forced(lang or i18n.lang(vault)):
        return _fix(vault, only)


def _fix(vault: Path, only: list[str] | None = None) -> dict[str, Any]:
    """Run the repairs the checks ask for (or just `only`), then check again. Each repair is
    independent: one failing does not stop the others."""
    before = _run(vault)
    # without `only`, slow repairs (embeddings: minutes, maybe a model download) are left out
    wanted = [r for r in before["repairable"] if (r in only if only else r not in ON_REQUEST)]
    unknown = sorted(set(only or []) - set(REPAIRS))
    if unknown:
        raise MnemoError("NO_SUCH_REPAIR", _(f"riparazioni sconosciute {unknown}; disponibili: {sorted(REPAIRS)}", f"unknown repair(s) {unknown}; available: {sorted(REPAIRS)}"))
    done = []
    for name in wanted:
        try:
            done.append({"repair": name, "ok": True, "result": _repair(vault, name)})
        except (MnemoError, OSError) as exc:
            done.append({"repair": name, "ok": False, "error": getattr(exc, "message", str(exc))})
    after = _run(vault)
    return {"repairs": done, "nothing_to_repair": not wanted, "healthy": after["healthy"], "counts": after["counts"],
            "still_to_do": [{k: c[k] for k in ("check", "detail", "fix") if k in c}
                            for c in after["checks"] if c["status"] in ("fail", "warn") and c.get("fix")]}
