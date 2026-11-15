"""The reviewer side: review-pick, review-fetch, review-compare, review-verdict, review-deploy, review-withdraw."""

import argparse
import contextlib
import io
import json
import re
import subprocess
from pathlib import Path

from assembla_api import AssemblaApi, AssemblaError, bare_number

from develop_ticket.comments import post_or_edit_comment
from develop_ticket.common import BASE, CONFIG, STATUS, TIER_ORDER, _write_state, branch_name, inside_ticket_folder, now_iso, record_metric, result, ticket_dir, ticket_row
from develop_ticket.ungated import ungated_files
from develop_ticket.protocol import latest_handoff, parse_handoff, render_verdict, verdict_payload, verdict_problems
from develop_ticket import common
from develop_ticket import ticket_fetch


# ---------------------------------------------------------------------- reviewer review (read-only: pick)

def _reviewer_problem(api: AssemblaApi, me: dict):
    """None when the caller may review (a configured reviewer who is not a developer), else (status, message)."""
    reviewers = {str(r).strip().lower() for r in CONFIG.get("reviewers") or [] if str(r).strip()}
    if not reviewers:
        return "REVIEWERS_NOT_CONFIGURED", "No reviewers are configured: the lead adds their logins to `reviewers`."
    if common._remembered_role() == "developer" or str(me.get("login", "")).lower() not in reviewers:
        return ("NOT_A_REVIEWER", "Reviewing and deploying a developer's ticket is for the configured reviewers "
                                  "(`reviewers`). If your role is wrong: dt role --set reviewer --yes.")
    return None


def _review_row(api: AssemblaApi, t: dict, found) -> dict:
    """One row for the reviewer: the ticket and what its HANDOFF says (no HANDOFF -> has_handoff false)."""
    row = ticket_row(api, t)
    row["story"] = t.get("hierarchy_type") == CONFIG["deploy_attributes"]["plan_level"]
    if not found:
        return dict(row, has_handoff=False)
    p = found["payload"]
    return dict(row, has_handoff=True, author=p.get("author") or found["writer_login"], risk=p.get("risk"),
                round=p.get("round"), head=str(p.get("head") or "")[:12], handoff_at=found["at"],
                db_ticket=(p.get("db") or {}).get("ticket"), authentic=found["authentic"],
                ambiguous=found["ambiguous"])


def cmd_review_pick(api: AssemblaApi, args) -> int:
    """Reviewer review, R1 (read-only): the tickets in Code Review assigned to me that carry a HANDOFF. `--ticket N` checks
    one. Nothing is written to Assembla or git."""
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], developer=me.get("login"), message=problem[1])
    current = api.get_current_milestones()
    if not current:
        return result("NO_CURRENT_MILESTONE", developer=me.get("login"),
                      message="No milestone has '_CURRENT' in its title.")
    current_ids = {m["id"] for m in current}
    current_titles = [m.get("title") for m in current]
    in_review = STATUS["code_review"]

    if args.ticket:
        try:
            t = api.get_ticket(args.ticket)
        except AssemblaError as exc:
            return result("NOT_FOUND", ticket=bare_number(args.ticket), message=str(exc))
        row = ticket_row(api, t)
        if t.get("assigned_to_id") != me["id"]:
            return result("NOT_ASSIGNED", reviewer=me.get("login"), **row,
                          message=f"Ticket is assigned to {row['assignee']}, not {me.get('login')}.")
        wrong_repo = common.repo_node_problem(t)
        if wrong_repo:
            return result(wrong_repo[0], reviewer=me.get("login"), **row, message=wrong_repo[1])
        if t.get("status") != in_review:
            return result("WRONG_STATUS", reviewer=me.get("login"), **row,
                          message=f"Ticket is '{t.get('status')}', expected '{in_review}'.")
        if t.get("milestone_id") not in current_ids:
            return result("WRONG_MILESTONE", reviewer=me.get("login"), current_milestones=current_titles, **row,
                          message="Ticket is not in a _CURRENT milestone.")
        found = latest_handoff(api, t["number"])
        if not found:
            return result("NO_HANDOFF", reviewer=me.get("login"), **row,
                          message="Assigned but not handed off: ask the author to run dt handoff.")
        return result("PICKED", reviewer=me.get("login"), **_review_row(api, t, found))

    tickets = api.get_my_active_tickets()
    if not tickets:  # an empty body from Assembla is not always trustworthy (as in `pick`)
        tickets = [t for mid in current_ids for t in api.get_milestone_tickets(mid)]
    mine = [t for t in tickets if t.get("assigned_to_id") == me["id"] and t.get("status") == in_review
            and t.get("milestone_id") in current_ids and not common.is_db_companion(api, t)
            and common.repo_node_problem(t) is None]  # with one Node per repo: only this repo's tickets
    rows = [_review_row(api, t, latest_handoff(api, t["number"])) for t in sorted(mine, key=lambda x: x["number"])]
    ready = [r for r in rows if r["has_handoff"]]
    waiting = [r for r in rows if not r["has_handoff"]]
    if not ready:
        others = [ticket_row(api, t) for mid in current_ids for t in api.get_milestone_tickets(mid)
                  if t.get("status") == in_review and t.get("assigned_to_id") != me["id"]][:25]
        return result("NONE", reviewer=me.get("login"), current_milestones=current_titles, not_handed_off=waiting,
                      assigned_to_others=others,
                      message=f"No '{in_review}' ticket with a hand-off is assigned to {me.get('login')}. "
                              "assigned_to_others is information only: nothing is taken over.")
    if len(ready) > 1:
        return result("MULTIPLE", reviewer=me.get("login"), candidates=ready, suggested=ready[0]["ticket"],
                      not_handed_off=waiting,
                      message="Several tickets wait for review; the lowest number is suggested (the staging job "
                              "deploys the lowest-numbered ticket first).")
    return result("PICKED", reviewer=me.get("login"), not_handed_off=waiting, **ready[0])


