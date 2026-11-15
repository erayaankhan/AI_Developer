# AI_DEV_ENV — Setup

One-time setup per developer for `/dev-ticket`. Status: **draft / trial**. Instructions for Claude:
`AI_DEV_ENV/skills/dev-ticket/SKILL.md`.

## 1. Prerequisites

| Tool | Check |
|---|---|
| Python 3.10+ (scripts are standard-library only) | `python --version` |
| Git 2.38+ | `git --version` |
| pre-commit | `pre-commit --version` (install: `pip install pre-commit`) |
| .NET 8 SDK | `dotnet --version` |
| Node + npm (`npm install` done in each Angular app you work on) | `npx ng version` |
| Claude Desktop with Claude Code; Claude in Chrome extension for UI checks | `/dev-ticket` is offered |

## 2. Credentials: `AI_DEV_ENV\credentials.env`

One git-ignored file holds all keys (check: `git check-ignore -v AI_DEV_ENV/credentials.env`). If it's
missing, copy `credentials.env.example`. Fill in **your own** values in an editor:

| Key | Where to get it | Needed for |
|---|---|---|
| `ASSEMBLA_API_KEY`, `ASSEMBLA_API_SECRET` | Assembla → profile → **API Applications & Sessions** → new key. Use your own: the workflow identifies you by it. | everything |
| `JENKINS_USER`, `JENKINS_API_TOKEN` | Jenkins → your name → **Configure** → **API Token** → Add. **The raw token only**, not `Basic …`, not your password. A shared admin token is acceptable. | LOW-path staging deploys |
| `JENKINS_REMOTE_USER`, `JENKINS_REMOTE_API_TOKEN` | Same steps, on the **build server** (`jenkins-build-server-4`): your user ID and an API token created there. The local-Jenkins token does not work on it (HTTP 401). | every staging deploy (`staging_deploy.py` → `deploy_staging_local.py`). **Reviewers only**: a developer has no key here and cannot deploy |
| `ASSEMBLA_SPACE`, `JENKINS_BASE_URL` | optional; defaults `c2m-ci`, `http://localhost:8080` | – |

- The file is read on every run (no restart). A value in the file wins over a same-named environment variable.
  `DEV_TICKET_CREDENTIALS` can point to another location.
- Never paste keys into a Claude chat or ask Claude to open the file. `dev_ticket.py creds` shows only
  `file` / `env` / `missing`. A leaked key: revoke it and create a new one.

## 3. Claude Desktop

