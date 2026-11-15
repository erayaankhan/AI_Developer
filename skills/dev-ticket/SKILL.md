---
name: dev-ticket
description: Takes the developer's single Assembla ticket in "Dev In Process" (in the _CURRENT milestone) from pick-up to Code Review, or for a reviewer's LOW-risk tickets all the way to staging (a developer hands the ticket to a reviewer at Code Review). Covers pick, ticket-detail check, staging-attribute fixes, FB_ branch, brief, plan, test-first build, local gates, risk re-check, UI checks in Chrome, an independent code review, and the hand-off. Stops and asks whenever a rule is not met; supports pause/resume. Use when the developer says "/dev-ticket", "start my ticket", "work on my Dev In Process ticket", "/dev-ticket 18474" or "/dev-ticket pause". Talks to the Assembla API directly and uses plain git; Jenkins is only used to trigger the staging deploy on the LOW path.
---

# dev-ticket

```
0 START → 1 PICK → 2 CHECK → 3 BRANCH → 4 UNDERSTAND → 5 PLAN → 6 BUILD → 7 VERIFY → 8 HAND OFF
```
Design rationale lives in the maintainer's `AI_DEV_ENV/PLAN.md`, if present; it isn't needed to run.

**Read these only when needed:**

| File (same folder) | Read when |
|---|---|
| `DB.md` | the approach needs SP / function / DDL / DML changes (step 4) |
| `UI_CHECKS.md` | step 7d, when the functional-checks mode is `run` |
| `LOW_PATH.md` | hand-off with `path=LOW` (step 8), or resuming with `state.deploy` set |
| `PAUSE.md` | `/dev-ticket pause`, or resuming a paused / reassigned ticket |
| `REVIEW.md` | step 7e: give it to the review sub-agent verbatim |
| `SUMMARY_FORMAT.md` | step 8, writing `SUMMARY.md` (also LOW path and any later edit of the posted summary) |
| `HANDOFF.md` | step 8 when `state.role = developer`; `dt rework` found a rejection (step 0) |
| `STANDARD_PATH.md` | step 8 as a reviewer on the STANDARD path; the developer says "deploy {N}" |

## Conventions

- **Shorthand:** `{ROOT}` = repo root (`git rev-parse --show-toplevel`); `{PY}` = `{ROOT}/AI_DEV_ENV/python_scripts`;
  **`dt`** = `python {PY}/dev_ticket.py`. Every script ends with one `RESULT: {json}` line: act on that line only.
  **Use forward slashes in any path passed to `python`** (`D:/…`): in Bash a backslash path loses its backslashes and the call fails.
  Use PowerShell for everything else on Windows.
- **`--yes` needs `--confirm {H}` on `dt comment`, `dt handoff`, `dt status`, `dt followup-create`, `dt db-create`.** Run the command without `--yes` first: the PREVIEW returns `confirm`, a short hash of exactly what it would do. Show the developer that preview, and only on their yes re-run with `--yes --confirm <that hash>` (`{H}` in this skill). `CONFIRM_REQUIRED` = you skipped the preview; `PREVIEW_CHANGED` = the file, title, status or code changed since: preview again, show it, confirm that one. The hash proves the action equals the preview; it is not the developer's consent, so never take it from a preview they did not see. Several commands on one yes (hand-off step f): preview each first, show them together, then apply each with its own hash.
- **Fewer permission prompts:** use a command's own tools to get information (Read/Glob/Grep, `dt`) instead of asking.
  Run **one simple command per Bash/PowerShell call** (no `cd`, pipes or `cat` chains: they trigger the path prompt even
  with allow rules). Call scripts as `python {PY}/<script>.py`. If a harmless command still prompts, add a narrow pattern
  to `.claude/settings.local.json`; never allow-list a destructive or outward command (commit, force-push, `rm -rf`, deploy).
- **Local env file** `{ROOT}\AI_DEV_ENV\local_env.env` (tracked in git, per developer: same rule as `credentials.env`: **never open or print
  it**, never ask for values in chat): the developer's staging, preprod and local URLs, the test-DB allow-list
  (host and database name) and the service start commands, never a connection string or password. Their remembered role
  (`DEV_ROLE`) is kept in the git-ignored `credentials.env`, not here. Use only `dt env-check`, `dt env-apply`, `dt service-start`, `dt role`; they report key names, booleans and
  the role word, never values. The test DB connection string lives only in the git-ignored `appsettings.local.json` overlay next to
  each service (`UI_CHECKS.md` §1); `dt env-apply` patches it into `appsettings.json` for the run and the patched file is restored
  afterwards. A tracked config file must never keep the test connection string.
- **Folder scope:** work freely inside `{ROOT}`. Outside it, touch only what the skill itself names: the DB repo
  `C2M_CI_DB` and the host repo `C2M_Console_Angular_Beta` (paths in `dev_ticket.config.json`, reached via `dt`/`git -C`),
  `D:\ProjectsGit\C2M_CommonServices\C2M_API` (the `C2M_APIUrl` service: run it with `dt service-start {N} c2mapi`; never edit or stage it),
  and the session scratchpad. Anything else outside `{ROOT}`: ask first.
- **Questions:** ask and wait for the answer. There is no timed default: silence is never a yes, and no answer is ever assumed
  (recommend an answer first, then wait).
- **Session-end improvements (Saleem Khan only):** the ticket's Assignee in `ticket.md` is `saleemk` for him. Only then,
  at the end of each session, append new numbered one-line improvement points for this skill or workflow to
  `DEV_TICKETS\IMPROVEMENTS.md` (tag date and ticket, continue from the highest number ever used). For any other developer, skip.
