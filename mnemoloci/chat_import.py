"""Import chat exports from ChatGPT and claude.ai.

Both services export a `conversations.json`, alone or inside the downloaded zip:
- ChatGPT: each conversation is a tree (`mapping`); the conversation the user saw
  is the branch from `current_node` back to the root, so edits and regenerated
  answers that were abandoned are left out;
- claude.ai: `chat_messages` in order, with sender human/assistant.

Conversations become redacted Markdown digests under raw/chats/<provider>/, the
same format as Claude Code session digests, compiled by the normal pipeline
(source `chats`), and are added to the session search index. Re-importing is
incremental: an unchanged conversation is skipped, a continued one is rewritten.
"""

import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import session_index
from .core import txn
from .core.errors import MnemoError, ValidationError
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .markdown import slugify
from .secrets import redact
from .sessions import WRITES_PER_TXN, render_parts
from .sources import load_sources
from .vault import load_state, now_iso, require_vault, save_state

STATE_SCHEMA = "mnemoloci.chat-imports.v1"
SOURCE_NAME = "chats"
RAW_ROOT = "raw/chats"
MAX_EXPORT_BYTES = 1024 * 1024 * 1024
PROVIDERS = {"chatgpt": "ChatGPT", "claude-ai": "Claude"}
ATTACHMENT_CHARS = 4_000
_UNSUPPORTED = "This block is not supported on your current device"


