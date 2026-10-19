"""Skill usage and curation,
without its automatic side: nothing is archived unless the user asks.

- usage: how often each skill was used, read from Claude Code transcripts: the
  model's Skill tool calls and the user's /slash commands. Cached per transcript.
- curate: installed skills never used lately, the same skill installed twice,
  generated skills never installed, and the most used ones. Pinned skills are
  never proposed.
- archive / restore: move a user-level skill folder (~/.claude/skills,
  ~/.agents/skills) into a sibling skills-archive/ and back. Plugin skills are
  managed by their plugin and only reported.
"""

import json
import re
import shlex
import shutil
import tempfile
import time
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone  # noqa: F401 - timezone used by trigger_audit
from io import StringIO
from pathlib import Path
from typing import Any

from .core.errors import ConflictError, MnemoError
from .core.lock import VaultLock
from .sessions import _iter_transcripts, claude_dirs
from .skill_factory import STATE_SCHEMA
from .skill_registry import _connect as registry_db
from .skill_registry import _ensure_fresh, invalidate
from .vault import init as init_vault
from .vault import load_state, require_vault, save_state

USAGE_SCHEMA = "mnemoloci.skill-usage.v1"
ARCHIVABLE = {"claude", "agents"}
_COMMAND = re.compile(r"<command-name>/?([A-Za-z0-9:._-]+)</command-name>")


def _scan_file(path: Path) -> dict[str, list]:
    counts: dict[str, list] = {}

    def hit(name: str, ts: str) -> None:
        entry = counts.setdefault(name, [0, ""])
        entry[0] += 1
        entry[1] = max(entry[1], ts or "")

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            has_tool, has_cmd = '"Skill"' in line, "<command-name>" in line  # cheap pre-filter, any JSON spacing
            if not (has_tool or has_cmd):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("isSidechain"):
                continue
            ts = rec.get("timestamp", "")
            content = (rec.get("message") or {}).get("content")
            if has_tool and rec.get("type") == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                        skill = (block.get("input") or {}).get("skill")
                        if isinstance(skill, str) and skill:
                            hit(skill, ts)
            if has_cmd and rec.get("type") == "user":
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict))
                for name in _COMMAND.findall(text):
                    hit(name, ts)
    return counts


def usage(vault: Path) -> dict[str, dict[str, Any]]:
    """skill name (as invoked, e.g. `plugin:skill` or `skill`) -> {count, last_used, sessions}."""
    require_vault(vault)
    with VaultLock(vault) as lock:
        cache = load_state(vault, "skill_usage.json", USAGE_SCHEMA, {"files": {}})
        for _, path in _iter_transcripts(claude_dirs()):
            key, st = str(path.resolve()), path.stat()
            known = cache["files"].get(key)
            if known and known["size"] == st.st_size and known["mtime_ns"] == st.st_mtime_ns:
                continue
            cache["files"][key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "counts": _scan_file(path)}
        save_state(vault, lock, "skill_usage.json", cache)
    total: dict[str, dict[str, Any]] = {}
    for entry in cache["files"].values():
        for name, (count, last) in entry["counts"].items():
            t = total.setdefault(name, {"count": 0, "last_used": "", "sessions": 0})
            t["count"] += count
            t["sessions"] += 1
            t["last_used"] = max(t["last_used"], last)
    return total


def _installed(vault: Path) -> list[dict[str, Any]]:
    _ensure_fresh(vault, None)
    from contextlib import closing

    with closing(registry_db(vault)) as conn:
        rows = conn.execute("SELECT path, name, label, plugin, link FROM skills WHERE label IN ('claude','agents','plugin')").fetchall()
    return [{"path": str(Path(p).parent), "name": n, "label": label, "plugin": plugin, "link": bool(link)}
            for p, n, label, plugin, link in rows]


def _invoked_as(skill: dict[str, Any]) -> set[str]:
    names = {skill["name"], Path(skill["path"]).name}
    if skill["plugin"]:
        plugin = skill["plugin"].split("/")[-1]
        names |= {f"{plugin}:{n}" for n in list(names)}
    return names


def _state(vault: Path) -> dict[str, Any]:
    state = load_state(vault, "skills.json", STATE_SCHEMA, {"generated": {}, "installs": []})
    state.setdefault("pinned", [])
    state.setdefault("archived", [])
    return state


