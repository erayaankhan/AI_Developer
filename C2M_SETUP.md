# AI Developer for C2M CI repos: setup

One-time setup on your machine. Afterwards, Claude Code in VS Code can take a ticket of a C2M CI repo (Assembla space
C2M CI) from **Dev Queue** to **Code Review** (`/dev-ticket`), and a reviewer can review it and approve it on to
**DevOps Staging** (`/review-and-deploy`).

Nothing is committed, pushed, posted to Assembla or status-changed without your "yes" in the chat.

**One workflow folder for every C2M CI repo:** `AI_Developer_C2M`. The C2M repos share the Assembla space, the status
flow and the Jenkins server (`Code_Merge` on jenkins-build-server), so they share one folder and one `credentials.env`.
Each repo links to it, and the workflow recognises the repo by its git origin. Launchpad is a different space with a
different process and Jenkins server, so it has its own folder, `AI_Developer` (`LAUNCHPAD_SETUP.md`).

Repos set up today:

| Repo folder | Origin | Assembla Node | Milestones |
|---|---|---|---|
| `C2M - GenAI` | `Plasma-Portfolio/GenAI.git` | GenAI | `GenAI 1.x.y` |
| `C2M - IMW2` | `Plasma-Portfolio/c2mimw2.git` | IMW2 | `BigData_3.x.0`, `Bigdata 2.1.x` |

## 1. What you need installed

| Tool | For | Check |
|---|---|---|
| Git | all | `git --version` |
| Python 3.10 or newer | all (runs the workflow) | `python3 --version` (Windows: `python --version`) |
| VS Code with the Claude Code extension | all | the Claude panel opens |
| Java and Maven | GenAI (`gen-ai-designer-service`), IMW2 (every Java app) | `mvn --version` |
| Node + npm, `npm install` once in each Angular app you work on | GenAI (`gen-ai-designer-mfe`, `prompt-designer`), IMW2 (`apm-chart`) | `node --version` |
| Each Python service's `.venv` (as in its README) | GenAI (`GenAIService`, `EmbeddingService`, `connector-bridge`) | `ls GenAIService/.venv` |
| `~/.m2/settings.xml` that can fetch the company parent `c2m:c2m-starter-parent` | IMW2 | `mvn -q -B -f icedata/pom.xml validate` in the repo works |

## 2. Folder layout

Put `AI_Developer_C2M` **next to** your C2M clones, with these exact folder names:

```
<your projects folder>/
├── AI_Developer_C2M/  ← from the bundle (step 3)
├── C2M - GenAI/       ← git clone of GenAI.git      (if you work on GenAI)
└── C2M - IMW2/        ← git clone of c2mimw2.git    (if you work on IMW2)
```

If a clone has another name or lives somewhere else, keep it there and set the path in step 4a.

## 3. Get AI_Developer from the bundle

You receive a file `AI_Developer.bundle`. In your projects folder, run `git clone AI_Developer.bundle AI_Developer_C2M`.
**Updates:** inside `AI_Developer_C2M`, run `git pull <path to the new AI_Developer.bundle> main`.

## 4. Your local settings (never shared, never committed)

**a) Profile.** Create `AI_Developer_C2M/python_scripts/dev_ticket.config.local.json`:

```json
{
  "profile": "c2m"
}
```

Only if a clone is elsewhere, add its path, e.g. `"repos": {"imw2": {"folder": "D:/code/c2mimw2"}}` (keys: `genai`,
`imw2`; absolute, or relative to `AI_Developer_C2M/python_scripts`).

**b) Your Assembla key.** Copy `AI_Developer_C2M/credentials.env.example` to `AI_Developer_C2M/credentials.env` and fill in
**your own** `ASSEMBLA_API_KEY` and `ASSEMBLA_API_SECRET` (Assembla → your profile → *API Applications & Sessions*).
Never paste a key into the Claude chat. `credentials.env` is git-ignored.

**c) Jenkins (reviewers, to deploy).** In the same file set `JENKINS_REMOTE_BASE_URL=https://jenkins-build-server.c2minternal.com`,
`JENKINS_REMOTE_USER` = your Jenkins **user ID** (click your name in Jenkins: the address ends in `/user/<id>/`) and
`JENKINS_REMOTE_API_TOKEN` = an API token made on that server (your name → Security → API Token → Add new token).
One login serves every C2M repo.

**d) Teams (everyone).** At hand-off the workflow posts in the repo team's Teams channel and @mentions the reviewer you
picked. The lead gives you the channel's webhook address (privately, not in a chat channel); add it to the same file
under that repo's key: `TEAMS_WEBHOOK_URL_GENAI=<address>` and/or `TEAMS_WEBHOOK_URL_IMW2=<address>`. Without it,
hand-off still works and tells you the message was not sent.

> **Lead, once per channel:** in Teams open the review channel → **⋯ → Workflows** → *Post to a channel when a webhook
> request is received* → choose the team and channel → **Save**, and copy the address it shows. Share it only with the
> team: anyone who has it can post to the channel. If it leaks, delete that workflow and make a new one.

## 5. Link the workflow into each repo

**macOS / Linux**, in your projects folder, once per repo you work on:

```bash
r="C2M - IMW2"   # or "C2M - GenAI"
ln -s ../AI_Developer_C2M "$r/AI_DEV_ENV"
mkdir -p "$r/.claude/commands"
ln -s ../../AI_DEV_ENV/.claude/commands/dev-ticket.md "$r/.claude/commands/dev-ticket.md"
ln -s ../../AI_DEV_ENV/.claude/commands/review-and-deploy.md "$r/.claude/commands/review-and-deploy.md"
printf '\n/AI_DEV_ENV\n/DEV_TICKETS/\n/.claude/commands/dev-ticket.md\n/.claude/commands/review-and-deploy.md\n/.claude/settings.local.json\n' >> "$r/.git/info/exclude"
```

