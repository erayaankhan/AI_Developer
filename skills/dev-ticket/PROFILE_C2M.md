# C2M CI profile — overrides for dev-ticket and review-and-deploy

Read this when `dt config-check` reports `"profile": "c2m"`, **and** the file it names in `repo_notes` (this repo's part,
e.g. `PROFILE_C2M_IMW2.md`). **Where these files and the skill differ, these files win** (the repo file over this one).
Everything not mentioned works as the skill says: the **status flow is the skill's own (original C2M) flow**.

## Where it runs

- One workflow folder, **`AI_Developer_C2M`**, serves every C2M CI repo (`C2M - GenAI`, `C2M - IMW2`, ...): they share
  the Assembla space, the status flow and the Jenkins `Code_Merge` job. Each repo links it as `AI_DEV_ENV`; the repo is
  recognised by its origin URL (`repos` in `python_scripts/profiles/c2m.json`). `{ROOT}` is the repo Claude is opened in;
  `{PY}` = `{ROOT}/AI_DEV_ENV/python_scripts`.
- `config-check` → `CONFIG_PROBLEM` "not one of the profile's repos" → this C2M repo is not set up yet: stop; the lead
  adds it to `repos` (and a `PROFILE_C2M_<KEY>.md`). Never work in a repo without its overlay: tickets of every Node would
  be offered.
- Work only in the repo Claude is opened in. Never touch the other C2M repos, `Launchpad- Config`, or a Launchpad repo
  from this ticket.
- There is **no DB repo** (`db.enabled: false`): skip every DB step (`DB.md`, `dt db-*`). A ticket that needs SQL or
  data-store changes outside this repo → stop and ask.
- macOS: use bash and **`python3`** wherever the skill says `python`. Ignore the skill's PowerShell / Windows-path
  instructions; forward slashes everywhere. The step about `D:\ProjectsGit\C2M_CommonServices\C2M_API` does not apply.
- Running the scripts' own test suite: `DEV_TICKET_PROFILE=none python3 -m unittest discover -s {PY} -p 'test_*.py'`.

## Tickets

- Assembla space `c2m-ci`, only tickets whose **`Node`** is this repo's (`repo_node` in config-check; `dt pick` refuses
  others: `WRONG_REPO` / `NOT_A_NODE_TICKET`).
- **Current milestones** are this repo's `current_milestones` (config-check shows them). Wherever the skill says
  "_CURRENT milestone", read "a milestone in `current_milestones`". When `NO_CURRENT_MILESTONE`, or the ticket is in
  another milestone, the lead updates that list in `profiles/c2m.json`; never guess.
- **Tickets start from Dev Queue** (`pick_from_queue`). `dt pick` resumes a ticket already in Dev In Process; with none,
  it offers your Dev Queue tickets: one → `PICKED` with `start_needed: true`; several → `CHOOSE_FROM_QUEUE`: show them, the
  developer picks, then `dt pick --ticket N`. `OTHER_IN_PROCESS` → another ticket of this repo is in progress: offer to
  pause or finish it first; never start a second one.
- **The start move (`start_needed`)**: the ticket stays in Dev Queue through step 2 and is moved to **Dev In Process
  right after step 3 creates or checks out the branch**: preview `dt status {N} --to "Dev In Process"`, show it, and on
  the developer's yes apply it with `--yes --confirm {H}`. Details missing at step 2 →
  `dt status {N} --to "Missing Implementation Details"`.
- The `ITsupport_Plasma` bot often creates `FB_{N}` on origin already ("branch has been created … Source branch:
  origin/master"). Step 3 then checks that branch out; it never makes a second one.
- Ticket types: `Category = Bug` / `Defect` → DEFECT (RCA / CLCA required); `Story`, `Task`, `Change Request (CR)`,
  `Change Request`, `New Change Request` → CR.
- Attributes: Plan level Story, this repo's `Node` and tag, and an active release Epic of that Node as parent (the repo
  file names them).

## Roles

Each repo has its own `reviewers` (config-check / `dt assign N --list`); everyone on them also writes code (a login may
be on only one list). Every author hands off (`HANDOFF.md`) and may assign it to **themselves** (`allow_self_review`):
then they run `/review-and-deploy N` on it like any review (the author folder waits as `DEV_TICKETS\{N}.author`;
`dt rework` puts it back after a reject). Ask the author which reviewer every time; never pick themselves by default.
The Teams post goes to this repo's channel (its own `TEAMS_WEBHOOK_URL_<REPO>` key in `credentials.env`).

## Deploy: DevOps' `Code_Merge` job

Staging deploy (LOW path step 5, reviewer R6: `staging_deploy.py deploy N …`) triggers the
Jenkins job **`Code_Merge`** on `JENKINS_REMOTE_BASE_URL` (jenkins-build-server.c2minternal.com), not the skill's
"C2MC - Merge and Deploy" job. All the skill's guards apply (approval and approved head, branch on origin = local,
ungated files, one trigger per ticket). How the job differs, and what to do:

- It has **no parameters** and also runs by itself **every 2 hours**. Each run takes **one** ticket in DevOps Staging
  from the **whole C2M CI space** (any Node), merges `FB_N` into `master` through an Assembla merge request, builds the
  apps the merge touched and ships them to staging. While it works the ticket stays in DevOps Staging; then IT Support
  moves it to **QA Queue**, or **Merge Failed / Build Failed** (the merge is reset).
- `SHARED_QUEUE` → other tickets are in DevOps Staging (e.g. other teams'). The run may take one of them instead. Show
  the list and say the timer would take them anyway; only on the reviewer's explicit yes re-run with
  `--accept-queue <token>`. Otherwise leave the ticket for the timer run and stop.
- `BUSY` → a Code_Merge build is running or queued: never trigger; wait, then check the ticket (that run may take it).
- `DEPLOYED` / `DEPLOYED_NO_APPS` → the ticket is in QA Queue; report the build number and the apps built.
- `DEPLOYED_OTHER_TICKET` → the run took someone else's ticket; ours still waits. Offer: wait for the timer, or (on yes)
  `deploy N --reviewer --retry --yes` (needs a new `--accept-queue` if others are still there).
- `NOT_PICKED` → the job skipped our ticket (its own rules, e.g. its milestone). Do not re-trigger; tell the reviewer
  to ask DevOps.
- `DEPLOY_FAILED` → name `failed_apps`; the reviewer checks the build's console in Jenkins and sends the ticket back if
  the code is at fault. **Never paste the Code_Merge console into Assembla or a file: it prints env files with secrets.**
- `staging_deploy.py queue N` (read-only) shows what is in DevOps Staging across the space.

## Sonar and Snyk

Not set up for C2M (`quality.enabled: false`): `dt quality` is not needed before hand-off. Turn it on per repo
once its Jenkins Sonar / Snyk jobs are known.
