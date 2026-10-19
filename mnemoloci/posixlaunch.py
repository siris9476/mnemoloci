"""Start-at-login and daily jobs on Linux (and other POSIX systems), the counterpart of
winlaunch.py. Only files and crontab lines that Mnemoloci wrote (they carry a marker)
are ever changed or removed.

- start at login: a systemd user service (~/.config/systemd/user/<name>.service, also
  on servers without a desktop). Without systemd, a desktop autostart entry
  (~/.config/autostart/<name>.desktop) when a desktop session is in use.
- daily job: a line in the user's crontab; without cron, a systemd user timer.
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .core.errors import MnemoError

MARKER = "created by Mnemoloci"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), input=stdin, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def systemd_user() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return _run("systemctl", "--user", "show-environment").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _systemd_quote(arg: str) -> str:
    """One ExecStart= argument: quoted, with \\ " and % escaped."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _desktop_quote(arg: str) -> str:
    """One Exec= argument of a .desktop file (Desktop Entry spec)."""
    escaped = re.sub(r'(["`$\\])', r"\\\1", arg).replace("%", "%%")
    return f'"{escaped}"'


def _unit_dir() -> Path:
    return _config_home() / "systemd" / "user"


def _ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text("utf-8", errors="replace")
    except OSError:
        return False


def start_at_login(name: str, argv: list[str], *, workdir: Path | None = None, description: str) -> dict[str, Any]:
    """Run argv at login (and now). Refuses to overwrite a file it did not write."""
    if systemd_user():
        unit = _unit_dir() / f"{name}.service"
        if unit.exists() and not _ours(unit):
            raise MnemoError("NOT_OURS", f"{unit} exists and was not written by Mnemoloci: left untouched")
        unit.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {MARKER}", "[Unit]", f"Description={description}", "", "[Service]",
                 "ExecStart=" + " ".join(_systemd_quote(a) for a in argv), "Restart=on-failure", "RestartSec=10"]
        if workdir:
            lines.append(f"WorkingDirectory={_systemd_quote(str(workdir))}")
        lines += ["", "[Install]", "WantedBy=default.target", ""]
        unit.write_text("\n".join(lines), "utf-8")
        _run("systemctl", "--user", "daemon-reload")
        out = _run("systemctl", "--user", "enable", "--now", f"{name}.service")
        if out.returncode != 0:
            raise MnemoError("AUTOSTART_FAILED", (out.stderr or out.stdout).strip()[:400])
        return {"autostart": True, "method": "systemd --user", "file": str(unit),
                "note": "on a server without a login session: `loginctl enable-linger` keeps it running after logout"}
    if os.environ.get("XDG_CURRENT_DESKTOP") or (_config_home() / "autostart").is_dir():
        entry = _config_home() / "autostart" / f"{name}.desktop"
        if entry.exists() and not _ours(entry):
            raise MnemoError("NOT_OURS", f"{entry} exists and was not written by Mnemoloci: left untouched")
        entry.parent.mkdir(parents=True, exist_ok=True)
        cmd = " ".join(_desktop_quote(a) for a in argv)
        if workdir:
            cmd = f"sh -c {_desktop_quote('cd ' + shlex.quote(str(workdir)) + ' && exec ' + shlex.join(argv))}"
        entry.write_text("\n".join([f"# {MARKER}", "[Desktop Entry]", "Type=Application", f"Name={description}",
                                    f"Exec={cmd}", "X-GNOME-Autostart-enabled=true", "NoDisplay=true", ""]), "utf-8")
        return {"autostart": True, "method": "desktop autostart", "file": str(entry),
                "note": "starts at the next desktop login"}
    raise MnemoError("NO_AUTOSTART", "neither systemd user services nor a desktop session were found: start it "
                                     "yourself, e.g. from your shell profile or your init system")


def stop_at_login(name: str) -> dict[str, Any]:
    removed = []
    unit = _unit_dir() / f"{name}.service"
    if unit.is_file() and _ours(unit):
        if shutil.which("systemctl"):
            _run("systemctl", "--user", "disable", "--now", f"{name}.service")
        unit.unlink()  # only ever our own unit
        if shutil.which("systemctl"):
            _run("systemctl", "--user", "daemon-reload")
        removed.append(str(unit))
    entry = _config_home() / "autostart" / f"{name}.desktop"
    if entry.is_file() and _ours(entry):
        entry.unlink()
        removed.append(str(entry))
    return {"autostart": False, **({"removed": removed} if removed else {"note": "not enabled"})}


