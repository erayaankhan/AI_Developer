# AI Developer for Launchpad: setup

One-time setup on your machine. Afterwards, Claude Code in VS Code can take a Launchpad ticket from **Dev in Process**
to **Dev Code Review** (`/dev-ticket`). Reviewers can also review a ticket and approve it into **DevOps Staging**
(`/review-and-deploy`).

Nothing is committed, pushed, posted to Assembla or status-changed without your "yes" in the chat.

## 1. What you need installed

| Tool | Check |
|---|---|
| Git | `git --version` |
| Python 3.10 or newer | `python3 --version` (Windows: `python --version`) |
| .NET 8 SDK | `dotnet --version` |
| Node + npm (and `npm install` done in each Angular app you work on) | `node --version` |
| Maven | `mvn --version` |
| VS Code with the Claude Code extension | the Claude panel opens |

## 2. Folder layout

Put `AI_Developer` **next to** your Launchpad clones, with these exact folder names:

```
<your projects folder>/
├── AI_Developer/              ← from the bundle (step 3)
├── Launchpad - CMGMT/         ← git clone of cmgmt.git
├── Launchpad - MAI-DC/        ← git clone of mai-dc.git
└── Launchpad - CMGMT-DB/      ← git clone of cmgmt-db.git
```

If your clones have other names or live somewhere else, keep them where they are and set the paths in step 4 instead.
Never use the `Launchpad- Config` repo with this workflow.

## 3. Get AI_Developer from the bundle

You receive a file `AI_Developer.bundle`. In your projects folder, run:

```
git clone AI_Developer.bundle AI_Developer
```

**Updates:** when you get a newer bundle, run this inside `AI_Developer`:

```
git pull <path to the new AI_Developer.bundle> main
```

## 4. Your local settings (never shared, never committed)

**a) Choose the Launchpad profile.** Create `AI_Developer/python_scripts/dev_ticket.config.local.json`:

```json
{
  "profile": "launchpad"
}
```

If you also work in C2M repos (`C2M - GenAI`, `C2M - IMW2`, ...), they share one separate copy, `AI_Developer_C2M`,
with its own credentials (see `C2M_SETUP.md`): another Assembla space and Jenkins server. Keep this folder for Launchpad only.

Only if your folders differ from step 2, add the paths. They can be absolute, or relative to `AI_Developer/python_scripts`:

```json
{
  "profile": "launchpad",
  "db": {"repo_path": "D:/code/cmgmt-db"},
  "repos": {
    "cmgmt": {"folder": "D:/code/cmgmt"},
    "mai-dc": {"folder": "D:/code/mai-dc"}
  }
}
```

**b) Your Assembla key.** Copy `AI_Developer/credentials.env.example` to `AI_Developer/credentials.env`. Fill in
**your own** `ASSEMBLA_API_KEY` and `ASSEMBLA_API_SECRET` (Assembla → your profile → *API Applications & Sessions* →
new key). The workflow identifies you by this key.

- Leave the Jenkins lines empty: Launchpad's Jenkins deploys by itself.
- Never paste a key into the Claude chat, and never ask Claude to open this file.
- `credentials.env` is git-ignored.

**b2) Sonar and Snyk (everyone).** The last step before hand-off scans your change with the same tools Jenkins uses.
In the same `credentials.env`:

- `SONAR_TOKEN=` a **user token** from https://sonarqube1-plasmacomp.c2minternal.com (your name → My Account → Security →
  Generate token, type *User*). The scan goes into a project of your own (`DeviceMgmtWebApp__dev_<you>`), never the real one.
- `SNYK_TOKEN=` your Snyk auth token (app.snyk.io → Account settings → Auth token). You need access to the company Snyk
  org (`itsupport-eoq`): ask DevOps.
- Tools: Node (`npx`, for the Sonar scanner) is already needed for the Angular gates. For DeviceMgmtAPI also run once:
  `dotnet tool install -g dotnet-sonarscanner`. For MAI-DC's Java apps: Java and Maven, with a `~/.m2/settings.xml` that
  can fetch the company parent `c2m-starter-parent` (the scan compiles the app, tests skipped).

**c) Teams (everyone).** At hand-off the workflow posts in the team's Teams channel and @mentions the reviewer you
picked. The lead gives you the channel's webhook address (privately, not in a chat channel); add it to the same file:
`TEAMS_WEBHOOK_URL_LAUNCHPAD=<the address>`. Without it, hand-off still works and tells you the message was not sent.

> **Lead, once:** in Teams open the review channel → **⋯ → Workflows** → *Post to a channel when a webhook request is
> received* → choose the team and channel → **Save**, and copy the address it shows. Share it only with the team: anyone
> who has it can post to the channel. If it leaks, delete that workflow and make a new one.

## 5. Link the workflow into each code repo

The workflow lives once, in `AI_Developer`. Each code repo (CMGMT and MAI-DC, **not** CMGMT-DB) gets two links and
three local-only ignore lines. Nothing in the repos is committed or changed for anyone else.

**macOS / Linux.** Run in your projects folder:

```bash
for r in "Launchpad - CMGMT" "Launchpad - MAI-DC"; do
  ln -s ../AI_Developer "$r/AI_DEV_ENV"
  mkdir -p "$r/.claude"
  ln -s ../AI_DEV_ENV/.claude/commands "$r/.claude/commands"
  printf '\n/AI_DEV_ENV\n/DEV_TICKETS/\n/.claude/commands\n/.claude/settings.local.json\n' >> "$r/.git/info/exclude"
done
```

**Windows.** Run in **Command Prompt** (not PowerShell), in your projects folder. Junctions need no admin rights.
Replace `C:\Projects` with your folder:

