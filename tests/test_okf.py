import os
from pathlib import Path

import pytest

from mnemoloci import okf, review, sources, vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.core.errors import MnemoError
from mnemoloci.core.hashing import sha256_bytes
from mnemoloci.core.lock import VaultLock
from mnemoloci.markdown import render_page, split_frontmatter

OFFICIAL = Path(os.environ.get("OKF_BUNDLES", "okf-src/bundles"))  # a clone of the official OKF repo, if you have one


@pytest.fixture
def v(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "README.md").write_text("Il motore scrive in modo sicuro.\nUsa un journal.\nAPI_KEY = \"sk-ant-abcdefghijklmnopqrstuvwxyz012345\"\n", "utf-8")
    root = tmp_path / "vault"
    vault_mod.init(root)
    sources.add_source(root, str(project), "proj")
    sha = sha256_bytes((project / "README.md").read_bytes())
    common = {"kind": "concept", "language": "it", "confidence": 0.9, "provenanceState": "extracted",
              "contradictedBy": [], "updated": "2026-09-24", "sources": [{"key": "proj/README.md", "sha256": sha}]}
    pages = {
        "wiki/concepts/motore.md": render_page(dict(common, title="Motore", summary="Scrive nel vault.", aliases=["Engine"]),
                                               "Il motore è sicuro ^[proj/README.md#L1-L1] e usa un journal ^[proj/README.md#L2-L2].\n\nVedi [[journal|il journal]].\n\n> [!quote]- Originale (en)\n> The engine is safe."),
        "wiki/concepts/journal.md": render_page(dict(common, title="Journal", summary="Registro delle scritture."),
                                                "Il journal registra ogni scrittura ^[proj/README.md#L2-L2]. Vedi [[inesistente]]."),
    }
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed", "operation": "test",
         "writes": [{"path": p, "action": "create", "content": c} for p, c in pages.items()]}
    txn.apply(root, b, txn.prepare(root, b).approval_sha256)
    return root, project


def _doc(bundle: Path, rel: str):
    fm, body = split_frontmatter((bundle / rel).read_text("utf-8"))
    return fm, body


def test_export_is_conformant_okf_v02(v, tmp_path):
    root, _ = v
    out = okf.export_okf(root, str(tmp_path / "bundle"), human_id="ada")
    bundle = Path(out["out"])
    assert out["conformance_problems"] == [] and out["concepts"] == 2
    index_fm, _ = split_frontmatter((bundle / "index.md").read_text("utf-8"))
    assert index_fm == {"okf_version": "0.2"}
    assert (bundle / "concepts/index.md").read_text("utf-8").startswith("# Concepts")
    assert "## 2026-09-24" in (bundle / "log.md").read_text("utf-8")

    fm, body = _doc(bundle, "concepts/motore.md")
    assert fm["type"] == "Concept" and fm["description"] == "Scrive nel vault."
    assert fm["generated"] == {"by": okf.PRODUCER, "at": "2026-09-24T00:00:00Z"}
    assert "verified" not in fm  # never approved by a human
    sid = fm["sources"][0]["id"]
    assert fm["sources"][0]["resource"] == "mnemoloci-source:proj/README.md"
    assert f"[^{sid}]" in body and f"[^{sid}]: proj/README.md" in body and "^[" not in body
    assert "[il journal](journal.md)" in body and "[[" not in body
    assert "> **Originale (en)**" in body
    assert fm["x-mnemoloci"]["citations"][0] == {"id": sid, "key": "proj/README.md", "start": 1, "end": 1}
    _, jbody = _doc(bundle, "concepts/journal.md")
    assert "inesistente" in jbody and "](" not in jbody.split("Vedi")[1]  # no fake link


def test_export_drops_the_per_citation_validity_annotation_without_crashing(v, tmp_path):
    root, project = v
    sha = sha256_bytes((project / "README.md").read_bytes())
    fm = {"title": "Prezzo", "kind": "concept", "language": "it", "confidence": 0.9, "provenanceState": "extracted",
          "contradictedBy": [], "updated": "2026-09-24", "summary": "s.", "sources": [{"key": "proj/README.md", "sha256": sha}]}
    content = render_page(fm, "Prezzo attuale ^[proj/README.md#L1-L1#V2020-01-01..2026-01-01].")
    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed-validity", "operation": "test",
         "writes": [{"path": "wiki/concepts/prezzo.md", "action": "create", "content": content}]}
    txn.apply(root, b, txn.prepare(root, b).approval_sha256)
    out = okf.export_okf(root, str(tmp_path / "bundle2"), human_id="ada")
    assert out["conformance_problems"] == []  # a per-citation validity annotation is not a schema violation
    _, body = _doc(Path(out["out"]), "concepts/prezzo.md")
    assert "^[" not in body and "#V" not in body and "[^" in body  # the annotation is dropped, the footnote stays


