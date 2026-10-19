"""Start the Mnemoloci engine with the right Python, whatever `python` is first on PATH.

The plugin's hooks and MCP server run `python -P launch.py <args>`. Any Python 3 can run
this file (standard library only). It then runs `-m mnemoloci <args>` with, in order:
1. the interpreter recorded in %USERPROFILE%\\.mnemoloci\\python.txt (written by
   `mnemoloci init`, `doctor --fix` or the app's "Ripara" button);
2. this interpreter, when it can import mnemoloci;
3. `py -3` on Windows, `python3` elsewhere.
stdin/stdout/stderr are passed straight through (the MCP server speaks over them).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _recorded() -> str | None:
    try:
        text = (Path.home() / ".mnemoloci" / "python.txt").read_text("utf-8-sig").strip()
    except OSError:
        return None
    return text if text and Path(text).is_file() else None


def _same(a: str, b: str) -> bool:
    """Same interpreter *path*. Not samefile(): a virtualenv's python is often a symlink to the
    system one, yet only the venv path has the venv's packages (mnemoloci among them)."""
    def norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p))

    return norm(a) == norm(b)


def _run_here(args: list[str]) -> int:
    import runpy

    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]
    sys.argv = ["mnemoloci", *args]
    try:
        runpy.run_module("mnemoloci", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def main(args: list[str]) -> int:
    recorded = _recorded()
    if recorded and not _same(recorded, sys.executable):
        return subprocess.call([recorded, "-P", "-m", "mnemoloci", *args])
    try:
        import mnemoloci  # noqa: F401 - this interpreter has the engine
    except ImportError:
        if recorded:
            print(f"mnemoloci: {recorded} cannot import mnemoloci", file=sys.stderr)
            return 1
        fallback = ["py", "-3"] if os.name == "nt" else ["python3"]
        try:
            return subprocess.call([*fallback, "-P", "-m", "mnemoloci", *args])
        except OSError:
            print("mnemoloci: no Python with the engine found; run `python -P -m mnemoloci doctor --fix` "
                  "with the Python where mnemoloci is installed", file=sys.stderr)
            return 1
    return _run_here(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
