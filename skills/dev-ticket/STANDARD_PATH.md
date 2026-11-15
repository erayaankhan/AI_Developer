# STANDARD path hand-off and deploy on request

_Read from `SKILL.md` step 8 when the role is reviewer and the ticket is not on the LOW path (`LOW_PATH.md` covers LOW). Conventions, stop rules (S-numbers) and `dt` are defined in `SKILL.md`._

**STANDARD:** order is **commit → sync (pull FB_N, push, pull master, push) → gates**. You commit on the developer's yes; `dt sync-branch` does the pulls and the push; you prepare and verify.

| Step | You give / run | Must see |
|---|---|---|
| a | show `git commit -m "…"` and run it on the developer's yes (+ the DB repo: commit and `git push -u origin FB_{D}`) | `dt pushed {D} --db` → `PUSHED` for the DB repo; the WF push comes from step b |
| b | `dt sync-branch {N}` (preview), then `dt sync-branch {N} --yes` (pull FB_N, push, pull master, push) | `NOT_COMMITTED` → step a first. `SYNCED` / `UP_TO_DATE` → c. `CONFLICTS` → S8: report the files (`merge_conflicts`, `step`), the developer resolves and commits, re-run. `FAILED` → report it |
| c | when `verify_gates` is true (see "Branch sync" in `SKILL.md`): re-run 7a | `PASS`. A fix = a new developer commit, then `dt sync-branch {N} --yes` again |
| d | `dt pushed {N}` (+ `--db`) | `PUSHED`. `BEHIND_ORIGIN` → `dt sync-branch {N} --yes`. `NOT_COMMITTED` (staged **or unstaged** edits) / `NOT_PUSHED` → wait. |
| e | ask: post SUMMARY? move to Code Review? | `dt comment {N} --file …\SUMMARY.md --yes --confirm {H}` / `dt status {N} --to "Code Review" --yes --confirm {H}` (only the parts they said yes to; `NOT_ALLOWED`, `NO_STATE`, `NOT_ASSIGNED`, `OTHER_IN_PROCESS` or `NOT_PUSHED` → tell them: the script refuses a move whose code isn't pushed, or a second ticket in Dev In Process) |

Then `dt state {N} --set stage=done`. After Code Review the team's normal flow takes over. Give the next-ticket hint (SKILL.md Conventions).

## Deploy on request (MEDIUM / HIGH, STANDARD path; reviewers only)

**Only for tickets the developer authored.** A folder with `state.kind = review` (a developer's ticket the reviewer approved) is deployed
with `/review-and-deploy` (`dt review-deploy`, `staging_deploy.py --reviewer`), which checks the approval and the approved head;
`dt status` and `staging_deploy.py --requested` refuse such a folder (`REVIEW_FOLDER`, `USE_REVIEWER_MODE`).

Never automatic: only when the developer says "deploy {N}". Check Assembla first, in this order, and stop at the first failure:
1. "deploy {N}" is itself the go-ahead to move the ticket. If it is in Code Review, run
   `dt status {N} --to "DevOps Staging" --requested --yes --confirm {H}` (the script refuses unless the WF branch and the DB branch are pushed
   and hand-off is finished: `NOT_PUSHED` / `NOT_HANDED_OFF` → report, stop). Any other status → report it, don't force it.
   Then `python {PY}/staging_deploy.py queue {N}`: must be `FIRST` (Plan level = Story). `QUEUED` → report `ahead`, stop.
2. If `state.db.ticket` is set, show its Assembla status and `dt pushed {D} --db`; the DB change must be pushed (S6c–e, `DB.md`). The DB ticket moves with N (`dt status` follows it), but it is a separate Story in the deploy queue.
3. Show the summary (ticket, risk tier, queue, branch head, DB ticket) and wait for a yes; then run
   `python {PY}/staging_deploy.py deploy {N} --requested --yes` **in the background** (45+ min), same RESULT table as `LOW_PATH.md`. It runs `deploy_staging_local.py` (self-contained, in `python_scripts\`) directly, no local Jenkins job; it needs `JENKINS_REMOTE_USER` / `JENKINS_REMOTE_API_TOKEN` (build server) in `credentials.env`, check with `staging_deploy.py check`
   (`NOT_HANDED_OFF` = the ticket never finished hand-off). Never re-trigger on your own (`ALREADY_TRIGGERED`). Only when the developer says the earlier run failed and asks for a retry:
   move the ticket back to DevOps Staging if they moved it out, run `dt sync-branch {N} --yes` (pull master), then
   `staging_deploy.py deploy {N} --requested --retry --yes`; it archives the old attempt, once. After the job succeeds the ticket sits in
   "Staging Code Merged" (merge OK; the job then triggers the app builds itself); the script keeps polling Assembla until
   `QA Queue` (= `DEPLOYED`), "Merge Failed" / "Build Failed" (= `DEPLOY_FAILED`). If it timed out in between:
   `python {PY}/staging_deploy.py confirm {N}` (read-only). There are no staging UI checks: the functional checks ran before hand-off.

**The DB ticket (`state.db.ticket` = D) is deployed in its own, second run** (the job takes one ticket per run, lowest number first, so N goes first and D next; the code reaches staging before its DB scripts, which the developer accepted). When N is `DEPLOYED` and D is in DevOps Staging (it moved with N): show D's status, ask, and on the developer's yes run `staging_deploy.py queue {D}` (must be `FIRST`) and then `python {PY}/staging_deploy.py deploy {D} --requested --yes` **in the background** (45+ min). The same guards apply, read for the DB ticket: its owner's hand-off state, the branch `FB_{D}` on the **DB repo's** origin and equal to the local one, one trigger per ticket (`ALREADY_TRIGGERED`, `--retry` only on the developer's say-so). `DEPLOYED_NO_APPS` is the normal result: the job merged the DB branch and sent the scripts to the database, and no application is built. Tell the developer that the DB scripts are on staging only after this run.
