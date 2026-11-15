# AI_DEV_ENV — dev-ticket planning history

_Step 7 letters here are as written at the time (7c risk, 7d master, 7e UI checks, 7b review); the current ones are 7a gates, 7b risk, 7c master, 7d functional checks, 7e review (SKILL.md)._ _Moved verbatim from `PLAN.md` on 2026-09-30 (split for readability; keep-drafts rule). `PLAN.md` holds the current design, decisions log and open items; this file holds the original v2 draft, verification notes, reviews, the full question-and-answer log and superseded text. Full pre-split copy: `history/PLAN-2026-09-30-before-split.md`._

---

## 1. What the developer does

```
/dev-ticket            # picks your single "Dev In Process" ticket
/dev-ticket 18474      # or a specific one (must be yours and in Dev In Process)
```

Then answers questions only when the process stops, approves the plan for MEDIUM/HIGH-risk tickets,
and runs `git commit` + `git push` at the end.

## 2. The workflow (8 steps)

| # | Step | Who | Output | Stops when |
|---|---|---|---|---|
| 1 | **Pick** ticket | 🤖 `dev_ticket.py pick` | ticket number | 0 tickets · >1 ticket · not assigned to you · wrong status |
| 2 | **Check** ticket has enough detail | 🧠 + 🤖 `fetch` | `ticket.md`, verdict in `state.json` | NEEDS_INFO → questions (optionally posted to the ticket) |
| 3 | **Branch** `FB_{N}` from `origin/master` | 🤖 `dev_ticket.py branch` | branch checked out | dirty tree · merge conflicts with master |
| 4 | **Understand** | 🧠 | `BRIEF.md` (problem, root cause, approach, ACs, risk) | open questions (asked in one batch) |
| 5 | **Plan** | 🧠 | `PLAN.md` checklist (task → files → test → AC) | MEDIUM/HIGH: waits for approval |
| 6 | **Build** (test-first, per task) | 🧠 | code + tests | a task still failing after 3 fixes |
| 7 | **Verify**: local gates + independent AI review | 🤖 `local_gates.py` + 🧠 sub-agent | `gates/*.log`, `reviews/code-review.md` | still failing after 2 rounds |
| 8 | **Hand off** | 🧠 + 🧑 | staged files, commit message, `SUMMARY.md` | developer commits/pushes; y/n for comment + status |

🤖 script · 🧠 Claude · 🧑 developer. Full instructions: `AI_DEV_ENV/skills/dev-ticket/SKILL.md`.

### Compared with grill-to-implementation

| grill-to-implementation | dev-ticket |
|---|---|
| Grill (one question at a time) → PRD → PRD review gate → ADR → slices → slice review gate → ralph → commit prep | Check ticket → BRIEF.md → PLAN.md → build → gates + one review → hand off |
| Requirements come from the conversation | Requirements come from the **ticket**; only gaps are asked, in one batch |
| 2 review sub-agents on documents | 1 review sub-agent on the **code** (against the ACs) |
| Many files per ticket (PRD, reviews, slice files, done/) | 3 files you write: BRIEF, PLAN, SUMMARY |
| Starts from an idea | Starts from Assembla status + assignee rules |

Use grill-to-implementation for large or unclear CRs; use dev-ticket for normal tickets.

## 3. Rules (hard stops)

- Only the developer's **own** tickets: identity comes from their own Assembla API key (`GET /v1/user`).
- Exactly **one** ticket in `Dev In Process` per developer; 0 or >1 → stop and ask.
- A named ticket that isn't assigned to the developer or isn't in `Dev In Process` → stop and ask.
- Ticket missing required detail → stop, list specific questions, offer to post them on the ticket.
- Claude never commits or pushes code, never changes status or posts comments without a yes, never
  stashes/discards the developer's changes.
- Status moves allowed only along `allowed_status_moves` in `dev_ticket.config.json`
  (`Dev In Process → Dev Done → DevOps Staging`).

## 4. What was built (in `AI_DEV_ENV/`)

