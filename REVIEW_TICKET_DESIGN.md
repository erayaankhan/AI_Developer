# Design: reviewer review and deploy (`review-and-deploy`)

Naming: two roles. **Developer** hands the ticket over and never deploys; **reviewer** reviews and deploys. (Drafts said junior/senior, then
developer/deployer; final names 2026-10-03, D-84.) The role (who runs the workflow) is `DEV_ROLE` / `state.role`; what a folder is, is `state.kind`
(`kind=review` = a review of someone else's ticket; a reviewer's own tickets have no kind). The config list is `reviewers`.

Status (2026-10-03): **all four phases built** (mocked and unit-tested; never run against a real Assembla ticket or Jenkins). Phase 1 (developer
hand-off: `dt role`, `dt handoff`, `dt assign`, `NOT_A_REVIEWER`), phase 2 (read-only review: `dt review-pick`, `review-fetch`, `review-compare`),
phase 3 (verdict: `dt review-verdict --approve | --reject`, and the developer's `dt rework`), phase 4 (reviewer deploy: `dt review-deploy`,
`staging_deploy.py deploy --reviewer`, `dt review-withdraw`, and the closed author path); the `/review-and-deploy` skill covers R1 to R7. See the four
`REVIEW_TICKET_PHASE*_PLAN.md` files and D-78 to D-83 in `PLAN.md`. Unproven, needing a real throwaway-ticket test: that a plain Assembla member can
reassign and move a ticket they do not own (open question 6), and the Jenkins side of the reviewer deploy. Where the built commands differ from the
sketch below, the plan files and the code win (e.g. `review-compare` splits blocking mismatches from warnings; the review folder keeps its stage in
`review.stage`).
Decisions from the developer lead: the **reviewer deploys, every tier (LOW, MEDIUM, HIGH)**; the developer has **no Jenkins access**; the developer
**assigns the ticket to the reviewer** when moving it to Code Review.

## 1. Goal and today's gap

`dev-ticket` is single-actor: the author picks, builds, hands off and (LOW only) deploys; it ends at Code Review ("the team's normal flow takes over",
SKILL.md step 8; HISTORY.md open questions 12 and 16).

| Gap in the current code | Where |
|---|---|
| `pick` and `status → Dev In Process` need the ticket assigned to *me*; a Code Review ticket owned by someone else can't be picked | `dev_ticket.py:215`, `:2125` |
| `status_guard` refuses any move without a local `DEV_TICKETS\{N}\state.json` (`NO_STATE`); a reviewer has none | `dev_ticket.py:2108` |
| Deploy needs the author's `state.stage` in `8-handoff`/`done` (`NOT_HANDED_OFF`) and the author's local branch equal to origin | `staging_deploy.py:274-305` |
| LOW path makes the **author** deploy inside the hand-off; MEDIUM/HIGH deploy "on request" by the author | `LOW_PATH.md`, SKILL "Deploy on request" |
| `allowed_status_moves` has no reviewer moves (Code Review → Dev Done / Dev Queue / Dev In Process) | `dev_ticket.config.json:15-28` |
| All hand-off evidence (BRIEF, PLAN, gates, functional checks, review, risk) is local and git-ignored | SKILL "Work folder" |
| Nothing records who authored the work, so a reviewer can't be stopped from approving their own ticket | none |

## 2. Roles

| Role | Who | Can | Cannot |
|---|---|---|---|
| **Developer author** | answers "developer" at the start | pick, build, verify, hand off (comments, Code Review, assign to a reviewer), rework | deploy, approve, move a ticket past Code Review |
| **Reviewer** | answers "reviewer"; has `JENKINS_REMOTE_*` in `credentials.env` | all a developer author can, **plus** the self-contained flow for their own tickets (hand off, LOW-path deploy, deploy on request), **plus** the review of a developer's ticket assigned to them: approve / reject, Dev Done / DevOps Staging, deploy | (nothing extra; the review of a developer's ticket may not be skipped) |

**Decision (2026-10-03):** a reviewer authors **and deploys their own work** as `dev-ticket` does today; no second reviewer is required. The
hand-off-to-reviewer path is **only for a developer's ticket**. So `dev-ticket` is unchanged for a reviewer, and `SELF_REVIEW` only blocks a
*developer's* ticket being approved by the developer.

