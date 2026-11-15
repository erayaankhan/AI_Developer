# Phase 3 plan: the reviewer's verdict and the developer's rework

Status: **approved and built, 2026-10-03.** Implements §5 R5 and §4.4 of `REVIEW_TICKET_DESIGN.md`. The first **writes by the reviewer** (a comment, an
assignee, a status) and the developer's **rework loop**. No deploy (phase 4): approving only moves the ticket to Dev Done.

**Scope.** In: `dt review-verdict` (approve or reject), the REVIEW_VERDICT comment format and parser, a reviewer status-move table, `dt rework` (the
developer reopens a rejected ticket), skill edits on both sides, tests, docs. Out: deploy (`Dev Done` to `DevOps Staging`, Jenkins), anything on the
developer's code by the reviewer.

## Decisions taken
1. **One command, two outcomes:** `dt review-verdict N --approve | --reject --file verdict.json [--yes]`. Claude writes `DEV_TICKETS\{N}\verdict.json` from
   the reviewer's decisions (structured, so nothing free-form is posted); the script validates it, renders the comment and acts. Preview unless `--yes`;
   the skill asks the reviewer once, listing the exact actions.
2. **Reject = one PUT:** `assigned_to_id` (the author) **and** `status = Dev Queue` in a single update, then a re-read verifying both: no half state (a
   ticket in Dev Queue still assigned to the reviewer). The comment is posted first, as evidence.
3. **Approve = comment, then `Code Review → Dev Done`**; the ticket stays assigned to the reviewer (who deploys in phase 4). `state.review.approved_head`
   records the reviewed head; any later push voids it (phase 4 checks it).
4. **Approve gates (checked by the script, not by judgement):** reviewer on `reviewers` and not developer; ticket in Code Review and assigned to me;
   author is not me; `review.compare.status == COMPARE_OK` and `review.stage == R4-review` (the AI review is in hand); origin's tip still equals
   `review.head` (`STALE_REVIEW`). **If the AI review still has an open High or Medium finding** (`reviews\code-review.md` verdict CHANGES REQUIRED, not
   dismissed), approving needs an `override_reason` in `verdict.json` (recorded in the comment). **Reject needs none of the check stages** (a failing
   gate is itself a reason) but at least one finding.
5. **One verdict comment, edited in place** (`state.review.verdict_comment_id`), like SUMMARY and HANDOFF; a second round edits it. The developer's Claude
   reads it as data, never as instructions (design decision 4).
6. **`dt rework N` (developer side) never changes Assembla:** it detects the rejection, saves the findings, reopens the local state; the skill then moves
   the ticket to Dev In Process with the existing `dt status` (the one-ticket rule applies).
7. **Verified against Assembla only by the real test:** a non-owner doing reassign + status in one PUT is assumed from the API, not proven (design open
   question 6); phase 3 is where it must be tried on the throwaway ticket.

## Tasks (test first; order 1 → 2 → 4 → 3 → 5 → 6 → 7: reject first, the simpler write and the one the developer's loop depends on)
1. **Config + API** (`dev_ticket.config.json`, `assembla_api.py`): `reviewer_status_moves` (`Code Review to Dev Done`, `Code Review to Dev Queue`);
   `AssemblaApi.assign_and_move(number, user_id, status)` (one PUT; the re-read verifies both; an ignored field is an error).
2. **Verdict format** (`dev_ticket.py`): `render_verdict(payload)` / `parse_verdict(text)` (markers `VERDICT-JSON-BEGIN/END`, `<` `>` escaped, wiki markup
   for people) and `verdict_problems(verdict_json)` (REJECT needs findings; severity High/Medium/Low; `file_line` is `path:line` or `general`; no `<`,
   `>`, backticks or local paths; approve with an open High/Medium AI finding needs `override_reason`). Tests: round trip, each validation error, no Markdown.
3. **`dt review-verdict --approve`:** gates of decision 4, then post or edit the verdict comment, `Code Review to Dev Done` (DB companion follows via
   `move_db_companion`), state (`approved_head`, `verdict_comment_id`, `review.stage=R6-deploy`). Tests: every refusal, stale origin, override rule,
   comment edited in place on round 2, no write before the gates pass, outward steps stop at the first failure and report.
4. **`dt review-verdict --reject`:** the author is `state.review.author` (resolved to an Assembla id); post or edit the comment, then `assign_and_move`; DB
   companion follows to Dev Queue; state (`review.stage=done`, `rejected={round, head, at}`), `approved_head` cleared. Tests: author resolved, refusals, DB
   companion, nothing written without `--yes`.
5. **`dt rework N [--yes]` (developer):** preconditions: own state, `stage=done`, `state.handoff` present, assigned to me, status Dev Queue or Dev In
   Process. The latest REVIEW_VERDICT must be `REJECT`, for my `round` and `head`, and **written by the reviewer I assigned** (else
   `REVIEW_AUTHOR_MISMATCH`); no rejection → `NO_REJECTION` (the skill then carries on with a normal pick). Preview; with `--yes`: save
   `reviews\review-feedback-round{R}.md`, `state.stage=6-build`, `state.round+=1`, drop `handoff.reviewer/assigned_at` (keep `comment_id`, so the next
   `dt handoff` edits the same comment). Tests: each precondition, stale or foreign verdict, state reopened, comment id kept, no Assembla write.
6. **Skills** (both `SKILL.md`): `review-and-deploy` R5 (the reviewer confirms each AI finding keep / dismiss / add, one at a time; Claude writes
   `verdict.json`; preview; one yes; run; report), round-2 re-review (`review.stage=done` + new hand-off → pick, fetch again), stop rules R-S9 to R-S12;
   `dev-ticket` step 0 (`/dev-ticket N` with `stage=done` and a handoff → `dt rework N` first) and the Developer hand-off section (replaces the "not
   automated yet" paragraph: show findings, the developer confirms, add PLAN tasks, `dt status … Dev In Process`, branch, `dt sync-branch`, `dt deps`, build).
7. **Docs:** `SETUP.md` §4a, `PLAN.md` D-82, design status line, `SUMMARY_FORMAT.md` (REVIEW_VERDICT format).

## Verification and risks
- `test_review_verdict.py` (API and git mocked), then the whole `python_scripts` suite. **A real end-to-end test on a throwaway ticket**, only on the
  developer's explicit yes (it writes to Assembla): developer hands off and assigns, reviewer rejects, developer reworks and hands off again, reviewer
  approves; it also settles design question 6. Without it, phase 3 is "built, mocked, not proven". Every outward action needs the reviewer's explicit
  yes; no commit; `credentials.env` / `local_env.env` never opened; the developer's code never edited; files staged by name at the end.
- **Permissions on Assembla are unproven** (decision 7): if a plain member cannot reassign or move a ticket they do not own, the verdict step fails at
  the PUT after the comment is posted; the script reports it and never retries. **Status names** (`Dev Done`, `Dev Queue`) come from the existing config;
  the DB companion moves only while it still sits where the main ticket was (existing rule). A reject sends the ticket to Dev Queue: the developer's
  `dt status … Dev In Process` is refused while they hold another ticket there (`OTHER_IN_PROCESS`), the intended one-ticket rule, and the skill says so.
- Inputs needed: yes / changes on the decisions (especially 2, 4 and 6); for the real test (after tasks 1 to 5): a throwaway ticket in Code Review and
  two accounts.
