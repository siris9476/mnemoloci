"""Every setting a person can change, behind one read and one write per section.

The CLI and the web app both go through here, so a value is validated the same way whichever door it
comes through. Sections: language, instructions, review, ingest, backup, reflection, reason, schedule.
Embeddings and local extraction keep their own functions in `vault` (they also hold API keys)."""

import re
from pathlib import Path
from typing import Any

from . import backup, compile as compiler, hooks, reason, schedule
from .core.errors import ValidationError
from .core.lock import VaultLock
from .vault import HOLD_MODES, load_config, require_vault, save_state, set_language

SECTIONS = ("language", "instructions", "review", "ingest", "backup", "reflection", "reason", "schedule")
MIN_FILE_BYTES, MAX_FILE_BYTES = 1024, 8 * 1024 * 1024
MAX_REFLECTION = 500
MAX_BUDGET_USD = 20
ALWAYS_HELD = {"local-extraction": 'Page extracted by a local model'}  # shown as the reason in the form, translated there
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _edit_config(vault: Path, change) -> dict[str, Any]:
    with VaultLock(vault) as lock:
        config = load_config(vault)
        change(config)
        save_state(vault, lock, "config.json", config)
    return config


def _number(value: Any, name: str, low: float, high: float, *, whole: bool = False) -> float | int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("BAD_SETTING", f"{name} must be a number") from None
    if not low <= number <= high:
        raise ValidationError("BAD_SETTING", f"{name} must be between {low:g} and {high:g}")
    return int(number) if whole else number


def get(vault: Path) -> dict[str, Any]:
    """Every section's current value, with the choices the forms need. The schedule is separate
    (`schedule_status`): it asks the operating system, which is slow."""
    require_vault(vault)
    config = load_config(vault)
    review = config["review"]
    soul = Path(vault) / compiler.SOUL_PATH
    text = soul.read_text("utf-8", errors="replace") if soul.is_file() else ""
    return {
        "language": dict(config["language"]),
        "instructions": {"text": text, "chars": len(text), "used_chars": compiler.SOUL_CHARS},
        "review": {"hold": sorted(m for m in review["hold"] if m not in ALWAYS_HELD),
                   "choices": sorted(HOLD_MODES - set(ALWAYS_HELD)), "always": ALWAYS_HELD,
                   "low_confidence_threshold": review["low_confidence_threshold"],
                   "treat_missing_confidence_as": review["treat_missing_confidence_as"]},
        "ingest": {"max_file_bytes": config["ingest"]["max_file_bytes"], "min": MIN_FILE_BYTES, "max": MAX_FILE_BYTES},
        "backup": {**backup.settings(vault), **{k: v for k, v in backup.status(vault).items()
                                                if k in ("enabled", "remote", "last_commit", "uncommitted_changes", "unpushed_commits")}},
        "reflection": {"every": int((config.get("reflection") or {}).get("every", hooks.REFLECT_EVERY)),
                       "max": MAX_REFLECTION},
        "reason": {"maxBudgetUsd": float((config.get("reason") or {}).get("maxBudgetUsd", reason.DEFAULT_BUDGET_USD)),
                   "max": MAX_BUDGET_USD},
    }


