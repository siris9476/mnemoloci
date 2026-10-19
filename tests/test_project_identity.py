import pytest

from mnemoloci import hooks, memory, vault as vault_mod


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "work" / "demo"
    (root / "src" / "deep").mkdir(parents=True)
    (root / memory.IDENTITY_FILE).write_text("# il nome del progetto\nDemo App\n", "utf-8")
    return root


def test_identity_is_found_from_any_folder_inside_the_project(proj):
    assert memory.project_identity(str(proj)) == "@demo app"
    assert memory.project_identity(str(proj / "src" / "deep")) == "@demo app"


def test_no_file_no_identity_and_missing_folders_are_tolerated(tmp_path):
    assert memory.project_identity(str(tmp_path)) is None
    assert memory.project_identity(r"C:\Users\you\Work\Non\Esiste") is None
    assert memory.project_identity(None) is None


def test_a_note_is_stored_under_the_identity_and_found_from_a_subfolder(v, proj):
    res = memory.add(v, "decision", "Usare SQLite", "Per l'indice.", project=str(proj))
    assert memory.read(v, res["id"])["frontmatter"]["project"] == "@demo app"
    found = memory.list_memory(v, project=str(proj / "src" / "deep"))
    assert [m["id"] for m in found] == [res["id"]]
    assert memory.list_memory(v, project=str(proj.parent)) == []


def test_the_note_follows_the_project_when_the_folder_is_renamed(v, proj):
    res = memory.add(v, "decision", "Usare SQLite", "Per l'indice.", project=str(proj))
    moved = proj.parent / "demo-renamed"
    proj.rename(moved)
    assert [m["id"] for m in memory.list_memory(v, project=str(moved))] == [res["id"]]


def test_old_path_notes_keep_working(v, proj, tmp_path):
    other = tmp_path / "plain"
    other.mkdir()
    res = memory.add(v, "decision", "Nota vecchia", "Corpo.", project=str(other))
    assert memory.read(v, res["id"])["frontmatter"]["project"] == str(other)
    assert [m["id"] for m in memory.list_memory(v, project=str(other / "x"))] == [res["id"]]


def test_a_lesson_is_confirmed_across_two_folders_of_the_same_project(v, proj):
    first = memory.add(v, "lesson", "Rileggere sempre", "Rileggere il file prima di modificarlo.", project=str(proj))
    again = memory.add(v, "lesson", "Rileggere sempre", "Rileggere il file prima di modificarlo.",
                       project=str(proj / "src"))
    assert again["reinforced"] and again["id"] == first["id"]


def test_hints_use_the_identity(v, proj):
    memory.add(v, "lesson", "Compilare sempre prima dei test", "Compilare prima dei test.", project=str(proj))
    memory.add(v, "lesson", "Compilare sempre dopo il commit", "Solo qui.", project=r"C:\Users\you\Work\Altro")
    from mnemoloci import index

    index.rebuild(v)
    hints = hooks.prompt_hints(v, "devo compilare sempre prima dei test", str(proj / "src"), set())
    assert [h["title"] for h in hints] == ["Compilare sempre prima dei test"]


def test_todos_follow_the_identity_too(v, proj):
    from mnemoloci import personal

    personal.todos_add(v, "Scrivere i test", project=str(proj))
    personal.todos_add(v, "Altro progetto", project=r"C:\Users\you\Work\Altro")
    personal.todos_add(v, "Per tutti")
    assert personal.todos_list(v)["todos"][0]["project"] in ("@demo app", r"c:\users\you\work\altro", None)
    moved = proj.parent / "demo-renamed"
    proj.rename(moved)
    texts = {t["text"] for t in personal.todos_list(v, project=str(moved / "src"))["todos"]}
    assert texts == {"Scrivere i test", "Per tutti"}
    stored = {t["text"]: t["project"] for t in personal.todos_list(v)["todos"]}
    assert stored["Scrivere i test"] == "@demo app" and stored["Altro progetto"] == r"c:\users\you\work\altro"
