# Launchpad profile — overrides for dev-ticket and review-and-deploy

Read this when `dt config-check` reports `"profile": "launchpad"`. **Where this file and the skill differ, this file wins.**
Everything not mentioned here works as the skill says.

## Where it runs

- Claude is opened in the **code repo** of the ticket: `Launchpad - CMGMT` (Node `CMGMT`) or `Launchpad - MAI-DC`
  (Node `MAI-DC`). `{ROOT}` is that repo; `{PY}` = `{ROOT}/AI_DEV_ENV/python_scripts` (a link to the shared `AI_Developer`).
- `Launchpad - CMGMT-DB` (Node `CMGMT-DB`) is the DB repo. It is never `{ROOT}`; it is reached through `dt db-*` and `git -C`.
- **Never touch `Launchpad- Config`** or any C2M repo.
- `dt config-check` → `CONFIG_PROBLEM`: show the problems and stop (e.g. Claude was opened in the wrong folder).
- macOS: use bash and **`python3`** wherever the skill says `python` (there is no `python` command). Ignore the skill's
  PowerShell / Windows-path instructions; forward slashes everywhere. The step about
  `D:\ProjectsGit\C2M_CommonServices\C2M_API` does not apply.
- Running the scripts' own test suite: `DEV_TICKET_PROFILE=none python3 -m unittest discover -s {PY} -p 'test_*.py'`
  (the tests expect the engine defaults, not this profile).

## Tickets

- Assembla space `Launchpad`. The current milestone is set by name in `python_scripts/profiles/launchpad.json`
  (`current_milestones`). Wherever the skill says "_CURRENT milestone", read "a milestone in `current_milestones`". When
  `NO_CURRENT_MILESTONE`, the lead updates that list; never guess a milestone.
- A requirement is a ticket with `Node = -NA-`. Each repo gets its own **Node ticket** (Story, Related to the
  requirement, same parent Epic). **The workflow only works Node tickets**; it never moves the requirement ticket (the
  BA / lead does).
- **Tickets start from Dev Queue** (`pick_from_queue`). `dt pick` resumes a ticket already in Dev in Process; with none,
  it offers your Dev Queue tickets in the current milestone: one → `PICKED` with `start_needed: true`; several →
  `CHOOSE_FROM_QUEUE`: show them, the developer picks, then `dt pick --ticket N`. `/dev-ticket N` on a Dev Queue ticket is
  accepted the same way. `OTHER_IN_PROCESS` → another of this repo's tickets is in progress: offer to pause or finish it
  first; never start a second one.