| File | Purpose |
|---|---|
| `python_scripts/assembla_api.py` | Stdlib Assembla REST client: current user, tickets, comments, tags, links, attachments, milestones, "my active tickets" report, add comment, update status (verified by re-reading). Keeps the Cloudflare empty-204 retry from `deploy_on_cloud_staging_v1.py`. |
| `python_scripts/dev_ticket.py` | CLI: `whoami`, `pick`, `fetch`, `branch`, `comment`, `status`. Each ends with one `RESULT: {json}` line. Write actions preview unless `--yes`. |
| `python_scripts/local_gates.py` | Local replacement for the CI pass: detects changed files, runs `pre-commit --files`, `dotnet build`/`dotnet test` for affected projects, `ng test --include` for affected specs. Logs to the ticket folder; prints only error lines. |
| `python_scripts/dev_ticket.config.json` | Status names, allowed moves, branch prefix, work folders, CR/Defect inference rules, staging-attribute checks. |
| `skills/dev-ticket/SKILL.md` | The workflow Claude follows. |
| `.claude/commands/dev-ticket.md` (here and at repo root) | Registers `/dev-ticket`. |

Branch creation is plain git: `git switch --no-track -c FB_N origin/master` (push only on yes).
"Would merging master conflict?" is answered with `git merge-tree`, which touches nothing and
creates no commit.

## 5. Verification status

| Check | Result |
|---|---|
| All three scripts compile | ✅ |
| `local_gates.py --dry-run` plans the right gates (pre-commit, 2× dotnet build, 2× dotnet test, ng test) for sample files | ✅ |
| `dev_ticket.py` without credentials returns a clean `ERROR` result | ✅ |
| `whoami` / `pick` / `fetch` against real Assembla | ✅ 2026-09-29, see §5a (`fetch` tested via a render-only harness; no ticket folders written) |
| `branch` on a real ticket | ⏳ not run — it switches branches; run on a clean tree |
| Full `/dev-ticket` run on a LOW defect | ⏳ next |

## 5a. Findings from the first live Assembla check (2026-09-29, read-only)

| Finding | Effect on the plan |
|---|---|
| `GET /v1/user` identifies the developer correctly (saleemk). | Identity by personal key works as designed. |
| `tickets.json?report=8` returns only **closed** tickets (959, all Fixed/Invalid/Closed). `/tickets/my_active.json` returns active tickets assigned to the key owner (112). | `get_my_active_tickets()` switched to `/tickets/my_active`. Open question 4 answered. |
| saleemk currently has **2 tickets in Dev In Process**: #9131 and #18359. | `pick` correctly returns `MULTIPLE`. The one-ticket rule will stop immediately until one is moved back (see question 6). |
| Ticket type is the custom field **`Category`**: Defect 38, Change Request 31, Bug 16, Task 9, Story 6, blank 5 (in the 112 active tickets). Summary prefixes (`Defect |`, `Story |`, `CR |`, …) are inconsistent. | Config: `custom_field = "Category"`; Defect/Bug → DEFECT, Change Request/Story → CR; **Task and blank → ask**. Open question 1 mostly answered. |
| Useful structured fields on every ticket: `C2M-Module`, `C2M-Sub-Module`, `Environment`, `Severity`. | Added to readiness hints (answers "which area?" / "where does it happen?"). |
| #9131 has 160 comments; #18359 has an **empty description**. | Snapshot keeps the latest 40 comments (`max_comments_in_snapshot`), full set in `ticket.json`. #18359 would be `NEEDS_INFO`, which is a good first test of the stop rule. |
| No milestone has `_CURRENT` in its title right now; active ones are `Workflow 3.16.0` / `Workflow 3.17.0`. | dev-ticket no longer depends on milestones. **But `deploy_on_cloud_staging_v1.py` and `create_feature_branch_v1.py` look for `_CURRENT`** (see question 7). |
| The space has statuses **Code Review** (between Dev In Process and Dev Done), **Missing Implementation Details** and **Req Questions**. | Candidates for the hand-off and NEEDS_INFO transitions (see questions 8 and 9). |
| Attachments endpoint returned 0 for both tickets checked. | Still unconfirmed whether that means "no attachments" or "endpoint not available". Check with a ticket that has attachments. |
| #18359's parent Epic #9590 is in status **Invalid**. | Possible extra readiness warning: "parent Epic is closed/invalid". |

## 5c. LOW-risk end-to-end path (draft, from D-6)

