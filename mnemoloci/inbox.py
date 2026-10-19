"""Inbox for images (phone screenshots) and other files.

Images reach the vault two ways: as files (e.g. attachments a Telegram channel
saved to disk) or straight out of a Claude Code transcript, where images attached
in the chat - also from the phone via Remote Control - are stored as base64.
Each image gets a sidecar note that embeds it in Obsidian; the agent looks at the
image and writes a transcription into the sidecar, which is then compiled like
any other source (as an external source, so its pages go through review).
"""

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from .core import txn
from .core.errors import MnemoError, ValidationError
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .markdown import render_page, split_frontmatter
from .secrets import redact
from .sessions import claude_dirs, clean_user_text
from .sources import load_sources
from .vault import now_iso, require_vault, save_state

RAW_ROOT = "raw/inbox"
SOURCE_NAME = "inbox"
SIDECAR_SCHEMA = "mnemoloci.inbox.v1"
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
               "image/heic": ".heic"}
EXT_TYPES = {v: k for k, v in IMAGE_TYPES.items()} | {".jpeg": "image/jpeg"}
MAX_BYTES = 20 * 1024 * 1024
PENDING = "_(da trascrivere: guarda l'immagine e usa `mnemoloci inbox transcribe`)_"


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    return None


def _existing_hashes(vault: Path) -> dict[str, str]:
    found = {}
    for note in (Path(vault) / RAW_ROOT).rglob("*.md"):
        try:
            fm, _ = split_frontmatter(note.read_text("utf-8"))
        except (OSError, ValidationError):
            continue
        if fm and fm.get("schema") == SIDECAR_SCHEMA:
            found[str(fm.get("sha256"))] = note.relative_to(vault).as_posix()
    return found


def _sidecar(image_name: str, sha: str, media: str, origin: str, note: str | None, context: str | None) -> str:
    fm = {"title": f"Screenshot {sha[:8]}", "schema": SIDECAR_SCHEMA, "image": image_name, "sha256": sha,
          "media_type": media, "origin": origin, "received": now_iso(), "status": "to-transcribe"}
    if note:
        fm["note"] = note
    body = f"# Screenshot {sha[:8]}\n\n![[{image_name}]]\n\n"
    if context:
        body += f"## Contesto\n\n{context}\n\n"
    body += f"## Trascrizione\n\n{PENDING}\n"
    return render_page(fm, body)


def _store(vault: Path, lock: VaultLock, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known = _existing_hashes(vault)
    writes, stored, skipped = [], [], []
    day = time.strftime("%Y-%m-%d")
    for item in items:
        data = item["data"]
        sha = sha256_bytes(data)
        if sha in known or any(s["sha256"] == sha for s in stored):
            skipped.append({"sha256": sha, "existing": known.get(sha), "reason": "duplicate"})
            continue
        media = _sniff(data)  # trust the bytes, never the file extension or declared type
        if media not in IMAGE_TYPES:
            skipped.append({"sha256": sha, "reason": f"unsupported type (declared {item.get('media_type')})"})
            continue
        if len(data) > MAX_BYTES:
            skipped.append({"sha256": sha, "reason": "larger than 20 MB"})
            continue
        base = f"{RAW_ROOT}/{day}/{sha[:12]}"
        image_rel = base + IMAGE_TYPES[media]
        staged = Path(vault) / ".mnemoloci" / "work" / "inbox" / Path(image_rel).name
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        writes.append({"path": image_rel, "action": "create", "content_file": str(staged), "sha256": sha})
        writes.append({"path": base + ".md", "action": "create",
                       "content": _sidecar(Path(image_rel).name, sha, media, item["origin"], item.get("note"), item.get("context"))})
        stored.append({"sha256": sha, "image": image_rel, "sidecar": base + ".md"})
    if writes:
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"inbox-{time.time_ns()}", "operation": "inbox-add",
                  "writes": writes}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        _ensure_source(vault, lock)
    return [*stored, *({"skipped": s} for s in skipped)]


def add_files(vault: Path, paths: list[str], *, note: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    items = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise MnemoError("NO_SUCH_FILE", f"{path} not found")
        items.append({"data": path.read_bytes(), "media_type": EXT_TYPES.get(path.suffix.lower()),
                      "origin": f"file:{path.name}", "note": note})
    with VaultLock(vault) as lock:
        return {"results": _store(vault, lock, items)}


def current_transcript(cwd: str) -> Path:
    """Most recently written transcript of the project folder for cwd."""
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))
    candidates = []
    for base in claude_dirs():
        projects = base / "projects"
        if not projects.is_dir():
            continue
        for d in projects.iterdir():
            if d.is_dir() and d.name.casefold() == folder.casefold():
                candidates += list(d.glob("*.jsonl"))
    if not candidates:
        raise MnemoError("NO_TRANSCRIPT", f"no Claude Code transcript found for {cwd}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def from_session(vault: Path, *, cwd: str | None = None, transcript: str | None = None, last: int = 5,
                 note: str | None = None) -> dict[str, Any]:
    """Save images the user attached in the chat (newest `last` ones)."""
    require_vault(vault)
    path = Path(transcript) if transcript else current_transcript(cwd or str(Path.cwd()))
    found: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"image"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            content = (rec.get("message") or {}).get("content")
            if rec.get("type") != "user" or rec.get("isSidechain") or not isinstance(content, list):
                continue
            text = " ".join(clean_user_text(b.get("text", "")) or "" for b in content
                            if isinstance(b, dict) and b.get("type") == "text").strip()
            for block in content:
                src = block.get("source") if isinstance(block, dict) and block.get("type") == "image" else None
                if not src or src.get("type") != "base64":
                    continue
                try:
                    data = base64.b64decode(src.get("data", ""), validate=True)
                except ValueError:
                    continue
                context = redact(text)[0][:1000] if text else None
                found.append({"data": data, "media_type": src.get("media_type"), "note": note,
                              "origin": f"session:{rec.get('sessionId', path.stem)}@{rec.get('timestamp', '')}",
                              "context": f"Messaggio che accompagnava l'immagine: {context}" if context else None})
    if not found:
        return {"transcript": str(path), "results": [], "note": "no attached images in this session"}
    with VaultLock(vault) as lock:
        return {"transcript": str(path), "results": _store(vault, lock, found[-max(1, last):])}


