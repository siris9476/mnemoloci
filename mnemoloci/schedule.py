"""A daily maintenance run,
strictly opt-in: Windows Task Scheduler, or on Linux cron / a systemd user timer.

`schedule enable --time 21:30` creates one task that runs `scheduled-run` once a
day, with pythonw (no window). The run only does safe, local, idempotent work:
refresh the session search index and skill usage, save the day's digest, record
the quality score and duplicate-page count, and back up the vault when backup
is enabled. Every run is logged in .mnemoloci/scheduled.log. `schedule disable`
removes the task.

This covers what a per-session hook cannot afford to ration by usage: sources,
connectors and the session index are full scans with no cheap "is it due yet"
check, so they stay on this once-a-day cadence rather than SessionEnd's. The
embed/fix-links/hub-links upkeep moved to SessionEnd instead (see hooks.py):
tied to actual use, so a PC that is off at the scheduled time no longer means
it silently skips a day.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import winlaunch
from .core.errors import MnemoError, ValidationError
from .vault import engine, require_vault

TASK = "Mnemoloci giornaliero"
LINUX_TAG = "mnemoloci-daily"
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise MnemoError("WINDOWS_ONLY", "scheduling is implemented with Windows Task Scheduler")
    # schtasks prints in the console (OEM) code page: Italian messages would come out garbled as UTF-8
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="oem", errors="replace", timeout=60)


def _pythonw() -> str:
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.is_file() else exe)


def enable(vault: Path, *, at: str = "21:30") -> dict[str, Any]:
    require_vault(vault)
    if not _TIME.match(at or ""):
        raise ValidationError("BAD_TIME", "time must be HH:MM (24h)")
    if os.name != "nt":
        from . import posixlaunch

        argv = [sys.executable, "-P", "-m", "mnemoloci", "--vault", str(Path(vault).resolve()), "scheduled-run"]
        return {"task": LINUX_TAG, "time": at, **posixlaunch.daily(LINUX_TAG, at, argv),
                "runs": ["session index", "skill usage", "daily digest", "quality score", "backup (if enabled)"],
                "note": "`mnemoloci schedule disable` removes it"}
    command = f"{winlaunch.arg(_pythonw())} -P -m mnemoloci --vault {winlaunch.arg(Path(vault).resolve())} scheduled-run"
    if len(command) > winlaunch.MAX_TASK_COMMAND:
        raise ValidationError("PATH_TOO_LONG", f"the task command is {len(command)} characters; Task Scheduler takes "
                                               f"{winlaunch.MAX_TASK_COMMAND}: use a shorter vault or Python path")
    out = _schtasks("/Create", "/SC", "DAILY", "/TN", TASK, "/TR", command, "/ST", at, "/F")
    if out.returncode != 0:
        raise MnemoError("SCHEDULE_FAILED", (out.stderr or out.stdout).strip()[:400])
    return {"task": TASK, "time": at, "command": command,
            "runs": ["session index", "skill usage", "daily digest", "quality score", "backup (if enabled)"],
            "note": "runs while you are logged in; `mnemoloci schedule disable` removes it"}


def disable() -> dict[str, Any]:
    if os.name != "nt":
        from . import posixlaunch

        return {"task": LINUX_TAG, **posixlaunch.daily_remove(LINUX_TAG)}
    out = _schtasks("/Delete", "/TN", TASK, "/F")
    return {"task": TASK, "removed": out.returncode == 0,
            **({} if out.returncode == 0 else {"note": "no such task (already disabled)"})}


def status(vault: Path) -> dict[str, Any]:
    log = engine(vault) / "scheduled.log"
    last = None
    if log.is_file():
        lines = [line for line in log.read_text("utf-8").splitlines() if line.strip()]
        last = json.loads(lines[-1]) if lines else None
    if os.name != "nt":
        from . import posixlaunch

        return {"task": LINUX_TAG, **posixlaunch.daily_status(LINUX_TAG), "last_run": last}
    out = _schtasks("/Query", "/TN", TASK, "/FO", "LIST", "/V")
    return {"enabled": out.returncode == 0, "task": TASK,
            "details": [line.strip() for line in out.stdout.splitlines() if line.strip()][:25] if out.returncode == 0 else [],
            "last_run": last}


def _verify(vault: Path, steps: dict[str, Any]) -> dict[str, Any]:
    """Recheck the day's strongest claims against the systems they say they touched, instead of
    trusting each step's own return value: a digest file
    that doesn't exist, a backup commit that isn't HEAD, or a quality score never actually recorded
    should all surface here rather than silently pass as "no error". Only claims with an
    independently verifiable ground truth are checked -- sources/connectors/skills_usage have no
    concrete external claim worth rechecking here."""
    from . import backup, quality

    issues: list[str] = []

    digest_step = steps.get("digest") or {}
    if "path" in digest_step:
        path = Path(vault) / digest_step["path"]
        if not path.is_file() or path.stat().st_size == 0:
            issues.append(f"digest: {digest_step['path']} missing or empty")

    backup_step = steps.get("backup") or {}
    if "commit" in backup_step:
        last = (backup.status(vault).get("last_commit") or {}).get("id")
        if last != backup_step["commit"]:
            issues.append(f"backup: commit {backup_step['commit']} is not HEAD ({last})")

    quality_step = steps.get("quality") or {}
    if quality_step.get("score") is not None:
        today = time.strftime("%Y-%m-%d")
        recent = quality.history(vault, 1)
        if not recent or not str(recent[-1].get("date", "")).startswith(today):
            issues.append("quality: score was not recorded in today's history")

    result: dict[str, Any] = {"issues": issues}
    # report-only: how many citations no longer line up with their source, and the change since the last run
    from . import citecheck

    try:
        found = citecheck.check(vault, limit=0)
        result.update(citations=found["citations"], weak_citations=found["weak"])
        before = _last_weak(vault)
        if before is not None:
            result["weak_citations_change"] = found["weak"] - before
    except Exception as exc:  # noqa: BLE001 - never hides the other checks' issues
        result["weak_citations_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return result


def _last_weak(vault: Path) -> int | None:
    log = engine(vault) / "scheduled.log"
    if not log.is_file():
        return None
    for line in reversed([ln for ln in log.read_text("utf-8").splitlines() if ln.strip()]):
        try:
            value = (json.loads(line).get("steps", {}).get("verify") or {}).get("weak_citations")
        except ValueError:
            continue
        if isinstance(value, int):
            return value
    return None


def run(vault: Path) -> dict[str, Any]:
    """The daily job. Each step is independent: one failing does not stop the others."""
    require_vault(vault)
    from . import backup, connectors, digest, maintain, quality, session_index, skill_curator
    from .sources import scan

    steps: dict[str, Any] = {}

    def step(name: str, fn) -> None:
        try:
            steps[name] = fn()
        except Exception as exc:  # noqa: BLE001 - logged, never fatal for the other steps
            steps[name] = {"error": f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:300]}

    step("sources", lambda: {"changed_files": sum(len(r.get("changed", [])) for r in scan(vault).values())})
    # never calls an LLM: only downloads files from connected providers (Google Drive, ...), which
    # then land as external clips like any other source, still waiting to be understood/extracted
    step("connectors", lambda: connectors.sync_all(vault))
    step("sessions", lambda: session_index.refresh(vault))
    step("skills_usage", lambda: {"skills": len(skill_curator.usage(vault))})
    step("digest", lambda: digest.save(vault))
    step("quality", lambda: {k: v for k, v in quality.run(vault, record=True).items() if k in ("score", "delta")})
    # report only: merging is a judgment call (which page is canonical, what to keep from each),
    # never applied on its own -- the nightly log just surfaces the count for `maintain propose-merge`
    step("duplicates", lambda: {"pairs": len(maintain.duplicates(vault)["pairs"])})
    step("backup", lambda: backup.now(vault, message="Mnemoloci: backup giornaliero",
                                      push_after=backup.settings(vault)["auto_push"])
         if backup.status(vault)["enabled"] else {"skipped": "backup not enabled"})
    step("verify", lambda: _verify(vault, steps))
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": steps}
    with open(engine(vault) / "scheduled.log", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record