```
MEDIUM / HIGH (unchanged):
  … 7 Verify → 8 Hand off → [human] commit+push → Code Review (human reviewer) → normal flow

LOW (D-6):
  … 7 Verify ─► 7c Risk re-check ─► 8L commit + push ─► Code Review ─► AI review = review record
       │           (script, on the         (see Q13)        │              │
       │            actual diff)                            ▼              ▼
       │                                              Dev Done ─► DevOps Staging ─► staging deploy (see Q14)
       │                                                                              │
       └── any gate/review fails → falls back to the MEDIUM/HIGH path                  ▼
                                                                         verify: deploy result + QA Queue
                                                                         + comment on ticket (SUMMARY + review)
```

Safeguards proposed for the LOW path:
1. **Risk is re-checked on the real diff by a script (7c), not only by Claude's judgement.** It
   bumps the ticket to MEDIUM (and so to the human path) if the diff touches:
   `wfapi_gateway_service/**` (Ocelot routes), `SharedLibraries_MicroServices/**`, encryption/Vault
   code, `*.sql` DDL, auth guards/interceptors, `**/appsettings*.json`, more than one
   service/app, or more than N changed lines (proposed N = 150).
2. **The developer confirms the tier once** (at step 5, when the plan is shown). Claude can't move a
   ticket *down* to LOW after that.
3. **The AI code review (7b) becomes the Code Review record.** `reviews/code-review.md` is posted as
   a ticket comment before the ticket moves past Code Review, so a human can audit it.
4. **Any failure drops back to the normal path.** That covers a failed gate or review after the fix
   budget, a merge conflict with master, or a failed deploy: the ticket stays in (or returns to)
   Code Review for a human.
5. **Staging result is verified, not assumed:** deploy outcome read back, and the ticket is confirmed
   in QA Queue.

## 5d. Workflow review (2026-09-29): all proposals accepted

| # | Issue found | Resolution | Status |
|---|---|---|---|
| R-1 | LOW plans didn't wait, but LOW now auto-pushes/deploys | Step 5 always confirms the tier in one line; for LOW, the tier confirmation and "go" are the only prompts | ✅ SKILL step 5 |
| R-2 | No script re-check of risk on the real diff | `risk_check.py` (step 7c): HIGH/MEDIUM path rules, DDL detection, AFD3 `final_json`, >1 app, >150 lines; result is a floor stored in `state.json` | ✅ built, tested on sample files |
| R-3 | Skill said "never commit/push" for all tiers; no LOW hand-off | SKILL 8-LOW branch added (go → commit → push → verify → comments → status moves) | ✅ text; ⏳ deploy step and LOW status moves not built |
| R-4 | Resume ran after `pick` | Step 0 resume check happens before `pick` | ✅ SKILL |
| R-5 | Nothing verified the push before Code Review | `dev_ticket.py pushed N` → PUSHED / NOT_COMMITTED / NOT_PUSHED / BEHIND_ORIGIN | ✅ built, tested on FB_9131 |
| R-6 | Master could move on after step 3 | `dev_ticket.py sync-check N` (step 7d); conflicts on the LOW path → standard path | ✅ built, tested on FB_9131 (BEHIND 7, no conflicts) |
| R-7 | Check before attribute fix order | Kept as is | ✅ |
| R-8 | `state.json` stage names undefined | `1-pick … 8-handoff, done` in config `stages` + SKILL conventions | ✅ |

**Still to build for the LOW path to be switched on:**
1. LOW-path status moves (Code Review → Dev Done → DevOps Staging), accepted by `status` only when
   `state.json` has `path=LOW` and `risk_check.min_tier=LOW`.
2. Staging step: DevOps Staging queue position check (`_CURRENT`, Story, lowest number), trigger
   `deploy_on_cloud_staging` via the Jenkins API, wait, then confirm QA Queue. Needs
   `JENKINS_USER` / `JENKINS_API_TOKEN` (add to SETUP.md).
3. An end-to-end dry run on a real LOW ticket.

_Update 2026-09-29:_
- _1 ✅ built: config `low_path_status_moves` + `low_path_problems()` guard in `dev_ticket.py status`._
- _2 ✅ built: `staging_deploy.py` (`queue N` read-only, `deploy N --yes`). Guards: LOW path,
  DevOps Staging, Story, FB_N on origin, first in queue (else `QUEUED`, D-11), job not running (else
  `BUSY`). Jenkins client adapted from `deploy_on_cloud_staging_v1.py`, but it identifies the new
  build by "last build number before trigger + 1" instead of "first running build". After the run it
  checks the console "Single-ticket mode: selected …" line matches #N and that the ticket is in QA
  Queue. SETUP.md §2b added. Tested: guards only (`NO_CURRENT_MILESTONE`, `NOT_LOW_PATH`); nothing
  triggered, and Jenkins credentials aren't set yet._