# ---------------------------------------------------------------------- reviewer review: fetch (safe checkout)

def dirty_files_in(repo: Path) -> list:
    """Every uncommitted tracked change and every untracked (not ignored) file in `repo`, as porcelain lines."""
    return [l for l in common.git("status", "--porcelain", "--untracked-files=all", repo=repo).stdout.splitlines() if l.strip()]


def fetch_origin_branch(branch: str, repo: Path = None) -> tuple:
    """Fetch origin/<branch>. -> ("OK", sha) | ("BRANCH_NOT_ON_ORIGIN", None) | ("FETCH_FAILED", stderr)."""
    proc = common.git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False, repo=repo)
    if proc.returncode != 0:
        if "couldn't find remote ref" in (proc.stderr or ""):
            return "BRANCH_NOT_ON_ORIGIN", None
        return "FETCH_FAILED", (proc.stderr or proc.stdout or "").strip()
    sha = common.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False, repo=repo).stdout.strip()
    return ("OK", sha) if sha else ("BRANCH_NOT_ON_ORIGIN", None)


def _is_doc_path(path: str) -> bool:
    """A documentation file (*.md, *.txt, or anything under a docs/ folder): not code."""
    p = path.replace("\\", "/").lower()
    return p.endswith((".md", ".txt")) or "docs" in p.split("/")[:-1]


def _own_code_diff(commit: str, repo: Path = None):
    """The branch's own non-documentation changes at `commit`: its diff against the merge base with origin/<BASE>."""
    base = common.git("merge-base", commit, f"origin/{BASE}", check=False, repo=repo)
    if base.returncode != 0 or not base.stdout.strip():
        return None
    diff = common.git("diff", base.stdout.strip(), commit, "--", ".", ":(exclude,glob)**/*.md", ":(exclude,glob)**/*.txt",
               ":(exclude,glob)**/docs/**", check=False, repo=repo)
    return diff.stdout if diff.returncode == 0 else None


def docs_only_delta(old: str, new: str, repo: Path = None) -> bool:
    """True when `new` is a descendant of `old` and the move is not the author's code: only documentation files changed
    between them, or the branch's own code changes are identical (a merge of master into the branch). A hand-off made at
    `old` still covers `new`, so no new hand-off is needed. Unknown commits or any other change of code -> False."""
    if not old or not new:
        return False
    if common.git("merge-base", "--is-ancestor", old, new, check=False, repo=repo).returncode != 0:
        return False
    diff = common.git("diff", "--name-only", old, new, check=False, repo=repo)
    if diff.returncode == 0 and all(_is_doc_path(f) for f in diff.stdout.splitlines() if f.strip()):
        return True
    common.git("fetch", "--quiet", "origin", BASE, check=False, repo=repo)
    before, after = _own_code_diff(old, repo), _own_code_diff(new, repo)
    return before is not None and before == after


def checkout_remote_branch(branch: str, repo: Path = None) -> tuple:
    """Put the working tree on origin's <branch> (already fetched), for a read-only review. -> (status, info).
    No local branch -> track the remote one. A local branch that is only behind -> fast-forward. A local branch with
    commits origin lacks is never reset or merged (`LOCAL_BRANCH_DIFFERS`): the reviewer's own work is not touched."""
    try:
        remote = common.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False, repo=repo).stdout.strip()
        if not remote:
            return "BRANCH_NOT_ON_ORIGIN", {}
        local = common.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False, repo=repo).stdout.strip()
        current = common.git("branch", "--show-current", repo=repo).stdout.strip()
        if not local:
            common.git("switch", "--track", f"origin/{branch}", repo=repo)
        else:
            if local != remote and common.git("merge-base", "--is-ancestor", local, remote, check=False,
                                       repo=repo).returncode != 0:
                return "LOCAL_BRANCH_DIFFERS", {"local": local[:12], "origin": remote[:12]}
            if current != branch:
                common.git("switch", branch, repo=repo)
            if local != remote:
                common.git("merge", "--ff-only", f"origin/{branch}", repo=repo)
        head = common.git("rev-parse", "HEAD", repo=repo).stdout.strip()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return "CHECKOUT_FAILED", {"message": str(exc)}
    if head != remote:
        return "CHECKOUT_FAILED", {"head": head[:12], "origin": remote[:12]}
    return "OK", {"head": head}


def _is_fetch_stub(folder: Path, state: dict) -> bool:
    """A folder holding only what a bare `dt fetch` writes (the ticket snapshot, its images and the fresh state.json): no
    plan, gates or hand-off, so it is not the author's work and a review may take it over."""
    if state.get("stage") != "2-check" or len(state.get("history") or []) > 1:
        return False
    return {p.name for p in folder.iterdir()} <= {"ticket.md", "ticket.json", "state.json", "attachments"}


def _save_handoff_files(api: AssemblaApi, number: str, found: dict) -> list:
    """Copy the author's hand-off comments into DEV_TICKETS/N/hand-off/ so the review reads files, not the API."""
    folder = ticket_dir(number) / "hand-off"
    folder.mkdir(parents=True, exist_ok=True)
    comments = api.get_comments(number)
    picks = {"summary.md": lambda text: text.lstrip().startswith("*Ticket #"),
             "author-review.md": lambda text: text.lstrip().startswith("# Code review")}
    saved = ["handoff.md"]
    (folder / "handoff.md").write_text(
        next((c["comment"] for c in comments if str(c.get("id")) == str(found["comment_id"])), ""), encoding="utf-8")
    for name, matches in picks.items():
        text = next((c["comment"] for c in reversed(comments) if matches(c.get("comment") or "")), None)
        if text:
            (folder / name).write_text(text, encoding="utf-8")
            saved.append(name)
    return saved


