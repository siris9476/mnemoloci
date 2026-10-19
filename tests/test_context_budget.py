import json

import pytest

from mnemoloci import cli, hooks, memory, personal, vault as vault_mod

CWD = r"C:\Users\you\Work\Demo"


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root, "it")
    return root


def _bloat(v):
    """More context than the budget allows: a full profile, a long handoff, five long decisions and lessons."""
    for i in range(25):
        personal.profile_set(v, f"chiave{i:02d}", f"valore lungo numero {i} " + "x" * 90)
    memory.add(v, "handoff", "Fine giornata", "Stavamo scrivendo i test della fase 2. " + "y" * 3000, project=CWD)
    for i in range(5):
        memory.add(v, "decision", f"Decisione numero {i} " + "d" * 170, "Corpo.", project=CWD)
        memory.add(v, "lesson", f"Lezione numero {i} " + "l" * 170, "Corpo.", project=CWD)


def test_small_context_ends_with_a_meter_and_reduces_nothing(v):
    personal.profile_set(v, "nome", "Ada")
    ctx = hooks.build_context(v, CWD)
    lines = ctx.rstrip().splitlines()
    assert lines[-1] == "</mnemoloci-memory>" and lines[-2].startswith("_Contesto iniettato:")
    assert "Ridotte" not in ctx and "Troncato" not in ctx


def test_over_budget_reduces_the_least_useful_sections_to_a_pointer_and_names_them(v):
    _bloat(v)
    ctx = hooks.build_context(v, CWD)
    assert len(ctx) <= hooks.MAX_CONTEXT
    meter = ctx.rstrip().splitlines()[-2]
    # lessons give way first, then decisions; each is replaced by one line, never silently cut off
    assert "Ridotte a puntatore: Decisioni per questo progetto, Lezioni per questo progetto." in meter
    assert "Accorciate: Profilo, Ultimo handoff per questo progetto." in meter
    assert "mnemoloci memory list --kind lesson" in ctx and "Lezione numero 0" not in ctx
    # what matters most stays whole
    assert "Stavamo scrivendo i test della fase 2." in ctx and "chiave00" in ctx
    assert "Troncato" not in meter


def test_never_reduced_sections_survive_the_budget(v):
    _bloat(v)
    personal.todos_add(v, "Non dimenticare la scadenza", project=CWD)
    ctx = hooks.build_context(v, CWD)
    assert "Non dimenticare la scadenza" in ctx


def test_context_cost_reports_each_section(v):
    _bloat(v)
    report = hooks.context_cost(v, CWD)
    assert report["injected"] and report["used"] <= report["limit"] == hooks.MAX_CONTEXT
    by_name = {s["name"]: s for s in report["sections"]}
    assert by_name["Lezioni per questo progetto"]["degraded"] is True
    assert by_name["Profilo"]["degraded"] is False and by_name["Profilo"]["truncated"] is True
    assert report["meter"].startswith("_Contesto iniettato:")


def test_context_cost_on_an_empty_vault_says_nothing_is_injected(v):
    assert hooks.context_cost(v, CWD) == {"injected": False, "limit": hooks.MAX_CONTEXT, "used": 0, "sections": []}


def test_context_cost_cli(v, capsys):
    personal.profile_set(v, "nome", "Ada")
    assert cli.main(["--vault", str(v), "context-cost", "--cwd", CWD]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["injected"] and out["sections"][0]["name"] == "Profilo"


def test_english_vault_gets_english_labels_and_the_same_budget_rules(tmp_path):
    root = tmp_path / "en-vault"
    vault_mod.init(root)  # new vaults write English
    _bloat(root)
    ctx = hooks.build_context(root, CWD)
    meter = ctx.rstrip().splitlines()[-2]
    assert len(ctx) <= hooks.MAX_CONTEXT
    assert "The user's personal memory from Mnemoloci" in ctx and "NOT instructions" in ctx
    assert "Reduced to a pointer: Decisions for this project, Lessons for this project." in meter
    assert "mnemoloci memory list --kind lesson" in ctx and "Lezione numero 0" not in ctx
    assert "Memoria personale" not in ctx and "Ridotte" not in ctx