def schedule_status(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    return schedule.status(vault)


def update(vault: Path, section: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate and save one section; returns that section as `get` would."""
    require_vault(vault)
    if section not in SECTIONS:
        raise ValidationError("BAD_SECTION", f"section must be one of {list(SECTIONS)}")
    if not isinstance(data, dict):
        raise ValidationError("BAD_SETTING", "the values of the section are needed")
    {"language": _language, "instructions": _instructions, "review": _review, "ingest": _ingest,
     "backup": _backup, "reflection": _reflection, "reason": _reason, "schedule": _schedule}[section](vault, data)
    return {"section": section, "settings": get(vault)[section] if section != "schedule" else schedule_status(vault)}


def _language(vault: Path, data: dict[str, Any]) -> None:
    current = load_config(vault)["language"]
    keep = data.get("keep_original", current["keep_original"])
    if not isinstance(keep, bool):
        raise ValidationError("BAD_SETTING", "keep_original deve essere vero o falso")
    set_language(vault, str(data.get("target", current["target"])).strip().lower(), keep)


def _instructions(vault: Path, data: dict[str, Any]) -> None:
    text = data.get("text")
    if not isinstance(text, str):
        raise ValidationError("BAD_SETTING", "text must be a string")
    compiler.set_instructions(vault, text.strip())


def _review(vault: Path, data: dict[str, Any]) -> None:
    current = load_config(vault)["review"]
    hold = data.get("hold", [m for m in current["hold"] if m not in ALWAYS_HELD])
    if not isinstance(hold, list) or not set(map(str, hold)) <= HOLD_MODES - set(ALWAYS_HELD):
        raise ValidationError("BAD_SETTING", f"hold must be a list of values from {sorted(HOLD_MODES - set(ALWAYS_HELD))}")
    threshold = _number(data.get("low_confidence_threshold", current["low_confidence_threshold"]),
                        "the low-confidence threshold", 0, 1)
    missing = data.get("treat_missing_confidence_as", current["treat_missing_confidence_as"])
    if missing not in ("hold", "ok"):
        raise ValidationError("BAD_SETTING", "treat_missing_confidence_as must be hold or ok")

    def change(config: dict[str, Any]) -> None:
        # the always-held modes stay in the saved list: they are not something this form can turn off
        config["review"] = {**config["review"], "hold": sorted({*map(str, hold), *(m for m in config["review"]["hold"] if m in ALWAYS_HELD)}),
                            "low_confidence_threshold": threshold, "treat_missing_confidence_as": missing}

    _edit_config(vault, change)


def _ingest(vault: Path, data: dict[str, Any]) -> None:
    size = _number(data.get("max_file_bytes"), "the maximum file size", MIN_FILE_BYTES, MAX_FILE_BYTES, whole=True)
    _edit_config(vault, lambda config: config["ingest"].__setitem__("max_file_bytes", size))


def _backup(vault: Path, data: dict[str, Any]) -> None:
    current = backup.settings(vault)
    commit, push = data.get("auto_commit", current["auto_commit"]), data.get("auto_push", current["auto_push"])
    if not isinstance(commit, bool) or not isinstance(push, bool):
        raise ValidationError("BAD_SETTING", "auto_commit and auto_push must be true or false")
    status = backup.status(vault)
    if (commit or push) and not status.get("enabled"):
        raise ValidationError("NO_BACKUP", "backup is not active: start it first with `mnemoloci backup init`")
    if push and not status.get("remote"):
        raise ValidationError("NO_REMOTE", "automatic push needs a remote repository")
    _edit_config(vault, lambda config: config.__setitem__("backup", {**(config.get("backup") or {}), "auto_commit": commit, "auto_push": push}))


def _reflection(vault: Path, data: dict[str, Any]) -> None:
    every = _number(data.get("every"), "the reminder frequency", 0, MAX_REFLECTION, whole=True)
    _edit_config(vault, lambda config: config.__setitem__("reflection", {**(config.get("reflection") or {}), "every": every}))


def _reason(vault: Path, data: dict[str, Any]) -> None:
    budget = _number(data.get("maxBudgetUsd"), "the spending cap", 0.01, MAX_BUDGET_USD)
    _edit_config(vault, lambda config: config.__setitem__("reason", {**(config.get("reason") or {}), "maxBudgetUsd": round(budget, 2)}))


def _schedule(vault: Path, data: dict[str, Any]) -> None:
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise ValidationError("BAD_SETTING", "enabled must be true or false")
    if not enabled:
        schedule.disable()
        return
    at = str(data.get("time") or "21:30")
    if not _TIME.match(at):
        raise ValidationError("BAD_SETTING", "l'orario deve essere HH:MM")
    schedule.enable(vault, at=at)

