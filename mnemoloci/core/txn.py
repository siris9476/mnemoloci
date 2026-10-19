"""Reviewed, journaled, recoverable vault writes.

Flow: prepare(bundle) -> Plan with approval_sha256 -> a reviewer approves that
hash -> apply(bundle, approved_sha256) re-prepares under the vault lock and
refuses if anything changed. Every original is backed up before it is touched;
an interrupted apply is finished by recover() when every write had already landed,
otherwise rolled back. Nothing is ever deleted: a file the interrupted transaction
created is moved into the transaction folder (rolled-back/), not removed.
"""

import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .atomic import atomic_json, atomic_write
from .errors import ConflictError, MnemoError, ValidationError
from .hashing import canonical_hash, sha256_bytes, sha256_file
from .lock import ENGINE_DIR, VaultLock
from .paths import fold, is_link_like, resolve_in_vault, validate_relpath

BUNDLE_SCHEMA = "mnemoloci.txn.v1"
JOURNAL_SCHEMA = "mnemoloci.journal.v1"
APPROVAL_SCHEMA = "mnemoloci.plan-approval.v1"

MAX_WRITES = 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
RESERVED_TOP = {ENGINE_DIR, ".obsidian", ".git", ".trash"}

_OP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_OPERATION = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")

INCOMPLETE_STATES = {"prepared", "applying"}


@dataclass(frozen=True)
class PreparedWrite:
    path: str
    action: str
    original_sha256: str | None
    new_sha256: str
    content: bytes = field(repr=False)


@dataclass
class Plan:
    operation_id: str
    operation: str
    bundle_sha256: str
    approval_sha256: str
    writes: list[PreparedWrite]

    def summary(self) -> dict[str, Any]:
        return {
            "schema": "mnemoloci.plan.v1",
            "operation_id": self.operation_id,
            "operation": self.operation,
            "approval_sha256": self.approval_sha256,
            "writes": [
                {
                    "path": w.path,
                    "action": w.action,
                    "original_sha256": w.original_sha256,
                    "new_sha256": w.new_sha256,
                    "bytes": len(w.content),
                }
                for w in self.writes
            ],
        }


def engine_dir(vault: Path) -> Path:
    return Path(vault) / ENGINE_DIR


def transactions_dir(vault: Path) -> Path:
    return engine_dir(vault) / "transactions"


def _vault_identity(vault: Path) -> dict[str, Any]:
    st = os.stat(vault)
    return {"device": st.st_dev, "inode": st.st_ino}


def _current_sha(target: Path) -> str | None:
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    if is_link_like(st) or not os.path.isfile(target):
        raise ValidationError("NOT_REGULAR_FILE", f"target is not a regular file: {target}")
    return sha256_file(target)


def _check_limits(writes: list[dict[str, Any]], base_dir: Path | None) -> None:
    if not writes:
        raise ValidationError("EMPTY_BUNDLE", "bundle has no writes")
    if len(writes) > MAX_WRITES:
        raise ValidationError("TOO_MANY_WRITES", f"{len(writes)} > {MAX_WRITES}")
    total = 0
    for entry in writes:
        if not isinstance(entry, dict):
            raise ValidationError("BAD_BUNDLE", "each write must be an object")
        if "content" in entry:
            if not isinstance(entry["content"], str):
                raise ValidationError("BAD_BUNDLE", "content must be a string")
            size = len(entry["content"].encode("utf-8"))
        elif "content_file" in entry:
            size = os.stat(_content_file_path(entry["content_file"], base_dir)).st_size
        else:
            raise ValidationError("BAD_BUNDLE", "write needs content or content_file")
        if size > MAX_FILE_BYTES:
            raise ValidationError("FILE_TOO_LARGE", f"{entry.get('path')}: {size} bytes")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValidationError("BUNDLE_TOO_LARGE", f"total {total} bytes")


def _content_file_path(raw: Any, base_dir: Path | None) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValidationError("BAD_BUNDLE", "content_file must be a path string")
    path = Path(raw)
    if not path.is_absolute():
        if base_dir is None:
            raise ValidationError("BAD_BUNDLE", "relative content_file needs a bundle directory")
        path = base_dir / path
    return path


def _content_of(entry: dict[str, Any], base_dir: Path | None) -> bytes:
    if "content" in entry:
        if "content_file" in entry:
            raise ValidationError("BAD_BUNDLE", "use content or content_file, not both")
        return entry["content"].encode("utf-8")
    data = _content_file_path(entry["content_file"], base_dir).read_bytes()
    declared = entry.get("sha256")
    if not isinstance(declared, str) or not hmac.compare_digest(declared, sha256_bytes(data)):
        raise ValidationError("CONTENT_HASH_MISMATCH", f"content_file hash mismatch for {entry.get('path')}")
    return data


