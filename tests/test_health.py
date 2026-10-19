"""doctor --fix, the recorded Python, the plugin launcher and the app's health routes."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mnemoloci import backup, doctor, hooks, index, remote, runtime
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import MnemoError

LAUNCHER = Path(__file__).resolve().parents[1] / "plugin" / "bin" / "launch.py"


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _check(report, name):
    return next(c for c in report["checks"] if c["check"] == name)


def test_python_is_recorded_and_checked(v):
    assert runtime.recorded_python() is None
    assert _check(doctor.run(v), "plugin-python").get("repair") == "python"
    out = runtime.record_python()
    assert Path(out["file"]).read_text("utf-8").strip() == runtime.console_python()
    assert _check(doctor.run(v), "plugin-python")["status"] == "ok"
    runtime.python_file().write_text(r"C:\nowhere\python.exe", "utf-8")
    assert _check(doctor.run(v), "plugin-python")["status"] == "fail"
    with pytest.raises(MnemoError):
        runtime.record_python(r"C:\nowhere\python.exe")


def test_doctor_fix_repairs_what_it_safely_can(v):
    # an interrupted write, a handoff waiting in the queue, a missing index, no recorded Python
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "crashy", "operation": "test",
         "writes": [{"path": "notes/a.md", "action": "create", "content": "A"}]}
    plan = txn.prepare(v, b)
    txdir = txn.transactions_dir(v) / "crashy"
    txdir.mkdir(parents=True)
    (txdir / "journal.json").write_text(json.dumps({
        "schema": txn.JOURNAL_SCHEMA, "operation_id": "crashy", "operation": "test", "state": "applying",
        "writes": [{"path": "notes/a.md", "action": "create", "original_sha256": None,
                    "new_sha256": plan.writes[0].new_sha256, "backup": None}], "applied": []}), "utf-8")
    hooks._queue_handoff(v, {"title": "Fine sessione: prova", "body": "fatto", "project": r"C:\Users\you\Work\Demo",
                             "source": "hook:session-end", "tag": "session:x", "supersede": False})
    index.db_path(v).unlink(missing_ok=True)

    report = doctor.run(v)
    assert {"transactions", "handoffs", "search-index", "python"} <= set(report["repairable"])
    out = doctor.fix(v)
    assert all(r["ok"] for r in out["repairs"]), out["repairs"]
    after = doctor.run(v)
    assert not {"transactions", "handoffs", "search-index", "python"} & set(after["repairable"])
    assert txn.incomplete_transactions(v) == [] and index.ready(v)
    assert doctor.fix(v, ["search-index"])["nothing_to_repair"]
    with pytest.raises(MnemoError):
        doctor.fix(v, ["format-disk"])


def test_stale_git_lock_is_moved_aside_not_deleted(v, tmp_path):
    lock = v / ".git" / "index.lock"
    lock.parent.mkdir()
    lock.write_text("", "utf-8")
    old = time.time() - 3600
    import os

    os.utime(lock, (old, old))
    assert backup.stale_git_lock(v) == lock
    assert _check(doctor.run(v), "git-lock")["repair"] == "git-lock"
    doctor.fix(v, ["git-lock"])
    assert not lock.exists() and list(lock.parent.glob("index.lock.stale-*"))


def test_doctor_flags_compile_blocks_left_without_maintenance(v):
    import os

    def txdir(name: str, mtime: float) -> None:
        d = txn.transactions_dir(v) / name
        d.mkdir(parents=True)
        os.utime(d, (mtime, mtime))

    assert _check(doctor.run(v), "maintenance")["status"] == "ok"  # nothing compiled yet: nothing to flag

    t0 = time.time() - 300
    txdir("compile-r1-1", t0)
    never = _check(doctor.run(v), "maintenance")
    assert never["status"] == "warn" and "mai eseguita" in never["detail"]  # routine never run at all

    txdir("hublinks-1", t0 + 60)  # one of the three ran (and wrote something) after that block
    assert _check(doctor.run(v), "maintenance")["status"] == "ok"

    txdir("compile-r2-1", t0 + 120)  # a second block lands, maintenance not re-run since
    warn = _check(doctor.run(v), "maintenance")
    assert warn["status"] == "warn" and "1 blocco" in warn["detail"] and warn["fix"]

    txdir("fixlinks-1", t0 + 180)  # a different one of the three catches up: still counts
    assert _check(doctor.run(v), "maintenance")["status"] == "ok"


def test_launcher_uses_the_recorded_python(v, tmp_path):
    runtime.record_python()
    env = {**__import__("os").environ}
    out = subprocess.run([sys.executable, "-P", str(LAUNCHER), "--vault", str(v), "index", "rebuild"],
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0 and json.loads(out.stdout)["ok"], out.stderr
    # a recorded Python that no longer exists is skipped: this interpreter has the engine
    runtime.python_file().write_text(r"C:\nowhere\python.exe", "utf-8")
    out = subprocess.run([sys.executable, "-P", str(LAUNCHER), "--vault", str(v), "index", "rebuild"],
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr


def test_plugin_hooks_and_mcp_go_through_the_launcher():
    plugin = LAUNCHER.parents[1]
    hooks_json = json.loads((plugin / "hooks" / "hooks.json").read_text("utf-8"))
    commands = [h["command"] for group in hooks_json["hooks"].values() for entry in group for h in entry["hooks"]]
    # one launcher name for every system: an sh script (Linux, macOS, Git Bash on Windows)
    # and, for Windows programs that start it without a shell, its .cmd twin
    assert commands and all(c.startswith('"${CLAUDE_PLUGIN_ROOT}/bin/mnemoloci-run" hook ') for c in commands)
    mcp = json.loads((plugin / ".mcp.json").read_text("utf-8"))["mcpServers"]["mnemoloci"]
    assert mcp == {"command": "${CLAUDE_PLUGIN_ROOT}/bin/mnemoloci-run", "args": ["mcp"]}
    sh = (plugin / "bin" / "mnemoloci-run").read_bytes()
    assert sh.startswith(b"#!/bin/sh\n") and b"\r" not in sh  # CRLF would break it on Linux
    assert (plugin / "bin" / "mnemoloci-run.cmd").read_bytes().count(b"\r\n") > 3
    mode = subprocess.run(["git", "-C", str(plugin), "ls-files", "-s", "bin/mnemoloci-run"],
                          capture_output=True, text=True).stdout
    assert not mode or mode.startswith("100755"), mode  # executable once committed


def test_session_start_rebuilds_a_missing_index_in_the_background(v, monkeypatch):
    started = []
    monkeypatch.setattr(runtime, "spawn_detached", lambda args: started.append(args))
    index.db_path(v).unlink(missing_ok=True)
    hooks.session_start(v, {"cwd": r"C:\Users\you\Work\Demo"})
    hooks.session_start(v, {"cwd": r"C:\Users\you\Work\Demo"})  # a second session soon after does not start another one
    assert started == [["--vault", str(v), "index", "rebuild"]]


def test_app_health_routes_are_owner_only(v):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api

    c = TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=True), base_url="http://127.0.0.1:8790")
    owner = {"Authorization": "Bearer launch"}
    report = c.get("/api/doctor", headers=owner).json()
    assert report["ok"] and "search-index" in {x["check"] for x in report["checks"]}
    assert c.post("/api/doctor/fix", json={"repair": "search-index"}, headers=owner).json()["ok"]

    host = "mypc.tail1234.ts.net"
    remote.allow_hosts(v, [host])
    code = c.post("/api/remote/pair", json={}, headers=owner).json()["code"]
    token = c.post("/api/remote/claim", json={"code": code, "name": "p"}).json()["token"]
    phone = {"Authorization": f"Bearer {token}", "Host": host}
    assert c.get("/api/doctor", headers=phone).json()["code"] == "OWNER_ONLY"
    dev = remote.devices(v)["devices"][0]["id"]
    assert c.post(f"/api/remote/scope/{dev}", json={"scope": "full"}, headers=phone).json()["code"] == "OWNER_ONLY"
    assert c.post(f"/api/remote/scope/{dev}", json={"scope": "full"}, headers=owner).json()["scope"] == "full"
    assert remote.devices(v)["devices"][0]["scope"] == "full"


def test_repair_everything_leaves_slow_repairs_to_an_explicit_request(v, monkeypatch):
    done = []
    monkeypatch.setattr(doctor, "_run", lambda vault: {"repairable": ["embeddings", "search-index"], "checks": [],
                                                      "healthy": True, "counts": {}})
    monkeypatch.setattr(doctor, "_repair", lambda vault, name: done.append(name))
    doctor.fix(v)
    assert done == ["search-index"]
    doctor.fix(v, ["embeddings"])
    assert done == ["search-index", "embeddings"]


def test_doctor_speaks_the_interface_language(v):
    it = {c["check"]: c for c in doctor.run(v)["checks"]}
    en = {c["check"]: c for c in doctor.run(v, lang="en")["checks"]}
    assert it["lock"]["detail"] == "libero" and en["lock"]["detail"] == "free"
    assert en["search-index"].get("repair_label", "").startswith("rebuild") or en["search-index"]["status"] == "ok"
    assert doctor.run(v)["checks"][0]["detail"].split()[1] == "in"  # the language never leaks between calls
