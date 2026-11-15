"""The state.json commands and the status move: dt state, dt status and the LOW-path and stage guards."""

import re

from assembla_api import AssemblaApi, bare_number
from state_io import write_state

from develop_ticket import common
from develop_ticket.comments import needs_rca_clca
from develop_ticket.common import CONFIG, STATUS, TIER_ORDER, now_iso, record_metric, result
from develop_ticket.guards import developer_problem, handoff_evidence_problem, my_in_process
from develop_ticket.rca import rca_clca_missing, sweep_check


STATE_FIELDS = {
    "stage":         lambda v: v in CONFIG["stages"] + ["paused"],
    "risk":          lambda v: v in TIER_ORDER,
    "path":          lambda v: v in ("LOW", "STANDARD"),
    "deploy":        lambda v: v in ("queued", "busy", "deployed", "deployed_by_other_run", "failed"),
    "check.verdict": lambda v: v in ("READY", "NEEDS_INFO"),
    "db.ticket":     lambda v: v.isdigit(),
    "db.files":      lambda v: all(p.strip() for p in v.split(",")),
    # D-51: FB_D commit this ticket started from (db_checks measures "our" changes from it).
    "db.base":       lambda v: bool(re.fullmatch(r"[0-9a-f]{7,40}", v)),
    # D-51: a destructive DB script the developer explicitly confirmed (comma-separated DB paths).
    "db.destructive_confirmed": lambda v: all(p.strip() for p in v.split(",")),
    # A-4: the developer decided to go on although the baseline tests of a new branch fail (set only on their yes).
    "baseline.accepted": lambda v: v == "true",
    # Developer/reviewer: copied from the developer's remembered role (`dt role`) when the ticket is fetched.
    "role":          lambda v: v in CONFIG["roles"]["values"],
    # A reviewer's review of a developer's ticket keeps its own stage here, so the author's `stage` is never confused.
    # (`kind=review` is written only by `dt review-fetch`, never by --set.)
    "review.stage":  lambda v: v in CONFIG["review_stages"],
}


# staging_urls.<AppName>=<url> is also accepted (D-30).


def _set_nested(state: dict, key: str, value):
    target = state
    *parents, leaf = key.split(".")
    for part in parents:
        target = target.setdefault(part, {})
    target[leaf] = value


