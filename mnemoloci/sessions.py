"""Import existing Claude Code session transcripts as clean, redacted Markdown
digests under raw/sessions/, ready for the normal compile pipeline.

Fixes the gaps found in other tools: list-form prompts are kept (about half of the
prompts on this machine), assistant replies split over several JSONL lines are
merged by message.id, subagent/meta/compact/notification noise is dropped, IDE
context tags are stripped from otherwise-real prompts, re-imports never duplicate.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .core import txn
from .core.hashing import sha256_bytes
from .core.lock import VaultLock
from .markdown import slugify
from .secrets import redact
from .sources import load_sources
from .vault import load_state, now_iso, require_vault, save_state

STATE_SCHEMA = "mnemoloci.sessions.v1"
SOURCE_NAME = "claude-sessions"
RAW_ROOT = "raw/sessions"
PART_CHARS = 120_000
MESSAGE_CHARS = 8_000
WRITES_PER_TXN = 400

DROP_IF_STARTS = ("<task-notification", "<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>")
STRIP_TAGS = ("ide_opened_file", "ide_selection", "system-reminder", "user-prompt-submit-hook",
              "ide_diagnostics", "command-name", "command-message", "command-args")
_OPEN = re.compile(r"<(%s)(?:\s[^>]{0,500})?>" % "|".join(STRIP_TAGS))
_COMMAND = re.compile(r"<command-(name|args)>(.*?)</command-\1>", re.DOTALL)


def _strip_tags(text: str) -> str:
    """Remove <tag ...>...</tag> blocks. Linear: an opening tag with no closing tag after it is
    left alone without searching again (a regex with .*? went quadratic on unclosed tags)."""
    last_close = {tag: text.rfind(f"</{tag}>") for tag in STRIP_TAGS}
    out, pos = [], 0
    for m in _OPEN.finditer(text):
        if m.start() < pos:
            continue  # inside a block already removed
        tag = m.group(1)
        if last_close[tag] < m.end():
            continue
        close = text.find(f"</{tag}>", m.end())
        out.append(text[pos:m.start()])
        pos = close + len(tag) + 3
    out.append(text[pos:])
    return "".join(out)


def _slash_command(text: str) -> str | None:
    """A slash command as typed: `/review fix the login` (Claude Code stores it as tags)."""
    found = {k: v.strip() for k, v in _COMMAND.findall(text[:20_000])}
    name, args = found.get("name"), found.get("args", "")
    if not name or not args:
        return None  # a bare /model or /clear says nothing about the work
    return f"{name if name.startswith('/') else '/' + name} {args}"


def claude_dirs(explicit: list[str] | None = None) -> list[Path]:
    if explicit:
        return [Path(p) for p in explicit]
    found = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    for candidate in ([Path(env)] if env else []) + [Path.home() / ".claude"]:
        if candidate.is_dir() and candidate.resolve() not in [f.resolve() for f in found]:
            found.append(candidate)
    return found


def clean_user_text(text: str) -> str | None:
    stripped = text.lstrip()
    if stripped.startswith(DROP_IF_STARTS):
        return None
    if stripped.startswith(("<command-name>", "<command-message>", "<command-args>")):
        return _slash_command(stripped)
    cleaned = _strip_tags(text).strip()
    return cleaned or None


def _user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return clean_user_text(content)
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None  # a tool result turn, not something the user typed
        if block.get("type") == "text":
            cleaned = clean_user_text(str(block.get("text") or ""))
            if cleaned:
                parts.append(cleaned)
        elif block.get("type") in ("image", "document"):
            parts.append(f"[{'immagine' if block['type'] == 'image' else 'documento'} allegato]")
    return "\n\n".join(parts) or None


def parse_transcript(path: Path) -> dict[str, Any]:
    """Return {session_id, cwd, git_branch, title, messages:[{role, ts, text}]}."""
    info: dict[str, Any] = {"session_id": path.stem, "cwd": None, "git_branch": None, "title": None}
    messages: list[dict[str, Any]] = []
    assistant_at: dict[str, int] = {}
    typed: set[str] = set()
    with open(path, encoding="utf-8-sig", errors="replace") as handle:  # -sig: a BOM must not hide line 1
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "ai-title" and not info["title"]:
                info["title"] = rec.get("aiTitle") if isinstance(rec.get("aiTitle"), str) else None
                continue
            if kind == "attachment" and not rec.get("isSidechain") and not rec.get("isMeta"):
                # a prompt typed while Claude was busy is stored only as a queued command
                att = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
                origin = att.get("origin") if isinstance(att.get("origin"), dict) else {}
                if att.get("type") == "queued_command" and att.get("commandMode") == "prompt" and origin.get("kind") != "peer":
                    text = _user_text(att.get("prompt"))
                    if text:
                        messages.append({"role": "user", "ts": str(rec.get("timestamp") or att.get("timestamp") or ""),
                                         "text": text, "_queued": True})
                continue
            if kind not in ("user", "assistant") or rec.get("isSidechain"):
                continue
            info["cwd"] = info["cwd"] or rec.get("cwd")
            info["git_branch"] = info["git_branch"] or rec.get("gitBranch")
            info["session_id"] = rec.get("sessionId") or info["session_id"]
            ts = str(rec.get("timestamp") or "")
            message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            if kind == "user":
                if rec.get("isMeta") or rec.get("isCompactSummary"):
                    continue
                text = _user_text(message.get("content"))
                if text:
                    messages.append({"role": "user", "ts": ts, "text": text})
                    typed.add(text)
                continue
            content = message.get("content") if isinstance(message.get("content"), list) else []
            texts = [str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n\n".join(t.strip() for t in texts if t.strip())
            if not text:
                continue
            mid = message.get("id")
            if mid and mid in assistant_at:
                messages[assistant_at[mid]]["text"] += "\n\n" + text
            else:
                if mid:
                    assistant_at[mid] = len(messages)
                messages.append({"role": "assistant", "ts": ts, "text": text})
    # a queued prompt that was also recorded as a normal user turn is kept once
    info["messages"] = [{k: v for k, v in m.items() if k != "_queued"} for m in messages
                        if not (m.get("_queued") and m["text"] in typed)]
    # Redacted like the messages are when written: titles reach headings, folder names and the index
    if info["title"]:
        info["title"] = redact(str(info["title"]))[0]
    return info


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def edited_files(path: Path) -> list[str]:
    """Files the main agent changed with its edit tools, ordered by their last change."""
    seen: dict[str, None] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_use"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
                continue
            message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    target = args.get("file_path") or args.get("notebook_path")
                    if isinstance(target, str) and target:
                        seen.pop(target, None)
                        seen[target] = None
    return list(seen)


def local_iso(ts: str) -> str:
    """UTC transcript timestamp -> local 'YYYY-MM-DDTHH:MM:SS' (the format memory notes use)."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ts[:19]


