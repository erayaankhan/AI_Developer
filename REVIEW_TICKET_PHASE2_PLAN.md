# Phase 2 plan: the reviewer's read-only review (`review-and-deploy`, steps R1 to R4)

Status: **approved and built, 2026-10-03.** Implements §5 R1 to R4 of `REVIEW_TICKET_DESIGN.md`. Does **not** approve, reject, move a status or deploy
(phases 3 and 4): the reviewer can pick, fetch, check and review a developer's ticket and nothing on Assembla changes.

**Scope.** In: `dt review-pick`, `dt review-fetch`, `dt review-compare`, the review state (`kind=review`, `review.stage`), a safe read-only checkout of
the developer's branch (WF and DB repo), the `review-and-deploy` skill (R1 to R4; R5 to R7 then marked "not built yet"), the slash command, tests. Out:
verdict, rework reopen, reviewer status moves, deploy.

## Decisions taken
1. **Review folder = `DEV_TICKETS\{N}\`, the author's layout**, with `state.kind = "review"` (and `role = "reviewer"`) and the stage in a separate
   `review.stage` (`R1-pick R2-fetch R3-check R4-review R5-verdict R6-deploy done`), so the author's `stage`/resume logic is never confused. Every script
   keyed on `state.json` (gates, risk check, `review-check`, `func-record`, `status_guard`) works unchanged.
2. **A folder holding an author's state is never overwritten:** `review-fetch` refuses it (`HAS_AUTHOR_STATE`); `/dev-ticket N` on a review folder is
   redirected to the review skill (one line in SKILL.md step 0).
3. **Safe, read-only checkout.** Clean tree (S7, untracked included). After `git fetch`: no local `FB_N` → `git switch --track origin/FB_N`; local behind
   origin → fast-forward only; local with commits origin lacks → refuse (`LOCAL_BRANCH_DIFFERS`), never reset. Same for `FB_D` in the DB repo, which a
   review **never creates** (missing → `DB_BRANCH_MISSING`). The reviewer never commits.
4. **The HANDOFF is checked, not trusted:** the latest comment with a valid HANDOFF block is used and its `author` must equal the Assembla login of the
   comment's actual writer (`HANDOFF_AUTHOR_MISMATCH`). That writer is the author of record; `SELF_REVIEW` if it is me.
5. **Functional checks need no new code:** the reviewer (Claude) writes `DEV_TICKETS\{N}\PLAN.md` holding only `## Functional checks` (FC-n from the
   acceptance criteria and SUMMARY); the existing `dt profile`, `func-mode`, `func-record` and `UI_CHECKS.md` run as for an author, always `run` on
   every tier (design decision 5).
