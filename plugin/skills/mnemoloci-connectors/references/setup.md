# Per-provider registration

What each provider needs before `mnemoloci connectors <provider> setup/token` can be run. Console
UIs move over time; if a label below doesn't match what the user sees, aim for the underlying
setting (app type, redirect URI, scope) instead of the exact click path.

## Google Drive (oauth)

1. Google Cloud Console → create/select a project → **APIs & Services → Library** → enable the
   **Google Drive API**.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
3. Application type: **Desktop app**. This type allows a loopback redirect
   (`http://127.0.0.1:<any port>`) with no public server needed — matches how `connectors connect`
   opens a temporary local listener.
4. Copy the generated **Client ID** and **Client secret** → `mnemoloci connectors google-drive
   setup --client-id ... --client-secret ...`.
5. Scope used: `drive.readonly` (read-only, matches the sync's one-directional nature).
6. `set-folder` takes the folder's ID — the long string in its Drive URL
   (`drive.google.com/drive/folders/<this part>`).

## Gmail (oauth)

Same Google Cloud project and OAuth client as Drive works for both — same console, same "Desktop
app" client type. Enable the **Gmail API** in the Library step instead of/alongside Drive's.
Scope: `gmail.readonly`. `set-folder` takes a Gmail search query, e.g. `label:appunti` or
`from:qualcuno@example.com`, not a literal folder.

## OneDrive (oauth)

1. **Azure Portal → Microsoft Entra ID → App registrations → New registration.**
2. Supported account types: personal + work/school accounts (so it works for either kind of
   Microsoft account) — the "common" endpoint.
3. **Redirect URI**: platform **Mobile and desktop applications**, URI literal `http://localhost`
   — Azure's public-client registration only accepts that exact host, no port. The actual port
   Mnemoloci's loopback listener picks is ignored at match time (RFC 8252 native-app behavior), so
   this fixed value works regardless of which port `connect` ends up using.
4. **API permissions → Microsoft Graph → Delegated → Files.Read** (plus `offline_access`, usually
   included by default for refresh tokens).
5. **Certificates & secrets → New client secret** → copy the value immediately, Azure only shows
   it once.
6. `mnemoloci connectors onedrive setup --client-id ... --client-secret ...`. PKCE is used
   automatically (no extra client-side setup).
7. `set-folder` takes the OneDrive folder's item ID.

## GitHub (token)

No OAuth app. Just a **fine-grained personal access token**:

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token.**
2. Repository access: only the repo(s) meant to sync — least privilege.
3. Permissions: **Contents: Read-only** is enough (this only reads files, never writes).
4. `mnemoloci connectors github token <the-token-value>`.
5. `set-folder` takes `owner/repo` (whole repo) or `owner/repo/path` (a subfolder).

## Notion (token)

No OAuth app either. An **Internal Integration token**:

1. notion.so/my-integrations → **New integration**, associated with the user's own workspace.
2. Copy the **Internal Integration Secret**.
3. In Notion itself, open the database to sync and use **Connect to** → add the integration — an
   integration only sees pages/databases explicitly shared with it.
4. `mnemoloci connectors notion token <the-token-value>`.
5. `set-folder` takes the database's ID (from its URL, the 32-character id before any `?v=`).
