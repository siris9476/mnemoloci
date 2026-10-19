import re
from pathlib import Path
from typing import Any

# Redaction is in-line only (never adds/removes newlines) so line-number
# citations into the original file stay valid.
_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n]*")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("connection-string", re.compile(r"(?i)\b(?:password|pwd)=[^;\s\"']{4,}")),
    ("azure-key", re.compile(r"(?i)\b(?:Shared)?AccountKey=[^;\s\"']{8,}|\bSharedAccessKey=[^;\s\"']{8,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36,}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("stripe-webhook", re.compile(r"\bwhsec_[A-Za-z0-9]{24,}")),
    ("telegram-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")),
    ("webhook-url", re.compile(r"https://(?:hooks\.slack\.com/services|(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks)/[A-Za-z0-9/_-]+")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)((?:api[_-]?key|secret|token|passw(?:or)?d|client[_-]?secret)[\"']?\s*[:=]\s*[\"'])([^\"'\n]{8,})([\"'])"
        ),
    ),
]

SECRET_FILE = re.compile(
    r"(?i)(^|/)(\.env(\..*)?|.*\.(pem|key|pfx|p12|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$"
)


# The value is replaced, the name kept (so the text still says what was there).
_KEEP_NAME = [
    # KEY=value with no quotes, as in .env files: the name must be written like an env variable
    ("env-secret", re.compile(r"\b([A-Z][A-Z0-9_]{0,40}?(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)[A-Z0-9_]{0,40}[ \t]*[:=][ \t]*)"
                              r"(?![$%{\[(]|os\.|process\.|getenv|env\()([^\s\"'(){}\[\],;]{8,})")),
    # YAML-style `password: hunter2` alone on its line
    ("yaml-secret", re.compile(r"(?im)^([ \t]*(?:password|passwd|pwd|secret|api_?key|token|client_secret)[ \t]*:[ \t]*)"
                               r"(?![$%{\[(|>])([^\s\"'#]{4,})[ \t]*$")),
    ("url-password", re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]{3,})(?=@)")),
    # a plain English sentence can start with "Basic"/"Bearer" too (e.g. "Basic motion/sound/person
    # alerts are free..."), and a run of lowercase words joined by slashes matches the token charset
    # just as well as a real one -- require a digit or a base64-only character (+, =, ~) to tell an
    # actual token apart from prose, since those essentially never appear in an English word run.
    ("auth-header", re.compile(r"(?i)(\b(?:bearer|basic)\s+)((?=[A-Za-z0-9._~+/=-]*[0-9+=~])[A-Za-z0-9._~+/=-]{16,})")),
    ("mysql-password", re.compile(r"(\b(?:mysql|mysqldump|mariadb)\b[^\n]{0,300}?\s-p)([^\s-]\S{2,})")),
]

_KEY_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_KEY_BODY = re.compile(r"^[\sA-Za-z0-9+/=\\\"',:-]*$")  # base64 lines, PEM headers, JSON-escaped pieces
_PUTTY_LINES = re.compile(r"^Private-Lines:\s*(\d{1,4})\s*$")


def _blank_key_blocks(text: str) -> tuple[str, int]:
    """Private keys, line by line (linear time, whatever the input): the header line from the
    marker on and every line up to the END marker are blanked, newlines kept. A key pasted without
    its END line loses the base64-looking lines that follow. PuTTY .ppk private lines too."""
    lines = text.split("\n")
    count, i = 0, 0
    while i < len(lines):
        line = lines[i]
        header = _KEY_HEADER.search(line)
        putty = _PUTTY_LINES.match(line.strip())
        if putty:
            count += 1
            for j in range(i + 1, min(len(lines), i + 1 + int(putty.group(1)))):
                lines[j] = ""
            lines[i] = "Private-Lines: [REDACTED:private-key]"
            i += 1 + int(putty.group(1))
            continue
        if not header:
            i += 1
            continue
        count += 1
        if "-----END" in line[header.end():]:  # a whole key on one line (JSON-escaped)
            lines[i] = line[:header.start()] + "[REDACTED:private-key]"
            i += 1
            continue
        lines[i] = line[:header.start()] + "[REDACTED:private-key]"
        j = i + 1
        end = next((k for k in range(j, min(len(lines), j + 400)) if "-----END" in lines[k]), None)
        stop = end + 1 if end is not None else next(
            (k for k in range(j, len(lines)) if not lines[k].strip() or not _KEY_BODY.match(lines[k])), len(lines))
        for k in range(j, stop):
            lines[k] = ""
        i = stop
    return "\n".join(lines), count


GENERIC = {"jwt", "connection-string", "assigned-secret", "env-secret", "yaml-secret", "auth-header", "mysql-password"}


def redact(text: str, *, strict: bool = False) -> tuple[str, int]:
    """strict=True counts only unmistakable key formats (used to judge skills, where
    documentation examples like `token = "your-token-here"` are normal)."""
    text, count = _blank_key_blocks(text)
    for name, pattern in _KEEP_NAME:
        if strict and name in GENERIC:
            continue
        text, n = pattern.subn(lambda m, name=name: m.group(0) if m.group(2).startswith("[REDACTED")
                               else f"{m.group(1)}[REDACTED:{name}]", text)
        count += n
    for name, pattern in _PATTERNS:
        if strict and name in GENERIC:
            continue
        if name == "assigned-secret":
            hits = 0

            def assigned(m: re.Match) -> str:
                nonlocal hits
                if m.group(2).startswith("[REDACTED"):
                    return m.group(0)
                hits += 1
                return f"{m.group(1)}[REDACTED:{name}]{m.group(3)}"

            text = pattern.sub(assigned, text)
            count += hits
        else:
            text, n = pattern.subn(f"[REDACTED:{name}]", text)
            count += n
    return text, count


def find(text: str) -> list[dict[str, Any]]:
    """Same patterns as redact(), but reports where a hit is (kind, line) instead of replacing it
    -- for an audit that has to point at a leak, not just strip it. The matched text itself is
    never included in a finding: the whole point is not to echo a secret back into a report."""
    findings: list[dict[str, Any]] = []
    for i, line in enumerate(text.split("\n"), 1):
        if _KEY_HEADER.search(line) or _PUTTY_LINES.match(line.strip()):
            findings.append({"kind": "private-key", "line": i})
    for name, pattern in _KEEP_NAME:
        for m in pattern.finditer(text):
            findings.append({"kind": name, "line": text.count("\n", 0, m.start()) + 1})
    for name, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            findings.append({"kind": name, "line": text.count("\n", 0, m.start()) + 1})
    return sorted(findings, key=lambda f: f["line"])


def audit_vault(vault: Path, roots: tuple[str, ...] = ("wiki", "memory", "_meta")) -> dict[str, Any]:
    """Report-only scan of the vault's active content (not `raw/`, which already goes through
    redact() once at capture time regardless of `mine`) for credential-shaped strings that slipped
    past that: a manual edit through the app's editor bypasses the capture pipeline entirely, and a
    pattern added to this file after a page was written was never applied to it retroactively --
    same staleness class as every other "saved once, never migrated" bug found this session,
    just for redaction patterns instead of config fields. Changes nothing; the user acts on what
    it finds."""
    from .pages import iter_pages
    from .vault import require_vault

    require_vault(vault)
    scanned, findings = 0, []
    for p in iter_pages(vault, roots):
        if p.error:
            continue
        scanned += 1
        text = (Path(vault) / p.path).read_text("utf-8", errors="replace")
        hits = find(text)
        if hits:
            findings.append({"path": p.path, "findings": hits})
    return {"scanned": scanned, "pages_with_findings": len(findings), "findings": findings}
