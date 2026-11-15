# dev-ticket — Independent code review brief (step 7e)

Given verbatim to a **fresh sub-agent** that did not write the code. Self-contained: it uses
standard code-review practice plus the checks below, and no rules from other folders.

You are reviewing ticket **#{N}** ({summary}). Risk tier: **{tier}**. On a reviewer's own
LOW-risk ticket no one else reads the code before deploy, so be thorough and specific. Your report
is kept in the ticket folder and is not posted to the ticket. Find problems; don't approve by default.

## Inputs

- WF repo: `git diff origin/master...HEAD` plus uncommitted changes to these files: {files}
- DB repo (only if listed): these files on `FB_{D}` in `C2M_CI_DB`: {db_files}
- `DEV_TICKETS\{N}\BRIEF.md` (problem, approach, acceptance criteria, assumptions) and `PLAN.md`
  (tasks, tests, UI checks)
- `DEV_TICKETS\{N}\reviews\functional-checks.md`: the independent run of the plan's functional checks, or none if the
  developer skipped them (`state.functional_skip.reason`; then check SUMMARY.md says so). Otherwise verify that every
  acceptance criterion has a PASS with concrete evidence.
- `DEV_TICKETS\{N}\attachments\` (only if it exists): the reporter's screenshots. Read each image and check that the
  reported screen, wording and expected result match what BRIEF.md, the change and the UI checks describe. Videos and
  ticket URLs are not available to you; don't report their absence.

## How to read the code

The diff is where you start, not your whole view. You have the full repository:
- Open the **enclosing class/component** of every changed method (class-level attributes such as
  `[Authorize]`, DI registrations, base classes, lifecycle hooks).
- Find the **callers** of every changed public method, endpoint, input/output or service method
  (Grep) and check they still work.
- Open **referenced types** (DTOs, models, interfaces) that the change depends on.
Only report what you verified by reading code. Cite `file:line`.

**Evidence.** Every High, Medium and security finding carries, in the `Evidence` column, the verbatim code line it is
about (one line, at most ~100 characters, copied from the file, no `|`; write `\|` if a pipe is unavoidable; backticks that are part of the code stay as they are, no backslash before them). The
`File:line` column holds exactly one primary citation, `path:line` (a range `path:line-line` is fine); further
locations go in the Finding text. A script checks that the file exists, that the line is in range and that your
excerpt appears at it, so copy it from the file instead of paraphrasing. A finding that is about something **absent**
(a missing test, an uncalled path) cites the nearest line that shows the gap and writes `n/a: <why>` as its evidence;
those are listed as not checkable, so use it only when no line can show the problem. Lows may leave Evidence empty.

## 1. Standard review

- **Correctness:** logic errors, off-by-one, null/undefined handling, wrong conditions, missed edge
  cases from BRIEF.md, behaviour change for existing callers.
- **Errors and resources:** swallowed exceptions, missing error handling on HTTP/DB/IO, undisposed
  resources (`IDisposable`, DB connections), unsubscribed observables / event handlers / timers.
- **Async and concurrency:** `.Result`/`.Wait()` blocking, missing `await`, fire-and-forget without
  handling, shared mutable state, race conditions.
- **Performance:** N+1 queries, work inside loops that belongs outside, unnecessary change detection,
  large payloads, missing pagination where lists can grow.
- **API and contracts:** breaking changes to endpoints, DTOs, SP signatures or component inputs;
  REST semantics; status codes.
- **Readability and maintainability:** naming, duplication (reuse existing helpers), dead code, magic
  values, comments on non-obvious logic, consistency with the surrounding code's style.
- **Scope:** changes unrelated to the ticket; local-only files (`src/environments/environment.ts`)
  staged or modified as part of the ticket.

## 2. Security

- **Secrets:** credentials, tokens, keys or connection strings in code, config (`appsettings*.json`)
  or tests. A removed secret still visible in the diff's removed lines is fine; a new one is not.
- **Injection:** SQL built by string concatenation (C# or dynamic SQL inside SPs), unparameterised
  queries, command/path injection.
- **Authorisation:** every new or changed endpoint is protected as its siblings are (check the class
  and the gateway route); no user-controlled IDs used without an ownership/permission check.
- **Input validation:** request models validated; over-posting (binding more fields than intended).
- **XSS / Angular:** `innerHTML`, `bypassSecurityTrust*`, building HTML from user data.
- **Encryption comparisons:** output of `EncryptDecrypt.Encrypt()` uses a random IV, so it must
  never be compared with `==`, used as a dictionary/cache key, or used in a SQL `WHERE`. Decrypt and
  compare plaintext instead (known past bug in this repo).
- **Sensitive data in logs** or error messages returned to clients.

## 3. Repository conventions

- **Documented decisions:** the repo has no ADR folders. Check the change against the module `README.md`
  and the conventions stated in the surrounding code and comments (e.g. AFD3 persistence in `final_json`,
  one owner of the X6 graph, round-trip preservation); a change that contradicts them is a finding.
- **README impact:** if the diff changes something a module `README.md` documents (commands, routes, config key names,
  dependencies, structure, gotchas) and BRIEF.md `## README impact` doesn't name it, that is a Low finding.
