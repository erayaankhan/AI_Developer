"""Branch sync with master and the pushed check: sync-check, merge-master, sync-branch, pushed."""

import subprocess

from assembla_api import bare_number

from develop_ticket import common
from develop_ticket.common import BASE, _write_state, branch_name, record_metric, result


def _merge_expected(number: str, expected: bool) -> bool:
    """A merge is expected to be made by the developer (conflicts reported, or the master pull previewed). Set the mark
    (expected=True), or consume it (False). -> True when it was set: the merge has now happened, so the gates must run
    before any code is edited (a bad merge breaks the build here, not in the ticket's own change)."""
    state, path = common.load_state(number)
    if path is None:
        return False
    was = bool((state or {}).get("merge_expected"))
    if was != expected:
        if expected:
            state["merge_expected"] = True
        else:
            state.pop("merge_expected", None)
        _write_state(path, state, f"merge_expected={expected}")
    return was


def _verify_message(number: str) -> str:
    return (f"Run the gates now, before editing any code: `python local_gates.py --ticket {number}` (and risk_check on the LOW "
            "path). A bad merge breaks the build here, not in your own change.")


def cmd_sync_check(_api, args) -> int:
    """Step 7c: has master moved on since the branch was cut, and would it conflict now?"""
    number = bare_number(args.ticket)
    try:
        current = common.git("branch", "--show-current").stdout.strip()
        if current != branch_name(number):
            return result("WRONG_BRANCH", ticket=number, current_branch=current)
        common.git("fetch", "--quiet", "origin", BASE)
        ahead, behind, conflicts = common.master_sync_info()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, ticket=number, message=str(exc))
    status = "CONFLICTS" if conflicts else ("BEHIND" if behind else "UP_TO_DATE")
    return result(status, ticket=number, ahead=ahead, behind=behind, merge_conflicts=conflicts)


def cmd_merge_master(_api, args) -> int:
    """Step 8 (D-46): bring origin/master into FB_N, since the staging job no longer syncs it
    (QA generation is off, D-44).

    Order (developer decision): commit locally -> push to origin/FB_N -> pull origin/master
    into FB_N -> push again. So this refuses to run until the ticket commit is on origin.

    Reports how far behind master FB_N is and the files that would conflict, without touching
    anything, and gives the `git pull --no-rebase --no-edit origin master` command for the
    developer to run (it makes a merge commit; this script never writes history). --yes is
    accepted for old callers but changes nothing.
    """
    number = bare_number(args.ticket)
    branch = branch_name(number)
    try:
        current = common.git("branch", "--show-current").stdout.strip()
        if current != branch:
            return result("WRONG_BRANCH", ticket=number, current_branch=current)
        dirty = common.uncommitted_tracked()
        if dirty:
            return result("NOT_COMMITTED", ticket=number, files=dirty[:50],
                          message="Commit the ticket's changes before merging master.")
        pushed = common.push_state(number)
        if pushed["status"] != "PUSHED":
            return result("NOT_PUSHED_YET", ticket=number, push_state=pushed["status"],
                          message=f"Push the ticket commit to origin/{branch} first (order: commit -> push -> pull master -> push).")
        common.git("fetch", "--quiet", "origin", BASE)
        ahead, behind, conflicts = common.master_sync_info()
        if not behind:
            merged = _merge_expected(number, False)
            return result("UP_TO_DATE", ticket=number, ahead=ahead, verify_gates=merged,
                          **({"message": _verify_message(number)} if merged else {}))
        command = f"git pull --no-rebase --no-edit origin {BASE}"
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, ticket=number, message=str(exc))
    # The pull creates a merge commit, and the developer runs every git command that writes history:
    # this only reports. After they run `command`, a second call returns UP_TO_DATE with verify_gates: run the gates.
    _merge_expected(number, True)
    record_metric(number, "merge_master", status="PREVIEW", behind=behind, files=len(conflicts))
    note = ("`--yes` no longer runs the pull: the developer runs it. " if args.yes else "")
    return result("PREVIEW", ticket=number, behind=behind, merge_conflicts=conflicts, command=command,
                  message=note + ("The merge WILL conflict in the listed files: resolve before pulling."
                                  if conflicts else "No conflicts expected."))


