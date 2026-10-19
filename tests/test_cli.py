import json

from mnemoloci.cli import main
from mnemoloci.core.hashing import sha256_bytes


def _run(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_plan_apply_roundtrip_with_content_file(vault, tmp_path, capsys):
    staged = tmp_path / "staged.md"
    staged.write_bytes(b"# Titolo\n")
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps({
        "schema": "mnemoloci.txn.v1",
        "operation_id": "cli-1",
        "operation": "ingest",
        "writes": [{"path": "wiki/t.md", "action": "create", "content_file": "staged.md",
                    "sha256": sha256_bytes(b"# Titolo\n")}],
    }))
    code, plan = _run(capsys, "--vault", str(vault), "txn", "plan", str(bundle_file))
    assert code == 0 and plan["writes"][0]["path"] == "wiki/t.md"
    code, result = _run(capsys, "--vault", str(vault), "txn", "apply", str(bundle_file),
                        "--approved", plan["approval_sha256"])
    assert code == 0 and result["state"] == "complete"
    assert (vault / "wiki/t.md").read_bytes() == b"# Titolo\n"


def test_every_command_dispatches_through_the_cli(vault, capsys):
    """Positional args named like internal attributes (e.g. `key`) must not break dispatch."""
    v = str(vault)
    assert _run(capsys, "--vault", v, "init")[0] == 0
    code, out = _run(capsys, "--vault", v, "profile", "set", "ruolo", "sviluppatore", "--confidence", "0.8")
    assert code == 0 and out["fact"]["value"] == "sviluppatore"
    assert _run(capsys, "--vault", v, "profile", "show")[1]["facts"][0]["key"] == "ruolo"
    code, out = _run(capsys, "--vault", v, "goals", "add", "Finire la fase 2", "--area", "progetti")
    assert code == 0 and out["goal"]["id"] == "g1"
    assert _run(capsys, "--vault", v, "goals", "update", "g1", "--status", "paused")[1]["goal"]["status"] == "paused"
    assert _run(capsys, "--vault", v, "goals", "list")[1]["goals"][0]["id"] == "g1"
    code, out = _run(capsys, "--vault", v, "memory", "add", "lesson", "--title", "Testare la CLI", "--text", "Sempre.",
                     "--project", "C:\\Users\\you\\X")
    assert code == 0
    assert _run(capsys, "--vault", v, "memory", "list", "--project", "c:\\users\\you\\x")[1]["memories"][0]["id"] == out["id"]
    assert _run(capsys, "--vault", v, "memory", "show", out["id"])[0] == 0
    for argv in (["lint"], ["review", "list"], ["compile", "status"], ["scan"], ["index", "rebuild"],
                 ["query", "testare"], ["source", "list"], ["txn", "status"], ["sessions", "import", "--dry-run",
                 "--claude-dir", v]):
        code, out = _run(capsys, "--vault", v, *argv)
        assert code == 0, (argv, out)


def test_errors_are_json_with_code(vault, tmp_path, capsys):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps({"schema": "nope"}))
    code, out = _run(capsys, "--vault", str(vault), "txn", "plan", str(bundle_file))
    assert code == 2 and out == {"ok": False, "code": "BAD_BUNDLE", "message": out["message"]}