- **Shared libraries:** edits to sources of `SharedLibraries_MicroServices` DLLs (e.g. under
  `c2mwf_common_utilities/`, `C2MWebAPI/`) need the DLL rebuilt and redeployed. Flag it for
  SUMMARY.md.
- **Angular:** every new `.component/.service/.directive/.pipe.ts` has a `.spec.ts`; the federated
  build (`ng run listingcms:serve-federated`) isn't broken by config changes.
- **Database:** SP/DML/DDL live in the DB repo (`C2M_CI_DB\Database\C2M\SP\`) on the DB ticket's
  branch, never in the WF repo.

## 4. DB scripts, only if DB files are listed

The `db-checks` gate already enforced the mechanical rules (templates, naming, DEFINER/USE,
destructive statements, SP existence). Read its warnings (given below) and judge what a script
can't: logic, parameters and transactions.

- DDL is safe to re-run or clearly one-off; no accidental `DROP`; indexes for new lookup columns.
- SPs: parameterised (no dynamic SQL from inputs), errors handled, transactions where multiple
  writes must succeed together, `DEFINER`/permissions consistent with neighbouring SPs.
- Every SP the WF code now calls exists on the DB branch with the matching parameters.

## 5. Tests and UI checks

- Each acceptance criterion has a test or a UI check that would **fail if the criterion were not
  met**. Flag tests that assert nothing meaningful, only exercise mocks, or can't fail.
- UI checks have exact steps and an observable expected result, and (when screenshots exist) cover the screen and
  behaviour the screenshots show.
- Tests follow the change (updated specs for changed behaviour, not deleted to make them pass).
- **DEFECT:** BRIEF.md `## Same flaw elsewhere` records the sweep. Grep the root-cause pattern yourself: a hit that is
  neither listed there nor fixed in the diff is a Medium finding (a missed place); a "fix here" hit must be in the diff with a test.

## Severity

- **High:** incorrect behaviour, data loss or corruption, security issue, breaking change, an
  acceptance criterion not met.
- **Medium:** likely bug in edge cases, missing error handling, missing/weak test for an AC,
  convention or README violation, performance problem at realistic sizes.
- **Low:** readability, naming, minor duplication, style.
- **Any security finding is at least Medium**.

## Report format: write `DEV_TICKETS\{N}\reviews\code-review.md` exactly like this

```markdown
# Code review — #{N} {summary}
Reviewer: independent sub-agent ({model}) · Risk: {tier} · Commit/diff: {HEAD short sha}{ + uncommitted}
Files reviewed: {n} WF, {n} DB · Context read: {classes/callers/types opened}

## Verdict: PASS | CHANGES REQUIRED
(PASS only with 0 High, 0 Medium and 0 security findings)

## Findings
| # | Severity | Category | File:line | Evidence | Finding | Suggested fix |
|---|---|---|---|---|---|---|
| 1 | High | Correctness | path/x.ts:42 | `if (id != null) {` | … | … |

## Acceptance criteria coverage
| AC | Covered by | Verified? |
|---|---|---|
| 1 | test `x.spec.ts › "…"` | yes / no, and why |
| 2 | UI check UI-1 | expected result is concrete: yes/no |

## Security
- {checked items and result, or "no issues found": list what was checked}

## Conventions
- {READMEs and conventions that applied and whether they're followed}

## Notes for SUMMARY.md
- {e.g. shared DLL rebuild needed, DB ticket #D must be deployed, accepted Lows}
```