def cmd_review_fetch(api: AssemblaApi, args) -> int:
    """Reviewer review, R2: take the ticket over for review. Checks the caller, the ticket and the HANDOFF, needs a clean
    tree, refuses when the branch moved after the hand-off, then checks out the developer's branch (WF and DB) read-only and
    writes the reviewer's state (`kind=review`). Never commits, never writes to Assembla."""
    number = bare_number(args.ticket)
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], ticket=number, message=problem[1])
    t = api.get_ticket(number)
    if t.get("assigned_to_id") != me["id"]:
        return result("NOT_ASSIGNED", ticket=number, message=f"Ticket is assigned to "
                      f"{api.user_label(t.get('assigned_to_id'))}, not {me.get('login')}.")
    wrong_repo = common.repo_node_problem(t)
    if wrong_repo:  # the review checks out FB_N in this repo: it must be the ticket's own repo
        return result(wrong_repo[0], ticket=number, message=wrong_repo[1])
    if t.get("status") != STATUS["code_review"]:
        return result("WRONG_STATUS", ticket=number, ticket_status=t.get("status"),
                      message=f"Ticket is '{t.get('status')}', expected '{STATUS['code_review']}'.")
    state, state_path = common.load_state(number)
    has_author_state = result_args = None
    if state_path is not None and state.get("kind") != "review" and not _is_fetch_stub(state_path.parent, state):
        result_args = dict(ticket=number, message=f"{ticket_dir(number)} holds work on this ticket that is not a review: "
                                                  "not touched.")
        if not common.self_review_allowed():
            return result("HAS_AUTHOR_STATE", **result_args)
        has_author_state = state  # self-review: only my own handed-off folder may be moved aside (checked below)

    found = latest_handoff(api, number)
    if not found:
        return result("NO_HANDOFF", ticket=number, message="Assigned but not handed off: ask the author to run dt handoff.")
    if found["ambiguous"]:
        return result("HANDOFF_AMBIGUOUS", ticket=number, message="HANDOFF comments by more than one person since the last "
                      "REJECT or QUALITY-BOUNCE: ask which is the author's and have the other removed.")
    if not found["authentic"]:
        return result("HANDOFF_AUTHOR_MISMATCH", ticket=number, writer=found["writer_login"],
                      claimed=found["payload"].get("author"),
                      message="The HANDOFF names an author other than the person who wrote the comment: not trusted.")
    own_handoff = found["writer_login"].lower() == str(me.get("login", "")).lower()
    if own_handoff and not common.self_review_allowed():
        return result("SELF_REVIEW", ticket=number, message="You wrote this hand-off: someone else reviews it.")
    if has_author_state is not None:
        handed_off_here = str((has_author_state.get("handoff") or {}).get("comment_id")) == str(found["comment_id"])
        if not (own_handoff and handed_off_here):
            return result("HAS_AUTHOR_STATE", **result_args)
        if common.parked_author_dir(number).exists():
            return result("AUTHOR_FOLDER_PARKED", ticket=number, parked=str(common.parked_author_dir(number)),
                          message="An earlier author folder is already parked for this ticket: sort it out by hand first.")

    p = found["payload"]
    db = p.get("db") or {}
    repos = [("wf", None, branch_name(number), p.get("head"))]
    if db.get("files"):
        if not common.DB_REPO.is_dir():
            return result("DB_REPO_MISSING", ticket=number, message="The DB repo (C2M_CI_DB) is not where the config says.")
        repos.append(("db", common.DB_REPO, branch_name(db["ticket"]), db.get("head")))

    dirty = {"wf": common.start_dirty_files()}
    if len(repos) > 1:
        dirty["db"] = dirty_files_in(common.DB_REPO)
    for name, files in dirty.items():
        if files:
            return result("DIRTY", ticket=number, repo=name, total=len(files), files=files[:50],
                          message="The working tree must be clean before a review: commit, stash or pause your own work "
                                  "first (never done for you), then re-run.")
    tips, docs_only = {}, {}
    for name, repo, branch, expected in repos:
        status, info = fetch_origin_branch(branch, repo)
        if status == "BRANCH_NOT_ON_ORIGIN" and name == "db":
            return result("DB_BRANCH_MISSING", ticket=number, repo=name, branch=branch,
                          message="The DB branch is not on origin: ask the author to push it.")
        if status != "OK":
            return result(status, ticket=number, repo=name, branch=branch, **({"message": info} if info else {}))
        if info != expected and docs_only_delta(expected, info, repo):
            docs_only[name] = {"from": str(expected)[:12], "to": info[:12]}  # documentation only after the hand-off: still valid
        elif info != expected:
            return result("STALE_HANDOFF", ticket=number, repo=name, handoff_head=str(expected)[:12], origin_head=info[:12],
                          message="The branch moved after the hand-off: ask the author to run dt handoff again.")
        tips[name] = info
    for name, repo, branch, _expected in repos:
        status, info = checkout_remote_branch(branch, repo)
        if status != "OK":
            return result(status, ticket=number, repo=name, branch=branch, **info)

    parked = None
    if has_author_state is not None:  # self-review: the author's folder waits as DEV_TICKETS/{N}.author (dt rework restores it)
        try:
            parked = common.park_author_folder(number)
        except OSError as exc:
            return result("PARK_FAILED", 1, ticket=number, message=f"Could not move the author folder aside: {exc}")
    with contextlib.redirect_stdout(io.StringIO()):  # the ticket, its comments and the reporter's images, as an author fetch
        outcome = ticket_fetch.cmd_fetch(api, argparse.Namespace(ticket=number, type=None, from_review=True))
        if not (ticket_dir(number) / "state.json").is_file():
            outcome = ticket_fetch.cmd_fetch(api, argparse.Namespace(ticket=number, type="CR", from_review=True))
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("FETCH_FAILED", 1, ticket=number, message="The ticket could not be saved.")
    saved = _save_handoff_files(api, number, found)

    if not docs_only:  # a documentation-only or master-merge move keeps it: review-compare rejects it by fingerprint if the code differs
        for stale in ("gates", "risk_check", "functional", "functional_mode", "functional_skip", "profile", "services"):
            state.pop(stale, None)  # evidence of an earlier head means nothing for this one
    state.update(role="reviewer", kind="review", stage="done", branch=branch_name(number), branch_origin="existing",
                 review={"stage": "R2-fetch", "author": found["writer_login"], "head": tips["wf"],
                         "handoff": {"comment_id": found["comment_id"], "round": p.get("round"), "risk": p.get("risk"),
                                     "at": found["at"]},
                         "db_head": tips.get("db"), "fetched_at": now_iso()})
    if docs_only:
        state["review"]["docs_only_delta"] = docs_only
    if db.get("files"):
        state["db"] = {"ticket": db["ticket"], "files": db["files"]}
    else:
        state.pop("db", None)
    _write_state(state_path, state, "review fetched")
    record_metric(number, "review_fetch", author=found["writer_login"], round=p.get("round"), risk=p.get("risk"))
    if own_handoff:
        state["review"]["self_review"] = True
        _write_state(state_path, state, "self-review")
    return result("FETCHED", ticket=number, author=found["writer_login"], risk=p.get("risk"), round=p.get("round"),
                  self_review=own_handoff or None, author_folder_parked=str(parked) if parked else None,
                  head=tips["wf"][:12], db_ticket=db.get("ticket"), db_head=(tips.get("db") or "")[:12] or None,
                  folder=str(ticket_dir(number)), hand_off_files=saved)


