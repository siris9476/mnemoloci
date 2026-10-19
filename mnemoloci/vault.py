import json
import re
import time
from pathlib import Path
from typing import Any

from .core import txn
from .core.atomic import atomic_json
from .core.errors import MnemoError
from .core.lock import VaultLock

CONFIG_SCHEMA = "mnemoloci.config.v1"
KIND_DIRS = {
    "concept": "wiki/concepts",
    "entity": "wiki/entities",
    "comparison": "wiki/comparisons",
    "overview": "wiki/overviews",
    "query": "wiki/queries",  # saved answers (`answer save`), never produced by compile
}
HOLD_MODES = {
    "low-confidence", "contradicted", "schema-violating", "provenance-violating", "external-source",
    "single-source",  # a `critical: true` page needs 2+ independent sources
    "local-extraction",  # at least one contribution came from a local model (Ollama), not Claude
}
# One "engine" per phase (extract, write): provider -- ollama (local, free), anthropic (Claude's
# own paid API, needs a key) or claude_code (the `claude` CLI in headless mode, billed to the same
# subscription as an interactive session, no separate key) -- plus that provider's model/base_url
# fields (the others stay None).
DEFAULT_PHASE_ENGINE = {"provider": "ollama", "ollama_model": None, "ollama_base_url": None,
                        "anthropic_model": None, "claude_code_model": None}
DEFAULT_CONFIG = {
    "schema": CONFIG_SCHEMA,
    "review": {
        "hold": sorted(HOLD_MODES),
        "low_confidence_threshold": 0.5,
        "treat_missing_confidence_as": "hold",
    },
    "ingest": {"max_file_bytes": 512 * 1024},
    "language": {"target": "en", "keep_original": True},
    "embeddings": {"provider": "fastembed", "ollama_model": None, "ollama_base_url": None,
                  "openai_model": None, "voyage_model": None},
    # local_extraction: the two phases (extract, write) can use different engines -- e.g. ollama to
    # pull out raw concepts, claude_code to write better prose; by default `write` mirrors
    # `extract`. Neither phase is ever wired into the daily scheduled job: only ever triggered by
    # an explicit CLI/app action.
    "local_extraction": {"mode": "extract_only", "extract": dict(DEFAULT_PHASE_ENGINE), "write": dict(DEFAULT_PHASE_ENGINE)},
}
DEFAULT_LANGUAGE = DEFAULT_CONFIG["language"]
LEGACY_LANGUAGE = {"target": "it", "keep_original": True}  # a config saved before the language setting existed
DEFAULT_EMBEDDINGS = DEFAULT_CONFIG["embeddings"]
DEFAULT_LOCAL_EXTRACTION = DEFAULT_CONFIG["local_extraction"]
LOCAL_EXTRACTION_MODES = {"extract_only", "extract_and_write"}
LOCAL_EXTRACTION_PROVIDERS = {"ollama", "anthropic", "claude_code"}
EMBEDDING_PROVIDERS = {"fastembed", "ollama", "openai", "voyage"}
CLOUD_EMBEDDING_PROVIDERS = {"openai", "voyage"}  # need an API key, unlike fastembed/ollama
CLOUD_EXTRACTION_PROVIDERS = {"anthropic"}  # same: needs an API key, unlike ollama
OLLAMA_DEFAULT_URL = "http://localhost:11434"
_LANG = re.compile(r"^[a-z]{2,3}$")


def valid_language(code: Any) -> bool:
    return isinstance(code, str) and bool(_LANG.match(code))

