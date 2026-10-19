# Contributing

Thanks for looking. Mnemoloci is a small project; issues and pull requests are welcome.

## Set up

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,app]"     # Windows: .venv\Scripts\python
.venv/bin/python -m pytest -q                        # about four minutes
cd app && npm ci && npm run build                    # the web/desktop interface
```

The engine is in `mnemoloci/`, the Claude Code plugin in `plugin/`, the app in `app/`, the browser extension
in `extension/`, the tests in `tests/`. Run Python as `python -P -m mnemoloci` so a folder named `mnemoloci`
in the current directory is not picked up instead of the package.

## Rules that matter

- Writes go through the transaction engine (`mnemoloci/core/`): lock, plan, approval hash, journal. Do not
  write vault files directly.
- Nothing is deleted automatically. Retire, supersede or hold for review instead.
- Language. Code, comments, docs and the default output are English. Text the engine writes into a vault
  follows the vault's language (`language.target`): use `i18n.t(vault, italian, english)` and keep both. The app's
  interface text is English in the source, with the Italian translation in `app/src/i18n-it.ts`; a test checks
  that every sentence of the engine's catalogues has an entry.
- Tests. Add a test for every change in behaviour. A new CLI command must belong to a feature in
  `mnemoloci/features.py` (or be listed as internal).
- Line endings. Keep a file's existing line endings when you edit it.

## Pull requests

Keep them focused, say what changed and why, and make sure `pytest` and `npm run build` pass.

By contributing you agree that your work is released under the MIT License.