# ---------------------------------------------------------------------- reviewer review: compare (R3)

def compare_handoff(p: dict, state: dict, facts: dict, early: bool = False) -> tuple:
    """Compare the author's HANDOFF with what the reviewer reproduced. -> (blocking, warnings, tier).

    Blocking: the review is not of the handed-off code (`STALE_HANDOFF`, `HEAD_DIFFERS`, `TREE_MODIFIED`), the branch is
    behind master (`BEHIND_MASTER`: the developer syncs first), or the reviewer's own gates / risk check / functional checks are
    missing, failing or stale. Warnings: a higher tier than the author's (`RISK_HIGHER`, becomes the tier of record) and a
    different gated file list (`FILES_DIFFER`). `early` leaves the functional checks out (they usually come last)."""
    import functional
    blocking, warnings = [], []
    heads = [("wf", p.get("head"), facts["origin_head"], facts["local_head"])]
    if p.get("db"):
        heads.append(("db", p["db"].get("head"), facts["db_origin_head"], facts["db_local_head"]))
    for repo, handed_off, origin, local in heads:
        if origin != handed_off and facts.get(f"{repo}_docs_only"):  # only documentation after the hand-off: still covered
            warnings.append({"code": "DOCS_ONLY_DELTA", "repo": repo, "handoff_head": str(handed_off)[:12],
                             "origin_head": str(origin)[:12]})
            if local != origin:
                blocking.append({"code": "HEAD_DIFFERS", "repo": repo, "handoff_head": str(origin)[:12],
                                 "local_head": str(local)[:12]})
        elif origin != handed_off:
            blocking.append({"code": "STALE_HANDOFF", "repo": repo, "handoff_head": str(handed_off)[:12],
                             "origin_head": str(origin)[:12]})
        elif local != handed_off:
            blocking.append({"code": "HEAD_DIFFERS", "repo": repo, "handoff_head": str(handed_off)[:12],
                             "local_head": str(local)[:12]})
    if facts["dirty"]:
        blocking.append({"code": "TREE_MODIFIED", "files": facts["dirty"][:50]})
    if facts["behind_master"]:  # a clean lag merges cleanly in the staging job: only conflicts need the author
        (blocking if facts["conflicts"] else warnings).append(
            {"code": "BEHIND_MASTER", "behind": facts["behind_master"], "conflicts": facts["conflicts"]})

    gates = state.get("gates") or {}
    if not gates:
        blocking.append({"code": "GATES_NOT_RUN"})
    elif gates.get("status") not in ("PASS", "NO_CHANGES"):
        blocking.append({"code": "GATES_NOT_PASS", "gates": gates.get("status")})
    elif gates.get("fingerprint") != facts["fingerprint_now"]():
        blocking.append({"code": "GATES_STALE"})

    risk = state.get("risk_check") or {}
    tier = p.get("risk") if p.get("risk") in TIER_ORDER else "LOW"
    if not risk:
        blocking.append({"code": "RISK_NOT_RUN"})
    else:
        if risk.get("fingerprint") != facts["risk_fingerprint_now"]():
            blocking.append({"code": "RISK_STALE"})
        if risk.get("min_tier") in TIER_ORDER and TIER_ORDER.index(risk["min_tier"]) > TIER_ORDER.index(tier):
            warnings.append({"code": "RISK_HIGHER", "handoff": p.get("risk"), "review": risk["min_tier"],
                             "reasons": risk.get("reasons") or []})
            tier = risk["min_tier"]

    theirs = (p.get("gates") or {}).get("files") or []
    ours = gates.get("files") or []
    if gates and (p.get("gates") or {}).get("files_count") == len(theirs):
        if set(theirs) != set(ours):
            warnings.append({"code": "FILES_DIFFER", "only_in_review": sorted(set(ours) - set(theirs)),
                             "only_in_handoff": sorted(set(theirs) - set(ours))})
    elif gates and (p.get("gates") or {}).get("files_count") != len(ours):  # the comment's list is capped: counts only
        warnings.append({"code": "FILES_DIFFER", "handoff_count": (p.get("gates") or {}).get("files_count"),
                         "review_count": len(ours)})

    if not early:
        problem = functional.gate_problem(state, facts["fingerprint_now"])
        if problem:
            blocking.append({"code": problem[0], "message": problem[1]})
    return blocking, warnings, tier


