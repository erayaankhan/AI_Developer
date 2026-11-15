---
name: review-and-deploy
description: A reviewer's side of the developer hand-off. Picks a ticket a developer left in Code Review assigned to them, takes it over read-only (developer's branch, hand-off comments), reproduces the gates, risk check and functional checks, runs an independent AI review as an aid and compares everything with the developer's HANDOFF. After the reviewer decides it posts the verdict: approve (Dev Done) or send back to the developer (Dev Queue). On "deploy" it moves the ticket to DevOps Staging and runs the staging deploy bound to the approved head (the DB ticket in its own second run). Stops and asks whenever a rule is not met. Use when a reviewer says "/review-and-deploy", "review my ticket", "/review-and-deploy 18474" or "/review-and-deploy list".
---

# review-and-deploy

```
0 START → R1 PICK → R2 FETCH → R3 CHECK → R4 REVIEW → R5 VERDICT → R6 DEPLOY → R7 DONE
```
Design: `AI_DEV_ENV\REVIEW_TICKET_DESIGN.md` (decisions) and `REVIEW_TICKET_PHASE2_PLAN.md` to `PHASE4_PLAN.md` (what is built).
Counterpart of `skills/dev-ticket/SKILL.md`: **a developer's ticket ends there at Code Review, assigned to a reviewer.**

## Conventions (as in dev-ticket)

- **Shorthand:** `{ROOT}` = repo root (`git rev-parse --show-toplevel`); `{PY}` = `{ROOT}/AI_DEV_ENV/python_scripts`; **`dt`** = `python {PY}/dev_ticket.py`. Use forward slashes in any path passed to `python`: in Bash a backslash path loses its backslashes.
  Every script ends with one `RESULT: {json}` line: act on that line only.
- **One simple command per Bash/PowerShell call** (no `cd`, pipes or `cat` chains); scripts as `python {PY}/<script>.py`.
- **Never open** `{ROOT}\AI_DEV_ENV\credentials.env` or `local_env.env`; never ask for secrets in chat. The role is read and written only through `dt role`.
- **Read-only review:** the checkout is the developer's pushed branch. **Never edit, stage, commit or push on it**; every fix goes back to the
  developer. `dt review-compare` reports `TREE_MODIFIED` if it was touched.
- **Consent:** nothing is posted, moved or deployed without the reviewer's explicit yes in this conversation. **Silence is never a yes**;
  there is no timed default anywhere (ask, then wait).
