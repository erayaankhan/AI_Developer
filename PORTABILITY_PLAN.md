# Portability plan: hand the dev-ticket / review-and-deploy workflow to other teams

Status: **initial draft, 2026-10-07, not started.** Early discussion; this is the single working version, edited in place.

## 1. Goal

Another team, with its own Assembla space and its own Jenkins, can take `AI_DEV_ENV` and run `/dev-ticket` and
`/review-and-deploy` by filling in **one team profile file** and their own credentials, without editing Python or the
skills. The C2M team's behaviour must not change.

Out of scope for v1: changing the workflow itself (stages, roles, hand-off protocol), a non-Assembla tracker, a non-git
VCS, a GUI.

## 2. Where C2M is baked in today (inventory, 2026-10-07)

| Area | Where | Notes |
|---|---|---|
| Assembla hosts | `assembla_api.py:36-37`, duplicated in `deploy_staging_local.py:91-92` | `api.assembla.com` is the same for everyone; keep one constant. |
| Assembla space | `assembla_api.py:38` `DEFAULT_SPACE = "c2m-ci"`; override `ASSEMBLA_SPACE` in `credentials.env` | The default is C2M's. |
| Jenkins build server | `deploy_staging_local.py:94`: a C2M internal hostname as the **default** of `JENKINS_REMOTE_BASE_URL` | Must never be a source default. |
| Jenkins job names | `deploy_staging_local.py:71,96` (`deploy_staging_local`, `C2MC - Merge and Deploy Feature Branch`); `dev_ticket.config.json` `staging_deploy.job` | Job parameters and console format are C2M's. |
| Staging app URLs | `deploy_staging_local.py:167-169` | |
| Deploy script | `deploy_staging_local.py` (1,578 lines), `staging_deploy.py` (580 lines) | Deeply tied to C2M's merge-and-deploy job, its downstream builds and its result CSV. **The hardest part.** |
| Workflow vocabulary | `dev_ticket.config.json`: `statuses`, `*_status_moves`, `staging_deploy.done_status/merged_status/failed_statuses`, `metrics.*`, `epics.inactive_statuses` | Status names of C2M's Assembla workflow. |
| People | `reviewers`, `developers` (Assembla logins) | Personal data in a committed file. |
| Ticket model | `ticket_type_rules` (field `Category`), `deploy_attributes` (`Node`, `WF`, plan level, tag), `followup.module_field` (`C2M-Module`), `rca_clca`, `db.node_value` | Custom fields of C2M's space. |
| Repo layout and stack | `staging_deploy.app_folders`, `db.*`, `functional_checks.*`, `ui_checks.*`, `local_only_patterns`, `risk_check.*` | Paths, serve commands, AFD3 / gateway risk patterns. |
| Other repos on the machine | `config_loader.py` + `dev_ticket.config.json`: `db.repo_path`, `ui_checks.host_repo_path`; defaults assume the clones sit **next to `C2M_WF`** (`..\C2M_CI_DB`, `..\C2M_CommonServices\C2M_Console_Angular_Beta`); `SETUP.md` step 2 and 3; `.claude/settings.local.json` `additionalDirectories` | The **repo names, sibling-folder assumption and drive/path habits are C2M's**. A team that clones elsewhere, names the DB repo differently, or has no DB repo gets a wrong default or a silent miss. Claude can't read a repo that is not in `additionalDirectories`, and nothing checks that. |
| Layout inside those repos | `skills/dev-ticket/DB.md` (`Database\<schema>\`, `Database\C2M\SP\`, file-name rules), `db.*`, `db_checks.py`; branch naming `FB_{n}` and the `re#{D} - {N} - summary` commit format; "milestone DB Story" model (D-18) | Folder names, schema name and branch/commit conventions are C2M's. |
| Machine-specific paths in docs and notes | `skills/**/*.md`, `SETUP.md`, `PLAN.md` examples such as `E:/code/...`, `D:\ProjectsGit\...` | Examples, not behaviour, but a receiving team copies them. Count in Phase 0. |
| Hard-coded absolute paths found 2026-10-07 | `skills/dev-ticket/SKILL.md:46` and `python_scripts/local_env.env.example:39` (`D:\ProjectsGit\C2M_CommonServices\C2M_API`, the `c2mapi` service cwd); `PLAN.md` D-29 (UI host path); `.claude/settings.local.json` (`python D:\ProjectsGit\C2M_CI_WF\AI_DEV_ENV\*` allow rules; git-ignored, so harmless to others, but the shared `settings.json` must use relative patterns) | The `c2mapi` service is a **third repo** with no profile key yet: add it to the `repos` map. |
| DB / UI path resolved in several places | `(SCRIPT_DIR / CONFIG["db"]["repo_path"]).resolve()` is repeated in `develop_ticket/common.py:129`, `db_checks.py:69`, `risk_check.py:172`, `review_aid.py:94`; the UI path in `functional_cmds.py:31,144` | Five copies of the same lookup. Phase 1 gives them one `repo_path(key)` function, so the resolution order and the "not found" message live in one place. |
| Gates | `local_gates.py`: pre-commit, `dotnet build/test`, `ng test`, db-checks hard-coded | Stack-specific. |
| Skills text | `skills/**/*.md`, `SETUP.md` use the literal status names, folders and commands | Count to be taken in Phase 0. |
| Unused keys | `JENKINS_USER`, `JENKINS_API_TOKEN`, `JENKINS_BASE_URL` (`credentials.py`), `staging_deploy.jenkins_base_url` | Look unused; confirm in Phase 0, then remove. |

## 3. Design

**Three config layers, last wins** (the loader already merges the third over the first):

1. **Engine defaults**: `dev_ticket.config.json`, neutral values only, no team names, hosts or logins.
2. **Team profile** (new, committed per team): `profiles/<team>.json`. Everything in section 2 that is a team choice. Picked
   by one setting (`DEV_TICKET_PROFILE`, or a `profile` key in the developer file).
3. **Developer file**: `dev_ticket.config.local.json` (exists, git-ignored): paths on this machine.

Credentials stay out of all three, in `credentials.env` as today.

**What goes in which layer for other repos**
- **Profile (team):** that a repo exists and what it is called (`repos.db.name`), its layout (schema, folders, file-name
  rules), branch and commit-message conventions, and the feature switch. Never an absolute path.
- **Developer file (machine):** only `repos.<key>.path`. Absolute, or relative to `python_scripts`.
- **Resolution order for a path:** developer file, then `DEV_TICKET_<KEY>_REPO` environment variable, then the **sibling
  folder of the WF clone named in the profile**, then stop. A missing path never falls back to a guessed drive or user folder.
- **Claude's read access** is derived from the same paths: `dt config-check` prints the exact `additionalDirectories`
  lines to add to `.claude/settings.local.json` and reports any repo path that is not listed. Nothing in the repo
  hard-codes a drive letter.
- If `features.db` is off, no DB path is needed and the DB steps are skipped.

**Rules**
- No team value as a source default. A missing required value stops with a message that names the key and the SETUP section.
- One definition per value (the duplicate Assembla host goes).
- **C2M stays the reference profile** (`profiles/c2m.json`), selected by default for now, so nothing changes for this team.
  Proof: the merged config for C2M equals today's `dev_ticket.config.json` key for key (a test).
- Optional features are switched in the profile (`features`): Jenkins staging deploy, DB repo, UI checks. A feature that is
  off makes its `dt` commands answer `FEATURE_OFF` and the skills skip its steps.
- **Deploy becomes an adapter**, not a config tweak: the workflow talks to a small interface (trigger, wait, report result,
  map result to a ticket status). C2M's current scripts are the first adapter; another team writes its own or turns the
  feature off. Do not try to parametrise `deploy_staging_local.py`.
- **Skills name statuses by role** ("the Code Review status"), with one glossary table that points at the profile, so a
  rename in the profile needs no edit across the markdown.

## 4. Phases

Each phase is shippable alone, and ends with the full script test suite passing.

**Phase 0: decisions and inventory (small)**
- Settle the open questions in section 6.
- Count and list every team literal in `skills/**/*.md`, `SETUP.md`, scripts and config (a script, kept as a test later).
- Confirm which Jenkins keys are unused.
- List every repo the scripts and skills touch besides the WF repo (DB, UI host, any other), where each default path comes
  from, and every absolute or drive-letter path in docs and examples.
- Check the Assembla API endpoints for statuses, custom fields, users and milestones against its documentation (needed in Phase 4).

**Phase 1: config layering (medium)**
- Loader: engine defaults, then profile, then developer file; profile selection; clear error for an unknown profile.
- `dt config-check`: required keys, types, status graph consistency (every move uses a known status), reviewers/developers present.
  **Repos:** each enabled repo path exists, is a git repo, has the expected remote/layout folder, and is covered by
  `additionalDirectories`; prints the lines to add when it is not.
- Generalise `db.repo_path` / `ui_checks.host_repo_path` into one `repos.<key>.path` map (old keys still read, so C2M's
  local file keeps working).
- Move today's values into `profiles/c2m.json`; leave neutral defaults in the engine file.
- Tests: merge order, equality with today's config for C2M, validation errors.

**Phase 2: endpoints and names out of Python (small to medium)**
- Space, Jenkins base URL, job names, staging URL map move to the profile or `credentials.env`; no C2M default left in source.
- Single Assembla host constant. Remove the unused Jenkins keys.
- Repo names, DB layout (schema, folders), branch and commit-format templates move from `DB.md`/`db_checks.py` literals into the profile.
- Tests: a missing value gives the message, not a C2M fallback.

**Phase 3: optional features and the deploy adapter (large)**
- `features` flags and `FEATURE_OFF`; skills skip the steps of an off feature.
- Define the deploy adapter interface; wrap today's scripts as the C2M adapter; the hand-off, review and approval flow only talks to the interface.
- Gates become configurable (commands per file pattern); C2M's dotnet/ng set moves into its profile.
- Tests: adapter contract with a fake adapter; a profile with deploy off runs the whole flow to approval.

**Phase 4: onboarding command (medium)**
- `dt init-team`: **read-only** calls to Assembla (statuses, custom fields, users, milestones) and Jenkins (reachable, jobs exist);
  writes a profile skeleton and prints a gap report (a status the workflow needs that the space lacks, a missing field,
  an unknown login). Never writes to Assembla or Jenkins. Also asks where the team's other repos are cloned, writes
  the developer file, and prints the `additionalDirectories` block for `.claude/settings.local.json`.
- Tests with a fake API.

**Phase 5: skills and docs (medium, many small edits)**
- Skills rewritten to role names plus the glossary; a test fails if a profile's team literal appears in an engine file.
- Drive-letter and user-folder paths in skills, `SETUP.md` and examples replaced by placeholders (`<your path>`), guarded by a test.
- `SETUP.md` portability section; a short handover README for the receiving team (what to fill in, what not to touch,
  how to update the engine); migration note for C2M.
- Dry run: a second, throw-away profile against a sandbox Assembla space, end to end up to approval.

## 5. Acceptance

- The C2M team's effective config, results and skill behaviour are unchanged (equality test plus the full suite).
- A new team reaches "pick, fetch, gates, hand-off, review, approve" using only its profile and credentials.
- `dt init-team` reports a wrong status name or custom field before the first real ticket does.
- No team host, job name, login or space remains in an engine file (test).
- No drive letter, user folder or machine path remains in an engine file, skill or doc (test). A developer whose DB or UI
  repo is anywhere on disk sets one path and `dt config-check` confirms it, including Claude's read access.

## 6. Open questions (answers change the plan)

1. Will other teams use a stack other than .NET and Angular? (Decides how far gates are configurable in Phase 3.)
2. Must Jenkins staging deploy stay supported for every team, or may it be off? (Decides adapter depth.)
3. Profile storage: one committed profile per team in this repo, or each team's own copy of `AI_DEV_ENV`? (Decides profile
   selection and how updates reach the teams.)
