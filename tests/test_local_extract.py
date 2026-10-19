import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from mnemoloci import compile as compiler
from mnemoloci import local_extract, review, sources, vault as vault_mod
from mnemoloci.core.errors import MnemoError

README = """# Progetto Demo

Il motore transazionale scrive nel vault in modo sicuro.
Ogni scrittura ha un hash di approvazione.
Il lock di vault serializza i processi.
"""


@pytest.fixture
def world(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "README.md").write_text(README, "utf-8")
    v = tmp_path / "vault"
    vault_mod.init(v, "it")
    sources.add_source(v, str(project), "demo")
    return v, project


class _FakeOllamaChatResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _chat_reply(content: str):
    def fake_urlopen(req, timeout=None):
        return _FakeOllamaChatResponse(json.dumps({"message": {"role": "assistant", "content": content}}).encode("utf-8"))
    return fake_urlopen


def packet_for(plan_result, key):
    path = next(f["packet"] for f in plan_result["files"] if f["key"] == key)
    return json.loads(Path(path).read_text("utf-8"))


VALID_CONCEPT = {
    "title": "Motore transazionale", "kind": "concept", "entity_type": None, "aliases": [],
    "summary": "Scrive nel vault in modo sicuro.", "claims": [{"text": "Ogni scrittura ha un hash.", "lines": [3, 4]}],
    "confidence": 0.9, "provenance": "extracted", "related": [], "contradicts": [],
}


def test_extract_packet_writes_valid_extract_tagged_with_the_model(world, monkeypatch):
    v, _ = world
    monkeypatch.setattr(local_extract.urllib.request, "urlopen",
                        _chat_reply(json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT]})))
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, model="llama3.1")
    assert result == {"concepts": 1, "discarded": 0, "warnings": []}
    extract = json.loads(Path(packet["extract_path"]).read_text("utf-8"))
    assert extract["schema"] == compiler.EXTRACT_SCHEMA
    assert extract["extracted_by"] == "ollama:llama3.1"
    assert extract["concepts"][0]["title"] == "Motore transazionale"


def test_extract_packet_discards_invalid_concepts_but_keeps_valid_ones(world, monkeypatch):
    v, _ = world
    bad = dict(VALID_CONCEPT, title="", claims=[])  # missing title, empty claims
    monkeypatch.setattr(local_extract.urllib.request, "urlopen",
                        _chat_reply(json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT, bad]})))
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, model="llama3.1")
    assert result["concepts"] == 1 and result["discarded"] == 1
    assert result["warnings"] and "concepts[1]" in result["warnings"][0]
    extract = json.loads(Path(packet["extract_path"]).read_text("utf-8"))
    assert len(extract["concepts"]) == 1


def test_extract_packet_degrades_to_empty_extract_on_bad_model_output(world, monkeypatch):
    v, _ = world
    monkeypatch.setattr(local_extract.urllib.request, "urlopen", _chat_reply("not json at all"))
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, model="llama3.1")
    assert result["concepts"] == 0
    assert any("failed" in w for w in result["warnings"])
    extract = json.loads(Path(packet["extract_path"]).read_text("utf-8"))
    assert extract["schema"] == compiler.EXTRACT_SCHEMA and extract["concepts"] == []


def test_extract_packet_reports_unreachable_ollama_as_a_warning_not_a_crash(world, monkeypatch):
    v, _ = world

    def fail(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", fail)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, model="llama3.1")
    assert result["concepts"] == 0
    assert any("OLLAMA_UNREACHABLE" in w for w in result["warnings"])


def test_run_extract_only_stops_before_writing_pages(world, monkeypatch):
    v, _ = world
    monkeypatch.setattr(local_extract.urllib.request, "urlopen",
                        _chat_reply(json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT]})))

    out = local_extract.run(v, source="demo", mode="extract_only", extract_model="llama3.1")
    assert out["mode"] == "extract_only" and out["state"] == "writing" and out["jobs"] == 1
    assert compiler.status(v)["active"] is not None  # left for a later session to write pages + submit


