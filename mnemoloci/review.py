"""Review queue: pages held by policy wait here until a human approves or rejects.

Approval is pinned to the exact content hash the reviewer saw and re-verified
under the vault lock. Rejected and approved candidates are archived (renamed),
never deleted.
"""

import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from .core import txn
from .core.atomic import atomic_json
from .core.errors import ConflictError, MnemoError
from .core.hashing import sha256_bytes, sha256_file
from .core.lock import VaultLock
from .index import update as index_update
from .markdown import split_frontmatter
from .publish import build_writes, stamp_now
from .vault import engine, now_iso, require_vault

CANDIDATE_SCHEMA = "mnemoloci.candidate.v1"


def _dir(vault: Path) -> Path:
    return engine(vault) / "candidates"


def _content_hash(content: str, extra: list[dict[str, Any]]) -> str:
    """What the reviewer approves: the page plus any extra write (e.g. the stub of a merged page)."""
    if not extra:
        return sha256_bytes(content.encode("utf-8"))
    joined = content + "".join(f"\n\0{e['target_path']}\0{e['content']}" for e in extra)
    return sha256_bytes(joined.encode("utf-8"))


def save_candidate(
    vault: Path, lock: VaultLock, *, run_id: str, slug: str, target_path: str,
    base_sha256: str | None, content: str, reasons: list[dict[str, str]],
    extra_writes: list[dict[str, Any]] | None = None,
) -> str:
    lock.assert_held()
    extra = list(extra_writes or [])
    content_sha = _content_hash(content, extra)
    cid = f"{slug}-{content_sha[:10]}"
    record = {
        "schema": CANDIDATE_SCHEMA, "id": cid, "slug": slug, "target_path": target_path,
        "base_sha256": base_sha256, "content": content, "content_sha256": content_sha,
        "reasons": reasons, "run_id": run_id, "created": now_iso(),
    }
    if extra:
        record["extra_writes"] = extra  # [{target_path, base_sha256, content}], applied in the same transaction
    atomic_json(_dir(vault) / f"{cid}.json", record)
    return cid


def _load(vault: Path, cid: str) -> dict[str, Any]:
    if not cid or any(ch in cid for ch in "/\\:") or cid.startswith("."):
        raise MnemoError("BAD_CANDIDATE_ID", f"invalid id {cid!r}")
    path = _dir(vault) / f"{cid}.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise MnemoError("NO_SUCH_CANDIDATE", f"no pending candidate {cid}") from None
    if data.get("schema") != CANDIDATE_SCHEMA or data.get("id") != cid:
        raise MnemoError("CORRUPT_CANDIDATE", f"candidate {cid} is malformed")
    if _content_hash(data["content"], data.get("extra_writes") or []) != data["content_sha256"]:
        raise MnemoError("CORRUPT_CANDIDATE", f"candidate {cid} content does not match its hash")
    return data


def list_candidates(vault: Path) -> list[dict[str, Any]]:
    require_vault(vault)
    out = []
    for path in sorted(_dir(vault).glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        out.append({"id": data["id"], "slug": data["slug"], "reasons": [r["code"] for r in data["reasons"]],
                    "created": data["created"], "target_path": data["target_path"]})
    return out


def show(vault: Path, cid: str) -> dict[str, Any]:
    require_vault(vault)
    data = _load(vault, cid)
    out = {k: data[k] for k in ("id", "slug", "target_path", "reasons", "content_sha256", "content", "run_id", "created")}
    if data.get("extra_writes"):
        out["extra_writes"] = [{"target_path": e["target_path"], "content": e["content"]} for e in data["extra_writes"]]
    return out


def _archive(vault: Path, data: dict[str, Any], outcome: str, **extra: Any) -> None:
    archive = _dir(vault) / "archive"
    archive.mkdir(exist_ok=True)
    source = _dir(vault) / f"{data['id']}.json"
    atomic_json(archive / f"{outcome}-{data['id']}.json", {**data, "outcome": outcome, "decided": now_iso(), **extra})
    os.replace(source, archive / f"{outcome}-{data['id']}.original.json")


def approve(vault: Path, cid: str, content_sha256: str) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        data = _load(vault, cid)
        if not hmac.compare_digest(data["content_sha256"], str(content_sha256)):
            raise ConflictError("CONTENT_CHANGED", "candidate content differs from what you reviewed; show it again")
        fm, body = split_frontmatter(data["content"])
        if not fm or not body.strip():
            raise MnemoError("CORRUPT_CANDIDATE", "candidate page has no frontmatter or body")
        pages = {data["target_path"]: data["content"]}
        bases: dict[str, str | None] = {}
        for item in [{"target_path": data["target_path"], "base_sha256": data["base_sha256"]},
                     *(data.get("extra_writes") or [])]:
            target = Path(vault) / item["target_path"]
            current = sha256_file(target) if target.is_file() else None
            wanted = data["content"] if item["target_path"] == data["target_path"] else item.get("content", "")
            if current != item["base_sha256"] and current != sha256_bytes(wanted.encode("utf-8")):
                raise ConflictError("CANDIDATE_STALE", f"{item['target_path']} changed since this candidate was made")
            bases[item["target_path"]] = item["base_sha256"]
        for extra in data.get("extra_writes") or []:
            pages[extra["target_path"]] = extra["content"]
        if all((Path(vault) / p).is_file() and sha256_file(Path(vault) / p) == sha256_bytes(c.encode("utf-8"))
               for p, c in pages.items()):
            # an earlier approve wrote the pages and was stopped before archiving: finish that one
            _archive(vault, data, "approved")
            index_update(vault, list(pages), lock)
            return {"approved": cid, "path": data["target_path"], "also": sorted(set(pages) - {data["target_path"]}),
                    "transaction": None, "note": "already applied; candidate archived now"}
        bundle = {
            "schema": txn.BUNDLE_SCHEMA,
            "operation_id": f"review-{sha256_bytes(cid.encode('utf-8'))[:10]}-{time.time_ns()}",  # any slug length
            "operation": "review-approve",
            "writes": build_writes(vault, pages, f"review approvata [[{data['slug']}]]", stamp_now(), bases),
        }
        plan = txn.prepare(vault, bundle)
        result = txn.apply(vault, bundle, plan.approval_sha256, lock=lock)
        _archive(vault, data, "approved")
        index_update(vault, list(pages), lock)
    return {"approved": cid, "path": data["target_path"], "also": sorted(set(pages) - {data["target_path"]}),
            "transaction": result["operation_id"]}


def reject(vault: Path, cid: str, reason: str) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault):
        data = _load(vault, cid)
        _archive(vault, data, "rejected", reason=reason)
    return {"rejected": cid, "note": "archived; it will not come back unless its sources change"}
