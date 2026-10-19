# Mnemoloci

Mnemoloci is a personal second brain for Claude Code. Its source of truth is a Markdown vault, compatible with Obsidian. A deterministic Python engine does the hashing, locking, transactional writes and indexing. Your Claude Code session does the part that needs judgement.

Mnemoloci runs on Windows and Linux. *Leggi questo file in italiano: [README.it.md](README.it.md).*

## A quick look

These screenshots show the app on a demo vault with invented content (a small tide-forecasting project).

**Today:** open to-dos, what is waiting for review, and the pages published today.

![Today view with to-dos and published pages](docs/screenshots/today.png)

**Search:** results with the page type, a summary and the line that matched.

![Search results for "harmonic"](docs/screenshots/search.png)

**A page:** every sentence carries a numbered citation back to the source line it came from.

![A wiki page with citations and links](docs/screenshots/page.png)

**Graph:** how the pages link to each other, with hubs, bridges and communities listed below.

![Link graph of the demo vault](docs/screenshots/graph.png)

**Review:** pages the engine is not sure about wait for your decision before they are published.

![Review queue with one page held for low confidence](docs/screenshots/review.png)

## What it does

- Compiles your projects (code and `.md` files) into cited wiki pages, written in the language of your vault with the original text kept.
- Saves web pages, PDFs, YouTube videos (transcript), files and pasted text. Pages created from external material always go through review.
- Imports your existing Claude Code sessions. Searches past sessions and resumes the right one ("where did we leave off?"). At the end of a session it writes a handoff for the next one on its own.
- Imports ChatGPT and claude.ai conversations from their export, including exports split into several files, and makes them searchable.
- To-dos that carry over from one session to the next. Daily and weekly summaries. A **Today** view in the app, which is also its home page.
- Git backup of the vault: automatic commit at the end of a session, an optional remote copy, earlier versions always recoverable.
- Maintenance happens only with your approval: it merges duplicates and leaves a redirect, adds missing links and proposes synthesis pages. A lesson you confirm again is strengthened, and relevant lessons are shown when they apply. Instincts become skills. The skill curator archives only on your command.
- Exploration:
  - people and entities ranked by current importance;
  - graph analysis: central pages, bridges, communities, unexpected connections;
  - validity over time, with "what was true on date X" search;
  - a quality score with history;
  - Canvas and Bases for Obsidian;
  - `llms.txt` export;
  - an optional scheduled daily maintenance job.
- Saves good answers as pages (`wiki/queries/`), linked to the pages they rest on. Lint flags them when those pages change.
- Protection from hidden instructions (prompt injection) in external content, in memory and in the context injected at session start.
- Saves screenshots from your phone: Remote Control or Telegram.
- Keeps a profile, goals, decisions, lessons and handoffs, injected at the start of every new session.
- Finds the skills already on your machine, creates new ones with a safety check and review, and installs them only on request.
- Learns "instincts" (when X → do Y) from your corrections in past sessions. None is used until you approve it.
- Free, local, multilingual semantic search. Export of the graph (offline HTML, GraphML, JSON) and of the whole vault as JSON.
- Knowledge exchange in [Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format), Google Cloud's standard. The export conforms. Import brings packages from other tools into the review queue and keeps unknown fields.

## Language

A vault has a language (`language.target` in `.mnemoloci/config.json`, or `mnemoloci config language <code>`). The engine writes the pages it generates, its summaries and its hints in that language: English by default, Italian when the target is `it`. `mnemoloci init --language it` creates an Italian vault. The app interface follows the browser language (English or Italian) and can be switched at the bottom of the side menu. Vaults created before the setting existed are treated as Italian.

## Where the vault is

Mnemoloci looks for the vault in this order: `--vault <folder>`, the `MNEMOLOCI_VAULT` variable, the path recorded in `~/.mnemoloci/vault.txt`, then `~/Mnemoloci`. The first `mnemoloci init` records the vault it creates, so hooks, the MCP server and the app all use the same one. `mnemoloci vault show` tells you which one is in use and why. `mnemoloci vault set <folder>` switches to another existing vault, and the **Vault** section of the app's Status view does the same. Restart the app and your Claude Code sessions afterwards.

## Installation

Requires Python 3.11 or later, git and Claude Code. On Linux use `python3` where the commands say `python`, inside a virtual environment (see the Linux section).

1. Install the engine. The extras add the MCP server (`mcp`), the app (`app`), local semantic search (`semantic`) and PDF and YouTube capture (`ingest`):