def test_run_extract_and_write_always_holds_never_publishes_directly(world, monkeypatch):
    v, _ = world

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        content = body["messages"][0]["content"]
        if "Scrivi in" in content:  # the page-writing prompt
            payload = {"summary": "Il motore scrive in modo sicuro.",
                      "body": "# Motore transazionale\n\nOgni scrittura ha un hash. ^[demo/README.md#L3-L4]"}
        else:
            payload = {"source_language": "it", "concepts": [VALID_CONCEPT]}
        return _FakeOllamaChatResponse(json.dumps({"message": {"content": json.dumps(payload)}}).encode("utf-8"))

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", fake_urlopen)
    out = local_extract.run(v, source="demo", mode="extract_and_write", extract_model="llama3.1")
    assert out["live"] == 0 and out["held"] == 1  # extracted_by forces review, whatever check_page said
    assert compiler.status(v)["active"] is None  # the run completed (submit ran), unlike extract_only
    [cand] = review.list_candidates(v)
    assert "local-extraction" in cand["reasons"]


def test_write_page_local_falls_back_to_a_draft_when_the_model_output_is_unusable(world, monkeypatch):
    v, _ = world
    monkeypatch.setattr(local_extract.urllib.request, "urlopen",
                        _chat_reply(json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT]})))
    local_extract.extract_packet(v, packet_for(compiler.plan(v), "demo/README.md"), model="llama3.1")
    merged = compiler.merge(v)
    job = json.loads(Path(merged["jobs"][0]["job"]).read_text("utf-8"))

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", _chat_reply("garbage, not JSON"))
    result = local_extract.write_page_local(v, job, model="llama3.1")
    assert result["degraded"] is True
    content = Path(job["page_file"]).read_text("utf-8")
    assert "^[demo/README.md#L3-L4]" in content  # the fallback still carries the real citation


def test_run_rejects_bad_mode_and_missing_model(world):
    v, _ = world
    with pytest.raises(MnemoError) as info:
        local_extract.run(v, source="demo", mode="not-a-mode", extract_model="llama3.1")
    assert info.value.code == "BAD_MODE"
    with pytest.raises(MnemoError) as info:
        local_extract.run(v, source="demo", mode="extract_only", extract_model="")
    assert info.value.code == "NO_MODEL"
    with pytest.raises(MnemoError) as info:
        local_extract.run(v, source="demo", mode="extract_only", extract_provider="not-a-provider", extract_model="x")
    assert info.value.code == "BAD_PROVIDER"


# ---------- provider anthropic (Claude's own API, paid, needs a key) ----------

def _claude_reply(text: str):
    def fake_urlopen(req, timeout=None):
        return _FakeOllamaChatResponse(json.dumps({"content": [{"type": "text", "text": text}]}).encode("utf-8"))
    return fake_urlopen


def test_extract_packet_uses_the_claude_api_when_provider_is_anthropic(world, monkeypatch):
    v, _ = world
    vault_mod.set_api_key(v, "anthropic", "sk-ant-test")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_header("X-api-key"), req.get_header("Anthropic-version"), json.loads(req.data)))
        payload = json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT]})
        return _FakeOllamaChatResponse(json.dumps({"content": [{"type": "text", "text": payload}]}).encode("utf-8"))

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", fake_urlopen)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="anthropic", model="claude-haiku-4-5-20251001")
    assert result == {"concepts": 1, "discarded": 0, "warnings": []}
    assert calls[0][0] == "https://api.anthropic.com/v1/messages"
    assert calls[0][1] == "sk-ant-test" and calls[0][2] == "2023-06-01"
    assert calls[0][3]["model"] == "claude-haiku-4-5-20251001"
    extract = json.loads(Path(packet["extract_path"]).read_text("utf-8"))
    assert extract["extracted_by"] == "anthropic:claude-haiku-4-5-20251001"


def test_extract_packet_reports_missing_anthropic_key_as_a_warning_not_a_crash(world):
    v, _ = world
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="anthropic", model="claude-haiku-4-5-20251001")
    assert result["concepts"] == 0
    assert any("NO_API_KEY" in w for w in result["warnings"])


def test_extract_packet_reports_claude_api_http_error_as_a_warning_not_a_crash(world, monkeypatch):
    v, _ = world
    vault_mod.set_api_key(v, "anthropic", "sk-ant-test")

    def fail(req, timeout=None):
        raise urllib.error.HTTPError("https://api.anthropic.com/v1/messages", 401,
                                     "unauthorized", {}, io.BytesIO(b'{"error":{"message":"bad key"}}'))

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", fail)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="anthropic", model="claude-haiku-4-5-20251001")
    assert result["concepts"] == 0
    assert any("CLAUDE_API_FAILED" in w for w in result["warnings"])