def cmd_review_compare(_api, args) -> int:
    """Reviewer review, R3: after the reviewer ran `local_gates.py --ticket N`, `risk_check.py --ticket N` and the functional
    checks, compare the HANDOFF with that. COMPARE_OK advances the review to R3-check and records the tier of record
    (the higher of the author's and the reviewer's). `--early` leaves the functional checks out and never advances."""
    import local_gates  # lazy: it reconfigures stdout on import
    number = bare_number(args.ticket)
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="Run review-fetch first.")
    if state.get("kind") != "review":
        return result("NOT_A_REVIEW_FOLDER", ticket=number, message="This folder is not a review of someone else's ticket (kind is not review).")
    if not args.early:  # the functional checks are over by now: put any still-patched run file back
        from develop_ticket.functional_cmds import auto_restore_env
        auto_restore_env(number, state)
    handoff_file = ticket_dir(number) / "hand-off" / "handoff.md"
    p = parse_handoff(handoff_file.read_text(encoding="utf-8")) if handoff_file.is_file() else None
    if p is None:
        return result("NO_HANDOFF", ticket=number, message="No saved hand-off: run dt review-fetch again.")

    branch = branch_name(number)
    try:
        current = common.git("branch", "--show-current").stdout.strip()
        if current != branch:
            return result("WRONG_BRANCH", ticket=number, current_branch=current)
        status, origin = fetch_origin_branch(branch)
        if status != "OK":
            return result(status, ticket=number, repo="wf", **({"message": origin} if origin else {}))
        facts = {"origin_head": origin, "local_head": common.git("rev-parse", "HEAD").stdout.strip(),
                 "db_origin_head": None, "db_local_head": None, "dirty": common.uncommitted_tracked()}
        if p.get("db"):
            status, db_origin = fetch_origin_branch(branch_name(p["db"]["ticket"]), common.DB_REPO)
            if status != "OK":
                return result(status, ticket=number, repo="db", **({"message": db_origin} if db_origin else {}))
            facts.update(db_origin_head=db_origin, db_local_head=common.git("rev-parse", "HEAD", repo=common.DB_REPO).stdout.strip())
            facts["dirty"] = facts["dirty"] + common.uncommitted_tracked(common.DB_REPO)
        facts["wf_docs_only"] = docs_only_delta(p.get("head"), origin)
        if p.get("db"):
            facts["db_docs_only"] = docs_only_delta(p["db"].get("head"), db_origin, common.DB_REPO)
        common.git("fetch", "--quiet", "origin", BASE)
        _ahead, behind, conflicts = common.master_sync_info()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, ticket=number, message=str(exc))
    rc = state.get("risk_check") or {}
    risk_files = local_gates.risk_files() if rc.get("fp_auto") else rc.get("fp_files") or []
    facts.update(behind_master=behind, conflicts=conflicts,
                 fingerprint_now=lambda: local_gates.diff_fingerprint(local_gates.changed_files()),
                 risk_fingerprint_now=lambda: local_gates.diff_fingerprint(risk_files))

    blocking, warnings, tier = compare_handoff(p, state, facts, early=args.early)
    status = "COMPARE_MISMATCH" if blocking else ("COMPARE_PARTIAL" if args.early else "COMPARE_OK")
    review = state.setdefault("review", {})
    review["compare"] = {"status": status, "blocking": [b["code"] for b in blocking],
                         "warnings": [w["code"] for w in warnings], "early": bool(args.early), "at": now_iso()}
    if status == "COMPARE_OK":
        review.update(stage="R3-check", tier=tier)
    _write_state(state_path, state, f"review compare {status}")
    record_metric(number, "review_compare", status=status, blocking=len(blocking), warnings=len(warnings), tier=tier)
    return result(status, ticket=number, tier=tier, head=str(origin)[:12], blocking=blocking, warnings=warnings)


# ---------------------------------------------------------------------- reviewer review: the verdict (reject)

def ai_review_summary(path: Path) -> dict:
    """The AI review's verdict and its OPEN findings by severity (the first findings table; a dismissed finding has been
    moved out of it). `open_high_medium` > 0 means an approval needs an override reason."""
    empty = {"verdict": "not run", "high": 0, "medium": 0, "low": 0, "open_high_medium": 0}
    if not path.is_file():
        return empty
    import review_check
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^##\s+Verdict:\s*(PASS|CHANGES REQUIRED)", text, re.M | re.I)
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in review_check.parse_findings(text):
        severity = finding["severity"].strip().lower()
        if severity in counts:
            counts[severity] += 1
    return dict(empty, verdict=match.group(1).upper() if match else "n/a", **counts,
                open_high_medium=counts["high"] + counts["medium"])


def _wiki_safe(text) -> str:
    return str(text).replace("<", "").replace(">", "").replace("`", "").replace("\n", " ")


def _review_checks(state: dict, ai: dict) -> dict:
    """What the reviewer reproduced, as short strings for the verdict comment."""
    func, skip = state.get("functional") or {}, state.get("functional_skip") or {}
    if state.get("functional_mode") == "skip":
        functional = f"skipped ({skip.get('reason')})"
    elif func:
        functional = f"{func.get('passed')} passed, {func.get('failed')} failed"
    else:
        functional = "not run"
    return {"gates": _wiki_safe((state.get("gates") or {}).get("status") or "not run"),
            "risk": _wiki_safe((state.get("risk_check") or {}).get("min_tier") or "not run"),
            "functional": _wiki_safe(functional),
            "compare": _wiki_safe(((state.get("review") or {}).get("compare") or {}).get("status") or "not run"),
            "ai_review": {k: ai[k] for k in ("verdict", "high", "medium", "low")}}