def prepare(vault: Path, bundle: dict[str, Any], *, base_dir: Path | None = None) -> Plan:
    vault = Path(vault)
    if not isinstance(bundle, dict) or bundle.get("schema") != BUNDLE_SCHEMA:
        raise ValidationError("BAD_BUNDLE", f"bundle schema must be {BUNDLE_SCHEMA}")
    op_id, operation = bundle.get("operation_id"), bundle.get("operation")
    if not isinstance(op_id, str) or not _OP_ID.match(op_id):
        raise ValidationError("BAD_OPERATION_ID", f"invalid operation_id {op_id!r}")
    if not isinstance(operation, str) or not _OPERATION.match(operation):
        raise ValidationError("BAD_OPERATION", f"invalid operation {operation!r}")
    writes_raw = bundle.get("writes")
    if not isinstance(writes_raw, list):
        raise ValidationError("BAD_BUNDLE", "writes must be a list")
    _check_limits(writes_raw, base_dir)

    seen: set[str] = set()
    prepared: list[PreparedWrite] = []
    for entry in writes_raw:
        rel = validate_relpath(entry.get("path"))
        if rel.parts[0].casefold() in {r.casefold() for r in RESERVED_TOP}:
            raise ValidationError("RESERVED_PATH", f"{rel} is engine-owned")
        key = fold(rel)
        if key in seen:
            raise ValidationError("DUPLICATE_PATH", f"{rel} appears twice (case-insensitive)")
        seen.add(key)
        action = entry.get("action")
        if action not in ("create", "replace"):
            raise ValidationError("BAD_ACTION", f"{rel}: action must be create or replace")
        target = resolve_in_vault(vault, rel)
        current = _current_sha(target)
        expected = entry.get("expected_sha256")
        if action == "create":
            if current is not None:
                raise ConflictError("TARGET_EXISTS", f"{rel} already exists")
        else:
            if not isinstance(expected, str) or not _SHA.match(expected):
                raise ValidationError("MISSING_EXPECTED_HASH", f"{rel}: replace needs expected_sha256")
            if current is None or not hmac.compare_digest(current, expected):
                raise ConflictError("STALE_TARGET", f"{rel} changed since it was read")
        content = _content_of(entry, base_dir)
        prepared.append(
            PreparedWrite(str(rel), action, current, sha256_bytes(content), content)
        )

    bundle_hash = canonical_hash(bundle)
    approval = canonical_hash(
        {
            "schema": APPROVAL_SCHEMA,
            "vault": str(vault.resolve()),
            "vault_identity": _vault_identity(vault),
            "bundle_sha256": bundle_hash,
            "writes": [
                {"path": w.path, "action": w.action, "original_sha256": w.original_sha256, "new_sha256": w.new_sha256}
                for w in prepared
            ],
        }
    )
    return Plan(op_id, operation, bundle_hash, approval, prepared)


