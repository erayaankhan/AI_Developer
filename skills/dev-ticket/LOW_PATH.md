# dev-ticket — LOW-risk hand-off (step 8)

**Reviewers only.** A developer's ticket, LOW included, ends at Code Review assigned to a reviewer (`HANDOFF.md`):
the steps below from step 4 on (Dev Done, DevOps Staging, deploy) are never offered to a developer (`NOT_A_REVIEWER`, S15).
The reviewer then reviews and deploys that ticket with `/review-and-deploy` (`skills/review-and-deploy/SKILL.md`); this file is only for
tickets the reviewer authored, and its commands refuse a review folder (`REVIEW_FOLDER`, `USE_REVIEWER_MODE`).

Only when `path=LOW` **and** `risk_check.min_tier=LOW`; otherwise use STANDARD (`STANDARD_PATH.md`).
The scripts enforce this: status moves past Code Review and the deploy refuse (`NOT_ALLOWED` / `NOT_LOW_PATH`)
for anything else. `dt` = `python {PY}/dev_ticket.py`.

## 1. One summary screen, then wait for "go"

```
#N  LOW  (risk re-check on diff: LOW, <files> files, <lines> lines)
Tests: … · Gates: PASS · AI review: 0 High/Medium, <n> Low · Master: <UP_TO_DATE|BEHIND n>
Functional checks: <n>/<n> PASS in Chrome, or: <n> checks for YOU to do first (listed), or: skipped by you (<reason>)
Commit: re#N CAT | …
Next: commit → sync branch: pull FB_N, push, pull master, push → gates + risk re-check → summary comment → Code Review → Dev Done → DevOps Staging
      → deploy if first in queue → confirm QA Queue
Type "go" to run all of it (commit, branch sync and the rest).
```
Anything but "go" → `dt state {N} --set path=STANDARD`, then STANDARD from step a. If checks are listed for the developer, **"go" also means they did them and they pass.**

## 2. After "go": in order, stop at the first failure, never retry an outward action

| # | Run | Must see / on failure |
|---|---|---|
| 1 | Stage the ticket's files, then `git commit -m "…"` (DB repo too, with its push). The "go" is the yes to commit. | the commit exists (`dt pushed {D} --db` → `PUSHED` for the DB repo) |
| 2 | `dt sync-branch {N} --yes` (pull origin/FB_N, push, pull master, push) | `SYNCED` / `UP_TO_DATE` → re-run 7a (`PASS`) and `risk_check.py --ticket {N}` (the guards refuse a LOW verdict the diff has outgrown), then `dt pushed {N}` → `PUSHED`. `CONFLICTS`, or a failed gate → **`dt state {N} --set path=STANDARD`, then report the files for the developer to resolve (S8)** and continue as STANDARD step b |
| 3 | `dt comment {N} --file …\SUMMARY.md --yes --confirm {H}` (the AI review stays in `reviews\code-review.md`, not posted) | `POSTED` |
| 4 | `dt status {N} --to "Code Review" --yes --confirm {H}`, then `"Dev Done"`, then `"DevOps Staging"` | each `UPDATED` |
| 5 | `python {PY}/staging_deploy.py deploy {N} --yes` (runs `deploy_staging_local.py`), **in the background** (45+ min): wait for the notice, don't poll | table below |
| 6 | `DEPLOYED`: nothing more to run (no staging UI checks; the functional checks ran before hand-off) | |
| 7 | `dt state {N} --set stage=done`; report the commit, comments, statuses, deploy and checks; give the next-ticket hint (SKILL.md Conventions) | |

The deploy always runs with QA test-case generation off. This workflow never creates QA test cases.

| Deploy RESULT | Action |
|---|---|
| `DEPLOYED_NO_APPS` | succeeded and merged into master, but **no app was built** (empty build list). Normal when the merge brought only folders without a build job (see `apps_with_build_job`): say so to the developer |
| `DEPLOYED` | #N is in QA Queue → step 6 (the script waits through the intermediate "Staging Code Merged"; "Merge Failed" / "Build Failed" → `DEPLOY_FAILED`; if it timed out meanwhile, `staging_deploy.py confirm N`) |
| `QUEUED` | a lower-numbered ticket is first (`ahead`): **not triggered**. `dt state {N} --set deploy=queued`, keep `8-handoff`, report, stop |
| `BUSY` | a deploy is already running: not triggered. `--set deploy=busy`, keep `8-handoff`, stop |
| `NOT_LOW_PATH` `NOT_HANDED_OFF` `WRONG_STATUS` `NOT_STORY` `BRANCH_NOT_ON_ORIGIN` `ORIGIN_DIFFERS` `NOT_IN_QUEUE` `NO_CURRENT_MILESTONE` | a guard failed: not triggered. Report and stop |
| `DEPLOY_FAILED` `DEPLOYED_OTHER_TICKET` `DEPLOYED_STATUS_UNCONFIRMED` `FAILED` `ALREADY_TRIGGERED` | report the build number, escalations and status. **Never re-trigger** |
| `ERROR` | with a `build` field → a build was triggered: report it, never re-trigger. Otherwise: Jenkins credentials → point to SETUP.md §2 (`JENKINS_REMOTE_*` keys for the build server); the ticket stays in DevOps Staging |

## Deploy later (resume with `state.deploy` = queued / busy)

`python {PY}/staging_deploy.py queue {N}`: still `QUEUED` → report the position and stop. `FIRST` → step 5 (covered
by the original "go"). `NOT_IN_QUEUE` → check the status. In QA Queue → `dt state {N} --set deploy=deployed_by_other_run`,
then step 6. Otherwise report it.