- **Work folder** `{ROOT}\DEV_TICKETS\{N}\` (git-ignored), written by `dt review-fetch`: `ticket.md/json`, `attachments\`, `hand-off\` (the
  developer's HANDOFF, SUMMARY and own review as files), `state.json` (`kind=review`), later `PLAN.md` (functional checks only), `gates\`,
  `reviews\`. The review's stage is `state.review.stage` (`R2-fetch R3-check R4-review R5-verdict R6-deploy done`); set it only with
  `dt state {N} --set review.stage=…`. The author's `stage` is `done` here on purpose: it is not a dev-ticket ticket.
- **Metrics:** after the AI review `dt event {N} review round=1 verdict=PASS high=0 medium=0 low=2 security=0 unverified=0` (as dev-ticket 7e);
  the scripts log their own events (`review_fetch`, `review_compare`).

## 0 · Start

0. `dt config-check`. `CONFIG_PROBLEM` → show the problems and stop. If it names `profile_notes`, read that file now (e.g. `skills/dev-ticket/PROFILE_LAUNCHPAD.md`), and `repo_notes` too when set: it overrides this skill wherever they differ (for Launchpad: no R6 deploy).
1. `dt role` (the `reviewers` / `developers` config lists decide it; `ROLE_FIXED_BY_LIST` / `ROLE_CONFLICT` → the lead fixes the config). `ROLE_UNSET` (on neither
   list) → ask once, never silence-defaulted: "Are you a developer or a reviewer?", then `dt role --set developer|reviewer --yes` (a reviewer must be on `reviewers`).
   **developer** → stop: reviewing and deploying a developer's ticket is for reviewers; offer `/dev-ticket`. The login must also be in `reviewers`
   (`dev_ticket.config.json`): `dt review-pick` checks it; `NOT_A_REVIEWER` / `REVIEWERS_NOT_CONFIGURED` → tell the developer, the lead fixes the config.
2. Arguments:
   - **empty** → R1, list or pick.
   - **`list`** → `dt review-pick`, show the result, **stop** (nothing is fetched). Give the next-ticket hint (R7).
   - **a ticket number N** → if `DEV_TICKETS\N\state.json` exists with `kind=review`, resume by `review.stage`: `R2-fetch` → R3, `R3-check` → R4,
     `R4-review` → R5, `R6-deploy` → approved: R6, `done` → a finished or sent-back review (a new round starts only when the author has handed
     off again: `dt review-pick --ticket N`, then R2). Another kind in that state is the developer's own work on N: say so, use `/dev-ticket N`.
     No state → `dt review-pick --ticket N`.
3. **One review at a time.** Another `DEV_TICKETS\*\state.json` has `kind=review` and `review.stage` ≠ `done`, and you are on its `FB_` branch:
   ask "You are reviewing #M. Park it and start #N?" (its branch is replaced; it stays resumable).

## R1 · Pick

`dt review-pick [--ticket N]` (read-only, no Assembla write). Show a table: ticket, summary, author, risk, round, head, DB ticket; warn on
`authentic=false`, `ambiguous=true` (two people handed off since the latest REJECT or QUALITY-BOUNCE) or `story=false`.

| RESULT | Action |
|---|---|
| `PICKED` | One-screen summary (author, tier, hand-off age, DB ticket) and "Review #N? (go)", then R2. |
| `MULTIPLE` | The reviewer picks; recommend `suggested` (lowest number: the staging job deploys lowest first). |
| `NONE` (R-S1) | Nothing is assigned to them with a hand-off. Show `not_handed_off` ("ask the author to run `dt handoff`") and `assigned_to_others` **for information only** (a developer may have used the wrong login); take nothing over. |
| `NOT_ASSIGNED` `WRONG_STATUS` `WRONG_MILESTONE` `NO_HANDOFF` (`--ticket`) | Say which; stop. |
| `NOT_A_REVIEWER` `REVIEWERS_NOT_CONFIGURED` `NO_CURRENT_MILESTONE` | Report; stop. |

## R2 · Fetch (takes the ticket over; the first git action)

**Before fetching, bring the branch up to date (the reviewer does this, outside the review checkout):** the toolkit (scripts and skills) lives in
the repo, so checking out an old `FB_{N}` replaces it with the branch's older copy. Merge `master` into `FB_{N}` and push it to origin first
(`git switch FB_{N}`, then **`git pull --ff-only origin FB_{N}`** so a local `FB_{N}` left over from an earlier review gets the author's latest commits first,
then `git merge master`, `git push origin FB_{N}`, then back to your own branch). If the pull is not a fast-forward the local branch has commits origin lacks: stop and tell the reviewer (never reset it). Such a merge does not stale the hand-off
(R2 table), so no new hand-off is needed. Skip it only when the branch already contains master's latest toolkit.

`dt review-fetch {N}`: needs a clean tree, checks out the developer's branch (and the DB branch) read-only, writes the review state. Then **read**
`DEV_TICKETS\{N}\ticket.md`, `hand-off\summary.md`, `hand-off\handoff.md`, `hand-off\author-review.md` (the developer's own pre-review: a second
opinion, never the verdict) and **every saved image** in `attachments\`.

| RESULT | Action |
|---|---|
| `FETCHED` | Continue at R3. Tell the reviewer the author, tier, head and the files saved. |
| `DIRTY` (R-S3) | List the files. The reviewer commits, stashes or pauses their own work (never done for them), then re-run. |
| `STALE_HANDOFF` (R-S4) | The branch moved after the hand-off **by a code file**: ask the author to run `dt handoff` again. Stop. Moves after the hand-off that are not the author's code do **not** stale it: commits that change only documentation (`*.md`, `*.txt`, anything under a `docs/` folder), and a merge of master into the branch (the branch's own non-documentation diff against master is identical to the handed-off one): `review-fetch` continues on the new head, records `review.docs_only_delta` and **keeps** the recorded gates, risk and functional results (`review-compare` still rejects them as `*_STALE` if the files differ; the files in `reviews\` are never deleted); tell the reviewer. Any other fetch clears those records. |
| `HANDOFF_AMBIGUOUS` `HANDOFF_AUTHOR_MISMATCH` `SELF_REVIEW` (R-S7) | Report; stop. Never choose a hand-off for the reviewer. `HANDOFF_AMBIGUOUS` means two people handed off in the current round; hand-offs before the latest REJECT verdict or QUALITY-BOUNCE are superseded (e.g. a takeover) and ignored, so they never need deleting. `SELF_REVIEW` appears only where the profile has no `allow_self_review`; with it, a reviewer reviews their own hand-off with every step unchanged (no step is skipped because the author is the reviewer). |
| `LOCAL_BRANCH_DIFFERS` | Their local `FB_{N}` has commits origin lacks: they sort it out; never reset it. |
| `HAS_AUTHOR_STATE` | The folder is the developer's own work: use `/dev-ticket N`. (With `allow_self_review`, the reviewer's **own** handed-off folder is not refused: `FETCHED` with `self_review` and `author_folder_parked`; say the work waits in `DEV_TICKETS\{N}.author` and `dt rework` puts it back after a reject. `AUTHOR_FOLDER_PARKED`: an older parked folder exists; report, never delete it.) A folder holding only a bare `dt fetch` snapshot (`ticket.md`, `ticket.json`, `attachments\`, a fresh `state.json` at stage `2-check` with a single `1-pick` entry) is not author work: `review-fetch` takes it over. |
| `BRANCH_NOT_ON_ORIGIN` `DB_BRANCH_MISSING` `FETCH_FAILED` `CHECKOUT_FAILED` `DB_REPO_MISSING` | Report the cause; stop. |

## R3 · Check (reproduce, never trust)

In order; fix nothing on the developer's code. A failure is a finding for the reviewer and can be the reason to reject (R5).

1. `dt deps {N} --files <the diff's files>` (`git diff --name-only origin/master...HEAD`; there is no author PLAN.md to read them from). Angular apps
   only; dev-ticket S13 applies: `DEPS_CONFLICT` → ask about `--legacy-peer-deps`.
