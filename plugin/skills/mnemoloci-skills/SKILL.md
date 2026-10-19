---
name: mnemoloci-skills
description: Find Agent Skills that already exist on this machine (Claude Code, plugins, marketplaces, ~/.agents for Kimi/Codex) and create new ones from verified knowledge, session lessons, or git history, with security scanning and a review step before install. Use when the user asks "is there already a skill for…" ("c'è già una skill per…"), wants a new skill, wants to turn a repeated workflow into a skill, or asks whether a skill is safe.
---

# Skills: find first, then create

## 1. Always search before creating

```
mnemoloci skills find <words…> [--installed] [--limit 10]
mnemoloci skills show <name>
```

The first `find` scans every skill folder:
- `~/.claude/skills` (or the folder `CLAUDE_CONFIG_DIR` points to);
- installed plugins;
- marketplace catalogs (installable but not installed);
- `~/.agents/skills`;
- the vault's review folder.

Each result lists all copies of that name, and `installed` tells you whether Claude Code already has it.

Show the user the best matches:
- If an installed skill already fits, stop there.
- If a marketplace one fits, say which plugin provides it. Installing plugins is the user's action via `/plugin`.
- If nothing local fits, you may try `mnemoloci skills find <words> --remote`. That is public GitHub: say the results are unvetted, and audit before using any.

## 2. Check a skill's safety

```
mnemoloci skills audit <name>          # every local copy
mnemoloci skills audit --path <folder> # any folder, e.g. a downloaded skill
```

The verdict is `safe`, `caution` or `dangerous`, with findings by file and line. Explain the findings plainly:
- `caution` often has a legitimate reason, such as a documented crontab line or a credentials path the skill protects.
- `dangerous` means don't install it until a human has read the flagged lines.

## 3. Create a new skill, only after the user agrees

1. **Gather evidence.** Base the skill on something real, not on your general knowledge.
   ```
   mnemoloci skills material --query "<topic>"   # mature wiki pages + memory notes (runbooks, lessons)
   mnemoloci skills material --git <repo path>    # recurring commit types, co-changed files, recent work
   ```
   - Only `mature_pages` qualify as knowledge sources: confidence ≥ 0.8, extracted or merged, not contradicted, not stale.
   - `excluded_pages` shows why the others were left out. Tell the user if the evidence is thin, for example fewer than 3 mature pages.
2. **Create the draft.** `mnemoloci skills draft <name>` returns the draft folder and warns if the name is already used anywhere.
3. **Write the draft.**
   - Read `references/patterns.md` first — patterns and pitfalls for writing and managing skills.
   - `SKILL.md` has frontmatter `name` (the folder name) and `description`.
   - The description says what the skill does **and when to use it**, with the trigger phrases a user would say. Keep it at most 1024 characters.
   - The body holds procedural knowledge distilled from the evidence: steps, decisions, pitfalls. Don't copy pages; link to them by name when useful.
   - Keep uncertainty visible, for example "(dedotto, da verificare)".
   - Extra material goes under `references/`, `templates/`, `scripts/` or `assets/` only.
   - Prefer instructions over scripts; if you add a script, keep it short and readable.
4. **Submit.**
   ```
   mnemoloci skills submit <draft folder> --origin vault|sessions|git|manual --source <page or repo>…
   ```
   - The engine validates the draft against the skill format rules and runs the security scan **before writing anything**.
   - `SKILL_BLOCKED` means nothing was written. Fix the flagged lines; never try to evade the scanner.
   - On success the skill lands in the vault at `_generated-skills/<name>/`, with a `SKILL_FACTORY.md` provenance manifest.
   - A new version needs `--replace`; the old version is archived, not deleted.
5. **Review with the user.** Show `SKILL.md` and the verdict. For a quality check or improvement loop, use the `skill-creator` skill, which provides evals and description optimisation.

## 4. Install, only when the user explicitly asks

```
mnemoloci skills install <name> --target claude|agents|project [--project-dir DIR] [--replace]
```

- `claude`: Claude Code user skills (`$CLAUDE_CONFIG_DIR\skills`).
- `agents`: `~/.agents/skills`, read by Kimi Code CLI, Codex and other agents.
- `project`: `<project>\.claude\skills`.

The engine re-validates and re-scans the reviewed copy, because it may have been edited in Obsidian. It never overwrites without `--replace`; with it, the previous copy is backed up. The skill becomes visible from the next Claude Code session.

Never copy skills into agent folders yourself; always go through `install`.

## 5. Improve a generated skill

```
mnemoloci skills patch <name> --old "<exact text>" --new "<replacement>" [--file references/x.md]
```

- `--old` must occur **exactly once**, otherwise the engine refuses with `PATCH_NOT_UNIQUE`. Include enough surrounding text to make it unique. For long passages use `--old-file` and `--new-file`.
- The result is re-validated and re-scanned and published as a new version; the previous one is archived.
- Installed copies do not change by themselves: reinstall with `skills install <name> --target … --replace`.
- Only skills the engine generated can be patched. For other skills, propose the change to the user.

## 6. Keep the skill collection tidy

```
mnemoloci skills stocktake [--name N] [--scope user|generated|all]   # quality, not just safety
```

The stocktake reports these problems, with a 0-100 score per skill:
- a description that doesn't say when to use the skill, or is too short;
- a name that doesn't match the folder;
- a body over 500 lines;
- references to files that don't exist, and files that are never mentioned;
- descriptions that are near-duplicates of another skill.

Fix generated skills with `skills patch`. For other skills, suggest the fix, or use `skill-creator`.

```
mnemoloci skills triggers [--name N]   # do skills fire when their trigger phrases are used?
```

The trigger phrases are the quoted ones in a skill's description. For every later user message containing one, the command checks whether that skill ran before the next message. Messages written before the skill was installed or updated don't count.
- A low `rate` means the description doesn't make Claude choose the skill. Sharpen it with `skill-creator` or `skills patch`.
- `measured: 0` just means there's no data yet.

```
mnemoloci skills usage                 # uses per skill, from the Claude Code transcripts
mnemoloci skills curate [--days 60]    # report only
```

`curate` reports:
- skills installed but unused for N days, and which of them are `archivable` (user-level folders);
- skills installed twice, for example in both `~/.claude` and `~/.agents`;
- generated skills never installed;
- the most used skills.

Show the report and let the user decide. Nothing is archived automatically.

```
mnemoloci skills archive <name>    # moves the folder into skills-archive/ next to it
mnemoloci skills restore <name>    # brings it back
mnemoloci skills pin <name>        # never propose it again
mnemoloci skills unpin <name>
```

Plugin skills belong to their plugin: to remove them, the user disables or uninstalls the plugin with `/plugin`.

## 7. From instincts to a skill

`mnemoloci instincts evolve` groups approved instincts that share a subject. A group of two or more is good material for a skill:
1. Draft the skill from that group.
2. Submit it with `--origin instincts --source <instinct path>…`.