def autostart_file(name: str) -> Path | None:
    for path in (_unit_dir() / f"{name}.service", _config_home() / "autostart" / f"{name}.desktop"):
        if path.is_file() and _ours(path):
            return path
    return None


# ---------------- daily job ----------------

def _cron_line(tag: str, at: str, argv: list[str]) -> str:
    hour, minute = at.split(":")
    command = shlex.join(argv).replace("%", "\\%")  # % is a newline in crontab
    return f"{int(minute)} {int(hour)} * * * {command} # {MARKER}: {tag}"


def _crontab() -> tuple[bool, list[str]]:
    if not shutil.which("crontab"):
        return False, []
    out = _run("crontab", "-l")
    return True, out.stdout.splitlines() if out.returncode == 0 else []


def daily(tag: str, at: str, argv: list[str]) -> dict[str, Any]:
    has_cron, lines = _crontab()
    if has_cron:
        kept = [line for line in lines if f"# {MARKER}: {tag}" not in line]
        new = kept + [_cron_line(tag, at, argv)]
        out = _run("crontab", "-", stdin="\n".join(new) + "\n")
        if out.returncode != 0:
            raise MnemoError("SCHEDULE_FAILED", (out.stderr or out.stdout).strip()[:400])
        return {"method": "cron", "line": new[-1]}
    if systemd_user():
        base = _unit_dir() / tag
        for suffix in (".service", ".timer"):
            path = base.with_suffix(suffix)
            if path.exists() and not _ours(path):
                raise MnemoError("NOT_OURS", f"{path} exists and was not written by Mnemoloci: left untouched")
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".service").write_text("\n".join([
            f"# {MARKER}", "[Unit]", "Description=Mnemoloci daily job", "", "[Service]", "Type=oneshot",
            "ExecStart=" + " ".join(_systemd_quote(a) for a in argv), ""]), "utf-8")
        base.with_suffix(".timer").write_text("\n".join([
            f"# {MARKER}", "[Unit]", "Description=Mnemoloci daily job", "", "[Timer]", f"OnCalendar=*-*-* {at}:00",
            "Persistent=true", "", "[Install]", "WantedBy=timers.target", ""]), "utf-8")
        _run("systemctl", "--user", "daemon-reload")
        out = _run("systemctl", "--user", "enable", "--now", f"{tag}.timer")
        if out.returncode != 0:
            raise MnemoError("SCHEDULE_FAILED", (out.stderr or out.stdout).strip()[:400])
        return {"method": "systemd timer", "file": str(base.with_suffix(".timer"))}
    raise MnemoError("NO_SCHEDULER", "neither cron nor systemd user timers were found: install cron "
                                     "(e.g. `sudo apt install cron`) or run `mnemoloci scheduled-run` from your own scheduler")


def daily_remove(tag: str) -> dict[str, Any]:
    removed = False
    has_cron, lines = _crontab()
    if has_cron and any(f"# {MARKER}: {tag}" in line for line in lines):
        kept = [line for line in lines if f"# {MARKER}: {tag}" not in line]
        _run("crontab", "-", stdin=("\n".join(kept) + "\n") if kept else "")
        removed = True
    base = _unit_dir() / tag
    if base.with_suffix(".timer").is_file() and _ours(base.with_suffix(".timer")):
        if shutil.which("systemctl"):
            _run("systemctl", "--user", "disable", "--now", f"{tag}.timer")
        for suffix in (".timer", ".service"):
            path = base.with_suffix(suffix)
            if path.is_file() and _ours(path):
                path.unlink()
        removed = True
    return {"removed": removed}


def daily_status(tag: str) -> dict[str, Any]:
    _, lines = _crontab()
    line = next((line for line in lines if f"# {MARKER}: {tag}" in line), None)
    if line:
        return {"enabled": True, "method": "cron", "details": [line]}
    timer = (_unit_dir() / tag).with_suffix(".timer")
    if timer.is_file() and _ours(timer):
        return {"enabled": True, "method": "systemd timer", "details": [str(timer)]}
    return {"enabled": False, "details": []}
