import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from . import compile as compiler
from . import source_map
from . import (answers, backup, briefing, chat_import, citecheck, clips, codex_import, connectors, context_pack, doctor, embed, filing, folds, research, voice, digest, export, graph_analysis, hooks, inbox, index,
               instincts, lint, local_extract, maintain, memory, obsidian, okf, projects, quality, reason, reflect, remote, personal, review, schedule,
               secrets, session_index, sessions, skill_curator, skill_factory, skill_registry, sources, tags, vault, weekly)
from .config import record_vault, registered_vault, resolve_vault, switch_vault, vault_info
from .core import txn
from .core.errors import MnemoError


def _emit(value: Any) -> None:
    sys.stdout.buffer.write((json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.flush()


def _load_bundle(path: str) -> tuple[dict[str, Any], Path]:
    bundle_path = Path(path).resolve()
    try:
        return json.loads(bundle_path.read_text("utf-8")), bundle_path.parent
    except (OSError, ValueError) as exc:
        raise MnemoError("BAD_BUNDLE", f"cannot read {bundle_path}: {exc}") from exc


def _engine_from_args(args, saved_phase: dict[str, Any], prefix: str) -> tuple[str, str | None, str | None]:
    """prefix: "" reads the extract-phase CLI flags (--provider, --ollama-model, ...), "write_"
    reads the write-phase ones (--write-provider, --write-ollama-model, ...)."""
    return local_extract.resolve_engine(
        saved_phase, provider=getattr(args, f"{prefix}provider"), ollama_model=getattr(args, f"{prefix}ollama_model"),
        ollama_base_url=getattr(args, f"{prefix}ollama_base_url"), anthropic_model=getattr(args, f"{prefix}anthropic_model"),
        claude_code_model=getattr(args, f"{prefix}claude_code_model"))


def cmd_compile_extract_local(args) -> Any:
    """Every --flag (and its --write- counterpart for the page-writing phase) overrides the vault's
    saved `local_extraction` config (`mnemoloci config local-extraction`) for this one run only;
    omitted, they fall back to it, so a repeated manual run needs no flags once configured once."""
    saved = vault.load_config(args.vault)["local_extraction"]
    extract_provider, extract_model, extract_base_url = _engine_from_args(args, saved["extract"], "")
    write_provider, write_model, write_base_url = _engine_from_args(args, saved["write"], "write_")
    return local_extract.run(
        args.vault, source=args.source, limit=args.limit, mode=args.mode or saved["mode"],
        extract_provider=extract_provider, extract_model=extract_model, extract_base_url=extract_base_url,
        write_provider=write_provider, write_model=write_model, write_base_url=write_base_url,
    )


def cmd_txn_plan(args) -> Any:
    bundle, base = _load_bundle(args.bundle)
    return txn.prepare(args.vault, bundle, base_dir=base).summary()


def cmd_txn_apply(args) -> Any:
    bundle, base = _load_bundle(args.bundle)
    return txn.apply(args.vault, bundle, args.approved, base_dir=base)


def _scan_summary(report: dict[str, Any], full: bool) -> dict[str, Any]:
    out = {}
    for name, rep in report.items():
        if "error" in rep:
            out[name] = rep
            continue
        changed = rep["changed"]
        out[name] = {
            **{k: rep[k] for k in ("root", "files", "unchanged", "missing", "skipped")},
            "changed": len(changed),
            "changed_files": changed if full else [c["key"] for c in changed[:50]],
        }
    return out


COMMANDS = {
    "init": lambda a: _init(a.vault, a.language),
    "source-add": lambda a: sources.add_source(a.vault, a.path, a.name, a.kind, "docs" if a.docs_only else "all"),
    "source-scope": lambda a: sources.set_scope(a.vault, a.name, a.scope),
    "source-exclude": lambda a: sources.set_exclude(a.vault, a.name, a.folders, add=not a.remove),
    "source-include": lambda a: source_map.include(a.vault, a.name, a.folders),
    "source-show": lambda a: source_map.tree(a.vault, a.name, a.path or "", fresh=True) if a.path is not None
    else source_map.show(a.vault, a.name),
    "source-add-many": lambda a: source_map.add_projects(a.vault, a.parent, docs_only=not a.with_code, children=True),
    "source-list": lambda a: sources.load_sources(a.vault),
    "source-report": lambda a: sources.provenance_report(a.vault),
    "scan": lambda a: {"sources": _scan_summary(sources.scan(a.vault, a.source), a.full)},
    "compile-plan": lambda a: compiler.plan(a.vault, source=a.source, limit=a.limit, stale_only=a.stale),
    "vault-show": lambda a: vault_info(a.vault_arg),
    "vault-set": lambda a: switch_vault(a.folder),
    "doctor": lambda a: doctor.fix(a.vault, a.only or None) if a.fix else doctor.run(a.vault),
    "sessions-topics": lambda a: session_index.topics(a.vault, threshold=a.threshold),
    "filing": lambda a: filing.generate(a.vault, a.mode),
    "log-fold": lambda a: folds.fold(a.vault, month=a.month),
    "voice": lambda a: voice.samples(a.vault, limit=a.limit),
    "export-jsonld": lambda a: export.export_jsonld(a.vault, a.out),
    "export-marp": lambda a: export.export_marp(a.vault, a.note, a.out, theme=a.theme),
    "skills-triggers": lambda a: skill_curator.trigger_audit(a.vault, name=a.name),
    "maintain-fix-links": lambda a: maintain.fix_links(a.vault, approved=a.approved),
    "instincts-promote-candidates": lambda a: instincts.promotion_candidates(a.vault),
    "instincts-promote": lambda a: instincts.promote(a.vault, a.id),
    "instincts-export": lambda a: instincts.export_instincts(a.vault, a.out),
    "instincts-import": lambda a: instincts.import_instincts(a.vault, a.file),
    "quality-eval": lambda a: quality.retrieval_eval(a.vault, k=a.k, record=a.record),
    "research-start": lambda a: research.start(a.vault, " ".join(a.question), rounds=a.rounds, per_round=a.per_round),
    "research-add": lambda a: research.add_source(a.vault, a.id, url=a.url, title=a.title,
                                                  text=_read(a.text_file) if a.text_file else None),
    "research-next": lambda a: research.next_round(a.vault, a.id),
    "research-status": lambda a: research.status(a.vault, a.id),
    "research-finish": lambda a: research.finish(a.vault, a.id, _read(a.dossier)),
    "import-codex": lambda a: codex_import.import_codex(a.vault, home=a.codex_home, since=a.since, dry_run=a.dry_run),
    "source-add-obsidian": lambda a: sources.add_obsidian(a.vault, a.path, a.name),
    "source-retire": lambda a: sources.retire_source(a.vault, a.name, reason=a.reason),
    "source-unretire": lambda a: sources.retire_source(a.vault, a.name, reason="", retired=False),
    "config-instructions": lambda a: compiler.set_instructions(a.vault, _read(a.file)) if a.file else {
        "path": compiler.SOUL_PATH, "instructions": compiler.editorial_instructions(a.vault)},
    "skills-stocktake": lambda a: skill_curator.stocktake(a.vault, name=a.name, scope=a.scope),
    "skills-eval-readiness": lambda a: skill_curator.eval_readiness(a.vault, name=a.name, scope=a.scope),
    "skills-run-tests": lambda a: skill_curator.run_tests(a.vault, a.name),
    "skills-export-evals": lambda a: skill_curator.export_evals(a.vault, a.name),
    "brief-check": lambda a: briefing.check(a.vault, _read(a.file)),
    "brief-save": lambda a: briefing.save(a.vault, _read(a.file), title=a.title, topic=a.topic, mode=a.mode),
    "sessions-file": lambda a: session_index.file_history(a.vault, a.path, line=a.line),
    "sessions-file-risk": lambda a: session_index.file_risk(a.vault, a.path, limit=a.limit),
    "sessions-commit": lambda a: session_index.commit_sessions(a.vault, a.sha, repo=a.repo),
    "sessions-commits": lambda a: session_index.recent_commits(a.vault, project=a.project, limit=a.limit),
    "tags-report": lambda a: tags.report(a.vault),
    "tags-rename": lambda a: tags.rename(a.vault, a.old, a.new, approved=a.approved),
    "context-cost": lambda a: hooks.context_cost(a.vault, a.cwd),
    "reason": lambda a: reason.ask(a.vault, a.question, model=a.model, budget=a.budget, roots=a.root),
    "reason-history": lambda a: reason.history(a.vault, a.limit),
    "context": lambda a: (lambda r: {"markdown": r["markdown"]} if a.markdown else r)(
        context_pack.build(a.vault, " ".join(a.text), budget=a.budget, summary_only=a.summary_only,
                           include_memory=not a.no_memory)),
    "weekly-review": lambda a: weekly.review(a.vault, day=a.day),
    "weekly-save": lambda a: weekly.save(a.vault, _read(a.plan_file), day=a.day),
    "compile-status": lambda a: compiler.status(a.vault),
    "compile-merge": lambda a: compiler.merge(a.vault),
    "compile-submit": lambda a: compiler.submit(a.vault, approved=a.approved),
    "compile-abandon": lambda a: compiler.abandon(a.vault),
    "review-list": lambda a: {"candidates": review.list_candidates(a.vault)},
    "review-show": lambda a: review.show(a.vault, a.id),
    "review-approve": lambda a: review.approve(a.vault, a.id, a.content_sha256),
    "review-reject": lambda a: review.reject(a.vault, a.id, a.reason),
    "query": lambda a: index.query(a.vault, " ".join(a.text), limit=a.limit, mode=a.mode, as_of=a.as_of,
                                   include_expired=a.include_expired),
    "index-rebuild": lambda a: index.rebuild(a.vault),
    "lint": lambda a: lint.lint(a.vault),
    "sessions-import": lambda a: sessions.import_sessions(
        a.vault, claude_dir=a.claude_dir, include=a.project, exclude=a.exclude, since=a.since, dry_run=a.dry_run,
        only=a.only),
    "sessions-search": lambda a: session_index.search(a.vault, " ".join(a.text), project=a.project, limit=a.limit),
    "sessions-recent": lambda a: session_index.recent(a.vault, project=a.project, limit=a.limit),
    "sessions-show": lambda a: session_index.show(a.vault, a.id, around=a.around, context=a.context, last=a.last),
    "sessions-reindex": lambda a: session_index.refresh(a.vault, a.claude_dir),
    "ingest-url": lambda a: clips.add_url(a.vault, a.url, note=a.note, mine=a.mine, allow_private=a.allow_private,
                                          languages=a.lang, title=a.title),
    "ingest-file": lambda a: clips.add_file(a.vault, a.path, note=a.note, mine=a.mine, title=a.title),
    "ingest-text": lambda a: clips.add_text(a.vault, _text_arg(a), title=a.title, url=a.url, note=a.note, mine=a.mine),
    "ingest-list": lambda a: clips.list_clips(a.vault, a.limit),
    "answer-save": lambda a: answers.save(a.vault, title=a.title, question=a.question, text=_text_arg(a),
                                          summary=a.summary, replace=a.replace),
    "answer-stale": lambda a: {"stale": answers.stale(a.vault)},
    "todo-list": lambda a: personal.todos_list(a.vault, project=a.project, include_closed=a.all),
    "todo-add": lambda a: personal.todos_add(a.vault, a.text, project=a.project, due=a.due, goal=a.goal),
    "todo-done": lambda a: personal.todos_update(a.vault, a.id, status="done"),
    "todo-drop": lambda a: personal.todos_update(a.vault, a.id, status="dropped"),
    "todo-update": lambda a: personal.todos_update(a.vault, a.id, status=a.status, text=a.text, due=a.due),
    "import-chats": lambda a: chat_import.import_export(a.vault, a.export, include=a.include, exclude=a.exclude,
                                                        since=a.since, dry_run=a.dry_run),
    "backup-init": lambda a: backup.init(a.vault, remote=a.remote, auto_commit=not a.no_auto_commit, auto_push=a.auto_push),
    "backup-now": lambda a: backup.now(a.vault, message=a.message, push_after=a.push),
    "backup-status": lambda a: backup.status(a.vault),
    "backup-history": lambda a: backup.history(a.vault, a.path, a.limit),
    "digest": lambda a: digest.save(a.vault, day=a.day, week=a.week) if a.save else (
        {"markdown": digest.render(digest.build(a.vault, day=a.day, week=a.week), a.vault)} if a.markdown
        else digest.build(a.vault, day=a.day, week=a.week)),
    "people": lambda a: graph_analysis.entities(a.vault, entity_type=None if a.type == "all" else a.type, limit=a.limit),
    "graph-analyze": lambda a: {k: v for k, v in graph_analysis.analyze(a.vault, top=a.top).items() if k != "community_of"},
    "graph-backfill": lambda a: graph_analysis.backfill(a.vault, a.path, at=a.at, window_days=a.window_days,
                                                         hops=a.hops, limit=a.limit),
    "maintain-validity": lambda a: maintain.set_validity(a.vault, a.slug, valid_from=a.valid_from, valid_until=a.until,
                                                         refresh_due=a.refresh_due, superseded_by=a.superseded_by),
    "quality": lambda a: quality.run(a.vault, record=a.record, min_score=a.min_score),
    "quality-history": lambda a: {"history": quality.history(a.vault, a.limit)},
    "obsidian-canvas": lambda a: obsidian.canvas(a.vault, a.slug, depth=a.depth),
    "obsidian-bases": lambda a: obsidian.bases(a.vault),
    "obsidian-graph-colors": lambda a: obsidian.graph_colors(a.vault),
    "schedule-enable": lambda a: schedule.enable(a.vault, at=a.time),
    "schedule-disable": lambda a: schedule.disable(),
    "schedule-status": lambda a: schedule.status(a.vault),
    "scheduled-run": lambda a: schedule.run(a.vault),
    "memory-retract": lambda a: memory.retract(a.vault, a.id, reason=a.reason, redact_text=a.redact),
    "memory-suppress": lambda a: memory.suppress(a.vault, a.id),
    "memory-unsuppress": lambda a: memory.unsuppress(a.vault, a.id),
    "export-llms": lambda a: export.export_llms(a.vault, a.out, title=a.title),
    "maintain-duplicates": lambda a: maintain.duplicates(a.vault, limit=a.limit),
    "maintain-propose-merge": lambda a: maintain.propose_merge(a.vault, keep=a.keep, absorb=a.absorb, page_file=a.page),
    "maintain-submit-page": lambda a: maintain.submit_page(a.vault, a.page, replace=a.replace),
    "maintain-links": lambda a: maintain.link_suggestions(a.vault, limit=a.limit, min_score=a.min_score),
    "maintain-apply-links": lambda a: maintain.apply_links(a.vault, _json_list(a.file), approved=a.approved),
    "maintain-hub-links": lambda a: maintain.link_hub_children(a.vault, approved=a.approved),
    "maintain-synthesis": lambda a: maintain.synthesis(a.vault, limit=a.limit),
    "maintain-check-citations": lambda a: citecheck.check(a.vault, source=a.source, below=a.below, limit=a.limit),
    "maintain-reject-link": lambda a: maintain.reject_link(a.vault, page=a.page, target=a.target, reason=a.reason,
                                                            citation=a.citation, run_id=a.run_id),
    "maintain-rejections": lambda a: {"rejections": maintain.list_rejections(a.vault)},
    "instincts-evolve": lambda a: instincts.evolve(a.vault, min_size=a.min_size),
    "skills-patch": lambda a: skill_factory.patch(a.vault, a.name, old=_read(a.old_file) if a.old_file else a.old,
                                                  new=_read(a.new_file) if a.new_file else a.new, file=a.file),
    "skills-usage": lambda a: {"usage": dict(sorted(skill_curator.usage(a.vault).items(), key=lambda kv: -kv[1]["count"]))},
    "skills-curate": lambda a: skill_curator.curate(a.vault, unused_days=a.days),
    "skills-archive": lambda a: skill_curator.archive(a.vault, a.name),
    "skills-restore": lambda a: skill_curator.restore(a.vault, a.name),
    "skills-pin": lambda a: skill_curator.pin(a.vault, a.name, True),
    "skills-unpin": lambda a: skill_curator.pin(a.vault, a.name, False),
    "profile-show": lambda a: personal.profile_show(a.vault),
    "profile-set": lambda a: personal.profile_set(a.vault, a.key, a.value, confidence=a.confidence, source=a.source),
    "goals-list": lambda a: personal.goals_list(a.vault, include_closed=a.all),
    "goals-add": lambda a: personal.goals_add(a.vault, a.text, area=a.area, links=a.link),
    "goals-update": lambda a: personal.goals_update(a.vault, a.id, status=a.status, text=a.text, area=a.area, add_links=a.link),
    "memory-add": lambda a: memory.add(a.vault, a.kind, a.title, _text_arg(a), project=a.project, tags=a.tag,
                                       source=a.source, confidence=a.confidence, verification=a.verification,
                                       generality=a.generality),
    "memory-list": lambda a: {"memories": memory.list_memory(a.vault, kind=a.kind, project=a.project,
                                                             status=None if a.status == "any" else a.status,
                                                             source=a.source, limit=a.limit)},
    "memory-show": lambda a: memory.read(a.vault, a.id),
    "memory-supersede": lambda a: memory.supersede(a.vault, a.id, a.by),
    "projects-root": lambda a: projects.set_root(a.vault, a.path) if a.path else {"root": str(projects.get_root(a.vault) or "") or None},
    "projects-new": lambda a: projects.new_project(a.vault, a.name, description=a.description, git=not a.no_git,
                                                   register=not a.no_source),
    "projects-list": lambda a: projects.list_projects(a.vault),
    "projects-rc-autostart": lambda a: projects.rc_autostart(a.vault, a.enable),
    "remote-setup": lambda a: remote.setup_instructions(a.vault),
    "remote-pair": lambda a: remote.create_pairing_code(a.vault),
    "remote-devices": lambda a: remote.devices(a.vault),
    "remote-revoke": lambda a: remote.revoke(a.vault, a.id),
    "remote-grant": lambda a: remote.set_scope(a.vault, a.id, "full" if a.full else "capture"),
    "remote-allow-host": lambda a: remote.allow_hosts(a.vault, a.values, True),
    "remote-disallow-host": lambda a: remote.allow_hosts(a.vault, a.values, False),
    "remote-allow-login": lambda a: remote.allow_logins(a.vault, a.values, True),
    "remote-disallow-login": lambda a: remote.allow_logins(a.vault, a.values, False),
    "remote-autostart": lambda a: remote.autostart(a.vault, a.enable),
    "connectors-list": lambda a: connectors.list_connectors(a.vault),
    "reflect-trace": lambda a: reflect.trace(a.vault, a.path, limit=a.limit),
    "privacy-audit": lambda a: secrets.audit_vault(a.vault),
    "instincts-mine": lambda a: instincts.mine(a.vault, include=a.project, since=a.since, limit=a.limit),
    "instincts-propose": lambda a: instincts.propose(a.vault, a.file),
    "instincts-list": lambda a: instincts.list_instincts(a.vault, a.status),
    "instincts-approve": lambda a: instincts.set_status(a.vault, a.id, "active"),
    "instincts-reject": lambda a: instincts.set_status(a.vault, a.id, "rejected", a.reason),
    "instincts-retire": lambda a: instincts.set_status(a.vault, a.id, "retired", a.reason),
    "index-embed": lambda a: _embed().build(a.vault),
    "export-graph": lambda a: export.export_graph(a.vault, a.format, a.out),
    "export-vault": lambda a: export.export_vault(a.vault, a.out, a.project_id),
    "export-okf": lambda a: okf.export_okf(a.vault, a.out, human_id=a.human_id, include_memory=a.include_memory,
                                           include_sources=a.include_sources),
    "import-okf": lambda a: okf.import_okf(a.vault, a.bundle, dry_run=a.dry_run),
    "inbox-add": lambda a: inbox.add_files(a.vault, a.files, note=a.note),
    "inbox-from-session": lambda a: inbox.from_session(a.vault, cwd=a.cwd, transcript=a.transcript, last=a.last, note=a.note),
    "inbox-pending": lambda a: inbox.pending(a.vault),
    "inbox-transcribe": lambda a: inbox.transcribe(a.vault, a.id, _text_arg(a)),
    "inbox-ocr": lambda a: inbox.ocr(a.vault, a.id),
    "skills-scan": lambda a: skill_registry.scan(a.vault, a.project_dir),
    "skills-find": lambda a: skill_registry.remote(" ".join(a.text), a.limit) if a.remote else skill_registry.find(
        a.vault, " ".join(a.text), limit=a.limit, project_dir=a.project_dir, installed_only=a.installed),
    "skills-show": lambda a: skill_registry.show(a.vault, a.name, a.project_dir),
    "skills-audit": lambda a: skill_factory.audit(a.vault, name=a.name, path=a.path),
    "skills-suggest": lambda a: skill_factory.suggest_clusters(a.vault, limit=a.limit),
    "skills-material": lambda a: skill_factory.material(a.vault, query=a.query, git=a.git, limit=a.limit),
    "skills-draft": lambda a: skill_factory.draft_dir(a.vault, a.name),
    "skills-submit": lambda a: skill_factory.submit(a.vault, a.draft, origin=a.origin, sources=a.source, replace=a.replace),
    "skills-generated": lambda a: skill_factory.list_generated(a.vault),
    "skills-install": lambda a: skill_factory.install(a.vault, a.name, target=a.target, replace=a.replace,
                                                      project_dir=a.project_dir),
    "config-show": lambda a: vault.load_config(a.vault),
    "config-language": lambda a: vault.set_language(a.vault, a.code, not a.no_original),
    "config-embeddings": lambda a: vault.set_embeddings(a.vault, a.provider, ollama_model=a.ollama_model, ollama_base_url=a.ollama_base_url,
                                                        openai_model=a.openai_model, voyage_model=a.voyage_model),
    "config-embeddings-key": lambda a: vault.set_api_key(a.vault, a.provider, a.key),
    "config-local-extraction": lambda a: vault.set_local_extraction(
        a.vault, provider=a.provider, mode=a.mode, ollama_model=a.ollama_model, ollama_base_url=a.ollama_base_url,
        anthropic_model=a.anthropic_model, claude_code_model=a.claude_code_model,
        write_provider=a.write_provider, write_ollama_model=a.write_ollama_model,
        write_ollama_base_url=a.write_ollama_base_url, write_anthropic_model=a.write_anthropic_model,
        write_claude_code_model=a.write_claude_code_model),
    "config-local-extraction-key": lambda a: vault.set_api_key(a.vault, a.provider, a.key),
    "compile-extract-local": cmd_compile_extract_local,
    "txn-plan": cmd_txn_plan,
    "txn-apply": cmd_txn_apply,
    "txn-recover": lambda a: {"recovered": txn.recover(a.vault)},
    "txn-status": lambda a: {"incomplete": txn.incomplete_transactions(a.vault)},
}

# One CLI subcommand group per connector (cli.py's own "connectors" subparser, built the same way
# in build_parser()); generated instead of spelled out five times over, since the five providers
# genuinely only differ in auth_kind (oauth: setup+connect; token: a single token command) and in
# what `folder_id` means to them.
for _cmd_name, _provider in (("google-drive", "google_drive"), ("onedrive", "onedrive"), ("gmail", "gmail"),
                             ("github", "github"), ("notion", "notion")):
    _prefix = f"connectors-{_cmd_name}-"
    if connectors.PROVIDERS[_provider]["auth_kind"] == "oauth":
        COMMANDS[f"{_prefix}setup"] = (lambda p: lambda a: connectors.set_app_credentials(
            a.vault, p, client_id=a.client_id, client_secret=a.client_secret))(_provider)
        COMMANDS[f"{_prefix}connect"] = (lambda p: lambda a: connectors.authorize(a.vault, p))(_provider)
    else:
        COMMANDS[f"{_prefix}token"] = (lambda p: lambda a: connectors.set_token(a.vault, p, a.value))(_provider)
    COMMANDS[f"{_prefix}set-folder"] = (lambda p: lambda a: connectors.set_folder(a.vault, p, a.folder_id, a.name))(_provider)
    COMMANDS[f"{_prefix}sync"] = (lambda p: lambda a: connectors.sync(a.vault, p))(_provider)
    COMMANDS[f"{_prefix}disable"] = (lambda p: lambda a: connectors.disable(a.vault, p))(_provider)
del _cmd_name, _provider, _prefix


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mnemoloci", description="Personal second brain engine")
    parser.add_argument("--vault", help="vault path (default: $MNEMOLOCI_VAULT, then the vault registered by `vault set`/`init`, then ~/Mnemoloci)")
    parser.add_argument("--version", action="version", version=f"mnemoloci {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def leaf(subparsers, name: str, key: str, help_text: str) -> argparse.ArgumentParser:
        p = subparsers.add_parser(name, help=help_text)
        p.set_defaults(_cmd=key)
        return p

    vlt = sub.add_parser("vault", help="which vault Mnemoloci uses").add_subparsers(dest="sub", required=True)
    leaf(vlt, "show", "vault-show", "the vault in use and the rule that chose it")
    p = leaf(vlt, "set", "vault-set", "use this existing vault from now on (hooks, MCP and the app follow)")
    p.add_argument("folder", help="full path of an existing vault")

    p = leaf(sub, "init", "init", "create a new vault")
    p.add_argument("--language", choices=["en", "it"], default="en", help="language the engine writes in (default en)")

    cfg = sub.add_parser("config", help="vault settings").add_subparsers(dest="sub", required=True)
    leaf(cfg, "show", "config-show", "show the vault configuration")
    p = leaf(cfg, "language", "config-language", "language pages are written in")
    p.add_argument("code", help="ISO 639-1 code, e.g. it")
    p.add_argument("--no-original", action="store_true", help="do not keep the original-language text block")
    p = leaf(cfg, "embeddings", "config-embeddings",
             "semantic search provider: fastembed (default, local, no server), a local Ollama server, or OpenAI/Voyage AI (cloud, paid)")
    p.add_argument("provider", choices=["fastembed", "ollama", "openai", "voyage"])
    p.add_argument("--ollama-model", help="required for provider ollama, e.g. nomic-embed-text")
    p.add_argument("--ollama-base-url", help="default http://localhost:11434")
    p.add_argument("--openai-model", help=f"default {embed.CLOUD_DEFAULT_MODEL['openai']}")
    p.add_argument("--voyage-model", help=f"default {embed.CLOUD_DEFAULT_MODEL['voyage']}")
    p = leaf(cfg, "embeddings-key", "config-embeddings-key",
             "API key for a cloud embedding provider (stored in .mnemoloci/, never in the git backup)")
    p.add_argument("provider", choices=["openai", "voyage"])
    p.add_argument("key", help=f"or set the {' / '.join(vault.API_KEY_ENV.values())} environment variable instead")
    p = leaf(cfg, "instructions", "config-instructions", "your writing guidelines for the wiki (_meta/SOUL.md)")
    p.add_argument("--file", help="set them from this file (omit to show the current ones)")
    p = leaf(cfg, "local-extraction", "config-local-extraction",
             "unattended extraction (compile extract-local): ollama (local, free), anthropic (Claude's API, paid) or "
             "claude_code (claude -p, billed to your subscription, no key); always held for review, never published directly. "
             "--write-* configure a different engine for the page-writing phase (extract_and_write only); omitted, it "
             "mirrors the extraction engine")
    p.add_argument("--provider", required=True, choices=sorted(vault.LOCAL_EXTRACTION_PROVIDERS), help="extraction-phase engine")
    p.add_argument("--mode", required=True, choices=sorted(vault.LOCAL_EXTRACTION_MODES),
                   help="extract_only: extraction only, pages left for a later Claude Code session; "
                        "extract_and_write: the model writes the pages too")
    p.add_argument("--ollama-model", help="required for provider ollama, e.g. llama3.1 (unrelated to config embeddings)")
    p.add_argument("--ollama-base-url", help="default http://localhost:11434")
    p.add_argument("--anthropic-model", help="required for provider anthropic, e.g. claude-haiku-4-5-20251001")
    p.add_argument("--claude-code-model", help="required for provider claude_code, e.g. sonnet or claude-sonnet-5")
    p.add_argument("--write-provider", choices=sorted(vault.LOCAL_EXTRACTION_PROVIDERS),
                   help="page-writing engine, if different from --provider (extract_and_write only)")
    p.add_argument("--write-ollama-model", help="required if --write-provider ollama")
    p.add_argument("--write-ollama-base-url", help="default http://localhost:11434")
    p.add_argument("--write-anthropic-model", help="required if --write-provider anthropic")
    p.add_argument("--write-claude-code-model", help="required if --write-provider claude_code")
    p = leaf(cfg, "local-extraction-key", "config-local-extraction-key",
             "API key for a cloud extraction provider (Anthropic; stored in .mnemoloci/, never in the git backup)")
    p.add_argument("provider", choices=["anthropic"])
    p.add_argument("key", help="or set the ANTHROPIC_API_KEY environment variable instead")

    src = sub.add_parser("source", help="registered knowledge sources").add_subparsers(dest="sub", required=True)
    p = leaf(src, "add", "source-add", "register a folder as a source")
    p.add_argument("path")
    p.add_argument("--name", required=True)
    p.add_argument("--docs-only", action="store_true",
                   help="only documents and project files (README, CLAUDE.md, notes, package.json...); code later")
    p.add_argument("--kind", default="project", choices=sorted(sources.SOURCE_KINDS))
    leaf(src, "list", "source-list", "list sources")
    leaf(src, "report", "source-report", "which sources produced which vault pages, grouped by kind/name")
    p = leaf(src, "add-obsidian", "source-add-obsidian", "adopt an existing Obsidian vault as a read-only source of your notes")
    p.add_argument("path")
    p.add_argument("--name", required=True)
    p = leaf(src, "scope", "source-scope", "what a source contributes: docs (documents only) or all (code too)")
    p.add_argument("name")
    p.add_argument("scope", choices=["docs", "all"])
    p = leaf(src, "show", "source-show", "what a source contributes: settings, folders read and skipped (read it before compiling)")
    p.add_argument("name")
    p.add_argument("--path", help="one folder, level by level (as the app's Fonti page shows it)")
    p = leaf(src, "include", "source-include", "read folders an automatic rule or an excluded parent would skip")
    p.add_argument("name")
    p.add_argument("folders", nargs="+")
    p = leaf(src, "add-many", "source-add-many", "register every subfolder of a folder as a project (documents only)")
    p.add_argument("parent")
    p.add_argument("--with-code", action="store_true", help="read the code too (default: documents only)")
    p = leaf(src, "exclude", "source-exclude", "skip subfolders of a source (vendored code, toolchains, generated output)")
    p.add_argument("name")
    p.add_argument("folders", nargs="+", help="folders relative to the source, e.g. docs/archive")
    p.add_argument("--remove", action="store_true", help="include them again")
    p = leaf(src, "retire", "source-retire", "stop following a source (pages stay, never reported stale)")
    p.add_argument("name")
    p.add_argument("--reason", required=True)
    p = leaf(src, "unretire", "source-unretire", "follow a retired source again")
    p.add_argument("name")

    p = leaf(sub, "scan", "scan", "show new/modified/missing files per source")
    p.add_argument("--source")
    p.add_argument("--full", action="store_true", help="include per-file details")

    comp = sub.add_parser("compile", help="two-phase compile").add_subparsers(dest="sub", required=True)
    p = leaf(comp, "plan", "compile-plan", "create extraction packets for changed files")
    p.add_argument("--source")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--stale", action="store_true", help="only files that existing pages cite (refresh stale pages)")
    leaf(comp, "status", "compile-status", "show the active run")
    leaf(comp, "merge", "compile-merge", "group extracted concepts into page jobs")
    p = leaf(comp, "submit", "compile-submit", "validate pages; dry run unless --approved")
    p.add_argument("--approved", metavar="SHA256")
    leaf(comp, "abandon", "compile-abandon", "abandon the active run (files kept)")
    p = leaf(comp, "extract-local", "compile-extract-local",
             "unattended plan+extract(+write) with ollama, the Claude API or claude -p instead of an active Claude Code "
             "session; always held for review")
    p.add_argument("--source")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--provider", choices=sorted(vault.LOCAL_EXTRACTION_PROVIDERS), help="default: config local-extraction's saved provider")
    p.add_argument("--mode", choices=sorted(vault.LOCAL_EXTRACTION_MODES), help="default: config local-extraction's saved mode")
    p.add_argument("--ollama-model", help="default: config local-extraction's saved model (provider ollama)")
    p.add_argument("--ollama-base-url", help="default: config local-extraction's saved base URL, or http://localhost:11434")
    p.add_argument("--anthropic-model", help="default: config local-extraction's saved model (provider anthropic)")
    p.add_argument("--claude-code-model", help="default: config local-extraction's saved model (provider claude_code)")
    p.add_argument("--write-provider", choices=sorted(vault.LOCAL_EXTRACTION_PROVIDERS),
                   help="page-writing engine for this run, if different (default: config local-extraction's saved write provider)")
    p.add_argument("--write-ollama-model", help="default: config local-extraction's saved write model (provider ollama)")
    p.add_argument("--write-ollama-base-url", help="default: config local-extraction's saved write base URL")
    p.add_argument("--write-anthropic-model", help="default: config local-extraction's saved write model (provider anthropic)")
    p.add_argument("--write-claude-code-model", help="default: config local-extraction's saved write model (provider claude_code)")

    rev = sub.add_parser("review", help="review queue").add_subparsers(dest="sub", required=True)
    leaf(rev, "list", "review-list", "pending candidates")
    p = leaf(rev, "show", "review-show", "show a candidate")
    p.add_argument("id")
    p = leaf(rev, "approve", "review-approve", "publish a candidate")
    p.add_argument("id")
    p.add_argument("--content-sha256", required=True, help="hash printed by review show")
    p = leaf(rev, "reject", "review-reject", "archive a candidate")
    p.add_argument("id")
    p.add_argument("--reason", required=True)

    p = leaf(sub, "query", "query", "full-text search with link expansion")
    p.add_argument("text", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--mode", choices=["auto", "lexical", "hybrid"], default="auto",
                   help="auto = hybrid (BM25 + embeddings) when `index embed` has been run")
    p.add_argument("--as-of", help="YYYY-MM-DD: only pages valid on that date")
    p.add_argument("--include-expired", action="store_true",
                   help="also return pages expired as of today (excluded by default, flagged when as-of is not given)")
    idx = sub.add_parser("index", help="search index").add_subparsers(dest="sub", required=True)
    leaf(idx, "rebuild", "index-rebuild", "rebuild the SQLite index from the vault")
    leaf(idx, "embed", "index-embed", "compute local multilingual embeddings (incremental; needs the semantic extra)")

    ins = sub.add_parser("instincts", help="learned rules from your corrections (approved before use)").add_subparsers(dest="sub", required=True)
    p = leaf(ins, "mine", "instincts-mine", "find correction/confirmation moments in Claude Code transcripts")
    p.add_argument("--project", action="append", help="only projects whose path contains this text")
    p.add_argument("--since", help="only moments on/after YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=200)
    p = leaf(ins, "propose", "instincts-propose", "submit instinct proposals (they stay inactive until approved)")
    p.add_argument("file")
    p = leaf(ins, "list", "instincts-list", "list instincts")
    p.add_argument("--status", choices=["proposed", "active", "rejected", "retired"])
    p = leaf(ins, "approve", "instincts-approve", "activate an instinct (it will be injected into sessions)")
    p.add_argument("id")
    leaf(ins, "promote-candidates", "instincts-promote-candidates", "project rules that recur in 2+ projects")
    p = leaf(ins, "promote", "instincts-promote", "make a project instinct global (explicit)")
    p.add_argument("id")
    p = leaf(ins, "export", "instincts-export", "export active and proposed instincts to a JSON file")
    p.add_argument("--out", required=True)
    p = leaf(ins, "import", "instincts-import", "import instincts from another vault (they arrive as proposed)")
    p.add_argument("file")
    p = leaf(ins, "evolve", "instincts-evolve", "group approved instincts by subject: material for new skills")
    p.add_argument("--min-size", type=int, default=2)
    for name, key in (("reject", "instincts-reject"), ("retire", "instincts-retire")):
        p = leaf(ins, name, key, f"{name} an instinct (kept, never deleted)")
        p.add_argument("id")
        p.add_argument("--reason")

    exp = sub.add_parser("export", help="export the graph or the whole vault").add_subparsers(dest="sub", required=True)
    p = leaf(exp, "graph", "export-graph", "knowledge graph as GraphML, JSON, a self-contained HTML file, Neo4j Cypher or Postgres SQL")
    p.add_argument("--format", choices=["graphml", "json", "html", "cypher", "sql"], required=True)
    p.add_argument("--out", required=True)
    p = leaf(exp, "okf", "export-okf", "Open Knowledge Format v0.2 bundle (Google Cloud spec)")
    p.add_argument("--out", required=True, help="new or empty directory outside the vault")
    p.add_argument("--human-id", help="id for pages you approved in review (verified: human:<id>)")
    p.add_argument("--include-memory", action="store_true", help="also export memory notes and instincts")
    p.add_argument("--include-sources", action="store_true",
                   help="copy the (secret-redacted) source files into references/ - they may be private")
    imp = sub.add_parser("import", help="import knowledge from other tools").add_subparsers(dest="sub", required=True)
    p = leaf(imp, "okf", "import-okf", "stage an OKF bundle's concepts in the review queue")
    p.add_argument("bundle")
    p.add_argument("--dry-run", action="store_true")
    p = leaf(imp, "codex", "import-codex", "import OpenAI Codex CLI sessions (~/.codex/sessions)")
    p.add_argument("--codex-home", help="default: $CODEX_HOME or ~/.codex")
    p.add_argument("--since", help="only sessions started on/after YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true")
    p = leaf(imp, "chats", "import-chats", "import a ChatGPT or claude.ai export (zip or conversations.json)")
    p.add_argument("export")
    p.add_argument("--include", action="append", help="only conversations whose title contains this text")
    p.add_argument("--exclude", action="append", help="skip conversations whose title contains this text")
    p.add_argument("--since", help="only conversations started on/after YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true")
    p = leaf(exp, "jsonld", "export-jsonld", "schema.org JSON-LD of the wiki (private pages excluded)")
    p.add_argument("--out", required=True)
    p = leaf(exp, "marp", "export-marp", "a Marp slide deck from a page or briefing")
    p.add_argument("note", help="vault-relative path or file")
    p.add_argument("--out", required=True)
    p.add_argument("--theme", default="default")
    p = leaf(exp, "llms", "export-llms", "llms.txt index + llms-full.txt for other agents")
    p.add_argument("--out", required=True, help="folder outside the vault")
    p.add_argument("--title", default="Second brain")
    p = leaf(exp, "vault", "export-vault", "every page as a JSON envelope (metadata, citations, freshness)")
    p.add_argument("--out", required=True)
    p.add_argument("--project-id", help="stable id prefix, so re-exports update instead of duplicating downstream")
    leaf(sub, "lint", "lint", "read-only vault health check")

    ses = sub.add_parser("sessions", help="Claude Code session history").add_subparsers(dest="sub", required=True)
    p = leaf(ses, "import", "sessions-import", "import session transcripts as redacted digests into raw/sessions")
    p.add_argument("--claude-dir", action="append", help="Claude config dir (default: $CLAUDE_CONFIG_DIR and ~/.claude)")
    p.add_argument("--project", action="append", help="only projects whose path contains this text (repeatable)")
    p.add_argument("--exclude", action="append", help="skip projects whose path contains this text (repeatable)")
    p.add_argument("--since", help="only sessions started on/after YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", help="import just this one transcript .jsonl, skipping the full scan (for hooks)")
    p = leaf(ses, "search", "sessions-search", "find past sessions by what was said (redacted full-text index)")
    p.add_argument("text", nargs="+")
    p.add_argument("--project", help="only sessions whose folder contains this text (e.g. the current cwd)")
    p.add_argument("--limit", type=int, default=8)
    p = leaf(ses, "recent", "sessions-recent", "latest sessions, newest first, with any question left open")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=10)
    p = leaf(ses, "show", "sessions-show", "load a past session's messages (opening + ending, or around a hit)")
    p.add_argument("id", help="session id or its first 6+ characters")
    p.add_argument("--around", type=int, help="message index from `sessions search`")
    p.add_argument("--context", type=int, default=6, help="messages before/after --around")
    p.add_argument("--last", type=int, default=12, help="final messages to show without --around")
    p = leaf(ses, "reindex", "sessions-reindex", "update the session search index (incremental)")
    p.add_argument("--claude-dir", action="append")
    p = leaf(ses, "file", "sessions-file", "which sessions edited a file; with --line, which commit and session wrote it")
    p.add_argument("path")
    p.add_argument("--line", type=int)
    p = leaf(ses, "file-risk", "sessions-file-risk",
             "whether a file has a track record of trouble (reverts, a run of fix commits) before you edit it")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=20, help="how many recent commits on the file to check")
    p = leaf(ses, "commit", "sessions-commit", "which Claude Code session produced a commit")
    p.add_argument("sha")
    p.add_argument("--repo", help="repository folder (default: current directory)")
    p = leaf(ses, "topics", "sessions-topics", "past sessions grouped by subject")
    p.add_argument("--threshold", type=float, default=0.22, help="similarity needed to join a group (0-1)")
    p = leaf(ses, "commits", "sessions-commits", "recent commits made in Claude Code sessions")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=20)

    ref = sub.add_parser("reflect", help="belief tracing: a wiki page's whole recorded history").add_subparsers(dest="sub", required=True)
    p = leaf(ref, "trace", "reflect-trace",
             "every version of a page on record (confidence, provenance, contradictions, summary, body) -- "
             "the raw material for narrating how a position changed over time; doesn't itself narrate anything")
    p.add_argument("path", help="e.g. wiki/concepts/motore-transazionale.md")
    p.add_argument("--limit", type=int, default=20)

    priv = sub.add_parser("privacy", help="credential/PII leak scanning").add_subparsers(dest="sub", required=True)
    leaf(priv, "audit", "privacy-audit",
         "scan wiki/memory/_meta for credential-shaped strings that slipped past capture-time redaction "
         "(a manual edit, or a pattern added after the page was written); report-only, changes nothing")

    tg = sub.add_parser("tags", help="tag vocabulary: counts, variants, rename").add_subparsers(dest="sub", required=True)
    leaf(tg, "report", "tags-report", "every tag with its count, variants of the same tag, tags used once")
    p = leaf(tg, "rename", "tags-rename", "replace a tag everywhere (dry run unless --approved)")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--approved", metavar="SHA256")

    p = leaf(sub, "context-cost", "context-cost",
             "what the session-start injection costs for a project, section by section")
    p.add_argument("--cwd", help="the project folder a session would start in")
    p = leaf(sub, "reason", "reason", "answer a question from the vault with a read-only `claude -p` run (spends tokens, capped, audited)")
    p.add_argument("question")
    p.add_argument("--model")
    p.add_argument("--budget", type=float, help="spend cap in US dollars (default: config reason.maxBudgetUsd, else 0.50)")
    p.add_argument("--root", action="append", help="another folder the run may read (repeatable)")
    p = leaf(sub, "reason-history", "reason-history", "past `reason` runs: question, cost, turns, time")
    p.add_argument("--limit", type=int, default=20)
    p = leaf(sub, "context", "context", "context pack on a question, within a token budget (private pages excluded)")
    p.add_argument("text", nargs="+")
    p.add_argument("--budget", type=int, default=4000, help="tokens (about 4 characters each)")
    p.add_argument("--summary-only", action="store_true")
    p.add_argument("--no-memory", action="store_true", help="wiki pages only")
    p.add_argument("--markdown", action="store_true", help="print only the ready-to-paste text")

    br = sub.add_parser("brief", help="briefings, plain explanations, lessons: audit and save").add_subparsers(dest="sub", required=True)
    p = leaf(br, "check", "brief-check", "audit links, citations and unsupported paragraphs of a written briefing")
    p.add_argument("file")
    p = leaf(br, "save", "brief-save", "save an audited briefing in _meta/briefings/")
    p.add_argument("file")
    p.add_argument("--title", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--mode", default="briefing", choices=["briefing", "spiegazione", "lezione"])

    wk = sub.add_parser("weekly", help="weekly review: what moved, stuck goals, plan for next week").add_subparsers(dest="sub", required=True)
    p = leaf(wk, "review", "weekly-review", "gather the week (goals moved/stuck, open questions, todos)")
    p.add_argument("--day", help="any day of the week to review (default today)")
    p = leaf(wk, "save", "weekly-save", "save the review and the plan agreed with the user")
    p.add_argument("--plan-file", required=True)
    p.add_argument("--day")

    ing = sub.add_parser("ingest", help="capture web pages, PDFs, YouTube videos, files or text").add_subparsers(dest="sub", required=True)
    p = leaf(ing, "url", "ingest-url", "fetch a web page, PDF or YouTube transcript into raw/clips")
    p.add_argument("url")
    p.add_argument("--title", help="override the page/PDF/video title")
    p.add_argument("--note", help="why it is worth keeping")
    p.add_argument("--mine", action="store_true", help="your own writing: raw/notes, no external-source review")
    p.add_argument("--allow-private", action="store_true", help="allow LAN/localhost addresses (off by default)")
    p.add_argument("--lang", action="append", help="preferred transcript language for YouTube (default: the vault language, then en)")
    p = leaf(ing, "file", "ingest-file", "capture a local PDF, HTML or text file (images go to the inbox)")
    p.add_argument("path")
    p.add_argument("--title")
    p.add_argument("--note")
    p.add_argument("--mine", action="store_true")
    p = leaf(ing, "text", "ingest-text", "capture text you already have (pasted, read from a PDF, fetched)")
    p.add_argument("--title", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--text-file")
    p.add_argument("--url", help="where the text comes from")
    p.add_argument("--note")
    p.add_argument("--mine", action="store_true")
    p = leaf(ing, "list", "ingest-list", "captured clips and notes, newest first")
    p.add_argument("--limit", type=int, default=50)

    ans = sub.add_parser("answer", help="save good answers as wiki pages (wiki/queries)").add_subparsers(dest="sub", required=True)
    p = leaf(ans, "save", "answer-save", "save an answer grounded in wiki pages ([[links]] must resolve)")
    p.add_argument("--title", required=True)
    p.add_argument("--question", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--text-file")
    p.add_argument("--summary")
    p.add_argument("--replace", action="store_true", help="update an answer saved earlier")
    leaf(ans, "stale", "answer-stale", "answers whose source pages changed since they were saved")

    td = sub.add_parser("todo", help="things to do, carried from session to session (_meta/todos.md)").add_subparsers(dest="sub", required=True)
    p = leaf(td, "list", "todo-list", "open todos (with --project: that project's plus the general ones)")
    p.add_argument("--project")
    p.add_argument("--all", action="store_true", help="include done and dropped")
    p = leaf(td, "add", "todo-add", "add a todo")
    p.add_argument("text")
    p.add_argument("--project", help="project folder it belongs to (default: general)")
    p.add_argument("--due", help="YYYY-MM-DD")
    p.add_argument("--goal", help="goal id it serves, e.g. g2")
    for name, key in (("done", "todo-done"), ("drop", "todo-drop")):
        p = leaf(td, name, key, f"mark a todo {name} (kept, never deleted)")
        p.add_argument("id")
    p = leaf(td, "update", "todo-update", "change text, due date or status")
    p.add_argument("id")
    p.add_argument("--status", choices=["open", "done", "dropped"])
    p.add_argument("--text")
    p.add_argument("--due")

    bk = sub.add_parser("backup", help="git versioning of the vault (and an optional remote copy)").add_subparsers(dest="sub", required=True)
    p = leaf(bk, "init", "backup-init", "start versioning the vault with git (first commit included)")
    p.add_argument("--remote", help="git remote URL for an off-machine copy (e.g. a private GitHub repo)")
    p.add_argument("--no-auto-commit", action="store_true", help="do not commit automatically at session end")
    p.add_argument("--auto-push", action="store_true", help="also push to the remote at session end")
    p = leaf(bk, "now", "backup-now", "commit the current state of the vault")
    p.add_argument("--message")
    p.add_argument("--push", action="store_true", help="push to the configured remote afterwards")
    leaf(bk, "status", "backup-status", "last backup, uncommitted changes, remote")
    p = leaf(bk, "history", "backup-history", "earlier versions (of the whole vault or one note)")
    p.add_argument("path", nargs="?")
    p.add_argument("--limit", type=int, default=20)

    mt = sub.add_parser("maintain", help="wiki maintenance that writes, always behind review or approval").add_subparsers(dest="sub", required=True)
    p = leaf(mt, "duplicates", "maintain-duplicates", "pairs of pages that are probably the same thing")
    p.add_argument("--limit", type=int, default=20)
    p = leaf(mt, "propose-merge", "maintain-propose-merge", "queue a merged page for review (the other becomes a stub)")
    p.add_argument("--keep", required=True, help="slug of the page that stays")
    p.add_argument("--absorb", required=True, help="slug of the page merged into it")
    p.add_argument("--page", required=True, help="file with the merged page (frontmatter + body)")
    p = leaf(mt, "submit-page", "maintain-submit-page", "send a page written outside compile to the review queue")
    p.add_argument("page")
    p.add_argument("--replace", action="store_true", help="update the existing page with this title")
    p = leaf(mt, "links", "maintain-links",
             "pages that should probably link to each other but don't yet, scored (mentions, shared tags, "
             "a peripheral page reaching a hub)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--min-score", type=int, default=3, help="drop weak (\"bassa\") suggestions below this score")
    p = leaf(mt, "apply-links", "maintain-apply-links", "add the chosen links (dry run unless --approved)")
    p.add_argument("file", help="JSON list of suggestions to apply (from `maintain links`)")
    p.add_argument("--approved", metavar="SHA256")
    p = leaf(mt, "fix-links", "maintain-fix-links", "repair broken [[links]] that can mean only one page (dry run unless --approved)")
    p.add_argument("--approved", metavar="SHA256")
    p = leaf(mt, "hub-links", "maintain-hub-links", "link every page to its most specific project hub (dry run unless --approved)")
    p.add_argument("--approved", metavar="SHA256")
    p = leaf(mt, "check-citations", "maintain-check-citations",
             "report-only: citations whose cited lines share too little with the sentence (numbers, names, words)")
    p.add_argument("--source", help="only citations of this source")
    p.add_argument("--below", type=float, default=citecheck.FLAG_BELOW, help="report support under this (0..1)")
    p.add_argument("--limit", type=int, default=50)
    p = leaf(mt, "synthesis", "maintain-synthesis", "pages often cited together but never linked: synthesis candidates")
    p.add_argument("--limit", type=int, default=15)
    p = leaf(mt, "reject-link", "maintain-reject-link",
             "record that a page should NOT link to something, and why (surfaced by lint if unverified)")
    p.add_argument("--page", required=True, help="slug of the page the judgment is about")
    p.add_argument("--target", required=True, help="slug or label of what it was judged not to relate to")
    p.add_argument("--reason", required=True)
    p.add_argument("--citation", help="source/rel/path.md#L10-L14 proving the judgment")
    p.add_argument("--run-id", dest="run_id", default="manual")
    leaf(mt, "rejections", "maintain-rejections", "list recorded link-rejection judgments")
    p = leaf(mt, "validity", "maintain-validity", "record when a page holds: valid from/until, refresh due, superseded")
    p.add_argument("slug")
    p.add_argument("--from", dest="valid_from", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--refresh-due", help="YYYY-MM-DD: check the page again by this date")
    p.add_argument("--superseded-by", help="slug of the page that replaces it")

    p = leaf(sub, "people", "people", "people (or other entities) ranked by how much they matter now")
    p.add_argument("--type", default="person", choices=["person", "organization", "place", "tool", "project", "event", "all"])
    p.add_argument("--limit", type=int, default=50)
    gr = sub.add_parser("graph", help="link graph analysis").add_subparsers(dest="sub", required=True)
    p = leaf(gr, "analyze", "graph-analyze", "hubs, bridges, communities, surprising links, isolated pages")
    p.add_argument("--top", type=int, default=10)
    p = leaf(gr, "backfill", "graph-backfill",
             "pages near this one in the link graph AND close in time to it -- what was going on around here, "
             "not what reads like it; use when a failure shares no vocabulary with whatever caused it")
    p.add_argument("path", help="e.g. wiki/concepts/motore-transazionale.md")
    p.add_argument("--at", help="anchor date YYYY-MM-DD (default: the page's own `updated`)")
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--hops", type=int, default=2)
    p.add_argument("--limit", type=int, default=20)
    p = leaf(sub, "quality", "quality", "vault health score 0-100 with trend")
    p.add_argument("--record", action="store_true", help="add today's score to the history")
    p.add_argument("--min-score", type=int, help="exit code 1 below this score (gate)")
    p = sub.add_parser("quality-eval", help="search quality on known questions: recall@k and MRR")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--record", action="store_true")
    p.set_defaults(_cmd="quality-eval")
    p = leaf(sub, "doctor", "doctor", "installation health check with fixes")
    p.add_argument("--fix", action="store_true", help="run the safe repairs the checks ask for, then check again")
    p.add_argument("--only", nargs="+", metavar="REPAIR", help="with --fix: only these repairs (" + ", ".join(doctor.REPAIRS) + ")")
    p = leaf(sub, "filing", "filing", "navigation notes for a filing method (pages are not moved)")
    p.add_argument("mode", choices=["para", "lyt", "zettelkasten"])
    p = leaf(sub, "log-fold", "log-fold", "monthly summaries of wiki/log.md in _meta/folds/ (log unchanged)")
    p.add_argument("--month", help="YYYY-MM (default: every month before the current one)")
    p = leaf(sub, "voice", "voice", "samples of the user's own writing, to write in their voice")
    p.add_argument("--limit", type=int, default=8)
    rs = sub.add_parser("research", help="bounded web research with a reviewed merge").add_subparsers(dest="sub", required=True)
    p = leaf(rs, "start", "research-start", "open a research run with a budget")
    p.add_argument("question", nargs="+")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--per-round", type=int, default=5)
    p = leaf(rs, "add", "research-add", "capture a source into the research (counts against the round budget)")
    p.add_argument("id")
    p.add_argument("--url")
    p.add_argument("--text-file")
    p.add_argument("--title")
    p = leaf(rs, "next", "research-next", "move to the next round")
    p.add_argument("id")
    p = leaf(rs, "status", "research-status", "one research, or all of them")
    p.add_argument("id", nargs="?")
    p = leaf(rs, "finish", "research-finish", "check and save the dossier")
    p.add_argument("id")
    p.add_argument("--dossier", required=True)
    p = sub.add_parser("quality-history", help="recorded quality scores")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(_cmd="quality-history")
    ob = sub.add_parser("obsidian", help="Obsidian views: canvas maps, Bases dashboards, graph colours").add_subparsers(dest="sub", required=True)
    p = leaf(ob, "canvas", "obsidian-canvas", "JSON Canvas map around a page (wiki/canvases/<slug>.canvas)")
    p.add_argument("slug")
    p.add_argument("--depth", type=int, default=1, choices=[1, 2])
    leaf(ob, "bases", "obsidian-bases", "write .base dashboards in _meta/bases/ (only files Mnemoloci made are replaced)")
    leaf(ob, "graph-colors", "obsidian-graph-colors", "colour the graph view by page kind (only if you have no colour groups)")
    sc = sub.add_parser("schedule", help="daily maintenance via Windows Task Scheduler (opt-in)").add_subparsers(dest="sub", required=True)
    p = leaf(sc, "enable", "schedule-enable", "run the daily job at a time (HH:MM)")
    p.add_argument("--time", default="21:30")
    leaf(sc, "disable", "schedule-disable", "remove the scheduled task")
    leaf(sc, "status", "schedule-status", "task details and the last run")
    sub.add_parser("scheduled-run", help="the daily job itself (called by the scheduled task)").set_defaults(_cmd="scheduled-run")

    p = leaf(sub, "digest", "digest", "what happened today / on a day / this week")
    p.add_argument("--day", help="YYYY-MM-DD (default today)")
    p.add_argument("--week", action="store_true", help="the whole week containing --day")
    p.add_argument("--markdown", action="store_true", help="print it as a readable note")
    p.add_argument("--save", action="store_true", help="save it in _meta/digests/")

    prof = sub.add_parser("profile", help="personal profile (_meta/profile.md)").add_subparsers(dest="sub", required=True)
    leaf(prof, "show", "profile-show", "show profile facts")
    p = leaf(prof, "set", "profile-set", "add or update a fact (previous value is kept)")
    p.add_argument("key")
    p.add_argument("value")
    p.add_argument("--confidence", type=float, default=1.0)
    p.add_argument("--source", default="utente")

    goals = sub.add_parser("goals", help="personal goals (_meta/goals.md)").add_subparsers(dest="sub", required=True)
    p = leaf(goals, "list", "goals-list", "active and paused goals")
    p.add_argument("--all", action="store_true", help="include done and dropped")
    p = leaf(goals, "add", "goals-add", "add an active goal")
    p.add_argument("text")
    p.add_argument("--area")
    p.add_argument("--link", action="append", help="related wiki page slug (repeatable)")
    p = leaf(goals, "update", "goals-update", "change status/text/area or add links")
    p.add_argument("id")
    p.add_argument("--status", choices=["active", "paused", "done", "dropped"])
    p.add_argument("--text")
    p.add_argument("--area")
    p.add_argument("--link", action="append")

    mem = sub.add_parser("memory", help="working memory notes").add_subparsers(dest="sub", required=True)
    p = leaf(mem, "add", "memory-add", "create a decision/fact/lesson/handoff/preference/runbook note")
    p.add_argument("kind", choices=["decision", "fact", "lesson", "handoff", "preference", "runbook"])
    p.add_argument("--title", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--text-file")
    p.add_argument("--project", help="project path this note belongs to (usually the session cwd)")
    p.add_argument("--tag", action="append")
    p.add_argument("--source", default="manual")
    p.add_argument("--confidence", choices=list(memory.CONFIDENCE), help="default: inferred (unverified for automatic notes)")
    p.add_argument("--verification", help="how it was checked (required to keep 'verified')")
    p.add_argument("--generality", help="where a lesson/preference/runbook applies beyond one project")
    p = leaf(mem, "list", "memory-list", "list memory notes, newest first")
    p.add_argument("--kind")
    p.add_argument("--project")
    p.add_argument("--status", default="active", help="active|superseded|retracted|suppressed|any")
    p.add_argument("--source", help="filter by source, e.g. 'assistant-auto' for notes saved without asking")
    p.add_argument("--limit", type=int, default=50)
    p = leaf(mem, "show", "memory-show", "show a memory note")
    p.add_argument("id")
    p = leaf(mem, "supersede", "memory-supersede", "mark a note superseded by a newer one (nothing is deleted)")
    p.add_argument("id")
    p.add_argument("--by", required=True)
    p = leaf(mem, "retract", "memory-retract", "withdraw a note: kept, never injected again (safe 'forget')")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--redact", action="store_true", help="also blank its text (the file stays)")
    p = leaf(mem, "suppress", "memory-suppress",
             "hide a note from hints without saying it was wrong -- reversible, no reason needed")
    p.add_argument("id")
    p = leaf(mem, "unsuppress", "memory-unsuppress", "bring a suppressed note back into hints")
    p.add_argument("id")

    sk = sub.add_parser("skills", help="find, create, audit and install Agent Skills").add_subparsers(dest="sub", required=True)
    p = leaf(sk, "scan", "skills-scan", "rebuild the registry of every skill on this machine")
    p.add_argument("--project-dir", help="also include <dir>/.claude/skills")
    p = leaf(sk, "find", "skills-find", "search existing skills (installed, plugins, marketplaces, review)")
    p.add_argument("text", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--installed", action="store_true", help="only skills already installed")
    p.add_argument("--remote", action="store_true", help="search public GitHub SKILL.md files instead")
    p.add_argument("--project-dir")
    p = leaf(sk, "show", "skills-show", "all locations and a preview of one skill")
    p.add_argument("name")
    p.add_argument("--project-dir")
    p = leaf(sk, "audit", "skills-audit", "security-scan an existing skill")
    p.add_argument("name", nargs="?")
    p.add_argument("--path", help="scan this skill folder instead of a registry name")
    p = leaf(sk, "suggest", "skills-suggest", "candidate skill clusters found on their own (link communities of mature pages), no --query needed")
    p.add_argument("--limit", type=int, default=10)
    p = leaf(sk, "material", "skills-material", "evidence for a new skill: mature pages, memory notes, git patterns")
    p.add_argument("--query")
    p.add_argument("--git", help="repository to mine for recurring change patterns")
    p.add_argument("--limit", type=int, default=15)
    p = leaf(sk, "draft", "skills-draft", "create a draft folder for a new skill")
    p.add_argument("name")
    p = leaf(sk, "submit", "skills-submit", "validate + security-scan a draft, publish it to the review folder")
    p.add_argument("draft")
    p.add_argument("--origin", required=True, choices=["vault", "sessions", "git", "manual"])
    p.add_argument("--source", action="append", help="page path, memory note, repo or session it was built from")
    p.add_argument("--replace", action="store_true", help="publish a new version (the old one is archived)")
    leaf(sk, "generated", "skills-generated", "skills waiting in the review folder")
    p = leaf(sk, "patch", "skills-patch", "edit a generated skill: replace text that occurs exactly once (new version)")
    p.add_argument("name")
    p.add_argument("--file", default="SKILL.md", help="file inside the skill (default SKILL.md)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--old")
    group.add_argument("--old-file")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--new")
    group.add_argument("--new-file")
    p = leaf(sk, "stocktake", "skills-stocktake", "quality audit of skills: triggers, length, broken references, near-duplicates")
    p.add_argument("--name")
    p.add_argument("--scope", default="user", choices=["user", "generated", "all"])
    p = leaf(sk, "eval-readiness", "skills-eval-readiness", "which skills have a tests.yaml of verifiable scenarios")
    p.add_argument("--name")
    p.add_argument("--scope", default="user", choices=["user", "generated", "all"])
    p = leaf(sk, "run-tests", "skills-run-tests",
             "actually run a skill's tests.yaml scenarios on a throwaway vault and show the real result")
    p.add_argument("name")
    p = leaf(sk, "export-evals", "skills-export-evals",
             "translate tests.yaml into evals/evals.json, the format the skill-creator skill reads")
    p.add_argument("name")
    p = leaf(sk, "triggers", "skills-triggers", "do skills fire when their trigger phrases are used? (measured on transcripts)")
    p.add_argument("--name")
    leaf(sk, "usage", "skills-usage", "how often each skill was used (from Claude Code transcripts)")
    p = leaf(sk, "curate", "skills-curate", "unused, doubly installed and never-installed skills (report only)")
    p.add_argument("--days", type=int, default=60, help="unused for at least this many days")
    for name, key, what in (("archive", "skills-archive", "move a user-level skill into skills-archive/ (reversible)"),
                            ("restore", "skills-restore", "bring an archived skill back"),
                            ("pin", "skills-pin", "never propose this skill for archiving"),
                            ("unpin", "skills-unpin", "remove the pin")):
        p = leaf(sk, name, key, what)
        p.add_argument("name")
    p = leaf(sk, "install", "skills-install", "install a reviewed skill (explicit, re-scanned, backed up)")
    p.add_argument("name")
    p.add_argument("--target", required=True, choices=["claude", "agents", "project"])
    p.add_argument("--replace", action="store_true")
    p.add_argument("--project-dir")

    sub.add_parser("mcp", help="run the read-only MCP server on stdio").set_defaults(_cmd="mcp")
    p = sub.add_parser("app", help="run the desktop app (local web UI on 127.0.0.1)")
    p.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p.add_argument("--no-window", action="store_true", help="only print the URL (used by the Tauri shell)")
    p.add_argument("--parent-pid", type=int, default=0, help="exit when this process ends (the desktop app passes its own)")
    p.set_defaults(_cmd="app")

    prj = sub.add_parser("projects", help="new project folders for Remote Control sessions from the phone").add_subparsers(dest="sub", required=True)
    p = leaf(prj, "root", "projects-root", "show or set the folder where new projects are created")
    p.add_argument("path", nargs="?")
    p = leaf(prj, "new", "projects-new", "create a project folder (CLAUDE.md, git init, second-brain source)")
    p.add_argument("name")
    p.add_argument("--description", default="")
    p.add_argument("--no-git", action="store_true")
    p.add_argument("--no-source", action="store_true", help="do not register it as a Mnemoloci source")
    leaf(prj, "list", "projects-list", "folders under the projects root")
    p = leaf(prj, "rc-autostart", "projects-rc-autostart", "start `claude remote-control` in the root at Windows login")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")

    rem = sub.add_parser("remote", help="use the app from the phone through Tailscale").add_subparsers(dest="sub", required=True)
    leaf(rem, "setup", "remote-setup", "step-by-step instructions and current state")
    p = rem.add_parser("serve", help="run the app server for the phone (fixed port, no window)")
    p.add_argument("--port", type=int, default=8790)
    p.set_defaults(_cmd="remote-serve")
    leaf(rem, "pair", "remote-pair", "create a one-time 8-character code to pair a phone")
    leaf(rem, "devices", "remote-devices", "paired devices and access policy")
    p = leaf(rem, "revoke", "remote-revoke", "revoke a paired device (kept for audit)")
    p.add_argument("id")
    p = leaf(rem, "grant", "remote-grant", "set what a paired device may do (new devices: read + capture only)")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", action="store_true", help="also edit pages, approve reviews, change goals/profile/tags")
    g.add_argument("--capture", action="store_true", help="read and capture links, texts, todos, digests")
    for name, key, what in (("allow-host", "remote-allow-host", "tailnet host name, e.g. mypc.tail1234.ts.net"),
                            ("disallow-host", "remote-disallow-host", "host name to remove"),
                            ("allow-login", "remote-allow-login", "Tailscale login allowed (e-mail)"),
                            ("disallow-login", "remote-disallow-login", "login to remove")):
        p = leaf(rem, name, key, what)
        p.add_argument("values", nargs="+")
    p = leaf(rem, "autostart", "remote-autostart", "start the phone server at Windows login")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")

    conn = sub.add_parser("connectors", help="periodic sync from external services (Google Drive, OneDrive, Gmail, "
                          "GitHub, Notion); never calls an LLM, only downloads content").add_subparsers(dest="sub", required=True)
    leaf(conn, "list", "connectors-list", "configured connectors and their status")

    def connector_group(cmd_name: str, provider: str, help_text: str, folder_help: str) -> None:
        """oauth providers (google-drive, onedrive, gmail) get setup+connect; token providers
        (github, notion) get a single token leaf instead -- same 5-command shape either way."""
        prefix = f"connectors-{cmd_name}-"
        group = conn.add_parser(cmd_name, help=help_text).add_subparsers(dest="provider_cmd", required=True)
        if connectors.PROVIDERS[provider]["auth_kind"] == "oauth":
            p = leaf(group, "setup", f"{prefix}setup",
                     "register the OAuth app credentials you created on the provider's developer console")
            p.add_argument("--client-id", required=True)
            p.add_argument("--client-secret", required=True)
            leaf(group, "connect", f"{prefix}connect", "one-time browser consent (opens your browser)")
        else:
            p = leaf(group, "token", f"{prefix}token", "set the personal access / integration token")
            p.add_argument("value")
        p = leaf(group, "set-folder", f"{prefix}set-folder", f"what to sync: {folder_help}")
        p.add_argument("folder_id")
        p.add_argument("--name", help="a label to show in the app")
        leaf(group, "sync", f"{prefix}sync", "sync now")
        leaf(group, "disable", f"{prefix}disable", "revoke the stored credentials (never touches files already imported)")

    connector_group("google-drive", "google_drive", "Google Drive folder sync", "the folder ID (from the Drive URL)")
    connector_group("onedrive", "onedrive", "OneDrive folder sync", "the folder ID (from the OneDrive URL)")
    connector_group("gmail", "gmail", "Gmail message sync", "a Gmail search query, e.g. label:appunti")
    connector_group("github", "github", "GitHub repository sync", "owner/repo, or owner/repo/path")
    connector_group("notion", "notion", "Notion database sync", "the Notion database ID")

    inb = sub.add_parser("inbox", help="screenshots and images (phone, Telegram, files)").add_subparsers(dest="sub", required=True)
    p = leaf(inb, "add", "inbox-add", "copy image files into the inbox")
    p.add_argument("files", nargs="+")
    p.add_argument("--note")
    p = leaf(inb, "from-session", "inbox-from-session", "save images attached in the current Claude Code chat")
    p.add_argument("--cwd", help="project folder of the session (default: current directory)")
    p.add_argument("--transcript", help="explicit transcript .jsonl path")
    p.add_argument("--last", type=int, default=5, help="newest N images (default 5)")
    p.add_argument("--note")
    leaf(inb, "pending", "inbox-pending", "images still waiting for a transcription")
    p = leaf(inb, "transcribe", "inbox-transcribe", "store the transcription/description of an image")
    p.add_argument("id")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--text-file")
    p = leaf(inb, "ocr", "inbox-ocr", "extract text from a pending image instead of transcribing it by hand")
    p.add_argument("id")

    hook = sub.add_parser("hook", help="Claude Code hook entry points (read event JSON on stdin)")
    hook.add_argument("event", choices=["session-start", "pre-compact", "session-end", "user-prompt"])
    hook.set_defaults(_cmd="hook")

    t = sub.add_parser("txn", help="low-level journaled writes").add_subparsers(dest="sub", required=True)
    p = leaf(t, "plan", "txn-plan", "validate a bundle and print its approval hash")
    p.add_argument("bundle")
    p = leaf(t, "apply", "txn-apply", "apply a bundle whose plan hash was approved")
    p.add_argument("bundle")
    p.add_argument("--approved", required=True, metavar="SHA256")
    leaf(t, "recover", "txn-recover", "roll back interrupted transactions")
    leaf(t, "status", "txn-status", "list interrupted transactions")
    return parser


def _embed():
    from . import embed

    return embed


def _read(path: str) -> str:
    return Path(path).read_text("utf-8")


def _json_list(path: str) -> list[dict[str, Any]]:
    data = json.loads(_read(path))
    if isinstance(data, dict):
        data = data.get("suggestions", [])
    if not isinstance(data, list):
        raise MnemoError("BAD_FILE", "expected a JSON list of suggestions")
    return data


def _text_arg(args) -> str:
    if args.text is not None:
        return args.text
    return Path(args.text_file).read_text("utf-8")


def _init(target: Path, language: str = "en") -> dict[str, Any]:
    out = vault.init(target, language)
    from . import runtime

    if not registered_vault() and not os.environ.get("MNEMOLOCI_VAULT"):  # the first vault becomes the one everything uses
        out["registered_vault"] = str(record_vault(target))

    if not runtime.recorded_python():  # the plugin and the app start the engine with this Python
        try:
            out["python"] = runtime.record_python()["python"]
        except MnemoError as exc:
            out["python_warning"] = exc.message
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args._cmd == "remote-serve":
        from .api import serve as serve_app

        if args.vault:
            os.environ["MNEMOLOCI_VAULT"] = args.vault
        return serve_app(port=args.port, open_window=False, remote_mode=True)
    if args._cmd in ("hook", "mcp", "app"):
        if args.vault:
            os.environ["MNEMOLOCI_VAULT"] = args.vault
        if args._cmd == "mcp":
            from .mcp_server import serve

            return serve()
        if args._cmd == "app":
            from .api import serve as serve_app

            try:
                return serve_app(port=args.port, open_window=not args.no_window, parent_pid=args.parent_pid)
            except MnemoError as exc:
                _emit({"ok": False, "code": exc.code, "message": exc.message})
                return 2
        return hooks.run(args.event)
    args.vault_arg = args.vault  # what was typed, before the default is filled in
    args.vault = resolve_vault(args.vault)
    try:
        result = COMMANDS[args._cmd](args)
        _emit({"ok": True, **result} if isinstance(result, dict) else {"ok": True, "result": result})
        gate = result.get("gate") if isinstance(result, dict) else None
        return 1 if gate and not gate["passed"] else 0  # `quality --min-score` as a CI-style gate
    except MnemoError as exc:
        out = {"ok": False, "code": exc.code, "message": exc.message}
        if exc.details is not None:
            out["details"] = exc.details
        _emit(out)
        return 2


if __name__ == "__main__":
    sys.exit(main())
