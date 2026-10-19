---
name: mnemoloci-new-project
description: Create a brand-new project folder under the user's projects root and move the session there — typically from a Claude Code Remote Control session opened on the phone. Use when the user says "new project X", "create a project", "start a new project", or, in Italian, "nuovo progetto X", "crea un progetto", "inizia un progetto nuovo", or asks to start working on something new in its own folder.
---

# New project from anywhere (phone included)

A session opened from the phone with `claude remote-control` starts in the projects root. The user chooses that folder in the Mnemoloci app (Stato → Cartella progetti) or with `projects root <folder>`, and `projects list` shows the current one. This skill creates the new folder there. The user then moves the session into it with `/cd`.

## 1. Create the folder

Ask for a short name if the user did not give one, and a one-line description of what the project is for. Then run:

```
python -P -m mnemoloci projects new "<nome>" --description "<a cosa serve>"
```

The engine:
- validates the name, which must be a single portable folder name;
- **refuses if the folder already exists**; tell the user they can open it with `/cd` instead;
- writes a starter `CLAUDE.md`;
- runs `git init`;
- registers the folder as a second-brain source.

## 2. Tell the user the exact command to type

The result contains `next`, e.g. `/cd C:\Users\you\Projects\Pippo`. **You cannot run `/cd` yourself.** It is a user-only command by design, because moving the session changes its permissions and configuration. So show the line and ask the user to type or paste it, in the phone app or the terminal.

After the move, Claude Code loads the new folder's `CLAUDE.md` and settings. No trust prompt appears when the root was trusted, because trust on a folder outside git covers its subfolders.

## 3. Continue in the new folder

Once the user has moved, check with `pwd` or by reading `CLAUDE.md`. Then:
- replace the placeholder description in `CLAUDE.md` with what the user explains;
- start the work.

## Other commands

- `python -P -m mnemoloci projects list`: existing project folders, and which are already second-brain sources.
- `python -P -m mnemoloci projects root [<cartella>]`: show or change the projects root.
- `python -P -m mnemoloci projects rc-autostart --enable|--disable`: start `claude remote-control` in the root at Windows login. Only enable it when the user asks.

Before the first use the user must, once:
1. run `claude` in the root and accept the trust dialog;
2. run `claude remote-control` by hand and answer `y` to "Enable Remote Control?".

## Never

- Create project folders with mkdir yourself; always use `projects new`, so the name is validated and the second brain knows about the folder.
- Edit Claude Code's trust configuration (`.claude.json`) to skip the trust dialog. That is a safety check the user must accept.
