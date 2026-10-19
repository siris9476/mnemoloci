---
name: mnemoloci-inbox
description: Save screenshots and images into the Mnemoloci second brain — images the user attaches in the chat (also from the phone via Claude Code Remote Control), files saved by a Telegram channel, or any image path — then transcribe and compile them. Use when the user sends a screenshot/photo and says "save it", "remember it", "put it in the second brain" (in Italian "salvalo", "ricordalo", "mettilo nel second brain"), or asks to process the inbox.
---

# Inbox: screenshots and images

## 1. Get the image into the vault

**Image attached in this chat.** This includes one sent from the phone through Remote Control. The image is stored in the session transcript, and the engine extracts it:

```
mnemoloci inbox from-session [--last 1] [--note "cosa è"]
```

Run it from the session's working directory (default `--cwd` is the current directory). `--last N` takes the newest N attached images; use `--last 1` for "questo screenshot".

**Image files**, e.g. saved by a Telegram channel, a synced phone folder, or given as a path:

```
mnemoloci inbox add <file> [<file>…] [--note "…"]
```

Images are stored under `raw/inbox/<date>/`. Each gets a sidecar note that embeds it in Obsidian. The same image is never stored twice, because the engine compares hashes.

## 2. Transcribe

The engine never reads images; you do.

1. `mnemoloci inbox pending` lists images waiting, with their `image` path.
2. Look at each image with the Read tool.
3. Write a transcription. Save it to a file, then run `mnemoloci inbox transcribe <id> --text-file <file>`. Include:
   - all readable text, verbatim, keeping structure (lists, tables as Markdown);
   - a short description of what the screenshot shows: app, context, what matters;
   - uncertain readings marked `[?]`.

Don't invent text you can't read. Secrets visible in a screenshot are redacted by the engine; mention that one was there.

## 3. Compile

```
mnemoloci compile plan --source inbox
```

Continue with the normal `mnemoloci-ingest` loop. Images from outside are external sources, so their pages go to the review queue and the user approves them with `mnemoloci-review`.

## Phone setup

Tell the user these options when they ask how to send screenshots from the iPhone.

**Remote Control** needs no extra setup:
1. Start a Claude Code session on the PC.
2. Connect from the Claude app on the phone.
3. Attach the screenshot and write "salvalo nel second brain".
4. Run `inbox from-session --last 1`.

**Telegram** uses Claude Code Channels:
1. Install Bun (native on Windows since 1.1).
2. Create a bot with @BotFather and install the Telegram channel plugin from the official marketplace. Follow the current Claude Code Channels docs for the exact commands.
3. Start Claude Code with that channel enabled.

Photos sent to the bot arrive in the session. If they arrive as images in the chat, use `from-session`. If the channel saves them as files, use `inbox add <path>` with the path it reports.
