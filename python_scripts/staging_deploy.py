#!/usr/bin/env python3
"""
Staging deploy for LOW-path tickets - step 8-LOW.5.4 of the dev-ticket workflow (D-8, D-11).

The actual merge/build/deploy is done by `deploy_staging_local.py` (same folder: a self-contained
copy of CR-9131's deploy_on_cloud_staging_v1.py), which this script runs as a subprocess. It deploys
ONE ticket per run: the lowest-numbered Plan level = Story ticket in DevOps Staging in the _CURRENT
milestone. So this script only runs it when our ticket is first in that queue.

    python staging_deploy.py check            read-only: deploy script + credentials + build server visible?
    python staging_deploy.py queue N          read-only: where is N in the DevOps Staging queue?
    python staging_deploy.py confirm N        read-only: after a deploy, wait for 'Staging Code Merged' -> QA Queue /
                                              Merge Failed / Build Failed (never triggers)
    python staging_deploy.py deploy N --yes   guards -> trigger (GENERATE_QA_TESTCASES=false) -> wait
                                              -> verify QA Queue

A profile with staging_deploy.adapter = "code_merge" (C2M CI) uses DevOps' Code_Merge job instead: see code_merge.py.
The same guards apply, except the queue: that job takes one ticket per run from the whole space, so other tickets in
DevOps Staging need an explicit acceptance (--accept-queue TOKEN), and a running/queued build refuses (BUSY).

Guards before triggering (any failure = no trigger):
    - LOW path allowed for N (state.json: risk=LOW, path=LOW, risk_check.min_tier=LOW),
      OR `--requested` (MEDIUM/HIGH, only when the developer explicitly asks: state.stage must be
      8-handoff/done), OR `--reviewer` (a reviewer deploying a developer's ticket: an APPROVE verdict, `dt review-deploy`
      done, caller on `reviewers` and not the author, origin still at the approved head; a review folder is refused
      by the other two modes). All the Assembla/origin/queue/Jenkins guards below still apply.
    - N is in DevOps Staging, Plan level = Story, in the _CURRENT milestone
    - N is the lowest-numbered Story in DevOps Staging        (else QUEUED, D-11)
    - FB_N exists on origin
    (Information only, never a guard: the preview and result list which changed folders have a build job,
     staging_deploy.app_folders in dev_ticket.config.json. FB_N is merged into master either way.)
    - no other local deploy run is in progress                (else BUSY; lock file in DEV_TICKETS/_deploy)

Long-running (the job can take 45+ minutes): run it in the background.

Credentials: JENKINS_REMOTE_USER, JENKINS_REMOTE_API_TOKEN (the build server) + ASSEMBLA_API_KEY/SECRET
from AI_DEV_ENV/credentials.env, falling back to environment variables.
Ends with: RESULT: {"status": "...", ...}
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assembla_api import AssemblaApi, AssemblaError, bare_number  # noqa: E402
import code_merge  # noqa: E402
from state_io import update_state  # noqa: E402
from develop_ticket.guards import handoff_evidence_problem  # noqa: E402
from develop_ticket.ungated import ungated_files  # noqa: E402
from dev_ticket import (CONFIG, DB_REPO, PROJECT_ROOT, STATUS, _reviewer_problem, branch_name,  # noqa: E402
                        is_db_node, developer_problem, load_state, low_path_problems, now_iso, result)

DEPLOY = CONFIG["staging_deploy"]
STORY = CONFIG["deploy_attributes"]["plan_level"]
CODE_MERGE = DEPLOY.get("adapter") == "code_merge"  # DevOps' Code_Merge job instead of deploy_staging_local.py

# D-44: this workflow never generates QA test cases; the local script has that (and the browser QA
# Phase 3) removed. Staging checks are done by Claude in Chrome after the deploy; the developer signs
# in in the browser (D-45).
QA_DISABLED_MARKER = "QA test-case generation: DISABLED"
DEPLOY_SCRIPT = Path(__file__).resolve().parent / "deploy_staging_local.py"
LOCK_FILE = PROJECT_ROOT / CONFIG["tickets_dir"] / "_deploy" / "deploy.lock"


def lock_holder():
    """PID of a live deploy run holding the lock, else None (a stale lock is ignored)."""
    try:
        pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return pid if str(pid) in out else None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def db_remote_url() -> str:
    """The DB repo's origin URL (a DB ticket's branch lives there), or "" when it can't be read."""
    proc = subprocess.run(["git", "-C", str(DB_REPO), "remote", "get-url", "origin"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def owner_of(db_number: str):
    """(owner WF ticket number, its state, its state.json path) of the local ticket whose state.db.ticket is this DB
    ticket, else None. A DB ticket has no work folder of its own: its hand-off state and its one-trigger-only deploy
    attempt are kept in its owner's state.json."""
    tickets = PROJECT_ROOT / CONFIG["tickets_dir"]
    for state_path in sorted(tickets.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str((state.get("db") or {}).get("ticket")) == str(db_number):
            return state_path.parent.name, state, state_path
    return None


def attempt_slot(number: str, db: bool):
    """(state, path, key) holding the deploy attempt of this ticket: its own `deploy_attempt`, or for a DB ticket the
    `db_deploy_attempt` in its owner's state.json."""
    if db:
        found = owner_of(number)
        return (found[1], found[2], "db_deploy_attempt") if found else ({}, None, "db_deploy_attempt")
    state, path = load_state(number)
    return state, path, "deploy_attempt"


def run_deploy_script() -> tuple:
    """Run deploy_staging_local.py, echoing its output live. -> (exit code, full output)."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DB_GIT_REMOTE_URL=db_remote_url())  # lets the script look for a DB ticket's branch in the DB repo
    proc = subprocess.Popen([sys.executable, "-u", str(DEPLOY_SCRIPT)], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            cwd=str(PROJECT_ROOT), env=env)
    LOCK_FILE.write_text(str(proc.pid), encoding="utf-8")
    lines = []
    try:
        for line in proc.stdout:
            print(line.rstrip("\n"), flush=True)
            lines.append(line)
        return proc.wait(), "".join(lines)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------- queue

def staging_queue(api: AssemblaApi) -> tuple:
    """(milestone title, [Story tickets in DevOps Staging sorted by number]).

    Mirrors deploy_on_cloud_staging_v1.py's selection: first _CURRENT milestone,
    status DevOps Staging, hierarchy_type Story, lowest number first.
    """
    milestones = api.get_current_milestones()
    if not milestones:
        return None, []
    milestone = milestones[0]
    queue = [t for t in api.get_milestone_tickets(milestone["id"])
             if t.get("status") == STATUS["staging"] and t.get("hierarchy_type") == STORY]
    return milestone, sorted(queue, key=lambda t: int(t.get("number") or 0))


def queue_position(api: AssemblaApi, number: str) -> dict:
    milestone, queue = staging_queue(api)
    numbers = [str(t.get("number")) for t in queue]
    info = {"milestone": milestone.get("title") if milestone else None, "queue": numbers}
    if milestone is None:
        return dict(info, status="NO_CURRENT_MILESTONE")
    if number not in numbers:
        return dict(info, status="NOT_IN_QUEUE")
    ahead = numbers[: numbers.index(number)]
    return dict(info, status="FIRST" if not ahead else "QUEUED", ahead=ahead)


def cmd_queue(api: AssemblaApi, args) -> int:
    if CODE_MERGE:
        info = code_merge.queue_position(api, STATUS["staging"], bare_number(args.ticket))
        return result(info.pop("status"), ticket=bare_number(args.ticket), job=DEPLOY["job"], **info)
    info = queue_position(api, bare_number(args.ticket))
    return result(info.pop("status"), ticket=bare_number(args.ticket), **info)


# ---------------------------------------------------------------------- deploy

def origin_sha(branch: str, repo: Path = None) -> str:
    """Commit sha of the branch on origin ("" if it isn't there or origin can't be reached). `repo`: the DB repo."""
    proc = subprocess.run(["git", "-C", str(repo or PROJECT_ROOT), "ls-remote", "--heads", "origin", branch],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return proc.stdout.split()[0] if proc.returncode == 0 and proc.stdout.strip() else ""


def local_sha(branch: str, repo: Path = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo or PROJECT_ROOT), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def changed_app_folders(number: str) -> dict:
    """What merging FB_N into master would bring: its top-level folders, and which of them have a build job
    (config staging_deploy.app_folders). Folders without one (AI_DEV_ENV, docs, ...) are merged but never built."""
    branch = branch_name(number)
    git = ["git", "-C", str(PROJECT_ROOT)]
    subprocess.run(git + ["fetch", "--quiet", "origin", "master", branch], capture_output=True, timeout=120)
    diff = subprocess.run(git + ["diff", "--name-only", f"origin/master...origin/{branch}"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    if diff.returncode != 0:
        return {"error": (diff.stderr or "git diff failed").strip()[:200]}
    files = [f for f in diff.stdout.splitlines() if f.strip()]
    folders = sorted({f.split("/")[0] for f in files})
    merged = subprocess.run(git + ["merge-base", "--is-ancestor", f"origin/{branch}", "origin/master"],
                            capture_output=True, timeout=60).returncode == 0
    return {"files": len(files), "folders": folders, "merged_into_master": merged,
            "apps": [f for f in folders if f in DEPLOY["app_folders"]]}


def cmd_deploy(api: AssemblaApi, args) -> int:
    """Wrapper: record every deploy outcome as a D-53 quality event, then return it."""
    import io
    import contextlib
    import metrics

    class Tee(io.StringIO):
        """Show output live (the 'Triggered #N' line must survive a crash) and keep a copy for the RESULT."""
        def write(self, text):
            sys.__stdout__.write(text)
            sys.__stdout__.flush()
            return super().write(text)

    buffer = Tee()
    with contextlib.redirect_stdout(buffer):
        try:
            code = _deploy(api, args)
        except Exception as exc:  # noqa: BLE001 - always finish with a RESULT line, keep the build id
            state, _path, key = attempt_slot(bare_number(args.ticket), db_ticket_flag(api, bare_number(args.ticket)))
            attempt = (state or {}).get(key) or {}
            code = result("ERROR", 1, ticket=bare_number(args.ticket), build=attempt.get("build"),
                          message=f"{type(exc).__name__}: {exc}"
                                  + (" - a build WAS triggered; do not re-trigger." if attempt else ""))
    text = buffer.getvalue()
    match = re.search(r'^RESULT: (\{.*\})$', text, re.M)
    if match:
        outcome = json.loads(match.group(1))
        if outcome.get("status") != "PREVIEW":
            metrics.record(PROJECT_ROOT, CONFIG["tickets_dir"], bare_number(args.ticket), "deploy",
                           status=outcome.get("status"), build=outcome.get("build"))
    return code


def db_ticket_flag(api: AssemblaApi, number: str) -> bool:
    """True when the Assembla ticket is a DB ticket (Node = C2M-DB, which is what the staging job goes by)."""
    try:
        return is_db_node(api.get_ticket(number))
    except AssemblaError:
        return False


def archive_attempt(number: str, db: bool = False) -> None:
    """Move the deploy attempt into its history list so one new trigger is allowed."""
    state, path, key = attempt_slot(number, db)
    if path is not None and state.get(key):
        def archive(current):
            if current.get(key):
                current.setdefault(key + "s", []).append(dict(current.pop(key), retried_at=now_iso()))
        update_state(path, archive)


def _record_attempt(number: str, build, db: bool = False) -> None:
    """Persist the trigger before waiting, so a crash or timeout can never lead to a second trigger."""
    state, path, key = attempt_slot(number, db)
    if path is not None:
        attempt = {"build": build, "at": now_iso()}
        update_state(path, lambda current: current.update({key: attempt}))


def reviewer_deploy_problem(api: AssemblaApi, number: str, ticket: dict, role_state: dict, db: bool):
    """The extra guards of `--reviewer` (a reviewer deploying what they approved): None when allowed, else the RESULT
    already printed. The approval is bound to commits: origin's branch must still be at the head that was approved."""
    if role_state.get("kind") != "review":
        return result("NOT_A_REVIEW_FOLDER", ticket=number,
                      message="--reviewer is for a ticket you reviewed and approved on this machine (/review-and-deploy).")
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], ticket=number, message=problem[1])
    review = role_state.get("review") or {}
    if (review.get("verdict") or {}).get("verdict") != "APPROVE" or not review.get("approved_head"):
        return result("NOT_APPROVED", ticket=number, message="This review has no approval (dt review-verdict --approve).")
    if str(review.get("author", "")).lower() == str(me.get("login", "")).lower() and not CONFIG.get("allow_self_review"):
        return result("SELF_REVIEW", ticket=number, message="You wrote this hand-off: someone else deploys it.")
    if not review.get("deploy_requested"):
        return result("NOT_REQUESTED", ticket=number,
                      message="Run dt review-deploy N first: it checks the approval and moves the ticket to DevOps Staging.")
    if not db and ticket.get("assigned_to_id") != me["id"]:
        return result("NOT_ASSIGNED", ticket=number, message="The ticket is not assigned to you.")
    approved = review.get("approved_db_head") if db else review["approved_head"]
    tip = origin_sha(branch_name(number), DB_REPO if db else None)
    if tip != approved:
        return result("APPROVAL_STALE", ticket=number, approved_head=str(approved)[:12], origin_head=tip[:12],
                      message="origin's branch is not the head that was approved: the author pushed after the approval. "
                              "Not triggered: withdraw the approval (dt review-withdraw) and review again.")
    return None


def _deploy(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    ticket = api.get_ticket(number)
    # D-71: a DB ticket is deployed by the same job as a separate Story, in its own run. It is never a LOW-path
    # ticket, has no work folder of its own (its hand-off state lives in its owner's state.json), and its branch is in
    # the DB repo.
    db = is_db_node(ticket)
    repo = DB_REPO if db else None

    # A developer never deploys: clear message before any guard or credentials check (the Jenkins keys are the real gate).
    if db:  # a DB ticket keeps its hand-off state in its owner's state.json
        found = owner_of(number)
        role_state = found[1] if found else {}
    else:
        role_state = load_state(number)[0] or {}
    problem = developer_problem(role_state)
    if problem:
        return result("NOT_A_REVIEWER", ticket=number, message=problem)
    if role_state.get("kind") == "review" and not getattr(args, "reviewer", False):
        # The LOW and --requested paths do not check the approval or the approved head: a reviewer's review folder
        # (stage `done`) must not slip through them.
        return result("USE_REVIEWER_MODE", ticket=number,
                      message="This ticket is a reviewer's review of a developer's work: deploy it with --reviewer, which "
                              "checks the approval and the approved head.")

    reviewer = getattr(args, "reviewer", False)
    if reviewer:
        refused = reviewer_deploy_problem(api, number, ticket, role_state, db)
        if refused is not None:
            return refused
        owner = found[0] if db and found else None  # (for the DB ticket's pre-check line)
    elif db:
        if not getattr(args, "requested", False):
            return result("NOT_LOW_PATH", ticket=number, reasons=["a DB ticket is never on the LOW path"],
                          message="A DB ticket is deployed only on the developer's request: use --requested.")
        found = owner_of(number)
        if found is None:
            return result("NO_OWNER", ticket=number,
                          message="No local ticket records this DB ticket as state.db.ticket, so its hand-off can't be "
                                  "checked. Deploy it from the ticket folder that created it.")
        owner, state = found[0], found[1]
        if state.get("stage") not in ("8-handoff", "done"):
            return result("NOT_HANDED_OFF", ticket=number, owner=owner, stage=state.get("stage"),
                          message=f"The owning ticket #{owner} has not finished the dev-ticket hand-off.")
    elif getattr(args, "requested", False):
        # MEDIUM / HIGH: never automatic. Only on the developer's explicit "deploy N"; the Assembla
        # prerequisites below still apply, and the ticket must have been through the workflow here.
        state = load_state(number)[0] or {}
        if state.get("stage") not in ("8-handoff", "done"):
            return result("NOT_HANDED_OFF", ticket=number, stage=state.get("stage"),
                          message="Requested deploy needs a ticket that finished the dev-ticket hand-off.")
    else:
        problems = low_path_problems(number)
        if problems:
            return result("NOT_LOW_PATH", ticket=number, reasons=problems,
                          message="Not a LOW-path ticket. To deploy it on the developer's request, use --requested.")

    if ticket.get("status") != STATUS["staging"]:
        return result("WRONG_STATUS", ticket=number, ticket_status=ticket.get("status"),
                      message=f"Ticket must be in '{STATUS['staging']}'.")
    if ticket.get("hierarchy_type") != STORY:
        return result("NOT_STORY", ticket=number, message="The deploy job only picks Plan level = Story.")
    remote = origin_sha(branch_name(number), repo)
    if not remote:
        return result("BRANCH_NOT_ON_ORIGIN", ticket=number, branch=branch_name(number),
                      message="The deploy job would skip this ticket and move it to QA Queue undeployed"
                              + (" (looked in the DB repo)." if db else "."))
    local = local_sha(branch_name(number), repo)
    if local != remote:  # the job deploys what is on origin: it must be exactly what was reviewed and gated here
        return result("ORIGIN_DIFFERS", ticket=number, branch=branch_name(number), origin=remote[:12],
                      local=local[:12] or None,
                      message="origin's branch is not the same commit as the local one (unpushed commits, or "
                              "someone else pushed). Not triggered: the reviewed code is not what would deploy.")
    if getattr(args, "requested", False) and not reviewer and not db:
        # The hand-off evidence (gates, functional, risk) must still describe exactly this code, not an earlier push.
        stale = handoff_evidence_problem(number, role_state)
        if stale:
            return result(stale[0], ticket=number, message=stale[1] + " Not triggered.")

    # Informational only, never blocks: the run merges FB_N into master even when no app has a build job.
    if db:  # the DB merge brings SQL scripts, not application folders: there is no build-list pre-check
        precheck = {"is_db_ticket": True, "owner": owner}
    else:
        change = changed_app_folders(number)
        precheck = ({"precheck_error": change["error"]} if "error" in change else
                    {"merge_brings": change["folders"], "apps_with_build_job": change["apps"],
                     "already_in_master": change["merged_into_master"]})

    # The job merges the whole branch: files no gates run (or DB-file list) covered ride along unreviewed. The preview
    # lists them; --yes needs the developer's explicit acceptance of exactly this list (--accept-ungated TOKEN).
    ungated = ungated_files(number, role_state, db)
    if ungated.get("error"):
        precheck = {**precheck, "ungated_check_error": ungated["error"]}
    elif ungated:
        precheck = {**precheck, "ungated_files": ungated}
    if args.yes and ungated.get("error"):
        return result("UNGATED_CHECK_FAILED", ticket=number, message=f"Could not work out which files the branch brings "
                      f"beyond the gated ones ({ungated['error']}). Not triggered.")
    if args.yes and ungated and getattr(args, "accept_ungated", None) != ungated.get("token"):
        return result("UNGATED_FILES", ticket=number, ungated_files=ungated,
                      message=f"The branch brings {ungated['count']} file(s) no gates run covered. Show the developer this "
                              "list; only on their yes re-run with --accept-ungated " + ungated["token"] + ". Not triggered.")
    if CODE_MERGE:
        info = code_merge.queue_position(api, STATUS["staging"], number)
        if info["status"] == "NOT_IN_QUEUE":
            return result("NOT_IN_QUEUE", ticket=number, queue=info["queue"])
        if info["status"] == "SHARED":
            precheck = {**precheck, "others_in_staging": info["others"], "queue_token": info["token"]}
            if args.yes and getattr(args, "accept_queue", None) != info["token"]:
                # D-11 for a job whose order we can't see: a run may merge one of these instead of (or before) ours.
                return result("SHARED_QUEUE", ticket=number, others=info["others"], token=info["token"],
                              message=f"{len(info['others'])} other ticket(s) in {STATUS['staging']} across the space: "
                                      f"{DEPLOY.get('job')} takes ONE per run in an order we can't see, so this run may "
                                      "merge and deploy one of them instead. Show the list; only on an explicit yes re-run "
                                      "with --accept-queue " + info["token"] + ". Not triggered.")
        info["milestone"] = None
    else:
        info = queue_position(api, number)
        if info["status"] != "FIRST":
            # D-11: never trigger a deploy that would ship someone else's ticket.
            return result(info.pop("status"), ticket=number, **info,
                          message="Not triggered - the job deploys the lowest-numbered ticket first.")

    missing = [k for k in ("JENKINS_REMOTE_USER", "JENKINS_REMOTE_API_TOKEN") if not os.environ.get(k)]
    if missing:
        return result("ERROR", 1, ticket=number,
                      message=f"{', '.join(missing)} not set - add them to AI_DEV_ENV/credentials.env (SETUP.md §2).")
    if token_format_problem(os.environ["JENKINS_REMOTE_API_TOKEN"]):
        return result("BAD_TOKEN_FORMAT", 1, ticket=number,
                      message=token_format_problem(os.environ["JENKINS_REMOTE_API_TOKEN"]))

    attempt = (attempt_slot(number, db)[0] or {}).get("db_deploy_attempt" if db else "deploy_attempt")
    if attempt and getattr(args, "retry", False) and (getattr(args, "requested", False) or reviewer) and args.yes:
        archive_attempt(number, db)  # the developer said the earlier run failed and asked for a retry
        attempt = None
    if attempt:  # "never re-trigger": one trigger per ticket, whatever its outcome
        return result("ALREADY_TRIGGERED", 1, ticket=number, build=attempt.get("build"), at=attempt.get("at"),
                      message="A deploy was already triggered for this ticket. Not triggering again; check the build "
                              "and the ticket status. If the developer says that run failed and asks for a retry: "
                              "deploy N --requested --retry --yes.")

    holder = lock_holder()
    if holder:
        return result("BUSY", ticket=number, pid=holder, message="A local deploy run is already in progress - not started.")
    if CODE_MERGE:
        return _deploy_code_merge(api, number, db, args, precheck)
    if not args.yes:
        return result("PREVIEW", ticket=number, script=DEPLOY_SCRIPT.name, milestone=info["milestone"], **precheck,
                      message="All guards passed. Re-run with --yes to start.")
    _record_attempt(number, "local", db)
    print(f"Starting {DEPLOY_SCRIPT.name} - waiting for it to finish...", flush=True)
    code, console = run_deploy_script()
    outcome = "SUCCESS" if code == 0 else "FAILURE"
    job = DEPLOY_SCRIPT.name
    build = "local"

    selected = re.search(r"Single-ticket mode: selected (\S+)", console)
    selected_number = bare_number(selected.group(1)) if selected else None
    escalations = [l.strip() for l in console.splitlines() if l.startswith("ESCALATION:")][:5]
    base = {"ticket": number, "job": job, "build": build, "selected": selected_number,
            "qa_generation_disabled": QA_DISABLED_MARKER in console, **precheck}

    if selected_number and selected_number != number:
        return result("DEPLOYED_OTHER_TICKET", 1, **base,
                      message=f"The run picked #{selected_number}, not #{number} - queue changed after the check.")
    if outcome != "SUCCESS":
        return result("DEPLOY_FAILED", 1, **base, outcome=outcome, escalations=escalations)

    apps = re.search(r"Applications to build this cycle: (\[.*\])", console)
    nothing_built = bool(apps) and apps.group(1).strip() == "[]"
    final = await_final_status(api, number)
    if final == DEPLOY["done_status"] and nothing_built:
        return result("DEPLOYED_NO_APPS", **base, final_status=final, db_ticket=db_ticket_status(api, number),
                      message=("DB ticket: its branch was merged in the DB repo and the job sent the scripts to the "
                               "database; no application build is expected. Check the merge-and-deploy console if "
                               "unsure." if db else
                               "The run succeeded and the branch was merged into master, but no application was "
                               "built (empty build list). Expected when the merge brought no folder with a build job "
                               "(see apps_with_build_job); otherwise check the merge-and-deploy console."))
    if final == DEPLOY["done_status"]:
        return result("DEPLOYED", **base, final_status=final, db_ticket=db_ticket_status(api, number))
    if final in DEPLOY["failed_statuses"]:
        return result("DEPLOY_FAILED", 1, **base, outcome=final, final_status=final,
                      message=f"The job merged/built but the ticket ended as '{final}'. Check the Jenkins build logs.")
    return result("DEPLOYED_STATUS_UNCONFIRMED", 1, **base, final_status=final,
                  message=f"Job succeeded but the ticket is still '{final}' (expected '{DEPLOY['done_status']}'). "
                          "Re-check with: staging_deploy.py confirm N. Do not re-trigger.")


def code_merge_job():
    import deploy_staging_local as local  # same folder; reads the same credentials.env
    client = local.JenkinsClient(os.environ["JENKINS_REMOTE_USER"], os.environ["JENKINS_REMOTE_API_TOKEN"],
                                 local.JENKINS_REMOTE_BASE_URL)
    return code_merge.CodeMergeJob(client, DEPLOY["job"])


def _deploy_code_merge(api: AssemblaApi, number: str, db: bool, args, precheck: dict) -> int:
    """Trigger DevOps' Code_Merge job once and follow it (see code_merge.py). All the guards above have passed."""
    job = code_merge_job()
    busy = job.busy()
    if busy:  # the timer run (or someone's manual run) is working: it may be taking our ticket right now
        return result("BUSY", ticket=number, job=DEPLOY["job"], **busy,
                      message=f"{DEPLOY['job']} is already running or queued - not triggered. Wait for it, then check the "
                              "ticket: that run may take this ticket by itself.")
    if not args.yes:
        return result("PREVIEW", ticket=number, job=DEPLOY["job"], **precheck,
                      message="All guards passed. Re-run with --yes to trigger the job once.")
    _record_attempt(number, DEPLOY["job"], db)
    build = job.trigger()
    _record_attempt(number, build, db)
    print(f"Triggered {DEPLOY['job']} #{build} - waiting for it to finish...", flush=True)
    facts = job.wait(build)
    base = {"ticket": number, "job": DEPLOY["job"], "build": build, "selected": facts["tickets"],
            "apps": facts["apps"], "builds": facts["builds"], **precheck}
    if facts["nothing_to_merge"] or not facts["tickets"]:
        return result("NOT_PICKED", 1, **base, job_result=facts["result"],
                      message=f"The run found nothing it would merge, although #{number} is in {STATUS['staging']}: the job "
                              "skips this ticket (its own rules, e.g. the milestone or release type). Ask DevOps; do not "
                              "re-trigger.")
    if number not in facts["tickets"]:
        return result("DEPLOYED_OTHER_TICKET", 1, **base,
                      message=f"The run took #{', #'.join(facts['tickets'])}, not #{number}. Ours is still waiting: the "
                              "timer run (every 2 hours) or another trigger, on the developer's yes "
                              "(deploy N --reviewer|--requested --retry --yes), will take it.")
    if facts["result"] != "SUCCESS":
        return result("DEPLOY_FAILED", 1, **base, outcome=facts["result"], failed_apps=facts["failed_apps"],
                      message=f"{DEPLOY['job']} #{build} ended {facts['result']}. Check its console in Jenkins.")
    final = await_final_status(api, number)
    if final == DEPLOY["done_status"]:
        status = "DEPLOYED_NO_APPS" if facts["apps"] == [] else "DEPLOYED"
        return result(status, **base, final_status=final)
    if final in DEPLOY["failed_statuses"] or facts["failed_apps"] or facts["merge_failed"]:
        return result("DEPLOY_FAILED", 1, **base, final_status=final, failed_apps=facts["failed_apps"],
                      merge_failed=facts["merge_failed"], reset=number in facts["reset_for"],
                      message="The merge or a build failed" + (" and the job reset the merge" if number in
                                                               facts["reset_for"] else "") +
                              f"; the ticket is '{final}'. Check the build's console in Jenkins, fix, and hand off again.")
    return result("DEPLOYED_STATUS_UNCONFIRMED", 1, **base, final_status=final,
                  message=f"The job built everything but the ticket is still '{final}' (expected "
                          f"'{DEPLOY['done_status']}'). Re-check with: staging_deploy.py confirm N. Do not re-trigger.")


def await_final_status(api: AssemblaApi, number: str) -> str:
    """Poll Assembla until the ticket leaves the intermediate 'Staging Code Merged' state.

    After the merge the job triggers the app build jobs itself: QA Queue = all built, 'Merge Failed' /
    'Build Failed' = failure. Anything else (or the timeout) is returned as is.
    """
    deadline = time.time() + DEPLOY.get("build_timeout_minutes", 90) * 60
    while True:
        status = api.get_ticket(number).get("status")
        if status != DEPLOY["merged_status"] or time.time() >= deadline:
            return status
        time.sleep(DEPLOY["poll_seconds"])


def cmd_confirm(api: AssemblaApi, args) -> int:
    """Read-only follow-up for a deploy that ended 'Staging Code Merged': wait for the builds, never trigger."""
    number = bare_number(args.ticket)
    final = await_final_status(api, number)
    if final == DEPLOY["done_status"]:
        return result("DEPLOYED", ticket=number, final_status=final, db_ticket=db_ticket_status(api, number))
    if final in DEPLOY["failed_statuses"]:
        return result("DEPLOY_FAILED", 1, ticket=number, final_status=final)
    return result("DEPLOYED_STATUS_UNCONFIRMED", 1, ticket=number, final_status=final)


def db_ticket_status(api: AssemblaApi, number: str):
    """Informational: the paired DB ticket's number and Assembla status (None when there is none)."""
    db = ((load_state(number)[0] or {}).get("db") or {}).get("ticket")
    if not db:
        return None
    try:
        return {"ticket": db, "status": api.get_ticket(db).get("status")}
    except (AssemblaError, urllib.error.URLError):
        return {"ticket": db, "status": "UNKNOWN"}


def token_format_problem(token: str) -> str:
    """Catch common paste mistakes without ever echoing the token."""
    if token.lower().startswith(("basic ", "bearer ")):
        return ("JENKINS_API_TOKEN looks like an Authorization header ('Basic ...'). Put only the raw API "
                "token Jenkins showed you; the script builds the header itself.")
    if any(ch.isspace() for ch in token):
        return "JENKINS_API_TOKEN contains whitespace - paste the token on one line with nothing else."
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-8")
        if ":" in decoded and decoded.isprintable():
            return ("JENKINS_API_TOKEN looks like base64 of 'user:token' (the inside of a 'Basic' header). "
                    "Put only the raw API token Jenkins showed you.")
    except (ValueError, UnicodeDecodeError):
        pass
    return ""


def cmd_check(_api, _args) -> int:
    """Read-only: is the deploy script there, are the build-server credentials set, and do they work?"""
    if not DEPLOY_SCRIPT.is_file():
        return result("MISSING", 1, message=f"{DEPLOY_SCRIPT} not found.")
    user, token = os.environ.get("JENKINS_REMOTE_USER"), os.environ.get("JENKINS_REMOTE_API_TOKEN")
    if not user or not token:
        return result("MISSING", 1, message="JENKINS_REMOTE_USER / JENKINS_REMOTE_API_TOKEN not set "
                                            "(AI_DEV_ENV/credentials.env).")
    format_problem = token_format_problem(token)
    if format_problem:
        return result("BAD_TOKEN_FORMAT", 1, message=format_problem)
    import deploy_staging_local as local  # same folder; reads the same credentials.env
    base = local.JENKINS_REMOTE_BASE_URL
    jenkins = local.JenkinsClient(user, token, base)
    try:
        who = jenkins._get_json("/whoAmI/api/json") or {}
        if not who.get("authenticated") or who.get("anonymous"):
            return result("NOT_AUTHENTICATED", 1, base_url=base, message="Jenkins treated the request as anonymous.")
        job_name = DEPLOY["job"] if CODE_MERGE else local.MERGE_DEPLOY_JOB
        found = jenkins.job_exists(job_name)
    except urllib.error.HTTPError as exc:
        hint = {401: "username/token rejected - JENKINS_REMOTE_USER must be the Jenkins user ID and "
                     "JENKINS_REMOTE_API_TOKEN an API token for that same user",
                403: "authenticated but not allowed to read this job"}.get(exc.code, "")
        return result("HTTP_ERROR", 1, base_url=base, code=exc.code, hint=hint)
    except urllib.error.URLError as exc:
        return result("UNREACHABLE", 1, base_url=base, message=str(exc.reason))
    return result("OK" if found else "JOB_NOT_FOUND", 0 if found else 1, base_url=base,
                  jenkins_user=who.get("name"), job=job_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("queue").add_argument("ticket")
    sub.add_parser("confirm").add_argument("ticket")
    p = sub.add_parser("deploy")
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--requested", action="store_true",
                      help="developer explicitly asked to deploy this MEDIUM/HIGH ticket (skips the LOW-path guard only)")
    mode.add_argument("--reviewer", action="store_true",
                      help="a reviewer deploying a developer's ticket they approved (dt review-deploy first): checks the "
                           "approval and that origin is still at the approved head")
    p.add_argument("--accept-ungated", metavar="TOKEN",
                   help="the token of the ungated-files list the preview showed, after the developer accepted that list")
    p.add_argument("--accept-queue", metavar="TOKEN",
                   help="code_merge: the token of the other-tickets-in-staging list the preview showed, after an explicit yes")
    p.add_argument("--retry", action="store_true",
                   help="with --requested --yes: the developer says the previous run failed; archive it, allow one new trigger")
    args = parser.parse_args()
    if DEPLOY.get("enabled") is False:  # a team whose own Jenkins deploys from the staging status (profile setting)
        print("RESULT: " + json.dumps({"status": "FEATURE_OFF", "feature": "staging_deploy", "message": DEPLOY.get(
            "after_approval_note") or "Staging deploy is not run by this workflow for this team: the team's own job "
                                      "deploys tickets from the staging status."}))
        return 0
    try:
        if args.cmd == "check":
            return cmd_check(None, args)
        api = AssemblaApi()
        return {"queue": cmd_queue, "deploy": cmd_deploy, "confirm": cmd_confirm}[args.cmd](api, args)
    except AssemblaError as exc:
        return result("ERROR", 1, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - every run must end with a RESULT line, never a bare traceback
        return result("ERROR", 1, message=f"{type(exc).__name__}: {exc}",
                      note="Unexpected error. If this was a write (comment, status, upload), check Assembla "
                           "before retrying: it may have gone through.")


if __name__ == "__main__":
    sys.exit(main())
