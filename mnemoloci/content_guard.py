"""Prompt-injection guard for knowledge that an agent will later read: web clips,
pasted text, memory notes, profile facts, and the SessionStart context.

A threat scan for memory text, with the skill guard's injection rules reused. Skills get the stricter skill_guard;
this guard targets text that is data, not instructions: it never executes
anything, it reports, strips hidden characters and neutralises lines.
"""

import re
import unicodedata
from typing import Any

from .skill_guard import _DEFENSIVE, RULES as SKILL_RULES

# Unicode "tag" characters (U+E0000-E007F) spell invisible ASCII ("ASCII smuggling");
# bidi overrides and word joiners hide or reorder text. Removing them changes no newline.
_HIDDEN = re.compile("[\U000e0000-\U000e007f‪-‮⁠-⁤⁦-⁩​­᠎﻿]")

_R = lambda regex: re.compile(regex, re.IGNORECASE)  # noqa: E731
RULES: list[tuple[str, str, re.Pattern, str]] = [
    (r.category, r.severity, r.pattern, r.why) for r in SKILL_RULES if r.category in ("injection", "exfiltration", "execution")
] + [
    ("injection", "dangerous", _R(r"\b(ignora|dimentica)\s+(tutte\s+)?(le\s+)?(istruzioni|regole)\s+(precedenti|di\s+prima|sopra)"),
     "tries to override the agent's instructions (Italian)"),
    ("injection", "dangerous", _R(r"^\s*(system|assistant)\s*:\s|<\|?(system|im_start|im_end)\|?>|\[/?(system|inst)\]"),
     "fake chat-role or system-prompt markup"),
    ("injection", "dangerous", _R(r"\b(ai|assistant|claude|chatgpt|llm|agent|model|assistente|modello)\b[\s,:]{1,3}(please\s+|per\s+favore\s+)?"
                                    r"(run|execute|send|upload|delete|install|download|esegui|invia|cancella|installa|scarica)\b"),
     "addresses the AI reading this text with a command"),
    ("injection", "caution", _R(r"\b(new|updated|real)\s+instructions\s*:|\bnuove\s+istruzioni\s*:|\bact\s+as\s+(an?\s+)?(unrestricted|jailbroken|dan)\b"),
     "announces replacement instructions"),
    ("injection", "dangerous", _R(r"\b(disregard|ignore|forget)\s+(everything|all|anything)\s+(said\s+)?(above|before|earlier|previously|so\s+far)"),
     "tries to override the agent's instructions"),
    ("injection", "caution", _R(r"\bfrom\s+now\s+on,?\s+you\s+(must|will|are|should)\b|\bd['’]ora\s+in\s+poi\s+(devi|sei|dovrai)\b"),
     "tries to change the agent's rules"),
    ("exfiltration", "caution", _R(r"!\[[^\]]*\]\(https?://[^)\s]*\?[^)\s]*=[^)\s]*\{"),
     "image link that would send templated data to a remote server"),
]

BLOCKED = "[BLOCCATO da Mnemoloci: possibile istruzione nascosta per l'agente]"


def strip_hidden(text: str) -> tuple[str, int]:
    """Remove invisible tag/bidi/zero-width characters. Newlines are untouched, so
    line-number citations stay valid."""
    return _HIDDEN.subn("", text)


# Look-alike letters used to dodge keyword rules (Cyrillic/Greek that render like Latin).
_CONFUSABLE = str.maketrans("аеорсухіјѕԁАВЕКМНОРСТХІЈЅοαειкνρτυΑΒΕΙΚΜΝΟΡΤΧΥ",
                            "aeopcyxijsdABEKMHOPCTXIJSoaeiknptuABEIKMNOPTXY")
# Separators that other tools treat as line breaks: here they never split a line (see neutralize).
_SEPARATORS = re.compile("[  \u0085\x0b\x0c]")


def _normalise(line: str) -> str:
    """The form rules are matched against: NFKC (fullwidth -> ASCII), no format characters
    (soft hyphen, BOM, joiners), look-alike letters mapped to Latin."""
    folded = unicodedata.normalize("NFKC", _SEPARATORS.sub(" ", line))
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return folded.translate(_CONFUSABLE)


def scan(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    hidden = len(_HIDDEN.findall(text))
    if hidden:
        findings.append({"category": "obfuscation", "severity": "dangerous", "line": None,
                         "why": f"{hidden} invisible/bidi character(s) that can hide instructions", "excerpt": ""})
    # split exactly like neutralize() does, so a finding's line number is the line it replaces
    lines = [_normalise(line) for line in text.split("\n")]
    for number, line in enumerate(lines, 1):
        for category, severity, pattern, why in RULES:
            m = pattern.search(line)
            if not m:
                continue
            if category == "injection" and severity == "dangerous" and _DEFENSIVE.search(line[: m.start()]):
                severity, why = "caution", why + " (quoted or negated: likely a discussion of it)"
            findings.append({"category": category, "severity": severity, "line": number, "why": why,
                             "excerpt": line.strip()[:160]})
    return findings


def verdict(findings: list[dict[str, Any]]) -> str:
    severities = {f["severity"] for f in findings}
    return "dangerous" if "dangerous" in severities else "caution" if findings else "safe"


def summary(text: str) -> dict[str, Any]:
    findings = scan(text)
    return {"verdict": verdict(findings), "findings": findings[:20], "count": len(findings)}


def neutralize(text: str) -> tuple[str, int]:
    """For text injected into an agent's context: hidden characters removed and every
    line with a dangerous finding replaced by a visible marker."""
    text, _ = strip_hidden(text)
    bad = {f["line"] for f in scan(text) if f["severity"] == "dangerous" and f["line"]}
    if not bad:
        return text, 0
    lines = text.split("\n")
    for number in bad:
        lines[number - 1] = BLOCKED
    return "\n".join(lines), len(bad)


def require_safe(text: str, what: str) -> None:
    """Refuse to store text that would be re-injected into every future session."""
    from .core.errors import ValidationError

    findings = [f for f in scan(text) if f["severity"] == "dangerous"]
    if findings:
        raise ValidationError("INJECTION_SUSPECT",
                              f"{what} looks like instructions aimed at an AI agent; not stored. "
                              "Rephrase it as a plain statement of fact.", {"findings": findings[:5]})