def fmt_ts(ts: str) -> str:
    """Transcript timestamps are UTC ISO strings; show local time."""
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16].replace("T", " ")


def render_parts(info: dict[str, Any], source: str = "claude-code") -> list[str]:
    """Deterministic split: earlier parts never change as a session grows."""
    title = info.get("title") or "Untitled session"
    parts, current, size = [], [], 0
    for msg in info["messages"]:
        text, _ = redact(msg["text"])
        if len(text) > MESSAGE_CHARS:
            text = text[:MESSAGE_CHARS] + f"\n\n…[troncato: {len(text) - MESSAGE_CHARS} caratteri]"
        who = "Utente" if msg["role"] == "user" else info.get("assistant_name", "Claude")
        block = f"### {fmt_ts(msg['ts'])} · {who}\n\n{text}\n"
        if current and size + len(block) > PART_CHARS:
            parts.append(current)
            current, size = [], 0
        current.append((msg["ts"], block))
        size += len(block)
    if current:
        parts.append(current)
    rendered = []
    for n, blocks in enumerate(parts, 1):
        header = (
            "---\n"
            f"session_id: {info['session_id']}\n"
            f"project: {json.dumps(info.get('cwd') or '')}\n"
            f"git_branch: {json.dumps(info.get('git_branch') or '')}\n"
            f"part: {n}\n"
            f"from: {json.dumps(blocks[0][0])}\n"
            f"source: {source}\n"
            "---\n\n"
            f"# {title} — parte {n}\n\n"
        )
        rendered.append(header + "\n".join(b for _, b in blocks))
    return rendered