def unmerged_files() -> list:
    return [l for l in common.git("diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines() if l.strip()]


def pull_merge(*pull_args) -> dict:
    """`git pull --no-rebase --no-edit <args>`. {"ok": True}, or {"ok": False, "conflicts": [...], "error": "..."}.
    A failed merge is aborted, so the working tree is exactly as it was (never left half-merged)."""
    proc = common.git("pull", "--no-rebase", "--no-edit", *pull_args, check=False, timeout=300)
    if proc.returncode == 0:
        return {"ok": True}
    conflicts = unmerged_files()
    if conflicts or common.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        common.git("merge", "--abort", check=False)
    return {"ok": False, "conflicts": conflicts, "error": (proc.stderr or proc.stdout).strip()[:300]}


def predict_conflicts(ref: str) -> list:
    """Files that would conflict if `ref` were merged into HEAD (no changes made). Raises if git can't tell."""
    mt = common.git("merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", ref, check=False)
    if mt.returncode == 1:
        return [l for l in mt.stdout.splitlines()[1:] if l.strip()]
    if mt.returncode != 0:
        raise RuntimeError(f"could not check for merge conflicts (git merge-tree exit {mt.returncode}; needs git >= 2.38)")
    return []


def cmd_sync_branch(_api, args) -> int:
    """Bring FB_N fully up to date and on origin: (1) pull origin/FB_N, (2) push origin/FB_N, (3) pull
    origin/master into FB_N, (4) push origin/FB_N. Run on resume before any code is written, and again
    before the ticket is handed off. A push happens only when there is something to push.

    The one place Claude runs pull / merge / push itself (the developer's explicit exception to "the developer
    runs git"): it makes merge commits but never commits work. Needs everything committed first. A conflict in
    either pull aborts that merge (tree unchanged) and is reported: the developer resolves it, then re-runs.
    Without --yes it only reports the plan.
    """
    number = bare_number(args.ticket)
    branch = branch_name(number)
    try:
        current = common.git("branch", "--show-current").stdout.strip()
        if current != branch:
            return result("WRONG_BRANCH", ticket=number, current_branch=current)
        dirty = common.uncommitted_tracked()
        if dirty:
            return result("NOT_COMMITTED", ticket=number, files=dirty[:50],
                          message="Commit (or stash) the changes first: a pull needs a clean tree.")
        common.git("fetch", "--quiet", "origin", BASE)
        common.git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
        remote_ref = f"refs/remotes/origin/{branch}"
        on_origin = common.git("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0
        behind_branch = int(common.git("rev-list", "--count", f"HEAD..{remote_ref}").stdout.strip() or 0) if on_origin else 0
        _ahead_m, behind_master, _ = common.master_sync_info()
        plan = {"behind_branch": behind_branch, "behind_master": behind_master, "branch_on_origin": on_origin}
        if not args.yes:
            return result("PREVIEW", ticket=number, **plan,
                          branch_conflicts=predict_conflicts(remote_ref) if behind_branch else [],
                          master_conflicts=predict_conflicts(f"origin/{BASE}") if behind_master else [],
                          message="Re-run with --yes to pull FB_N, push, pull master, push.")

        actions = []

        def push_if_needed():
            common.git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
            there = common.git("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0
            unpushed = int(common.git("rev-list", "--count", f"{remote_ref}..HEAD").stdout.strip() or 0) if there else 1
            if unpushed:
                common.git("push", *([] if there else ["-u"]), "origin", branch, timeout=300)
                actions.append(f"pushed origin/{branch}")

        for label, pull_args, needed in ((f"origin/{branch}", ("origin", branch), behind_branch),
                                         (f"origin/{BASE}", ("origin", BASE), behind_master)):
            if label.endswith(BASE):  # master may have moved, or the branch pull may have brought it in
                needed = common.master_sync_info()[1]
            if not needed:
                continue
            merged = pull_merge(*pull_args)
            if not merged["ok"]:
                record_metric(number, "sync_branch", status="CONFLICTS", step=label, files=len(merged["conflicts"]))
                if merged["conflicts"]:
                    _merge_expected(number, True)
                    return result("CONFLICTS", ticket=number, step=f"pull {label}", merge_conflicts=merged["conflicts"],
                                  done=actions, message="Merge aborted, nothing was changed by it. The developer "
                                  f"resolves these files (git pull --no-rebase origin {pull_args[1]}, fix, commit), then re-run.")
                return result("FAILED", 1, ticket=number, step=f"pull {label}", done=actions, message=merged["error"])
            actions.append(f"merged {label}")
            push_if_needed()  # push after each pull, so the branch pull is on origin before master comes in

        push_if_needed()  # nothing to pull: still make sure local commits are on origin
        head = common.git("rev-parse", "--short", "HEAD").stdout.strip()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, ticket=number, message=str(exc))
    status = "SYNCED" if actions else "UP_TO_DATE"
    record_metric(number, "sync_branch", status=status, actions=len(actions))
    resolved = _merge_expected(number, False)  # a merge the developer made after CONFLICTS shows up here as UP_TO_DATE
    verify = resolved or any(a.startswith("merged") for a in actions)
    return result(status, ticket=number, actions=actions, head=head, verify_gates=verify,
                  message=_verify_message(number) if verify else ("Only pushed: nothing was merged in." if actions else "Nothing to do."))


def cmd_pushed(_api, args) -> int:
    """Step 8: is everything committed and on origin before the ticket leaves the developer?"""
    number = bare_number(args.ticket)
    branch = branch_name(number)
    repo = common.DB_REPO if args.db else None  # --db: N is the DB ticket, checked in C2M_CI_DB
    try:
        current = common.git("branch", "--show-current", repo=repo).stdout.strip()
        if current != branch:
            return result("WRONG_BRANCH", ticket=number, current_branch=current)
        uncommitted = common.uncommitted_tracked(repo)
        if uncommitted:
            return result("NOT_COMMITTED", ticket=number, uncommitted=uncommitted[:50])
        common.git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False, repo=repo)
        remote = common.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False, repo=repo).stdout.strip()
        local = common.git("rev-parse", "HEAD", repo=repo).stdout.strip()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, ticket=number, message=str(exc))
    if not remote:
        return result("NOT_PUSHED", ticket=number, branch=branch, message="Branch is not on origin.")
    if remote != local:
        unpushed = int(common.git("rev-list", "--count", f"{remote}..HEAD", repo=repo).stdout.strip() or 0)
        unpulled = int(common.git("rev-list", "--count", f"HEAD..{remote}", repo=repo).stdout.strip() or 0)
        if unpushed:
            return result("NOT_PUSHED", ticket=number, branch=branch, unpushed_commits=unpushed,
                          origin_ahead_by=unpulled)
        return result("BEHIND_ORIGIN", ticket=number, branch=branch, origin_ahead_by=unpulled,
                      message="Everything local is pushed, but origin has newer commits - pull first.")
    return result("PUSHED", ticket=number, branch=branch, head=local[:12])