CONVENTIONS_IT = """\
# Mnemoloci vault

Questo vault è la fonte di verità di Mnemoloci. È Markdown puro compatibile con Obsidian.

## Struttura
- `wiki/concepts|entities|comparisons|overviews/` — pagine compilate, una per concetto
- `wiki/queries/` — risposte salvate (`mnemoloci answer save`), legate alle pagine su cui si basano
- `wiki/index.md` — indice generato, `wiki/log.md` — registro delle attività (solo append)
- `raw/` — fonti immutabili copiate nel vault (screenshot, sessioni, pagine web/PDF/video in `clips/`, note tue in `notes/`, import)
- `memory/` — memoria di lavoro: decisioni, fatti, lezioni, handoff
- `_meta/` — profilo e obiettivi personali
- `_generated-skills/` — skill generate in attesa di revisione
- `.mnemoloci/` — dati interni del motore (non modificare a mano)

## Formato pagina
Frontmatter YAML: `title`, `kind` (concept|entity|comparison|overview), `summary`, `aliases`,
`sources` (lista di `{key, sha256}`), `confidence` (0-1), `provenanceState`
(extracted|merged|inferred|ambiguous), `contradictedBy` (lista di slug), `updated`.

Lingua: le pagine sono scritte nella lingua del vault (`mnemoloci config language`);
se la fonte è in un'altra lingua il testo originale resta in fondo alla pagina in un blocco
richiudibile `> [!quote]- Originale (en)` con le stesse citazioni, e il titolo originale è un alias.

Citazioni come nota inline Obsidian: `^[progetto/percorso/file.md#L10-L14]`.
Collegamenti: `[[slug]]` o `[[slug|Etichetta]]`.

## Regole
- Le pagine si scrivono solo tramite il motore (`mnemoloci compile submit`), mai a mano da un agente.
- Niente viene cancellato automaticamente.
"""

CONVENTIONS_EN = """\
# Mnemoloci vault

This vault is the source of truth for Mnemoloci. It is plain Markdown, compatible with Obsidian.

## Structure
- `wiki/concepts|entities|comparisons|overviews/` — compiled pages, one per concept
- `wiki/queries/` — saved answers (`mnemoloci answer save`), linked to the pages they rest on
- `wiki/index.md` — generated index, `wiki/log.md` — activity log (append only)
- `raw/` — immutable sources copied into the vault (screenshots, sessions, web pages/PDFs/videos in `clips/`, your own notes in `notes/`, imports)
- `memory/` — working memory: decisions, facts, lessons, handoffs
- `_meta/` — personal profile and goals
- `_generated-skills/` — generated skills waiting for review
- `.mnemoloci/` — the engine's internal data (do not edit by hand)

## Page format
YAML frontmatter: `title`, `kind` (concept|entity|comparison|overview), `summary`, `aliases`,
`sources` (list of `{key, sha256}`), `confidence` (0-1), `provenanceState`
(extracted|merged|inferred|ambiguous), `contradictedBy` (list of slugs), `updated`.

Language: pages are written in the vault's language (`mnemoloci config language`);
if the source is in another language the original text stays at the bottom of the page in a
collapsible `> [!quote]- Originale (en)` block with the same citations, and the original title is an alias.

Citations as an Obsidian inline note: `^[project/path/file.md#L10-L14]`.
Links: `[[slug]]` or `[[slug|Label]]`.

## Rules
- Pages are written only through the engine (`mnemoloci compile submit`), never by hand by an agent.
- Nothing is deleted automatically.
"""

CONVENTIONS = CONVENTIONS_IT


def engine(vault: Path) -> Path:
    return Path(vault) / ".mnemoloci"


def is_initialized(vault: Path) -> bool:
    return (engine(vault) / "config.json").is_file()


def require_vault(vault: Path) -> None:
    if not is_initialized(vault):
        raise MnemoError("NOT_INITIALIZED", f"no Mnemoloci vault at {vault} (run: mnemoloci init)")


