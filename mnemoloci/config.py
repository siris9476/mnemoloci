"""Where the vault is. One resolver, used by the CLI, the hooks, the MCP server and the app, so they
all agree. Order: an explicit path (`--vault`), `MNEMOLOCI_VAULT`, the path registered in
`~/.mnemoloci/vault.txt` (`mnemoloci vault set`, or the first `init`), then `~/Mnemoloci`."""

import os
from pathlib import Path


def default_vault() -> Path:
    return Path.home() / "Mnemoloci"


def registry_file() -> Path:
    return Path.home() / ".mnemoloci" / "vault.txt"


def registered_vault() -> Path | None:
    """The vault recorded in vault.txt, or None."""
    try:
        raw = registry_file().read_text("utf-8").strip()
    except OSError:
        return None
    return Path(raw).expanduser() if raw else None


def resolve_vault(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("MNEMOLOCI_VAULT")
    if raw:
        return Path(raw).expanduser()
    return registered_vault() or default_vault()


def vault_source(explicit: str | None = None) -> str:
    """Which rule decided the vault: "argument", "environment", "registered" or "default"."""
    if explicit:
        return "argument"
    if os.environ.get("MNEMOLOCI_VAULT"):
        return "environment"
    return "registered" if registered_vault() else "default"


def record_vault(path: Path | str) -> Path:
    """Make `path` the vault every part of Mnemoloci uses (writes ~/.mnemoloci/vault.txt)."""
    target = Path(path).expanduser().resolve()
    file = registry_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(str(target) + "\n", "utf-8")
    return target


def vault_info(explicit: str | None = None) -> dict:
    """What the app and `vault show` report: the path in use, the rule that chose it and what vault.txt says."""
    from .vault import is_initialized

    path = resolve_vault(explicit)
    registered = registered_vault()
    source = vault_source(explicit)
    return {"path": str(path), "source": source, "initialized": is_initialized(path),
            "registered": str(registered) if registered else None,
            # an argument or MNEMOLOCI_VAULT beats vault.txt, so changing the file would not change anything
            "overridden": source in ("argument", "environment")}


def switch_vault(path: str) -> dict:
    """Point Mnemoloci at an existing vault. Hooks, the MCP server and the app pick it up when they next start."""
    from .core.errors import MnemoError
    from .vault import is_initialized

    raw = str(path or "").strip().strip('"')
    if not raw or not Path(raw).expanduser().is_absolute():
        raise MnemoError("NOT_ABSOLUTE", "give the full path of the vault folder, e.g. C:\\Users\\you\\Notes\\MyVault")
    target = Path(raw).expanduser()
    if not is_initialized(target):
        raise MnemoError("NOT_A_VAULT", f"{target} is not a Mnemoloci vault: create it first with `mnemoloci --vault \"{target}\" init`")
    record_vault(target)
    return {**vault_info(), "restart_needed": True}