```powershell
python -m pip install "mnemoloci[mcp,app,semantic,ingest] @ git+https://github.com/siris9476/mnemoloci"
```

2. Create the vault and register the Python that runs this command. The hooks, the MCP server and the app use that Python from then on:

```powershell
python -P -m mnemoloci init
```

3. Add the plugin (skills, hooks and the MCP server):

```powershell
claude plugin marketplace add https://github.com/siris9476/mnemoloci
claude plugin install mnemoloci@mnemoloci --scope user
```

Then open a new Claude Code session. To get started:
- "re-read project X";
- "read my sessions";
- "save this screenshot";
- "save this article / PDF / video: <link>";
- "where did we leave off?" or "which session did we talk about … in?";
- "save this answer";
- "is there already a skill for …?".

Other agents can use the read-only MCP server with `python -P -m mnemoloci mcp`.

Optional steps:
- Local embeddings for semantic search: `python -P -m mnemoloci index embed` downloads a model of about 220 MB, once.
- Working from a clone instead of the GitHub install: `git clone https://github.com/siris9476/mnemoloci`, then `python -m pip install -e "<path to the clone>[mcp,app,semantic,ingest]"` and `claude plugin marketplace add <path to the clone>`.
- The app's interface is not part of the Python package, so the app needs a clone. Build the interface once (it needs Node.js 20 or later) with `cd app && npm ci && npm run build`, then start the app with `python -P -m mnemoloci app`. Without the build, it shows an "Interface not built" page.

### Choosing what to read (Sources page)

In the **Sources** page of the app (changes can only be made from the PC):
- add a project, or every subfolder of a folder, in **documents only** mode (README, notes, project files) or **documents and code**;
- open a source and browse its folders: for each one you see whether it is read, what it contributes and why a folder or file is skipped:
  - excluded by you;
  - dependencies and builds;
  - Python virtual environment;
  - `.gitignore`;
  - code outside documents-only mode;
  - binaries;
  - files that are too large;
  - possible secrets;
- tick a folder to exclude it, or include a folder that an automatic rule would skip (for example your own `vendor` folder). The most specific choice wins.

Claude uses the same settings:
- it reads them before compiling (`source show NAME`);
- it updates them when you ask in chat (`source exclude`, `source include`, `source scope`), and the change shows up in the page;
- it never excludes anything on its own initiative.

Source folders are never written to. From the terminal: `source add <folder> --name <name> --docs-only`, `source add-many <folder>`.

### Adding an Obsidian vault

From the app (Status view, **Sources** section, PC only): paste the vault path and press **Add**. The vault stays read-only. To turn the notes into wiki pages, copy the suggested request ("Compile the Mnemoloci source …") and paste it into a Claude Code session. **Check** counts new or changed notes. From the terminal: `python -P -m mnemoloci source add-obsidian <folder> --name <name>`.

### Diagnosis and repairs

```powershell
python -P -m mnemoloci doctor            # checks the installation (read only)
python -P -m mnemoloci doctor --fix      # repairs what can be repaired on its own, then checks again
python -P -m mnemoloci doctor --fix --only search-index handoffs
```

Repairs never delete anything. They are the same as the "Repair" button in the "System health" section of the app (Status view, PC only):
- registering the Python to use (`%USERPROFILE%\.mnemoloci\python.txt`);
- recovering interrupted writes;
- setting aside a stuck git lock;
- saving queued handoffs;
- rebuilding the index;
- only on explicit request, computing the embeddings.

Hooks, MCP and the app start the engine with the registered Python (through `plugin/bin/launch.py`), so another `python` placed first on the PATH, for example a venv, no longer switches memory off. `init` registers it the first time.

## Linux

Mnemoloci also runs on Linux, with the same vault, the same plugin and the same app. It needs Python 3.11 or later.

Install as described above, inside a virtual environment: create it with `python3 -m venv ~/.venvs/mnemoloci` and use `~/.venvs/mnemoloci/bin/python` wherever the commands say `python`. `init` registers that Python and creates the vault in `~/Mnemoloci`, unless you choose another place.

- Plugin: hooks and the MCP server go through `plugin/bin/mnemoloci-run`, an `sh` script that finds `python3` (or `python`) and then uses the Python registered in `~/.mnemoloci/python.txt`. The virtualenv does not need to be activated.
- Autostart (`remote autostart --enable`, `projects rc-autostart --enable`):
  - uses a systemd user service in `~/.config/systemd/user/`;
  - on a server with no active login, `loginctl enable-linger` keeps it running;
  - without systemd, on a desktop, it uses an entry in `~/.config/autostart/`.