def _installed_since(skill: dict[str, Any], since: str) -> bool:
    try:
        stamp = (Path(skill["path"]) / "SKILL.md").stat().st_mtime
    except OSError:
        return False
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") >= since


def curate(vault: Path, *, unused_days: int = 60) -> dict[str, Any]:
    used = usage(vault)
    installed = _installed(vault)
    state = _state(vault)
    since = (datetime.now(timezone.utc) - timedelta(days=unused_days)).strftime("%Y-%m-%dT%H:%M:%S")
    unused, by_name, rows = [], {}, []
    for skill in installed:
        names = _invoked_as(skill)
        stats = [used[n] for n in names if n in used]
        count = sum(s["count"] for s in stats)
        last = max((s["last_used"] for s in stats), default="")
        rows.append({**skill, "count": count, "last_used": last[:10] or None})
        by_name.setdefault(skill["name"], []).append(skill)
        if skill["name"] in state["pinned"] or last >= since or _installed_since(skill, since):
            continue  # a skill installed inside the window has not had the chance to be used yet
        unused.append({"name": skill["name"], "label": skill["label"], "plugin": skill["plugin"], "path": skill["path"],
                       "last_used": last[:10] or None, "uses": count,
                       "archivable": skill["label"] in ARCHIVABLE and not skill["link"]})
    return {
        "installed": len(installed), "window_days": unused_days,
        "most_used": sorted((r for r in rows if r["count"]), key=lambda r: -r["count"])[:15],
        "unused": sorted(unused, key=lambda u: (u["label"] != "claude", u["name"])),
        "installed_twice": [{"name": n, "locations": [f"{s['label']}: {s['path']}" for s in skills]}
                            for n, skills in sorted(by_name.items()) if len(skills) > 1],
        "generated_not_installed": sorted(n for n in state["generated"]
                                          if not any(i["name"] == n for i in state["installs"])),
        "pinned": state["pinned"], "archived": state["archived"],
        "note": "nothing is archived automatically: `skills archive <name>` moves a user skill aside, `skills restore` brings it back",
    }


_TRIGGER = re.compile(r"\b(use (it |this )?when|use for|usa(la)? quando|quando l'utente|when the user|trigger|"
                      r"if the user|se l'utente)\b", re.IGNORECASE)
_REF = re.compile(r"(?:\]\(|`)((?:references|templates|scripts|assets)/[^)`\s]+)")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.casefold())}


def _check_skill(folder: Path) -> dict[str, Any]:
    from .markdown import split_frontmatter

    issues: list[str] = []
    skill_md = folder / "SKILL.md"
    text = skill_md.read_text("utf-8", errors="replace")
    try:
        fm, body = split_frontmatter(text)
    except Exception as exc:  # noqa: BLE001 - a broken frontmatter is itself the finding
        return {"issues": [f"unreadable frontmatter: {exc}"], "score": 0, "description": ""}
    fm = fm or {}
    name, description = str(fm.get("name") or ""), " ".join(str(fm.get("description") or "").split())
    if not name or not description:
        issues.append("name or description missing from the frontmatter")
    elif name != folder.name and ":" not in name:
        issues.append(f"the name '{name}' does not match the folder '{folder.name}'")
    if description and len(description) < 60:
        issues.append("description too short to tell when to activate it")
    if len(description) > 1024:
        issues.append("description over 1024 characters")
    if description and not _TRIGGER.search(description):
        issues.append("the description does not say when to use it (phrases or situations that trigger it)")
    lines = body.count("\n") + 1
    if lines > 500:
        issues.append(f"long body ({lines} lines): move the details to references/")
    if lines > 30 and "\n#" not in "\n" + body:
        issues.append("no heading: hard to follow")
    # trailing punctuation is prose, and <name>/{x}/* are placeholders, not files
    mentioned = {r.rstrip(".,;:'\")") for r in _REF.findall(body) if not re.search(r"[<>{}*]", r)}
    for ref in sorted(mentioned):
        # "assets/x" or "scripts/x" may mean the user's project; only flag them when the skill has that folder
        owned = ref.startswith("references/") or (folder / ref.split("/")[0]).is_dir()
        if owned and not (folder / ref.split("#")[0]).exists():
            issues.append(f"reference to a file that does not exist: {ref}")
    extra = [p.relative_to(folder).as_posix() for p in folder.rglob("*")
             if p.is_file() and p.name not in ("SKILL.md", "SKILL_FACTORY.md", "LICENSE", "LICENSE.txt", "README.md")]
    orphans = [f for f in extra if not any(f.startswith(m.split("#")[0]) or m.startswith(f) for m in mentioned)
               and f.split("/")[-1] not in body]
    if orphans:
        issues.append(f"{len(orphans)} files never mentioned in SKILL.md (e.g. {orphans[0]})")
    return {"issues": issues, "score": max(0, 100 - 20 * len(issues)), "description": description}


