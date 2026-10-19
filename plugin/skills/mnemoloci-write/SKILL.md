---
name: mnemoloci-write
description: Write a post, email, note or short text in the user's own voice, grounded in what their second brain knows. Use when the user says "write it the way I would", "prepare a post about X in my style", "draft an email about X", "write it in my voice", or, in Italian, "scrivi come scriverei io", "prepara un post su X con il mio stile", "bozza di email su X", "scrivilo con la mia voce".
---

# Write in the user's voice

1. **Material:** `mnemoloci context <argomento> --budget 3000`. These are the facts, with citations.
2. **Voice:** `mnemoloci voice [--limit 8]`. This gives samples of text the user wrote (their notes and longer messages) plus simple style numbers such as sentence length and first person.
   - Imitate the tone, rhythm, vocabulary and punctuation habits.
   - **Never** copy personal details from the samples into the new text.
   - If there are fewer than 3 samples, say so: the voice will be approximate.
3. **Write** the draft: facts only from the context pack, in the user's voice.
   - For a post or an email, don't include wiki citations. Keep a short list of the pages used under the draft so the user can check them.
   - Mark anything you added from general knowledge.
4. Offer to adjust: shorter or longer, more formal or casual, a different audience. Never publish or send anything: the user does that.
