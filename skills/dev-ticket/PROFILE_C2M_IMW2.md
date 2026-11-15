# C2M CI profile — the `C2M - IMW2` repo

Read with `PROFILE_C2M.md` (shared by every C2M CI repo) when `dt config-check` reports `"repo_key": "imw2"`.
Where they differ, this file wins.

## The repo

| Folder | What | Build / test |
|---|---|---|
| `c2m-api-gateway` | Spring Cloud gateway (routes from `route.json`, S3 / Azure blob sources) | Maven, has unit tests |
| `icedata` | Ice data service (feed id encryption / decryption) | Maven, parent `c2m:c2m-starter-parent` |
| `druid_client_presto` | Big Data query service (XML / JSON results) | Maven, parent `c2m-starter-parent` |
| `druid-reindexer` | Druid re-index jobs | Maven, parent `c2m-starter-parent` |
| `bigdata_CRUD_utility` | Big Data CRUD schedulers (kill tasks, Cassandra table clean-up) and a web UI | Maven, parent `c2m-starter-parent` |
| `apm-chart` | Angular 17 app (IceChart, Highcharts) | `ng test` (Karma) |

- Each app is its own Maven project (no parent `pom.xml` at the root). `c2m-starter-parent` comes from the company Maven
  repository through `~/.m2/settings.xml`; a resolve error means the developer's Maven settings, not the code.
- **`bigdata_CRUD_utility/src/main/resources/public/`** is a **compiled Angular bundle** (hashed `chunk-*.js`,
  `main-*.js`); its source is not in this repo. Never hand-edit those files. A UI change there means building the UI
  where its source lives and replacing the whole bundle: ask the developer for the build output; the AI review and the
  risk check treat the bundle as one opaque change.
- **Build number:** apps carry `applicationBuildNumber` in `<app>/config/application.properties`; the team bumps it
  (e.g. `0022` → `0023`) when an app's code changes so the deployed build is new. In the plan, list the bump for each
  changed app and ask the developer to confirm it; never bump an app the ticket did not change.
- Most apps keep run-time settings in `<app>/config/` (`application.properties`, `files/`). Changes there are
  **medium risk** and must not carry local hosts, passwords or keys.
- `c2m-api-gateway/CHANGE_REQUESTS/` and `DEFECTS/` hold design notes from earlier work; they are not the workflow's
  `DEV_TICKETS` folder. Read them for context; don't add to them.

## Tickets

- `Node = IMW2`, tag **`IMW2`** (a few older tickets carry `WF`), parent: an active IMW2 Epic of the feature (e.g.
  "Epic| Licence Api update in IMW2 Node").
- Milestones are named `BigData_3.x.0` and `Bigdata 2.1.x`.

## Gates

- **Maven**: `mvn -q -B test` in each changed app (`maven-test:<app>`). Only `c2m-api-gateway` has unit tests today;
  for the others the gate still compiles the app. A new behaviour in any app gets a unit test under `src/test/java`
  (test-first, as the skill says), even where the folder doesn't exist yet.
- **Angular** (`ng test`): `apm-chart`. It needs `npm install` in `apm-chart` once.
- No Python, no pre-commit config: those gates do not run. `--skip <gate>` only with the developer's yes and a reason in
  SUMMARY.md.

## Functional checks (step 7d)

- `apm-chart`: `ng serve` in `apm-chart` (`http://localhost:4200`), check it in Chrome; the developer signs in.
- A Java service: start it locally (`mvn spring-boot:run`, or the app's `script/start.sh` as its README says) when its
  dependencies (Druid, Cassandra, Presto, S3 / Azure) are reachable, and call the changed endpoint with curl. Usually
  they are not reachable from a laptop: then rely on the unit tests and, on the developer's yes,
  `dt func-mode {N} skip --reason "…" --confirmed`.
