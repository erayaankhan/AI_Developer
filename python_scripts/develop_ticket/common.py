"""Shared helpers and constants of the dt commands: config, git, ticket folder and state access."""

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from assembla_api import AssemblaApi, AssemblaError, bare_number
from config_loader import load_config, resolve_project_root
import metrics
from state_io import write_state


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # python_scripts/, next to the config files


CONFIG = load_config()  # dev_ticket.config.json + optional dev_ticket.config.local.json


PROJECT_ROOT = resolve_project_root(CONFIG)  # "auto": the git repo of the current directory


STATUS = CONFIG["statuses"]


# Status meanings a team profile may override (read at call time, so a test that patches STATUS is honoured):
# - approved: where a reviewer's approval moves the ticket (default `done`). A team without an "approved, not yet
#   deployed" status sets it to its staging status, so the approval itself requests the deploy.
# - rejected: where a reviewer's rejection moves the ticket (default `queue`).
# - dev_done: optional developer-side status between in_process and code_review ("code committed"). The hand-off
#   (`dt status N --to <code_review>`) then passes through it.
def approved_status() -> str:
    return STATUS.get("approved") or STATUS["done"]


def rejected_status() -> str:
    return STATUS.get("rejected") or STATUS["queue"]


def dev_done_status():
    return STATUS.get("dev_done") or None


BASE = CONFIG["base_branch"]


def result(status: str, exit_code: int = 0, **fields) -> int:
    print("RESULT: " + json.dumps(dict(status=status, **fields), ensure_ascii=False))
    return exit_code