- _3 ⏳ still open._
- _Credentials 2026-09-29: all four keys now come from `AI_DEV_ENV/credentials.env` (git-ignored;
  `dev_ticket.py creds` shows file/env/missing). Assembla `whoami` = saleemk ✅.
  `staging_deploy.py check` = OK, Jenkins user **admin**, job `deploy_on_cloud_staging` visible,
  last build #20 SUCCESS. The first attempt failed with 401 because the token field held
  base64(user:token); `BAD_TOKEN_FORMAT` guard added. Open question 24: shared admin token vs
  per-developer Jenkins tokens._
- _Facts learned from reading the deploy script: (a) it uses the **first** `_CURRENT` milestone
  only; (b) if FB_N is missing on origin it **moves the ticket to QA Queue without deploying**,
  hence the `BRANCH_NOT_ON_ORIGIN` guard; (c) after a real deploy, the QA Queue status comes from
  the remote merge job, not from this script._

## 5e. Pause / resume

_Update 2026-09-29, built (D-32..D-40):_
- _`assembla_api.upload_attachment()` (bigfiles host, ticket id) and `download_attachment()`.
  `/tickets/{n}/attachments.json` verified on #17581 (9 files); download verified (S3 redirect,
  byte count matches). `/documents.json` ignores ticket filters, so it isn't used._
- _`dev_ticket.py handover N --notes F [--yes]`: requires the WF (and DB) WIP pushed; builds
  HANDOVER.md with BRIEF/PLAN/state embedded between markers; attaches `HANDOVER-{N}-<stamp>.md`;
  posts a one-line comment; Dev In Process → Dev Queue; state `paused`._
- _`dev_ticket.py restore N [--force]`: rebuilds `DEV_TICKETS\{N}` from the latest attachment._
- _Allowed moves added: Dev In Process ↔ Dev Queue. SKILL: Pause and Resume sections, S4e, and the
  "every chat answer goes into BRIEF.md" rule._
- _Tested: guards (`NO_HANDOVER`, `NOT_PUSHED`/`WRONG_BRANCH`), embed/extract round-trip (exact,
  including code blocks). Not yet: a real pause/restore on a ticket (it uploads and moves status)._

_Original draft below, kept for reference._

### Situations
| # | Situation | Covered by |
|---|---|---|
| A | Stop and come back on the same PC, still on `FB_N` | Step 0 resume (exists) |
| B | Switched branch/ticket in between | Resume by ticket number from any branch |
| C | Uncommitted changes when switching | Pause → WIP commit + push (D-32) |
| D | Must take another ticket (one-ticket rule) | Pause → status Dev Queue (D-33) |
| E | Another PC, or a teammate takes over | `HANDOVER.md` (D-34) + WIP commit on origin; reassigned dev may resume (D-35) |
| F | Master / ticket changed while paused | Resume re-runs `fetch`, `sync-check`, `risk_check` and flags drift |

### `/dev-ticket pause` (proposed)
1. Stage the ticket's files only (never local-only files, never `DEV_TICKETS/`).
2. WIP commit `re#{N} WIP | paused at {stage}: {next task}` + push `FB_N` (D-32).
   _Who runs it (Claude vs developer) and whether WIP commits in master's history are OK: open Q34._
3. Write `DEV_TICKETS\{N}\HANDOVER.md` (template below) (D-34).
4. Status `Dev In Process → Dev Queue` (D-33; needs adding to `allowed_status_moves`).
5. `state.json`: `stage = "paused"`, `paused_from = <previous stage>`, `paused_at`, `paused_by`.

