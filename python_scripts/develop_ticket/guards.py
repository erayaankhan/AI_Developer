"""Guards shared by several commands: the developer-role check, the in-process ticket count and the hand-off evidence check."""

import subprocess

from assembla_api import AssemblaApi

from develop_ticket import common
from develop_ticket.common import STATUS


def developer_problem(state: dict):
    """None for a reviewer (or an unset role), else why a developer may not deploy. The ticket's own role wins; a ticket
    that has none yet (started before roles existed) uses the developer's remembered one."""
    role = (state or {}).get("role") or common._remembered_role()
    if role != "developer":
        return None
    return ("A developer hands the ticket to a reviewer in Code Review; the reviewer approves and deploys "
            "(dt assign). Run `dt role` if your role is wrong.")


def my_in_process(api: AssemblaApi, me_id: str, current_ids: set) -> tuple:
    """(in_process, dev_queue, in_process_elsewhere, source) for tickets assigned to me.

    Only tickets in a _CURRENT milestone are pickable (team decision 2026-09-29);
    Dev In Process tickets in other milestones are returned separately, for information.
    /tickets/my_active is the primary source; if it comes back empty the _CURRENT
    milestones are scanned as a fallback, since an empty body from Assembla is not
    always trustworthy.
    """
    tickets = api.get_my_active_tickets()
    source = "my-active"
    if not tickets:
        tickets = [t for mid in current_ids for t in api.get_milestone_tickets(mid)]
        source = "current-milestone-scan"
    mine = [t for t in tickets if t.get("assigned_to_id") == me_id and not common.is_db_companion(api, t)]
    if common.CONFIG.get("repo_node"):  # a profile with one Node per repo: only this repo's Node tickets are pickable here
        mine = [t for t in mine if common.repo_node_problem(t) is None]
    in_process_all = [t for t in mine if t.get("status") == STATUS["in_process"]]
    in_process = [t for t in in_process_all if t.get("milestone_id") in current_ids]
    elsewhere = [t for t in in_process_all if t.get("milestone_id") not in current_ids]
    queue = [t for t in mine if t.get("status") == STATUS["queue"] and t.get("milestone_id") in current_ids]
    return in_process, queue, elsewhere, source


def handoff_evidence_problem(number: str, state: dict):
    """None when the recorded gates / risk / functional results still describe the code on this branch, else
    (RESULT status, message). Used before an author-path deploy (`--requested`): `state.stage` alone only says the
    hand-off once happened, not that nothing was pushed after it. The fingerprints hash the working-tree files, so the
    checkout must be on FB_N with nothing uncommitted for them to describe what origin holds."""
    import functional
    import local_gates  # lazy: it reconfigures stdout on import
    branch = common.branch_name(number)
    try:
        on_branch = common.git("branch", "--show-current").stdout.strip()
        dirty = common.uncommitted_tracked()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return "FAILED", f"Could not read the git state: {exc}"
    if on_branch != branch:
        return "WRONG_BRANCH", f"Check out {branch} first: the recorded evidence can only be compared with that branch."
    if dirty:
        return "NOT_COMMITTED", "Commit and push everything first (dt pushed): the evidence must describe what is on origin."
    last = state.get("gates") or {}
    if last.get("status") not in ("PASS", "NO_CHANGES"):
        return "GATES_REQUIRED", "No passing gates run on record: run local_gates.py --ticket N."
    if local_gates.diff_fingerprint(local_gates.changed_files()) != last.get("fingerprint"):
        return "GATES_STALE", "The files changed since the last gates run: re-run local_gates.py --ticket N."
    from develop_ticket.functional_cmds import auto_restore_env
    auto_restore_env(number, state)
    problem = functional.gate_problem(state, lambda: local_gates.diff_fingerprint(local_gates.changed_files()))
    if problem:
        return problem
    risk = state.get("risk_check") or {}
    if risk.get("fingerprint"):
        files = local_gates.risk_files() if risk.get("fp_auto") else risk.get("fp_files") or []
        if local_gates.diff_fingerprint(files) != risk["fingerprint"]:
            return "RISK_STALE", f"The diff changed since the last risk check: re-run risk_check.py --ticket {number}."
    return None