def stocktake(vault: Path, *, name: str | None = None, scope: str = "user") -> dict[str, Any]:
    """Quality audit of skills, deterministic and read-only.
    scope: user (~/.claude, ~/.agents), generated (review folder), all (plugins too)."""
    require_vault(vault)
    labels = {"user": {"claude", "agents"}, "generated": {"review"}, "all": {"claude", "agents", "plugin", "review"}}[scope]
    _ensure_fresh(vault, None)
    from contextlib import closing

    with closing(registry_db(vault)) as conn:
        rows = conn.execute("SELECT path, name, label FROM skills").fetchall()
    rows = [(Path(p).parent, n, label) for p, n, label in rows if label in labels and (not name or n == name)]
    results = []
    for folder, skill_name, label in rows:
        if not (folder / "SKILL.md").is_file():
            continue
        results.append({"name": skill_name, "label": label, "path": str(folder), **_check_skill(folder)})
    words = {r["path"]: _words(r["description"]) for r in results}
    for a in results:
        for b in results:
            if a["path"] < b["path"] and a["name"] != b["name"] and words[a["path"]] and words[b["path"]]:
                overlap = len(words[a["path"]] & words[b["path"]]) / len(words[a["path"]] | words[b["path"]])
                if overlap >= 0.6:
                    a["issues"].append(f"molto simile a '{b['name']}' ({overlap:.0%} parole della descrizione)")
                    a["score"] = max(0, a["score"] - 10)
    for r in results:
        r.pop("description")
    results.sort(key=lambda r: (r["score"], r["name"]))
    return {"skills": len(results), "with_issues": sum(1 for r in results if r["issues"]),
            "average_score": round(sum(r["score"] for r in results) / len(results)) if results else None,
            "results": results[:80] if not name else results,
            "next": "improve a generated skill with `skills patch`, or use the skill-creator skill for a full rewrite"}


def _read_tests(folder: Path) -> tuple[list[dict[str, Any]], str | None]:
    """(scenarios, error) for folder/tests.yaml. A missing file is not an error -- it just means
    no coverage yet; a malformed one is, so the gap stays visible instead of silently "no file"."""
    import yaml

    path = folder / "tests.yaml"
    if not path.is_file():
        return [], None
    try:
        data = yaml.safe_load(path.read_text("utf-8", errors="replace")) or []
    except yaml.YAMLError as exc:
        return [], f"tests.yaml is not valid YAML: {exc}"
    if not isinstance(data, list):
        return [], "tests.yaml must be a list of scenarios"
    scenarios = [s for s in data if isinstance(s, dict) and s.get("input") and s.get("expect_cli")]
    return scenarios, None if len(scenarios) == len(data) else "one or more scenarios without 'input' or 'expect_cli'"


def eval_readiness(vault: Path, *, name: str | None = None, scope: str = "user") -> dict[str, Any]:
    """Which skills have a tests.yaml of verifiable scenarios and which don't -- a completeness
    inventory, not a quality score (Mnemoloci deliberately hands off actual quality/correctness
    evaluation to the general-purpose skill-creator skill; this only tracks whether the raw
    material for that evaluation exists). Scenarios are read here, never executed: skill-creator,
    or a human, runs them against the real engine. Format per scenario in tests.yaml (a list):
    {input: "what the user said", expect_cli: "the mnemoloci command the skill should run",
    expect_assertion: "optional: what a passing result looks like"}."""
    require_vault(vault)
    labels = {"user": {"claude", "agents"}, "generated": {"review"}, "all": {"claude", "agents", "plugin", "review"}}[scope]
    _ensure_fresh(vault, None)
    from contextlib import closing

    with closing(registry_db(vault)) as conn:
        rows = conn.execute("SELECT path, name, label FROM skills").fetchall()
    rows = [(Path(p).parent, n, label) for p, n, label in rows if label in labels and (not name or n == name)]
    results = []
    for folder, skill_name, label in rows:
        if not (folder / "SKILL.md").is_file():
            continue
        scenarios, error = _read_tests(folder)
        results.append({"name": skill_name, "label": label, "path": str(folder), "scenarios": len(scenarios),
                        "covered": bool(scenarios), "error": error})
    results.sort(key=lambda r: (r["covered"], r["name"]))
    return {"skills": len(results), "covered": sum(1 for r in results if r["covered"]),
            "no_coverage": [r["name"] for r in results if not r["covered"] and not r["error"]],
            "malformed": [r["name"] for r in results if r["error"]],
            "results": results,
            "note": "'skills run-tests NAME' actually executes each scenario's expect_cli (on a "
                    "throwaway vault) and shows the real result; it does not judge whether Claude "
                    "would choose to run it -- for that, hand the scenarios to skill-creator"}