def _project_slug(cwd: str | None, folder: str) -> str:
    return slugify(cwd or folder)[:60]


def _iter_transcripts(dirs: list[Path]) -> Iterator[tuple[Path, Path]]:
    for base in dirs:
        for path in sorted((base / "projects").glob("*/*.jsonl")):
            yield base, path


def _matches(cwd: str | None, folder: str, include: list[str], exclude: list[str]) -> bool:
    hay = f"{cwd or ''} {folder}".casefold()
    if include and not any(s.casefold() in hay for s in include):
        return False
    return not any(s.casefold() in hay for s in exclude)


def import_sessions(
    vault: Path,
    *,
    claude_dir: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    since: str | None = None,
    dry_run: bool = False,
    only: str | Path | None = None,
) -> dict[str, Any]:
    """only: import just this one transcript instead of scanning every Claude dir -- the only shape
    a time-boxed hook (SessionEnd/PreCompact) can afford. Project memory files and the pruned-history
    sweep are skipped in that mode: with a single transcript marked "seen", every other session would
    look deleted and get staged as a bogus "pruned from history" entry."""
    require_vault(vault)
    vault = Path(vault)
    dirs = claude_dirs(claude_dir)
    include, exclude = include or [], exclude or []
    single_base: Path | None = None
    if only is not None:
        only = Path(only).resolve()
        single_base = next((d for d in dirs if (d / "projects").resolve() == only.parent.parent.resolve()), None)
        if single_base is None or not only.is_file():
            empty = {"scanned": 0, "unchanged": 0, "imported": 0, "empty": 0, "filtered": 0, "memory_files": 0,
                     "pruned_sessions_from_history": 0, "files_to_write": 0}
            return {"dry_run": dry_run, **empty, "skipped": "transcript not under a configured claude dir"}
    with VaultLock(vault) as lock:
        state = load_state(vault, "sessions.json", STATE_SCHEMA, {"files": {}, "sessions": {}})
        writes: list[dict[str, Any]] = []
        report = {"scanned": 0, "unchanged": 0, "imported": 0, "empty": 0, "filtered": 0, "memory_files": 0}
        folder_cwd: dict[tuple[str, str], str] = {}  # (claude_dir, project folder name) -> last known cwd:
        # the same folder name can exist under two Claude config dirs (e.g. after moving
        # CLAUDE_CONFIG_DIR), each with its own session history, so the cache must not mix them
        seen_sessions: set[str] = set()
        staged_session_ids: set[str] = set()

        def stage(rel: str, content: str) -> None:
            target = vault / rel
            data = content.encode("utf-8")
            if target.is_file():
                current = target.read_bytes()
                if current == data:
                    return
                writes.append({"path": rel, "action": "replace", "content": content, "expected_sha256": sha256_bytes(current)})
            else:
                writes.append({"path": rel, "action": "create", "content": content})

        transcripts = [(single_base, only)] if only is not None else _iter_transcripts(dirs)
        for base, path in transcripts:
            report["scanned"] += 1
            key = str(path.resolve())
            st = path.stat()
            known = state["files"].get(key)
            if known and known["size"] == st.st_size and known["mtime_ns"] == st.st_mtime_ns:
                report["unchanged"] += 1
                seen_sessions.add(known.get("session_id", path.stem))
                if known.get("cwd"):
                    folder_cwd.setdefault((str(base), path.parent.name), known["cwd"])
                continue
            info = parse_transcript(path)
            seen_sessions.add(info["session_id"])
            if info["cwd"]:
                folder_cwd.setdefault((str(base), path.parent.name), info["cwd"])
            if not _matches(info["cwd"], path.parent.name, include, exclude):
                report["filtered"] += 1
                continue
            first_ts = next((m["ts"] for m in info["messages"] if m["ts"]), "")
            if since and first_ts and first_ts[:10] < since:
                report["filtered"] += 1
                continue
            if not info["messages"]:
                report["empty"] += 1
            elif info["session_id"] in staged_session_ids:
                # the same session transcript exists under more than one Claude config dir
                # (e.g. a stale ~/.claude alongside CLAUDE_CONFIG_DIR): stage it once
                report["duplicate_transcripts"] = report.get("duplicate_transcripts", 0) + 1
            else:
                folder = f"{RAW_ROOT}/{_project_slug(info['cwd'], path.parent.name)}/{(first_ts[:10] or 'undated')}-{info['session_id'][:8]}"
                for n, content in enumerate(render_parts(info), 1):
                    stage(f"{folder}/part-{n:03d}.md", content)
                report["imported"] += 1
                staged_session_ids.add(info["session_id"])
                state["sessions"][info["session_id"]] = {
                    "folder": folder, "title": info["title"], "cwd": info["cwd"], "messages": len(info["messages"]),
                    "imported": now_iso(),
                }
            state["files"][key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "session_id": info["session_id"], "cwd": info["cwd"]}

        if only is None:
            for base in dirs:
                for mem in sorted((base / "projects").glob("*/memory/*.md")):
                    cwd = folder_cwd.get((str(base), mem.parent.parent.name))
                    if not _matches(cwd, mem.parent.parent.name, include, exclude):
                        continue
                    text, _ = redact(mem.read_text("utf-8", errors="replace"))
                    if text.strip():
                        stage(f"{RAW_ROOT}/{_project_slug(cwd, mem.parent.parent.name)}/memory/{slugify(mem.stem)}.md", text)
                        report["memory_files"] += 1

            pruned = _pruned_from_history(dirs, seen_sessions, include, exclude)
            for sid, info in pruned.items():
                folder = f"{RAW_ROOT}/{_project_slug(info['cwd'], '')}/{info['messages'][0]['ts'][:10]}-{sid[:8]}-cronologia"
                for n, content in enumerate(render_parts(info), 1):
                    stage(f"{folder}/part-{n:03d}.md", content)
            report["pruned_sessions_from_history"] = len(pruned)
        else:
            report["pruned_sessions_from_history"] = 0
        report["files_to_write"] = len(writes)

        if dry_run:
            return {"dry_run": True, **report}
        for start in range(0, len(writes), WRITES_PER_TXN):
            chunk = writes[start : start + WRITES_PER_TXN]
            bundle = {"schema": txn.BUNDLE_SCHEMA, "operation_id": f"sessions-{int(time.time() * 1000)}-{start}",
                      "operation": "sessions-import", "writes": chunk}
            txn.apply(vault, bundle, txn.prepare(vault, bundle).approval_sha256, lock=lock)
        save_state(vault, lock, "sessions.json", state)
        _ensure_source(vault, lock)
    return {"dry_run": False, **report, "source": SOURCE_NAME,
            "next": f"mnemoloci compile plan --source {SOURCE_NAME}"}


