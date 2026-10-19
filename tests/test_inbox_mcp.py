import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import pytest

from mnemoloci import compile as compiler
from mnemoloci import inbox, vault as vault_mod
from mnemoloci.core.errors import MnemoError

from conftest import REPO

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32 + b"fake-image-1"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\0" * 32 + b"fake-image-2"


@pytest.fixture
def v(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "vault"
    vault_mod.init(root)
    return root


def test_add_file_dedup_transcribe_and_compile(v, tmp_path):
    shot = tmp_path / "IMG_0001.PNG"
    shot.write_bytes(PNG)
    first = inbox.add_files(v, [str(shot)], note="scontrino")["results"]
    assert first[0]["image"].endswith(".png")
    sidecar = (v / first[0]["sidecar"]).read_text("utf-8")
    assert f"![[{Path(first[0]['image']).name}]]" in sidecar and "note: scontrino" in sidecar
    again = inbox.add_files(v, [str(shot)])["results"]
    assert again[0]["skipped"]["reason"] == "duplicate"

    [item] = inbox.pending(v)["pending"]
    assert Path(item["image"]).read_bytes() == PNG
    planned = compiler.plan(v, source="inbox")
    assert planned["run_id"] is None and planned["awaiting_transcription"]

    inbox.transcribe(v, item["id"], "Totale: 12,50 EUR\nNegozio: Libreria")
    assert inbox.pending(v)["pending"] == []
    planned = compiler.plan(v, source="inbox")
    assert [f["key"] for f in planned["files"]] == [f"inbox/{first[0]['sidecar'].split('raw/inbox/')[1]}"]


def test_ocr_without_the_extra_installed_says_so_and_leaves_the_item_pending(v, tmp_path):
    shot = tmp_path / "receipt.png"
    shot.write_bytes(PNG)
    inbox.add_files(v, [str(shot)])
    [item] = inbox.pending(v)["pending"]
    with pytest.raises(MnemoError) as info:
        inbox.ocr(v, item["id"])
    assert info.value.code == "MISSING_EXTRA" and "mnemoloci[ocr]" in info.value.message
    assert inbox.pending(v)["pending"] != []  # still waiting: nothing was written on failure


def test_non_images_are_refused(v, tmp_path):
    doc = tmp_path / "x.png"
    doc.write_bytes(b"not really a png")
    assert inbox.add_files(v, [str(doc)])["results"][0]["skipped"]["reason"].startswith("unsupported")


def test_from_session_extracts_attached_images(v, tmp_path):
    cwd = r"C:\Work\Phone"
    folder = tmp_path / "claude" / "projects" / "C--Work-Phone"
    folder.mkdir(parents=True)
    records = [
        {"type": "user", "sessionId": "s1", "cwd": cwd, "timestamp": "2026-09-24T10:00:00Z", "origin": {"kind": "human"},
         "message": {"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}},
             {"type": "text", "text": "salva questo screenshot"}]}},
        {"type": "user", "sessionId": "s1", "cwd": cwd, "timestamp": "2026-09-24T10:05:00Z",
         "message": {"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG2).decode()}}]}},
    ]
    (folder / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in records), "utf-8")
    out = inbox.from_session(v, cwd=cwd, last=1)
    assert len(out["results"]) == 1
    assert (v / out["results"][0]["image"]).read_bytes() == PNG2
    both = inbox.from_session(v, cwd=cwd, last=5)["results"]
    assert sum(1 for r in both if "image" in r) == 1  # PNG2 already stored
    notes = [p.read_text("utf-8") for p in (v / "raw/inbox").rglob("*.md")]
    assert any("salva questo screenshot" in n for n in notes)
    with pytest.raises(MnemoError):
        inbox.from_session(v, cwd=r"C:\Nowhere")


def _mcp_call(vault: Path, calls: list[tuple[str, dict]]):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "mnemoloci", "--vault", str(vault), "mcp"],
                                       cwd=str(REPO), env={**os.environ, "PYTHONPATH": str(REPO)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                results = []
                for name, args in calls:
                    res = await session.call_tool(name, args)
                    results.append(res.structured_content or json.loads(res.content[0].text))
                return tools, results

    return asyncio.run(run())


def test_mcp_server_over_stdio(v):
    from mnemoloci import personal

    personal.goals_add(v, "Finire la fase 4")
    tools, (goals, missing, escape, status) = _mcp_call(v, [
        ("goals", {}), ("get_page", {"path": "wiki/nope.md"}), ("get_page", {"path": "../secret.md"}), ("status", {}),
    ])
    names = {t.name for t in tools.tools}
    assert names == {"search", "get_page", "recap", "goals", "profile", "skills_find", "status", "review_list",
                     "sessions_search", "sessions_recent", "session_read", "context_pack", "file_history", "file_risk",
                     "reflect_trace"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.tools)
    assert goals["goals"][0]["text"] == "Finire la fase 4"
    assert missing["code"] == "NO_SUCH_PAGE"
    assert escape["ok"] is False
    assert "pending_review" in status