2. `python {PY}/local_gates.py --ticket {N}` (R-S5). In a review the pre-commit fixers (whitespace, end of file, line endings) may rewrite files: the script restores them from HEAD afterwards and lists them in `restored_after_gates`, so `TREE_MODIFIED` is not raised by the gates themselves. `FAIL` → show the failures, never fix them; `INCOMPLETE` → install the tool or run without
   `--skip`; `STOP` from `db-checks` → `skills/dev-ticket/DB.md`.
3. `python {PY}/risk_check.py --ticket {N}` (never with `--files`). A tier above the developer's is a warning, shown by compare.
4. **Functional checks, always, every tier.** Derive them from the acceptance criteria in `ticket.md` and `hand-off\summary.md`; write
   `DEV_TICKETS\{N}\PLAN.md` holding **only** a `## Functional checks` section (`- [ ] FC-1 (AC 2): <app + screen> · <exact clicks> → expected:
   <observable result>`; every AC covered, backend changes via the Angular app that calls them). Settle it with the reviewer grill-me style (one
   question at a time, your recommendation first). Then `dt func-mode {N} run` (a skip needs the reviewer's one-line reason and explicit yes:
   `dt func-mode {N} skip --reason "…" --confirmed`), `dt profile {N}` (`needs_choice` → ask the reviewer, per `skills/dev-ticket/UI_CHECKS.md` §1), run them as that file describes (fresh sub-agent,
   written steps only), `dt func-record {N} --passed X --failed Y`. `func-record` puts the patched run files back by itself (§1), so none
   follows the reviewer to the next branch; `review-compare` also restores any still patched, and refuses only if a snapshot is missing.
