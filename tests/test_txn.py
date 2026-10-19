import os
import threading
import time

import pytest

from mnemoloci.core import atomic, txn
from mnemoloci.core.errors import ConflictError, MnemoError, ValidationError
from mnemoloci.core.hashing import sha256_bytes

from conftest import snapshot, spawn


def bundle(op_id, *writes):
    return {"schema": txn.BUNDLE_SCHEMA, "operation_id": op_id, "operation": "test", "writes": list(writes)}


def create(path, text):
    return {"path": path, "action": "create", "content": text}


def replace(path, text, old):
    return {"path": path, "action": "replace", "content": text, "expected_sha256": sha256_bytes(old)}


def run(vault, b):
    plan = txn.prepare(vault, b)
    return txn.apply(vault, b, plan.approval_sha256)


def test_create_and_replace(vault):
    run(vault, bundle("op1", create("wiki/a.md", "uno"), create("wiki/sub/b.md", "due")))
    assert (vault / "wiki/a.md").read_text() == "uno"
    result = run(vault, bundle("op2", replace("wiki/a.md", "uno bis", b"uno")))
    assert result["changed_paths"] == ["wiki/a.md"]
    assert (vault / "wiki/a.md").read_text() == "uno bis"


def test_plan_changed_when_vault_edited_after_review(vault):
    run(vault, bundle("op1", create("a.md", "v1")))
    b = bundle("op2", replace("a.md", "v2", b"v1"))
    plan = txn.prepare(vault, b)
    (vault / "a.md").write_text("edited in obsidian")
    with pytest.raises(ConflictError) as info:
        txn.apply(vault, b, plan.approval_sha256)
    assert info.value.code == "STALE_TARGET"
    assert (vault / "a.md").read_text() == "edited in obsidian"


def test_wrong_approval_hash_refused(vault):
    b = bundle("op1", create("a.md", "x"))
    with pytest.raises(ConflictError) as info:
        txn.apply(vault, b, "0" * 64)
    assert info.value.code == "PLAN_CHANGED"
    assert not (vault / "a.md").exists()


def test_create_refuses_existing_file(vault):
    (vault / "a.md").write_text("mine")
    with pytest.raises(ConflictError) as info:
        txn.prepare(vault, bundle("op1", create("a.md", "x")))
    assert info.value.code == "TARGET_EXISTS"


@pytest.mark.parametrize(
    "bad",
    ["../escape.md", "C:/x.md", "/abs.md", "a/../b.md", "CON.md", "nul", "a.md:stream",
     "dir./x.md", "a\\b.md", ".mnemoloci/x.md", ".obsidian/app.json", ""],
)
def test_unsafe_paths_rejected(vault, bad):
    with pytest.raises(ValidationError):
        txn.prepare(vault, bundle("op1", create(bad, "x")))


def test_case_insensitive_duplicates_rejected(vault):
    with pytest.raises(ValidationError) as info:
        txn.prepare(vault, bundle("op1", create("Note.md", "a"), create("note.md", "b")))
    assert info.value.code == "DUPLICATE_PATH"


def test_size_limit_checked_before_anything_else(vault, monkeypatch):
    monkeypatch.setattr(txn, "MAX_FILE_BYTES", 10)
    with pytest.raises(ValidationError) as info:
        txn.prepare(vault, bundle("op1", create("../bad-and-big.md", "x" * 11)))
    assert info.value.code == "FILE_TOO_LARGE"


def test_failure_mid_apply_rolls_back_in_process(vault, monkeypatch):
    run(vault, bundle("op1", create("a.md", "A1"), create("b.md", "B1")))
    before = snapshot(vault)
    real = txn.atomic_write
    calls = {"n": 0}

    def flaky(path, data):
        if path.parent == vault:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk exploded")
        real(path, data)

    monkeypatch.setattr(txn, "atomic_write", flaky)
    b = bundle("op2", replace("a.md", "A2", b"A1"), replace("b.md", "B2", b"B1"), create("c.md", "C"))
    plan = txn.prepare(vault, b)
    with pytest.raises(MnemoError) as info:
        txn.apply(vault, b, plan.approval_sha256)
    assert info.value.code == "APPLY_FAILED"
    assert snapshot(vault) == before


