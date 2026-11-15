---
description: Reviewer: review a developer's ticket left in Code Review for me (read-only checkout, gates, risk, functional checks, AI review aid), then approve it (Dev Done) or send it back, and deploy an approved ticket to staging
argument-hint: "[ticket-number | list]"
---
Read `AI_DEV_ENV/skills/review-and-deploy/SKILL.md` (under the repo root — `git rev-parse --show-toplevel`) and follow it exactly.

Arguments: `$ARGUMENTS`
- **empty** → role check, then R1: pick from my Code Review tickets that carry a hand-off (`dev_ticket.py review-pick`).
- **`list`** → show what is waiting for me (`review-pick`) and stop; fetch nothing.
- **a ticket number N** → resume the review of N if `DEV_TICKETS\N\state.json` has `kind=review`; otherwise `review-pick --ticket N`.