def load_config(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    try:
        config = json.loads((engine(vault) / "config.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise MnemoError("CONFIG_INVALID", f"cannot read config: {exc}") from exc
    review = config.get("review") if isinstance(config, dict) else None
    if config.get("schema") != CONFIG_SCHEMA or not isinstance(review, dict):
        raise MnemoError("CONFIG_INVALID", "config schema or review section invalid")
    hold = review.get("hold")
    if not isinstance(hold, list) or not set(hold) <= HOLD_MODES:
        raise MnemoError("CONFIG_INVALID", f"unknown review hold mode in {hold!r}")
    threshold = review.get("low_confidence_threshold")
    if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise MnemoError("CONFIG_INVALID", "low_confidence_threshold must be within 0..1")
    if review.get("treat_missing_confidence_as") not in ("hold", "ok"):
        raise MnemoError("CONFIG_INVALID", "treat_missing_confidence_as must be hold|ok")
    language = config.setdefault("language", dict(LEGACY_LANGUAGE))
    if (not isinstance(language, dict) or not valid_language(language.get("target"))
            or not isinstance(language.get("keep_original"), bool)):
        raise MnemoError("CONFIG_INVALID", "language must be {target: 'it', keep_original: true|false}")
    embeddings = config.setdefault("embeddings", dict(DEFAULT_EMBEDDINGS))
    if not isinstance(embeddings, dict) or embeddings.get("provider") not in EMBEDDING_PROVIDERS:
        raise MnemoError("CONFIG_INVALID", f"embeddings.provider must be one of {sorted(EMBEDDING_PROVIDERS)}")
    if embeddings["provider"] == "ollama" and not embeddings.get("ollama_model"):
        raise MnemoError("CONFIG_INVALID", "embeddings.ollama_model is required when provider is ollama")
    local_extraction = config.setdefault("local_extraction", dict(DEFAULT_LOCAL_EXTRACTION))
    if not isinstance(local_extraction, dict):
        raise MnemoError("CONFIG_INVALID", "local_extraction must be an object")
    if "extract" not in local_extraction:
        # migrate the old flat, single-engine shape (one provider shared by both phases, saved by
        # an earlier version of this feature) into the new per-phase shape: both phases start out
        # mirroring whatever was configured before, exactly reproducing the old behaviour
        flat = {field: local_extraction.get(field, default) for field, default in DEFAULT_PHASE_ENGINE.items()}
        local_extraction["extract"] = dict(flat)
        local_extraction["write"] = dict(flat)
    for phase in ("extract", "write"):
        block = local_extraction.setdefault(phase, dict(DEFAULT_PHASE_ENGINE))
        if not isinstance(block, dict):
            raise MnemoError("CONFIG_INVALID", f"local_extraction.{phase} must be an object")
        for field, default in DEFAULT_PHASE_ENGINE.items():
            block.setdefault(field, default)
        if block.get("provider") not in LOCAL_EXTRACTION_PROVIDERS:
            raise MnemoError("CONFIG_INVALID", f"local_extraction.{phase}.provider must be one of {sorted(LOCAL_EXTRACTION_PROVIDERS)}")
    if local_extraction.get("mode") not in LOCAL_EXTRACTION_MODES:
        raise MnemoError("CONFIG_INVALID", f"local_extraction.mode must be one of {sorted(LOCAL_EXTRACTION_MODES)}")
    return config


def set_language(vault: Path, target: str, keep_original: bool) -> dict[str, Any]:
    if not valid_language(target):
        raise MnemoError("BAD_LANGUAGE", "use an ISO 639-1 code such as it, en, es")
    with VaultLock(vault) as lock:
        config = load_config(vault)
        config["language"] = {"target": target, "keep_original": keep_original}
        save_state(vault, lock, "config.json", config)
    return {"language": config["language"]}


def set_embeddings(vault: Path, provider: str, *, ollama_model: str | None = None,
                   ollama_base_url: str | None = None, openai_model: str | None = None,
                   voyage_model: str | None = None) -> dict[str, Any]:
    """Switch the embedding provider: fastembed (local, free, default), a local Ollama server,
    or a cloud API (OpenAI, or Voyage AI -- Anthropic's own recommended embedding partner, since
    Claude itself has no embeddings endpoint). Existing vectors are never deleted here: they stay
    tagged with their own model id (`fastembed`, `ollama:<model>`, `openai:<model>`,
    `voyage:<model>`), so switching back and forth never mixes embedding spaces -- `index embed`
    just re-embeds whatever the newly active provider hasn't covered yet. The API key for a cloud
    provider is set separately (`set_api_key`), never through this function."""
    if provider not in EMBEDDING_PROVIDERS:
        raise MnemoError("BAD_PROVIDER", f"provider must be one of {sorted(EMBEDDING_PROVIDERS)}")
    if provider == "ollama" and not ollama_model:
        raise MnemoError("NO_OLLAMA_MODEL", "give --ollama-model, e.g. nomic-embed-text")
    with VaultLock(vault) as lock:
        config = load_config(vault)
        config["embeddings"] = {
            "provider": provider,
            "ollama_model": ollama_model if provider == "ollama" else None,
            "ollama_base_url": ollama_base_url if provider == "ollama" else None,
            "openai_model": openai_model if provider == "openai" else None,
            "voyage_model": voyage_model if provider == "voyage" else None,
        }
        save_state(vault, lock, "config.json", config)
    return {"embeddings": config["embeddings"]}


def _phase_engine(flag_prefix: str, provider: str, ollama_model: str | None, ollama_base_url: str | None,
                  anthropic_model: str | None, claude_code_model: str | None) -> dict[str, Any]:
    """Validate and build one phase's engine block. `flag_prefix` ("" for extract, "write-" for
    write) only changes the CLI flag names quoted back in error messages."""
    if provider not in LOCAL_EXTRACTION_PROVIDERS:
        raise MnemoError("BAD_PROVIDER", f"--{flag_prefix}provider must be one of {sorted(LOCAL_EXTRACTION_PROVIDERS)}")
    if provider == "ollama" and not ollama_model:
        raise MnemoError("NO_OLLAMA_MODEL", f"give --{flag_prefix}ollama-model, e.g. llama3.1")
    if provider == "anthropic" and not anthropic_model:
        raise MnemoError("NO_ANTHROPIC_MODEL", f"give --{flag_prefix}anthropic-model, e.g. claude-haiku-4-5-20251001")
    if provider == "claude_code" and not claude_code_model:
        raise MnemoError("NO_CLAUDE_CODE_MODEL", f"give --{flag_prefix}claude-code-model, e.g. sonnet or claude-sonnet-5")
    return {
        "provider": provider,
        "ollama_model": (ollama_model or "").strip() or None if provider == "ollama" else None,
        "ollama_base_url": (ollama_base_url or "").strip() or None if provider == "ollama" else None,
        "anthropic_model": (anthropic_model or "").strip() or None if provider == "anthropic" else None,
        "claude_code_model": (claude_code_model or "").strip() or None if provider == "claude_code" else None,
    }


def set_local_extraction(vault: Path, *, mode: str, provider: str, ollama_model: str | None = None,
                         ollama_base_url: str | None = None, anthropic_model: str | None = None,
                         claude_code_model: str | None = None, write_provider: str | None = None,
                         write_ollama_model: str | None = None, write_ollama_base_url: str | None = None,
                         write_anthropic_model: str | None = None, write_claude_code_model: str | None = None) -> dict[str, Any]:
    """Configure unattended extraction (`mnemoloci compile extract-local` / the app's "Estrazione
    locale" section) -- not embeddings, a real chat/generation model, so the model fields here are
    independent of `embeddings.ollama_model`. `provider` picks who reasons about the text for the
    extraction phase: `ollama` (local, free), `anthropic` (Claude's own API, paid -- needs a key via
    `set_api_key`) or `claude_code` (the `claude` CLI in headless mode, billed to the same
    subscription as an interactive session, no separate key).

    `write_provider` (only meaningful with `mode=extract_and_write`) lets the page-writing phase use
    a *different* engine, e.g. ollama to pull raw concepts out cheaply, then claude_code or anthropic
    to write better prose where quality matters more. Leave it out and the write phase mirrors the
    extract phase exactly, as before this option existed.

    Whichever engine either phase uses, what it produces still always lands in the review queue
    (`local-extraction` hold, enforced in `compile.submit`), never published directly -- that
    safeguard lives in compile.py, not here, and is not something this setter can turn off.
    Switching away from `ollama` here only ever takes effect when something explicitly runs the
    extraction (a CLI command or the app's "Esegui ora" button): none of the paid/subscription
    providers are ever wired into the daily scheduled job, so Claude is never called unattended."""
    if mode not in LOCAL_EXTRACTION_MODES:
        raise MnemoError("BAD_MODE", f"mode must be one of {sorted(LOCAL_EXTRACTION_MODES)}")
    extract = _phase_engine("", provider, ollama_model, ollama_base_url, anthropic_model, claude_code_model)
    write = dict(extract) if write_provider is None else _phase_engine(
        "write-", write_provider, write_ollama_model, write_ollama_base_url, write_anthropic_model, write_claude_code_model)
    with VaultLock(vault) as lock:
        config = load_config(vault)
        config["local_extraction"] = {"mode": mode, "extract": extract, "write": write}
        save_state(vault, lock, "config.json", config)
    return {"local_extraction": config["local_extraction"]}


API_KEYS_SCHEMA = "mnemoloci.embedding_keys.v1"  # name predates local_extraction reusing this same store
API_KEY_ENV = {"openai": "OPENAI_API_KEY", "voyage": "VOYAGE_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
CLOUD_KEY_PROVIDERS = CLOUD_EMBEDDING_PROVIDERS | CLOUD_EXTRACTION_PROVIDERS


def set_api_key(vault: Path, provider: str, key: str) -> dict[str, Any]:
    """Store a cloud provider's API key (embeddings: openai/voyage; local extraction: anthropic) in
    `.mnemoloci/embedding_keys.json` -- gitignored by `backup init` (same treatment as
    `remote.json`'s device tokens), so it never lands in a git backup commit. An environment
    variable (see `API_KEY_ENV`) always wins over this file when both are set, for whoever would
    rather not have Mnemoloci touch the key at all."""
    if provider not in CLOUD_KEY_PROVIDERS:
        raise MnemoError("BAD_PROVIDER", f"provider must be one of {sorted(CLOUD_KEY_PROVIDERS)}")
    key = (key or "").strip()
    if not key:
        raise MnemoError("EMPTY_KEY", "the API key is empty")
    with VaultLock(vault) as lock:
        keys = load_state(vault, "embedding_keys.json", API_KEYS_SCHEMA, {})
        keys[provider] = key
        save_state(vault, lock, "embedding_keys.json", keys)
    return {"provider": provider, "key_set": True}


def get_api_key(vault: Path, provider: str) -> str | None:
    import os

    env = os.environ.get(API_KEY_ENV.get(provider, ""))
    if env:
        return env
    keys = load_state(vault, "embedding_keys.json", API_KEYS_SCHEMA, {})
    return keys.get(provider)


def load_state(vault: Path, name: str, schema: str, default: dict[str, Any]) -> dict[str, Any]:
    path = engine(vault) / name
    if not path.exists():
        return {"schema": schema, **default}
    data = json.loads(path.read_text("utf-8"))
    if data.get("schema") != schema:
        raise MnemoError("STATE_INVALID", f"{name} has unexpected schema")
    return data


def save_state(vault: Path, lock: VaultLock, name: str, data: dict[str, Any]) -> None:
    lock.assert_held()
    atomic_json(engine(vault) / name, data)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def init(vault: Path, language: str = "en") -> dict[str, Any]:
    """Create a vault. `language` is the language the engine writes in: "it" or "en" (the default)."""
    vault = Path(vault)
    italian = language == "it"
    if is_initialized(vault):
        raise MnemoError("ALREADY_INITIALIZED", f"{vault} is already a Mnemoloci vault")
    if vault.exists() and any(p.name not in (".obsidian",) for p in vault.iterdir()):
        raise MnemoError("VAULT_NOT_EMPTY", f"{vault} exists and is not empty")
    vault.mkdir(parents=True, exist_ok=True)
    with VaultLock(vault) as lock:
        for sub in ("transactions", "candidates", "work"):
            (engine(vault) / sub).mkdir(parents=True, exist_ok=True)
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["language"]["target"] = "it" if italian else "en"
        save_state(vault, lock, "config.json", config)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        bundle = {
            "schema": txn.BUNDLE_SCHEMA,
            "operation_id": f"init-{int(time.time())}",
            "operation": "init",
            "writes": [
                {"path": "MNEMOLOCI.md", "action": "create", "content": CONVENTIONS_IT if italian else CONVENTIONS_EN},
                {"path": "wiki/index.md", "action": "create",
                 "content": "# Indice\n\n_Nessuna pagina ancora._\n" if italian else "# Index\n\n_No pages yet._\n"},
                {"path": "wiki/log.md", "action": "create", "content": f"# Log\n\n## [{stamp}] init\n"},
            ],
        }
        plan = txn.prepare(vault, bundle)
        result = txn.apply(vault, bundle, plan.approval_sha256, lock=lock)
    return {"vault": str(vault), "created": result["changed_paths"]}