def _pruned_from_history(dirs: list[Path], seen: set[str], include: list[str], exclude: list[str]) -> dict[str, Any]:
    """Sessions whose transcript was deleted still have their prompts in history.jsonl."""
    out: dict[str, Any] = {}
    for base in dirs:
        hist = base / "history.jsonl"
        if not hist.is_file():
            continue
        for line in hist.read_text("utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            sid, text = rec.get("sessionId"), clean_user_text(str(rec.get("display", "")))
            if not sid or sid in seen or not text or not _matches(rec.get("project"), "", include, exclude):
                continue
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(rec.get("timestamp", 0) / 1000))
            entry = out.setdefault(sid, {"session_id": sid, "cwd": rec.get("project"), "git_branch": None,
                                         "title": "Prompt from history (transcript no longer available)", "messages": []})
            entry["messages"].append({"role": "user", "ts": ts, "text": text})
    for entry in out.values():
        entry["messages"].sort(key=lambda m: m["ts"])
    return out


def _ensure_source(vault: Path, lock: VaultLock) -> None:
    state = load_sources(vault)
    entry = state["sources"].get(SOURCE_NAME)
    if entry is None or "root_rel" not in entry:
        state["sources"][SOURCE_NAME] = {"root_rel": RAW_ROOT, "kind": "sessions",
                                         "added": (entry or {}).get("added") or now_iso(), "in_vault": True}
        save_state(vault, lock, "sources.json", state)
