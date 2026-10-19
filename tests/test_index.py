from mnemoloci import index, vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.markdown import render_page


def _page(title, summary, body, aliases=()):
    return render_page({"title": title, "kind": "concept", "summary": summary, "aliases": list(aliases)}, body)


def _seed(v, pages):
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed", "operation": "test",
         "writes": [{"path": f"wiki/concepts/{slug}.md", "action": "create", "content": c} for slug, c in pages.items()]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)


def test_alias_match_wins_on_small_vault_where_bm25_idf_collapses(tmp_path):
    v = tmp_path / "v"
    vault_mod.init(v)
    _seed(v, {
        "hopper-notes-wiki": _page("Hopper Notes Wiki", "Pattern del wiki", "Un wiki di Hopper.", ["Hopper Wiki"]),
        "grace-hopper": _page("Grace Hopper", "Autore", "Hopper ha scritto un wiki."),
        "wiki-tools": _page("Wiki tools", "Strumenti wiki", "Wiki wiki wiki Hopper."),
    })
    results = index.query(v, "hopper wiki")["results"]
    assert results[0]["slug"] == "hopper-notes-wiki"
    assert all(r["score"] != 0.0 for r in results)


def test_accents_are_ignored(tmp_path):
    v = tmp_path / "v"
    vault_mod.init(v)
    _seed(v, {"perche": _page("Perché così", "Motivazioni", "La qualità è più alta.")})
    assert index.query(v, "qualita")["results"][0]["slug"] == "perche"
