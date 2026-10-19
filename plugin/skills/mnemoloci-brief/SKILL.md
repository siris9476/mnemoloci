---
name: mnemoloci-brief
description: Write a briefing, a plain-language explanation or a progressive lesson on a topic from the user's second brain, with every claim cited and audited. Use when the user says "give me a briefing on X", "explain X in plain words", "teach me X step by step", "summarise what I know about X for a meeting", or, in Italian, "fammi un briefing su X", "spiegami X in parole semplici", "insegnami X passo passo", "riassumi quello che so su X per una riunione".
---

# Briefings, explanations, lessons

## 1. Gather the evidence

```
mnemoloci context <argomento> --budget 6000
```

This is the vault's best pages on the topic. Use only this material. If the pack is thin, say so, and offer to capture or compile more sources first.

## 2. Write, in the mode the user wants

| Mode | Shape |
|---|---|
| `briefing` | For a meeting or decision: the situation in 3 lines, key facts, open questions, risks. At most one page. |
| `spiegazione` | For someone new to the topic: plain words, one idea per paragraph, a concrete example. No jargon without defining it. |
| `lezione` | Progressive: basics first, then the parts that build on them, each step linked to its page, with a short "verifica" question at the end of each step. |

Rules:
- Every factual paragraph links its page (`[[slug]]`) and keeps the `^[fonte#Lx-Ly]` citations from the pack.
- Anything that is your own inference or opinion is marked `(opinione)` or phrased as such.
- Say where the vault is silent. Don't fill gaps with general knowledge without labelling it.

## 3. Audit, then save if the user wants to keep it

```
mnemoloci brief check <file>
mnemoloci brief save <file> --title "…" --topic "…" --mode briefing|spiegazione|lezione
```

- `check` reports broken links, citations to lines that don't exist, and `unsupported_paragraphs`. Fix the broken links and citations, which `save` refuses. For each unsupported paragraph, add a citation or mark it as opinion.
- The saved note lands in `_meta/briefings/`, with its `coverage`: the share of paragraphs backed by the vault.
