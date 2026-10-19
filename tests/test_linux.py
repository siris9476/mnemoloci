"""Linux (POSIX) counterparts of the Windows-only pieces: daily job through cron or a systemd
user timer, start at login through a systemd user service or a desktop autostart entry,
the engine following the app's process, and the plugin's sh launcher. Fake `crontab` and
`systemctl` programs on PATH stand in for the real ones."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX only")

from mnemoloci import posixlaunch, remote, schedule  # noqa: E402
from mnemoloci import vault as vault_mod  # noqa: E402
from mnemoloci.core.errors import MnemoError  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, "utf-8")
    path.chmod(0o755)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """A PATH with only the basics plus the fakes each test adds, and a private config home."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("sh", "cat", "printf", "uname", "dirname", "env"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    return bin_dir


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def test_daily_job_through_cron_keeps_the_users_own_lines(v, tools, tmp_path):
    table = tmp_path / "crontab.txt"
    table.write_text("0 7 * * * /usr/bin/backup-mine\n", "utf-8")
    _fake(tools, "crontab", f'if [ "$1" = "-l" ]; then cat "{table}"; else cat > "{table}"; fi\n')
    out = schedule.enable(v, at="21:15")
    assert out["method"] == "cron"
    schedule.enable(v, at="22:05")  # enabling again replaces our line, never duplicates it
    lines = table.read_text("utf-8").splitlines()
    ours = [line for line in lines if posixlaunch.MARKER in line]
    assert lines[0] == "0 7 * * * /usr/bin/backup-mine" and len(ours) == 1
    assert ours[0].startswith("5 22 * * * ") and "scheduled-run" in ours[0]
    assert schedule.status(v)["enabled"]
    assert schedule.disable()["removed"]
    assert table.read_text("utf-8").splitlines() == ["0 7 * * * /usr/bin/backup-mine"]
    assert not schedule.status(v)["enabled"]


def test_daily_job_without_cron_uses_a_systemd_timer_or_says_what_to_do(v, tools, tmp_path):
    with pytest.raises(MnemoError) as info:
        schedule.enable(v, at="21:15")
    assert info.value.code == "NO_SCHEDULER"
    log = tmp_path / "systemctl.log"
    _fake(tools, "systemctl", f'echo "$@" >> "{log}"\nexit 0\n')
    out = schedule.enable(v, at="06:30")
    assert out["method"] == "systemd timer"
    timer = tmp_path / "config/systemd/user/mnemoloci-daily.timer"
    assert "OnCalendar=*-*-* 06:30:00" in timer.read_text("utf-8")
    assert "--user enable --now mnemoloci-daily.timer" in log.read_text("utf-8")
    assert schedule.status(v)["method"] == "systemd timer"
    schedule.disable()
    assert not timer.exists()


def test_phone_server_starts_at_login_as_a_systemd_user_service(v, tools, tmp_path):
    log = tmp_path / "systemctl.log"
    _fake(tools, "systemctl", f'echo "$@" >> "{log}"\nexit 0\n')
    out = remote.autostart(v, True)
    unit = Path(out["file"])
    text = unit.read_text("utf-8")
    assert out["method"] == "systemd --user" and posixlaunch.MARKER in text
    assert f'"{sys.executable}" "-P" "-m" "mnemoloci" "--vault"' in text and text.rstrip().endswith("WantedBy=default.target")
    assert "--user enable --now mnemoloci-remote.service" in log.read_text("utf-8")
    remote.autostart(v, False)
    assert not unit.exists()
    # a unit the user wrote with the same name is never overwritten nor removed
    unit.write_text("[Service]\nExecStart=/bin/true\n", "utf-8")
    with pytest.raises(MnemoError) as info:
        remote.autostart(v, True)
    assert info.value.code == "NOT_OURS"
    assert remote.autostart(v, False)["note"] == "not enabled" and unit.exists()


def test_without_systemd_a_desktop_session_gets_an_autostart_entry(v, tools, tmp_path, monkeypatch):
    with pytest.raises(MnemoError) as info:
        remote.autostart(v, True)
    assert info.value.code == "NO_AUTOSTART"
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    out = remote.autostart(v, True)
    entry = Path(out["file"])
    assert out["method"] == "desktop autostart" and entry.parent.name == "autostart"
    assert "Exec=" in entry.read_text("utf-8") and "remote" in entry.read_text("utf-8")
    remote.autostart(v, False)
    assert not entry.exists()


def test_systemd_arguments_are_quoted():
    assert posixlaunch._systemd_quote('/home/a b/100% "x"') == '"/home/a b/100%% \\"x\\""'


def test_engine_exits_when_the_app_that_started_it_is_gone(tmp_path):
    # like the desktop app: the parent passes its own pid (--parent-pid), the engine watches it
    child_code = ("import sys, time; sys.path.insert(0, %r); from mnemoloci import runtime; "
                  "runtime.watch_parent(int(sys.argv[1])); time.sleep(60)") % str(REPO)
    parent_code = ("import os, subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', %r, str(os.getpid())]); "
                   "print(p.pid, flush=True); time.sleep(60)") % child_code
    parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    engine_pid = int(parent.stdout.readline())
    parent.kill()
    parent.wait()
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            os.kill(engine_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(engine_pid, 9)
    pytest.fail("the engine kept running after its parent died")


def test_plugin_sh_launcher_finds_python3(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("uname", "dirname"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    # only python3, as on Debian/Ubuntu (a wrapper: a symlink to a venv's python loses the venv)
    (bin_dir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', "utf-8")
    (bin_dir / "python3").chmod(0o755)
    out = subprocess.run(["/bin/sh", str(REPO / "plugin/bin/mnemoloci-run"), "--version"], capture_output=True,
                         text=True, env={**os.environ, "PATH": str(bin_dir), "HOME": str(tmp_path)}, timeout=60)
    assert out.returncode == 0 and "mnemoloci" in out.stdout, out.stderr
    out = subprocess.run(["/bin/sh", str(REPO / "plugin/bin/mnemoloci-run"), "--version"], capture_output=True,
                         text=True, env={**os.environ, "PATH": str(tmp_path / "empty")}, timeout=60)
    assert out.returncode == 1 and "no Python 3.11+" in out.stderr


def test_launcher_tells_a_venv_python_from_the_system_one_it_links_to(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("launch", REPO / "plugin/bin/launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)
    venv_python = tmp_path / "venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)  # how venv links it; only this path sees the venv's packages
    assert not launch._same(str(venv_python), sys.executable)
    assert launch._same(sys.executable, sys.executable)