6. **The review sub-agent gets `REVIEW.md` verbatim plus an "Inputs override"** (no BRIEF/PLAN of the developer: it reads `ticket.md`, the SUMMARY and
   HANDOFF comments saved under `DEV_TICKETS\{N}\hand-off\`, the diff and the DB files). Its report is an aid; `dt review-check` verifies its citations;
   the reviewer decides in phase 3.
7. **Dirty tree (decided 2026-10-03, option A):** `review-fetch` stops with `DIRTY` and lists the files; the reviewer commits (or stashes / pauses) their
   own work, then re-runs. No `git worktree` in phase 2 (possible phase 5: it needs a configurable repo root in every gate script and a second
   `npm install`). Claude never commits or stashes for them.
8. **Start-up:** `/review-and-deploy` (list or pick) or `/review-and-deploy N` (direct, or resume when `DEV_TICKETS\N` has `kind=review`). First
   `dt role` (unset → ask once, same remembered `DEV_ROLE`; developer → `NOT_A_REVIEWER`), then `dt whoami`; the login must be on `reviewers` (the single
   definition of "reviewer"). Pull-based: Assembla's assignment email is the signal.
9. **R1:** the reviewer's tickets only. One → "Review #N from <author>? (yes)" (low-stakes, 30 s default yes); several → the reviewer picks (suggest the
   lowest number: the staging job deploys lowest first); none → S1: say nothing is assigned and list Code Review tickets assigned to others **for
   information only** (a developer may have used the wrong login), nothing taken over. A ticket without a HANDOFF is "assigned, not handed off" and can't
   be reviewed yet. Before any git action: a one-screen summary (author, tier, HANDOFF age, files/lines, DB ticket) and the reviewer's "go".
10. **Extras:** `/review-and-deploy list` runs `review-pick` and stops; `dt deps N` joins R3 (gates and functional checks need `node_modules`); a second
    pick while a review is in progress warns and asks whether to park the first (one checked-out review at a time).

## Tasks (test first; order 1 → 3 → 2 → 4 → 5 → 6 → 7)
1. **Config + state keys** (`dev_ticket.config.json`, `dev_ticket.py` `STATE_FIELDS`): `review_stages`; `review.stage` accepted by `dt state --set`
   (validated); `kind=review` written only by `review-fetch`.
2. **`dt review-pick [--ticket N]`** (read-only): caller on `reviewers` (`NOT_A_REVIEWER`); my tickets in Code Review, in a `_CURRENT` milestone, Story, not
   DB companions, each with its latest HANDOFF summary (author, risk, head, round, or `no_handoff`). One → `PICKED`; none → `NONE` (R-S1) plus
   `assigned_to_others` for information; several → `MULTIPLE` (R-S2); `--ticket N` not mine / not Code Review → `NOT_ASSIGNED` / `WRONG_STATUS`.
3. **`latest_handoff(api, number)`**: newest comment with a valid block plus the writer's login (used by pick, fetch and later phases).
4. **`dt review-fetch N`**: guards (Code Review, assigned to me, `HAS_AUTHOR_STATE`, `NO_HANDOFF`, `HANDOFF_AUTHOR_MISMATCH`, `SELF_REVIEW`, `DIRTY`); saves
   ticket + attachments via the existing fetch code and the SUMMARY / REVIEW / HANDOFF comments to `hand-off\`; safe checkout (decision 3) for WF and DB;
   writes `state` (`kind=review`, `review={stage:R2-fetch, handoff:{…}, author, head}`); `STALE_HANDOFF` when the handoff head is not origin's `FB_N` tip.
5. **`dt review-compare N`**: after the reviewer ran `local_gates.py --ticket N` and `risk_check.py --ticket N`, compare with the HANDOFF: `STALE_HANDOFF`,
   `BEHIND_MASTER`, `GATES_NOT_PASS`, `FILES_DIFFER`, `RISK_HIGHER` (reviewer tier above the author's), `FUNCTIONAL_*` (reviewer's own run missing or
   stale). Result `COMPARE_OK` or the mismatches, stored in `state.review.compare`; `review.stage=R3-check` only on OK. Pure function
   `compare_handoff(...)`.
6. **Skill `skills/review-and-deploy/SKILL.md`** (start-up and role gate, `list`, R1 to R4 incl. `dt deps N` and the park warning; R5 to R7 stubs "phase
   3 / 4, not built"; conventions from dev-ticket; stop rules R-S1..R-S6; the "Inputs override" block), `.claude/commands/review-and-deploy.md` (repo
   root and `AI_DEV_ENV`), and the SKILL.md step 0 redirect for a `kind=review` folder.
7. **Docs:** `SETUP.md` (§4a), `PLAN.md` D-81, design doc marks phase 2 done.

## Verification
`test_review_flow.py` (API and git mocked, temp folders) plus all `python_scripts\test_*.py`. A **real read-only dry run** on a Code Review ticket
assigned to the developer, if one exists: `review-pick`, then `review-fetch` only on their yes (it switches their branch); otherwise it waits for the
phase 1 reassign test. No Assembla write, no commit, no change to existing commands' behaviour; `credentials.env` / `local_env.env` never opened; files
staged by name at the end. No inputs needed to start.