def confirm_token(command: str, payload) -> str:
    """Short hash of exactly what a command would do. A preview returns it; `--yes` must carry it back as `--confirm`, so
    the action applied is the one that was previewed (a file, title or status that changed in between gives a new hash)."""
    text = json.dumps([command, payload], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def confirm_refusal(command: str, number: str, args, payload):
    """None when `--yes` carries the token of this payload, else the printed RESULT's exit code (CONFIRM_REQUIRED /
    PREVIEW_CHANGED). The token is never echoed here: it comes from a preview the developer was shown."""
    given = getattr(args, "confirm", None)
    if not given:
        return result("CONFIRM_REQUIRED", 1, ticket=number, command=command,
                      message=f"Run `{command}` without --yes first, show the developer the preview, and only on their yes "
                              "re-run with --yes --confirm <the hash the preview returned>.")
    if given != confirm_token(command, payload):
        return result("PREVIEW_CHANGED", 1, ticket=number, command=command,
                      message="What would be applied is not what the confirmed preview showed (the file, title, status or "
                              "code changed). Run the preview again, show it, and confirm that one.")
    return None


def record_metric(number, event: str, **fields) -> None:
    """D-53: append a quality event to DEV_TICKETS/{N}/metrics.jsonl (no-op without a work folder)."""
    metrics.record(PROJECT_ROOT, CONFIG["tickets_dir"], bare_number(number), event, **fields)


def _role_env():
    """(env file path, env key, allowed roles) of the remembered developer/reviewer role of whoever runs the workflow.
    The role lives in the git-ignored, per-person credentials.env, never in the tracked local_env.env."""
    return (PROJECT_ROOT / CONFIG["roles"]["env_file"],
            CONFIG["roles"]["env_key"], CONFIG["roles"]["values"])


def _remembered_role():
    """'developer' | 'reviewer' | None (unset). Reads only the one role key of the local env file."""
    import functional
    path, key, allowed = _role_env()
    return functional.read_role(path, key, allowed)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def branch_name(number) -> str:
    return f"{CONFIG['branch_prefix']}{bare_number(number)}"


def ticket_row(api: AssemblaApi, t: dict) -> dict:
    return {
        "ticket": t.get("number"),
        "summary": t.get("summary"),
        "ticket_status": t.get("status"),
        "assignee": api.user_label(t.get("assigned_to_id")),
        "updated": t.get("updated_at"),
    }


RELATED = 2  # Assembla ticket association "Related" (0 is Parent)


def is_db_node(ticket: dict) -> bool:
    """The ticket's Node is C2M-DB. This is what the staging job goes by to deploy it from the DB repo."""
    node = ((ticket.get("custom_fields") or {}).get(CONFIG["deploy_attributes"]["node_field"]) or "").strip()
    return node == CONFIG["db"]["node_value"]


def is_db_companion(api: AssemblaApi, ticket: dict) -> bool:
    """A DB ticket that travels with a main ticket (D-71, refined): Node = C2M-DB **and** a Related link to a ticket
    whose Node is not C2M-DB. Such a ticket never counts as a second ticket in progress. A C2M-DB ticket with no such
    link (a stray or shared one) is not a companion and counts like any other ticket."""
    if not is_db_node(ticket):
        return False
    for assoc in api.get_associations(ticket["number"]):
        if assoc.get("relationship") != RELATED:
            continue
        other_id = assoc.get("ticket2_id") if assoc.get("ticket1_id") == ticket.get("id") else assoc.get("ticket1_id")
        other = api.get_ticket_by_id(other_id) if other_id else {}
        if other and not is_db_node(other):
            return True
    return False


# ---------------------------------------------------------------------- fetch

def ticket_node(ticket: dict) -> str:
    return ((ticket.get("custom_fields") or {}).get(CONFIG["deploy_attributes"]["node_field"]) or "").strip()


def repo_node_problem(ticket: dict):
    """None when the ticket belongs to the code repo this run works in, else (status, message). Only for a profile whose
    repo overlay sets `repo_node` (e.g. Node = CMGMT for 'Launchpad - CMGMT'); without it every ticket is accepted."""
    want = CONFIG.get("repo_node")
    if not want:
        return None
    node = ticket_node(ticket)
    if node == want:
        return None
    if node and node == (CONFIG.get("db") or {}).get("node_value"):
        return "DB_TICKET", (f"#{ticket.get('number')} is a DB ticket (Node = {node}): it is worked together with its code "
                             "ticket. Pick the code ticket here; `dt db-ticket <code ticket>` finds this one.")
    repos = CONFIG.get("repos") or {}
    other = next((r.get("folder") for r in repos.values() if (r.get("config") or {}).get("repo_node") == node), None)
    if other:
        return "WRONG_REPO", (f"#{ticket.get('number')} has Node = {node}: it is worked in '{other}', not here "
                              f"('{PROJECT_ROOT.name}'). Open Claude in that repo and run /dev-ticket {ticket.get('number')}.")
    return "NOT_A_NODE_TICKET", (f"#{ticket.get('number')} has Node = '{node or '(empty)'}', not {want}. Work the Node "
                                 "ticket of this repo (a requirement ticket with Node -NA- is moved by the BA / lead).")


def ticket_dir(number: str) -> Path:
    """D-31: one work folder per ticket, {ROOT}/DEV_TICKETS/{N}, whatever its type."""
    return PROJECT_ROOT / CONFIG["tickets_dir"] / number


# ---------------------------------------------------------------------- branch

DB_REPO = (SCRIPT_DIR / CONFIG["db"]["repo_path"]).resolve()


def git(*args, check=True, timeout=120, repo: Path = None) -> subprocess.CompletedProcess:
    # UTF-8 explicitly (Windows would decode with the ANSI code page and garble or choke on non-ASCII names);
    # quotepath off so non-ASCII file names come back as-is instead of octal-escaped and quoted.
    proc = subprocess.run(["git", "-C", str(repo or PROJECT_ROOT), "-c", "core.quotepath=false", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def master_sync_info() -> tuple:
    """(ahead, behind, conflicting files) of HEAD vs origin/master.

    merge-tree answers "would merging master conflict?" without touching the working
    tree or creating a commit. Caller must have fetched origin/master.
    """
    behind = int(git("rev-list", "--count", f"HEAD..origin/{BASE}").stdout.strip() or 0)
    ahead = int(git("rev-list", "--count", f"origin/{BASE}..HEAD").stdout.strip() or 0)
    conflicts = []
    if behind:
        mt = git("merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", f"origin/{BASE}", check=False)
        if mt.returncode == 1:
            conflicts = [l for l in mt.stdout.splitlines()[1:] if l.strip()]
        elif mt.returncode != 0:
            # 0 = clean merge, 1 = conflicts; anything else (e.g. 129 on git < 2.38, which lacks --write-tree)
            # means we could NOT tell, and "no conflicts" would be a guess.
            raise RuntimeError("could not check for merge conflicts: `git merge-tree --write-tree` failed "
                               f"(exit {mt.returncode}; needs git >= 2.38): {(mt.stderr or mt.stdout).strip()[:200]}")
    return ahead, behind, conflicts


def is_local_only_path(name: str) -> bool:
    return any(re.search(p, name.replace("\\", "/")) for p in CONFIG.get("local_only_patterns", []))


def uncommitted_tracked(repo: Path = None) -> list:
    """Tracked files with staged OR unstaged changes (porcelain lines). Local-only files (WF repo) don't count."""
    lines = [l for l in git("status", "--porcelain", "--untracked-files=no", repo=repo).stdout.splitlines() if l.strip()]
    return lines if repo else [l for l in lines if not is_local_only_path(l[3:].strip())]


def start_dirty_files() -> list:
    """Every uncommitted tracked change AND every untracked (not git-ignored) file in the WF repo, as porcelain
    lines, minus local-only files. A ticket may only start from a clean tree: from then on everything that
    changes is the ticket's own, so the gates and the risk check never have to guess whose file it is."""
    lines = git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return [l for l in lines if l.strip() and not is_local_only_path(l[3:].strip().strip('"'))]


def _write_state(path: Path, state: dict, note: str) -> None:
    state.setdefault("history", []).append({"stage": state.get("stage"), "result": note, "at": now_iso()})
    write_state(path, state)


# ---------------------------------------------------------------------- write actions

def inside_ticket_folder(number: str, file_arg: str) -> bool:
    """Only files in DEV_TICKETS/<N>/ may be posted or attached: never credentials.env or anything else on disk."""
    return Path(file_arg).resolve().is_relative_to(ticket_dir(number).resolve())


def push_state(number: str, repo: Path = None) -> dict:
    """Committed + pushed state of FB_<number> in repo (WF repo by default); never raises."""
    branch = branch_name(number)
    try:
        if git("branch", "--show-current", repo=repo).stdout.strip() != branch:
            return {"status": "WRONG_BRANCH", "branch": branch}
        if uncommitted_tracked(repo):
            return {"status": "NOT_COMMITTED", "branch": branch}
        git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False, repo=repo)
        remote = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False, repo=repo).stdout.strip()
        local = git("rev-parse", "HEAD", repo=repo).stdout.strip()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"status": "FAILED", "branch": branch, "message": str(exc)}
    if remote and remote == local:
        return {"status": "PUSHED", "branch": branch, "head": local[:12]}
    return {"status": "NOT_PUSHED", "branch": branch}


