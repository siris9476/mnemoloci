# Security policy

Mnemoloci handles personal notes, tokens and a local web server, so security reports are welcome.

## Reporting a vulnerability

Please do not open a public issue. Report it privately with GitHub's "Report a vulnerability" button on the
repository's **Security** tab.

Include what you found, how to reproduce it and the version (`mnemoloci --version`). Reports are answered as
time allows. Fixes are published with a note in the changelog, crediting you if you want.

## What counts

Things that cross a boundary Mnemoloci promises to keep:

- the local server or the phone access (Tailscale) being reachable or usable by someone who was not paired;
- a web page, a document or a clipped text running commands or changing memory through prompt injection
  despite the content guard;
- secrets (tokens, keys) leaving the machine or being written to the vault unredacted;
- writes outside the vault, or a failed write leaving the vault corrupted;
- a malicious skill or import bypassing the skill guard or the review queue.

Not in scope: attacks that need full control of your account on the machine, or of the vault folder itself.

## Supported versions

Only the latest release receives fixes.