- **Runs in** Claude Desktop with Chrome control; from a plain terminal, functional checks fall back to the developer.
- **Credentials** live in `{ROOT}\AI_DEV_ENV\credentials.env`. **Never open it**, never ask for secrets in chat.
  Credential `ERROR` → run `dt creds` (shows file/env/missing only) and point to `SETUP.md` §2.
- **Work folder:** `{ROOT}\DEV_TICKETS\{N}\` (git-ignored): `ticket.md/json`, `state.json`, `metrics.jsonl`,
  `questions.md`, `BRIEF.md`, `PLAN.md`, `reviews\`, `gates\`, `SUMMARY.md`.
- **`state.json` is written only via `dt state {N} --set k=v …`**. Keys: `stage`, `risk`, `path`, `deploy`,
  `check.verdict`, `db.ticket`, `db.files`, `db.base`, `db.destructive_confirmed`, `baseline.accepted`, `role`. Written by their own commands (not `--set`):
  `profile` (`dt profile`), `functional_mode` / `functional_skip` (`dt func-mode`), `functional` (`dt func-record`),
  `services` (`dt service-start`), `handoff` (`dt handoff`, `dt assign`).
- **Role (developer / reviewer):** the person running the skill has a role (`dt role`: decided by the `reviewers` / `developers` config lists, else asked once); `fetch` copies it
  into `state.role`. A **reviewer** works the ticket end to end as described below (hands off, and on LOW or on request deploys) and
  also reviews and deploys a developer's ticket (`/review-and-deploy`). A **developer** hands every ticket (all tiers) to a reviewer
  at step 8 (`HANDOFF.md`) and never deploys: the reviewer approves and deploys. **Elsewhere in this skill "the developer"
  means whoever is running it, in either role**; the role named `developer` is the one that cannot deploy. The role picks the path;
  it is not a security boundary (a developer-role user has no Jenkins credentials, and `NOT_A_REVIEWER` says so first). The role
  says who runs the workflow; a folder's **`kind`** says what the folder is: `state.kind = review` marks a reviewer's review of
  someone else's ticket (a reviewer's own tickets have no `kind`). Design: `AI_DEV_ENV\REVIEW_TICKET_DESIGN.md`.
  Refusals (`RISK_DOWN_REFUSED`, `LOW_PATH_REFUSED`) are final: tell the developer.
  Stages: `1-pick 2-check 3-branch 4-understand 5-plan 6-build 7-verify 8-handoff done` (+ `paused`).
  Set the next stage at the end of every step.
- **One summary comment, edited in place** (shape: `SUMMARY_FORMAT.md`): QA reads the ticket's single SUMMARY comment. Post it once (`dt comment` prints its `comment_id`); every later change (corrections, late fixes) goes into the same comment with `dt comment {N} --file …\SUMMARY.md --edit-id <comment_id> --yes --confirm {H}`. Never post a separate correction comment. Comment and hand-over files must live in `DEV_TICKETS\{N}\` (`FILE_OUTSIDE_TICKET_FOLDER` otherwise), and `--edit-id` only edits your own comment on that ticket (`NOT_YOUR_COMMENT`, `COMMENT_NOT_FOUND`).
- **Every developer answer goes into BRIEF.md `## Assumptions / Decisions` immediately**: nothing may live only in chat.
- **Consent:** no commit, push, status change or Assembla comment without an explicit yes in this conversation.
  Exceptions: `branch --push` / `db-branch --push` after their own yes, the single "go" of `LOW_PATH.md`, and `dt sync-branch N --yes`
  (pull origin/FB_N, push, pull origin/master into it, push; the developer's standing instruction for resume and hand-off, no extra ask).
  Claude commits and pushes (never force, never `--no-verify`, never amend) only after the developer's yes in chat, and states the branch
  and remote first (`git branch --show-current`). Stage only the ticket's files, one by one.
- **Metrics:** scripts log their own events. Log the rest with `dt event {N} <type> k=v`:

  | When | Event |
  |---|---|
  | a stop rule fires | `stop rule=S6 reason="…"` |
  | each 7e round | `review round=1 verdict=PASS high=0 medium=0 low=2 security=0 unverified=0` |
  | functional checks done | `ui_checks where=local passed=3 failed=0` |
  | LOW → STANDARD | automatic: `dt state {N} --set path=STANDARD` logs `fallback`. Always make the switch that way; never log it by hand |
  | a fix attempt (6 / 7a / 7d / 7e), logged **before** trying (the 7a unit-test rounds are logged by `dt gate-round`, never by hand; 7e = fixing a review finding. The review itself is a `review` event, never a `fix_round`) | `fix_round step=7a attempt=1` |
  | questions asked (step 4), when the round is over | `questions count=3 step=4` (count = how many you asked) |

  **Attempt caps** (S9) are counted from the `fix_round` events in `metrics.jsonl` for that step, never from
  memory of the chat, so they survive a context compaction or a resume.

  The developer's own report: `python {PY}/metrics_report.py` → `DEV_TICKETS\METRICS-<date>.md`.
- **Next-ticket hint.** Whenever this skill suggests picking up another ticket (after a hand-off, a finished ticket or a pause), tell the
  developer to type `/clear` and then `/dev-ticket <N>`. The ticket state is on disk (`DEV_TICKETS\N\state.json`), so a fresh context loses
  nothing and the last ticket's diff and decisions are not carried into the next one.
- **Back to master.** When the work ends (hand-off done, ticket finished or paused) and the tree is clean and the branch pushed, offer
  `git switch master` (explicit yes first; never with uncommitted or unpushed work: report it instead). Say so before the `/clear` hint.

## Stop-and-ask rules

Stop, show the facts, ask, wait. Never work around a rule.

| Rule | Trigger | Ask / do |
|---|---|---|
| S1 | `pick` → `NONE` | Show `dev_queue`; developer moves one to Dev In Process; re-run `pick`. |
| S2 | `pick` → `MULTIPLE` | Only one ticket may be in Dev In Process: which one? (others back to Dev Queue). Continue with `pick --ticket N`. |
| S3 | `NOT_ASSIGNED` | Reassign it to yourself in Assembla, or pick another? |
| S4 | `WRONG_STATUS` / `NOT_FOUND` | Move it to Dev In Process, or name another ticket. Special cases: S4d, S4e. |
| S4a | `NO_CURRENT_MILESTONE` | No `_CURRENT` milestone: the lead must mark one. Stop. |
| S4b | `WRONG_MILESTONE` | Not in a `_CURRENT` milestone: move it or pick another? |
| S4c | `NONE` with `in_process_outside_current` | Also list those: in Dev In Process, but not in a `_CURRENT` milestone, so not pickable. |
| S4d | status = Missing Implementation Details | Redo step 2. READY → "Move #N back to Dev In Process?" → `dt status {N} --to "Dev In Process" --yes --confirm {H}` → `pick --ticket N`. |
| S4e | status = Dev Queue and paused / hand-over exists | See `PAUSE.md` → Resume. |
| S5 | `PICKED` with `others_in_process` | "You also have #X in Dev In Process. Continue with #N?" |
| S6 | step 2 → NEEDS_INFO | See step 2. |
| S6a | attribute warnings / `ATTRIBUTES_MISSING` | Fix them (step 2 "Attributes"). Every tier. |
| S6b | Epic needed | Show candidates. If the top-ranked Epic clearly leads (re-rank by functional area first; it must still be the top after that), name it as your recommendation and ask the developer to confirm. Otherwise **the developer picks**. |
| S6c–e | DB ticket / DB branch problems: several related DB tickets, a dirty DB tree, an unclear attribute when creating it | See `DB.md`. |
| S7 | `branch` → `DIRTY` | The tree must be clean to start (tracked changes **and** untracked files; git-ignored files don't count), whether `branch` switches or you are already on `FB_{N}`. List the files: the developer commits (elsewhere), stashes (`git stash -u`) or deletes them, then re-run. Never do it yourself. |
| S8 | merge conflicts with master | List files: resolve now, or at hand-off? |
| S9 | a fix loop is out of attempts | Show what was tried and what still fails; ask how to proceed. |
| S10 | a decision the ticket, code and BRIEF can't answer | Check the READMEs and code first, then ask that one question grill-me style (your recommended answer, agree or pick). |
| S11 | `baseline` → `BASELINE_FAIL` (new branch) | Tests fail on the untouched start. For each failure group read the test and the code under test, show the developer **stale test or code problem** with the evidence, and let them decide. Only on their explicit yes: `dt state {N} --set baseline.accepted=true`. Never start step 6 first. |
| S12 | `status … --to "Code Review"` → `RCA_CLCA_MISSING` | A DEFECT main ticket (Category Defect / Bug, or verdict DEFECT; never a DB ticket) has an empty or placeholder (`-NA-`, `Pending`) RCA or CLCA in Assembla. Write them in `DEV_TICKETS\{N}\rca.md` / `clca.md` from BRIEF.md (root cause; corrective action, the test that prevents a repeat, and the sweep result: what else was fixed here and which hits are follow-ups), show them, and on the developer's yes `dt rca {N} --rca-file … --clca-file … --yes`. Keep the prose short: `dt rca` appends the closing line "Same flaw elsewhere: n hits (x fixed here, y follow-ups (#…))" to the CLCA itself (from BRIEF.md and the follow-ups raised; never type those numbers), shows the composed text (`clca-final.md`), and refuses over 1024 characters (`TOO_LONG`: trim the prose). `--plain` turns the line off. Then retry the move. |
| S13 | `deps` → `DEPS_CONFLICT` / `DEPS_ERROR` / `DEPS_DIRTY` | Show the output. `DEPS_CONFLICT` (peer conflict): ask whether to retry with `dt deps {N} --legacy-peer-deps` (changes node_modules only; nothing tracked). Never edit `package.json` to get past it. |
| S14 | CLCA sweep found other hits, or → `SWEEP_MISSING` | Ask: fix the other hits in this ticket, or list them as follow-ups (see step 4 "CLCA sweep"). `SWEEP_MISSING`: `## Same flaw elsewhere` is absent, empty, or a hit has no decision. |
| S15 | `NOT_A_REVIEWER` | A developer tried to deploy or move past Code Review. Say the reviewer approves and deploys; offer the developer hand-off. If `dt role` is wrong, the developer fixes it (`dt role --set … --yes`). Never work around it. |
| S16 | `dt assign` → `REVIEWERS_NOT_CONFIGURED` / `NOT_A_REVIEWER` / `REVIEWER_NOT_FOUND` / `SELF_ASSIGN` | Not configured or not found: the lead must add the reviewer's Assembla login to `reviewers`; stop. `NOT_A_REVIEWER` / `SELF_ASSIGN`: show `dt assign N --list` and let the developer pick again (`SELF_ASSIGN` only where the profile has no `allow_self_review`). `ASSIGNED` with `self_review` → tell them to run `/review-and-deploy N` next. |
| S17 | `dt handoff` / `dt assign` → `NO_HANDOFF` / `STALE_HANDOFF` or a gate (`NOT_PUSHED`, `NOT_COMMITTED`, `GATES_REQUIRED`, `GATES_STALE`, `FUNCTIONAL_*`, `QUALITY_REQUIRED`, `QUALITY_STALE`, `QUALITY_FAILED`, `SUMMARY_CHECK_FAILED`) | The evidence is missing or no longer matches the pushed code: fix the cause (commit, `dt sync-branch`, gates, functional checks, SUMMARY), then `dt handoff N --yes --confirm {H}` again (it edits the same comment). Never assign a ticket whose hand-off is stale. A commit after the hand-off that changes only documentation (`*.md`, `*.txt`, `docs/`) or merges master into the branch does **not** make it stale: `dt assign` goes ahead without a new `dt handoff`. |
| S18 | `dt rework` → `REVIEW_AUTHOR_MISMATCH` / `VERDICT_NOT_FOR_THIS_HANDOFF` / `NOT_HANDED_OFF` / `ALREADY_REOPENED` | The verdict is not from the reviewer the ticket was assigned to, or is about other code than the one handed off: show the facts, do not take it up, ask the reviewer. `ALREADY_REOPENED`: the rejection is already taken up, carry on at the build step. |

## 0 · Start / resume (before `pick`)

- **Team profile first:** run `dt config-check`. `CONFIG_PROBLEM` → show the problems and stop. If it names `profile_notes`,
  read that file now (e.g. `skills/dev-ticket/PROFILE_LAUNCHPAD.md`), and `repo_notes` too when set (e.g.
  `PROFILE_C2M_IMW2.md`, this repo's part of a profile that serves several repos): they override this skill wherever they
  differ.
- **Role first:** run `dt role`. **The config lists decide it:** a login on `reviewers` is a reviewer, one on `developers` a developer
  (`source: lists`; it also refreshes the remembered role). `ROLE_UNSET` (on neither list) → ask once, never silence-defaulted: "Are you a
  developer or a reviewer?", then `dt role --set developer|reviewer --yes` (preview first); a reviewer must be on `reviewers`
  (`NOT_ON_REVIEWERS` → the lead adds the login). `ROLE_FIXED_BY_LIST` / `ROLE_CONFLICT` → the role comes from, or the lists conflict in,
  `dev_ticket.config.json`: tell the developer, the lead fixes it. Never ask again once it is set. A resumed ticket keeps the `state.role` it started with.
- `/dev-ticket` (no argument) → **always a fresh pick**: go straight to step 1 (`dt pick`). Do not resume, and do
  not check the current branch or any `state.json` first. The developer resumes explicitly with `/dev-ticket N`.
- **Your own self-review** (`state.kind = review` with `review.self_review`, and `DEV_TICKETS\{N}.author` exists): you reviewed
  your own hand-off. Run `dt rework {N}` first, exactly as for a handed-off ticket below; on its `--yes` it puts your work back
  as `DEV_TICKETS\{N}` and keeps the review as `DEV_TICKETS\{N}.review-rK`. Never `dt takeover` it. If the review was not a
  reject (`NO_REJECTION`), the ticket is still under your review: use `/review-and-deploy N`.
- **A folder whose `state.kind` is `review` is a reviewer's review of a developer's ticket, not work to resume here.** `dt fetch`
  refuses it (`REVIEW_FOLDER_EXISTS`); never run the steps below on it. Say so and offer the two ways on, whoever authored the ticket:
  to **review** it, `/review-and-deploy N` (`AI_DEV_ENV\skills\review-and-deploy\SKILL.md`); to **fix it yourself** (the ticket came back to
  you for rework, or another developer's ticket was handed to you), ask "take it over?". Yes → `dt takeover {N}` (preview: where the
  review folder moves; an unfinished review is named), then `dt takeover {N} --yes` (moves it aside as `DEV_TICKETS\{N}-review-rK`, never
  deletes it), then `dt fetch {N}` for a fresh folder and continue at step 2. The same applies after `dt pick` when the picked ticket's
  folder is a review folder.
- **`/dev-ticket N` with a `state.json` that has `state.handoff` (a developer's handed-off ticket)** → run `dt rework {N}` first.
  `PREVIEW` → a reviewer sent it back: follow "Rework after a rejection" in `HANDOFF.md`. `NO_REJECTION` → nothing to
  rework: say where the ticket is (with whom, which status) and stop, unless it is in Dev In Process (then resume normally).
  Other results → S18.
- `/dev-ticket N` with `DEV_TICKETS\{N}\state.json` and `stage` ≠ `done` → `dt fetch {N}`, then resume at that stage
  **Before any code is written on a resume** (stage 3 or later, the branch exists): `dt branch {N}`, then `dt sync-branch {N} --yes`
  (see "Branch sync" below). `CONFLICTS` → S8: report the files, the developer resolves, re-run.
  (`paused` → `PAUSE.md`; `8-handoff` with `state.deploy` → `LOW_PATH.md`).
- `/dev-ticket N` with no local `state.json` → `dt restore {N}` first (`PAUSE.md`); nothing to restore →
  `dt pick --ticket N`.
- `/dev-ticket pause [N]` → `PAUSE.md`.

## 1 · Pick

`dt pick` (or `dt pick --ticket N`). `PICKED` → continue; anything else → its stop rule.

## 2 · Check

`dt fetch {N}` (type from `Category`; `TYPE_UNKNOWN` → decide CR vs DEFECT, ask if unclear, re-run with `--type`;
`USE_REVIEW_AND_DEPLOY` → the ticket is in Code Review, assigned to you as a reviewer: stop, use `/review-and-deploy {N}`).
Read the title and the whole description carefully before judging. `fetch` saves image attachments to
`DEV_TICKETS\{N}\attachments\` (`ticket.md` marks them "saved … (view it)"): **Read every saved image**, and open
each URL in `hints.urls_in_text` in Chrome when it is available (skip with a note if there is no Chrome or a login is
needed; never ask for secrets). A video can't be viewed: if it is the only repro, ask for a description. Use the
screenshots and URLs to identify the project, screen and feature, and write what you learned under `## Discovery` in
BRIEF.md. Judge `ticket.md` (hints are only signals):

| Needed | CR | DEFECT |
|---|---|---|
| Goal / problem clear | ✔ | ✔ |
| Current vs expected behaviour | – | ✔ |
| How to reproduce | – | ✔ |
| Testable acceptance criteria (stated or derivable) | ✔ | ✔ |
| Affected app / screen / service | ✔ | ✔ |
| UI: mock or precise description | if UI | if UI |
| DB / SP impact stated | if needed | if needed |

- **READY** → `dt state {N} --set check.verdict=READY`.
- **NEEDS_INFO** (S6) → write 1–5 *specific* questions to `questions.md`. Ask to post them
  (`dt comment {N} --file …\questions.md --yes --confirm {H}`), then ask to move the ticket
  (`dt status {N} --to "Missing Implementation Details" --yes --confirm {H}`). Stop.

**Attributes** (must be right before any branch): if `deploy_attribute_warnings` is non-empty:
1. `dt fix-attributes {N}` → `PREVIEW`, or `NEEDS_EPIC`.
2. `NEEDS_EPIC` → `dt epics {N}`. Re-rank by **functional area** if the score is off (one-line reason each);
   show up to 10 plus "or another number". The developer picks (S6b). Then `dt fix-attributes {N} --epic {E}`.
3. Show the preview; on yes re-run with `--yes` (verifies and comments). `FAILED` → stop.

## 3 · Branch

`dt branch {N}`: re-checks attributes (`ATTRIBUTES_MISSING` → S6a, git untouched), then creates or checks out
`FB_{N}` from `origin/master`, no commits. A **fresh start needs a clean working tree** (checked right after the attribute gate and before any git change, on or off the branch): from then on every change is this ticket's own. It stores `state.start_head` and `state.branch_origin` (`created` for a new branch from master, else `existing`). A resumed ticket (past step 3, or paused) is not re-checked. `DIRTY` → S7; `merge_conflicts` → S8. `pushed=false` → ask, then `--push`.

## 4 · Understand → `BRIEF.md`

Grep first, read only what you need; check the module `README.md` (the repo has no ADR folders).
Defects: find the root cause, ideally as a failing test. A defect also needs **RCA** (Root Cause Analysis) and **CLCA**
(Closed-Loop Corrective Action) filled in on the Assembla ticket before Code Review: keep the root cause and the corrective
action under `## Root cause / where` and `## Approach` so they can be written at hand-off (S12, `dt rca {N}` shows the current values).

```markdown
# {N} — {summary}
## Problem
## Root cause / where        (DEFECT: file:line · CR: where the change lives)
## Approach                  (the change, and what you won't touch)
## Acceptance criteria        (numbered, testable)
## Risk: LOW | MEDIUM | HIGH — reason
## Discovery                    (what the READMEs, code and git showed, so it isn't asked)
## README impact                (does the change alter something a module README documents? which README / section, or none)
## Same flaw elsewhere          (DEFECT only: the CLCA sweep, below)
## Assumptions / Decisions
## DB changes                 (only if needed → DB.md)
## Open questions
```

| Tier | Typical |
|---|---|
| LOW | text / label / style; isolated bug in one component or method; logging |
| MEDIUM | business logic; API contract; SP/DML; > 1 project; canvas code beyond label/color; > 150 changed lines |
| HIGH | auth; encryption/Vault; DDL; gateway routes; shared DLLs; breaking API; AFD3 persistence |

**CLCA sweep (DEFECT main tickets, once the root cause is known):** CLCA closes the loop, so look for the **same flaw
elsewhere**: `git grep` the pattern that caused it (the call, the cast, the missing check, the encryption compare…) across
the repo, the Angular apps (`c2mwf_admin`, `c2mwf_runtime`, `C2MWFAng`) and the services; read each hit and drop false
positives. List the real hits under `## Same flaw elsewhere` as bullets, one per hit:
`- path:line - what is the same - decision: fix here | follow-up`. Then **ask the developer (S14)**, grill-me style with
your recommendation: "I found N other places with the same flaw. Fix them in this ticket, or list them as follow-ups?".
`fix here` → add them to PLAN.md as tasks (with tests) and re-check the tier at 7b (more projects can raise it);
`follow-up` → they go into the CLCA text, and **one hit at a time** you offer to raise it: write the description to
`DEV_TICKETS\{N}\followup-<n>.md` (what, where `file:line`, repro or why it matters, `Related to #{N}`), run
`dt followup-create {N} --title "<Bug | …>" --description-file <that file> --hit "<path:line of that bullet>"` (a preview: Plan level Story, Node WF, tag WF, the main
ticket's milestone and C2M-Module, status New, assigned to the developer who creates it, no priority; `brief_bullet` shows the bullet it will tag), show it and ask "Create this follow-up ticket?".
Only on the developer's yes re-run with `--yes --confirm {H}`: it creates the ticket and appends ` - ticket: #N` to that bullet in BRIEF.md
(`HIT_NOT_FOUND` / `HIT_AMBIGUOUS` → nothing was created: fix the `--hit` text). `SIMILAR_EXISTS` → an open ticket Related to #{N} in Assembla
already has a near-identical title (raised on another machine, or the folder was lost): show it. Same flaw → write ` - ticket: #M` onto the bullet by hand and create
nothing; a different one, and only on the developer's say-so → re-run with `--allow-similar`. **On a no, write ` - declined: <the developer's reason>` onto the bullet**
(only from their explicit no, never to get past the check). **Every `follow-up` bullet must end in `ticket: #N` or `declined: <reason>`**: `dt rca` and the move to
Code Review refuse the sweep otherwise (`SWEEP_MISSING`). Never create one without that yes,
and never several on one yes. A review finding or Low you and the developer decide to defer is raised the same way, without `--hit`. If the root cause
is not a code pattern that can recur, write `not applicable: <reason>` and have the developer confirm. `dt rca N` and the
move to Code Review refuse a defect without this section (`SWEEP_MISSING`).

**Discovery before any question.** Answer from the sources first: the module `README.md` files, the code (`git grep` the
symbols, routes and screens the ticket names, read their callers and the enclosing classes), and, on an existing branch,
`git log origin/master..HEAD`. Write what you found under `## Discovery` in BRIEF.md. Only what is still open goes into
`## Open questions`; never ask the developer something a README or the code answers.

**Then ask grill-me style:** one question at a time, each with your recommended answer ("I'd do X because Y: agree, or
which option?"), in dependency order: settle the decision the others depend on first, and drop a later question when an
earlier answer makes it moot. Write each answer into `## Assumptions / Decisions` as it comes (convention above). When the
round is over, log `dt event {N} questions count=<how many you asked> step=4`.
DB changes → follow `DB.md` now: the ticket gets **its own DB ticket** in Assembla (`dt db-create`: a preview first, created
only on the developer's yes, titled `DB | #{N} <what changes>`, linked Related to N).

## 5 · Plan → `PLAN.md`

```markdown
# Plan — {N}
- [ ] 1. <task> — files: `<repo-relative path>/a.ts`, `<repo-relative path>/a.spec.ts` — test: <what it proves> — AC: 1
Out of scope: …
## Functional checks
- [ ] FC-1 (AC 2): <app + screen> · <exact clicks> → expected: <observable result>
```
Every AC is covered by a task **and** by a functional check (`FC-n`), for every ticket, not only UI ones: functionality is
verified by running the app in the browser (step 7d). Tests: **logic** → unit test first (mandatory); **canvas logic** →
extract to a function or service and unit test it; **component behaviour** → TestBed spec; **visual-only** →
functional check only; **AFD3 persistence** → save → load round-trip. **API / SP / backend change** → the functional
check goes through the Angular app(s) that call it (grep the route in `c2mwf_admin`, `c2mwf_runtime`, `C2MWFAng`);
never a direct API or DB call as the check. No screen reaches the change (job, consumer, unused SP): look for a screen
that shows its effect; if there is none, that is a reason to skip (below).
**Ask the developer once, right after the tier is confirmed:** "Functional checks for #{N}: run them at 7d (recommended),
skip, or decide later?" Record the answer: `dt func-mode {N} run|skip|later`. The default (no clear answer) is **run**; the
silence is never a yes here. **skip** needs a one-line reason and the developer's explicit yes
(`dt func-mode {N} skip --reason "<reason>" --confirmed`); on MEDIUM / HIGH or a backend / DB change recommend `run` and
ask a second time before accepting a skip. **later** is asked again when 7d starts. A skip is shown in SUMMARY.md
("Functional checks: skipped by the developer (reason)", enforced by `dt summary-check`), and the developer may change
the answer any time before hand-off. On `skip`, the `## Functional checks` section and 7d are not needed.
After the plan is confirmed (and the mode is `run`) run `dt profile {N}`; the profile comes from the files, never from judgement.
Handling of the result, `dt env-apply` and the automatic restore of the patched run files: `UI_CHECKS.md` §1.

**Confirm the tier:**
- LOW, **reviewer** → "Risk: LOW (reason). I'll commit, push and deploy after one 'go'. Confirm?"
  Yes → `dt state {N} --set risk=LOW path=LOW`.
- LOW, **developer** → "Risk: LOW (reason). When done you hand it to a reviewer for review and deploy. Confirm?"
  Yes → `dt state {N} --set risk=LOW path=STANDARD` (a developer never uses the LOW path).
- MEDIUM / HIGH → show the approach and plan: "Risk: {tier} (reason). OK to implement?"
  Yes → `dt state {N} --set risk={tier} path=STANDARD`.

**Dependencies (`dt deps {N}`, Angular apps):** once the planned files are known and before anything is edited, run it. It deletes
the local `package-lock.json` (only when it is not tracked) and runs `npm install --no-package-lock` in each Angular app
the plan touches, so tests start on installed versions that match `package.json` and the tree stays clean. New branch:
run it right before the baseline. Existing branch (no baseline) or a resume: run it once before the first edit, after
`dt sync-branch`. `NO_ANGULAR_APP` = nothing to do. `DEPS_CONFLICT` etc. → S13.

**Baseline (new branch only, `state.branch_origin = created`):** with the plan confirmed, dependencies fresh and nothing edited yet, run
`dt baseline {N}`. It takes the planned files from PLAN.md (the backticked repo paths; or `--files …`), finds the unit-test
projects that cover **only the projects this ticket will modify** (same rules as 7a), and runs them on the untouched
start commit. `BASELINE_PASS` (also when no test project covers the files) → set `stage=6-build`. `BASELINE_FAIL` → S11.
`BASELINE_ERROR` → fix the build or tool problem and re-run. `dt state … stage=6-build` is refused (`BASELINE_REQUIRED`)
until the baseline passed or the developer accepted it. An **existing** branch has no baseline: it starts at step 6.

## 6 · Build

Per task: test first → code → tidy, in the surrounding style. On an existing branch no tests run at the start: work from
the ticket's functionality and fix the failures that relate to it; the unit tests run after the coding (7a). Pre-commit rules apply: no trailing whitespace,
final newline, LF, ESLint clean, a `.spec.ts` for every new component/service/directive/pipe, `dotnet format`
clean. Run only that task's tests (`ng test --include=…` / `dotnet test <project>`), log to a file, read the
failures. Max **3** attempts, then S9. Tick the task. Comment purpose and complex logic;
leave unrelated code alone.

## 7 · Verify: **7a → 7b → 7c → 7d → 7e** (review last)

**7a Gates:** `python {PY}/local_gates.py --ticket {N}`. This is where the unit tests of the modified projects run **after
the coding**, on a new and on an existing branch alike. The run is recorded in `state.gates`, and `dt state … stage=8-handoff`
is refused (`GATES_REQUIRED`, `GATES_STALE`) without a passing run that still matches the files (INCOMPLETE and FAIL
don't count). It fetches `origin/master` first (7b too), so a pull of master
can't inflate the file list; a failed fetch is a `base_warning` in the RESULT. The tree started clean (step 3), so the
list is this ticket's own; if it still includes other tickets' files, re-run with `--files …`. `FAIL` on `ng-test` → **automated rounds**: run `dt gate-round {N}`. It reads the failing tests, checks which of them already fail on the start commit (a temporary checkout; nothing in the working tree is touched), counts the rounds from `metrics.jsonl`, logs the `fix_round` itself and writes `gates\round-K.md`. Max **3** rounds, then S9 (`ROUND_CAP`). **Boundary:** without asking you may edit **test files only**, and only the **setup** of a test that fails on the start commit with NG0100 (`auto_fixable`: typically `fixture.componentRef.changeDetectorRef.markForCheck()` before `detectChanges()`, or setting inputs before the first render). Everything else stops the loop and goes to the developer with the evidence (S11-style, stale test or code problem): `needs_decision` failures, any edit that changes an expected value or an assertion, any production-code edit, any product question. `ticket_failures` (they pass on the start commit) are the ticket's own and are fixed as in step 6. After each round re-run the gates and fill the "What was changed and why" part of `round-K.md`; those notes feed the Verification bullet of SUMMARY.md. Other gates (`pre-commit`, `dotnet-*`): fix from `errors`, max **2** re-runs, then S9. `INCOMPLETE` = a gate didn't run (tool missing or `--skip`): install it or run without `--skip`; never treat it as PASS. `STOP` from `db-checks` → `DB.md`.

**7b Risk:** `python {PY}/risk_check.py --ticket {N}`. `min_tier` is a floor. It also judges deleted files and new config files (`appsettings*`, ocelot, `.pubxml`); never pass `--files` with `--ticket` (such a run is not recorded, so it can't open the LOW path). `NO_CHANGES` → nothing to judge. `BUMPED` → explain its `reasons`,
`dt state {N} --set risk=<new>` (this forces STANDARD). A HIGH bump also means the HIGH diff look in 7e.

**7c Master:** `dt sync-check {N}`. `UP_TO_DATE`/`BEHIND` → fine (step 8 pulls master in). After that pull, if it changed any file this ticket also touches (compare the pull's changed files with the ticket's), re-run 7e on the merged result; otherwise the review stands.
`CONFLICTS` → S8; on LOW also `dt state {N} --set path=STANDARD`.

**7d Functional checks:** mode `later` → ask again now (run or skip). Mode `skip` → nothing to run. Mode `run` →
`UI_CHECKS.md` (re-run `dt profile {N}` first: the files may have changed). A fresh sub-agent runs the plan's `FC-n`
steps from the written steps only, then you record the outcome with `dt func-record {N} --passed X --failed Y`.
**No Chrome control in this session:** run `dt func-prepare {N}` (writes `FUNCTIONAL_CHECKS.md` from the plan's `## Functional checks`),
hand that file to Claude Desktop, and when it has saved `reviews\functional-checks-rerun.md` run
`dt func-record {N} --from {N-folder}\reviews\functional-checks-rerun.md` (reads the `passed X failed Y` line; refused with
`NO_RESULT_LINE` or `MISSING_CHECK_IDS` if the run is unfinished). The file is the evidence for the review.
`dt state … stage=8-handoff` is refused without a recorded choice (`FUNCTIONAL_MODE_REQUIRED`), and in mode `run` without a
passing run on exactly the files being handed off (`FUNCTIONAL_REQUIRED`, `FUNCTIONAL_STALE`). A recorded skip passes.

**7e Review:** run `dt review-brief {N}` (`--round 2` for a later round): it fills in `REVIEW.md` from state (N, summary, tier,
WF files, DB files, db-checks output) and writes `reviews\REVIEW_BRIEF[-roundN].md`. Spawn a **fresh sub-agent** (never the thread that
wrote the code) whose whole prompt is "Read `<that path>` and follow it": never paste the brief's text (it can be cut off). The reviewer also reads
`DEV_TICKETS\{N}\attachments\` when it exists (REVIEW.md Inputs); no extra fill-in needed. Model: the most capable
one, for every tier (on a reviewer's own LOW ticket it is the only review before deploy). It writes `reviews\code-review.md` (kept locally, never posted to the ticket); read its **Verdict**.
- **Then run `dt review-check {N}`** (`--file …\code-review-round2.md` for a later round). It checks, mechanically, that
  every High / Medium / security finding cites a real `file:line` and that its `Evidence` excerpt appears there.
  `REVIEW_CHECK_UNVERIFIED` → open each cited line yourself: if the finding holds, correct its citation and evidence in
  the review file; if it does not, show the developer the finding and the failed check, and only on their yes move it
  under `## Dismissed` in the review file with the reason. A finding is never dropped silently and counts toward the
  verdict until it is dismissed. Log the round with `unverified=<how many were left>`.
- `CHANGES REQUIRED` → fix, re-run 7a and any affected functional checks, re-review with a **new** sub-agent. Max **2** rounds, then S9.
  At the cap S9 asks the developer. If they grant one more round (state the finding and why it is small), do it with a new sub-agent and log it as
  usual (`dt event {N} review round=3 …`). No new rule or counter: every round past the cap needs the developer's explicit yes again, never assumed.
- A security finding on LOW → `path=STANDARD`. Lows → fix if trivial, else list in SUMMARY.md.
- Never write to `.claude-review-ack.json`. HIGH → show the developer the diff summary before hand-off.

## Branch sync (`dt sync-branch`)

Run it **before any code is written on a resume**, and again **before the ticket is handed off** (`STANDARD_PATH.md` step b, LOW path step 3).
It needs everything committed (`NOT_COMMITTED` otherwise) and does, in order: **pull origin/FB_{N}**, **push origin/FB_{N}**, **pull origin/master into FB_{N}**,
**push origin/FB_{N}** (a push only when there is something to push). Without `--yes` it only previews (`behind_branch`, `behind_master`, predicted conflicts).
Results: `SYNCED` (see `actions`), `UP_TO_DATE`, `CONFLICTS`, `NOT_COMMITTED`, `WRONG_BRANCH`, `FAILED`.
A conflict in either pull is aborted by the script (the tree is left as it was) and reported with `step` and `merge_conflicts`:
**tell the developer which files to resolve** (S8), they resolve and commit, then re-run. Never resolve conflicts yourself.
**`verify_gates: true`** in the result (something was merged in; or the merge the developer resolved after `CONFLICTS`, or ran from
`dt merge-master`, is now done, which shows as `UP_TO_DATE`) → run 7a (and `risk_check` on the LOW path) **immediately, before editing any code**, also
on a resume or rework. A failure then belongs to the merge (a stale line from a conflict resolution), not to the ticket's change: tell the developer,
who fixes it in a new commit; `dt sync-branch` again. It covers the WF branch only; the DB repo is still committed and pushed by the developer.

## 8 · Hand off

1. Stage **only** the ticket's files, one by one: never `git add .`, never `DEV_TICKETS/`, never local-only files
   (`local_only_ignored`, e.g. `environment.ts`). DB repo: run `dt db-files {N}`, then stage `state.db.files` in `C2M_CI_DB`.
2. Messages: `re#{N} {BUG|FEAT|REFACTOR|DOCS|TEST} | {summary}`; DB repo: `re#{D} - {N} - {summary}`.
3. Write `SUMMARY.md` in the fixed shape of **`SUMMARY_FORMAT.md`**: a title and two sections, **Impact Analysis Report**
   (point-wise, in user terms: which application features and functions change) and **Approach Taken**, written in **Assembla wiki markup** (bold heading lines, one `* ` bullet per point; Markdown does not render there). Facts from `state.json` and the gate / review results, not from memory; no process
   history, developer chores or local paths. Run `dt summary-check {N}`; `dt comment` refuses a `SUMMARY.md` that fails it
   (`SUMMARY_CHECK_FAILED`). A review's accepted Lows go under risks and gaps only when they matter to QA.
   **DEFECT main ticket:** `dt rca {N}` must say `OK` (RCA and CLCA filled in, and the CLCA sweep recorded); otherwise S12 / S14. The move to Code Review (STANDARD e, LOW path) is refused without them.
   **README check:** if `## README impact` in BRIEF.md names a README, show the developer which file and section to update, with
   suggested wording, in the hand-off message. Never edit the README yourself, and don't put it in SUMMARY.md. The developer decides.
4. **Read the file for your path now, and follow it:** `state.role = developer` → **`HANDOFF.md`** (every tier, LOW included; it also holds
   "Rework after a rejection"). Otherwise (reviewer): `path=LOW` and `risk_check.min_tier=LOW` → **`LOW_PATH.md`**; else STANDARD →
   **`STANDARD_PATH.md`** (the hand-off table, and "Deploy on request" when the developer says "deploy {N}").
