"""Import OpenAI Codex CLI sessions.

Codex writes one JSONL "rollout" per session under ~/.codex/sessions/YYYY/MM/DD/
(and archived_sessions/). Verified on this machine's own rollouts: a
`session_meta` record (id, cwd), then `response_item` records; the conversation
is the `message` items with role user (`input_text`) and assistant
(`output_text`). Developer messages, reasoning, tool calls and the automatic
`<environment_context>` user blocks are left out.

Same output as Claude Code imports: redacted digests under raw/codex/, compiled
as source `codex-sessions`, and added to the session search index.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from . import session_index
from .core import txn
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .markdown import slugify
from .secrets import redact
from .sessions import WRITES_PER_TXN, render_parts
from .sources import load_sources
from .vault import load_state, now_iso, require_vault, save_state

STATE_SCHEMA = "mnemoloci.codex-imports.v1"
SOURCE_NAME = "codex-sessions"
RAW_ROOT = "raw/codex"
AUTOMATIC = ("<environment_context", "<user_instructions", "<permissions", "<INSTRUCTIONS>")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def rollouts(home: Path | None = None) -> list[Path]:
    base = home or codex_home()
    return sorted(p for d in ("sessions", "archived_sessions") for p in (base / d).rglob("rollout-*.jsonl"))


def parse_rollout(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"session_id": path.stem, "cwd": None, "git_branch": None, "title": None, "messages": []}
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rec.get("type") == "session_meta":
                info["session_id"] = str(payload.get("id") or info["session_id"])
                info["cwd"] = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
                continue
            if rec.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            kind = "input_text" if role == "user" else "output_text"
            content = payload.get("content") if isinstance(payload.get("content"), list) else []
            text = "\n\n".join(str(c.get("text") or "") for c in content
                               if isinstance(c, dict) and c.get("type") == kind).strip()
            if not text or (role == "user" and text.lstrip().startswith(AUTOMATIC)):
                continue
            info["messages"].append({"role": role, "ts": str(rec.get("timestamp") or ""), "text": text})
    first = next((m["text"] for m in info["messages"] if m["role"] == "user"), "")
    # the title is the first prompt, which may hold a pasted key: redacted before it becomes a heading
    info["title"] = " ".join(redact(first)[0].split())[:80] or "Sessione Codex"
    info["assistant_name"] = "Codex"
    return info


def import_codex(vault: Path, *, home: str | None = None, since: str | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    report = {"scanned": 0, "imported": 0, "unchanged": 0, "empty": 0, "filtered": 0}
    to_index = []
    with VaultLock(vault) as lock:
        state = load_state(vault, "codex_imports.json", STATE_SCHEMA, {"files": {}})
        writes: list[dict[str, Any]] = []
        for path in rollouts(Path(home) if home else None):
            report["scanned"] += 1
            key, st = str(path.resolve()), path.stat()
            known = state["files"].get(key)
            if known and known["size"] == st.st_size and known["mtime_ns"] == st.st_mtime_ns:
                report["unchanged"] += 1
                continue
            try:
                info = parse_rollout(path)
            except OSError:
                report["errors"] = report.get("errors", 0) + 1
                continue
            first = next((m["ts"] for m in info["messages"] if m["ts"]), "")
            if since and first and first[:10] < since:
                report["filtered"] += 1
                continue
            if not info["messages"]:
                report["empty"] += 1
            else:
                folder = known["folder"] if known and known.get("folder") else (
                    f"{RAW_ROOT}/{slugify(info['cwd'] or 'codex')[:60]}/{first[:10] or 'undated'}-{info['session_id'][:8]}")
                for n, content in enumerate(render_parts(info, source="codex"), 1):
                    rel = f"{folder}/part-{n:03d}.md"
                    target = vault / rel
                    if target.is_file():
                        current = target.read_bytes()
                        if current != content.encode("utf-8"):
                            writes.append({"path": rel, "action": "replace", "content": content,
                                           "expected_sha256": sha256_bytes(current)})
                    else:
                        writes.append({"path": rel, "action": "create", "content": content})
                state["files"][key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "folder": folder,
                                       "session_id": info["session_id"], "imported": now_iso()}
                to_index.append(info)
                report["imported"] += 1
                continue
            state["files"][key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "session_id": info["session_id"]}
        report["files_to_write"] = len(writes)
        if dry_run:
            return {"dry_run": True, **report, "codex_home": str(Path(home) if home else codex_home())}
        for start in range(0, len(writes), WRITES_PER_TXN):
            bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"codex-{time.time_ns()}-{start}",
                      "operation": "codex-import", "writes": writes[start:start + WRITES_PER_TXN]}
            txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        save_state(vault, lock, "codex_imports.json", state)
        if writes:
            sources = load_sources(vault)
            entry = sources["sources"].get(SOURCE_NAME)
            if entry is None or "root_rel" not in entry:
                sources["sources"][SOURCE_NAME] = {"root_rel": RAW_ROOT, "kind": "sessions",
                                                   "added": (entry or {}).get("added") or now_iso(), "in_vault": True}
                save_state(vault, lock, "sources.json", sources)
    for info in to_index:
        session_index.index_conversation(vault, f"codex:{info['session_id']}", title=info["title"],
                                         cwd=info["cwd"] or "Codex", messages=info["messages"])
    return {"dry_run": False, **report, "source": SOURCE_NAME, "next": f"mnemoloci compile plan --source {SOURCE_NAME}"}
