import os
import signal
import time

import pytest

from mnemoloci.core.errors import ConflictError
from mnemoloci.core.lock import VaultLock

from conftest import spawn

HOLDER = """
import os, time
from mnemoloci.core.lock import VaultLock
lock = VaultLock(r"{vault}")
lock.acquire()
print("READY", os.getpid(), flush=True)
time.sleep(30)
"""


def _wait_ready(proc) -> str:
    line = proc.stdout.readline().split()
    assert line and line[0] == "READY", proc.stderr.read()
    return line[1]


def test_second_process_times_out(vault):
    proc = spawn(HOLDER.format(vault=vault))
    try:
        holder_pid = _wait_ready(proc)
        with pytest.raises(ConflictError) as info:
            VaultLock(vault, timeout=0.3).acquire()
        assert info.value.code == "LOCK_TIMEOUT"
        assert holder_pid in info.value.message
    finally:
        proc.kill()
        proc.wait()


def test_lock_freed_immediately_when_holder_is_killed(vault):
    proc = spawn(HOLDER.format(vault=vault))
    holder_pid = int(_wait_ready(proc))
    os.kill(holder_pid, signal.SIGTERM)  # TerminateProcess on Windows: no cleanup runs
    proc.wait()
    start = time.monotonic()
    with VaultLock(vault, timeout=5):
        elapsed = time.monotonic() - start
    assert elapsed < 1.0


def test_same_process_second_lock_conflicts(vault):
    with VaultLock(vault):
        with pytest.raises(ConflictError):
            VaultLock(vault, timeout=0.1).acquire()


def test_release_keeps_lock_file_and_allows_reacquire(vault):
    lock = VaultLock(vault)
    lock.acquire()
    lock.release()
    assert (vault / ".mnemoloci" / "lock" / "lockfile.bin").exists()
    with VaultLock(vault, timeout=0.5) as again:
        again.assert_held()