**How the role is known:** `dev-ticket` asks once ("Are you a developer or reviewer?", §4.0). The answer selects the step-8 path; it is not a security
boundary: the real enforcement is that a developer has no Jenkins credentials (a deploy without them stops with `ERROR`/`NOT_A_REVIEWER`). A review
also needs the ticket **assigned to the reviewer** in Code Review, and the author of record (the user who posted the HANDOFF comment) to differ from
the reviewer.

## 3. End-to-end flow

```
AUTHOR (dev-ticket, steps 0-8)                     REVIEWER (review-and-deploy, steps R1-R7)
... 7 Verify → 8 Hand off:                          R1 PICK   tickets in Code Review assigned to me
   comments: SUMMARY + REVIEW + HANDOFF             R2 FETCH  read the three comments, checkout origin/FB_N
   status → Code Review                             R3 CHECK  my own gates, risk, (functional) re-run
   assign → reviewer            ─────────────►      R4 REVIEW read diff (+ fresh AI sub-agent), my findings
                                                    R5 VERDICT approve → Dev Done │ reject → back to author
   rework (if rejected)         ◄─────────────      R6 DEPLOY  DevOps Staging → Jenkins → QA Queue
   /dev-ticket N  → step 6                          R7 DONE
```

## 4. Author side: what changes in `dev-ticket`

