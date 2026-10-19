"""Local HTTP API + static web UI for the desktop app.

Security model (the API can approve pages and edit goals):
- binds 127.0.0.1 only;
- a random bearer token per launch, required on every /api call; the page gets it
  through the URL fragment, which browsers never send to servers;
- the Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding);
- no CORS headers, JSON bodies only.
"""

import hmac
import json
import secrets
import socket
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from . import source_map
from . import (answers, backup, clips, connectors, digest, doctor, editor, features, graph_analysis, local_extract, maintain, quality, skill_curator, tags, weekly, index, inbox, instincts, remote, lint, memory, personal, projects, review,
               session_index, settings, skill_factory, skill_registry, sources, tools)
from .pages import iter_pages
from .config import resolve_vault, switch_vault, vault_info
from .core.errors import MnemoError
from .core.paths import resolve_in_vault, validate_relpath
from .markdown import split_frontmatter
from .mcp_server import read_page
from .vault import (CLOUD_EMBEDDING_PROVIDERS, get_api_key, load_config, require_vault, set_api_key, set_embeddings,
                    set_local_extraction)

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 2 * 1024 * 1024  # JSON bodies only: the largest is a page edit or a pasted text
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def graph(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    if not index.ready(vault):
        index.rebuild(vault)
    with closing(sqlite3.connect(index.db_path(vault))) as conn:
        pages = conn.execute("SELECT path, slug, title, kind, confidence FROM pages").fetchall()
        slugs = {p[1] for p in pages}
        links = conn.execute("SELECT p.slug, l.dst_slug FROM links l JOIN pages p ON p.path = l.src_path").fetchall()
    return {
        "nodes": [{"id": s, "path": p, "title": t, "kind": k or "memory", "confidence": c} for p, s, t, k, c in pages],
        "links": [{"source": a, "target": b} for a, b in sorted(set(links)) if b in slugs and a != b],
    }


def inbox_items(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    items = []
    for note in sorted((Path(vault) / inbox.RAW_ROOT).rglob("*.md"), reverse=True):
        fm, _ = split_frontmatter(note.read_text("utf-8"))
        if fm and fm.get("schema") == inbox.SIDECAR_SCHEMA:
            items.append({"id": note.stem, "sidecar": note.relative_to(vault).as_posix(), "status": fm.get("status"),
                          "received": fm.get("received"), "note": fm.get("note"), "origin": fm.get("origin")})
    return {"items": items}


def inbox_image(vault: Path, item_id: str) -> tuple[bytes, str]:
    if not item_id.isalnum():
        raise MnemoError("BAD_ID", "invalid inbox id")
    for note in (Path(vault) / inbox.RAW_ROOT).rglob(f"{item_id}.md"):
        fm, _ = split_frontmatter(note.read_text("utf-8"))
        rel = validate_relpath(note.with_name(fm["image"]).relative_to(vault).as_posix())
        return resolve_in_vault(vault, rel).read_bytes(), fm.get("media_type", "application/octet-stream")
    raise MnemoError("NO_SUCH_ITEM", f"no inbox item {item_id}")


def create_app(vault: Path, token: str, port_ref: dict[str, int], *, remote_mode: bool = False):
    """remote_mode: also serve the tailnet hosts configured with `remote allow-host`
    (through `tailscale serve`) and accept paired-device tokens."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    def ok(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status)

    def body(request_json: Any, *keys: str) -> list[Any]:
        if not isinstance(request_json, dict):
            raise MnemoError("BAD_REQUEST", "JSON object expected")
        return [request_json.get(k) for k in keys]

    def gate(request: Request) -> tuple[str | None, JSONResponse | None]:
        """Return (host_kind, error). host_kind: 'local' or 'remote'."""
        host = request.headers.get("host", "").lower()
        if host in (f"127.0.0.1:{port_ref['port']}", f"localhost:{port_ref['port']}"):
            kind = "local"
        else:
            hosts, _ = remote.access_policy(vault) if remote_mode else (set(), set())
            if not remote_mode or (host not in hosts and host.rsplit(":", 1)[0] not in hosts):
                return None, ok({"ok": False, "code": "BAD_HOST"}, 403)
            kind = "remote"
        login = request.headers.get("tailscale-user-login")
        _, logins = remote.access_policy(vault)
        # with a login allow-list, a remote request must carry an allowed Tailscale login: no header
        # (Funnel, tagged nodes, another local process) is refused, not waved through
        if logins and (login or kind == "remote") and (not login or login.lower() not in logins):
            return None, ok({"ok": False, "code": "TAILSCALE_USER_NOT_ALLOWED"}, 403)
        return kind, None

    def handler(fn, method: str = "GET", *, public: bool = False, owner_only: bool = False,
                device_scope: str | None = None):
        """device_scope: what a paired device needs for this route. GET routes default to reading
        (any device); POST routes default to "full" unless declared "capture"."""
        needs = device_scope or ("full" if method == "POST" else None)

        async def endpoint(request: Request):
            kind, error = gate(request)
            if error:
                return error
            size = request.headers.get("content-length")
            if size and size.isdigit() and int(size) > MAX_BODY:
                return ok({"ok": False, "code": "TOO_LARGE", "message": f"request body over {MAX_BODY // 1024} KB"}, 413)
            auth = request.headers.get("authorization", "")
            bearer = auth[7:] if auth.startswith("Bearer ") else ""
            from starlette.concurrency import run_in_threadpool

            if hmac.compare_digest(auth, f"Bearer {token}") and kind == "local":
                request.state.role, request.state.device = "owner", None
            elif not public and bearer and (device := await run_in_threadpool(remote.authenticate, vault, bearer)):
                request.state.role, request.state.device = "device", device
            elif public:
                request.state.role, request.state.device = "anonymous", None
            else:
                return ok({"ok": False, "code": "UNAUTHORIZED"}, 401)
            if owner_only and request.state.role != "owner":
                return ok({"ok": False, "code": "OWNER_ONLY", "message": "only from the PC itself"}, 403)
            if (needs == "full" and request.state.role == "device"
                    and (request.state.device or {}).get("scope", "capture") != "full"):
                return ok({"ok": False, "code": "DEVICE_SCOPE",
                           "message": "this device can read and capture; on the PC run "
                                      "`mnemoloci remote grant <device id> --full` to allow changes"}, 403)
            payload = None
            if method == "POST":
                if not request.headers.get("content-type", "").startswith("application/json"):
                    return ok({"ok": False, "code": "JSON_REQUIRED"}, 415)
                raw = await request.body()
                if len(raw) > MAX_BODY:  # also when Content-Length was missing or lied
                    return ok({"ok": False, "code": "TOO_LARGE"}, 413)
                try:
                    payload = json.loads(raw or b"null")
                except ValueError:
                    return ok({"ok": False, "code": "BAD_JSON"}, 400)
            try:
                result = await run_in_threadpool(fn, request, payload)
            except MnemoError as exc:
                return ok({"ok": False, "code": exc.code, "message": exc.message, "details": exc.details}, 400)
            if isinstance(result, Response):
                return result
            return ok({"ok": True, **result} if isinstance(result, dict) else {"ok": True, "result": result})
        return endpoint

    q = lambda r, k, d=None: r.query_params.get(k, d)  # noqa: E731

    def qi(r, key: str, default: int, low: int = 1, high: int = 200) -> int:
        """An integer query parameter: 400 on garbage, clamped to a sane range."""
        raw = r.query_params.get(key)
        if raw in (None, ""):
            return default
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            raise MnemoError("BAD_REQUEST", f"{key} must be a number") from None

    def status(r, _):
        report = lint.lint(vault)
        registry = sources.load_sources(vault)["sources"]
        to_compile = sum(len(sources.scan(vault, n)[n].get("changed", [])) for n in ("clips", "notes") if n in registry)
        return {k: report[k] for k in ("pages", "counts", "pending_review", "incomplete_transactions", "compile")} | {
            "healthy": report["ok"], "vault": str(vault), "inbox_pending": len(inbox.pending(vault)["pending"]),
            "instincts_proposed": len(instincts.list_instincts(vault, "proposed")["instincts"]),
            "memory_auto_pending": len(memory.list_memory(vault, source="assistant-auto", limit=1000)),
            "clips_to_compile": to_compile, "stale_answers": report["counts"].get("stale_answers", 0),
            "todos_overdue": sum(1 for t in personal.todos_list(vault)["todos"]
                                 if t.get("due") and t["due"] < time.strftime("%Y-%m-%d"))}

    def image(r, _):
        data, media = inbox_image(vault, r.path_params["id"])
        return Response(data, media_type=media, headers={"Cache-Control": "no-store"})

    def review_approve(r, p):
        (sha,) = body(p, "content_sha256")
        return review.approve(vault, r.path_params["id"], str(sha or ""))

    def review_reject(r, p):
        (reason,) = body(p, "reason")
        return review.reject(vault, r.path_params["id"], str(reason or "rifiutata dall'app"))

    def memory_retract(r, p):
        (reason,) = body(p, "reason")
        return memory.retract(vault, r.path_params["id"], reason=str(reason or ""))

    def memory_suppress(r, p):
        return memory.suppress(vault, r.path_params["id"])

    def memory_unsuppress(r, p):
        return memory.unsuppress(vault, r.path_params["id"])

    def goals_add(r, p):
        text, area = body(p, "text", "area")
        return personal.goals_add(vault, str(text or ""), area=area or None)

    def goals_update(r, p):
        status_, text, area = body(p, "status", "text", "area")
        return personal.goals_update(vault, r.path_params["id"], status=status_ or None, text=text or None, area=area or None)

    def profile_set(r, p):
        key, value, conf, source = body(p, "key", "value", "confidence", "source")
        return personal.profile_set(vault, str(key or ""), str(value or ""), confidence=float(conf if conf is not None else 1.0),
                                    source=str(source or "app"))

    def edit_save(r, p):
        path, base, title, summary, body_text, aliases, tags = body(
            p, "path", "base_sha256", "title", "summary", "body", "aliases", "tags")
        return editor.save(vault, str(path or ""), base_sha256=str(base or ""), title=str(title or ""),
                           summary=str(summary or ""), body=str(body_text or ""), aliases=aliases, tags=tags)

    def remote_claim(r, p):
        code, name = body(p, "code", "name")
        return remote.claim(vault, str(code or ""), str(name or ""))

    def clips_list(r, _):
        out = clips.list_clips(vault, qi(r, "limit", 100))
        registry = sources.load_sources(vault)["sources"]
        # only the two capture sources: scanning every project would make this view slow
        out["to_compile"] = {n: len(sources.scan(vault, n)[n].get("changed", [])) for n in ("clips", "notes") if n in registry}
        return out

    def clips_url(r, p):
        url, note, mine, title = body(p, "url", "note", "mine", "title")
        # never allow_private from the app: a page must not steer the engine into the LAN
        return clips.add_url(vault, str(url or ""), note=note or None, mine=bool(mine), title=title or None)

    def clips_text(r, p):
        title, text, url, note, mine = body(p, "title", "text", "url", "note", "mine")
        return clips.add_text(vault, str(text or ""), title=str(title or ""), url=url or None, note=note or None,
                              mine=bool(mine))

    def answers_list(r, _):
        stale = {s["path"]: s["changed"] for s in answers.stale(vault)}
        items = [{"path": p.path, "title": p.title, "question": p.frontmatter.get("question"),
                  "updated": p.frontmatter.get("updated"), "confidence": p.frontmatter.get("confidence"),
                  "based_on": [b.get("slug") for b in p.frontmatter.get("basedOn") or [] if isinstance(b, dict)],
                  "stale": stale.get(p.path, [])}
                 for p in iter_pages(vault, ("wiki",)) if not p.error and p.frontmatter.get("kind") == "query"]
        items.sort(key=lambda a: str(a["updated"]), reverse=True)
        return {"answers": items}

    def session_show(r, _):
        around = q(r, "around")
        return session_index.show(vault, q(r, "id", ""), around=qi(r, "around", 0, 1, 10**7) if around not in (None, "") else None,
                                  context=qi(r, "context", 6), last=qi(r, "last", 12))

    def today(r, _):
        week = q(r, "week") == "1"
        return {"digest": digest.build(vault, day=q(r, "day") or None, week=week),
                "todos": personal.todos_list(vault)["todos"], "backup": backup.status(vault)}

    def todo_add(r, p):
        text, due, project = body(p, "text", "due", "project")
        return personal.todos_add(vault, str(text or ""), due=due or None, project=project or None)

    def todo_update(r, p):
        status_, text, due = body(p, "status", "text", "due")
        return personal.todos_update(vault, r.path_params["id"], status=status_ or None, text=text or None, due=due or None)

    def digest_save(r, p):
        day, week = body(p, "day", "week")
        return digest.save(vault, day=day or None, week=bool(week))

    def backup_now(r, p):
        (push,) = body(p, "push")
        return backup.now(vault, message="Mnemoloci: backup dall'app", push_after=bool(push))

    def maintenance(r, _):
        return {"fix_links": maintain.fix_links(vault), "links": maintain.link_suggestions(vault, limit=60)["suggestions"],
                "tags": tags.report(vault)["variants"], "duplicates": maintain.duplicates(vault, limit=15)["pairs"],
                "synthesis": maintain.synthesis(vault, limit=10)["pairs"]}

    def fix_links_apply(r, p):
        (approved,) = body(p, "approved")
        return maintain.fix_links(vault, approved=approved or None)

    def links_apply(r, p):
        chosen, approved = body(p, "chosen", "approved")
        if not isinstance(chosen, list):
            raise MnemoError("BAD_REQUEST", "chosen must be a list of suggestions")
        return maintain.apply_links(vault, chosen, approved=approved or None)

    def tags_rename(r, p):
        old, new, approved = body(p, "old", "new", "approved")
        return tags.rename(vault, str(old or ""), str(new or ""), approved=approved or None)

    def weekly_save(r, p):
        (plan,) = body(p, "plan")
        return weekly.save(vault, str(plan or ""))

    def skills_quality(r, _):
        return {"stocktake": skill_curator.stocktake(vault), "curate": skill_curator.curate(vault)}

    def skills_archive(r, p):
        (name,) = body(p, "name")
        return skill_curator.archive(vault, str(name or ""))

    def briefings(r, _):
        items = []
        for folder, kind in (("_meta/briefings", "briefing"), ("_meta/research", "ricerca")):
            for note in sorted((Path(vault) / folder).glob("*.md"), reverse=True):
                fm, _ = split_frontmatter(note.read_text("utf-8"))
                fm = fm or {}
                items.append({"path": note.relative_to(vault).as_posix(), "title": fm.get("title") or note.stem, "kind": kind,
                              "mode": fm.get("mode"), "coverage": fm.get("coverage"),
                              "date": str(fm.get("created") or fm.get("finished") or "")[:10]})
        return {"items": items}

    def projects_root(r, p):
        (root,) = body(p, "root")
        return projects.set_root(vault, str(root or ""))

    def device_scope_set(r, p):
        (scope,) = body(p, "scope")
        return remote.set_scope(vault, r.path_params["id"], str(scope or ""))

    def sources_add_projects(r, p):
        path, name, docs_only, children = body(p, "path", "name", "docs_only", "children")
        path = str(path or "").strip().strip('"')
        return source_map.add_projects(vault, path, docs_only=docs_only is not False, children=bool(children),
                                       name=str(name or "").strip() or None)

    def source_folder(r, p):
        folder, selected = body(p, "folder", "selected")
        return source_map.set_folder(vault, r.path_params["name"], str(folder or ""), bool(selected))

    def source_scope(r, p):
        (scope,) = body(p, "scope")
        return sources.set_scope(vault, r.path_params["name"], str(scope or ""))

    def sources_add_obsidian(r, p):
        path, name = body(p, "path", "name")
        path = str(path or "").strip().strip('"')
        if not path or not Path(path).is_absolute():
            raise MnemoError("NOT_ABSOLUTE", "the full path of the vault folder is needed, e.g. C:\\Users\\you\\Notes\\MyVault")
        return sources.add_obsidian(vault, path, str(name or "").strip() or sources.free_name(vault, path))

    def doctor_fix(r, p):
        repair, lang = body(p, "repair", "lang")  # one repair, or all the checks ask for when missing
        return doctor.fix(vault, [str(repair)] if repair else None, lang=str(lang) if lang else None)

    def vault_switch(r, p):
        (path,) = body(p, "path")
        return switch_vault(str(path or ""))

    def tool_run(r, p):
        if not isinstance(p, dict):
            raise MnemoError("BAD_PARAMS", "servono i valori dello strumento")
        return tools.run(vault, r.path_params["tool"], p.get("params") or {}, confirm_cost=p.get("confirm_cost") is True)

    def settings_update(r, p):
        return settings.update(vault, r.path_params["section"], p)

    def config_embeddings_set(r, p):
        provider, ollama_model, ollama_base_url, openai_model, voyage_model = body(
            p, "provider", "ollama_model", "ollama_base_url", "openai_model", "voyage_model")
        clean = lambda v: (str(v).strip() or None) if v else None  # noqa: E731
        return set_embeddings(vault, str(provider or ""), ollama_model=clean(ollama_model),
                              ollama_base_url=clean(ollama_base_url), openai_model=clean(openai_model),
                              voyage_model=clean(voyage_model))

    def config_embeddings_key_set(r, p):
        provider, key = body(p, "provider", "api_key")
        return set_api_key(vault, str(provider or ""), str(key or ""))

    def _engine_from_body(p, prefix):
        raw = body(p, f"{prefix}provider", f"{prefix}ollama_model", f"{prefix}ollama_base_url",
                  f"{prefix}anthropic_model", f"{prefix}claude_code_model")
        return [str(v).strip() or None if v else None for v in raw]

    def local_extraction_config_set(r, p):
        mode, = body(p, "mode")
        provider, ollama_model, ollama_base_url, anthropic_model, claude_code_model = _engine_from_body(p, "")
        write_provider, write_ollama_model, write_ollama_base_url, write_anthropic_model, write_claude_code_model = \
            _engine_from_body(p, "write_")
        return set_local_extraction(
            vault, mode=str(mode or ""), provider=str(provider or ""), ollama_model=ollama_model,
            ollama_base_url=ollama_base_url, anthropic_model=anthropic_model, claude_code_model=claude_code_model,
            write_provider=write_provider, write_ollama_model=write_ollama_model,
            write_ollama_base_url=write_ollama_base_url, write_anthropic_model=write_anthropic_model,
            write_claude_code_model=write_claude_code_model)

    def local_extraction_key_set(r, p):
        provider, key = body(p, "provider", "api_key")
        return set_api_key(vault, str(provider or ""), str(key or ""))

    def local_extraction_run(r, p):
        source, mode, limit = body(p, "source", "mode", "limit")
        provider, ollama_model, ollama_base_url, anthropic_model, claude_code_model = _engine_from_body(p, "")
        write_provider, write_ollama_model, write_ollama_base_url, write_anthropic_model, write_claude_code_model = \
            _engine_from_body(p, "write_")
        saved = load_config(vault)["local_extraction"]
        extract_provider, extract_model, extract_base_url = local_extract.resolve_engine(
            saved["extract"], provider=provider, ollama_model=ollama_model, ollama_base_url=ollama_base_url,
            anthropic_model=anthropic_model, claude_code_model=claude_code_model)
        write_provider, write_model, write_base_url = local_extract.resolve_engine(
            saved["write"], provider=write_provider, ollama_model=write_ollama_model, ollama_base_url=write_ollama_base_url,
            anthropic_model=write_anthropic_model, claude_code_model=write_claude_code_model)
        return local_extract.run(
            vault, source=str(source).strip() or None if source else None,
            mode=str(mode).strip() if mode else saved["mode"],
            extract_provider=extract_provider, extract_model=extract_model or "", extract_base_url=extract_base_url,
            write_provider=write_provider, write_model=write_model, write_base_url=write_base_url,
            limit=int(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit else 25,
        )

    def connectors_credentials_set(r, p):
        client_id, client_secret = body(p, "client_id", "client_secret")
        return connectors.set_app_credentials(vault, r.path_params["provider"],
                                              client_id=str(client_id or ""), client_secret=str(client_secret or ""))

    def connectors_connect(r, p):
        # blocking (waits on the user's browser); handler() already runs every call in a
        # threadpool, and the connectors.AUTH_TIMEOUT (5 min) is the real bound the client waits on
        return connectors.authorize(vault, r.path_params["provider"])

    def connectors_token_set(r, p):
        (token,) = body(p, "token")
        return connectors.set_token(vault, r.path_params["provider"], str(token or ""))

    def connectors_set_folder(r, p):
        folder_id, folder_name = body(p, "folder_id", "folder_name")
        return connectors.set_folder(vault, r.path_params["provider"], str(folder_id or ""), folder_name or None)

    def connectors_sync(r, p):
        return connectors.sync(vault, r.path_params["provider"])

    def connectors_disable(r, p):
        return connectors.disable(vault, r.path_params["provider"])

    def whoami(r, _):
        device = r.state.device
        return {"role": r.state.role, "device": {"id": device["id"], "name": device["name"],
                                                 "scope": device.get("scope", "capture")} if device else None,
                "remote_mode": remote_mode}

    def instinct_decide(status_: str):
        def decide(r, p):
            (reason,) = body(p, "reason")
            return instincts.set_status(vault, r.path_params["id"], status_, reason or None)
        return decide

    routes = [
        Route("/api/whoami", handler(whoami)),
        Route("/api/remote/devices", handler(lambda r, _: remote.devices(vault))),
        Route("/api/remote/pair", handler(lambda r, _: remote.create_pairing_code(vault), "POST", owner_only=True), methods=["POST"]),
        Route("/api/remote/revoke/{id}", handler(lambda r, _: remote.revoke(vault, r.path_params["id"]), "POST", owner_only=True), methods=["POST"]),
        Route("/api/remote/scope/{id}", handler(device_scope_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/vault", handler(lambda r, _: vault_info(), owner_only=True)),
        Route("/api/vault", handler(vault_switch, "POST", owner_only=True), methods=["POST"]),
        Route("/api/doctor", handler(lambda r, _: doctor.run(vault, lang=q(r, "lang", "") or None), owner_only=True)),
        Route("/api/sources", handler(lambda r, _: sources.overview(vault))),
        Route("/api/sources/projects", handler(sources_add_projects, "POST", owner_only=True), methods=["POST"]),
        Route("/api/sources/{name}/tree", handler(lambda r, _: source_map.tree(vault, r.path_params["name"], q(r, "path", ""),
                                                                               fresh=q(r, "fresh") == "1"))),
        Route("/api/sources/{name}/folder", handler(source_folder, "POST", owner_only=True), methods=["POST"]),
        Route("/api/sources/{name}/scope", handler(source_scope, "POST", owner_only=True), methods=["POST"]),
        Route("/api/sources/obsidian", handler(sources_add_obsidian, "POST", owner_only=True), methods=["POST"]),
        Route("/api/sources/{name}/check", handler(lambda r, _: sources.check(vault, r.path_params["name"]), "POST",
                                                   owner_only=True), methods=["POST"]),
        Route("/api/doctor/fix", handler(doctor_fix, "POST", owner_only=True), methods=["POST"]),
        Route("/api/features", handler(lambda r, _: features.catalogue())),
        Route("/api/tools", handler(lambda r, _: tools.catalogue(), owner_only=True)),
        Route("/api/tools/{tool}", handler(tool_run, "POST", owner_only=True), methods=["POST"]),
        Route("/api/settings", handler(lambda r, _: settings.get(vault), owner_only=True)),
        Route("/api/settings/schedule", handler(lambda r, _: settings.schedule_status(vault), owner_only=True)),
        Route("/api/settings/{section}", handler(settings_update, "POST", owner_only=True), methods=["POST"]),
        Route("/api/config/embeddings", handler(lambda r, _: {
            "embeddings": load_config(vault)["embeddings"],
            "keys_set": {p: bool(get_api_key(vault, p)) for p in CLOUD_EMBEDDING_PROVIDERS},
        })),
        Route("/api/config/embeddings", handler(config_embeddings_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/config/embeddings/key", handler(config_embeddings_key_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/local-extraction", handler(lambda r, _: {
            "local_extraction": load_config(vault)["local_extraction"],
            "sources": sources.overview(vault)["sources"],
            "anthropic_key_set": bool(get_api_key(vault, "anthropic")),
        }, owner_only=True)),
        Route("/api/local-extraction/config", handler(local_extraction_config_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/local-extraction/key", handler(local_extraction_key_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/local-extraction/run", handler(local_extraction_run, "POST", owner_only=True), methods=["POST"]),
        Route("/api/connectors", handler(lambda r, _: connectors.list_connectors(vault), owner_only=True)),
        Route("/api/connectors/{provider}/credentials", handler(connectors_credentials_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/connectors/{provider}/connect", handler(connectors_connect, "POST", owner_only=True), methods=["POST"]),
        Route("/api/connectors/{provider}/token", handler(connectors_token_set, "POST", owner_only=True), methods=["POST"]),
        Route("/api/connectors/{provider}/folder", handler(connectors_set_folder, "POST", owner_only=True), methods=["POST"]),
        Route("/api/connectors/{provider}/sync", handler(connectors_sync, "POST", owner_only=True), methods=["POST"]),
        Route("/api/connectors/{provider}/disable", handler(connectors_disable, "POST", owner_only=True), methods=["POST"]),
        Route("/api/maintain", handler(maintenance)),
        Route("/api/maintain/fix-links", handler(fix_links_apply, "POST"), methods=["POST"]),
        Route("/api/maintain/apply-links", handler(links_apply, "POST"), methods=["POST"]),
        Route("/api/tags/rename", handler(tags_rename, "POST"), methods=["POST"]),
        Route("/api/weekly", handler(lambda r, _: weekly.review(vault))),
        Route("/api/weekly/save", handler(weekly_save, "POST", device_scope="capture"), methods=["POST"]),
        Route("/api/skills/quality", handler(skills_quality)),
        Route("/api/skills/archive", handler(skills_archive, "POST", owner_only=True), methods=["POST"]),
        Route("/api/briefings", handler(briefings)),
        Route("/api/people", handler(lambda r, _: graph_analysis.entities(
            vault, entity_type=None if q(r, "type") == "all" else (q(r, "type") or "person"), limit=qi(r, "limit", 60)))),
        Route("/api/graph/analysis", handler(lambda r, _: graph_analysis.analyze(vault, top=qi(r, "top", 10)))),
        Route("/api/quality", handler(lambda r, _: {**quality.run(vault), "history": quality.history(vault, 30)})),
        Route("/api/today", handler(today)),
        Route("/api/todos", handler(lambda r, _: personal.todos_list(vault, include_closed=q(r, "all") == "1"))),
        Route("/api/todos/add", handler(todo_add, "POST", device_scope="capture"), methods=["POST"]),
        Route("/api/todos/{id}", handler(todo_update, "POST", device_scope="capture"), methods=["POST"]),
        Route("/api/digest/save", handler(digest_save, "POST", device_scope="capture"), methods=["POST"]),
        Route("/api/backup/now", handler(backup_now, "POST", owner_only=True), methods=["POST"]),
        Route("/api/sessions/recent", handler(lambda r, _: session_index.recent(
            vault, project=q(r, "project") or None, limit=qi(r, "limit", 15)))),
        Route("/api/sessions/search", handler(lambda r, _: session_index.search(
            vault, q(r, "q", ""), project=q(r, "project") or None, limit=qi(r, "limit", 10)))),
        Route("/api/sessions/show", handler(session_show)),
        Route("/api/clips", handler(clips_list)),
        Route("/api/clips/url", handler(clips_url, "POST", device_scope="capture"), methods=["POST"]),
        Route("/api/clips/text", handler(clips_text, "POST", device_scope="capture"), methods=["POST"]),
        Route("/api/answers", handler(answers_list)),
        Route("/api/projects", handler(lambda r, _: projects.list_projects(vault))),
        Route("/api/projects/root", handler(projects_root, "POST", owner_only=True), methods=["POST"]),
        Route("/api/edit", handler(lambda r, _: editor.load(vault, q(r, "path", "")))),
        Route("/api/edit/save", handler(edit_save, "POST"), methods=["POST"]),
        Route("/api/instincts", handler(lambda r, _: instincts.list_instincts(vault, q(r, "status") or None))),
        Route("/api/instincts/{id}/approve", handler(instinct_decide("active"), "POST"), methods=["POST"]),
        Route("/api/instincts/{id}/reject", handler(instinct_decide("rejected"), "POST"), methods=["POST"]),
        Route("/api/status", handler(status)),
        Route("/api/search", handler(lambda r, _: index.query(vault, q(r, "q", ""), limit=qi(r, "limit", 20)))),
        Route("/api/page", handler(lambda r, _: read_page(q(r, "path", ""), vault, allow_private=r.state.role == "owner"))),
        Route("/api/graph", handler(lambda r, _: graph(vault))),
        Route("/api/review", handler(lambda r, _: {"candidates": review.list_candidates(vault)})),
        Route("/api/review/{id}", handler(lambda r, _: review.show(vault, r.path_params["id"]))),
        Route("/api/review/{id}/approve", handler(review_approve, "POST"), methods=["POST"]),
        Route("/api/review/{id}/reject", handler(review_reject, "POST"), methods=["POST"]),
        Route("/api/goals", handler(lambda r, _: personal.goals_list(vault, include_closed=q(r, "all") == "1"))),
        Route("/api/goals/add", handler(goals_add, "POST"), methods=["POST"]),
        Route("/api/goals/{id}", handler(goals_update, "POST"), methods=["POST"]),
        Route("/api/profile", handler(lambda r, _: personal.profile_show(vault))),
        Route("/api/profile/set", handler(profile_set, "POST"), methods=["POST"]),
        Route("/api/memory", handler(lambda r, _: {"memories": memory.list_memory(
            vault, kind=q(r, "kind") or None, project=q(r, "project") or None,
            source=q(r, "source") or None, status=q(r, "status", "active"), limit=qi(r, "limit", 100))})),
        Route("/api/memory/{id}/retract", handler(memory_retract, "POST"), methods=["POST"]),
        Route("/api/memory/{id}/suppress", handler(memory_suppress, "POST"), methods=["POST"]),
        Route("/api/memory/{id}/unsuppress", handler(memory_unsuppress, "POST"), methods=["POST"]),
        Route("/api/skills", handler(lambda r, _: skill_registry.find(vault, q(r, "q", ""), limit=qi(r, "limit", 20),
                                                                    installed_only=q(r, "installed") == "1"))),
        Route("/api/skills/generated", handler(lambda r, _: skill_factory.list_generated(vault))),
        Route("/api/inbox", handler(lambda r, _: inbox_items(vault))),
        Route("/api/inbox/{id}/image", handler(image)),
    ]
    if remote_mode:
        routes.append(Route("/api/remote/claim", handler(remote_claim, "POST", public=True), methods=["POST"]))
    if WEB_DIR.is_dir():
        routes.append(Mount("/", StaticFiles(directory=WEB_DIR, html=True)))
    else:
        from starlette.responses import HTMLResponse

        routes.append(Route("/", lambda r: HTMLResponse(
            "<h1>Mnemoloci</h1><p>Interface not built: run <code>npm install</code> and "
            "<code>npm run build</code> in the <code>app/</code> folder, then restart.</p>")))
    return Starlette(routes=routes)


def serve(vault: Path | None = None, *, port: int = 0, open_window: bool = True, remote_mode: bool = False,
          parent_pid: int = 0) -> int:
    import uvicorn

    if parent_pid:
        from .runtime import watch_parent

        watch_parent(parent_pid)  # started by the desktop app: never outlive it

    vault = vault or resolve_vault(None)
    require_vault(vault)
    token = secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))  # never 0.0.0.0: the tailnet reaches it only through `tailscale serve`
    port_ref = {"port": sock.getsockname()[1]}
    url = f"http://127.0.0.1:{port_ref['port']}/#token={token}"
    # The first stdout line is read by the Tauri shell; keep its format stable.
    print(f"MNEMOLOCI_APP_URL {url}", flush=True)
    if open_window:
        _open_window(url)
    app = create_app(vault, token, port_ref, remote_mode=remote_mode)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=[sock])
    return 0


def _open_window(url: str) -> None:
    """Fallback desktop window when the Tauri shell is not used: Edge app mode."""
    if EDGE.is_file():
        subprocess.Popen([str(EDGE), f"--app={url}", "--new-window"])
    else:
        import webbrowser

        webbrowser.open(url)