- **The start move (`start_needed`)**: the ticket stays in Dev Queue through step 2 (check) and is moved to
  **Dev in Process right after step 3 creates or checks out the branch**: preview `dt status {N} --to "Dev in Process"`,
  show it, and on the developer's yes apply it with `--yes --confirm {H}`. Ticket details missing at step 2 →
  `dt status {N} --to "Req Questions"` straight from Dev Queue (S6), not via Dev in Process. Rule S4 ("move it to Dev In
  Process") does not apply to a Dev Queue ticket: the workflow does that move itself, at step 3.
- `dt pick` only sees this repo's Node tickets. New refusals (stop, show the message, ask):

  | RESULT | Meaning |
  |---|---|
  | `WRONG_REPO` | The ticket's Node belongs to the other code repo: open Claude there. |
  | `NOT_A_NODE_TICKET` | Node is `-NA-` / empty (a requirement ticket) or unknown. Work its Node ticket instead. |
  | `DB_TICKET` | A `CMGMT-DB` ticket: worked together with its code ticket (below), never picked on its own. |

- Ticket types: `Category = Bug` → DEFECT (RCA / CLCA required); `Change Request (CR)` / `Requirement` → CR.
- Attributes: Plan level Story, `Node` = this repo's Node, an active Epic parent. There is **no tag** to set.

## Status names (the skill's words → Launchpad)

| Skill says | Launchpad status | Who moves it |
|---|---|---|
| Dev Queue | Dev Queue | developer |
| Dev In Process | **Dev in Process** | developer |
| Missing Implementation Details | **Req Questions** | developer |
| (new) code committed | **Dev Done** | `dt status N --to "Dev Code Review"` passes through it automatically (RESULT `via`) |
| Code Review | **Dev Code Review** | developer, then `dt assign` to a reviewer |
| Dev Done (= approved) | — none: approval goes straight to **DevOps Staging** | reviewer (`dt review-verdict --approve`) |
| DevOps Staging | DevOps Staging | the approval itself |
| QA Queue | **Dev Review Staging**, then **QA Queue Staging** | Jenkins, then the reviewer |
| sent back | **Dev in Process**, assigned to the author | reviewer (`dt review-verdict --reject`) |

## Hand-off (step 8): everyone hands off to a reviewer

- **Every author follows `HANDOFF.md`, whatever `state.role` says** — a reviewer writing code too. A reviewer author
  may assign it to **themselves** (`allow_self_review`): then they run `/review-and-deploy N` on it like any
  review (the author folder waits as `DEV_TICKETS\{N}.author`; `dt rework` puts it back after a reject). Ask the
  author which reviewer every time; never pick themselves by default.
- There is **no LOW path** and no "deploy on request": ignore `LOW_PATH.md` and the deploy parts of `STANDARD_PATH.md`.
  Risk tiers still decide the checks; every tier ends in Dev Code Review, assigned to a reviewer.
- Commit messages: the team's convention `re #{N} {summary}` in the code repo, `re #{D} {summary}` in the DB repo (not the
  skill's `re#{N} {CAT} | …` / `re#{D} - {N} - …`). The DB branch `FB_{D}` belongs to one DB ticket, so `dt db-files`
  counts every commit on it since `db.base`.

## DB changes (replaces `DB.md` "Files and templates"; the rest of `DB.md` applies)

- The DB ticket is a separate Node ticket (`Node = CMGMT-DB`), Related to the **requirement** ticket. `dt db-ticket {N}`
  finds it through the requirement (`via_requirement` in the RESULT). None found → `dt db-create {N} --title "Story: Database
  changes for <requirement, e.g. LAUNCH-4257>"`: it links the new ticket to N and to the requirement.
- Layout is **flat**: `Database/<schema>/<file>.sql` (schemas `C2M`, `C2M_Log`, `C2M_OAuth`, `timeline`). One file may
  hold several procedures. Name new files after the ticket, `LAUNCH-{N}-<short-slug>.sql` (the requirement's number), or
  after the object for a single procedure (`OT_DM_API_<Name>.sql`).
- The SQL is MySQL: keep the template of `DB.md` (`DROP PROCEDURE IF EXISTS`, `DELIMITER $$`, no `DEFINER`, no `USE`),
  re-runnable DDL/DML. `--normalize` and the per-folder naming errors do not apply to the flat layout; the destructive-
  statement stop, the hygiene checks and the "C# calls an SP that exists nowhere" check (`RunSp*`) still run.
- Manual or one-off scripts (backfill, rollback, verify) stay out of `Database/` so a deploy never runs them.

## Gates

- .NET: `dotnet build` / `dotnet test` for changed projects. Angular (`ng test`) for the configured apps:
  CMGMT `AIStudioWebApp`, `DeviceAdminMgmtWebApp`, `DeviceCertificationWebApp`, `DeviceMgmtBetaWebApp`, `DeviceMgmtWebApp`;
  MAI-DC `launchpad-admin-portal/frontend`, `line-management-mfe`.
- **Maven**: `mvn -q -B test` in each changed Maven module (`maven-test:<module>` gate; `--skip maven-test` only with the
  developer's yes and a reason in SUMMARY.md).
- No pre-commit config in these repos: that gate does not run.

## Functional checks (step 7d)

There is no module-federation host. To check a UI change, serve the app itself (`ng serve` in its folder, e.g.
`DeviceMgmtWebApp`, on `http://localhost:4200`) and check it in Chrome; the developer signs in. If that is not practical,
`dt func-mode {N} skip --reason "…" --confirmed` on the developer's yes (it shows in SUMMARY.md).

## Sonar and Snyk (`quality`)

- Jenkins (jenkins-build-server-3) builds each app after `Code_Merge_Launchpad_*` merges it. DeviceMgmtWebApp and
  DeviceMgmtAPI (CMGMT) and nine MAI-DC apps (all but launchpad-admin-portal; see `quality.apps` in the profile) run the
  SonarQube quality gate, which fails on **any** new issue, and most also run `snyk test` (CMGMT medium; MAI-DC mostly
  **low**, launchpad-api high: `snyk.severity_threshold`). A failure → **Build Failed**, merge reset.
- Developer: `dt quality {N}` is the last step before hand-off (`HANDOFF.md` step c2). Blocking: High / Medium Sonar issues on
  the changed lines, fixable Snyk vulnerabilities (team decision). MAI-DC's Java apps scan with Maven (`mvn compile` + the Sonar plugin,
  tests skipped), so they need the company parent `c2m-starter-parent` from `~/.m2/settings.xml`; a resolve error there
  is `SCAN_FAILED`: the developer's Maven settings, not the code.
- Reviewer / lead: `dt quality-bounce` (preview) lists the Build Failed tickets and why (Sonar, Snyk, or another cause, read
  from the app's Jenkins build). Show the table; only on the yes run `dt quality-bounce --yes --confirm {H}`: it posts the
  reasons and moves each Sonar / Snyk one to **Dev Queue**, assigned to its hand-off author. `NOT_QUALITY` (compile, tests,
  deploy) is never bounced: the author checks Jenkins. `--ticket N` limits it to one ticket.

## Reviewer (`/review-and-deploy`)

- Opened in the ticket's code repo too (`review-pick` lists only this repo's tickets; `WRONG_REPO` otherwise).
- **R5 approve = deploy request.** `dt review-verdict --approve` moves the ticket from Dev Code Review straight to
  **DevOps Staging** (the DB ticket follows if it is still with it). The approval is bound to the reviewed head as usual.
- **R6 does not apply**: do not run `dt review-deploy` or `staging_deploy.py` (it answers `FEATURE_OFF`). Tell the
  reviewer: the Jenkins deploy job picks the ticket up on its 30-minute run (the merge job can be started by hand in
  Jenkins) and moves it to **Dev Review Staging** when deployed. `Merge Failed` / `Build Failed` → the reviewer checks
  Jenkins and sends it back (Dev in Process) if the code is at fault.
- `dt review-withdraw N` still works while the ticket is in DevOps Staging and Jenkins has not taken it yet.
- **Staging review**: once the ticket is in Dev Review Staging, the reviewer checks it on staging and moves it to
  **QA Queue Staging** in Assembla (no `dt` command for this step yet).
