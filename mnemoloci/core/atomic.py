import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .errors import MnemoError
from .hashing import pretty_json

# Windows refuses os.replace while another process (Obsidian, an antivirus
# scanner, a sync client) holds the target open without FILE_SHARE_DELETE.
# Those holds are brief, so retry with backoff before giving up.
REPLACE_RETRY_SECONDS = 3.0


def _replace_with_retry(source: Path, target: Path) -> None:
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    delay = 0.02
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise MnemoError(
                    "FILE_BUSY", f"cannot replace {target}: file is in use ({exc})"
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, pretty_json(value))
