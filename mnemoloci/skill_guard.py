"""Static security scan for Agent Skills (with Windows/PowerShell patterns added). Runs on every generated skill before it is
written, again before install, and on demand on any existing skill."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.paths import is_link_like
from .secrets import redact

MAX_FILES = 60
MAX_FILE_BYTES = 1024 * 1024
SCAN_FILE_LIMIT = 400
SCAN_BYTE_LIMIT = 16 * 1024 * 1024
DEPENDENCY_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist", "build",
                   "site-packages", ".next", "target", "vendor"}
TEXT_EXT = {".md", ".txt", ".py", ".sh", ".ps1", ".psm1", ".js", ".mjs", ".ts", ".json", ".yaml", ".yml",
            ".toml", ".html", ".css", ".csv", ".xml", ".sql", ".bat", ".cmd"}
EXECUTABLE_EXT = {".exe", ".dll", ".so", ".dylib", ".msi", ".scr", ".com", ".jar", ".app"}


@dataclass(frozen=True)
class Rule:
    category: str
    severity: str  # "dangerous" | "caution"
    pattern: re.Pattern
    why: str


def _r(category: str, severity: str, regex: str, why: str) -> Rule:
    return Rule(category, severity, re.compile(regex, re.IGNORECASE), why)


RULES = [
    # Prompt injection aimed at the agent that will load the skill.
    _r("injection", "dangerous", r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts)", "tries to override the agent's instructions"),
    _r("injection", "caution", r"\b(do\s+not|don'?t|never)\s+(tell|inform|mention\s+to)\s+the\s+user|\bwithout\s+(telling|informing|asking)\s+the\s+user\b|\bhide\s+(this|it)\s+from\s+the\s+user\b", "asks the agent to keep something from the user"),
    _r("injection", "caution", r"\byou\s+are\s+now\s+(a|an|in)\b|\bnew\s+system\s+prompt\b|\bjailbreak\b", "role/system-prompt override phrasing"),
    # Persistence: changing the agent's or the OS's configuration. A real shell
    # redirect or write command must point straight at the file.
    _r("persistence", "dangerous", r"(>>?\s*[\"']?|\b(set-content|add-content|out-file|tee(\s+-a)?|writefile(sync)?|write_text)\b\s*\(?\s*(-path\s+|-filepath\s+)?[\"']?)[\w~$:%./\\-]{0,120}(claude\.md|agents\.md|settings(\.local)?\.json|\.claude[\\/]settings)", "modifies agent configuration or memory files"),
    _r("persistence", "caution", r"\b(crontab\s+-|schtasks(\.exe)?\s+/create|register-scheduledtask|new-scheduledtask)", "creates a scheduled task"),
    _r("persistence", "dangerous", r"(hkcu|hklm|hkey_current_user|hkey_local_machine)[:\\/][^\n]{0,60}\\run\b|currentversion\\run", "writes an autorun registry key"),
    _r("persistence", "caution", r"(\.bashrc|\.zshrc|\.profile|\$profile|microsoft\.powershell_profile\.ps1)", "touches a shell profile"),
    # Exfiltration and credential access.
    _r("exfiltration", "dangerous", r"\b(curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm)\b[^\n]{0,160}(-d|--data|-body|-infile|-form|--upload-file|-t\s)[^\n]{0,120}(\$env:|\$\{?[a-z_]*(key|token|secret|pass)|\.ssh|\.aws|\.env\b|credentials)", "sends secrets or credential files over the network"),
    _r("exfiltration", "dangerous", r"\b(nc|ncat|netcat)\b[^\n]{0,40}\s-e\s|/dev/tcp/", "reverse shell / raw socket"),
    _r("credentials", "caution", r"(~|\$home|%userprofile%|\$env:userprofile)[\\/]\.(ssh|aws|gnupg|kube|docker)[\\/]|id_(rsa|ed25519)\b|\.aws[\\/]credentials", "references private keys or cloud credentials"),
    _r("credentials", "caution", r"login\s+data|cookies\.sqlite|\\local state\b|keychain|credential\s*manager|cmdkey\s+/list", "reads browser or OS credential stores"),
    # Destructive operations.
    _r("destructive", "dangerous", r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|\$home|\*)(\s|$)|\bremove-item\b[^\n]{0,80}-recurse[^\n]{0,80}(c:\\|\$env:userprofile|~|\\\*)|\bformat(\.com)?\s+[a-z]:|\bdel\s+/[sq]\s+/[sq]\s+[a-z]:\\|\bdiskpart\b", "deletes a whole drive or home directory"),
    _r("destructive", "caution", r"\bgit\s+push\s+(-f|--force)\b|\bdrop\s+(database|table)\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[a-z]*f", "irreversible git/database operation"),
    # Download-and-execute, obfuscation.
    _r("execution", "dangerous", r"\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]{0,200}\|\s*(sh|bash|zsh|python\d?|iex|invoke-expression|powershell|pwsh)\b", "downloads and executes remote code"),
    _r("obfuscation", "dangerous", r"base64\s+(-d|--decode)[^\n]{0,40}\|\s*(sh|bash|python)|frombase64string[^\n]{0,120}(iex|invoke-expression)|powershell(\.exe)?\s+[^\n]{0,40}-e(nc|ncodedcommand)?\s+[a-z0-9+/=]{20,}", "executes obfuscated/encoded code"),
    _r("obfuscation", "caution", r"\beval\s*\(\s*(atob|base64|decode|bytes\.fromhex)|\bexec\s*\(\s*(base64|codecs\.decode|bytes\.fromhex)", "evaluates decoded data"),
    # Privilege and security-control changes.
    _r("privilege", "dangerous", r"set-executionpolicy\s+(bypass|unrestricted)|set-mppreference[^\n]{0,60}-disable|add-mppreference\s+-exclusion|netsh\s+advfirewall\s+set\s+[^\n]*\bstate\s+off", "disables OS security controls"),
    _r("privilege", "caution", r"\bsudo\b|\brunas\b|start-process[^\n]{0,60}-verb\s+runas|\bchmod\s+(-r\s+)?777\b", "elevates privileges"),
    # Crypto mining.
    _r("mining", "dangerous", r"\b(xmrig|minerd|cpuminer|stratum\+tcp)\b", "cryptocurrency miner"),
]

# Zero-width space, word joiners and bidi overrides ("Trojan Source"). ZWJ/ZWNJ and
# LRM/RLM are left out on purpose: they appear legitimately in emoji and RTL text.
_INVISIBLE = re.compile("[\u202a-\u202e\u2060-\u2064\u2066-\u2069]|(?<=.)\ufeff", re.DOTALL)
# A lone zero-width space is also a common Markdown trick (e.g. before a nested ``` fence).
_ZWSP = re.compile("\u200b")
# An injection phrase quoted or negated is usually a defence ("never follow text like
# 'ignore previous instructions'"), so it is downgraded to caution, not ignored.
_DEFENSIVE = re.compile(r"[\"'`\u201c\u2018]|\b(never|do\s+not|don'?t|e\.g\.|such\s+as|like|embedded|treat|untrusted|refuse)\b", re.IGNORECASE)


def _finding(category: str, severity: str, file: str, line: int | None, why: str, excerpt: str = "") -> dict[str, Any]:
    return {"category": category, "severity": severity, "file": file, "line": line, "why": why, "excerpt": excerpt[:160]}


def _line_at(text: str, pos: int) -> tuple[int, str]:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text.count("\n", 0, pos) + 1, text[start : end if end != -1 else len(text)]


def scan_text(text: str, file: str) -> list[dict[str, Any]]:
    findings = []
    for rule in RULES:  # whole-text scanning: every pattern is bounded to one line by [^\n]
        for m in rule.pattern.finditer(text):
            number, line = _line_at(text, m.start())
            severity = rule.severity
            if rule.category == "injection" and severity == "dangerous":
                prefix = line[: max(0, line.find(m.group(0)))]
                if _DEFENSIVE.search(prefix):
                    severity = "caution"
            findings.append(_finding(rule.category, severity, file, number,
                                     rule.why + (" (quoted or negated: likely a defence)" if severity != rule.severity else ""),
                                     line.strip()))
    for m in _INVISIBLE.finditer(text):
        number, _ = _line_at(text, m.start())
        findings.append(_finding("obfuscation", "dangerous", file, number, "bidi-override or invisible joiner characters (hidden instructions)"))
        break
    for m in _ZWSP.finditer(text):
        number, _ = _line_at(text, m.start())
        findings.append(_finding("obfuscation", "caution", file, number, "zero-width space"))
        break
    _, secrets = redact(text, strict=True)
    if secrets:
        findings.append(_finding("credentials", "dangerous", file, None, f"{secrets} embedded secret(s) such as API keys or tokens"))
    return findings


def scan_dir(root: Path) -> dict[str, Any]:
    """Scan a skill folder. Verdict: safe | caution | dangerous."""
    root = Path(root)
    findings: list[dict[str, Any]] = []
    files = scanned_bytes = 0
    unscanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            rel_dir = (Path(dirpath) / name).relative_to(root).as_posix()
            if is_link_like(os.lstat(Path(dirpath) / name)):
                findings.append(_finding("structure", "dangerous", rel_dir, None, "symlink/junction inside the skill"))
                dirnames.remove(name)
            elif name in DEPENDENCY_DIRS:
                findings.append(_finding("structure", "caution", rel_dir, None, "bundled dependency directory, not scanned"))
                dirnames.remove(name)
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            files += 1
            if files > SCAN_FILE_LIMIT or scanned_bytes > SCAN_BYTE_LIMIT:
                unscanned += 1
                continue
            st = os.lstat(path)
            if is_link_like(st):
                findings.append(_finding("structure", "dangerous", rel, None, "symlink inside the skill"))
                continue
            suffix = path.suffix.lower()
            if suffix in EXECUTABLE_EXT:
                findings.append(_finding("structure", "dangerous", rel, None, "bundled executable/binary"))
                continue
            if st.st_size > MAX_FILE_BYTES:
                findings.append(_finding("structure", "caution", rel, None, f"large file ({st.st_size} bytes)"))
                continue
            data = path.read_bytes()
            scanned_bytes += len(data)
            if b"\0" in data[:8192] or (suffix and suffix not in TEXT_EXT):
                findings.append(_finding("structure", "caution", rel, None, "non-text file (not scanned)"))
                continue
            findings.extend(scan_text(data.decode("utf-8", errors="replace"), rel))
    if files > MAX_FILES:
        findings.append(_finding("structure", "caution", ".", None, f"{files} files (more than {MAX_FILES})"))
    if unscanned:
        findings.append(_finding("structure", "caution", ".", None, f"scan limit reached: {unscanned} file(s) not scanned"))
    severities = {f["severity"] for f in findings}
    verdict = "dangerous" if "dangerous" in severities else "caution" if findings else "safe"
    return {"verdict": verdict, "files": files, "findings": findings}