def test_anthropic_key_env_var_wins_and_run_end_to_end_still_always_holds(world, monkeypatch):
    v, _ = world
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_header("X-api-key"))
        body = json.loads(req.data)
        content = body["messages"][0]["content"]
        if "Scrivi in" in content:
            payload = {"summary": "Il motore scrive in modo sicuro.",
                      "body": "# Motore transazionale\n\nOgni scrittura ha un hash. ^[demo/README.md#L3-L4]"}
        else:
            payload = {"source_language": "it", "concepts": [VALID_CONCEPT]}
        return _FakeOllamaChatResponse(json.dumps({"content": [{"type": "text", "text": json.dumps(payload)}]}).encode("utf-8"))

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", fake_urlopen)
    out = local_extract.run(v, source="demo", mode="extract_and_write", extract_provider="anthropic",
                            extract_model="claude-haiku-4-5-20251001")
    assert calls and all(c == "sk-ant-from-env" for c in calls)
    assert out["live"] == 0 and out["held"] == 1
    [cand] = review.list_candidates(v)
    assert "local-extraction" in cand["reasons"]


def test_set_local_extraction_validates_provider_and_model(world):
    v, _ = world
    with pytest.raises(MnemoError) as info:
        vault_mod.set_local_extraction(v, provider="not-a-provider", mode="extract_only")
    assert info.value.code == "BAD_PROVIDER"
    with pytest.raises(MnemoError) as info:
        vault_mod.set_local_extraction(v, provider="ollama", mode="extract_only")  # no --ollama-model
    assert info.value.code == "NO_OLLAMA_MODEL"
    with pytest.raises(MnemoError) as info:
        vault_mod.set_local_extraction(v, provider="anthropic", mode="extract_only")  # no --anthropic-model
    assert info.value.code == "NO_ANTHROPIC_MODEL"
    with pytest.raises(MnemoError) as info:
        vault_mod.set_local_extraction(v, provider="claude_code", mode="extract_only")  # no --claude-code-model
    assert info.value.code == "NO_CLAUDE_CODE_MODEL"

    anthropic_engine = {"provider": "anthropic", "ollama_model": None, "ollama_base_url": None,
                       "anthropic_model": "claude-haiku-4-5-20251001", "claude_code_model": None}
    out = vault_mod.set_local_extraction(v, provider="anthropic", mode="extract_and_write",
                                         anthropic_model="claude-haiku-4-5-20251001")
    # no write_provider given: the write phase mirrors extract exactly, as before per-phase engines existed
    assert out["local_extraction"] == {"mode": "extract_and_write", "extract": anthropic_engine, "write": anthropic_engine}

    ollama_engine = {"provider": "ollama", "ollama_model": "llama3.1", "ollama_base_url": None,
                     "anthropic_model": None, "claude_code_model": None}
    out = vault_mod.set_local_extraction(v, provider="ollama", mode="extract_only", ollama_model="llama3.1")
    assert out["local_extraction"] == {"mode": "extract_only", "extract": ollama_engine, "write": ollama_engine}


def test_set_local_extraction_can_use_a_different_engine_per_phase(world):
    v, _ = world
    with pytest.raises(MnemoError) as info:  # write_provider given but no write-claude-code-model
        vault_mod.set_local_extraction(v, provider="ollama", mode="extract_and_write", ollama_model="llama3.1",
                                       write_provider="claude_code")
    assert info.value.code == "NO_CLAUDE_CODE_MODEL"

    out = vault_mod.set_local_extraction(v, provider="ollama", mode="extract_and_write", ollama_model="llama3.1",
                                         write_provider="claude_code", write_claude_code_model="sonnet")
    lex = out["local_extraction"]
    assert lex["extract"] == {"provider": "ollama", "ollama_model": "llama3.1", "ollama_base_url": None,
                              "anthropic_model": None, "claude_code_model": None}
    assert lex["write"] == {"provider": "claude_code", "ollama_model": None, "ollama_base_url": None,
                            "anthropic_model": None, "claude_code_model": "sonnet"}