def cmd_state(_api, args) -> int:
    """Read or change DEV_TICKETS/N/state.json through validated keys only.

    Rules enforced here, not left to judgement:
      - risk can only go UP once the plan is confirmed (stage past 5-plan), never down
      - path=LOW needs risk=LOW, and risk_check.min_tier=LOW if a risk check has run
      - raising risk above LOW forces path=STANDARD
    Every change is appended to state.history.
    """
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    if not args.set:
        return result("STATE", ticket=number, state=state)

    stages = CONFIG["stages"]
    changes = {}
    for item in args.set:
        if "=" not in item:
            return result("BAD_ARG", 1, ticket=number, message=f"'{item}' is not key=value.")
        key, value = (s.strip() for s in item.split("=", 1))
        if key.startswith("staging_urls."):
            if not re.match(r"^https?://", value):
                return result("BAD_VALUE", 1, ticket=number, key=key, message="Staging URL must start with http(s)://")
        elif key not in STATE_FIELDS:
            return result("BAD_KEY", 1, ticket=number, key=key, allowed=sorted(STATE_FIELDS) + ["staging_urls.<App>"])
        elif not STATE_FIELDS[key](value):
            return result("BAD_VALUE", 1, ticket=number, key=key, value=value)
        changes[key] = value

    new_risk = changes.get("risk", state.get("risk"))
    old_risk = state.get("risk")
    stage = state.get("stage") or "1-pick"
    # The tier is "confirmed" once the ticket is past step 5 (a paused ticket counts by the stage it paused at).
    # The confirmed tier is stored as risk_floor and is what a later change is measured against, so moving
    # `stage` backwards (or pausing) can't be used to lower it.
    seen_stage = stage if stage in stages else state.get("paused_from")
    confirmed = seen_stage in stages and stages.index(seen_stage) > stages.index("5-plan")
    floor = state.get("risk_floor") or (old_risk if confirmed and old_risk in TIER_ORDER else None)
    if "risk" in changes and floor in TIER_ORDER and TIER_ORDER.index(new_risk) < TIER_ORDER.index(floor):
        return result("RISK_DOWN_REFUSED", 1, ticket=number, current=floor, requested=new_risk,
                      message="The tier can only go up after step 5.")
    new_stage = changes.get("stage", stage)
    # A-4: a branch created from master must pass the baseline tests of the projects it will modify (dt baseline,
    # run once PLAN.md is written) before the build starts, unless the developer accepted the failures.
    if ("stage" in changes and state.get("branch_origin") == "created" and new_stage in stages
            and stages.index(new_stage) > stages.index("5-plan")):
        base = state.get("baseline") or {}
        if base.get("status") != "PASS" and not base.get("accepted") and changes.get("baseline.accepted") != "true":
            return result("BASELINE_REQUIRED", 1, ticket=number, baseline=base.get("status"),
                          message="Run `dt baseline N` first: a new branch must start green for the projects it will "
                                  "modify (or the developer accepts the failures: `--set baseline.accepted=true`).")
    # The unit tests (local_gates) must have run AFTER the coding, on exactly the files being handed off: crossing
    # into hand-off needs a recorded passing gates run whose file fingerprint still matches (new or existing branch).
    handoff = stages.index("8-handoff")
    if ("stage" in changes and new_stage in stages and stages.index(new_stage) >= handoff
            and stage in stages and stages.index(stage) < handoff):
        import local_gates  # lazy: it reconfigures stdout on import
        last = state.get("gates") or {}
        if last.get("status") not in ("PASS", "NO_CHANGES"):
            return result("GATES_REQUIRED", 1, ticket=number, gates=last.get("status"),
                          message="Run `python {PY}\\local_gates.py --ticket N` after the coding: the unit tests of the "
                                  "modified projects must pass before hand-off (INCOMPLETE or FAIL does not count).")
        if local_gates.diff_fingerprint(local_gates.changed_files()) != last.get("fingerprint"):
            return result("GATES_STALE", 1, ticket=number, gates=last.get("status"),
                          message="The files changed since the last gates run: re-run `local_gates.py --ticket N`.")
        # Functional checks (7d): the developer chose run / skip / later (`dt func-mode`). Run needs a passing record on
        # exactly these files; skip needs the developer's recorded reason; unset or later is refused.
        import functional
        from develop_ticket.functional_cmds import auto_restore_env
        auto_restore_env(number, state)
        problem = functional.gate_problem(
            state, lambda: local_gates.diff_fingerprint(local_gates.changed_files()))
        if problem:
            return result(problem[0], 1, ticket=number, mode=state.get("functional_mode"), message=problem[1])
    new_floor = None
    if new_stage in stages and stages.index(new_stage) > stages.index("5-plan") and new_risk in TIER_ORDER:
        # leaving step 5 (or already past it): the floor is the tier now in force, or the higher one
        new_floor = floor if floor in TIER_ORDER and TIER_ORDER.index(floor) > TIER_ORDER.index(new_risk) else new_risk
    # An explicit path=LOW request is checked first and refused outright, never silently changed.
    if changes.get("path") == "LOW":
        min_tier = (state.get("risk_check") or {}).get("min_tier")
        if new_risk != "LOW" or (min_tier and min_tier != "LOW"):
            return result("LOW_PATH_REFUSED", 1, ticket=number, risk=new_risk, risk_check_min_tier=min_tier)
    # Raising the tier above LOW always takes the ticket off the LOW path.
    if "risk" in changes and new_risk in ("MEDIUM", "HIGH") and state.get("path") != "STANDARD":
        changes["path"] = "STANDARD"

    old_path = state.get("path")
    if new_floor:
        state["risk_floor"] = new_floor
    for key, value in changes.items():
        if key in ("db.files", "db.destructive_confirmed"):
            value = [p.strip().replace("\\", "/") for p in value.split(",")]
        elif key == "db.ticket":
            value = int(value)
        _set_nested(state, key, value)
    state.setdefault("history", []).append({"stage": state.get("stage"), "result": f"set {changes}", "at": now_iso()})
    write_state(path, state)
    if old_path == "LOW" and state.get("path") == "STANDARD":  # logged here so Claude can't forget it
        why = f"risk raised to {changes['risk']}" if "risk" in changes else "path set to STANDARD"
        record_metric(number, "fallback", reason=why)
    return result("UPDATED", ticket=number, changes=changes)