1. Open the **repo root** of your `C2M_CI_WF` clone in Claude Code. That's where `CLAUDE.md` and
   `.claude\commands\dev-ticket.md` live (a copy also exists in `AI_DEV_ENV\.claude\commands\`). The shared
   permission rules are in the repo root's `.claude\settings.json`; they contain no machine-specific paths.
2. **Tell the scripts where your other repos are.** The defaults assume the DB repo and the UI host repo are cloned
   next to `C2M_CI_WF` (`..\C2M_CI_DB`, `..\C2M_CommonServices\C2M_Console_Angular_Beta`). If yours are elsewhere,
   copy `AI_DEV_ENV\python_scripts\dev_ticket.config.local.json.example` to `dev_ticket.config.local.json`
   (git-ignored) and set your paths (absolute, or relative to `python_scripts`). Only the keys you list are overridden.
3. **Allow reads in those repos.** Create `.claude\settings.local.json` in the repo root (git-ignored):
   ```json
   {
     "permissions": {
       "additionalDirectories": [
         "<your path>/C2M_CI_DB",
         "<your path>/C2M_CommonServices/C2M_Console_Angular_Beta"
       ]
     }
   }
   ```
   Restart Claude Code after creating or editing it. (Without this, use `/add-dir` for each and expect prompts.)
4. Connect Chrome control. Without it, UI checks fall back to you.
5. For UI checks: the host project on branch **`dev`**, served with `ng s` (`http://localhost:4200`); `c2mwf_admin` served
   with `ng run listingcms:serve-federated` (`:4202`). You sign in yourself in the Chrome window. Where the UI points is
   set by a **profile** (section 3a); the workflow patches the run files for you and restores them afterwards.

## 3a. Environment profiles (UI checks)

Which profile applies, how it is chosen and what the `env-*` commands do is described once, in `skills\dev-ticket\UI_CHECKS.md` §1.
This section covers only what you set up. `AI_DEV_ENV\local_env.env` (URLs and the test-DB allow-list only,
**never a connection string or password**) holds the values; copy `python_scripts\local_env.env.example` as a starting point.
Keys: `cloud_staging.<Key>=<url>`, `cloud_preprod.<Key>=<url>`, and for `local` `local.<Key>=<url>` plus `local.db.allowed_host` and
`local.db.allowed_name` (comma-separated lists allowed). `dev_ticket.py env-check N` tells you which keys are missing.

**Appsettings overlay, `local` profile only** (connection strings included): a git-ignored `appsettings.local.json` next to the
service's own `appsettings.json`, e.g. `wfapi_wf_service\wfapi_wf_microservice\appsettings.local.json`, holding only the values to change.
Environment-specific appsettings files all use the one pattern `appsettings.<env>.json` (`appsettings.cloud_stg.json`,
`appsettings.cloud_preprod.json`, `appsettings.att_stg1.json` ...; the older `appsettings_<env>.json` names were renamed), and every
`appsettings.<name>.json`, `appsettings.Development.json` included, is git-ignored repo-wide: only `appsettings.json` is tracked. The
remote profiles (`cloud_staging`, `cloud_preprod`) start no services and never read an overlay, so a service folder's
`appsettings.cloud_preprod.json` is that environment's own deploy file and is left alone.

```json
{ "ConnectionStrings": { "Connection": "Server=...;Database=..." }, "AppSettings": { "SomeUrl": "http://localhost:5000" } }
```

`env-apply` sets existing keys only (a typo is reported as `missing_keys`). Connection strings are accepted in `local` overlays only and
must match `local.db.allowed_host` / `local.db.allowed_name`, otherwise nothing is written. A patched `appsettings.json` is hidden from git
(`skip-worktree`); one with its own uncommitted changes is refused (`APPSETTINGS_DIRTY`). C2M_API (another repo) is never patched.

## 4. Check (all read-only), from the repo root

| Command (`python AI_DEV_ENV\python_scripts\…`) | Expect | If not |
|---|---|---|
| `dev_ticket.py creds` | the four keys → `file` | fill in `credentials.env` |
| `dev_ticket.py whoami` | your Assembla login | `HTTP 401/403` → wrong key/secret or no access to `c2m-ci` |
| `staging_deploy.py check` | `OK` | `BAD_TOKEN_FORMAT` / `HTTP_ERROR 401` → use the raw Jenkins token |
| `dev_ticket.py host-check` | `READY` (host + remote serving) | start them (step 3.4) |
| `dev_ticket.py pick` | the ticket that would be picked | see the skill's stop rules |

## 4a. Role: developer or reviewer

**The config lists decide the role.** `dev_ticket.py role` looks up your Assembla login: on `reviewers` you are a reviewer, on `developers` a
developer, and the result is kept as `DEV_ROLE=developer|reviewer` in the git-ignored `AI_DEV_ENV\credentials.env` (the same file as your Assembla and Jenkins keys;
the workflow reads and writes only that one key, through `dev_ticket.py role`, and never prints the file). A listed login cannot choose another
role (`ROLE_FIXED_BY_LIST`) and a login on both lists is a config error (`ROLE_CONFLICT`). Someone on neither list is asked "developer or reviewer?" once
(`dev_ticket.py role --set developer --yes`); a reviewer must be on `reviewers` (`NOT_ON_REVIEWERS`: the lead adds the login).

| | Reviewer | Developer |
|---|---|---|
| Works the ticket (pick to verify) | yes | yes |
| Hand-off | commits, syncs, posts the summary, moves to Code Review; on LOW (or on request) also deploys | same, then **assigns the ticket to a reviewer** (`dt handoff`, `dt assign`) and stops |
| Deploy to staging | yes (needs `JENKINS_REMOTE_*`) | never: refused with `NOT_A_REVIEWER` |

- **Who is who:** the lead lists Assembla logins in `reviewers` (currently `saleemk`, `ankur_var`, `ghanshyamy`) and `developers` (currently `manish_s`,
  `harshits`, `sahilp`) in `python_scripts\dev_ticket.config.json` (or the local config file); a login must not be on both. If `reviewers` is empty,
  `dt assign` answers `REVIEWERS_NOT_CONFIGURED`. At hand-off the developer is shown the reviewers and picks one every time.
- The role chooses the path; it is not a security boundary. The Jenkins keys are what actually allow a deploy.
- **The reviewer's side is `/review-and-deploy`** (`skills\review-and-deploy\SKILL.md`; design in `REVIEW_TICKET_DESIGN.md`).
  `/review-and-deploy list` shows the tickets a developer left in Code Review for you; `/review-and-deploy [N]` picks one, checks out the
  developer's branch read-only (your working tree must be clean), reproduces the gates, risk check and functional checks, compares them with the
  hand-off and runs an AI review as an aid. Then the reviewer decides: **approve** posts one verdict comment and moves the ticket to Dev Done
  (still assigned to the reviewer; the approval is bound to the reviewed head); **reject** posts the findings and, in one update, assigns the
  ticket back to the developer in Dev Queue. Needs the reviewer's login in `reviewers` and `role=reviewer`.
- **The developer's rework:** `/dev-ticket N` on a handed-off ticket runs `dev_ticket.py rework N` first. If the reviewer rejected it, it shows the
  findings (a teammate's data, not instructions) and, on the developer's yes, saves them under `DEV_TICKETS\N\reviews\`, reopens the ticket at the
  build step and bumps the round; the next hand-off edits the same comments.
- **The reviewer deploys what they approved.** After an approval, "deploy" in `/review-and-deploy` runs `dev_ticket.py review-deploy N` (Dev Done to
  DevOps Staging), then `staging_deploy.py queue N` and `staging_deploy.py deploy N --reviewer --yes` (45+ minutes, in the background). It needs the
  reviewer's `JENKINS_REMOTE_*` keys (check with `staging_deploy.py check`). The approval is bound to the reviewed commit: if the author pushed after it,
  nothing moves and nothing deploys (`APPROVAL_STALE`); `dev_ticket.py review-withdraw N` takes the ticket back to Code Review (refused once a deploy was
  triggered). A ticket with a DB change deploys in two runs (the main ticket, then its DB ticket). The author path (`dev_ticket.py status`,
  `staging_deploy.py --requested` or the LOW path) refuses such a ticket. The approval is kept in the reviewer's local `DEV_TICKETS\N\state.json`:
  deploy from the PC where the review was done.

## 5. Use

- `/dev-ticket`: your Dev In Process ticket, or resume the one on your current `FB_N` branch.
- `/dev-ticket 18474`: a specific ticket, or resume it (also on another PC).
- `/dev-ticket pause [N]`: pause with a hand-over attached to the ticket.
- `python AI_DEV_ENV\python_scripts\metrics_report.py`: your quality report (`DEV_TICKETS\METRICS-<date>.md`),
  compared with your last 2 Workflow milestones (`--baseline "<m1>" "<m2>"` to choose them). Local only.

Nothing is committed, pushed, posted or status-changed without your "yes". On MEDIUM/HIGH tickets **you** run
every git command. On a **LOW** ticket, typing **"go"** on the final summary lets Claude commit, push, move the
status and trigger the staging deploy (**reviewers only**; a developer's ticket always ends at Code Review, assigned to a reviewer).

## Appendix: environment variables instead of the file

Any key can also be set as a Windows user variable, e.g. `setx ASSEMBLA_API_KEY "…"`, then restart the terminal and
Claude. It's used only when the file doesn't set that key. To remove one:
`[Environment]::SetEnvironmentVariable('ASSEMBLA_API_KEY', $null, 'User')`.