4. Do other teams keep C2M's role model (developer hands off, reviewer approves and deploys)? (If not, roles become config.)
5. Is `deploy_staging_local.py`'s result CSV or its database a C2M-only contract, or does any other team need it?
6. Do other teams have a separate DB repo and a UI host repo at all, and are the repos always cloned side by side? (Decides
   whether the sibling-folder fallback stays or every path must be explicit.)
7. Do other teams use the same DB layout and the "one DB ticket per milestone" model, or does `features.db` stay off for them?

## 7. Risks

- **The deploy scripts are the bulk of the work**, not the config. Plan Phase 3 as a refactor with an adapter boundary.
- Wording edits across the skills are error-prone; the glossary and the banned-literal test are the guard.
- Assembla workflows differ in meaning, not only in names (for example another team's "Code Review" may have no
  reviewer assignment). `dt config-check` can verify names, not intent.
- Anything that changes shared behaviour affects the C2M team: keep the C2M profile equality test green at every step.
- A stale profile after an engine update: version the profile schema and make `dt config-check` report the version.

## 8. Rough order and size

Phase 0 (S) -> 1 (M) -> 2 (S/M) -> 4 (M) -> 3 (L) -> 5 (M). Phases 1, 2 and 4 already let a team with a normal Jenkins set up
the basics; 3 is needed for a team with a different deploy; 5 can run alongside.