def test_load_config_migrates_local_extraction_saved_before_providers_existed(world):
    """Same staleness class as the .gitignore and review.hold bugs fixed earlier this session: a
    vault whose config.json was written before `provider` existed must still load cleanly, as
    ollama (the only provider that existed back then) -- for both phases, since a single-engine
    config predates the extract/write split too."""
    v, _ = world
    from mnemoloci.vault import engine

    cfg_path = engine(v) / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    cfg["local_extraction"] = {"mode": "extract_only", "ollama_model": "llama3.1", "ollama_base_url": None}
    cfg_path.write_text(json.dumps(cfg), "utf-8")

    loaded = vault_mod.load_config(v)
    assert loaded["local_extraction"]["extract"]["provider"] == "ollama"
    assert loaded["local_extraction"]["extract"]["ollama_model"] == "llama3.1"
    assert loaded["local_extraction"]["write"] == loaded["local_extraction"]["extract"]


def test_load_config_migrates_local_extraction_saved_before_claude_code_existed(world):
    """A flat config saved by the anthropic-only commit (before claude_code, and before the
    extract/write split) must still load: both phases get claude_code_model=None, mirroring the
    old single anthropic engine."""
    v, _ = world
    from mnemoloci.vault import engine

    cfg_path = engine(v) / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    cfg["local_extraction"] = {"provider": "anthropic", "mode": "extract_only",
                              "ollama_model": None, "ollama_base_url": None, "anthropic_model": "claude-haiku-4-5-20251001"}
    cfg_path.write_text(json.dumps(cfg), "utf-8")

    loaded = vault_mod.load_config(v)
    for phase in ("extract", "write"):
        assert loaded["local_extraction"][phase]["provider"] == "anthropic"
        assert loaded["local_extraction"][phase]["anthropic_model"] == "claude-haiku-4-5-20251001"
        assert loaded["local_extraction"][phase]["claude_code_model"] is None


def test_load_config_migrates_local_extraction_saved_before_write_phase_existed(world):
    """A nested-but-single-phase config (this session's anthropic/claude_code commits, before the
    extract/write split existed at all) must still load: `write` gets synthesised, mirroring the
    only engine that was ever saved."""
    v, _ = world
    from mnemoloci.vault import engine

    cfg_path = engine(v) / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    cfg["local_extraction"] = {"provider": "claude_code", "mode": "extract_and_write",
                              "ollama_model": None, "ollama_base_url": None, "anthropic_model": None,
                              "claude_code_model": "sonnet"}
    cfg_path.write_text(json.dumps(cfg), "utf-8")

    loaded = vault_mod.load_config(v)
    assert loaded["local_extraction"]["extract"]["claude_code_model"] == "sonnet"
    assert loaded["local_extraction"]["write"] == loaded["local_extraction"]["extract"]


# ---------- provider claude_code (claude -p, billed to the subscription, no key) ----------

class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _claude_code_result(payload: dict, *, is_error: bool = False, returncode: int = 0):
    return _FakeCompletedProcess(returncode, json.dumps({"type": "result", "is_error": is_error, **payload}))


def test_extract_packet_uses_claude_code_headless_when_provider_is_claude_code(world, monkeypatch):
    v, _ = world
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        payload = json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT]})
        return _claude_code_result({"result": payload})

    monkeypatch.setattr(local_extract.subprocess, "run", fake_run)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="claude_code", model="sonnet")
    assert result == {"concepts": 1, "discarded": 0, "warnings": []}
    cmd = calls[0]
    assert cmd[:2] == ["claude", "-p"] and "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--json-schema" in cmd  # a real schema, not just a prompt instruction
    extract = json.loads(Path(packet["extract_path"]).read_text("utf-8"))
    assert extract["extracted_by"] == "claude_code:sonnet"


def test_extract_packet_reports_claude_code_not_found_as_a_warning_not_a_crash(world, monkeypatch):
    v, _ = world

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(local_extract.subprocess, "run", fake_run)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="claude_code", model="sonnet")
    assert result["concepts"] == 0
    assert any("CLAUDE_CODE_NOT_FOUND" in w for w in result["warnings"])


def test_extract_packet_reports_claude_code_timeout_as_a_warning_not_a_crash(world, monkeypatch):
    v, _ = world

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(local_extract.subprocess, "run", fake_run)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="claude_code", model="sonnet")
    assert result["concepts"] == 0
    assert any("CLAUDE_CODE_TIMEOUT" in w for w in result["warnings"])


