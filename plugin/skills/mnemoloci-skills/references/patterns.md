# Writing and managing skills: patterns and pitfalls

Read this before writing a new `SKILL.md`, not instead of gathering the new skill's own evidence.

## What makes a description trigger reliably

A good description has two parts: a mechanism sentence (what the skill does), then a trigger clause
("Use when...", "Use for...") with the phrases a user would actually say. A description that skips the
trigger clause, or buries it, is harder to tell apart from a neighbouring skill at a glance.

Description quality can be treated as a measurable problem: write test queries, set some aside as a
held-out set, and score candidate descriptions against the held-out set only, so the description is not
overfitted to the queries used to write it. Keep the description short (one sentence for the mechanism),
avoid marketing words, and verify any claim about a platform against the real code path instead of
copying it from a similar skill.

**Applying this**: write the mechanism sentence first, in one line if possible. Add the trigger clause
as a second sentence with 3-5 concrete phrases the user would say, not a paraphrase of the mechanism.
Don't claim a capability (a platform, an integration, a guarantee) without checking it is actually true
of this skill's own instructions.

## Creating and managing skills automatically: what to keep

- **Generate only from mature material.** Draft from a cluster of mature wiki pages (confidence-gated),
  always write to a separate review folder, never auto-install. This is the `mnemoloci skills draft` /
  `submit` split.
- **Keep the lock outside the skill folder**, so a delete and recreate cannot pull the lock out from
  under a waiting writer. Write atomically, and run a security scan after the write with a real rollback
  if it finds something dangerous.
- **Git history is a second evidence source.** When there is no mature wiki page yet but there is real
  repeated behaviour in a repository's history, `mnemoloci skills material --git` generates from commit
  messages and co-modified files.
- **Documentation alone is the weakest baseline**: a skill that only describes how to write a skill
  generates nothing.

Three properties are deliberate here, so do not regress them when changing the flow: the security scan
is mandatory before any write, evidence can come from mature pages or from git, and nothing is
installed without a request.

## Concrete pitfalls

- **A CLI footgun worked around in every skill instead of fixed once.** If a tool reads the process's
  working directory for git operations even when a path is passed explicitly, every skill that wraps it
  ends up repeating the same defensive wrapper (`cd` into the store before each call). If the same
  workaround shows up repeated across more than one Mnemoloci skill, the fix belongs in the engine, not
  copy-pasted into skill prose.
- **Enforcement strength is a decision, not a detail.** A guard can actively *block* edits until there is
  a concrete investigation, or only *warn* (as `sessions file-risk` does). Pick one deliberately per
  skill and say which in the description: don't imply blocking behaviour the skill doesn't have, or the
  reverse.
- **A feature can exist only because of an unrelated problem.** A step that consolidates hundreds of
  semi-duplicate skills into shared rules solves a problem a small curated collection doesn't have. Don't
  port a pattern just because a bigger, messier collection needed it.
- **A skill should know the dangerous shortcuts of the CLI it wraps and refuse them.** If the tool has a
  slow, unsafe or legacy invocation (for example an unpathed command that triggers a classifier needing
  credentials the environment doesn't have), the skill should name it and route around it, not leave the
  agent to discover it by trial and error.
