"""Which Python runs Mnemoloci, and how the engine follows the app that started it.

- The plugin's hooks and MCP server, and the desktop app, start the engine through
  `~/.mnemoloci/python.txt` (`%USERPROFILE%\\.mnemoloci\\python.txt` on Windows): the absolute path of a Python that can import
  mnemoloci, written by `init`, `doctor --fix` or the app's "Ripara" button. With it,
  a venv or another Python put first on PATH no longer switches the memory off.
- `watch_parent(pid)`: the engine started by the desktop app exits as soon as the app
  process is gone, even after a crash, so no server is left running with its token.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .core.atomic import atomic_write


def python_file() -> Path:
    return Path.home() / ".mnemoloci" / "python.txt"


def recorded_python() -> str | None:
    try:
        text = python_file().read_text("utf-8-sig").strip()
    except OSError:
        return None
    return text or None


def console_python(exe: str | None = None) -> str:
    """python.exe, never pythonw.exe: hooks and MCP need stdout."""
    path = Path(exe or sys.executable)
    if path.name.casefold() == "pythonw.exe" and path.with_name("python.exe").is_file():
        path = path.with_name("python.exe")
    return str(path)


def can_import(exe: str) -> tuple[bool, str]:
    """Does this interpreter find the engine (run from a neutral folder, like the plugin)?"""
    try:
        out = subprocess.run([exe, "-P", "-c", "import mnemoloci, sys; sys.stdout.write(mnemoloci.__version__)"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             cwd=tempfile.gettempdir(), timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return (True, out.stdout.strip()) if out.returncode == 0 else (False, (out.stderr or out.stdout).strip()[-300:])


def record_python(exe: str | None = None) -> dict[str, str]:
    """Remember this interpreter (or `exe`) for the plugin and the app, after checking it works."""
    chosen = console_python(exe)
    ok, detail = can_import(chosen)
    if not ok:
        from .core.errors import MnemoError

        raise MnemoError("PYTHON_CANNOT_IMPORT", f"{chosen} non riesce a importare mnemoloci: {detail}")
    target = python_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, (chosen + "\n").encode("utf-8"))
    return {"python": chosen, "file": str(target), "engine": detail}


def watch_parent(pid: int) -> None:
    """Exit this process when process `pid` ends. Windows: wait on its handle (no polling);
    Linux/macOS: check every 2 s whether it is still there (it is our parent, so its pid
    cannot be reused while we run)."""
    if pid <= 0:
        return
    if os.name != "nt":
        def poll() -> None:
            while True:
                if os.getppid() != pid:  # re-parented: the app is gone
                    os._exit(0)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    os._exit(0)
                except PermissionError:
                    pass  # exists, owned by someone else
                time.sleep(2)

        threading.Thread(target=poll, name="parent-watch", daemon=True).start()
        return
    import ctypes

    SYNCHRONIZE, INFINITE = 0x0010_0000, 0xFFFF_FFFF
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:  # already gone
        os._exit(0)

    def wait() -> None:
        kernel.WaitForSingleObject(ctypes.c_void_p(handle), INFINITE)
        os._exit(0)  # the app is gone: stop serving at once

    threading.Thread(target=wait, name="parent-watch", daemon=True).start()


def detached_popen(cmd: list[str], **kwargs) -> subprocess.Popen:
    """Start a process that outlives the one starting it (a hook ends in seconds) and never
    opens a window: Windows breaks away from the hook's job when allowed; elsewhere it gets
    its own session."""
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=True, **kwargs)
    base = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(cmd, creationflags=base | 0x0100_0000, **kwargs)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:  # the job does not allow breakaway
        return subprocess.Popen(cmd, creationflags=base, **kwargs)


def spawn_detached(args: list[str]) -> None:
    """Start `python -P -m mnemoloci <args>` on its own: no console window, not waited for,
    not killed with the hook that started it."""
    detached_popen([console_python(), "-P", "-m", "mnemoloci", *args], cwd=tempfile.gettempdir())


def python_names() -> list[str]:
    """Interpreter names to look for on PATH, most likely first on this system."""
    return ["python", "python3"] if os.name == "nt" else ["python3", "python"]
