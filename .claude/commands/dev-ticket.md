---
description: Take my Assembla ticket from Dev In Process to Code Review (or, for LOW risk, to staging); also pause/resume
argument-hint: "[ticket-number | pause [ticket-number]]"
---
Read `AI_DEV_ENV/skills/dev-ticket/SKILL.md` (under the repo root — `git rev-parse --show-toplevel`) and follow it exactly.

Arguments: `$ARGUMENTS`
- **empty** → always a fresh pick (`dev_ticket.py pick`). No resume check; resuming needs an explicit ticket number.
- **a ticket number N** → resume ticket N if `DEV_TICKETS\N\state.json` exists (or `restore N` finds a
  hand-over); otherwise `pick --ticket N`.
- **`pause`** → follow the skill's **Pause** section for the current ticket (the `FB_N` branch you're on).
  **`pause N`** → pause ticket N.
