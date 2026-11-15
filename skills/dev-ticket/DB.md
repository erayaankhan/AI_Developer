# dev-ticket — DB changes (step 4, gate 7a)

Read when the approach needs SP / function / DDL / DML changes. `dt` = `python {PY}/dev_ticket.py`.

## Where DB changes go

Never on `FB_{N}` in the DB repo. Every main ticket with DB changes gets **its own DB ticket** in Assembla (a Story with
Node = C2M-DB, related to it); the changes go on that ticket's branch `FB_{D}`. The shared `DB | Release x` ticket is no
longer used by this workflow.

| Command | RESULT → action |
|---|---|
| `dt db-ticket {N}` | The DB ticket related (relationship Related) to N. `FOUND` → use `db_ticket` = D. `NO_DB_TICKET` → create it with `dt db-create` (below). `MULTIPLE_DB_TICKETS` → **S6d**: which one? |
| `dt db-create {N} --title "DB \| #{N} <what changes in the database>" [--description-file …]` | Creates the DB ticket and links it **Related** to N. **Title** `DB \| #{N} …`, written from BRIEF.md `## DB changes`, so anyone can tell what it is (e.g. `DB \| #18359 Slug-named workflow tables: add application.wf_table_suffix, update 66 SPs`). **Attributes** (set by the script): Plan level Story, Node C2M-DB, tag WF, N's milestone / category / priority / status, assigned to the developer. Without `--yes` it returns `PREVIEW`: show it and ask. On yes re-run with `--yes --confirm {H}` → `CREATED` (`CREATED_WITH_PROBLEMS`: the ticket exists and is recorded, do **not** create another; fix what `problems` lists by hand or ask). `ALREADY_EXISTS` → nothing created. `BAD_TITLE` → fix the title. **If an attribute is unclear** (e.g. no Category on N), ask the developer; never guess. |
| `dt db-branch {D} --ticket {N}` | Checks out `FB_{D}` in `C2M_CI_DB`, or creates it from `origin/master`. `DIRTY` → **S6e**: the DB tree must be clean to start (tracked changes **and** untracked files; git-ignored ones don't count), on or off the branch. List the files and ask: the developer commits them elsewhere, stashes (`-u`), deletes or excludes them (`.git/info/exclude`); never do it yourself. The first call (no `db.base` yet) records `db.ticket` and `db.base` in the state for you; later calls (resume, pause) aren't re-checked. `CREATED_LOCAL` → ask, then `--push`. |

`db.base` (set by `dt db-branch … --ticket`) is the commit this ticket started from; the checks judge only what it adds
after that.

Do **not** type `db.files`. After writing DB files, again before the gates (7a) and again before staging, run
`dt db-files {N}`: it sets `state.db.files` from the files of the commits since `db.base` whose message starts
`re#{D} - {N} - `, plus the uncommitted work of a tree that started clean (tracked edits, untracked `.sql` under
`Database/`). Other tickets' commits on the shared branch (`re#{D} - {M} - …`) are left out and listed; commits without
that message prefix are listed too, so an unattributed commit of this ticket isn't lost silently (check them, and
if one is yours, re-commit it with the prefix or tell the developer). `db-checks` still scans every change since
`db.base` for destructive statements, listed or not.

## Files and templates

| Kind | Folder (`C2M_CI_DB\Database\<schema>\`) | File name |
|---|---|---|
| Procedure | `SP\` | `<Name>.sql` |
| Function | `Functions\` | `<Name>.sql` |
| Table create / alter | `Tables\Schema\` | descriptive, e.g. `<TABLE>_AddColumn.sql` |
| DDL / DML | `DDL_DML\` | `{D}.sql`, then `{D}_1.sql`, `{D}_2.sql`, … |

**Procedure** (write exactly like this; the function form is identical with `FUNCTION`, plus `RETURNS …` after the parameters):
```sql
/*!50003 DROP PROCEDURE IF EXISTS  `WF_PLTF_Name` */;

DELIMITER $$

/*!50003 CREATE PROCEDURE `WF_PLTF_Name`(
  IN vParam INT
)
BEGIN
  ...
END */$$
DELIMITER ;
```
Rules: one object per file, and the file name = the object name. No `DEFINER=`, no `USE`. Final newline.
DDL/DML must be re-runnable: `CREATE TABLE IF NOT EXISTS`, `INSERT … WHERE NOT EXISTS`, an existence check
before `ADD COLUMN`. Avoid `PREPARE … FROM CONCAT(<input>)`; if unavoidable, validate the inputs.

**Modifying an existing SP or function that isn't in the template:** convert it **first, as its own step**:
`python {PY}/db_checks.py --normalize <file> …` (preview), then again with `--write`. It rewrites only the header
and footer, drops `DEFINER`, and turns inner `/* */` comments into `--`. Report its `notes`.
`NEEDS_MANUAL` → convert by hand and tell the developer why. Then make the logic change.

## Consequences

- Risk: SP/DML → at least MEDIUM, DDL → HIGH. A DB ticket never takes the LOW path.
- The DB ticket travels with its main ticket: `dt status {N} --to … --yes --confirm {H}` (and the pause / resume moves) move it to the
  same status, but only while it is still where N was (`db_companion` in the RESULT says what happened; never force it).
  It never counts as a second ticket in Dev In Process. The staging job deploys DB tickets as separate Stories from
  DevOps Staging, so it must reach that status too. After N is deployed, D is deployed in its own run
  (`staging_deploy.py deploy {D} --requested`, `STANDARD_PATH.md` "Deploy on request"); the DB branch is merged in the DB repo and
  the scripts are sent to the database by the job.

## `db-checks` gate (runs inside 7a)

Besides `db.files` it also scans every **tracked** `.sql` change in the DB repo since `db.base`, plus untracked `DDL_DML/{D}.sql` / `{D}_n.sql` files, so a destructive script can't escape by being left off the list. Unlisted files are judged for destructive statements only (a `STOP` counts; template/naming problems are just warnings, since they may be a colleague's commits on the shared branch): list the ones that are yours. Other untracked DB files are ignored (local clutter). `--skip db-checks` is refused for a ticket with DB changes, and a DB ticket without `db.base` gets an `ERROR` until it is set.

Runs when there are DB files or any `.cs` change. `ERROR`s fail the gate; warnings go to the 7e review.
- **`STOP`** = a destructive statement this ticket added (`DROP TABLE/DATABASE`, `TRUNCATE`, `ALTER … DROP COLUMN/INDEX`, `DROP INDEX`,
  `UPDATE`/`DELETE` without `WHERE`, incl. JOIN and multi-table forms; `DROP TABLE IF EXISTS tmp_…` is only a warning). **Hard stop:** show the file and statement, and ask. Only on an explicit yes:
  `dt state {N} --set db.destructive_confirmed=<token>[,…]` using the `Developer confirmation token` printed in the STOP line (`<file>@<hash of the destructive statements>`), then re-run 7a. A statement added later changes the token, so it needs a new yes. Never confirm it yourself.
- A **new `RunSp…("X")` call to an SP that exists nowhere** → `ERROR`: write the SP (above).
  An SP that master already calls but the repo lacks → `WARNING`: note it in SUMMARY.md.
- "Not in the defined template" → run `--normalize` as above.