def cmd_review_verdict(api: AssemblaApi, args) -> int:
    """Reviewer review, R5: post the REVIEW_VERDICT comment and act on it. `--reject` then assigns the ticket back to its
    author and moves it to Dev Queue in ONE update. `--approve` (needs COMPARE_OK and the AI review in hand) moves it to
    Dev Done, still assigned to the reviewer, and records the reviewed head for the deploy. Everything outward needs `--yes`;
    the comment is posted first (evidence), and a failed move is reported, never retried."""
    number = bare_number(args.ticket)
    want = "REJECT" if args.reject else ("APPROVE" if getattr(args, "approve", False) else None)
    if want is None:
        return result("BAD_ARG", 1, ticket=number, message="Use --reject (or --approve) with --file verdict.json.")
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], ticket=number, message=problem[1])
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="Run review-fetch first.")
    if state.get("kind") != "review":
        return result("NOT_A_REVIEW_FOLDER", ticket=number, message="This folder is not a reviewer review.")
    t = api.get_ticket(number)
    if t.get("assigned_to_id") != me["id"]:
        return result("NOT_ASSIGNED", ticket=number, message="The ticket is not assigned to you.")
    if t.get("status") != STATUS["code_review"]:
        return result("WRONG_STATUS", ticket=number, ticket_status=t.get("status"),
                      message=f"Ticket is '{t.get('status')}', expected '{STATUS['code_review']}'.")
    review = state.get("review") or {}
    author = review.get("author")
    if not author or not review.get("head"):
        return result("NOT_A_REVIEW_FOLDER", ticket=number, message="No fetched review in this folder: run review-fetch.")
    if author.lower() == str(me.get("login", "")).lower() and not common.self_review_allowed():
        return result("SELF_REVIEW", ticket=number, message="You wrote this hand-off: someone else reviews it.")

    file_arg = Path(args.file)
    if not file_arg.is_absolute() and not file_arg.is_file():
        file_arg = ticket_dir(number) / file_arg
    if not inside_ticket_folder(number, str(file_arg)):
        return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=args.file,
                      message=f"Only a file inside {ticket_dir(number)} can be used.")
    if not file_arg.is_file():
        return result("NOT_FOUND", 1, ticket=number, file=str(file_arg), message="Write verdict.json first.")
    try:
        v = json.loads(file_arg.read_text(encoding="utf-8"))
    except ValueError as exc:
        return result("VERDICT_INVALID", 1, ticket=number, errors=[f"verdict.json is not valid JSON: {exc}"])
    if isinstance(v, dict) and v.get("verdict") != want:
        return result("VERDICT_MISMATCH", 1, ticket=number, file_verdict=v.get("verdict"), requested=want,
                      message="The flag and the verdict in verdict.json disagree.")
    ai = ai_review_summary(ticket_dir(number) / "reviews" / "code-review.md")
    errors = verdict_problems(v, ai_open=ai["open_high_medium"] > 0)
    if errors:
        return result("VERDICT_INVALID", 1, ticket=number, errors=errors,
                      message="Fix verdict.json (the errors say how), then run it again.")

    # The verdict must be about the code the reviewer looked at: the developer may have pushed since the fetch.
    heads = [("wf", branch_name(number), None, review.get("head"))]
    if (state.get("db") or {}).get("files"):
        heads.append(("db", branch_name(state["db"]["ticket"]), common.DB_REPO, review.get("db_head")))
    for repo_name, branch, repo, reviewed in heads:
        status, tip = fetch_origin_branch(branch, repo)
        if status != "OK":
            return result(status, ticket=number, repo=repo_name, **({"message": tip} if tip else {}))
        if tip != reviewed:
            return result("STALE_REVIEW", ticket=number, repo=repo_name, reviewed_head=str(reviewed)[:12],
                          origin_head=tip[:12],
                          message="The author pushed after you fetched: run review-fetch again, then review that head.")
    return _review_verdict_act(api, args, state, state_path, v, want, ai, me, t)