5. **`dt review-compare {N}`** (`--early` before step 4 gives early feedback and never advances). `COMPARE_OK` → R4 (sets `review.stage=R3-check`
   and the tier of record). `COMPARE_MISMATCH` → show `blocking` and `warnings`:

| Code | Meaning / action |
|---|---|
| `STALE_HANDOFF` `HEAD_DIFFERS` (R-S4); `BEHIND_MASTER` with conflicts | The review is not of the handed-off, up-to-date code. Stop: the author re-syncs (`dt sync-branch`) and runs `dt handoff` again; then `dt review-fetch {N}` again. |
| `BEHIND_MASTER` without conflicts (warning) | Master moved but merges cleanly (the staging job merges `FB_{N}` into master): continue, tell the reviewer how far behind. |
| `TREE_MODIFIED` | Something was edited in the checkout. Show the files; the reviewer restores them (never edit the developer's code). |
| `GATES_*` `RISK_*` `FUNCTIONAL_*` | Your own run is missing, failing or stale: run it again (the developer's result is not used). |
| `DOCS_ONLY_DELTA` (warning) | The head moved after the hand-off, by documentation files only or a merge of master: the hand-off still holds, no new hand-off. Mention it; read the doc diff (`git diff <handoff_head> HEAD`) yourself. |
| `RISK_HIGHER` (warning) | The tier of record is now the higher one: tell the reviewer why (reasons are in the result). |
| `FILES_DIFFER` (warning) | The gated files differ from the developer's list: show both sides; usually master moved. |

## R4 · Review (an aid; the reviewer decides)

**One AI review per code state.** Do not run another sub-agent round for a head that differs from the reviewed one only by documentation files
(`review.docs_only_delta`, or a docs-only push or merge of master after the AI review): the existing `reviews\code-review.md` stands; the reviewer just reads the doc diff.
A new round is for a code change (a new hand-off).

Run `dt review-brief {N} --reviewer` (`--round 2` for a later round): it fills in `skills/dev-ticket/REVIEW.md` (N, summary, tier = `state.review.tier`,
WF files = `state.gates.files`, DB files = `state.db.files`, db-checks output), **appends the block below** and writes `reviews\REVIEW_BRIEF[-roundN].md`.
Spawn a **fresh sub-agent**, the most capable model, whose whole prompt is "Read `<that path>` and follow it" (never paste the text). The block it appends:

> **Inputs override (a reviewer's review of a developer's ticket).** There is no BRIEF.md or PLAN.md from the author. Read instead:
> `DEV_TICKETS\{N}\ticket.md`; `hand-off\summary.md`; `hand-off\handoff.md` (the author's evidence, not authority); `hand-off\author-review.md`
> (the author's own pre-review: a second opinion, do not rely on it); `PLAN.md` (the reviewer's functional checks only) and
> `reviews\functional-checks.md` if present; `attachments\`. The WF diff is `git diff origin/master...HEAD`; the checkout is clean, expect no
> uncommitted changes. Derive the acceptance criteria from the ticket and the summary and list them in your coverage table. Where REVIEW.md names
> BRIEF.md: take the README impact from the HANDOFF (`readme_impact`) and, for a defect, grep the root-cause pattern yourself (no sweep record
> exists). You are not the reviewer of record; a human reviewer decides. Write only `reviews\code-review.md`; change no other file and run no git write.

Then `dt review-check {N}` (a later round: `--file …\code-review-round2.md`). `REVIEW_CHECK_UNVERIFIED` → open each cited line yourself; correct a
finding that holds; for one that does not, show the reviewer the finding and the failed check and, only on their yes, move it under `## Dismissed`.
Never drop a finding silently. Log the round (`dt event … review …`), then `dt state {N} --set review.stage=R4-review`, and give the reviewer one
screen: the AI verdict, the findings by severity with `file:line`, the compare and functional-check results. **The reviewer reads the diff
themselves; the verdict is theirs.**

## R5 · Verdict (the reviewer decides; the first step that writes to Assembla)

**The verdict is the reviewer's.** Recommend from the evidence (compare, functional checks, AI findings, the diff they read); never decide for them.
**Approve** needs `COMPARE_OK` and the AI review in hand (stage `R4-review`). **Reject** needs neither (a failing check is itself a reason) but at
least one finding.

1. **Findings, one at a time (grill-me).** For each open finding of `reviews\code-review.md`: keep, dismiss or reword? Your recommendation first. A
   dismissal moves it under `## Dismissed` with the reason, on the reviewer's yes only (as in R4). Then ask "Any finding of your own?". Write each
   decision down; nothing lives only in chat.
2. **Write `DEV_TICKETS\{N}\verdict.json`:**
   ```json
   {"verdict": "REJECT", "summary": "one line",
    "findings": [{"severity": "High|Medium|Low", "file_line": "path:42 or general", "finding": "one line", "fix": "one line"}],
    "fix_list": ["optional; derived from the findings when omitted"], "notes": ["optional"], "override_reason": "approve only"}
   ```
   Every text is **one line** of plain words: no angle brackets, backticks, `**`, local paths or TODO/TBD (Assembla markup). A reject needs at least
   one finding, each with a fix. An approval carries only Low findings; if the AI review still has an open High or Medium finding it needs an
   `override_reason`: **ask the reviewer for it, never invent it.**
3. **Preview:** `dt review-verdict {N} --reject|--approve --file …\verdict.json` shows the exact comment and the actions.
4. **One question listing exactly those actions** (the preview's `actions`): "Post this verdict and <move>? (yes)". No silence default. On the yes,
   the same command with `--yes`. Stop at the first failure.

| RESULT | Action |
|---|---|
| `REJECTED` | Done: the ticket is in Dev Queue, assigned back to the author, `review.stage=done`. Tell the reviewer the developer takes it up with `/dev-ticket N`. If other tickets wait for review, suggest the next one with the **next-ticket hint** (R7). A **second round** starts when the author hands off again: it shows in R1, then R2 (re-fetch clears the old evidence) to R5; the verdict comment is **edited in place**. |
| `APPROVED` | The ticket is in Dev Done, **still assigned to the reviewer**, `review.stage=R6-deploy`, `review.approved_head` is the reviewed head (any later push voids it). Deploying is R6, a separate yes: ask whether to deploy now. Give the next-ticket hint (R7) only after the deploy question is settled. |
| `VERDICT_INVALID` `VERDICT_MISMATCH` (R-S12) | Fix `verdict.json` as the `errors` say, preview again. Never post a hand-edited comment instead. |
| `STALE_REVIEW` (R-S9) | The author pushed after the fetch: `dt review-fetch {N}` again, redo R3 and R4 for that head. |
| `COMPARE_REQUIRED` `AI_REVIEW_REQUIRED` (R-S10) | Finish R3 / R4 first, or reject instead (a reject needs neither). |
| `MOVE_FAILED` (R-S11) | The comment **is** posted; the move failed. Report the error, check the ticket on Assembla, **never retry on your own**. A later `--yes` edits the same comment and tries the move once more, only on the reviewer's say-so. |
| `NOT_ASSIGNED` `WRONG_STATUS` `SELF_REVIEW` `NOT_A_REVIEW_FOLDER` `NOT_A_REVIEWER` `AUTHOR_NOT_FOUND` `NOT_ALLOWED` | Report; stop. |

## R6 · Deploy (a separate yes; the highest-impact step)

Only for an approved ticket (`review.stage=R6-deploy`). **Never use the author path** (`dt status … --requested`, `staging_deploy.py --requested`,
the LOW path): they refuse a review folder (`REVIEW_FOLDER`, `USE_REVIEWER_MODE`). Everything below is bound to the **approved head**: if the author
pushed after the approval, nothing moves and nothing is triggered.

1. **Ask:** "Deploy #{N} (approved head {sha}) to staging now?" Say it merges `FB_{N}` into master and builds, takes 45+ minutes, and that a DB ticket
   is a second run. No silence default. "Not now" → stop: the ticket stays in Dev Done and `/review-and-deploy N` resumes here.
2. `python {PY}/staging_deploy.py check` (read-only): the reviewer's `JENKINS_REMOTE_*` keys must work (`MISSING`, `BAD_TOKEN_FORMAT`, `HTTP_ERROR` →
   SETUP.md §2; stop).
3. `dt review-deploy {N}` (preview), then on the yes `dt review-deploy {N} --yes`: moves the ticket from Dev Done to DevOps Staging (the DB ticket
   follows), still assigned to the reviewer. It does **not** trigger the job. `APPROVAL_STALE` (R-S13) → stop.
4. `python {PY}/staging_deploy.py queue {N}` must be `FIRST` (Plan level = Story). `QUEUED` → report `ahead` and stop (`dt state {N} --set deploy=queued`;
   `/review-and-deploy N` resumes: queue again, deploy when first). `NOT_IN_QUEUE` → check the ticket's status.
5. Show the summary (ticket, tier of record, queue, approved head, DB ticket) and wait for the yes. Then run
   `python {PY}/staging_deploy.py deploy {N} --reviewer --yes` **in the background** (45+ minutes) and **wait for the completion notice; don't poll**.
   Never re-trigger on your own (`ALREADY_TRIGGERED`). Only if the reviewer says the earlier run failed and asks for a retry:
   `deploy {N} --reviewer --retry --yes` (the code stays the approved head: never sync or merge here).

| RESULT of `deploy --reviewer` | Action |
|---|---|
| `DEPLOYED` | #N is in QA Queue. With a DB ticket go to the DB step below, else R7. |
| `DEPLOYED_NO_APPS` | The run succeeded and the branch was merged, but no app was built (empty build list): normal when the merge brought only folders without a build job (`apps_with_build_job`). Say so; R7. |
| `QUEUED` `BUSY` | Not triggered (a lower-numbered ticket is first, or another run is going). `dt state {N} --set deploy=queued` or `busy`, report, stop. |
| `NOT_A_REVIEW_FOLDER` `NOT_A_REVIEWER` `NOT_APPROVED` `SELF_REVIEW` `NOT_REQUESTED` `NOT_ASSIGNED` `WRONG_STATUS` `NOT_STORY` `BRANCH_NOT_ON_ORIGIN` `ORIGIN_DIFFERS` `NOT_IN_QUEUE` `NO_CURRENT_MILESTONE` | A guard failed, not triggered. Report and stop. `NOT_REQUESTED` → step 3; `ORIGIN_DIFFERS` → the local checkout is not the origin head: tell the reviewer, never "fix" it by resetting. |
| `APPROVAL_STALE` (R-S13) | The author pushed after the approval: not triggered. Offer `dt review-withdraw {N}`, then review again. |
| `DEPLOY_FAILED` `DEPLOYED_OTHER_TICKET` `DEPLOYED_STATUS_UNCONFIRMED` `ALREADY_TRIGGERED` `ERROR` with a `build` | Report the build, escalations and status. **Never re-trigger.** `DEPLOYED_STATUS_UNCONFIRMED` → `staging_deploy.py confirm {N}` (read-only). |
| `ERROR` without a `build` | Jenkins credentials or network: report, point to SETUP.md §2; the ticket stays in DevOps Staging. |

**The DB ticket (D = `state.db.ticket`) is deployed in its own second run, after N** (the job takes one ticket per run, lowest number first). When N is
`DEPLOYED` and D is in DevOps Staging (it moved with N): show D's status, ask, and on the yes run `staging_deploy.py queue {D}` (must be `FIRST`), then
`python {PY}/staging_deploy.py deploy {D} --reviewer --yes` **in the background**. Same guards, read for the DB ticket: the owner's approval,
`approved_db_head` in the **DB repo's** origin, one trigger per ticket. `DEPLOYED_NO_APPS` is the normal result (the scripts reached the database, no
app is built). The DB scripts are on staging only after this run: tell the reviewer.

**Withdraw an approval** (not deployed yet): `dt review-withdraw {N}` (preview, then `--yes` on the reviewer's yes) moves the ticket back to Code Review
(still theirs) and clears the approval and the deploy request. Refused once a deploy was triggered (`ALREADY_DEPLOYED`). The APPROVED comment stays
until the next verdict edits it. Then `dt review-fetch {N}` if the author pushed, or decide again with `dt review-verdict`.

## R7 · Done

After `DEPLOYED` / `DEPLOYED_NO_APPS` (and the DB ticket's run, if any): `dt state {N} --set review.stage=done` and `dt state {N} --set deploy=deployed`.
Report the verdict comment, the heads, the statuses, the build and what the reviewer still has to do (QA test cases are not created by this
workflow). Nothing is committed or pushed here. **Back to master:** when the review ends (R5 `REJECTED`, R6 "not now", R7) the checkout is still on
`FB_{N}`: offer `git switch master` (explicit yes first; the tree is clean in a review, so a dirty one is reported, never forced).
**Next-ticket hint.** Whenever this skill suggests another ticket to review (R5 `REJECTED` / `APPROVED`, R7, or `review-pick` showing others
waiting), tell the reviewer to type `/clear` and then `/review-and-deploy <N>`. The review state is on disk (`DEV_TICKETS\N\state.json`), so a fresh
context loses nothing and the last ticket's diff and findings are not carried into the next review.

## Stop-and-ask rules

Stop, show the facts, ask, wait. Never work around a rule. The action for each is in the step named.

| Rule | Trigger | Step |
|---|---|---|
| R-S1 | `review-pick` → `NONE` | R1 |
| R-S2 | `review-pick` → `MULTIPLE` | R1 |
| R-S3 | `review-fetch` → `DIRTY` | R2 |
| R-S4 | `STALE_HANDOFF`, `BEHIND_MASTER`, `HEAD_DIFFERS` | R2, R3 |
| R-S5 | gates `FAIL` / `INCOMPLETE` | R3 |
| R-S6 | `review-check` → `REVIEW_CHECK_UNVERIFIED` | R4 |
| R-S7 | `HANDOFF_AMBIGUOUS`, `HANDOFF_AUTHOR_MISMATCH`, `SELF_REVIEW` | R2 |
| R-S8 | `NOT_A_REVIEWER`, `REVIEWERS_NOT_CONFIGURED` (the lead fixes `reviewers`, or the role is wrong: `dt role`) | 0 |
| R-S9 | `STALE_REVIEW` | R5 |
| R-S10 | `COMPARE_REQUIRED`, `AI_REVIEW_REQUIRED` | R5 |
| R-S11 | `MOVE_FAILED` (comment posted, move not: never retry unprompted) | R5 |
| R-S12 | `VERDICT_INVALID`, `VERDICT_MISMATCH` | R5 |
| R-S13 | `APPROVAL_STALE` (not moved, not triggered: `dt review-withdraw`, then review again) | R6 |
| R-S14 | any deploy result other than `DEPLOYED` / `DEPLOYED_NO_APPS` (never re-trigger on your own; `--retry` only on the reviewer's explicit say-so) | R6 |
| R-S15 | `QUEUED`, `BUSY` (record `deploy=queued|busy`, stop, resume with the queue check) | R6 |