def test_export_marks_human_review_and_staleness(v, tmp_path):
    root, project = v
    with VaultLock(root) as lock:
        cid = review.save_candidate(root, lock, run_id="r", slug="rivisto", target_path="wiki/concepts/rivisto.md",
                                    base_sha256=None, reasons=[{"code": "low-confidence", "detail": "x"}],
                                    content=render_page({"title": "Rivisto", "kind": "concept"}, "Testo."))
    review.approve(root, cid, review.show(root, cid)["content_sha256"])
    (project / "README.md").write_text("cambiato\n", "utf-8")
    bundle = Path(okf.export_okf(root, str(tmp_path / "b"), human_id="ada")["out"])
    fm, _ = _doc(bundle, "concepts/rivisto.md")
    assert fm["verified"]["by"] == "human:ada"
    stale, _ = _doc(bundle, "concepts/motore.md")
    assert stale["x-mnemoloci"]["freshness"] == "stale" and stale["stale_after"].endswith("Z")


def test_export_refuses_unsafe_targets_and_redacts_references(v, tmp_path):
    root, _ = v
    with pytest.raises(MnemoError):
        okf.export_okf(root, str(root / "inside"))
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x")
    with pytest.raises(MnemoError):
        okf.export_okf(root, str(busy))
    bundle = Path(okf.export_okf(root, str(tmp_path / "withsrc"), include_sources=True)["out"])
    ref = (bundle / "references/proj/README.md.md").read_text("utf-8")
    assert "sk-ant-" not in ref and "[REDACTED" in ref
    assert okf.check_conformance(bundle) == []


@pytest.mark.skipif(not OFFICIAL.is_dir(), reason="official OKF repo not cloned")
@pytest.mark.parametrize("name", ["ga4", "stackoverflow", "crypto_bitcoin", "acme_retail"])
def test_official_google_bundles_are_conformant_by_our_checker(name):
    assert okf.check_conformance(OFFICIAL / name) == []


@pytest.mark.skipif(not OFFICIAL.is_dir(), reason="official OKF repo not cloned")
def test_import_official_bundle_into_review_queue(tmp_path):
    root = tmp_path / "vault"
    vault_mod.init(root)
    dry = okf.import_okf(root, str(OFFICIAL / "ga4"), dry_run=True)
    assert dry["would_stage"] and not review.list_candidates(root)
    out = okf.import_okf(root, str(OFFICIAL / "ga4"))
    assert out["staged"] == len(dry["would_stage"])
    shown = next(review.show(root, c) for c in out["candidates"] if c.startswith("ga4-obfuscated-sample-ecommerce"))
    fm, body = split_frontmatter(shown["content"])
    assert fm["x-okf"]["type"] == "BigQuery Dataset"
    assert fm["x-okf"]["generated"]["at"] == "2026-07-10T21:14:56+00:00"  # author's text, not re-formatted
    assert "[[events|events_]]" in body or "[[events-|events_]]" in body or "events_" in body
    assert "^[okf-ga4/datasets/ga4_obfuscated_sample_ecommerce.md#L1-L" in body
    assert any(r["code"] == "external-source" for r in shown["reasons"])
    review.approve(root, shown["id"], shown["content_sha256"])
    assert (root / shown["target_path"]).is_file()


def test_round_trip_export_then_import(v, tmp_path):
    root, _ = v
    bundle = okf.export_okf(root, str(tmp_path / "rt"))["out"]
    other = tmp_path / "other"
    vault_mod.init(other)
    out = okf.import_okf(other, bundle)
    titles = {split_frontmatter(review.show(other, c)["content"])[0]["title"] for c in out["candidates"]}
    assert titles == {"Motore", "Journal"}
    motore = next(review.show(other, c)["content"] for c in out["candidates"] if c.startswith("motore"))
    assert "[[journal|il journal]]" in motore
    assert split_frontmatter(motore)[0]["x-okf"]["x-mnemoloci"]["aliases"] == ["Engine"]