### 4.0 Role question (new, step 0)
**Decided 2026-10-03 (ask once, remember):** the first `/dev-ticket` run on a machine, before `pick`, asks **"Are you a developer or a reviewer?"**
Not low-stakes (it selects the deploy path): the 30 s silence rule does not apply. The answer is stored as `DEV_ROLE=developer|reviewer` in
`AI_DEV_ENV\credentials.env` (git-ignored, per developer), never asked again. Claude must not open that file, so access is through `dt role` (reports
`developer|reviewer|unset`) and `dt role --set developer|reviewer` (writes the key, only on the developer's yes). `unset` triggers the question. At
`pick` the role is copied into the ticket (`state.role`), so a resumed ticket keeps its path. Reviewer-only commands (`review-*`, `--reviewer`) refuse a
`developer` role.

**Amended 2026-10-03 (D-85): the config lists decide the role.** `dt role` looks up the caller's Assembla login: on `reviewers` → reviewer, on
`developers` → developer (kept in `DEV_ROLE`, refreshed on every `dt role`). A listed login cannot choose another role (`ROLE_FIXED_BY_LIST`); a login on
both lists is a config error (`ROLE_CONFLICT`). Only a login on neither list is asked, once; a reviewer answer needs the login on `reviewers`
(`NOT_ON_REVIEWERS`). The role is still not the security boundary (Jenkins credentials and the live `reviewers` check in the `review-*` commands are).

### 4.1 Hand-off path by role
- **Reviewer:** unchanged: STANDARD and LOW, author deploys (SKILL step 8, `LOW_PATH.md`, "Deploy on request").
- **Developer:** STANDARD and LOW converge: the work **ends at "Code Review, assigned to a reviewer"**; LOW steps 4 (Dev Done, DevOps Staging) and 5 to 7
  (deploy) are not offered. The developer still commits (Claude never does), runs `dt sync-branch`, gates, risk check and `dt pushed`. On LOW the single
  "go" covers: stage → commit (developer) → sync → gates → comments → Code Review → assign. For a developer's ticket the human reviewer decides
  (the AI review is a local pre-read and is not posted); for a reviewer's own LOW ticket the AI review is the only independent check before deploy;
  it stays in the ticket folder, and D-9 / D-62 are superseded (see D-86).
- A developer who tries a deploy or a past-Code-Review move is refused (`NOT_A_REVIEWER`; also no Jenkins credentials).

### 4.2 Three comments, because the reviewer has no `DEV_TICKETS\{N}\`
Posted on the developer's yes, each edited in place on later rounds (existing rule, `--edit-id`):

| Comment | Content | Status |
|---|---|---|
| SUMMARY | unchanged (`SUMMARY_FORMAT.md`) | exists |
| REVIEW | `reviews\code-review.md` (author's pre-review: findings, verdict, dismissed) | not posted on any path (kept locally) |
| **HANDOFF** | fixed header + one fenced JSON block, below | **new**, `dt handoff {N}` generates and posts it |

HANDOFF JSON (facts copied from `state.json`, `gates\`, `risk_check`, `functional`, never from memory):
```
{ "ticket": N, "author": "<login>", "branch": "FB_N", "head": "<sha>", "round": 1,
  "risk": "MEDIUM", "risk_check": {"min_tier": "...", "reasons": [...]},
  "gates": {"result": "PASS", "run_at": "...", "files": [...]},
  "functional": {"mode": "run|skip", "passed": 3, "failed": 0, "skip_reason": null},
  "db": {"ticket": D, "branch": "FB_D", "head": "<sha>", "files": [...]},
  "baseline": "PASS|accepted|n/a", "readme_impact": "<file/section or none>" }
```
**Evidence, not authority:** the reviewer re-derives gates, risk and the fingerprint (R3) and trusts only what it reproduces. The `head` SHA binds
everything: if origin's `FB_N` moved after the HANDOFF, the reviewer is told (`STALE_HANDOFF`).

### 4.3 Assign to the reviewer
- `dt assign {N} --to <login> [--yes]`: preview, then on yes sets `assigned_to_id`. Refuses a login not in `reviewers` (`NOT_A_REVIEWER`) and a status
  other than Code Review. `dt assign {N} --list` prints `reviewers` (login + display name). **The developer picks from that list every time** (decided
  2026-10-03); the silence rule does **not** apply (outward action): no default without an answer.
- Step 8e order: comments → `dt status Code Review --yes` → `dt assign --to … --yes`. Then the ticket is no longer the author's: `state.stage = done`
  and `state.handoff = {reviewer, head, round, at}` (`dt state`).
- The author's one-ticket rule (Dev In Process) is untouched: a reassigned ticket frees the slot.

### 4.4 Rework (rejected ticket)
A reject returns the ticket to the author in **Dev Queue** (not Dev In Process: they may already hold another ticket there; the one-ticket rule stays
true). The author resumes with `/dev-ticket N`:
- `dt fetch` sees local `stage=done`, status Dev Queue/Dev In Process, assignee = me, and a REVIEW_VERDICT comment with `verdict=REJECT` newer than
  `state.handoff.at` → **reopen**: `stage=6-build`, `state.round += 1`, then the existing resume path (`branch`, `sync-branch --yes`, `deps`). Gates,
  functional and review records are fingerprint-bound, so they go stale and must be re-run (7a, 7d, 7e). The findings are copied into
  `BRIEF.md ## Assumptions / Decisions` and become PLAN tasks.
- Hand-off again repeats §4.2–4.3 with `round=2`, **editing** the same three comments.

## 5. Reviewer side: `review-and-deploy` skill

New skill folder `AI_DEV_ENV\skills\review-and-deploy\` (`SKILL.md` + `DEPLOY.md`); command surface in `dev_ticket.py` (prefix `dt review-…`), so it
works **with or without Claude**: every step is a `dt`/`staging_deploy.py` command a reviewer can run by hand; Claude drafts and runs the mechanical
parts. Conventions as `dev-ticket`: `RESULT:` lines, one command per call, `credentials.env` never opened, silence is never a yes.

Stages (state key `stage`; the review state lives in the same `DEV_TICKETS\{N}\state.json` with `kind="review"`):
`R1-pick R2-fetch R3-check R4-review R5-verdict R6-deploy done`.

| Step | Command | What it does / must see | Stop rule |
|---|---|---|---|
| R1 Pick | `dt review-pick [N]` | lists tickets in Code Review, in a `_CURRENT` milestone, assigned to me, Plan level Story, with a HANDOFF comment. Not Code Review → `WRONG_STATUS`; not mine → `NOT_ASSIGNED` | R-S1 none; R-S2 several (which one?) |
| R2 Fetch | `dt review-fetch N` | creates `DEV_TICKETS\N\` for the reviewer; saves `ticket.md`, SUMMARY / REVIEW / HANDOFF comments, attachments; parses HANDOFF; resolves the author (the HANDOFF comment's Assembla user); `HANDOFF_AMBIGUOUS` if HANDOFF comments by more than one person sit after the latest REJECT verdict or QUALITY-BOUNCE (earlier hand-offs are superseded, e.g. a takeover, and ignored). `SELF_REVIEW` if author = me. Checks out `origin/FB_N` **detached or as local `FB_N` reset to origin**, tree must be clean (S7). DB repo: same for `FB_D`. `STALE_HANDOFF` if `head` ≠ origin SHA | R-S3 dirty tree, R-S4 stale hand-off (ask the author to re-hand-off; don't review moving code) |
| R3 Check | `dt sync-check N`, `local_gates.py --ticket N`, `risk_check.py --ticket N`, `db_checks` (if DB), `dt review-compare N` | **independently reproduces** the author's evidence: gates PASS on the same files; risk re-computed, `reviewer tier = max(author, recomputed)`; compares with HANDOFF, flags mismatches (`RISK_HIGHER`, `GATES_DIFFER`). Behind master → author must sync (`BEHIND_MASTER`) unless the reviewer chooses to merge at deploy (open q 7) | R-S5 gate FAIL → reject path, not a fix by the reviewer |
| R3b Functional | per `UI_CHECKS.md` | **always re-run, every tier**, with the plan's `FC-n` steps (from SUMMARY/HANDOFF) and `dt func-record`. If the developer skipped the checks or no FC steps exist, the reviewer writes them from the acceptance criteria, or records their own skip (reason + explicit yes) | |
| R4 Review | fresh sub-agent with `REVIEW.md` on `origin/master...FB_N` (diff, DB files, attachments), then `dt review-check N`; the **reviewer reads the diff themself** | an *aid*: findings go through `review-check` (real `file:line`); the reviewer adds/dismisses findings; Claude never decides the verdict | R-S6 unverified finding (existing rule) |
| R5 Verdict | `dt review-verdict N --approve|--reject --file …\verdict.md --yes` | writes the single **REVIEW_VERDICT** comment (edited in place per round): verdict, reviewed `head`, tier, findings with `file:line`, what was re-run. **Approve** → `Dev Done`, `state.review.approved_head=<sha>`. **Reject** → comment, assign back to author, `Dev Queue` | consent: explicit yes from the reviewer; never silence |
| R6 Deploy | `dt status N --to "DevOps Staging" --reviewer --yes`, then `staging_deploy.py queue N`, `deploy N --reviewer --yes` (background, 45+ min) | see §6 | existing deploy rules; never re-trigger |
| R7 Done | `dt state N --set stage=done`; metrics; session improvements log | | |

Approve does **not** deploy by itself: the reviewer says "deploy" explicitly (R6). They may approve now and deploy later (queue position can make the
deploy wait; `state.deploy=queued|busy` resume as in `LOW_PATH.md` "Deploy later").

## 6. Deploy by the reviewer (all tiers)

`staging_deploy.py` and `deploy_staging_local.py` are reused unchanged where possible; only the **guards** change:

| Guard today | Reviewer version |
|---|---|
| `NOT_LOW_PATH` (LOW without `--requested`) | not applicable: new flag `--reviewer` replaces both `LOW` and `--requested` |
| `NOT_HANDED_OFF` (author's `stage`) | `NOT_APPROVED`: `state.review.approved_head` must exist and equal the **current origin SHA** of `FB_N` (and `FB_D` for the DB ticket). A push after approval voids it (`APPROVAL_STALE`) |
| `ORIGIN_DIFFERS` (local vs origin) | same check; local = the reviewer's checkout, which is what was reviewed |
| `owner_of(D)` finds the DB ticket via the author's local state | the review state records `db.ticket` from HANDOFF; the DB ticket is deployed in its own second run, as today |
| Jenkins credentials missing | an explicit `NOT_A_REVIEWER` check from config `reviewers` before the credentials check, so a developer gets a clear message instead of a generic `ERROR`. Credentials remain the real enforcement |
| status moves | new config `reviewer_status_moves`: `Code Review → Dev Done`, `Code Review → Dev Queue` (reject), `Dev Done → DevOps Staging`. Only with `--reviewer`, role reviewer, assignee = me, author ≠ me. `status_guard` `NO_STATE` accepts a review state |

Unchanged: Story-only, lowest-number-first queue (`FIRST`/`QUEUED`/`BUSY`), one trigger per ticket (`ALREADY_TRIGGERED`), `--retry` only on the
reviewer's say-so, QA test-case generation off, `DEPLOYED_NO_APPS`, "DB ticket second, own run". The author's own
`dt status … DevOps Staging --requested` path is removed from the author flow (kept only for a reviewer role).

## 7. Security and safety

- Role = config + Assembla identity, never self-declared; a developer can't deploy even by editing local state: Jenkins credentials are absent and
  `NOT_A_REVIEWER` fires first.
- The approval is bound to a commit SHA; a later push by anyone voids it. The deployed code is exactly the reviewed code.
- No self-review; the author comes from Assembla (the HANDOFF comment's user), not from local files.
- The review checkout is read-only by policy: no commits, no edits to `FB_N`; findings go back to the author (open q 8).
- Verdict, assign, status and deploy each need the reviewer's explicit yes in the conversation (Consent rule); the AI review cannot approve.
- No new secrets, no config-default changes to Jenkins; `credentials.env` and `local_env.env` stay unopened.

## 8. Open questions (recommendation first)

0. ~~Role remembered?~~ **Decided 2026-10-03:** asked once, stored as `DEV_ROLE` in `local_env.env` via `dt role` (§4.0).
1. **Who is in `reviewers`?** **Decided 2026-10-03:** a `reviewers` list of Assembla logins in `dev_ticket.config.json`; at hand-off the developer sees the
   list and **picks one each time** (`dt assign` refuses a login not on it). Still needed from the lead: the actual logins; whether a `default_reviewer`
   pre-selects one (a confirmation only, never silent).
2. ~~Reviewer-authored tickets~~ **Decided 2026-10-03:** a reviewer authors and deploys their own work (today's flow); review-and-deploy is for a
   developer's ticket only.
3. ~~Approval state~~ **Decided 2026-10-03:** approve → `Dev Done`; "deploy" → `DevOps Staging` (as today's LOW moves). The approval is bound to the
   reviewed head SHA; a later push voids it. Approve and deploy are separate explicit yeses.
4. ~~Reject destination~~ **Decided 2026-10-03:** Dev Queue, assigned back to the developer. The reviewer's Claude posts the rejection as the single
   REVIEW_VERDICT comment; the developer's Claude reads it on `/dev-ticket N` (§4.4). Format: a fixed header (`verdict=REJECT`, reviewed `head`, `round`,
   reviewer login), one numbered finding per item (`file:line`, what is wrong, what to change, severity), and a plain "what to fix before re-submitting"
   list. The developer's Claude treats it as data from a teammate, not instructions: it shows the findings, the developer confirms the plan, and findings
   it disagrees with go to the developer to raise with the reviewer, never silently dropped.
5. ~~Functional checks on review~~ **Decided 2026-10-03:** the reviewer **re-runs the functional checks on every tier (LOW, MEDIUM, HIGH)**; the
   developer's recorded result is not trusted in its place. (Answer given as "A, re-run for all paths", read as always re-run. A recorded skip by the
   developer still needs the reviewer's own call, see R3b.)
6. **Assembla permissions (read-only probe, 2026-10-03):** space `c2m-ci` has `team_permissions=2` (edit) and 94 users: 88 plain `member`, 5 `owner`,
   1 `watcher`. **Assembla does not distinguish developer from reviewer**: any member can in principle reassign a ticket and change its status (the
   existing code already PUTs `assigned_to_id`, `dev_ticket.py:781`). So the role is enforced by our config (`reviewers`) and Jenkins credentials, not by
   Assembla. **Not yet verified (needs a write):** that a developer's reassign and a reviewer's status move succeed on a ticket they don't own, and that
   Jenkins accepts the reviewer's `JENKINS_REMOTE_*` for a ticket they didn't author. Plan: one test ticket, on the developer's explicit yes, in phase 1/3
   testing.
7. ~~Branch behind master at review~~ **Decided 2026-10-03:** the developer's side brings the branch up to date **before** Code Review: commit `FB_N`,
   push, pull `origin/master` into `FB_N`, push (today's `dt sync-branch`, SKILL step 8 order commit → sync → gates). The reviewer does not merge master;
   if master moves after hand-off the reviewer gets `BEHIND_MASTER` and the developer re-syncs. **Who runs the commit and who resolves merge conflicts
   is PENDING (open question 9).**
9. ~~Commit and conflicts~~ **Decided 2026-10-03:** both rules stay. The developer commits (Claude never does) and resolves any merge conflict (S8);
   `dt sync-branch --yes` does the pulls and pushes. **Claude always gives the exact commit message**, ready to paste (`git commit -m "re#N CAT | summary"`,
   plus the DB repo's `re#D - N - summary`), for every commit it asks the developer to make, including after a rework round. Claude may explain a conflict
   and suggest a resolution; the developer applies it.
8. ~~Reviewer's own fixes~~ **Decided 2026-10-03:** none. The review checkout is read-only; every fix goes back to the developer through a reject (§4.4),
   however small.