def test_extract_packet_reports_claude_code_error_result_as_a_warning_not_a_crash(world, monkeypatch):
    v, _ = world

    def fake_run(cmd, **kwargs):
        return _claude_code_result({"result": "model not found"}, is_error=True, returncode=1)

    monkeypatch.setattr(local_extract.subprocess, "run", fake_run)
    plan = compiler.plan(v)
    packet = packet_for(plan, "demo/README.md")

    result = local_extract.extract_packet(v, packet, provider="claude_code", model="not-a-real-model")
    assert result["concepts"] == 0
    assert any("CLAUDE_CODE_FAILED" in w and "model not found" in w for w in result["warnings"])


def test_run_end_to_end_with_claude_code_always_holds_and_never_needs_tools(world, monkeypatch):
    v, _ = world

    def fake_run(cmd, **kwargs):
        assert cmd[cmd.index("--tools") + 1] == ""  # never an agent with file/shell access
        prompt = cmd[-1]
        if "Scrivi in" in prompt:
            payload = {"summary": "Il motore scrive in modo sicuro.",
                      "body": "# Motore transazionale\n\nOgni scrittura ha un hash. ^[demo/README.md#L3-L4]"}
        else:
            payload = {"source_language": "it", "concepts": [VALID_CONCEPT]}
        return _claude_code_result({"result": json.dumps(payload)})

    monkeypatch.setattr(local_extract.subprocess, "run", fake_run)
    out = local_extract.run(v, source="demo", mode="extract_and_write", extract_provider="claude_code", extract_model="sonnet")
    assert out["live"] == 0 and out["held"] == 1
    [cand] = review.list_candidates(v)
    assert "local-extraction" in cand["reasons"]


# ---------- a different engine per phase (the actual point of extract/write being separate) ----------

def test_run_uses_ollama_to_extract_and_claude_code_to_write(world, monkeypatch):
    """The feature this whole split exists for: pull raw concepts out cheaply with a free local
    model, then let a stronger engine write the final prose -- two different providers in one run,
    each called only for its own phase."""
    v, _ = world
    ollama_calls = []
    claude_code_calls = []

    def fake_urlopen(req, timeout=None):
        ollama_calls.append(req.full_url)
        payload = json.dumps({"source_language": "it", "concepts": [VALID_CONCEPT]})
        return _FakeOllamaChatResponse(json.dumps({"message": {"content": payload}}).encode("utf-8"))

    def fake_run(cmd, **kwargs):
        claude_code_calls.append(cmd)
        payload = {"summary": "Il motore scrive in modo sicuro.",
                  "body": "# Motore transazionale\n\nOgni scrittura ha un hash. ^[demo/README.md#L3-L4]"}
        return _claude_code_result({"result": json.dumps(payload)})

    monkeypatch.setattr(local_extract.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(local_extract.subprocess, "run", fake_run)

    out = local_extract.run(v, source="demo", mode="extract_and_write",
                            extract_provider="ollama", extract_model="llama3.1",
                            write_provider="claude_code", write_model="sonnet")
    assert out["live"] == 0 and out["held"] == 1
    assert ollama_calls and all("localhost:11434" in u for u in ollama_calls)  # extraction only
    assert claude_code_calls
    cmd = claude_code_calls[0]
    assert cmd[cmd.index("--model") + 1] == "sonnet"

    [cand] = review.list_candidates(v)
    assert "local-extraction" in cand["reasons"]


def test_config_local_extraction_defaults_write_to_the_configured_extract_engine(world, monkeypatch):
    """`compile extract-local` with no write_provider override reuses the vault's saved config, and
    when that config's write phase was never overridden it mirrors extract -- so a plain
    `set_local_extraction(provider=..., mode=extract_and_write)` (no write_provider) still results
    in one consistent engine end to end, exactly as before per-phase engines existed."""
    v, _ = world
    vault_mod.set_local_extraction(v, provider="ollama", mode="extract_and_write", ollama_model="llama3.1")
    write_engine = vault_mod.load_config(v)["local_extraction"]["write"]
    assert write_engine == {"provider": "ollama", "ollama_model": "llama3.1", "ollama_base_url": None,
                            "anthropic_model": None, "claude_code_model": None}