**Windows**, in **Command Prompt**, in your projects folder (replace `C:\Projects`; repeat per repo):

```bat
set r=C2M - IMW2
mklink /J "%r%\AI_DEV_ENV" "C:\Projects\AI_Developer_C2M"
mkdir "%r%\.claude"
mklink /J "%r%\.claude\commands" "C:\Projects\AI_Developer_C2M\.claude\commands"
(echo /AI_DEV_ENV& echo /DEV_TICKETS/& echo /.claude/commands& echo /.claude/settings.local.json) >> "%r%\.git\info\exclude"
```

**Check:** `git status` in the repo shows nothing new.

## 6. Let Claude reach the workflow

In each repo create `.claude/settings.local.json` with your own absolute path:

```json
{
  "permissions": {
    "additionalDirectories": ["/Users/<you>/Projects/AI_Developer_C2M"]
  }
}
```

Open **the repo itself** (e.g. `C2M - IMW2`) in VS Code, not the parent folder, then *Developer: Reload Window*.

## 7. Check (all read-only)

In the repo folder's terminal (Windows: `python` instead of `python3`):

| Command | Expect |
|---|---|
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py config-check` | `OK`, `"profile": "c2m"`, `"repo_node"` = this repo's Node (`GenAI` / `IMW2`) |
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py whoami` | your login, `space=c2m-ci` |
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py role` | `reviewer` |
| `python3 AI_DEV_ENV/python_scripts/staging_deploy.py check` | `OK`, `"job": "Code_Merge"` |

In the Claude panel, typing `/` lists `/dev-ticket` and `/review-and-deploy`.

## 8. How the team uses it (the C2M CI flow)

| Step | Who | Status |
|---|---|---|
| `/dev-ticket` offers your tickets of this repo's Node in Dev Queue (its current milestones), or resumes the one you started | author | Dev Queue |
| Ticket check, then the `FB_<n>` branch (the one the bot made, if it exists); moves on your yes | author | → **Dev In Process** |
| Brief, plan, build test-first, gates (Maven, ng test, and for GenAI py-compile / pytest), risk check, AI review; commit and push | author | Dev In Process |
| Hand-off: summary comment, assign a reviewer (one of the others, or yourself); Teams post @mentioning them | author | → **Code Review** |
| `/review-and-deploy`: re-runs the checks, AI review as an aid, then approve or reject | reviewer | approve → **Dev Done → DevOps Staging**, reject → back to the author |
| Deploy: on your yes, Claude triggers Jenkins **Code_Merge** once and follows it (it also runs every 2 hours by itself) | reviewer + Jenkins | → **QA Queue**, or Merge Failed / Build Failed |

- **Reviewers per repo:** GenAI: shamshadk, Igor-v, varadabhyankar. IMW2: shamshadk, nikhilt, gaurav-k. Everyone on
  them also writes code. At hand-off you may assign the ticket to another reviewer **or to yourself**, then review,
  approve and deploy it with `/review-and-deploy` (same checks as any review).
- **One ticket at a time** per repo: finish or pause it (`/dev-ticket pause N`) before starting the next.
- Each repo picks only its own Node's tickets.
- **IMW2:** when a ticket changes an app, the plan bumps its `applicationBuildNumber` in
  `<app>/config/application.properties`, as the team does today; you confirm it.
  `bigdata_CRUD_utility/src/main/resources/public/` is a built Angular bundle: Claude never edits it by hand.
- Sonar / Snyk checks before hand-off are not switched on for C2M yet.
- **Each new release:** the lead updates that repo's `current_milestones` in `python_scripts/profiles/c2m.json` and
  shares a new bundle.
- **Adding another C2M repo:** the lead adds it under `repos` in `profiles/c2m.json` (origin, Node, milestones,
  reviewers, gates) with a `skills/dev-ticket/PROFILE_C2M_<KEY>.md`; developers then do steps 5–7 for it.

Details for Claude are in `skills/dev-ticket/PROFILE_C2M.md` and `PROFILE_C2M_<REPO>.md`. You don't need to read them.

## Troubleshooting

| Message | Fix |
|---|---|
| hand-off says `TEAMS_NOT_CONFIGURED` / `TEAMS_BAD_URL` | add or correct that repo's Teams address (step 4d), then ask Claude to send the Teams message again (`dt teams-notify N`) |
| hand-off says `TEAMS_FAILED` | the ticket is assigned; the workflow may have been deleted: ask the lead, then `dt teams-notify N` |
| `config-check` → `not one of the profile's code repos` | VS Code is open on the wrong folder (step 6), or this C2M repo is not set up yet (ask the lead) |
| `HTTP 401/403` on `whoami` | wrong key or secret in `credentials.env` |
| `NO_CURRENT_MILESTONE` / `WRONG_MILESTONE` | the ticket's milestone is not in the repo's `current_milestones`: ask the lead |
| gate `pytest:<service>` SKIPPED, "no .venv" (GenAI) | create that service's environment (step 1), or skip the gate with a reason |
| gate `maven-test:<app>` fails resolving `c2m-starter-parent` (IMW2) | your `~/.m2/settings.xml` (step 1): ask a teammate for theirs |
| `SHARED_QUEUE` on deploy | other tickets wait in DevOps Staging; Code_Merge may take one of them first. Say yes only if that's fine, or let the 2-hourly run handle it |
| `NOT_PICKED` on deploy | Code_Merge skips this ticket (e.g. its milestone): ask DevOps |
| `HTTP_ERROR 401` on `staging_deploy.py check` | Jenkins user ID / token (step 4c) |
| `/dev-ticket` not offered | the `.claude/commands` links (step 5), then reload the window |