def _review_verdict_act(api, args, state, state_path, v, want, ai, me, ticket) -> int:
    number = bare_number(args.ticket)
    review = state["review"]
    approve = want == "APPROVE"
    target = common.approved_status() if approve else common.rejected_status()
    if [STATUS["code_review"], target] not in [list(m) for m in CONFIG.get("reviewer_status_moves") or []]:
        return result("NOT_ALLOWED", ticket=number, target=target, message="Not in reviewer_status_moves.")
    if approve:
        # An approval is only of a review that was done: every reproduced check matched the hand-off (COMPARE_OK) and the
        # AI review is in the reviewer's hands (R4). A reject needs neither: a failing check is itself a reason.
        if (review.get("compare") or {}).get("status") != "COMPARE_OK":
            return result("COMPARE_REQUIRED", ticket=number, compare=(review.get("compare") or {}).get("status"),
                          message="Run dt review-compare N until it says COMPARE_OK before approving.")
        if review.get("stage") != "R4-review":
            return result("AI_REVIEW_REQUIRED", ticket=number, review_stage=review.get("stage"),
                          message="Run the AI review (R4) and dt review-check before approving.")
        author = None
    else:
        users = {str(u.get("login", "")).lower(): u for u in api.get_space_users()}
        author = users.get(review["author"].lower())
        if not author or not author.get("id"):
            return result("AUTHOR_NOT_FOUND", ticket=number, author=review["author"],
                          message="The author is not a user of the Assembla space: tell the lead.")
    handoff = review.get("handoff") or {}
    payload = verdict_payload(v, ticket=number, reviewer=me.get("login"), author=review["author"], head=review["head"],
                              round=handoff.get("round") or 1,
                              tier=review.get("tier") or handoff.get("risk") or "UNKNOWN", checks=_review_checks(state, ai))
    body = render_verdict(payload)
    reviews = ticket_dir(number) / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"verdict-round{payload['round']}.md").write_text(body, encoding="utf-8")

    previous = review.get("verdict_comment_id")
    db_ticket = (state.get("db") or {}).get("ticket")
    actions = [f"edit the verdict comment #{previous}" if previous else "post the verdict comment",
               f"move #{number} to {target} (it stays assigned to you; the approval is bound to head {review['head'][:12]})"
               if approve else f"assign #{number} to {review['author']} and move it to {target}"]
    if db_ticket:
        actions.append(f"move DB ticket #{db_ticket} to {target} if it is still in {STATUS['code_review']}")
    if not args.yes:
        print("---- verdict comment preview (not posted) ----\n" + body + "-----------------------------------------------")
        return result("PREVIEW", ticket=number, verdict=want, author=review["author"], round=payload["round"],
                      actions=actions, message="Re-run with --yes to do all of this, in this order.")
    status, fields = post_or_edit_comment(api, number, body.strip(), previous)
    if status not in ("POSTED", "EDITED") or fields.get("comment_id") is None:
        return result(status, 1 if status in ("COMMENT_NOT_FOUND", "NOT_YOUR_COMMENT") else 0, ticket=number, **fields)
    review["verdict_comment_id"] = fields["comment_id"]  # kept even if the move fails: the next run edits it
    _write_state(state_path, state, f"verdict comment {status.lower()}")
    try:
        if approve:
            api.update_status(number, target)  # re-reads: Assembla ignores a status it does not accept
        else:
            api.assign_and_move(number, author["id"], target)
    except AssemblaError as exc:
        return result("MOVE_FAILED", 1, ticket=number, comment_id=fields["comment_id"], message=(
            f"{exc} The verdict comment is posted. Check the ticket on Assembla before doing anything; this is not retried."))
    companion = common.move_db_companion(api, state, STATUS["code_review"], target)
    review.update(stage="R6-deploy" if approve else "done",
                  verdict={"verdict": want, "round": payload["round"], "head": review["head"], "at": now_iso(),
                           "comment_id": fields["comment_id"]})
    if approve:  # phase 4 deploys only this head: any later push voids the approval
        review["approved_head"] = review["head"]
        review["approved_db_head"] = review.get("db_head")
        if target == STATUS["staging"]:  # the team's approval IS the deploy request (its Jenkins picks the ticket up)
            review["deploy_requested"] = {"at": now_iso(), "head": review["head"], "db_head": review.get("db_head"),
                                          "by": "approval"}
    else:
        review.pop("approved_head", None)
        review.pop("approved_db_head", None)
    _write_state(state_path, state, f"review {'approved' if approve else 'rejected'}")
    record_metric(number, "review_verdict", verdict=want, round=payload["round"], findings=len(payload["findings"]))
    return result("APPROVED" if approve else "REJECTED", ticket=number, comment_id=fields["comment_id"],
                  round=payload["round"], status_now=target, **({"approved_head": review["head"][:12]} if approve else
                                                                {"author": review["author"]}),
                  **({"db_companion": companion} if companion else {}),
                  **({"deploy": (CONFIG.get("staging_deploy") or {}).get("after_approval_note")
                      or "The ticket is in the staging status: the team's staging deploy picks it up."}
                     if approve and target == STATUS["staging"] else {}))


# ---------------------------------------------------------------------- reviewer review: the deploy request

UNGATED_NOTE = ("The branch brings files that no gates run covered: show the reviewer this list. The deploy refuses them "
                "unless it is run with --accept-ungated TOKEN (the token it prints), after the reviewer's yes.")


def cmd_review_deploy(api: AssemblaApi, args) -> int:
    """Reviewer review, R6: the reviewer's "deploy". Moves the approved ticket from Dev Done to DevOps Staging, but only while
    the approval still holds: origin's branches (WF and DB) must still be at the heads that were approved, so what is
    deployed is exactly what was reviewed. It does NOT trigger the job: `staging_deploy.py queue N` and
    `staging_deploy.py deploy N --reviewer` follow. Preview unless `--yes`; a failed move is reported, never retried."""
    number = bare_number(args.ticket)
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], ticket=number, message=problem[1])
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="No review of this ticket on this machine: run review-fetch.")
    if state.get("kind") != "review":
        return result("NOT_A_REVIEW_FOLDER", ticket=number, message="This folder is not a reviewer review.")
    review = state.get("review") or {}
    if (review.get("verdict") or {}).get("verdict") != "APPROVE" or not review.get("approved_head"):
        return result("NOT_APPROVED", ticket=number,
                      message="This review has no approval: approve it first (dt review-verdict --approve).")
    if str(review.get("author", "")).lower() == str(me.get("login", "")).lower() and not common.self_review_allowed():
        return result("SELF_REVIEW", ticket=number, message="You wrote this hand-off: someone else deploys it.")
    t = api.get_ticket(number)
    if t.get("assigned_to_id") != me["id"]:
        return result("NOT_ASSIGNED", ticket=number, message="The ticket is not assigned to you.")
    if t.get("status") == STATUS["staging"] and review.get("deploy_requested"):
        return result("ALREADY_REQUESTED", ticket=number, at=review["deploy_requested"].get("at"),
                      message="The ticket is already in DevOps Staging: next, staging_deploy.py queue N.")
    if t.get("status") != common.approved_status():
        return result("WRONG_STATUS", ticket=number, ticket_status=t.get("status"),
                      message=f"Ticket is '{t.get('status')}', expected '{common.approved_status()}' (approved).")
    target = STATUS["staging"]
    if [common.approved_status(), target] not in [list(m) for m in CONFIG.get("reviewer_status_moves") or []]:
        return result("NOT_ALLOWED", ticket=number, target=target, message="Not in reviewer_status_moves.")

    heads = [("wf", branch_name(number), None, review["approved_head"])]
    if (state.get("db") or {}).get("files"):
        heads.append(("db", branch_name(state["db"]["ticket"]), common.DB_REPO, review.get("approved_db_head")))
    for repo_name, branch, repo, approved in heads:
        status, tip = fetch_origin_branch(branch, repo)
        if status != "OK":
            return result(status, ticket=number, repo=repo_name, **({"message": tip} if tip else {}))
        if tip != approved:
            return result("APPROVAL_STALE", ticket=number, repo=repo_name, approved_head=str(approved)[:12],
                          origin_head=tip[:12],
                          message="The author pushed after the approval: what would deploy is not what was reviewed. "
                                  "Withdraw the approval (dt review-withdraw) and review again.")

    db_ticket = (state.get("db") or {}).get("ticket")
    has_db_files = bool(db_ticket and (state.get("db") or {}).get("files"))
    ungated = {name: found for name, found in (("wf", ungated_files(number, state)),
                                               ("db", ungated_files(str(db_ticket), state, db=True) if has_db_files else {}))
               if found}
    steps = [f"python {{PY}}\\staging_deploy.py queue {number}",
             f"python {{PY}}\\staging_deploy.py deploy {number} --reviewer --yes   (background, 45+ minutes)"]
    if not args.yes:
        return result("PREVIEW", ticket=number, approved_head=review["approved_head"][:12], target=target,
                      actions=[f"move #{number} to {target}"] + ([f"move DB ticket #{db_ticket} to {target} if it is still "
                                                                  f"in {common.approved_status()}"] if db_ticket else []),
                      next_steps=steps, **({"ungated_files": ungated, "ungated_note": UNGATED_NOTE} if ungated else {}),
                      message="Re-run with --yes to move it. The deploy itself is a separate step.")
    try:
        api.update_status(number, target)  # re-reads: Assembla ignores a status it does not accept
    except AssemblaError as exc:
        return result("MOVE_FAILED", 1, ticket=number, message=(
            f"{exc} Check the ticket on Assembla before doing anything; this is not retried."))
    companion = common.move_db_companion(api, state, common.approved_status(), target)
    review["deploy_requested"] = {"at": now_iso(), "head": review["approved_head"], "db_head": review.get("approved_db_head")}
    _write_state(state_path, state, "deploy requested")
    record_metric(number, "review_deploy_request", head=review["approved_head"][:12])
    return result("DEPLOY_REQUESTED", ticket=number, approved_head=review["approved_head"][:12], status_now=target,
                  next_steps=steps, **({"db_companion": companion} if companion else {}),
                  **({"ungated_files": ungated, "ungated_note": UNGATED_NOTE} if ungated else {}))