_BLOCKED_CMDS = {"remote-serve", "hook", "mcp", "app"}  # long-running/server commands: never inside a test scenario


def _run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Run one tests.yaml scenario's expect_cli for real, on a fresh throwaway vault that is
    always deleted afterward -- the user's real vault is never touched, and any --vault the
    scenario text tried to specify is stripped and overridden. Reports the real result next to
    expect_assertion; does not judge whether they match (that needs a human, or skill-creator)."""
    from .cli import COMMANDS, build_parser  # local: cli.py imports this module at load time

    out: dict[str, Any] = {"input": scenario["input"], "expect_cli": scenario["expect_cli"],
                           "expect_assertion": scenario.get("expect_assertion")}
    tokens = shlex.split(scenario["expect_cli"])
    if tokens and tokens[0] == "mnemoloci":
        tokens = tokens[1:]
    cleaned, skip_next = [], False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok == "--vault":
            skip_next = True
            continue
        if not tok.startswith("--vault="):
            cleaned.append(tok)

    scratch = Path(tempfile.mkdtemp(prefix="mnemoloci-eval-"))
    try:
        init_vault(scratch)
        errors = StringIO()
        try:
            with redirect_stderr(errors):
                args = build_parser().parse_args(cleaned)
        except SystemExit:
            out["status"] = "invalid_command"
            out["error"] = errors.getvalue().strip()[-500:] or "expect_cli could not be parsed as a mnemoloci command"
            return out
        if args._cmd in _BLOCKED_CMDS:
            out["status"] = "invalid_command"
            out["error"] = f"'{args._cmd}' is a server/long-running command; it cannot run inside a test scenario"
            return out
        args.vault = scratch
        try:
            result = COMMANDS[args._cmd](args)
        except MnemoError as exc:
            out["status"] = "command_failed"
            out["error"] = f"{exc.code}: {exc.message}"
            return out
        except Exception as exc:  # noqa: BLE001 - a scenario finding, not a harness crash
            out["status"] = "command_failed"
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        out["status"] = "ran"
        out["result"] = result
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_tests(vault: Path, name: str) -> dict[str, Any]:
    """Actually run a skill's tests.yaml scenarios (after `eval_readiness` finds them): each
    expect_cli command executes for real against a throwaway scratch vault, never the user's real
    one. Reports the real result next to expect_assertion so a human can judge the match. Does NOT
    check whether Claude would choose to run this command for the given input -- that is organic
    trigger behaviour (see `trigger_audit`), not something a CLI replay can measure; this only
    proves the scenario itself is well-formed and does what it claims."""
    require_vault(vault)
    _ensure_fresh(vault, None)
    from contextlib import closing

    with closing(registry_db(vault)) as conn:
        rows = conn.execute("SELECT path FROM skills WHERE name = ? AND label IN ('claude','agents','plugin','review')",
                            (name,)).fetchall()
    folder = next((Path(p).parent for (p,) in rows if (Path(p).parent / "SKILL.md").is_file()), None)
    if folder is None:
        raise MnemoError("NO_SUCH_SKILL", f"no installed skill named {name!r}")
    scenarios, error = _read_tests(folder)
    if error:
        raise MnemoError("BAD_TESTS_FILE", f"{name}: {error}")
    if not scenarios:
        return {"skill": name, "scenarios": 0, "ran": 0, "invalid_command": 0, "command_failed": 0, "results": [],
                "note": "no tests.yaml scenarios found -- see `skills eval-readiness`"}
    results = [_run_scenario(s) for s in scenarios]
    return {"skill": name, "scenarios": len(scenarios),
            "ran": sum(1 for r in results if r["status"] == "ran"),
            "invalid_command": sum(1 for r in results if r["status"] == "invalid_command"),
            "command_failed": sum(1 for r in results if r["status"] == "command_failed"),
            "results": results,
            "note": "each 'ran' result shows the real outcome next to expect_assertion for you to compare -- "
                    "this replays the command, it does not grade the assertion"}


def export_evals(vault: Path, name: str) -> dict[str, Any]:
    """Bridge tests.yaml (Mnemoloci's own scenario format) into evals/evals.json, the format
    Anthropic's skill-creator skill reads (see its references/schemas.md) -- the two formats have
    no field in common (input/expect_cli/expect_assertion vs prompt/expected_output/expectations),
    so today a human has to hand-retype scenarios before skill-creator's real-Claude evaluation
    loop can use them. This writes the translated file directly to <skill>/evals/evals.json, the
    exact path skill-creator expects.

    Only scenarios that actually run cleanly (reusing run_tests, not re-implementing it) are
    exported: copying over a scenario whose expect_cli is already known to be broken would be a
    false promise to skill-creator's evaluation loop. Broken ones are reported as skipped instead,
    so the gap stays visible rather than silently dropped."""
    require_vault(vault)
    run = run_tests(vault, name)
    _ensure_fresh(vault, None)
    from contextlib import closing

    with closing(registry_db(vault)) as conn:
        rows = conn.execute("SELECT path FROM skills WHERE name = ? AND label IN ('claude','agents','plugin','review')",
                            (name,)).fetchall()
    folder = next((Path(p).parent for (p,) in rows if (Path(p).parent / "SKILL.md").is_file()), None)
    if folder is None:
        raise MnemoError("NO_SUCH_SKILL", f"no installed skill named {name!r}")

    evals, skipped = [], []
    for i, r in enumerate(run["results"], start=1):
        if r["status"] != "ran":
            skipped.append({"input": r["input"], "status": r["status"], "error": r.get("error")})
            continue
        expectations = [f"Runs a mnemoloci command equivalent to: {r['expect_cli']}"]
        if r.get("expect_assertion"):
            expectations.append(str(r["expect_assertion"]))
        evals.append({"id": i, "prompt": r["input"], "expected_output": r.get("expect_assertion") or "",
                      "files": [], "expectations": expectations})

    out_dir = folder / "evals"
    try:
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "evals.json"
        out_path.write_text(json.dumps({"skill_name": name, "evals": evals}, indent=2, ensure_ascii=False), "utf-8")
    except OSError as exc:
        raise MnemoError("CANNOT_WRITE_EVALS", f"could not write {out_dir / 'evals.json'}: {exc} -- the installed "
                         "skill folder may be read-only; copy it somewhere writable first") from exc
    return {"skill": name, "exported": len(evals), "skipped": skipped, "path": str(out_path),
            "note": "each exported eval's 'expectations' already includes the exact command that ran "
                    "successfully -- hand this to skill-creator, which reads evals/evals.json from "
                    "exactly this path"}


_QUOTED = re.compile(r"[\"“«]([^\"“”«»\n]{3,60})[\"”»]")


def _fold(text: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold().split())


def trigger_audit(vault: Path, *, name: str | None = None, limit: int = 40) -> dict[str, Any]:
    """Does each skill fire when it should (measured, not simulated)?
    Its trigger phrases are the quoted ones in its description; every past user message containing
    one should be followed by that skill (Skill tool or /command) before the next message."""
    require_vault(vault)
    from contextlib import closing

    from .session_index import _connect, refresh

    refresh(vault)
    _ensure_fresh(vault, None)
    with closing(registry_db(vault)) as conn:
        rows = conn.execute("SELECT path, name, label, plugin, description FROM skills "
                            "WHERE label IN ('claude','agents','plugin')").fetchall()
    skills = {}
    for path, skill_name, label, plugin, description in rows:
        if name and skill_name != name:
            continue
        phrases = sorted({_fold(p.replace("…", " ").strip(" .")) for p in _QUOTED.findall(description or "")} - {""})
        phrases = [p for p in phrases if len(p) >= 4 and not re.fullmatch(r"[a-z]", p)]
        if phrases:
            info = {"name": skill_name, "path": str(Path(path).parent), "plugin": plugin}
            try:  # messages written before the skill existed (or before this version) cannot count as misses
                since = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            except OSError:
                since = ""
            skills[skill_name] = {"phrases": phrases, "names": _invoked_as(info), "since": since,
                                  "expected": 0, "fired": 0, "missed": []}
    with closing(_connect(vault)) as conn:
        prompts = conn.execute("SELECT session_id, CAST(idx AS INTEGER), ts, text FROM msgs WHERE role='user' "
                               "ORDER BY session_id, CAST(idx AS INTEGER)").fetchall()
        calls: dict[str, list[tuple[str, str]]] = {}
        for sid, skill, ts in conn.execute("SELECT session_id, skill, ts FROM skill_calls ORDER BY ts"):
            calls.setdefault(sid, []).append((skill, ts))
    for i, (sid, _, ts, text) in enumerate(prompts):
        nxt = prompts[i + 1][2] if i + 1 < len(prompts) and prompts[i + 1][0] == sid else "9999"
        folded = _fold(text[:4000])
        for s in skills.values():
            if ts < s["since"] or not any(p in folded for p in s["phrases"]):
                continue
            s["expected"] += 1
            if any(n in s["names"] and ts <= t < nxt for n, t in calls.get(sid, [])):
                s["fired"] += 1
            elif len(s["missed"]) < 3:
                s["missed"].append({"session": sid[:12], "message": " ".join(text.split())[:120]})
    report = [{"skill": n, "trigger_phrases": s["phrases"][:6], "expected": s["expected"], "fired": s["fired"],
               "rate": round(s["fired"] / s["expected"], 2), "missed_examples": s["missed"]}
              for n, s in skills.items() if s["expected"]]
    report.sort(key=lambda r: (r["rate"], -r["expected"], r["skill"]))
    return {"skills_with_triggers": len(skills), "measured": len(report), "results": report[:limit],
            "note": "a low rate means the description does not make Claude pick the skill: sharpen it (skill-creator)"}


def pin(vault: Path, name: str, pinned: bool = True) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        state = _state(vault)
        if pinned and name not in state["pinned"]:
            state["pinned"].append(name)
        if not pinned and name in state["pinned"]:
            state["pinned"].remove(name)
        save_state(vault, lock, "skills.json", state)
    return {"pinned": sorted(state["pinned"])}


def archive(vault: Path, name: str) -> dict[str, Any]:
    require_vault(vault)
    candidates = [s for s in _installed(vault) if s["name"] == name and s["label"] in ARCHIVABLE and not s["link"]]
    if not candidates:
        raise MnemoError("NOT_ARCHIVABLE", f"{name!r} is not a user-level skill folder (plugin skills belong to their plugin)")
    with VaultLock(vault) as lock:
        state = _state(vault)
        if name in state["pinned"]:
            raise ConflictError("PINNED", f"{name} is pinned: unpin it first")
        moved = []
        for skill in candidates:
            src = Path(skill["path"])
            dest = src.parent.parent / "skills-archive" / f"{src.name}-{time.strftime('%Y%m%d-%H%M%S')}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            record = {"name": name, "from": str(src), "to": str(dest), "archived": time.strftime("%Y-%m-%d")}
            state["archived"].append(record)
            moved.append(record)
        save_state(vault, lock, "skills.json", state)
    invalidate(vault)
    return {"archived": moved, "note": "not deleted: `mnemoloci skills restore " + name + "` puts it back"}


def restore(vault: Path, name: str) -> dict[str, Any]:
    require_vault(vault)
    with VaultLock(vault) as lock:
        state = _state(vault)
        records = [r for r in state["archived"] if r["name"] == name and not r.get("restored")]
        if not records:
            raise MnemoError("NOT_ARCHIVED", f"no archived skill named {name!r}")
        record = records[-1]
        if Path(record["from"]).exists():
            raise ConflictError("NAME_TAKEN", f"{record['from']} exists again: move it away first")
        shutil.move(record["to"], record["from"])
        record["restored"] = time.strftime("%Y-%m-%d")
        save_state(vault, lock, "skills.json", state)
    invalidate(vault)
    return {"restored": name, "path": record["from"], "note": "visible to Claude Code from the next session"}