### `HANDOVER.md` template (proposed)
```markdown
# Hand-over — #{N} {summary}
Paused: {date} by {developer} · Stage: {stage} · Risk: {tier} ({path}) · Branch: FB_{N} @ {short sha} (pushed)
DB: {none | ticket #D, branch FB_{D}, files: …}

## Where I stopped
- Last completed task: {n}. {task}
- Next task: {n+1}. {task}
- In progress / half-done: {what, which files}

## Decisions made so far
- {assumptions and answers from BRIEF.md "Assumptions" / Q&A}

## Open points / blockers
- {anything unresolved}

## How to resume
`/dev-ticket {N}` (assigned developer) → see §5e resume

<!-- The full BRIEF.md, PLAN.md and state.json are appended below, verbatim, between markers,
     so a resume on a PC without DEV_TICKETS\{N}\ can rebuild them. -->
<!-- BEGIN BRIEF.md --> … <!-- END BRIEF.md -->
<!-- BEGIN PLAN.md --> … <!-- END PLAN.md -->
<!-- BEGIN state.json --> … <!-- END state.json -->
```

### Resume (proposed outline, to refine with the developer's questions)
1. `/dev-ticket N` from any branch; the ticket must be assigned to the current developer (D-35).
2. If it's in Dev Queue: check the one-ticket rule, then offer Dev Queue → Dev In Process.
3. Check out `FB_N`, pull.
4. If `DEV_TICKETS\{N}\` is missing: rebuild BRIEF/PLAN/state from `HANDOVER.md`. **Open Q35: how
   does another PC get the file?**
5. Drift checks: `fetch`, `sync-check`, `risk_check`; report changes; continue at `paused_from`.

## 6. Open questions

1. **Ticket type:** which Assembla field or tag marks CR vs Defect? Set
   `ticket_type_rules.custom_field` in the config. Until then Claude decides from the text and asks
   when unsure.
    _Answered → D-4 + §5a: custom field `Category` (Defect/Bug → DEFECT, everything else → CR)._
2. **Branch creation side effects:** the old `C2MC - Create Feature Branch` Jenkins job may have done
   more than cut the branch (e.g. ticket links or naming checks on the build server). Does anything
   downstream rely on that job having created the branch?
   _Answered → D-16: nothing else. Plain `git` branch creation is sufficient._
3. **Staging:** the staging merge/deploy job still filters on Plan level = Story, Node = WF, tag WF
   and an Epic parent. `fetch` warns about these, but should the process fix them (with a yes)?
    _Answered → D-12/D-13: checked before branch creation and fixed by the workflow._
4. **Report 8 ("my active tickets"):** check on the first real `pick` that it returns
   `Dev In Process` tickets. If not, the current-milestone scan fallback is used.
    _Answered → §5a: report 8 returns closed tickets; `/tickets/my_active` is used instead._
5. **Attachments endpoint:** `GET /tickets/{n}/attachments` is best-effort (returns an empty list if
   the space doesn't expose it). Check on the first real `fetch`.
   _Update 2026-09-29: both tickets checked returned 0. Still unconfirmed; need a ticket with attachments._
6. **Your two Dev In Process tickets (#9131, #18359):** which stays in Dev In Process for the first
   trial run? Or should the rule allow "one per developer *per milestone*"?
    _Still open (test concern only): decide which ticket stays in Dev In Process for the first end-to-end run._
7. **`_CURRENT` milestone:** is the `_CURRENT` suffix added only at deploy time, or has the convention
   changed? The existing Jenkins scripts depend on it.
   _Answered → D-1: `_CURRENT` is required when picking a ticket to start development._
8. **Hand-off status:** should step 8 move the ticket to **Code Review** (which exists between Dev In
   Process and Dev Done) instead of straight to Dev Done?
   _Answered → D-2: yes._
9. **NEEDS_INFO status:** when the ticket lacks detail, should the process offer to move it to
   **Missing Implementation Details** (or Req Questions), besides posting the questions?
   _Answered → D-3: yes, Missing Implementation Details._
10. **Category = Task or blank:** treat as CR by default, or always ask?
    _Answered → D-4: treat as CR._
11. **Who moves a ticket back** from Missing Implementation Details to Dev In Process once the details
    are added: the reporter/BA, or the developer?
    _Answered → D-5: the developer._
12. **Code Review → next:** does the reviewer move it to Dev Done by hand, or should a later version
    of this workflow support the reviewer's side too?
    _Answered → D-6: for LOW risk the AI does it end to end up to staging; MEDIUM/HIGH unchanged._
13. **Commit and push on the LOW path:** today's rule is "Claude never commits, only stages". For
    D-6 to be truly end to end, either (a) Claude commits and pushes LOW tickets itself, or (b) the
    developer still runs `git commit` + `git push` (one command) and Claude does everything after that.
    _Answered → D-7: (a), Claude does everything._
14. **How staging deployment is triggered:** the actual merge/build/deploy runs on Jenkins
    (`C2MC - Merge and Deploy Feature Branch` on jenkins-build-server-4, driven by
    `deploy_on_cloud_staging`). Options: (a) the workflow moves the ticket to DevOps Staging and the
    existing job runs as it does today; (b) the workflow triggers `deploy_on_cloud_staging` itself via
    the Jenkins API (a Jenkins dependency, but only for this one step). Note that job deploys **every**
    ticket in DevOps Staging, not just this one.
    _Answered → D-8: (b), trigger it._
    _Correction 2026-09-29: the job deploys **one** ticket per run (the lowest-numbered Story in
    DevOps Staging), not every ticket. See the fact note under §5b._
15. **Approvals on the LOW path:** no prompts at all after the tier is confirmed, or one "go" before
    the first outward action (commit/push or status change)?
    _Answered → D-10: option B, one "go"._
16. **Who counts as the reviewer of record** for LOW tickets in Assembla/audit terms: the AI review
    comment alone, or should the developer's "go" count as the sign-off?
    _Answered → D-9: the AI review comment is enough._
    _(15. Approvals → answered D-10: option B, one "go".)_
17. **LOW ticket queued behind others in DevOps Staging:** (a) don't trigger; leave it queued and
    report (proposed), (b) wait and poll until it's at the front, then trigger, or (c) ask the
    developer each time?
    _Answered → D-11: (a)._
18. **Fixing staging attributes:** for LOW tickets, missing Plan level = Story (or Node/WF tag/Epic)
    blocks the deploy. Should the workflow offer to fix those fields in Assembla (with a yes), or
    should the developer fix them by hand?
    _Answered → D-12: they're checked before branch creation (stop and ask). Who fixes them is Q19._
24. **Jenkins identity:** the current token authenticates as `admin`. Use per-developer Jenkins
    users/tokens (deploys traceable to the developer), or is a shared admin token acceptable?
    _Answered → D-17: a shared admin token is acceptable._
25. **DB branch commits (D-18):** the DB commit sits on a *shared* branch (FB_{D}). Which ticket
    number goes in its message: `re#{N}` (the WF ticket), `re#{D}` (the DB ticket), or both? And who
    commits and pushes it: the developer as usual, or Claude on the LOW path?
    _Answered → D-19 (see note under 28)._
