# dev-ticket — Pause and resume

`dt` = `python {PY}/dev_ticket.py`.

## Pause (`/dev-ticket pause [N]`, any time after step 3)

1. Stage only the ticket's files (never local-only files, never `DEV_TICKETS/`); plus `state.db.files` in the DB repo.
2. Give the developer the WIP commits. **They run them**:
   `re#{N} WIP | paused at {stage}: {next task}` (DB repo: `re#{D} - {N} - WIP paused at {stage}`), then push both.
3. Update BRIEF.md "Assumptions / Decisions". Write `handover_notes.md` with `## Where I stopped` (last task,
   next task, anything half-done and where), `## Decisions made so far`, and `## Open points / blockers`.
4. `dt handover {N} --notes …\handover_notes.md` → `PREVIEW`. `NOT_PUSHED` → wait for the pushes.
5. Show the plan (attach `HANDOVER-{N}-<date>.md`, a one-line comment, Dev In Process → Dev Queue).
   On yes, re-run with `--yes` → `PAUSED`. Then give the next-ticket hint (SKILL.md Conventions).

## Resume (`stage = paused`, or no local folder)

1. No `DEV_TICKETS\{N}\state.json` → `dt restore {N}`. It rebuilds BRIEF, PLAN and state from the latest hand-over.
   `LOCAL_EXISTS` → show both timestamps and ask which to keep (`--force` = the attachment).
2. The ticket must be assigned to the current developer (a reassigned developer may resume). Otherwise S3.
3. In Dev Queue → the one-ticket rule (`dt pick` must show no other Dev In Process ticket), then ask:
   `dt status {N} --to "Dev In Process" --yes --confirm {H}`.
4. `dt branch {N}` (+ `dt db-branch {D}`).
   Then, **before any code is written**: `dt sync-branch {N} --yes` (pull origin/FB_{N}, push, pull origin/master into it, push). `CONFLICTS` → report the files to the developer to resolve, then re-run.
5. Drift: `dt fetch {N}` (compare with BRIEF), `dt sync-check {N}`, and `risk_check.py --ticket {N}` if code exists.
   Report changes and ask whether BRIEF or PLAN needs updating.
6. Read HANDOVER.md "Where I stopped", `dt state {N} --set stage=<resume_stage>` (the `resume_stage` field of the `dt restore` RESULT; for a local pause it is `state.paused_from`), and continue.
   The review and the functional checks (unless skipped) are always re-run.