def low_path_problems(number: str) -> list:
    """Why this ticket may NOT use the LOW path (empty list = allowed).

    Deterministic guard for D-6..D-11: the script, not Claude's judgement, decides
    whether LOW-only actions (status moves past Code Review, staging deploy) may run.
    """
    state, path = common.load_state(number)
    if path is None:
        return ["no state.json for this ticket"]
    problems = []
    if state.get("risk") != "LOW":
        problems.append(f"confirmed risk is {state.get('risk')!r}, not LOW")
    if state.get("path") != "LOW":
        problems.append(f"path is {state.get('path')!r}, not LOW")
    risk_check = state.get("risk_check") or {}
    if risk_check.get("min_tier") != "LOW":
        problems.append(f"risk_check.min_tier is {risk_check.get('min_tier')!r} (run risk_check.py --ticket {number})")
    elif not risk_check.get("fingerprint"):
        problems.append(f"risk_check has no diff fingerprint (re-run risk_check.py --ticket {number})")
    else:
        import local_gates  # lazy: it reconfigures stdout on import
        files = local_gates.risk_files() if risk_check.get("fp_auto") else risk_check.get("fp_files") or []
        if local_gates.diff_fingerprint(files) != risk_check["fingerprint"]:
            problems.append("the diff changed after the last risk check, so LOW is stale "
                            f"(re-run risk_check.py --ticket {number})")
    return problems


def status_guard(api: AssemblaApi, number: str, ticket: dict, target: str):
    """Extra rules a status move must meet, beyond the allowed-moves table. -> (status, fields) or None.

    - the ticket must have a local work folder: this never moves a ticket the workflow isn't driving
      (e.g. the milestone's shared DB Story, which DB.md says never to move);
    - Dev In Process: assigned to me, and no other ticket of mine already in Dev In Process (one-ticket rule);
    - Code Review / Dev Done / DevOps Staging: the code is committed and on origin (WF branch, and DB branch if any).
    """
    state, path = common.load_state(number)
    if path is None:
        return "NO_STATE", {"message": "No local work folder for this ticket (run fetch first). Status moves are only "
                                       "made on tickets this workflow is driving."}
    if state.get("kind") == "review":
        # A reviewer's review folder (`stage` is `done` on purpose). Its status moves and its deploy are `dt review-verdict`,
        # `dt review-deploy` and `staging_deploy.py --reviewer`, which check the approval; this author tool does not.
        return "REVIEW_FOLDER", {"message": "This folder is a reviewer's review of a developer's ticket: its status moves are "
                                              "made with /review-and-deploy (dt review-verdict, dt review-deploy), not dt status."}
    if target in (common.approved_status(), STATUS["staging"]):
        problem = developer_problem(state)
        if problem:
            return "NOT_A_REVIEWER", {"message": problem}
    if target == STATUS["code_review"] and needs_rca_clca(ticket, state):
        missing = rca_clca_missing(ticket)
        if missing:
            return "RCA_CLCA_MISSING", {"missing": missing,
                                        "message": "A defect needs RCA (Root Cause Analysis) and CLCA (Closed-Loop "
                                                   "Corrective Action) before Code Review: dt rca N --rca-file … --clca-file …"}
        sweep = sweep_check(number)
        if sweep:
            return "SWEEP_MISSING", {"problems": sweep,
                                     "message": "CLCA includes looking for the same flaw elsewhere: record the sweep in "
                                                "BRIEF.md `## Same flaw elsewhere` (a decision per hit)."}
    if target == STATUS["in_process"]:
        me = api.get_current_user()
        if ticket.get("assigned_to_id") != me["id"]:
            return "NOT_ASSIGNED", {"message": "The ticket is not assigned to you."}
        current_ids = {m["id"] for m in api.get_current_milestones()}
        others = [str(t.get("number")) for t in my_in_process(api, me["id"], current_ids)[0]
                  if str(t.get("number")) != number]
        if others:
            return "OTHER_IN_PROCESS", {"others": others,
                                        "message": "Only one ticket may be in Dev In Process: move the other back first."}
    if target in (CONFIG.get("push_required_targets") or ["Code Review", "Dev Done", "DevOps Staging"]):
        repos = [common.push_state(number)]
        db = state.get("db") or {}
        if db.get("files"):
            repos.append(common.push_state(str(db["ticket"]), common.DB_REPO))
        if any(r["status"] != "PUSHED" for r in repos):
            return "NOT_PUSHED", {"repos": repos, "message": "Commit and push everything first (dt pushed)."}
    return None