- Daily job (`schedule enable`): a line in the user's crontab; without cron, a systemd timer.
- What is touched: only files and lines marked "created by Mnemoloci". If there is neither cron nor systemd, as in WSL without systemd, the command says so and tells you what to do.
- Desktop app: the Tauri app also builds on Linux (`cargo build --release` in `app/src-tauri`, it needs the WebKitGTK system libraries), but it has not been built or tried on Linux yet. Alternatively, `python3 -P -m mnemoloci app` opens the interface in the browser.

## Desktop app

Search, page reader, graph, review, goals, profile, skills and the screenshot inbox. Then there are **Saved** (save a link or paste a text, also from the phone), **Answers** (saved answers, with a warning when they need updating) and **Sessions** (search past Claude Code sessions and read them again). There is also a page **editor** with live preview: save with `Ctrl+S`; it refuses to overwrite a page changed elsewhere, for example in Obsidian, after you opened it.

```powershell
cd app; npm ci; npm run build                           # interface -> mnemoloci\web
python -P -m mnemoloci app                                  # Edge window in app mode (needs the build above)
cd src-tauri; cargo build --release                       # or a native Tauri window:
.\target\release\mnemoloci-desktop.exe                   #   starts the engine and stops it on exit
```

The interface is in English or Italian: choose at the bottom of the side menu (also on the phone pairing screen); the choice stays on that device, and the first time the browser language is used. Vault pages stay in the vault's configured language.

The app talks to the engine on `127.0.0.1`, with a random token on every start. A web page open in the browser cannot use it. `MNEMOLOCI_PYTHON` chooses the interpreter the Tauri window uses to start the engine.

## Browser extension

This feature is optional and still in progress: loading, pairing and saving have been tried in a test browser, but not the toolbar button or the right-click menu.

The `extension/` folder holds the Chrome and Edge extension, "Save to Mnemoloci" (English, with an Italian translation).
1. Load it from `edge://extensions` (or `chrome://extensions`): Developer mode → Load unpacked.
2. Start `python -P -m mnemoloci remote serve`.
3. In the extension options enter a pairing code. `python -P -m mnemoloci remote pair` prints one (it works once and expires in 10 minutes), and the app shows one too (Status).

After that you can save the page or the selected text with one click, also from the right-click menu. For pages behind a login it saves the text you see.

## From your phone

Two optional features. Phone access over Tailscale is still in progress.

### New projects (Remote Control)

Remote Control is a Claude Code feature that lets you drive a session from your phone. Mnemoloci adds one thing to it: "new project X" in such a session creates the folder with a `CLAUDE.md` and `git init`, and registers it in the second brain. Choose the projects folder first, in the app (Status → Projects folder) or with `python -P -m mnemoloci projects root <folder>`, and start `claude remote-control` in it. Moving the session into the new folder stays manual: type `/cd <folder>/X`. The folder can only be changed from the PC, and if autostart (`projects rc-autostart --enable`) is on, it is updated.

### Using the app (Tailscale)

This part is optional and still in progress. It needs Tailscale on the PC and on the phone, signed in with the same account, and HTTPS certificates enabled for your tailnet.

The app can then be used on an iPhone. The server stays on `127.0.0.1`, and only `tailscale serve` makes it reachable from your own devices, over HTTPS. Every phone is paired with a single-use code and can be revoked from the PC. The remote access code is covered by automated tests that use a made-up Tailscale host name, but it has not been tried on every setup.

```powershell
python -P -m mnemoloci remote setup        # step-by-step instructions and current state
```

Your PC must be on, and `python -P -m mnemoloci remote serve` must be running. To start it with Windows there is `remote autostart --enable`.

## Uninstalling

Your vault and your data are not touched.

```powershell
claude plugin uninstall mnemoloci@mnemoloci
claude plugin marketplace remove mnemoloci
python -m pip uninstall mnemoloci
```

## Development

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

The plugin (skills, hooks, MCP configuration) is in `plugin/`, the engine in `mnemoloci/`, the app in `app/`, the browser extension in `extension/`.

## Credits

Built with Claude (Anthropic) as a coding assistant, under direct human direction and review at every step.

## License

MIT. See [LICENSE](LICENSE); third-party attributions are in [NOTICE](NOTICE).
