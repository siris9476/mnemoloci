"""Phase 13 block C: app API for maintenance, weekly review, skill quality and briefings."""

import pytest

from mnemoloci import briefing, index, personal, remote, skill_registry
from mnemoloci import vault as vault_mod
from mnemoloci.core import txn
from mnemoloci.markdown import render_page


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(skill_registry, "_SELF_PLUGIN_SKILLS", tmp_path / "no-self-plugin-here")
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def _fm(title, **extra):
    return {"title": title, "kind": "concept", "summary": f"{title}.", "aliases": [],
            "sources": [{"key": "p/a.md", "sha256": "a" * 64}], "confidence": 0.9, "provenanceState": "extracted",
            "contradictedBy": [], "updated": "2026-09-24", "language": "it", **extra}


def test_app_api_maintenance_weekly_quality_briefings(v, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from mnemoloci import api

    b = {"schema": txn.BUNDLE_SCHEMA, "operation_id": "seed-c", "operation": "test", "writes": [
        {"path": "wiki/concepts/lock-di-vault.md", "action": "create", "content": render_page(_fm("Lock di vault", tags=["Lock"]), "Il lock.")},
        {"path": "wiki/concepts/journal.md", "action": "create",
         "content": render_page(_fm("Journal", tags=["lock"]), "Prendi il [[lock]] e aggiorna il lock di vault.")}]}
    txn.apply(v, b, txn.prepare(v, b).approval_sha256)
    index.rebuild(v)
    (tmp_path / "claude" / "skills" / "x").mkdir(parents=True)
    (tmp_path / "claude" / "skills" / "x" / "SKILL.md").write_text("---\nname: x\ndescription: Fa cose.\n---\n\nbody\n", "utf-8")
    c = TestClient(api.create_app(v, "launch", {"port": 8790}, remote_mode=True), base_url="http://127.0.0.1:8790")
    owner = {"Authorization": "Bearer launch"}
    m = c.get("/api/maintain", headers=owner).json()
    assert m["fix_links"]["fixes"] == [{"page": "journal", "from": "lock", "to": "lock-di-vault"}]
    assert m["tags"][0]["tags"] == ["Lock", "lock"]
    dry = c.post("/api/maintain/fix-links", json={}, headers=owner).json()
    assert c.post("/api/maintain/fix-links", json={"approved": dry["approval_sha256"]}, headers=owner).json()["dry_run"] is False
    rename = c.post("/api/tags/rename", json={"old": "Lock", "new": "lock"}, headers=owner).json()
    assert c.post("/api/tags/rename", json={"old": "Lock", "new": "lock", "approved": rename["approval_sha256"]},
                  headers=owner).json()["ok"]
    assert c.get("/api/maintain", headers=owner).json()["tags"] == []
    personal.goals_add(v, "Finire")
    week = c.get("/api/weekly", headers=owner).json()
    assert week["stuck_goals"] == ["g1"]
    assert c.post("/api/weekly/save", json={"plan": "- finire"}, headers=owner).json()["path"].startswith("_meta/weekly/")
    quality = c.get("/api/skills/quality", headers=owner).json()
    assert quality["stocktake"]["results"][0]["name"] == "x" and quality["curate"]["installed"] == 1
    briefing.save(v, "Il [[journal]] registra.", title="Journal in breve", topic="journal")
    assert c.get("/api/briefings", headers=owner).json()["items"][0]["title"] == "Journal in breve"
    host = "mypc.tail1234.ts.net"
    remote.allow_hosts(v, [host])
    phone = {"Authorization": f"Bearer {remote.claim(v, remote.create_pairing_code(v)['code'], 'iPhone')['token']}", "Host": host}
    assert c.post("/api/skills/archive", json={"name": "x"}, headers=phone).json()["code"] == "OWNER_ONLY"
