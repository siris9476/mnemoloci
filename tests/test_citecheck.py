import json

import pytest

from mnemoloci import citecheck, cli, sources, vault as vault_mod
from mnemoloci.markdown import render_page

SRC = (
    "# Diario\n"
    "Il server ascolta sulla porta 8080 e usa ConnectionStrings__Db.\n"
    "\n"
    "Il deploy avviene ogni venerdì tramite la pipeline di rilascio.\n"
    "### 2026-08-29 17:46 · Claude\n"
)


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "note.md").write_text(SRC, "utf-8")
    sources.add_source(root, folder, name="proj")
    return root


def _page(v, name, lines, **extra):
    fm = {"title": name, "kind": "concept", "summary": "x", "language": "it", "sources": [], "confidence": 0.9, **extra}
    path = v / "wiki" / "concepts" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_page(fm, "\n".join(lines)), "utf-8")


def test_supported_sentences_pass_and_unrelated_ones_are_reported(v):
    _page(v, "prova", [
        "- Il server ascolta sulla porta 8080 ^[proj/note.md#L2-L2]",
        "- Il deploy avviene ogni venerdì con la pipeline di rilascio ^[proj/note.md#L4-L4]",
        "- Il database è PostgreSQL versione 16 ^[proj/note.md#L4-L4]",
    ])
    out = citecheck.check(v)
    assert out["citations"] == 3 and out["weak"] == 1
    assert out["items"][0]["sentence"].startswith("Il database è PostgreSQL") and "16" in out["items"][0]["not_found"]


def test_blank_or_heading_only_lines_are_the_weakest(v):
    _page(v, "prova", ["- Il deploy avviene ogni venerdì ^[proj/note.md#L3-L3]",
                       "- Il deploy avviene ogni venerdì ^[proj/note.md#L5-L5]"])
    out = citecheck.check(v)
    assert out["weak"] == 2 and out["items"][0]["support"] == 0.0
    assert "empty" in out["items"][0]["not_found"][0]


def test_a_translated_page_is_judged_on_numbers_and_names_only(v):
    _page(v, "tradotta", ["- The server listens on port 8080 and reads ConnectionStrings__Db ^[proj/note.md#L2-L2]"],
          originalLanguage=["en"])
    assert citecheck.check(v)["weak"] == 0
    _page(v, "tradotta", ["- The server listens on port 9090 ^[proj/note.md#L2-L2]"], originalLanguage=["en"])
    [item] = citecheck.check(v)["items"]
    assert item["not_found"] == ["9090"]


def test_a_sentence_with_nothing_to_compare_is_unchecked_not_passed(v):
    _page(v, "prova", ["- Sì ^[proj/note.md#L2-L2]"])
    out = citecheck.check(v)
    assert out["unchecked"] == 1 and out["weak"] == 0


def test_cli_is_report_only(v, capsys):
    _page(v, "prova", ["- Il database è PostgreSQL versione 16 ^[proj/note.md#L4-L4]"])
    assert cli.main(["--vault", str(v), "maintain", "check-citations"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["weak"] == 1
    assert cli.main(["--vault", str(v), "maintain", "check-citations", "--below", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["weak"] == 0


def test_a_wikilink_reads_as_its_label_not_its_slug(v):
    _page(v, "prova", ["- Il server ascolta sulla porta 8080 su [[un-lungo-slug-inventato|Server]] ^[proj/note.md#L2-L2]"])
    assert citecheck.check(v, below=0.9)["weak"] == 0  # the slug would cost half of the support


def test_weak_citations_in_page_texts_not_yet_in_the_vault(v):
    page = render_page({"title": "x", "kind": "concept", "language": "it"},
                       "- Il server ascolta sulla porta 8080 ^[proj/note.md#L2-L2]\n"
                       "- Il database è PostgreSQL versione 16 ^[proj/note.md#L4-L4]")
    out = citecheck.weak_in_texts(v, {"wiki/concepts/x.md": page})
    assert out["citations"] == 2 and out["weak"] == 1 and out["items"][0]["page"] == "wiki/concepts/x.md"


def test_the_nightly_verify_reports_weak_citations_and_their_change(v):
    from mnemoloci import schedule

    _page(v, "prova", ["- Il database è PostgreSQL versione 16 ^[proj/note.md#L4-L4]"])
    first = schedule._verify(v, {})
    assert first["weak_citations"] == 1 and "weak_citations_change" not in first and first["issues"] == []
    (v / ".mnemoloci" / "scheduled.log").write_text(json.dumps({"steps": {"verify": first}}) + "\n", "utf-8")
    _page(v, "altra", ["- Il cluster usa Redis 7 ^[proj/note.md#L4-L4]"])
    second = schedule._verify(v, {})
    assert second["weak_citations"] == 2 and second["weak_citations_change"] == 1