def _iso(value: Any) -> str:
    """Epoch seconds (ChatGPT) or ISO text (claude.ai) -> UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 1e11:  # milliseconds
            value = value / 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError, OverflowError):  # NaN, out of range
            return ""
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return value
    return ""


# Large ChatGPT exports are sharded (conversations-000.json, -001, ...). shared_conversations.json
# only holds title stubs and must never be read, hence the exact pattern.
_SHARD = re.compile(r"^conversations(-\d+)?\.json$")


def _as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        data = data["conversations"]
    if not isinstance(data, list):
        raise ValidationError("NOT_AN_EXPORT", "conversations.json must be a list of conversations")
    return [c for c in data if isinstance(c, dict)]


def load_export(path: str) -> list[dict[str, Any]]:
    """A zip as downloaded, a folder where it was unpacked, or one conversations*.json file."""
    target = Path(path)
    if not target.exists():
        raise MnemoError("NO_SUCH_FILE", f"{target} not found")
    try:
        if target.is_dir():
            files = sorted(p for p in target.rglob("*.json") if _SHARD.match(p.name))
            if not files:
                raise ValidationError("NOT_AN_EXPORT", "no conversations.json in this folder")
            return [c for f in files for c in _as_list(json.loads(f.read_text("utf-8")))]
        if target.stat().st_size > MAX_EXPORT_BYTES:
            raise ValidationError("TOO_LARGE", "export larger than 1 GB")
        if zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as archive:
                names = sorted(n for n in archive.namelist() if _SHARD.match(Path(n).name))
                if not names:
                    raise ValidationError("NOT_AN_EXPORT", "the zip has no conversations.json")
                return [c for n in names
                        for c in _as_list(json.load(io.TextIOWrapper(archive.open(n), encoding="utf-8")))]
        return _as_list(json.loads(target.read_text("utf-8")))
    except (ValueError, zipfile.BadZipFile) as exc:
        raise ValidationError("NOT_AN_EXPORT", f"cannot read the export: {exc}") from exc


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _chatgpt(conv: dict[str, Any]) -> dict[str, Any]:
    mapping = conv.get("mapping") if isinstance(conv.get("mapping"), dict) else {}
    mapping = {k: v for k, v in mapping.items() if isinstance(v, dict)}  # malformed nodes are skipped
    chain, node, seen = [], conv.get("current_node"), set()
    while isinstance(node, str) and node in mapping and node not in seen:
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    chain.reverse()

    def created(n: dict[str, Any]) -> float:
        value = _obj(n.get("message")).get("create_time")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    if not chain:  # no current_node: fall back to creation order
        chain = sorted(mapping.values(), key=created)
    messages = []
    for item in chain:
        msg = _obj(item.get("message"))
        role = _obj(msg.get("author")).get("role")
        content = _obj(msg.get("content"))
        meta = _obj(msg.get("metadata"))
        if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation") \
                or meta.get("is_user_system_message"):
            continue
        if role == "assistant" and msg.get("recipient", "all") != "all":
            continue  # a command to a tool (canvas, memory, browsing), not a reply to the user
        if content.get("content_type") not in ("text", "multimodal_text"):
            continue  # code, execution output, browsing, thoughts, custom instructions
        parts = []
        for part in content.get("parts") if isinstance(content.get("parts"), list) else []:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str) and part["text"].strip():
                    parts.append(part["text"])  # e.g. the transcription of a voice message
                else:
                    parts.append("[immagine allegata]" if "image" in str(part.get("content_type", "")) else "[allegato]")
        text = "\n\n".join(p for p in parts if p.strip()).strip()
        if text:
            messages.append({"role": role, "ts": _iso(msg.get("create_time")), "text": text})
    return {"id": str(conv.get("conversation_id") or conv.get("id") or ""), "title": conv.get("title"),
            "updated": _iso(conv.get("update_time")), "messages": messages}


def _claude_ai(conv: dict[str, Any]) -> dict[str, Any]:
    messages = []
    for msg in conv.get("chat_messages") if isinstance(conv.get("chat_messages"), list) else []:
        if not isinstance(msg, dict):
            continue
        role = {"human": "user", "assistant": "assistant"}.get(msg.get("sender"))
        if not role:
            continue
        # Newer exports leave `text` empty (or a "not supported on your device" placeholder) and
        # keep the body in the text blocks of `content`; thinking/tool blocks are left out.
        blocks = "\n\n".join(str(b["text"]) for b in (msg.get("content") if isinstance(msg.get("content"), list) else [])
                             if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
        fallback = msg.get("text") if isinstance(msg.get("text"), str) else ""
        text = blocks or ("" if fallback.startswith(_UNSUPPORTED) else fallback)
        extras = []
        for a in msg.get("attachments") if isinstance(msg.get("attachments"), list) else []:
            if isinstance(a, dict):
                name = a.get("file_name") or "file"
                body = str(a.get("extracted_content") or "").strip()
                extras.append(f"[allegato: {name}]" + (f"\n\n{body[:ATTACHMENT_CHARS]}" if body else ""))
        files = msg.get("files") if isinstance(msg.get("files"), list) else []
        extras += [f"[file: {f.get('file_name') or 'file'}]" for f in files if isinstance(f, dict)]
        text = "\n\n".join(x for x in [text.strip(), *extras] if x)
        if text:
            messages.append({"role": role, "ts": _iso(msg.get("created_at")), "text": text})
    return {"id": str(conv.get("uuid") or ""), "title": conv.get("name"), "updated": _iso(conv.get("updated_at")),
            "messages": messages}


def _fingerprint(messages: list[dict[str, Any]]) -> str:
    return sha256_bytes(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _extends(messages: list[dict[str, Any]], known: dict[str, Any]) -> bool:
    """True when `messages` is the imported conversation with new messages appended."""
    count = int(known.get("messages") or 0)
    return len(messages) >= count and _fingerprint(messages[:count]) == known.get("fingerprint")


def detect(conversations: list[dict[str, Any]]) -> str:
    sample = conversations[:20]
    if any("mapping" in c for c in sample):
        return "chatgpt"
    if any("chat_messages" in c for c in sample):
        return "claude-ai"
    raise ValidationError("NOT_AN_EXPORT", "not a ChatGPT or claude.ai conversations.json")


def import_export(vault: Path, path: str, *, include: list[str] | None = None, exclude: list[str] | None = None,
                  since: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    require_vault(vault)
    vault = Path(vault)
    conversations = load_export(path)
    if not conversations:
        return {"provider": None, "conversations": 0, "note": "the export is empty"}
    provider = detect(conversations)
    parse = _chatgpt if provider == "chatgpt" else _claude_ai
    include = [s.casefold() for s in include or []]
    exclude = [s.casefold() for s in exclude or []]
    report = {"provider": provider, "conversations": len(conversations), "imported": 0, "unchanged": 0, "empty": 0,
              "filtered": 0, "messages": 0}
    with VaultLock(vault) as lock:
        state = load_state(vault, "chat_imports.json", STATE_SCHEMA, {"conversations": {}})
        writes: list[dict[str, Any]] = []
        to_index: list[tuple[str, dict[str, Any]]] = []
        report["errors"] = 0
        for conv in conversations:
            try:
                chat = parse(conv)
            except (TypeError, ValueError, AttributeError, KeyError):  # one odd conversation must not stop the rest
                report["errors"] += 1
                continue
            title = redact(str(chat["title"] or "Conversazione senza titolo"))[0]
            if not chat["id"]:
                report["empty"] += 1
                continue
            if (include and not any(s in title.casefold() for s in include)) or any(s in title.casefold() for s in exclude):
                report["filtered"] += 1
                continue
            first = next((m["ts"] for m in chat["messages"] if m["ts"]), "")
            if since and first and first[:10] < since:
                report["filtered"] += 1
                continue
            if not chat["messages"]:
                report["empty"] += 1
                continue
            key = f"{provider}:{chat['id']}"
            fingerprint = _fingerprint(chat["messages"])
            known = state["conversations"].get(key)
            if known and known["fingerprint"] == fingerprint:
                report["unchanged"] += 1
                continue
            folder = known["folder"] if known else (
                f"{RAW_ROOT}/{provider}/{first[:10] or 'undated'}-{slugify(title)[:40]}-{chat['id'][:8]}")
            if known and not _extends(chat["messages"], known):
                # not the same conversation grown longer (an older export, or edited/branched in the app):
                # imported next to the one already here, which is never overwritten
                if known.get("messages", 0) > len(chat["messages"]) or any(
                        v.get("fingerprint") == fingerprint for v in known.get("variants", [])):
                    report["unchanged"] += 1  # an older copy of what is already imported
                    continue
                variant = f"{folder}-v{fingerprint[:6]}"
                known.setdefault("variants", []).append({"folder": known["folder"], "fingerprint": known["fingerprint"],
                                                         "messages": known.get("messages", 0)})
                folder = variant
            info = {"session_id": key, "cwd": PROVIDERS[provider], "git_branch": None, "title": title,
                    "messages": chat["messages"], "assistant_name": PROVIDERS[provider]}
            for n, content in enumerate(render_parts(info, source=provider), 1):
                rel = f"{folder}/part-{n:03d}.md"
                target = vault / rel
                if target.is_file():
                    current = target.read_bytes()
                    if current != content.encode("utf-8"):
                        writes.append({"path": rel, "action": "replace", "content": content,
                                       "expected_sha256": sha256_bytes(current)})
                else:
                    writes.append({"path": rel, "action": "create", "content": content})
            state["conversations"][key] = {"folder": folder, "title": title, "fingerprint": fingerprint,
                                           "messages": len(chat["messages"]), "imported": now_iso(),
                                           **({"variants": known["variants"]} if known and known.get("variants") else {})}
            to_index.append((key, info))
            report["imported"] += 1
            report["messages"] += len(chat["messages"])
        report["files_to_write"] = len(writes)
        if dry_run:
            return {"dry_run": True, **report}
        for start in range(0, len(writes), WRITES_PER_TXN):
            bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"chats-{time.time_ns()}-{start}",
                      "operation": "chat-import", "writes": writes[start:start + WRITES_PER_TXN]}
            txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        save_state(vault, lock, "chat_imports.json", state)
        if writes:
            sources = load_sources(vault)
            entry = sources["sources"].get(SOURCE_NAME)
            if entry is None or "root_rel" not in entry:
                sources["sources"][SOURCE_NAME] = {"root_rel": RAW_ROOT, "kind": "sessions",
                                                   "added": (entry or {}).get("added") or now_iso(), "in_vault": True}
                save_state(vault, lock, "sources.json", sources)
    for key, info in to_index:  # derived search index, outside the vault
        session_index.index_conversation(vault, key, title=info["title"], cwd=info["cwd"], messages=info["messages"])
    return {"dry_run": False, **report, "source": SOURCE_NAME, "next": f"mnemoloci compile plan --source {SOURCE_NAME}"}
