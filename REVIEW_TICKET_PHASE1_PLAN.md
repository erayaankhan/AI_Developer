# Phase 1 plan: developer hand-off to a reviewer

Status: **approved and built, 2026-10-03.** Implements §4 of `REVIEW_TICKET_DESIGN.md` (role question, HANDOFF comment, assign to a reviewer). No
reviewer commands or deploy (phases 2 to 4).

**Scope.** In: `dt role`, `dt handoff`, `dt assign`, config keys, a `NOT_A_REVIEWER` guard so a developer can't deploy, skill/doc edits, tests. Out:
`review-*` commands, `--reviewer` deploy mode, reviewer status moves, rework reopen.

## Tasks (test first; order 1 → 2 → 3 → 6 → 4 → 5 → 7 → 8: role and deploy guard first, assign needs handoff)

1. **Config** (`python_scripts/dev_ticket.config.json`): `reviewers` (Assembla logins; shipped empty, the lead fills it; empty makes `assign` refuse with
   `REVIEWERS_NOT_CONFIGURED`), `roles` (`["developer","reviewer"]`), `role_env_key` (`DEV_ROLE`).
2. **`dt role`** (no ticket, no API; `dev_ticket.py` `cmd_role`, handler, argparse, no-API list; `functional.py` only if a write helper is needed):
   reports `developer`, `reviewer` or `unset` from `DEV_ROLE` in `AI_DEV_ENV\local_env.env` (read via `functional.load_env_file`; only the role word is
   echoed). `dt role --set developer|reviewer --yes` writes/replaces that one key, keeps the other lines; previews without `--yes`. Claude never opens
   the file. Tests: reporting of all three states, bad value refused, other keys untouched, file created if missing, no other value printed.
3. **Role into the ticket** at `pick`/`fetch`: `state.role`; `role` and `handoff` in `STATE_FIELDS` (`dt state --set role=…`, validated); a resumed
   ticket keeps its role.
4. **`dt handoff N`** (`dev_ticket.py`, `SUMMARY_FORMAT.md`): builds `DEV_TICKETS\N\HANDOFF.md` (header + fenced JSON, design §4.2) from `state.json`,
   `gates\`, `risk_check`, `functional`, `db`, `baseline` and `git rev-parse HEAD`. Refuses unless the branch is pushed (`dt pushed` logic:
   `NOT_PUSHED`), gates PASS and current (as `GATES_STALE`), and `SUMMARY.md` passes `summary-check`. Preview by default; `--yes` posts via the existing
   comment path (file inside `DEV_TICKETS\N\`, `--edit-id` on later rounds) and stores `state.handoff = {comment_id, head, round, at}`. Tests: right
   facts, refusals, edit in place on round 2, no path outside the repo and no secret.
5. **`dt assign N --list`** prints `reviewers` (login + Assembla display name). **`dt assign N --to <login> [--yes]`**: preview, then PUT `assigned_to_id`
   and re-read to verify (like `update_status`). Refuses: not in Code Review (`WRONG_STATUS`), no HANDOFF (`NO_HANDOFF`), `state.handoff.head` ≠ current
   pushed head (`STALE_HANDOFF`), login not in `reviewers` (`NOT_A_REVIEWER`), login = me (`SELF_ASSIGN`). Logs a metrics event. Tests: each refusal,
   success verified by re-read (mocked API), no write without `--yes`.
6. **`NOT_A_REVIEWER` guard** (`staging_deploy.py`, `dev_ticket.py` `status_guard`): `staging_deploy.py deploy` and `dt status … DevOps Staging` /
   `Dev Done` refuse when `state.role == developer`, before the credentials check, with a message pointing to hand-off; a reviewer is unaffected.
7. **Skill edits** (`skills/dev-ticket/SKILL.md`, `LOW_PATH.md`, `SUMMARY_FORMAT.md`): step 0 asks the role when `dt role` says `unset` (not low-stakes,
   no silence rule) and conventions for `dt role`; step 8 for a developer: stage → commit (**Claude always gives the commit message**) →
   `dt sync-branch --yes` → gates → `dt handoff` → comments (SUMMARY, REVIEW, HANDOFF) → Code Review → `dt assign --list`, the developer picks a reviewer,
   `dt assign --to … --yes`; stop rules `NOT_A_REVIEWER`, `NO_HANDOFF`, `STALE_HANDOFF`, `REVIEWERS_NOT_CONFIGURED`. `LOW_PATH.md`: a developer stops
   after the Code Review move (steps 4 to 7 not offered). Reviewer flow untouched.
8. **Docs** (`SETUP.md`, `PLAN.md`): where `DEV_ROLE` lives, the `reviewers` config, a developer has no `JENKINS_REMOTE_*`; decision-log entries D-7x.

## Verification, risks, inputs
- New `python_scripts\test_role_handoff.py` (style of `test_gate_rounds.py`; API mocked, temp folders, no real Assembla call), run with the existing
  `test_*.py` files (`python -m pytest python_scripts\test_role_handoff.py`) to show no regression. One **real** write test on a throwaway ticket, only on
  the developer's explicit yes: reassign a Code Review ticket to a second account and back (also settles design open question 6).
- No schema/API-contract change: new `dt` subcommands and additive config/state keys only; existing commands behave the same for a reviewer or an
  `unset` role (it asks the question, never blocks a ticket mid-flight). Claude never commits; every commit instruction carries the exact message;
  conflicts stay with the developer (S8). The role is a convenience, not security: Jenkins credentials stay the real deploy gate. Tickets already in
  flight have no `state.role` and are treated as the machine's `DEV_ROLE`. I never open `credentials.env` or `local_env.env` (`dt role` is the only code
  touching `DEV_ROLE`); I only `git add` the changed files, you commit.
- Inputs still needed: the reviewers' Assembla logins for `reviewers` (until then `assign` refuses cleanly); a yes, a ticket and a second account for the
  real reassign test.
