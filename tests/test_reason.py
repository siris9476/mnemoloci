import json
import subprocess

import pytest

from mnemoloci import cli, reason, vault as vault_mod
from mnemoloci.core.errors import MnemoError, ValidationError


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


class Fake:
    """Stands in for subprocess.run and records how `claude` was called."""

    def __init__(self, payload=None, returncode=0, raises=None):
        self.payload = payload if payload is not None else {"result": "Risposta [[wiki/x.md]]", "total_cost_usd": 0.042,
                                                            "num_turns": 3, "session_id": "s1", "is_error": False}
        self.returncode, self.raises, self.calls = returncode, raises, []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, json.dumps(self.payload), "")


def test_the_run_is_read_only_capped_and_without_mcp(v, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(reason.subprocess, "run", fake)
    out = reason.ask(v, "Che cosa ho deciso sul backup?")
    cmd, kwargs = fake.calls[0]
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--strict-mcp-config" in cmd and "--max-budget-usd" in cmd and cmd[cmd.index("--max-budget-usd") + 1] == "0.5"
    assert json.loads(open(cmd[cmd.index("--mcp-config") + 1], encoding="utf-8").read()) == {"mcpServers": {}}
    assert kwargs["cwd"] == str(v) and kwargs["input"] == "Che cosa ho deciso sul backup?"
    assert "Che cosa ho deciso sul backup?" not in cmd
    assert out["answer"].startswith("Risposta") and out["cost_usd"] == 0.042 and out["turns"] == 3


def test_every_run_is_audited(v, monkeypatch):
    monkeypatch.setattr(reason.subprocess, "run", Fake())
    reason.ask(v, "Prima domanda?")
    reason.ask(v, "Seconda domanda?", model="haiku")
    runs = reason.history(v)
    assert [r["question"] for r in runs["runs"]] == ["Seconda domanda?", "Prima domanda?"]
    assert runs["total_cost_usd"] == 0.084 and runs["runs"][0]["model"] == "haiku" and runs["runs"][0]["ok"] is True


def test_budget_comes_from_config_and_is_bounded(v, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(reason.subprocess, "run", fake)
    reason.ask(v, "Domanda?", budget=1.25)
    assert fake.calls[0][0][fake.calls[0][0].index("--max-budget-usd") + 1] == "1.25"
    for bad in (0, -1, 500):
        with pytest.raises(ValidationError) as exc:
            reason.ask(v, "Domanda?", budget=bad)
        assert exc.value.code == "BAD_BUDGET"


def test_extra_roots_must_be_folders(v, tmp_path, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(reason.subprocess, "run", fake)
    reason.ask(v, "Domanda?", roots=[str(tmp_path)])
    assert "--add-dir" in fake.calls[0][0]
    with pytest.raises(ValidationError) as exc:
        reason.ask(v, "Domanda?", roots=[str(tmp_path / "manca")])
    assert exc.value.code == "BAD_ROOT"


def test_empty_and_huge_questions_are_refused(v):
    with pytest.raises(ValidationError):
        reason.ask(v, "   ")
    with pytest.raises(ValidationError):
        reason.ask(v, "x" * (reason.MAX_QUESTION + 1))


def test_failures_are_logged_and_reported(v, monkeypatch):
    monkeypatch.setattr(reason.subprocess, "run", Fake({"result": "budget exceeded", "is_error": True}, returncode=1))
    with pytest.raises(MnemoError) as exc:
        reason.ask(v, "Domanda?")
    assert exc.value.code == "CLAUDE_CODE_FAILED"
    [run] = reason.history(v)["runs"]
    assert run["ok"] is False and "budget" in run["error"]

    monkeypatch.setattr(reason.subprocess, "run", Fake(raises=subprocess.TimeoutExpired("claude", 1)))
    with pytest.raises(MnemoError) as exc:
        reason.ask(v, "Altra?")
    assert exc.value.code == "CLAUDE_CODE_TIMEOUT" and reason.history(v)["runs"][0]["error"] == "timeout"

    monkeypatch.setattr(reason.subprocess, "run", Fake(raises=FileNotFoundError()))
    with pytest.raises(MnemoError) as exc:
        reason.ask(v, "Ancora?")
    assert exc.value.code == "CLAUDE_CODE_NOT_FOUND"


def test_cli(v, monkeypatch, capsys):
    monkeypatch.setattr(reason.subprocess, "run", Fake())
    assert cli.main(["--vault", str(v), "reason", "Domanda?", "--budget", "0.2"]) == 0
    assert json.loads(capsys.readouterr().out)["answer"].startswith("Risposta")
    assert cli.main(["--vault", str(v), "reason-history"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["question"] == "Domanda?"
