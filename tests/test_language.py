"""The engine writes in the vault's language: English for a new vault, Italian when the target is "it"
(and for vaults saved before the setting existed)."""
import json

import pytest

from mnemoloci import digest, filing, hooks, i18n, memory, obsidian, personal, publish
from mnemoloci import vault as vault_mod
from mnemoloci.core.errors import MnemoError


@pytest.fixture(autouse=True)
def _no_real_sessions(tmp_path, monkeypatch):
    """The digest looks at past sessions: keep it away from the real ones."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "claude").mkdir()


@pytest.fixture
def en(tmp_path):
    root = tmp_path / "en"
    vault_mod.init(root)
    return root


@pytest.fixture
def it(tmp_path):
    root = tmp_path / "it"
    vault_mod.init(root, "it")
    return root


def test_new_vaults_are_english_unless_asked(en, it):
    assert vault_mod.load_config(en)["language"]["target"] == "en"
    assert vault_mod.load_config(it)["language"]["target"] == "it"
    assert i18n.lang(en) == "en" and i18n.lang(it) == "it"
    assert "# Index" in (en / "wiki/index.md").read_text("utf-8") and "# Indice" in (it / "wiki/index.md").read_text("utf-8")
    assert "This vault is the source of truth" in (en / "MNEMOLOCI.md").read_text("utf-8")
    assert "fonte di verità" in (it / "MNEMOLOCI.md").read_text("utf-8")


def test_a_config_without_the_language_section_is_italian(en):
    path = en / ".mnemoloci" / "config.json"
    config = json.loads(path.read_text("utf-8"))
    del config["language"]
    path.write_text(json.dumps(config), "utf-8")
    assert i18n.lang(en) == "it"
    assert vault_mod.load_config(en)["language"]["target"] == "it"


def test_a_missing_vault_defaults_to_english(tmp_path):
    assert i18n.lang(tmp_path / "nowhere") == "en" and i18n.lang(None) == "en"


def test_forced_language_wins_over_the_vault(en):
    with i18n.forced("it"):
        assert i18n.lang(en) == "it"
    assert i18n.lang(en) == "en"


def test_personal_notes_follow_the_language(en, it):
    for root, goals_title, profile_title in ((en, "# Goals", "# Profile"), (it, "# Obiettivi", "# Profilo")):
        personal.profile_set(root, "role", "developer")
        personal.goals_add(root, "Ship it")
        personal.todos_add(root, "Write tests")
        assert profile_title in (root / "_meta/profile.md").read_text("utf-8")
        assert goals_title in (root / "_meta/goals.md").read_text("utf-8")
    assert "# To-dos" in (en / "_meta/todos.md").read_text("utf-8")
    assert "# Cose da fare" in (it / "_meta/todos.md").read_text("utf-8")


def test_memory_warnings_follow_the_language(en, it):
    for root, word in ((en, "without a project"), (it, "senza progetto")):
        note = memory.add(root, "lesson", "Use SQLite", "Because it is simple.")
        assert any(word in w for w in note["warnings"]), note


def test_session_start_context_follows_the_language(en, it):
    personal.profile_set(en, "name", "Ada")
    personal.profile_set(it, "nome", "Ada")
    english = hooks.build_context(en, "C:\\Users\\you\\Work")
    italian = hooks.build_context(it, "C:\\Users\\you\\Work")
    assert "## Profile" in english and "Injected context" in english and "Profilo" not in english
    assert "## Profilo" in italian and "Contesto iniettato" in italian and "Injected" not in italian


def test_digest_headings_follow_the_language(en, it):
    for root, heading in ((en, "# Summary of"), (it, "# Riepilogo del")):
        data = digest.build(root)
        text = digest.render(data, root)
        assert heading in text


def test_index_and_bases_follow_the_language(en, it):
    assert publish.render_index(en, {}, "now").startswith("# Index")
    assert publish.render_index(it, {}, "now").startswith("# Indice")
    obsidian.bases(en)
    obsidian.bases(it)
    assert (en / "_meta/bases/To review.base").is_file()
    assert (it / "_meta/bases/Da rivedere.base").is_file()


def test_filing_views_follow_the_language(en, it):
    filing.generate(en, "para")
    filing.generate(it, "para")
    assert (en / "_meta/filing/para/Projects.md").is_file()
    assert (it / "_meta/filing/para/Progetti.md").is_file()


def test_init_refuses_an_unknown_language_gracefully(tmp_path):
    root = tmp_path / "x"
    vault_mod.init(root, "fr")  # anything but "it" is English
    assert vault_mod.load_config(root)["language"]["target"] == "en"
    with pytest.raises(MnemoError):
        vault_mod.init(root)
