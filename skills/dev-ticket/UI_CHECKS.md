# dev-ticket — functional checks (step 7d)

Read at step 7d when the ticket's functional-checks mode is `run` (the developer chose it at step 5: `dt func-mode`).
Mode `skip` has nothing to run; `later` is asked again first. There are no extra staging checks after a deploy. `dt` = `python {PY}/dev_ticket.py`. Functionality is verified by running the app
in Chrome (Claude Desktop), including API / backend changes: they are checked through the Angular screen that calls
them, never by a direct API or DB call. No Chrome control → list the checks for the developer to do by hand and wait
for their results.

## Who runs them

A **fresh sub-agent** (not the thread that wrote the code, and without the diff) runs the `FC-n` steps from `PLAN.md`
and the acceptance criteria, and writes `reviews\functional-checks.md` (per check: PASS/FAIL, what was seen, a
screenshot). The main thread then records the outcome: `dt func-record {N} --passed X --failed Y` (every planned check
must be reported, a lettered one such as `FC-5b` counting as its own; `COUNT_MISMATCH` otherwise). The code reviewer (7e) reads `functional-checks.md`.
FAIL → fix, re-run 7a and the check, new sub-agent. Max **2** rounds, then S9 (the developer may grant one more round; each round past the cap needs their explicit yes). Log `dt event {N} ui_checks where=local
passed=… failed=…`. Any later code change makes the record stale (`FUNCTIONAL_STALE`): re-run. Documentation files (`.md`, `.txt`, `.doc`, `.docx`, `.rtf`, `.pdf`, or
anything under a `docs/` folder) and comment-only lines (a full-line `//`, `--` or `#` comment, or a block comment on its own lines) do not
count, in this record and in the gates and risk records alike; a comment after code on the same line does.

**No UI path** (a job, a consumer, an SP no screen uses, docs-only): first look for a screen that shows the effect.
None → recommend `skip` with that as the reason; only the developer's yes records it
(`dt func-mode {N} skip --reason "<reason>" --confirmed`).

## 1 · Profile (decided by `dt profile {N}`, never by judgement)

| Profile | When | Where the UI points | Data |
|---|---|---|---|
| `cloud_staging` | every changed file is Angular code (the default of the two) | the staging APIs (`cloud_staging.*` keys) | **shared**: see staging rules |
| `cloud_preprod` | every changed file is Angular code, and the developer chose Preprod | the Preprod APIs (`cloud_preprod.*` keys) | **shared**: same rules as staging |
| `local` | anything else, or a mix (API / DB changes) | local gateway / runtime API (`local.*` keys) and the **test DB** | the test DB only |

For an Angular-only change `dt profile {N}` returns `needs_choice` with `choose_between: [staging, preprod]`: **ask the
developer which one** (recommend staging), then `dt profile {N} --choose cloud_staging|cloud_preprod`. A backend or DB change is
always `local` (`CHOICE_NOT_ALLOWED` otherwise). `--force` (e.g. staging down) only on the developer's say-so. Then
`dt env-check {N}` (it also lists every profile's key count and appsettings overlays): `ENV_FILE_MISSING` /
`ENV_PROBLEMS` / `OVERLAY_INVALID` → tell the developer which **keys** are missing (never ask for values in chat; they
edit `AI_DEV_ENV\local_env.env` and the git-ignored `appsettings.<profile>.json` next to each service's `appsettings.json`).
`dt env-apply {N}` snapshots then patches the run files: the profile's URLs into **every Angular app's** `environment.ts` (including the host application's, in its own repo:
`src/environments/environment.ts` under `ui_checks.host_repo_path`; its snapshot is restored the same way),
and, for the `local` profile only, the overlay values (`appsettings.local.json`, connection strings included, checked against the test-DB allow-list) into the
services' `appsettings.json` (existing keys only; `missing_keys` lists typos). Never stage them: patched `appsettings.json`
files are hidden from git. **The originals are put back automatically**: `dt func-record` restores them as soon as it has
recorded a PASS (a FAIL keeps them patched for the next round, so a re-run needs no new `env-apply`), and `hand-off` / `review-compare` restore any that are still patched before
their checks. Run `dt env-restore {N}` by hand only to put them back earlier. If a snapshot is missing (`RESTORE_INCOMPLETE`,
`env_restore_incomplete`) that file stays recorded and hand-off is refused (`ENV_PATCHED`): `git checkout -- <file>`, then `dt env-restore {N}`. An `appsettings.json` with its own uncommitted changes is refused (`APPSETTINGS_DIRTY`).

## 2 · Servers

`dt host-check` first (host project on its branch, host and remote ports serving).

| RESULT | Action |
|---|---|
| `READY` | continue |
| `HOST_WRONG_BRANCH` | the host project must be on `expected_branch`: ask before switching |
| `NOT_SERVING` | **Ask first:** "start them for you, or will you start them from VS Code?" If they start them, wait and re-check; never start a second copy. If you start them: **background, PowerShell, `npx`**: host `npx ng s` (`start_host`), remote `npx ng run listingcms:serve-federated` in `c2mwf_admin` (`start_remote`, **never plain `ng serve`**). Bash has no `ng` on its PATH. Then re-check. |

**Local profile only:** the services the change needs must answer on their URL (`dt env-check` shows `local_serving`).
Not serving → `dt service-start {N} <service>` (detached, test DB through environment variables, log in
`DEV_TICKETS\{N}\services\`). `wfapi_wf_service` and `wfapi_bm_service` run **behind the gateway** even locally, so start
the gateway too; the runtime API has its own URL. Ask first if the developer prefers to start them. Check the service
logs for startup errors before the first check.

Servers you started can be killed by the harness on low memory ("stopped because the system is running low on
memory"): report it, restart only when asked. Stop only the ones you started, never the developer's own.

## 3 · Test data and the database

- **Local profile:** DB changes go to the **test DB** only. `dt env-check` refuses unless the connection string's
  host and database match the allow-list in the env file. SP/DDL files stay in the DB repo on the DB ticket's branch;
  running them against the test DB doesn't change that. The `DB.md` destructive-statement confirmation still applies.
- **`cloud_staging` / `cloud_preprod` profiles (shared!):** create only throw-away records named `AI-CHECK-{N}-…`, and delete them afterwards.
  Never edit or delete data you didn't create. A check that needs existing staging data changed → stop and ask.
  Any destructive UI action (delete, bulk change, publish, trigger a job) needs the developer's yes first.
- Never print or store a connection string or password in chat, a file under the repo, or `SUMMARY.md`.

## 4 · Run

1. **Login:** "Please sign in in the Chrome window, then tell me." Never ask for or type a password.
2. **Test data:** per the rules above; for existing records ask which to use.
3. Open `http://localhost:4200` and run each check step by step. Record in `reviews\functional-checks.md`.