def cmd_status(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    t = api.get_ticket(number)
    current, target = t.get("status"), args.to
    allowed = [tuple(m) for m in CONFIG["allowed_status_moves"]]
    low_moves = [tuple(m) for m in CONFIG.get("low_path_status_moves", [])]
    requested = [tuple(m) for m in CONFIG.get("requested_status_moves", [])]
    requested_move = getattr(args, "requested", False) and (current, target) in requested
    if requested_move:
        # MEDIUM/HIGH: only on the developer's explicit "deploy N"; the ticket must have finished hand-off.
        stage = (common.load_state(number)[0] or {}).get("stage")
        if stage not in ("8-handoff", "done"):
            return result("NOT_HANDED_OFF", ticket=number, current=current, target=target, stage=stage,
                          message="Requested move needs a ticket that finished the dev-ticket hand-off.")
    elif (current, target) in low_moves:
        problems = low_path_problems(number)
        if problems:
            return result("NOT_ALLOWED", ticket=number, current=current, target=target, reasons=problems,
                          message="This move is only allowed on the LOW path.")
    elif (current, target) not in allowed:
        return result("NOT_ALLOWED", ticket=number, current=current, target=target,
                      allowed=[f"{a} -> {b}" for a, b in allowed + low_moves if a == current],
                      message="Transition not in allowed_status_moves (dev_ticket.config.json).")
    refused = status_guard(api, number, t, target)
    if refused:
        return result(refused[0], ticket=number, current=current, target=target, **refused[1])
    if requested_move:  # the hand-off happened, but the evidence must still describe the code on origin
        stale = handoff_evidence_problem(number, common.load_state(number)[0] or {})
        if stale:
            return result(stale[0], ticket=number, current=current, target=target, message=stale[1])
    # A team with a developer-side "code committed" status (statuses.dev_done) passes through it on the hand-off.
    via = common.dev_done_status() if (common.dev_done_status() and current == STATUS["in_process"]
                                       and target == STATUS["code_review"]) else None
    confirm_payload = {"ticket": number, "current": current, "target": target, **({"via": via} if via else {})}
    if not args.yes:
        return result("PREVIEW", ticket=number, current=current, target=target, **({"via": via} if via else {}),
                      confirm=common.confirm_token("status", confirm_payload),
                      message="Re-run with --yes --confirm <confirm> to apply.")
    refused = common.confirm_refusal("status", number, args, confirm_payload)
    if refused is not None:
        return refused
    state = common.load_state(number)[0]
    companion = None
    if via:
        api.update_status(number, via)
        record_metric(number, "status", previous=current, current=via)
        companion = common.move_db_companion(api, state, current, via)
        current_for_companion = via if (companion or {}).get("moved") else current
    else:
        current_for_companion = current
    api.update_status(number, target)
    record_metric(number, "status", previous=via or current, current=target)
    companion = common.move_db_companion(api, state, current_for_companion, target) or companion
    return result("UPDATED", ticket=number, previous=current, current=target, **({"via": via} if via else {}),
                  **({"db_companion": companion} if companion else {}))