26. **Risk floor for DB changes:** proposed SP/DML change → at least MEDIUM, DDL → HIGH (so a DB
    change never takes the automatic LOW path). OK?
    _Answered → D-20: yes._
27. **Missing DB branch:** if `FB_{D}` doesn't exist in C2M_CI_DB, may the workflow create it from
    the DB repo's master, or must someone else create it?
    _Answered → D-21: the workflow creates and checks out FB_{D}._
28. **Deploy order:** must the DB ticket reach staging before the WF ticket that depends on it? How
    are DB scripts applied to staging today?
    _Answered 2026-09-29 → 25: D-19 (`re#{D} - {N} - {summary}`; commits by the developer, since DB
    changes are ≥ MEDIUM and never go down the LOW path). 26: D-20 yes. 27: D-21 the workflow creates
    and checks it out. 28: D-22 either order; the same deploy job deploys the DB ticket when it's in
    DevOps Staging._
29. **Which ticket counts as the milestone's DB ticket:** only a Story ("DB changes for <release>"),
    or any active C2M-DB ticket? Workflow 3.17.0 currently has only #18478, a *Bug* with
    Node = C2M-DB, and the lookup would pick it.
    _Answered → D-23: only a Story with Node = C2M-DB. Workflow 3.17.0 would therefore stop with
    NO_DB_TICKET until a DB Story is created._
_DB facts (2026-09-29): C2M_CI_DB uses `FB_{n}` branches (591 on origin). DB tickets are usually
Stories named "DB changes for <release>" (#18533 Console 6.19.1, #18353 GenAI 1.20.0, #18313 Console
6.20.0). Workflow 3.17.0 currently has only #18478, which is a Bug with Node = C2M-DB, not a DB Story._
30. **UI checks environment (D-25):** for local checks, which URL does the federated app serve on,
    which backend/APIs does it call (local services or a shared dev/staging API), and how does
    Claude log in (a test user in `credentials.env`)? Are there test WF/BM records to use, or should
    Claude create throw-away ones?
    _Answered → D-27 (see note under 31)._
