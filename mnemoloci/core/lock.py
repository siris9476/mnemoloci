import hmac
import json
import os
import secrets
import socket
import time
from pathlib import Path

from .atomic import atomic_json
from .errors import ConflictError, MnemoError

ENGINE_DIR = ".mnemoloci"


class VaultLock:
    """Exclusive, crash-safe, cross-process lock for one vault.

    Windows: msvcrt byte-range lock; POSIX: flock. Both are held by the OS on an
    open handle and vanish the instant the holder dies, so a crash never leaves a
    stale lock and there is no PID-liveness guessing. The lock file is permanent
    and never removed on release: deleting it would let a waiting process lock a
    file that is about to disappear while a third process creates a new one.
    """

    def __init__(self, vault: Path, *, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.vault = Path(vault)
        self.timeout = timeout
        self.poll = poll
        self.token = secrets.token_hex(16)
        self._fd: int | None = None
        self.lock_dir = self.vault / ENGINE_DIR / "lock"

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _try_lock(self, fd: int) -> bool:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def owner(self) -> dict | None:
        try:
            return json.loads((self.lock_dir / "owner.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def acquire(self) -> None:
        if self.held:
            return
        if not self.vault.is_dir():
            raise MnemoError("NO_VAULT", f"vault does not exist: {self.vault}")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            fd = os.open(self.lock_dir / "lockfile.bin", flags, 0o644)
            if self._try_lock(fd):
                break
            os.close(fd)
            if time.monotonic() >= deadline:
                owner = self.owner() or {}
                raise ConflictError(
                    "LOCK_TIMEOUT", f"vault is locked by pid {owner.get('pid', 'unknown')}"
                )
            time.sleep(self.poll)
        self._fd = fd
        try:
            atomic_json(
                self.lock_dir / "owner.json",
                {
                    "schema": "mnemoloci.lock-owner.v1",
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "token": self.token,
                    "started_epoch": time.time(),
                },
            )
        except BaseException:
            self.release()
            raise

    def assert_held(self) -> None:
        owner = self.owner() or {}
        if not self.held or not hmac.compare_digest(str(owner.get("token", "")), self.token):
            raise MnemoError("LOCK_LOST", "vault lock ownership was lost")

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "VaultLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