```bat
for %R in ("Launchpad - CMGMT" "Launchpad - MAI-DC") do (
  mklink /J "%~R\AI_DEV_ENV" "C:\Projects\AI_Developer"
  mkdir "%~R\.claude"
  mklink /J "%~R\.claude\commands" "C:\Projects\AI_Developer\.claude\commands"
  (echo /AI_DEV_ENV& echo /DEV_TICKETS/& echo /.claude/commands& echo /.claude/settings.local.json) >> "%~R\.git\info\exclude"
)
```

**Check:** `git status` in each repo shows nothing new.

## 6. Let Claude reach the workflow and the DB repo

In **each** code repo, create `.claude/settings.local.json` with your own absolute paths:

```json
{
  "permissions": {
    "additionalDirectories": [
      "/Users/<you>/Projects/AI_Developer",
      "/Users/<you>/Projects/Launchpad - CMGMT-DB"
    ]
  }
}
```

On Windows, write the paths with forward slashes, e.g. `"C:/Projects/AI_Developer"`.

## 7. Open the right folder in VS Code

Claude Code works in the folder VS Code has open, so **open the code repo itself**: *File → Open Folder… →
`Launchpad - CMGMT`*. For a MAI-DC ticket, open `Launchpad - MAI-DC` in another window. Do not open the parent folder.
After changing `settings.local.json`, run *Developer: Reload Window* (Cmd/Ctrl + Shift + P).

## 8. Check (all read-only)

In the repo folder's terminal (Windows: `python` instead of `python3`):

| Command | Expect |
|---|---|
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py config-check` | `OK`, `"profile": "launchpad"`, `repo_key` = `cmgmt` or `mai-dc` |
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py creds` | `ASSEMBLA_API_KEY file`, `ASSEMBLA_API_SECRET file` |
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py whoami` | your login, `space=Launchpad` |
| `python3 AI_DEV_ENV/python_scripts/dev_ticket.py role` | `reviewer` (shamshadk, vickyg) or `developer` (kinjons) |

In the Claude panel, typing `/` lists `/dev-ticket` and `/review-and-deploy`.

## 9. How the team uses it

| Step | Who | Status |
|---|---|---|
| `/dev-ticket` picks a **Node ticket** from your Dev Queue (current release), or resumes the one you already started | author | Dev Queue |
| Ticket check, then the `FB_<n>` branch; the ticket moves to Dev in Process on your yes | author | → **Dev in Process** |
| Brief, plan, build test-first, gates, risk check, AI review; you commit and push | author | Dev in Process |
| Hand-off: summary comment, assign a reviewer | author | → Dev Done → **Dev Code Review** |
| `/review-and-deploy`: re-runs the checks, AI review as an aid, then approve or reject | reviewer | approve → **DevOps Staging**, reject → Dev in Process (back to the author) |
| Jenkins deploys (every 30 minutes, or the merge job by hand) | Jenkins | → **Dev Review Staging** |
| Staging review, then move the ticket on in Assembla | reviewer | → **QA Queue Staging** |

- **One ticket at a time.** Only one of your Node tickets per repo may be in Dev in Process: finish or pause it (`/dev-ticket pause N`) before starting the next.
- **Work the Node ticket.** Requirement tickets (`Node = -NA-`) are moved by the BA / lead. A ticket for the other
  repo is refused with the repo to open.
- **DB changes** go on the DB ticket (`Node = CMGMT-DB`, Related to the same requirement), on its own `FB_<n>` branch in
  `Launchpad - CMGMT-DB`. Claude finds that ticket for you.
- **Reviewers who write code** hand off as usual and may assign the ticket to another reviewer **or to themselves**;
  then they review, approve and deploy it with `/review-and-deploy` (same checks and approval comment as any review).
- **Each new release:** the lead updates `current_milestones` in `python_scripts/profiles/launchpad.json` and shares a
  new bundle.

Details for Claude are in `skills/dev-ticket/PROFILE_LAUNCHPAD.md`. You don't need to read it.

## Troubleshooting

| Message | Fix |
|---|---|
| `dt quality` → `NO_TOKEN` / `NOT_AUTHENTICATED` | add `SONAR_TOKEN` / `SNYK_TOKEN` (step 4b2) |
| `dt quality` → `TOOL_MISSING` | `dotnet tool install -g dotnet-sonarscanner`, or for MAI-DC install Maven (step 4b2) |
| `dt quality` → `SCAN_FAILED` on a MAI-DC Java app, "Could not resolve … c2m-starter-parent" | your `~/.m2/settings.xml`: ask a teammate for theirs |
| ticket back in Dev Queue with a "Build failed on code quality" comment | Jenkins failed the build on Sonar or Snyk: `/dev-ticket N` takes it up as a rework |
| hand-off says `TEAMS_NOT_CONFIGURED` / `TEAMS_BAD_URL` | add or correct the Teams address (step 4c), then ask Claude to send the Teams message again (`dt teams-notify N`) |
| hand-off says `TEAMS_FAILED` | the ticket is assigned; the workflow may have been deleted: ask the lead, then `dt teams-notify N` |
| `config-check` → `not inside a git repo` / `not one of the profile's code repos` | VS Code is open on the wrong folder (step 7) |
| `DB repo not found` | the CMGMT-DB path (steps 2 / 4a) |
| `HTTP 401/403` on `whoami` | wrong key or secret in `credentials.env` |
| `NO_CURRENT_MILESTONE` / `WRONG_MILESTONE` | the ticket is not in the release listed in `current_milestones`: ask the lead |
| `MULTIPLE` on pick | move all but one of your tickets back to Dev Queue |
| `/dev-ticket` not offered | the `.claude/commands` link (step 5), then reload the window |