31. **Checks after deploy:** should Claude also run the same UI checks on **staging** after a LOW
    deploy reaches QA Queue, as an early warning before QA?
    _Answered → D-27 (30: 4200 host / 4202 remote, backend via environment.ts, login and test data
    ask the developer) and D-28 (31: yes, after Java_Transfer has deployed the container)._
32. **Host app on 4200:** which project/command serves the host on `localhost:4200`? (CLAUDE.md says
    the shared host app was recently removed from this repo.)
    _Answered → D-29: C2M_CommonServices\C2M_Console_Angular_Beta, `ng s`, always on `dev`._
33. **Staging URL** for the post-deploy UI checks (config `ui_checks.staging_url`)?
    _Answered → D-29 (32: C2M_Console_Angular_Beta, `ng s`, always on `dev`) and D-30 (33: per
    project, ask one at a time; for API changes, test the related Angular project)._
34. **WIP commit on pause (D-32):** does Claude run the WIP commit + push (a new exception to
    "Claude never commits"), or does the developer run it with Claude's prepared message? And are
    WIP commits acceptable in master's history once the branch merges (or should they be squashed)?
    _Answered → D-36 (developer commits with Claude's message) and D-38 (WIP stays in history)._
35. **Where HANDOVER.md lives (D-34):** `DEV_TICKETS\{N}\` is git-ignored and local. For another PC
    or a teammate to resume, the file must be reachable. Options: commit it on `FB_N` (then it
    merges to master unless removed), a shared network folder, or attach it to the ticket.
    _Pending the developer's questions on resume._
    _Answered 2026-09-29 → 34: D-36 (developer commits with Claude's message; squash question still
    open). 35: D-37 (post on Assembla)._ _Squash → D-38: WIP commits stay in history._
36. **Hand-over format on Assembla (D-37):** (a) attach `HANDOVER-<date>.md` + one-line comment
    (needs the attachment list/download API verified), or (b) the whole hand-over as one comment
    with marked blocks (verified API, but long comments in the ticket history)?
    _Answered → D-39: (a), attachment + one-line comment. D-40: all three completeness rules confirmed._
37. **FYI (found while probing):** #17581 has `001-FB_16588-testcases-code.csv` attached, a QA
    test-case file generated for **another ticket's branch**. That's possibly a glitch in the staging
    job's QA step (`qa_testcases_from_code`); worth checking separately.
    _Still open (FYI, outside this workflow): the staging job's QA step attached another branch's test cases to #17581._
38. **Side effect of D-44:** `qa_testcases_from_code` is also the step that **merges `origin/master`
    into `FB_N` and pushes it** before the deploy (see its docstring). With QA generation off,
    that sync no longer happens, so the remote merge job receives FB_N as-is. Options: (a) accept
    it (step 7d already warns about conflicts; the merge job reports them), (b) the workflow merges
    master into FB_N before hand-off (a merge commit, run by the developer on STANDARD, part of
    "go" on LOW), (c) keep QA generation on only for the sync (not possible with the current flag).
    _Answered → D-46: (b)._

_Resume completeness (2026-09-29): one `HANDOVER.md` is enough **if** (1) it embeds BRIEF.md,
PLAN.md and state.json verbatim; (2) pause also prepares a WIP commit on `FB_{D}` in the DB repo
when there are DB changes (`re#{D} - {N} - WIP …`); (3) every answer the developer gives in chat is
written into BRIEF.md "Assumptions/Decisions" right away. Not needed: ticket snapshot (re-fetched),
gate logs, risk check, and code-review/UI-check results (re-run after resume). Local environment is
per PC by design._
19. **Fixing the attributes:** when `branch` stops with `ATTRIBUTES_MISSING`, should the workflow
    offer to fix the fields itself (with a yes, like the old `create_feature_branch` job did,
    except the Epic, which it never guesses), or does the developer always fix them in Assembla?
    _Answered → D-13: the workflow fixes them._
20. **Parent Epic status:** #18359's Epic (#9590) is **Invalid**. Should a closed/invalid Epic parent
    also count as "No Epic parent"?
    _Answered → D-14: yes; candidates from the whole space (Node = WF), ranked, developer picks._
21. **"Close to the ticket":** I read this as *most relevant to the ticket being developed* (shared
    words, same milestone, same module, Epic itself in development). Did you mean something else,
    e.g. only Epics whose status is Dev In Process?
    _Answered → D-15: Epics from the similar area of functionality._
22. **Inactive Epic statuses:** Closed, Invalid, Rejected, Not Reproducible, User Error and
    **Fixed or Prod-Deployed** are treated as inactive (config `epics.inactive_statuses`). Is a
    prod-deployed Epic really closed for new stories?
    _Answered: yes._
23. **Unlinking an old inactive Epic:** the `DELETE` association call hasn't been tested yet. It will
    be checked on the first real fix, which could be #18359.
    _Answered 2026-09-29, first real write test on #18359 (developer approved, Epic #10201 chosen
    by the developer): `fix-attributes --yes` → `FIXED`. Independent re-read confirmed tag WF added,
    #9590 (Invalid) unlinked (`DELETE .../ticket_associations/{id}.json` works), parent = #10201,
    staging-attribute problems = none, "Auto-fixed by dev-ticket" comment posted. Status unchanged
    (Dev In Process)._

