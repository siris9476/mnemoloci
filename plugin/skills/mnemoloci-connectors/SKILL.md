---
name: mnemoloci-connectors
description: Set up, check or fix a periodic sync connector to an external service (Google Drive, OneDrive, Gmail, GitHub, Notion) so its content flows into the vault's review queue automatically. Use when the user says "connect Google Drive/OneDrive/Gmail/GitHub/Notion", "I want to sync a folder/repo/database", "why is the connector not syncing", "how do I register the OAuth app for X", or, in Italian, "collega Google Drive/OneDrive/Gmail/GitHub/Notion", "voglio sincronizzare una cartella/repo/database", "perché il connettore non sincronizza", "come registro l'app OAuth per X".
---

# Set up a connector

A connector downloads content from an external service on a schedule (the nightly job) and drops
it into the review queue exactly like any other source — never publishes directly, never calls an
LLM. Setting one up has two parts: something only the user can do (register the app on the
provider's own developer console), and something you do (run the Mnemoloci commands once you have
what that registration produced).

## 1. Figure out which of the two auth shapes applies

```
mnemoloci connectors list
```

Shows every provider, whether it's implemented, and its `auth_kind`:

- **`oauth`** (Google Drive, OneDrive, Gmail) — the user registers an app on the provider's
  console and gets a `client_id`/`client_secret`; then a one-time browser consent screen.
- **`token`** (GitHub, Notion) — no OAuth app at all, just a personal token the user pastes in
  once. Simpler, do this path when the provider allows it.

Read `references/setup.md` for the exact registration steps and settings (scopes, redirect URI,
whether PKCE is required) per provider — these come from provider documentation and were verified
against it while this connector framework was built, but a console's UI can move; if what the user
sees doesn't match, describe the underlying goal (an app of this type, this redirect URI, this
scope) rather than guessing at button labels.

## 2. Wire it up

OAuth providers:
```
mnemoloci connectors <provider> setup --client-id <id> --client-secret <secret>
mnemoloci connectors <provider> connect          # opens the browser for one-time consent
mnemoloci connectors <provider> set-folder <folder_id> [--name "<label>"]
```

Token providers:
```
mnemoloci connectors <provider> token <value>
mnemoloci connectors <provider> set-folder <folder_id> [--name "<label>"]
```

`<provider>` is `google-drive`, `onedrive`, `gmail`, `github` or `notion`. What `set-folder`
expects varies by provider — `mnemoloci connectors list` and `references/setup.md` say which: a
Drive/OneDrive folder ID from its URL, a Gmail search query (e.g. `label:appunti`), a GitHub
`owner/repo` or `owner/repo/path`, or a Notion database ID.

Never ask the user to paste a client secret or token into chat if they'd rather run the command
themselves — it's their credential, and it lands in `.mnemoloci/connectors.json` (gitignored,
never logged).

## 3. Verify it actually works

```
mnemoloci connectors <provider> sync
```

Runs a sync immediately instead of waiting for the nightly job. Check the result: new items should
show up in the review queue (`mnemoloci review list` / the `mnemoloci-review` skill) exactly like
any manually captured note. If it errors, the message usually says which half failed — an auth
problem (token expired, wrong scope) versus a folder/query problem (wrong ID, no read access).

Once verified, syncing happens on its own in the nightly job — nothing further to run by hand
unless the user wants an out-of-band sync.

## 4. Turning it off

```
mnemoloci connectors <provider> disable
```

Revokes the stored credentials. It never touches content already imported — those pages stay in
the vault, only new syncing stops.

## When to skip this

- The user just wants to paste text/a link in manually — that's `mnemoloci-capture`, not a
  connector.
- Don't attempt a provider's OAuth registration steps yourself; that page requires the user's own
  login on Google Cloud Console / Azure Portal / GitHub / Notion. Your job is the Mnemoloci side
  only.
