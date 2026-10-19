import pytest

from mnemoloci import secrets
from mnemoloci import vault as vault_mod
from mnemoloci.core.errors import MnemoError
from mnemoloci.markdown import render_page


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def test_find_reports_kind_and_line_without_echoing_the_secret():
    text = "riga innocua\nAPI_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789\naltra riga"
    hits = secrets.find(text)
    assert all(h["line"] == 2 for h in hits)  # multiple patterns legitimately overlap on one leak
    assert {h["kind"] for h in hits} >= {"env-secret", "openai-key"}
    # the finding never carries the matched substring itself
    assert not any("sk-proj" in str(v) for h in hits for v in h.values())


def test_find_detects_a_private_key_block_by_line():
    text = "prima\n-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK...\n-----END RSA PRIVATE KEY-----\ndopo"
    hits = secrets.find(text)
    assert {"kind": "private-key", "line": 2} in hits


def test_find_ignores_basic_bearer_as_ordinary_english_words():
    # "Basic motion/sound/person..." matched the old auth-header pattern by coincidence: an
    # English sentence starting with "Basic" followed by a slash-joined word list satisfied the
    # same charset as a real base64 token. A real leak with digits/padding must still be caught.
    text = "Basic motion/sound/person alerts are free but delivered only as FCM push."
    assert not any(h["kind"] == "auth-header" for h in secrets.find(text))
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"
    assert any(h["kind"] == "auth-header" for h in secrets.find(text))


def test_find_is_read_only():
    text = "token: hunter2ish123"
    before = text
    secrets.find(text)
    assert text == before  # find() never mutates its input, unlike redact()


def test_audit_vault_requires_a_real_vault(tmp_path):
    with pytest.raises(MnemoError) as info:
        secrets.audit_vault(tmp_path / "not-a-vault")
    assert info.value.code == "NOT_INITIALIZED"


def test_audit_vault_finds_a_leak_that_bypassed_capture_time_redaction(v):
    clean_fm = {"title": "Pagina pulita", "kind": "concept", "summary": "Niente di sensibile.", "aliases": []}
    (v / "wiki/concepts").mkdir(parents=True, exist_ok=True)
    (v / "wiki/concepts/pulita.md").write_text(render_page(clean_fm, "Testo senza segreti."), "utf-8")

    # simulates a manual edit through the app's editor, which bypasses clips.store()'s redact()
    leaked_fm = {"title": "Pagina con fuga", "kind": "concept", "summary": "Contiene una chiave.", "aliases": []}
    body = "Ho incollato per sbaglio:\nAWS_SECRET=AKIAABCDEFGHIJKLMNOP\ndopo la chiave."
    (v / "wiki/concepts/fuga.md").write_text(render_page(leaked_fm, body), "utf-8")

    out = secrets.audit_vault(v)
    assert out["scanned"] == 2
    assert out["pages_with_findings"] == 1
    [entry] = out["findings"]
    assert entry["path"] == "wiki/concepts/fuga.md"
    assert {h["kind"] for h in entry["findings"]} >= {"aws-key"}


def test_audit_vault_clean_vault_reports_nothing(v):
    fm = {"title": "Pagina pulita", "kind": "concept", "summary": "Niente.", "aliases": []}
    (v / "wiki/concepts").mkdir(parents=True, exist_ok=True)
    (v / "wiki/concepts/pulita.md").write_text(render_page(fm, "Testo senza segreti."), "utf-8")

    out = secrets.audit_vault(v)
    assert out == {"scanned": 1, "pages_with_findings": 0, "findings": []}