def _read_journal(txdir: Path) -> dict[str, Any] | None:
    try:
        journal = json.loads((txdir / "journal.json").read_text("utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(journal, dict) or journal.get("schema") != JOURNAL_SCHEMA:
        raise MnemoError("CORRUPT_JOURNAL", f"unreadable journal in {txdir}")
    return journal


def incomplete_transactions(vault: Path) -> list[str]:
    root = transactions_dir(vault)
    if not root.is_dir():
        return []
    found = []
    for txdir in sorted(p for p in root.iterdir() if p.is_dir()):
        journal = _read_journal(txdir)
        if journal is not None and journal.get("state") in INCOMPLETE_STATES:
            found.append(txdir.name)
    return found


@contextmanager
def _locked(vault: Path, lock: VaultLock | None, timeout: float) -> Iterator[VaultLock]:
    """Reuse a lock the caller already holds, otherwise take (and release) our own."""
    if lock is not None:
        lock.assert_held()
        if Path(lock.vault).resolve() != vault.resolve():
            raise MnemoError("LOCK_MISMATCH", "lock belongs to a different vault")
        yield lock
        return
    with VaultLock(vault, timeout=timeout) as own:
        yield own


def _rollback(vault: Path, txdir: Path, journal: dict[str, Any]) -> dict[str, Any]:
    conflicts, restored = [], []
    for index, write in reversed(list(enumerate(journal["writes"]))):
        target = resolve_in_vault(vault, PurePosixPath(write["path"]))
        current = _current_sha(target)
        if current == write["original_sha256"]:
            continue
        if current == write["new_sha256"]:
            # the new content is kept, never deleted: it may be the only copy of what the user asked to save
            parked = txdir / "rolled-back" / f"{index:04d}{target.suffix}"
            parked.parent.mkdir(parents=True, exist_ok=True)
            write["parked"] = parked.relative_to(txdir).as_posix()
            if write["action"] == "create":
                os.replace(target, parked)
            else:
                backup = (txdir / write["backup"]).read_bytes()
                if sha256_bytes(backup) != write["original_sha256"]:
                    conflicts.append({"path": write["path"], "reason": "backup corrupt"})
                    continue
                atomic_write(parked, target.read_bytes())
                atomic_write(target, backup)
            restored.append(write["path"])
        else:
            conflicts.append({"path": write["path"], "reason": "modified by someone else; left untouched"})
    journal["state"] = "rollback-conflict" if conflicts else "rolled-back"
    journal["rollback"] = {"restored": restored, "conflicts": conflicts, "at": time.time()}
    atomic_json(txdir / "journal.json", journal)
    return journal


def apply(
    vault: Path,
    bundle: dict[str, Any],
    approved_sha256: str,
    *,
    base_dir: Path | None = None,
    lock_timeout: float = 10.0,
    lock: VaultLock | None = None,
) -> dict[str, Any]:
    vault = Path(vault)
    with _locked(vault, lock, lock_timeout) as lock:
        if incomplete_transactions(vault):
            # an earlier apply was killed (hook timeout, crash): settle it now instead of refusing
            # every later write; recover only touches files still exactly as that apply left them
            recover(vault, lock=lock)
        plan = prepare(vault, bundle, base_dir=base_dir)
        if not hmac.compare_digest(plan.approval_sha256, str(approved_sha256)):
            raise ConflictError("PLAN_CHANGED", "vault or bundle changed since the plan was approved")
        txdir = transactions_dir(vault) / plan.operation_id
        if txdir.exists():
            raise ConflictError("DUPLICATE_OPERATION", f"operation {plan.operation_id} already exists")
        txdir.mkdir(parents=True)

        journal: dict[str, Any] = {
            "schema": JOURNAL_SCHEMA,
            "operation_id": plan.operation_id,
            "operation": plan.operation,
            "approval_sha256": plan.approval_sha256,
            "bundle_sha256": plan.bundle_sha256,
            "state": "prepared",
            "started_epoch": time.time(),
            "writes": [
                {
                    "path": w.path,
                    "action": w.action,
                    "original_sha256": w.original_sha256,
                    "new_sha256": w.new_sha256,
                    "backup": f"backups/{i:04d}.orig" if w.action == "replace" else None,
                }
                for i, w in enumerate(plan.writes)
            ],
            "applied": [],
        }
        atomic_json(txdir / "journal.json", journal)
        for w, entry in zip(plan.writes, journal["writes"]):
            if entry["backup"]:
                original = resolve_in_vault(vault, PurePosixPath(w.path)).read_bytes()
                if sha256_bytes(original) != w.original_sha256:
                    journal["state"] = "aborted"
                    atomic_json(txdir / "journal.json", journal)
                    raise ConflictError("STALE_TARGET", f"{w.path} changed during apply")
                atomic_write(txdir / entry["backup"], original)

        journal["state"] = "applying"
        atomic_json(txdir / "journal.json", journal)
        try:
            for w in plan.writes:
                lock.assert_held()
                target = resolve_in_vault(vault, PurePosixPath(w.path))
                if _current_sha(target) != w.original_sha256:
                    raise ConflictError("STALE_TARGET", f"{w.path} changed during apply")
                atomic_write(target, w.content)
                journal["applied"].append(w.path)
                atomic_json(txdir / "journal.json", journal)
        except BaseException as exc:
            _rollback(vault, txdir, journal)
            if isinstance(exc, MnemoError):
                raise
            raise MnemoError("APPLY_FAILED", f"rolled back after: {exc!r}") from exc

        journal["state"] = "complete"
        journal["finished_epoch"] = time.time()
        atomic_json(txdir / "journal.json", journal)
        result = {
            "operation_id": plan.operation_id,
            "state": "complete",
            "changed_paths": [w.path for w in plan.writes],
            "hashes": {w.path: w.new_sha256 for w in plan.writes},
        }
        atomic_json(txdir / "result.json", result)
        return result


def _all_landed(vault: Path, journal: dict[str, Any]) -> bool:
    try:
        return all(_current_sha(resolve_in_vault(vault, PurePosixPath(w["path"]))) == w["new_sha256"]
                   for w in journal["writes"])
    except MnemoError:
        return False


def recover(
    vault: Path, *, lock_timeout: float = 10.0, lock: VaultLock | None = None
) -> list[dict[str, Any]]:
    vault = Path(vault)
    report = []
    with _locked(vault, lock, lock_timeout):
        for op_id in incomplete_transactions(vault):
            txdir = transactions_dir(vault) / op_id
            journal = _read_journal(txdir)
            if journal["state"] == "prepared":
                journal["state"] = "aborted"
                atomic_json(txdir / "journal.json", journal)
            elif _all_landed(vault, journal):
                # killed after the last write but before "complete": finish it (roll forward)
                journal["state"], journal["finished_epoch"], journal["recovered"] = "complete", time.time(), "rolled-forward"
                atomic_json(txdir / "journal.json", journal)
                atomic_json(txdir / "result.json", {
                    "operation_id": op_id, "state": "complete",
                    "changed_paths": [w["path"] for w in journal["writes"]],
                    "hashes": {w["path"]: w["new_sha256"] for w in journal["writes"]}})
            else:
                journal = _rollback(vault, txdir, journal)
            report.append({"operation_id": op_id, "state": journal["state"], "rollback": journal.get("rollback")})
    return report