def cmd_review_withdraw(api: AssemblaApi, args) -> int:
    """Reviewer review: withdraw an approval before anything was deployed (the author pushed again, or the reviewer changed
    their mind). Moves the ticket back to Code Review, still assigned to the reviewer, and clears the approval and the deploy
    request, so `dt review-deploy` refuses and the review can be redone (`dt review-fetch` when the author pushed, or a new
    verdict). Refused once a deploy was triggered: code that went out cannot be un-approved. Preview unless `--yes`."""
    number = bare_number(args.ticket)
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], ticket=number, message=problem[1])
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="No review of this ticket on this machine.")
    if state.get("kind") != "review":
        return result("NOT_A_REVIEW_FOLDER", ticket=number, message="This folder is not a reviewer review.")
    review = state.get("review") or {}
    if (review.get("verdict") or {}).get("verdict") != "APPROVE" or not review.get("approved_head"):
        return result("NOT_APPROVED", ticket=number, message="There is no approval to withdraw.")
    if str(review.get("author", "")).lower() == str(me.get("login", "")).lower() and not common.self_review_allowed():
        return result("SELF_REVIEW", ticket=number, message="You wrote this hand-off: someone else reviews it.")
    if state.get("deploy_attempt") or state.get("db_deploy_attempt"):
        return result("ALREADY_DEPLOYED", ticket=number,
                      message="A deploy was already triggered for this ticket: an approval cannot be withdrawn after the "
                              "code went out. Check the build and the ticket status.")
    t = api.get_ticket(number)
    if t.get("assigned_to_id") != me["id"]:
        return result("NOT_ASSIGNED", ticket=number, message="The ticket is not assigned to you.")
    previous, target = t.get("status"), STATUS["code_review"]
    if previous not in (common.approved_status(), STATUS["staging"]):
        return result("WRONG_STATUS", ticket=number, ticket_status=previous,
                      message=f"Only an approved ticket in '{common.approved_status()}' or '{STATUS['staging']}' can be "
                              "withdrawn.")
    if [previous, target] not in [list(m) for m in CONFIG.get("reviewer_status_moves") or []]:
        return result("NOT_ALLOWED", ticket=number, current=previous, target=target, message="Not in reviewer_status_moves.")
    db_ticket = (state.get("db") or {}).get("ticket")
    actions = [f"move #{number} from {previous} to {target} (it stays assigned to you)",
               "clear the approval and the deploy request (dt review-deploy refuses until you approve again)"]
    if db_ticket:
        actions.append(f"move DB ticket #{db_ticket} to {target} if it is still in {previous}")
    if not args.yes:
        return result("PREVIEW", ticket=number, approved_head=review["approved_head"][:12], actions=actions,
                      message="Re-run with --yes to withdraw. The APPROVED comment stays on the ticket until your next "
                              "verdict edits it in place.")
    try:
        api.update_status(number, target)  # re-reads: Assembla ignores a status it does not accept
    except AssemblaError as exc:
        return result("MOVE_FAILED", 1, ticket=number, message=(
            f"{exc} Check the ticket on Assembla before doing anything; this is not retried."))
    companion = common.move_db_companion(api, state, previous, target)
    review["withdrawn"] = {"at": now_iso(), "head": review["approved_head"], "was": previous, "verdict": review.pop("verdict")}
    for key in ("approved_head", "approved_db_head", "deploy_requested"):
        review.pop(key, None)
    review["stage"] = "R4-review"  # the reviewer decides again: a new verdict, or review-fetch first if the author pushed
    _write_state(state_path, state, "approval withdrawn")
    record_metric(number, "review_withdraw", was=previous)
    return result("WITHDRAWN", ticket=number, status_now=target, review_stage=review["stage"],
                  message="If the author pushed again: dt review-fetch N, then redo R3 and R4. Otherwise decide again with "
                          "dt review-verdict.", **({"db_companion": companion} if companion else {}))
