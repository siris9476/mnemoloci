---
name: mnemoloci-review
description: Show and decide Mnemoloci pages held in the review queue (low confidence, contradictions, citation problems, screenshots/imports). Use when the user wants to review pending pages or after a compile reports held pages.
---

# Review queue

1. `mnemoloci review list`: pending candidates with their reason codes.
2. `mnemoloci review show <id>`: full page, reasons with details, and `content_sha256`.
3. Present each candidate to the user:
   - the reasons, in plain words;
   - what the page says;
   - what is uncertain or wrong.
   Check the cited source lines yourself when the reason is `provenance-violating` or `low-confidence`.
4. Only the user decides, never you:
   - Approve: `mnemoloci review approve <id> --content-sha256 <hash from show>`. The hash pins exactly the version the user saw. If it fails with `CONTENT_CHANGED` or `CANDIDATE_STALE`, show it again.
   - Reject: `mnemoloci review reject <id> --reason "<why>"`. It is archived, not deleted, and comes back only if its sources change.
5. If the user wants changes, fix the underlying problem (extract or page) in a new compile run instead of approving a wrong page. Then reject the old candidate.