CRASHER = """
import os
from mnemoloci.core import txn
real = txn.atomic_write
count = {{"n": 0}}
def crashing(path, data):
    real(path, data)
    if path.parent.name == "notes":
        count["n"] += 1
        if count["n"] == {crash_after}:
            os._exit(9)
txn.atomic_write = crashing
b = {bundle!r}
plan = txn.prepare(r"{vault}", b)
txn.apply(r"{vault}", b, plan.approval_sha256)
"""


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_hard_crash_then_recover_restores_vault_byte_for_byte(vault, crash_after):
    run(vault, bundle("seed", create("notes/a.md", "A1\n"), create("notes/b.md", "B1\n")))
    before = snapshot(vault)
    b = bundle(
        "crashy",
        replace("notes/a.md", "A2\n", b"A1\n"),
        replace("notes/b.md", "B2\n", b"B1\n"),
        create("notes/c.md", "C\n"),
    )
    proc = spawn(CRASHER.format(crash_after=crash_after, bundle=b, vault=vault))
    proc.wait(timeout=60)
    assert proc.returncode == 9, proc.stderr.read()
    assert txn.incomplete_transactions(vault) == ["crashy"]

    report = txn.recover(vault)
    assert txn.incomplete_transactions(vault) == []
    txdir = txn.transactions_dir(vault) / "crashy"
    if crash_after == 3:  # every write had landed: finished, not undone
        assert report[0]["state"] == "complete"
        assert (vault / "notes/c.md").read_text() == "C\n" and (vault / "notes/a.md").read_text() == "A2\n"
        return
    assert report[0]["state"] == "rolled-back"
    assert snapshot(vault) == before
    # what the interrupted apply had written is parked in the transaction folder, never deleted
    parked = sorted(p.read_text() for p in (txdir / "rolled-back").iterdir())
    assert parked == ["A2\n", "B2\n"][:crash_after]


def test_apply_settles_an_interrupted_transaction_itself(vault):
    run(vault, bundle("seed", create("notes/a.md", "A1\n")))
    b = bundle("crashy", replace("notes/a.md", "A2\n", b"A1\n"), create("notes/c.md", "C\n"))
    proc = spawn(CRASHER.format(crash_after=1, bundle=b, vault=vault))
    proc.wait(timeout=60)
    assert txn.incomplete_transactions(vault) == ["crashy"]
    run(vault, bundle("next", create("notes/d.md", "D")))  # no "run recover first" dead end
    assert txn.incomplete_transactions(vault) == []
    assert (vault / "notes/a.md").read_text() == "A1\n" and (vault / "notes/d.md").read_text() == "D"


def test_recover_never_overwrites_foreign_edits(vault):
    run(vault, bundle("seed", create("notes/a.md", "A1")))
    b = bundle("crashy", replace("notes/a.md", "A2", b"A1"), create("notes/c.md", "C"))
    proc = spawn(CRASHER.format(crash_after=1, bundle=b, vault=vault))
    proc.wait(timeout=60)
    (vault / "notes/a.md").write_text("user edit after crash")
    report = txn.recover(vault)
    assert report[0]["state"] == "rollback-conflict"
    assert (vault / "notes/a.md").read_text() == "user edit after crash"


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks a replace only on Windows")
def test_atomic_write_retries_while_file_is_held_open(tmp_path):
    target = tmp_path / "busy.md"
    target.write_text("old")
    handle = open(target, "rb")
    threading.Timer(0.4, handle.close).start()
    start = time.monotonic()
    atomic.atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    assert time.monotonic() - start >= 0.3


def test_atomic_write_gives_up_with_file_busy(tmp_path, monkeypatch):
    import os

    if os.name != "nt":
        pytest.skip("Windows sharing semantics")
    monkeypatch.setattr(atomic, "REPLACE_RETRY_SECONDS", 0.2)
    target = tmp_path / "busy.md"
    target.write_text("old")
    with open(target, "rb"):
        with pytest.raises(MnemoError) as info:
            atomic.atomic_write(target, b"new")
    assert info.value.code == "FILE_BUSY"
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]
