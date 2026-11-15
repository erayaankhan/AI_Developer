# Phase 4 plan: the reviewer deploys the approved ticket

Status: **approved and built, 2026-10-03.** Implements §6 and R6/R7 of `REVIEW_TICKET_DESIGN.md`. The **reviewer deploy mode**: after approving, the
reviewer moves the ticket to DevOps Staging and runs the existing staging job, bound to the head they approved. `staging_deploy.py` and
`deploy_staging_local.py` are reused unchanged except for their guards.

**Scope.** In: closing the author-path gap, `dt review-deploy`, `staging_deploy.py deploy --reviewer`, the DB ticket's second run, withdrawing an
approval, skills, docs, tests. Out: changes to the Jenkins job or `deploy_staging_local.py`; QA test-case generation (stays off); the developer's side.

## What changes
| Today | Phase 4 |
|---|---|
| Author path: `dt status … DevOps Staging --requested` + `staging_deploy.py deploy --requested` accept a review folder (`stage=done`) and never look at the approval. | **Refused for review folders** (`REVIEW_FOLDER` / `USE_REVIEWER_MODE`); the reviewer mode is the only way. |
| `staging_deploy.py` knows `LOW` and `--requested`. | New `--reviewer`: needs `kind=review`, an APPROVE verdict and `review.approved_head`. |
| `owner_of(D)` finds the owner's `state.db.ticket`, so the DB ticket's deploy attempt lives in the owner's state. | The review state carries `db.ticket` (set by `review-fetch`): the DB ticket's second run works the same, bound to `approved_db_head`. |
| Reviewer status table: `Code Review to Dev Done`, `Code Review to Dev Queue`. | Adds `Dev Done to DevOps Staging` (deploy request) and `Dev Done to Code Review` (withdraw). |

## Decisions taken
1. **The deploy request is its own command, `dt review-deploy N [--yes]`**, not `dt status --reviewer`: it moves Dev Done to DevOps Staging only while the
   approval holds and is the single place the reviewer's "deploy" yes is asked. `dt status` stays the author's tool and now refuses a review folder.
2. **The approval is bound to commits:** `review-deploy` and `deploy --reviewer` compare origin's tips with `approved_head` (and `approved_db_head`); a
   later push gives `APPROVAL_STALE` and nothing moves or triggers.
3. **Existing guards plus the reviewer ones:** DevOps Staging, Story, `_CURRENT`, lowest-numbered (`FIRST`), `FB_N` on origin and equal to the local
   checkout, one trigger per ticket (`ALREADY_TRIGGERED`), `--retry` only on the reviewer's say-so, no other deploy running (`BUSY`), `JENKINS_REMOTE_*`
   present. Added: caller on `reviewers` and not developer, assigned to me, author is not me, verdict is APPROVE.
4. **The trigger is unchanged:** `deploy_staging_local.py`, QA generation off, `DEPLOYED` / `DEPLOYED_NO_APPS` / `DEPLOY_FAILED` and the rest of the result
   table as in `LOW_PATH.md`; the job moves the ticket to Staging Code Merged and QA Queue itself and the script polls.
5. **The DB ticket is deployed in its own second run** (N first, D next), as today; D moves with N (companion rule).
6. **Withdraw an approval** (`dt review-withdraw N`): Dev Done back to Code Review, assigned to me, approval cleared, so the reviewer can re-fetch if the
   developer pushed again or they changed their mind. Small and safe; recommended.
7. **State is local:** the approval lives in the reviewer's `DEV_TICKETS\N\state.json`; deploying from another PC means redoing the review there (no
   restore for a review folder). A documented limitation, not solved here.

## Tasks (test first; order 1 → 7, task 1 first: it closes the open gap before anything new relies on it)
1. **Close the gap + config** (`dev_ticket.config.json`, `dev_ticket.py`, `staging_deploy.py`): `reviewer_status_moves` gains `Dev Done to DevOps Staging` and
   `Dev Done to Code Review`; `status_guard` refuses `kind=review` (`REVIEW_FOLDER`); the author modes of `staging_deploy` (LOW, `--requested`) refuse a
   review folder (`USE_REVIEWER_MODE`). Test: the old path is closed for review folders, unchanged for authors.
2. **`dt review-deploy N [--yes]`** (`dev_ticket.py`): guards (reviewer, assigned to me, status Dev Done, author is not me, APPROVE verdict, origin tips equal
   the approved heads); preview; with `--yes` `Dev Done to DevOps Staging` (+ DB companion), `review.deploy_requested`. Tests: every refusal,
   `APPROVAL_STALE`, no write without `--yes`, companion, a failed move reported and not retried.
3. **`staging_deploy.py deploy N --reviewer`:** reviewer guards bound to the approved heads, then the unchanged guards and trigger; `--retry` allowed with
   `--reviewer --yes`. Tests: each guard refuses before any trigger, the trigger runs once, the result table is unchanged.
4. **DB ticket in reviewer mode:** `deploy D --reviewer` resolves the owner's review state, checks `approved_db_head`, uses `db_deploy_attempt`. Tests:
   owner found, stale DB head, one trigger per DB ticket.
5. **`dt review-withdraw N [--yes]`** (decision 6). Tests: status moved back, approval cleared, assigned to me, refusals.
6. **Skills** (both `SKILL.md`): `review-and-deploy` R6 (deploy request, queue, background run, results, DB second run) and R7 (`review.stage=done`,
   session end), stop rules R-S13 and up; `dev-ticket` "Deploy on request" says review folders are refused.
7. **Docs:** `SETUP.md` §4a, `PLAN.md` D-83, design status, `LOW_PATH.md` pointer.

## Verification and risks
- `test_review_deploy.py` (Assembla, git and the Jenkins subprocess mocked; `run_deploy_script` replaced), then the whole suite. **No real deploy without
  the developer's explicit yes:** the job merges `FB_N` into master and builds, so a real run is not a harmless test. A real one needs a throwaway ticket
  whose branch changes only a docs folder (expected result `DEPLOYED_NO_APPS`) and a time when no one else is deploying; `staging_deploy.py check`
  (read-only) can verify the reviewer's Jenkins keys any time. Every outward action needs the reviewer's explicit yes; no commit; `credentials.env` /
  `local_env.env` never opened; files staged by name at the end.
- **The deploy is the highest-impact action in the flow:** the guards are strict on purpose (approval bound to heads, queue position, one trigger per
  ticket); mocked tests cannot prove the Jenkins side. The unproven Assembla assumptions of earlier phases (a plain member's reassign and status move)
  still apply to `review-deploy`. If the developer pushes between approval and deploy the reviewer must withdraw and re-review: by design.
- Inputs needed: yes or changes on the decisions (especially 1 and 6); whether to run a real docs-only throwaway deploy test at the end, or only mocked tests.