def pending(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    out = []
    for note in sorted((Path(vault) / RAW_ROOT).rglob("*.md")):
        try:
            fm, _ = split_frontmatter(note.read_text("utf-8", errors="replace"))
        except ValidationError:
            continue  # a sidecar broken by hand must not hide all the others (lint reports it)
        if fm and fm.get("schema") == SIDECAR_SCHEMA and fm.get("status") == "to-transcribe" and fm.get("image"):
            rel = note.relative_to(vault).as_posix()
            out.append({"id": note.stem, "sidecar": rel, "image": str(note.with_name(fm["image"])),
                        "received": fm.get("received"), "note": fm.get("note")})
    return {"pending": out}


def transcribe(vault: Path, item_id: str, text: str) -> dict[str, Any]:
    require_vault(vault)
    text, redactions = redact((text or "").strip())
    if not text:
        raise ValidationError("EMPTY_TEXT", "transcription is empty")
    if not str(item_id or "").isalnum():  # also keeps glob characters out of rglob
        raise MnemoError("BAD_ID", "invalid inbox id")
    matches = list((Path(vault) / RAW_ROOT).rglob(f"{item_id}.md"))
    if len(matches) != 1:
        raise MnemoError("NO_SUCH_ITEM", f"no inbox item {item_id}")
    note = matches[0]
    rel = note.relative_to(vault).as_posix()
    with VaultLock(vault) as lock:
        data = note.read_bytes()
        fm, body = split_frontmatter(data.decode("utf-8"))
        head, _, previous = body.partition("\n## Trascrizione\n")
        head = head.rstrip()
        earlier = ""
        if previous.strip() and PENDING.strip() not in previous:  # transcribed again: the earlier text (maybe corrected by hand) is kept below
            earlier = f"\n\n## Trascrizione precedente ({fm.get('transcribed') or '?'})\n\n{previous.strip()}\n"
        fm.update(status="transcribed", transcribed=now_iso(), redactions=redactions)
        content = render_page(fm, f"{head}\n\n## Trascrizione\n\n{text}\n{earlier}")
        bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"inbox-transcribe-{time.time_ns()}",
                  "operation": "inbox-transcribe",
                  "writes": [{"path": rel, "action": "replace", "content": content, "expected_sha256": sha256_bytes(data)}]}
        txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
    return {"transcribed": item_id, "sidecar": rel, "redactions": redactions,
            "next": "mnemoloci compile plan --source inbox"}


def ocr(vault: Path, item_id: str) -> dict[str, Any]:
    """Text extraction for a pending image, for when nobody needs to actually look at it (a
    scanned document, a screenshot that's mostly text) -- `transcribe` stays the way to go when
    the image needs a human or Claude's own vision to understand it, not just read its text."""
    require_vault(vault)
    if not str(item_id or "").isalnum():
        raise MnemoError("BAD_ID", "invalid inbox id")
    matches = list((Path(vault) / RAW_ROOT).rglob(f"{item_id}.md"))
    if len(matches) != 1:
        raise MnemoError("NO_SUCH_ITEM", f"no inbox item {item_id}")
    note = matches[0]
    fm, _ = split_frontmatter(note.read_text("utf-8"))
    image = note.with_name(fm["image"])
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise MnemoError("MISSING_EXTRA", "OCR needs pytesseract and Pillow: "
                         "python -m pip install \"mnemoloci[ocr]\"") from exc
    text = pytesseract.image_to_string(Image.open(image), lang="ita+eng").strip()
    if not text:
        raise ValidationError("OCR_EMPTY", "no text found in the image; use `inbox transcribe` instead")
    return transcribe(vault, item_id, text)


def _ensure_source(vault: Path, lock: VaultLock) -> None:
    state = load_sources(vault)
    entry = state["sources"].get(SOURCE_NAME)
    if entry is None or "root_rel" not in entry:
        state["sources"][SOURCE_NAME] = {"root_rel": RAW_ROOT, "kind": "inbox",
                                         "added": (entry or {}).get("added") or now_iso(), "in_vault": True}
        save_state(vault, lock, "sources.json", state)
