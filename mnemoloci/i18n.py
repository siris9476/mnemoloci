"""The language the engine writes in for a vault.

It follows the vault's own `language.target`: "it" gives Italian, any other value gives English. A vault
whose config has no language section predates the setting and was Italian, so it stays Italian; a
vault with no config at all (a new one) gets English. The app can force a language for one request
(its interface language) with `forced()`.
"""

import contextvars
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_FORCED: contextvars.ContextVar[str | None] = contextvars.ContextVar("mnemoloci_language", default=None)
_CACHE: dict[str, tuple[int, str]] = {}


def lang(vault: Path | str | None = None) -> str:
    """"it" or "en"."""
    forced = _FORCED.get()
    if forced in ("it", "en"):
        return forced
    if vault is None:
        return "en"
    from .vault import engine

    path = engine(Path(vault)) / "config.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return "en"
    hit = _CACHE.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(path.read_text("utf-8"))
        target = (data.get("language") or {}).get("target", "it")
    except (OSError, ValueError, AttributeError):
        target = "en"
    value = "it" if target == "it" else "en"
    _CACHE[str(path)] = (stamp, value)
    return value


def t(vault: Path | str | None, it: str, en: str) -> str:
    """The Italian or the English text, according to the vault's language."""
    return it if lang(vault) == "it" else en


LINKS_HEADINGS = ("## Collegamenti", "## Links")  # either one marks a page's list of related pages


def links_heading(vault: Path | str | None = None) -> str:
    """The heading of a page's list of related pages, in the vault's language."""
    return t(vault, "## Collegamenti", "## Links")


def original_label(vault: Path | str | None = None) -> str:
    """The title of the collapsed original-text callout, in the vault's language."""
    return t(vault, "Originale", "Original")


@contextmanager
def forced(language: str | None) -> Iterator[None]:
    """Use this language for everything inside the block (None leaves the vault's own)."""
    token = _FORCED.set(language if language in ("it", "en") else None)
    try:
        yield
    finally:
        _FORCED.reset(token)
