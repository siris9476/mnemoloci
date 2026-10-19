---
name: mnemoloci-backup
description: Back up the Mnemoloci vault with git, optionally to a remote copy, and bring back earlier versions of notes. Use when the user says "turn on the backup", "back up my second brain", "sync the vault", "what did this page look like before", "restore yesterday's version", or, in Italian, "attiva il backup del second brain", "fai un backup", "sincronizza il vault", "com'era questa pagina prima", "ripristina la versione di ieri".
---

# Backup of the vault

## Set up (once)

```
mnemoloci backup init [--remote <url>] [--no-auto-commit] [--auto-push]
```

- It creates a git repository **inside the vault** and takes the first backup.
- From then on, every session end commits whatever changed (`auto_commit`), unless the user chose `--no-auto-commit`.
- Engine data that is rebuilt automatically (search indexes, compile scratch, journals, the lock) and phone pairings are never committed.
- `--remote` is for an off-machine copy. **Ask before using one:** the vault holds personal notes, profile and goals. Recommend a **private** repository, and never create one or push without the user's explicit yes.
- `--auto-push` also pushes at session end. Enable it only if the user asks. The push runs in the background and never prompts for credentials, so git must already be able to authenticate.

## Everyday

```
mnemoloci backup now [--message "…"] [--push]
mnemoloci backup status        # last backup, uncommitted changes, commits not yet pushed
mnemoloci backup history [<path>] [--limit 20]
```

A backup is refused while a write is interrupted (`INCOMPLETE_TRANSACTION`). Run `mnemoloci txn recover` first.

## Bring back an earlier version

1. Find it with `mnemoloci backup history <path>`.
2. Show it: `git -C <vault> show <commit>:<path>`.
3. Show the user the differences and **ask before restoring**.
4. To restore, write the old text back through the engine:
   - a wiki page: the app editor, or a new compile;
   - a memory note: `memory add` a new note, then `memory supersede` the old one;
   - goals, profile and todos: their commands.

   Never `git checkout` over the vault while the app or a compile is running. Nothing is lost either way: the version you replace stays in the history.