_Epic lookup facts (2026-09-29): 2,241 active tickets in the space, 153 active Epics, 40 with
Node = WF; full scan ≈ 25 s, hence the 60-minute cache. Keyword ranking is weak when the ticket
description is empty (#18359); a later improvement is to also rank by which Epics similar tickets
already sit under._
_Update (D-15): the "which Epics similar tickets sit under" signal is now in. Timings for #9131:
143 s → 15 s once the space scan was cached, links were matched against known Epic ids, and the
empty-body retry was shortened for tags/links. #9131's top candidate is #8783, its actual parent._

## 7. Next steps

1. Set `ASSEMBLA_API_KEY` / `ASSEMBLA_API_SECRET` (your own; see `SETUP.md` §2), then run
   `python AI_DEV_ENV\python_scripts\dev_ticket.py whoami` and `... pick`.
2. Answer open question 1, then set the type rule in the config.
3. Do one LOW-risk defect end to end with `/dev-ticket`, then tune the checklist and risk rules.
4. Later: optional smoke checks on staging after the ticket reaches QA Queue.

---

## Appendix A. Superseded skill and config text (moved 2026-09-30, cleanup)

_Moved verbatim out of `skills/dev-ticket/SKILL.md` and `dev_ticket.config.json` so the skill Claude
reads on every run stays lean. Kept here per the keep-drafts rule._

### From SKILL.md

> _Superseded 2026-09-29:_ these were informational ("don't block development; fix before DevOps
> Staging"). Now a hard gate before branch creation (D-12).

> _Superseded 2026-09-29:_ the earlier `DELIMITER $$` / `DROP FUNCTION IF EXISTS \`X\`;` / `END$$`
> function template matched only 1 of 140 repo functions. Replaced by the dominant form above (D-52).

> _Superseded 2026-09-29:_ "LOW → show the plan and continue without waiting". Replaced by the
> one-line tier confirmation (review issue 1).

> _Superseded 2026-09-29:_ 7b used the rules in `AI_DEV_ENVIRONMENT\skills\review-code\SKILL.md`.
> Replaced by the self-contained `REVIEW.md` (D-41).

> _Superseded 2026-09-29:_ step 5 used to offer "Move to Dev Done" and "…on to DevOps Staging".
> Replaced by the Code Review move (team decision, PLAN.md §5b).

S6e row: _(Superseded: `NO_BRANCH` → ask. Since D-21 the workflow creates the missing branch itself.)_

### From dev_ticket.config.json

```json
"allowed_status_moves_superseded_2026_09_29": [["Dev In Process", "Dev Done"], ["Dev Done", "DevOps Staging"]],  // replaced by D-2 / D-6
"work_dirs_superseded_2026_09_29": {"DEFECT": "DEFECTS", "CR": "CHANGE_REQUESTS"},  // replaced by D-31 (DEV_TICKETS)
"ui_checks": {"staging_url": "", "staging_url_note": "D-30: staging URLs differ per project - ask the developer, one project at a time; never guess."},
"db": {"sql_folders_note": "D-51: Database/<schema>/<folder>/; DDL_DML files are {D}.sql, {D}_1.sql, ..."}
```