# ---------------------------------------------------------------------- state.json writes (item 2)

TIER_ORDER = ["LOW", "MEDIUM", "HIGH"]


def load_state(number: str):
    """(state dict, path) for the ticket's work folder, or ({}, None)."""
    path = ticket_dir(number) / "state.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8")), path
    return {}, None


# ---------------------------------------------------------------------- self-review (config `allow_self_review`)

def self_review_allowed() -> bool:
    """True when a reviewer may assign, review, approve and deploy their own hand-off (team profiles opt in)."""
    return bool(CONFIG.get("allow_self_review"))


def parked_author_dir(number: str) -> Path:
    """Where the author's own work folder waits while the same person reviews the ticket in DEV_TICKETS/{N}."""
    return PROJECT_ROOT / CONFIG["tickets_dir"] / f"{number}.author"


def park_author_folder(number: str) -> Path:
    """Self-review: move the author's DEV_TICKETS/{N} aside to {N}.author so the review gets its own folder."""
    target = parked_author_dir(number)
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    ticket_dir(number).rename(target)
    return target


def restore_author_folder(number: str, review_round) -> Path:
    """Self-review rejected: keep the review as {N}.review-r{round} and put the author's folder back as DEV_TICKETS/{N}."""
    folder, parked = ticket_dir(number), parked_author_dir(number)
    keep = folder.with_name(f"{number}.review-r{review_round or 1}")
    suffix = 2
    while keep.exists():
        keep = folder.with_name(f"{number}.review-r{review_round or 1}-{suffix}")
        suffix += 1
    folder.rename(keep)
    parked.rename(folder)
    return keep


def move_db_companion(api: AssemblaApi, state: dict, previous: str, target: str):
    """D-71: a status move of the main ticket is followed by its DB ticket (state.db.ticket), but only while that ticket
    is still where the main ticket was. Never forces: a mismatch or an Assembla error is reported, not retried."""
    db_number = (state.get("db") or {}).get("ticket")
    if not db_number:
        return None
    try:
        current = api.get_ticket(db_number).get("status")
        if current != previous:
            return {"db_ticket": db_number, "moved": False,
                    "reason": f"the DB ticket is in '{current}', not '{previous}': move it by hand if it should follow"}
        api.update_status(db_number, target)
        return {"db_ticket": db_number, "moved": True, "to": target}
    except AssemblaError as exc:
        return {"db_ticket": db_number, "moved": False, "reason": str(exc)}
